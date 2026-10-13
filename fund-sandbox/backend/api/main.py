# -*- coding: utf-8 -*-
"""
fund-sandbox FastAPI 主服务
- 本机端口 8765（Electron 同源调用）
- 接入全部引擎模块（真实世界规则：外扣法 / 赎回阶梯 / T+1 / 15:00 截单 / 只投场外）
- 数据源失败自动降级，绝不抛 500（返回空/缺省，记录 data_health）
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import uvicorn

from ..db.schema import get_connection, get_db_path, init_db
from ..log_setup import setup as log_setup
from ..config import get, section, get_llm_config
from .. import engine
from ..engine import (
    fees, ledger, holdings, orders, portfolio, dividend, rules, status as fund_status,
    coordinator, dingtou, recovery, fx, inspector, rule_watch,
    triage, sector_news, intel_collector, rss_news, macro_data, llm_pool,
    news_quality, event_key, board_alias, audit, active_news, flash_news, time_lag,
    screen_rules, calendar, llm_client, ai_tools, analysis, review, screener, lag, news, health,
)
from ..data import fetcher


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"


@contextmanager
def conn():
    c = get_connection()
    try:
        yield c
    finally:
        c.close()


def _row(r) -> Optional[dict]:
    return {k: r[k] for k in r.keys()} if r else None


def _rows(rs) -> list:
    return [{k: r[k] for k in r.keys()} for r in rs] if rs else []


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 应用初始化
# ============================================================
app = FastAPI(title="fund-sandbox API", version="2.1.0", description="虚拟资金 + 真实规则的基金交易沙盒")
# ⚠️ 收紧 CORS：原为 `allow_origins=["*"] + allow_credentials=True` ——
# 服务虽只绑 127.0.0.1，但**浏览器里打开的任何网页**都能跨源打它；而这里有
# 下单/撤单/改配置等写操作接口，一旦被恶意页面跨源调用，后果直接落在账户上。
# 本项目是**单机 Electron + 本机浏览器**应用，来源只可能是本机 → 改用 regex 只放行
# 127.0.0.1 / localhost（任意端口，兼容 Electron 与 Vite dev server）。
# 若将来做手机端访问，请把手机侧来源显式加入白名单，**不要图省事改回 "*"**。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
def _startup():
    log_setup("backend")
    init_db()
    # 非阻塞：启动后处理到期单确认 + （可选）刷新
    threading.Thread(target=_post_startup, daemon=True).start()
    try:
        engine.scheduler.start_scheduler()
    except Exception:
        pass
    # RSS 资讯后台抓取线程（每 30 分钟一轮，抓完**自动提取事件**）。
    # ⚠️ 这一行是资讯/事件链路的关键（market_news / news_triage
    #    / sector_news 都依赖它）。rss_news.fetch_market_news 内部会调
    #    intel_collector.collect_from_market_news 提取事件，故起这一个线程即恢复整条链路。
    try:
        rss_news.start_background()
    except Exception as e:  # noqa: BLE001
        print(f"[启动] RSS 后台抓取线程启动失败：{type(e).__name__}: {e}")


def _post_startup():
    # 预热 akshare（性能优化）：首次 _import_akshare 实测 ~2.5 秒，
    # 不预热的话第一个触发抓取的请求会莫名卡几秒。放后台线程，**不占启动时间**。
    try:
        t = fetcher.warm_akshare()
        print(f"[启动] akshare 预热完成（{t:.1f}s，此后抓取不再付这段开销）")
    except Exception as e:  # noqa: BLE001
        print(f"[启动] akshare 预热失败（不影响运行，首次抓取会慢一次）：{e}")

    try:
        with conn() as c:
            orders.confirm_orders(c)
    except Exception:
        pass


@app.on_event("shutdown")
def _shutdown():
    try:
        engine.scheduler.get_scheduler().stop()
    except Exception:
        pass


# ============================================================
# Pydantic 模型
# ============================================================
class OrderRequest(BaseModel):
    fund_code: str
    side: str = "buy"
    amount: float
    reason: str = ""
    decision_source: str = "external"
    purpose: str = "buy"
    fund_name: Optional[str] = None
    fee_rate_discounted: bool = False


class RedeemPreviewRequest(BaseModel):
    fund_code: str
    shares: float


class AddFundRequest(BaseModel):
    code: str
    name: str = ""
    category: str = ""
    reason: str = ""


class DingtouRequest(BaseModel):
    fund_code: str
    amount: float
    frequency: str = "weekly"
    weekday: int = 1
    day_of_month: int = 1
    enabled: int = 1
    fund_name: Optional[str] = None


class AnalysisSubmitRequest(BaseModel):
    fund_code: str
    fund_name: str = ""
    order_id: Optional[str] = None
    decision_type: str = "buy"
    analysis: dict = Field(default_factory=dict)


class AIChatRequest(BaseModel):
    message: str
    context: str = ""


class ToolCallRequest(BaseModel):
    tool_name: str
    params: dict = Field(default_factory=dict)


class LLMConfigRequest(BaseModel):
    api_key: str = ""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    model: str = "glm-4-flash"


class SettingsRequest(BaseModel):
    settings: dict = Field(default_factory=dict)


# ============================================================
# 基础状态
# ============================================================
@app.get("/api/status")
def api_status():
    return {
        "status": "ok",
        "backend": "fund-sandbox",
        "version": "2.1.0",
        "host": "127.0.0.1",
        "port": 8765,
        "emergency_stop": coordinator.is_emergency_stop(),
    }


class EmergencyStopRequest(BaseModel):
    stop: bool
    note: str = ""


@app.post("/api/emergency-stop")
def api_emergency_stop(req: EmergencyStopRequest):
    payload = coordinator.set_emergency_stop(req.stop, req.note or "")
    return {"ok": True, **payload}


@app.get("/api/health")
def api_health():
    """持仓六维健康度（趋势25/持续性20/相对强度15/回撤15/估值10/舆情15，权重读 config）。

    注意：本接口是**持仓健康度**，数据源抓取状态是 /api/data-health，两者不是一回事。
    按 fund_code+as_of 幂等落库 position_health（重复访问只覆盖当天），
    逐日积累出的历史正是「持续性」维与四档处置硬门槛的依据。
    """
    try:
        return health.assess_positions()
    except Exception as e:
        return {"error": str(e), "total": 0, "ok": 0, "missing": 0, "items": []}


@app.get("/api/data-health")
def api_data_health():
    # 按 (source,data_type,status) 取每组最新一条的完整字段，
    # 让前端 Health.jsx 读到的 record_count / message / created_at 不再为空
    # （失败行的说明与时间必须可见）。
    with conn() as c:
        rows = c.execute(
            """
            SELECT source, data_type, status, record_count, message, created_at
            FROM data_health d
            WHERE id = (
                SELECT MAX(id) FROM data_health d2
                WHERE d2.source = d.source
                  AND d2.data_type = d.data_type
                  AND d2.status = d.status
            )
            ORDER BY created_at DESC
            LIMIT 40
            """
        ).fetchall()
    return {"items": _rows(rows), "ok": True}


# ── 诊断数据来源──
_PROCESS_STARTED_AT = datetime.now()  # 本进程启动时刻，用于算**真实**运行时长
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_LOG_DIR = _DATA_DIR / "logs"


def _read_json_file(path: Path) -> dict:
    """读一个 JSON 文件，失败返回 {}（诊断类接口不能因文件缺失就报错）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _count_crashes_today() -> int:
    """当日后端日志里的 `[CRASH]` 计数（`log_setup` 用 sys.excepthook 打的标记）。"""
    try:
        log_path = _LOG_DIR / f"backend-{date.today().isoformat()}.log"
        if not log_path.exists():
            return 0
        return sum(
            1
            for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if "[CRASH]" in line
        )
    except Exception:  # noqa: BLE001
        return 0


@app.get("/api/diagnostics")
def api_diagnostics():
    """运行诊断（崩溃留痕 / 自动恢复 / 真实运行时长）。

    ⚠️原实现 `crashes: 0, restarts: 0` 是**硬编码**，
    且 `uptime_hours = datetime.now().timestamp() / 3600` ≈ **49 万小时** —— 那是
    1970 年至今的小时数，不是进程运行时长，纯无意义。前端「软件运行状态」卡因此长期显示假数据。
    改为读真实来源：
      - 自动恢复次数 → `data/logs/watchdog-status.json` 的 `auto_restarts`（Electron 看护写入）
      - 当日崩溃数   → 当日日志的 `[CRASH]` 计数
      - 运行时长     → 本进程启动时刻
    """
    alive = HEARTBEAT_FILE.exists()
    wd = _read_json_file(_LOG_DIR / "watchdog-status.json")
    uptime = (datetime.now() - _PROCESS_STARTED_AT).total_seconds() / 3600
    return {
        "ok": True,
        "crashes": _count_crashes_today(),
        "restarts": int(wd.get("auto_restarts") or 0),
        "uptime_hours": round(uptime, 3),
        "backend_alive": True,
        "heartbeat_file": alive,
        "watchdog_healthy": wd.get("healthy"),
        "watchdog_updated_at": wd.get("updated_at"),
    }


# ============================================================
# 账户 / 持仓 / 订单
# ============================================================
@app.get("/api/account")
def api_account():
    with conn() as c:
        b = ledger.get_budget(c)
        positions = holdings.get_positions(c)
    mv = round(sum(p["current_value"] for p in positions), 2)
    cash = b["cash_balance"]
    return {
        "budget": b,
        # 原为硬编码 `{"alerts": []}` —— 而调度器 `_persist_risk_alert`
        # 明明把提醒写进了 `data/risk_alert.json`（注释还写着"供前端轮询展示"），
        # 前端却永远看不到提醒。改为真读该文件。
        "risk_alert": _read_json_file(_DATA_DIR / "risk_alert.json") or {"alerts": []},
        "account": {
            "initial": b["initial"], "cash": cash, "available": b["available"],
            "used_in_positions": b["used_in_positions"], "market_value": mv,
            "realized_pnl": b["realized_pnl"], "unrealized_pnl": b["unrealized_pnl"],
        },
        # 前端 Overview 顶层字段（契约对齐：市值用 nav 口径，非成本口径）
        "total_assets": round(mv + cash, 2),
        "market_value": mv,
        "position_count": len(positions),
        "cash": cash,
    }


@app.get("/api/positions")
def api_positions():
    with conn() as c:
        items = holdings.get_positions(c)
        for p in items:
            p["market_value"] = p["current_value"]                                # 前端资产配置环形图按 market_value 聚合
            p["lots"] = ledger.holding_report(c, p["code"], date.today())         # 前端 LOTS 批次卡按基金逐批
        total = sum(p["current_value"] for p in items)
    return {"items": items, "total": len(items), "market_value": round(total, 2)}


@app.get("/api/orders")
def api_orders(status: Optional[str] = Query(None), limit: int = Query(100)):
    with conn() as c:
        items = orders.get_orders(c, status=status, limit=limit)
    return {"items": items, "orders": items, "total": len(items)}


@app.post("/api/order")
def api_order(req: OrderRequest):
    # **入参校验**（原 `OrderRequest` 无任何约束）——
    # `side` 传任意字符串会走到 check_before_submit 的 else 分支被当成赎回；
    # `amount` 传 0 或负数会被当成有效金额（申购/赎回的负值都无意义，属脏输入）。
    if req.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"side 必须是 buy 或 sell，收到 {req.side!r}")
    if not (req.amount > 0):
        raise HTTPException(status_code=400, detail=f"amount 必须大于 0，收到 {req.amount!r}")
    with conn() as c:
        o = orders.submit_order(
            c, req.fund_code, req.side, req.amount, reason=req.reason,
            decision_source=req.decision_source, purpose=req.purpose,
            fund_name=req.fund_name, fee_rate_discounted=req.fee_rate_discounted,
        )
    # ⚠️`submit_order` 返回的是 `{"ok": bool, "reason": ...}`，
    # **根本没有 `status` 字段** —— 原判断 `o.get("status") == "rejected"` **恒为假**，
    # 于是拒单以 HTTP 200 返回、前端拿到后弹「下单成功」，用户以为买到了、实际被拒。
    # 改为按 `ok` 判定：`ok=False` 一律 400 抛错，让前端走失败分支。
    if not o.get("ok", False):
        raise HTTPException(
            status_code=400,
            detail=o.get("reason") or o.get("reject_reason") or "下单被拒",
        )
    return o


@app.post("/api/orders/{order_id}/cancel")
def api_cancel(order_id: str):
    with conn() as c:
        r = orders.cancel_order(c, order_id)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("reason", "撤销失败"))
    return r


@app.get("/api/portfolio/curve")
def api_portfolio_curve(limit: int = Query(365)):
    with conn() as c:
        r = portfolio.get_portfolio_curve(c, limit=limit)
    return r


@app.get("/api/portfolio/summary")
def api_portfolio_summary():
    with conn() as c:
        return portfolio.get_portfolio_summary(c)


@app.post("/api/portfolio/snapshot")
def api_portfolio_snapshot():
    with conn() as c:
        return portfolio.snapshot_portfolio(c)


@app.get("/api/portfolio/review")
def api_portfolio_review(period: str = "weekly"):
    try:
        return review.generate_review_report(period=period)
    except Exception as e:
        return {"period": period, "error": str(e), "themes": []}


# ============================================================
# 基金池
# ============================================================
# 申购状态展示映射：`fund_status` 存的是英文枚举（open/limited/closed），
# 而前端判断用的是中文（'限大额' / '暂停申购'）→ 在 API 层统一转成展示值。
_PS_ZH = {"open": "开放申购", "limited": "限大额", "closed": "暂停申购", "unknown": ""}


@app.get("/api/funds")
def api_funds(limit: int = Query(500)):
    with conn() as c:
        # 前端「起购 / 申购状态」一直显示「未知」，因为这两个字段原查询根本没返回
        # （min_purchase 在 funds 表、purchase_status 在 fund_status 表，都没进 SELECT）。
        # ⚠️ 说明写在 Python 层：SQL 的 `--` 注释会吞掉本行之后的内容（此处 SQL 是拼成一行的），会直接语法错。
        rows = c.execute(
            "SELECT f.code, f.name, f.category, f.is_qdii, f.status, f.add_reason, f.purchase_fee_rate, "
            "f.min_purchase, f.purchase_limit, "
            "(SELECT fs.purchase_status FROM fund_status fs WHERE fs.fund_code=f.code "
            " ORDER BY fs.status_date DESC, fs.id DESC LIMIT 1) AS purchase_status, "
            "(SELECT nav FROM nav_daily nd WHERE nd.fund_code=f.code ORDER BY nav_date DESC LIMIT 1) AS nav, "
            "(SELECT nav_date FROM nav_daily nd WHERE nd.fund_code=f.code ORDER BY nav_date DESC LIMIT 1) AS nav_date "
            "FROM funds f ORDER BY f.code LIMIT ?", (limit,)
        ).fetchall()
    items = _rows(rows)
    for it in items:
        v = it.get("purchase_status")
        it["purchase_status"] = _PS_ZH.get(v or "", v)
    return {"items": items, "total": len(items)}


@app.get("/api/funds/search")
def api_funds_search(q: str = Query(..., min_length=1)):
    with conn() as c:
        rows = c.execute(
            # 搜索结果卡片同样显示「起购 / 申购状态」，原查询只返回 4 个字段；
            # 另把 purchase_fee_rate 别名成 fee_rate（前端读 f.fee_rate）。
            "SELECT f.code, f.name, f.category, f.status, f.min_purchase, f.purchase_limit, "
            "f.purchase_fee_rate AS fee_rate, "
            "(SELECT fs.purchase_status FROM fund_status fs WHERE fs.fund_code=f.code "
            " ORDER BY fs.status_date DESC, fs.id DESC LIMIT 1) AS purchase_status "
            "FROM funds f WHERE f.code LIKE ? OR f.name LIKE ? ORDER BY f.code LIMIT 50",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    items = _rows(rows)
    for it in items:  # 同 /api/funds：英文枚举 → 中文展示值
        v = it.get("purchase_status")
        it["purchase_status"] = _PS_ZH.get(v or "", v)
    return {"items": items, "total": len(items)}


@app.post("/api/funds/add")
def api_funds_add(req: AddFundRequest):
    if not req.reason:
        raise HTTPException(status_code=400, detail="添加基金必须填写 reason")
    with conn() as c:
        r = holdings.add_fund(req.code, req.name, req.category, req.reason, reactivate=False, conn=c)
    return r


@app.delete("/api/funds/{code}")
def api_funds_delete(code: str):
    with conn() as c:
        c.execute("UPDATE funds SET status='delisted', updated_at=? WHERE code=?", (_now(), code))
        c.commit()
    return {"ok": True, "code": code, "status": "delisted"}


@app.get("/api/fund/{code}")
def api_fund_detail(code: str):
    with conn() as c:
        f = c.execute("SELECT * FROM funds WHERE code=?", (code,)).fetchone()
        if not f:
            raise HTTPException(status_code=404, detail="基金不存在")
        series = _rows(c.execute(
            "SELECT nav_date, nav, acc_nav FROM nav_daily WHERE fund_code=? ORDER BY nav_date", (code,)
        ).fetchall())
        pos = [p for p in holdings.get_positions(c) if p["code"] == code]
    return {"fund": _row(f), "series": series[-365:], "position": pos[0] if pos else None}


@app.post("/api/fund/{code}/purchase")
def api_fund_purchase(code: str, amount: float, reason: str = "", decision_source: str = "external"):
    with conn() as c:
        o = orders.submit_order(c, code, "buy", amount, reason=reason, decision_source=decision_source)
    if o.get("status") == "rejected":
        raise HTTPException(status_code=400, detail=o.get("reject_reason"))
    return o


@app.get("/api/fund/{code}/announcements")
def api_fund_announcements(code: str, limit: int = Query(20)):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM news_feed WHERE fund_code=? ORDER BY fetched_at DESC LIMIT ?",
            (code, limit),
        ).fetchall()
    return {"items": _rows(rows), "total": len(rows)}


@app.post("/api/redeem/preview")
def api_redeem_preview(req: RedeemPreviewRequest):
    with conn() as c:
        return ledger.preview_consume_lots(c, req.fund_code, req.shares)


@app.post("/api/funds/refresh")
def api_funds_refresh():
    try:
        r = fetcher.refresh_nav_all()
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# 定投
# ============================================================
@app.get("/api/dingtou/plans")
def api_dingtou_plans():
    with conn() as c:
        return {"items": dingtou.get_plans(c), "total": len(dingtou.get_plans(c))}


@app.post("/api/dingtou/plans")
def api_dingtou_add(req: DingtouRequest):
    # ⚠️原写法 `add_plan(c, ..., req.day_of_month, req.enabled, fund_name=req.fund_name)`
    # 把 `req.enabled` 当**第 7 个位置参数**传了进去 —— 而该位置是 `fund_name`，
    # 且又用关键字传了一次 `fund_name` → 必然 `TypeError: got multiple values for 'fund_name'`，
    # 即「新增定投计划」接口**从来没能成功过**。改为全部走关键字传参。
    with conn() as c:
        return dingtou.add_plan(
            c, req.fund_code, req.amount, req.frequency, req.weekday,
            req.day_of_month, fund_name=req.fund_name, enabled=req.enabled,
        )


@app.put("/api/dingtou/plans/{plan_id}")
def api_dingtou_update(plan_id: int, req: DingtouRequest):
    with conn() as c:
        return dingtou.update_plan(
            c, plan_id, fund_code=req.fund_code, amount=req.amount, frequency=req.frequency,
            weekday=req.weekday, day_of_month=req.day_of_month, enabled=req.enabled,
        )


@app.delete("/api/dingtou/plans/{plan_id}")
def api_dingtou_delete(plan_id: int):
    with conn() as c:
        return dingtou.delete_plan(c, plan_id)


@app.post("/api/dingtou/run")
def api_dingtou_run():
    with conn() as c:
        return dingtou.run_dingtou(c)


# ============================================================
# 板块 / 暴露
# ============================================================
@app.get("/api/boards")
def api_boards(limit: int = Query(12)):
    with conn() as c:
        rows = c.execute(
            "SELECT sector, AVG(net_inflow) AS net, MAX(fetched_at) AS t FROM sector_news "
            "GROUP BY sector ORDER BY t DESC LIMIT ?", (limit,)
        ).fetchall()
    items = [{"sector": r["sector"], "net_inflow": round(r["net"] or 0, 2)} for r in rows]
    if not items:
        items = [{"sector": "暂无板块数据", "net_inflow": 0.0}]
    return {"items": items, "total": len(items)}


@app.get("/api/boards/all")
def api_boards_all():
    """**全部**板块资金流（分析页「板块资金流」卡片点击弹窗用）。

    走 `build_boards_payload(limit=0)` 的**全量分支**：所有行业板块按主力净流入排序，
    正=流入 / 负=流出，并附涨跌幅（与卡片默认视图的「流入前6+流出前6」同一数据源与口径）。
    """
    return analysis.build_boards_payload(limit=0)


@app.get("/api/exposure")
def api_exposure():
    with conn() as c:
        pos = holdings.get_positions(c)
        by_cat: dict[str, float] = {}
        for p in pos:
            by_cat[p.get("category") or "未知"] = by_cat.get(p.get("category") or "未知", 0.0) + p["current_value"]
        total = sum(by_cat.values()) or 1.0
        breakdown = [{"industry": k, "weight": round(v / total * 100, 2)} for k, v in by_cat.items()]
    return {"industry_breakdown": breakdown, "risk_score": len(breakdown), "concentration_risk": "低" if len(breakdown) <= 3 else "中"}


# ============================================================
# 分析 / 筛选 / 信号
# ============================================================
@app.get("/api/analysis")
def api_analysis():
    """分析页数据（契约见前端 Analysis.jsx 读的键）：
    `boards`（板块资金流）/ `heatmap`（板块热力图）/ `exposure`（组合行业暴露）/
    `alloc_data`（环形图）/ `intel_summary` / `narrative`（人读叙述）/ `overlaps`。

    ⚠️原实现是**硬编码桩** ——
    `{"overall_score": 0, "boards": [], ...}`，导致分析页整页全是
    「暂无板块资金 / 暂无板块信号 / 无数据」。现接回 `engine.analysis` 的真实函数。
    """
    try:
        with conn() as c:
            boards = analysis.build_boards_payload(limit=6)
            exposure = analysis.build_exposure_payload(c)
        exposures = (exposure or {}).get("per_fund") or []
        narrative = analysis.build_narrative(boards, exposures, [])
        return {
            "boards": boards,
            "heatmap": boards,          # 板块热力图与资金流同源（前端两处展示）
            "exposure": exposure,
            "alloc_data": ((exposure or {}).get("portfolio") or {}).get("allocation") or [],
            "intel_summary": {},
            "narrative": narrative,
            "overlaps": [],
            "update_time": _now(),
        }
    except Exception as e:  # noqa: BLE001 — 分析页不该因单点失败整页空白
        return {"error": str(e), "boards": {"count": 0, "items": []},
                "exposure": {}, "update_time": _now()}


@app.post("/api/analyze")
def api_analyze(code: str = "", name: str = ""):
    if not code:
        return {"error": "缺少 fund_code"}
    try:
        return analysis.analyze_fund(code)
    except Exception as e:
        return {"fund_code": code, "error": str(e)}


@app.post("/api/analysis/submit")
def api_analysis_submit(req: AnalysisSubmitRequest):
    with conn() as c:
        a = req.analysis or {}
        c.execute(
            "INSERT INTO analysis_log (fund_code, fund_name, order_id, decision_type, "
            "valuation, decision_detail, strategy, decision, full_analysis_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (req.fund_code, req.fund_name, req.order_id, req.decision_type,
             json.dumps(a.get("valuation", {}), ensure_ascii=False),
             json.dumps(a.get("decision_detail", {}), ensure_ascii=False),
             json.dumps(a.get("strategy", {}), ensure_ascii=False),
             json.dumps(a.get("decision", {}), ensure_ascii=False),
             json.dumps(a, ensure_ascii=False), _now()),
        )
        c.commit()
        return {"status": "saved", "fund_code": req.fund_code}


@app.get("/api/analysis/log")
def api_analysis_log(limit: int = Query(50), fund_code: str = Query(None)):
    with conn() as c:
        if fund_code:
            rows = c.execute(
                "SELECT * FROM analysis_log WHERE fund_code=? ORDER BY created_at DESC LIMIT ?",
                (fund_code, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM analysis_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return {"entries": _rows(rows), "total": len(rows)}


@app.get("/api/analysis/{code}")
def api_analysis_history(code: str):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM analysis_log WHERE fund_code=? ORDER BY created_at DESC LIMIT 20", (code,)
        ).fetchall()
    return {"analyses": _rows(rows), "total": len(rows)}


@app.get("/api/screener/results")
def api_screener(days: int = Query(3), top_n: int = Query(20)):
    try:
        cands = screener.screen_all()
        return {"results": cands[:top_n], "days": days, "top_n": top_n, "total": len(cands)}
    except Exception as e:
        return {"results": [], "error": str(e), "days": days, "top_n": top_n}


@app.get("/api/review/themes")
def api_review_themes(days: int = Query(20), top_n: int = Query(12)):
    try:
        rep = review.get_portfolio_summary()
        return {"themes": [], "summary": rep, "days": days, "top_n": top_n}
    except Exception:
        return {"themes": [], "days": days, "top_n": top_n}


@app.get("/api/lag/opportunities")
def api_lag(hours: int = Query(24), top_n: int = Query(15)):
    try:
        return {"opportunities": lag.detect_valley_signals(), "hours": hours, "top_n": top_n}
    except Exception as e:
        return {"opportunities": [], "error": str(e)}


@app.get("/api/signals/valley")
def api_signals_valley():
    try:
        signals = lag.detect_valley_signals()
        reversals = [lag.detect_trend_reversal(s["fund_code"]) for s in signals]
        return {"opportunities": signals, "signals": reversals}
    except Exception as e:
        return {"opportunities": [], "error": str(e)}


# ============================================================
# 新闻 / 情报 / 研判
# ============================================================
@app.get("/api/news")
def api_news(limit: int = Query(60)):
    # 仅按真实持仓过滤
    with conn() as c:
        codes = [r["code"] for r in c.execute(
            "SELECT DISTINCT fund_code AS code FROM lots WHERE status='active'").fetchall()]
        if not codes:
            return {"items": [], "total": 0}
        ph = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT * FROM news_feed WHERE fund_code IN ({ph}) ORDER BY fetched_at DESC LIMIT ?",
            (*codes, limit),
        ).fetchall()
    return {"items": _rows(rows), "total": len(rows)}


@app.get("/api/market-news")
def api_market_news(limit: int = Query(50)):
    with conn() as c:
        rows = c.execute("SELECT * FROM market_news ORDER BY pub_time DESC LIMIT ?", (limit,)).fetchall()
    return {"items": _rows(rows), "total": len(rows), "date": _today()}


@app.get("/api/sector-news")
def api_sector_news(board_type: str = Query("industry"), limit: int = Query(40)):
    with conn() as c:
        return {"items": sector_news.get_sector_news(c, board_type=board_type, limit=limit), "total": 0}


@app.post("/api/news/refresh")
def api_news_refresh():
    """手动刷新 RSS 资讯（**抓 + 落库 + 提取事件**）。

    ⚠️原调 `news.fetch_market_news()` —— 该函数**只抓不存**，
    刷新恒返回 fetched>0 但**库不更新**。真正的「抓+存」在 `rss_news.fetch_market_news(conn)`。
    """
    try:
        with conn() as c:
            saved = rss_news.fetch_market_news(c)
        return {"fetched": saved, "saved": saved, "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"fetched": 0, "saved": 0, "ok": False, "error": str(e)}


@app.post("/api/market-news/refresh")
def api_market_news_refresh():
    """同上（前端「刷新抓取」按钮走这条）。"""
    try:
        with conn() as c:
            saved = rss_news.fetch_market_news(c)
        return {"fetched": saved, "saved": saved, "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"fetched": 0, "saved": 0, "ok": False, "error": str(e)}


@app.post("/api/sector-news/refresh")
def api_sector_news_refresh():
    with conn() as c:
        return sector_news.refresh_sector_news(c)


@app.get("/api/triage")
def api_triage(category: str = Query("all"), limit: int = Query(300)):
    with conn() as c:
        return {"items": triage.get_triage(c, category=category, limit=limit), "total": 0}


@app.post("/api/triage/run")
def api_triage_run():
    with conn() as c:
        return triage.run_triage(c)


@app.get("/api/triage/stream")
def api_triage_stream(limit: int = Query(50)):
    with conn() as c:
        return {"items": triage.stream_triage(c, limit=limit), "total": 0}


@app.get("/api/intel")
def api_intel(limit: int = Query(50)):
    with conn() as c:
        return {"items": intel_collector.get_intel_events(c, limit=limit), "total": 0}


@app.post("/api/intel/collect")
def api_intel_collect():
    with conn() as c:
        return intel_collector.collect_intel(c)


@app.get("/api/decisions")
def api_decisions(limit: int = Query(20)):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE decision_source IS NOT NULL ORDER BY order_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": _rows(rows), "total": len(rows)}


@app.get("/api/decision-feed")
def api_decision_feed(limit: int = Query(20)):
    with conn() as c:
        rows = c.execute("SELECT * FROM analysis_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": _rows(rows), "total": len(rows)}


# ============================================================
# 纪律线
# ============================================================
@app.get("/api/discipline-alerts")
def api_discipline_alerts():
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM discipline_alerts WHERE resolved_at IS NULL ORDER BY triggered_at DESC"
        ).fetchall()
    return {"items": _rows(rows), "total": len(rows)}


@app.post("/api/discipline-alerts/ack")
def api_discipline_ack(alert_id: int):
    with conn() as c:
        c.execute("UPDATE discipline_alerts SET ack=1, resolved_at=? WHERE id=?", (_now(), alert_id))
        c.commit()
    return {"ok": True, "id": alert_id}


@app.get("/api/health/{code}")
def api_fund_health(code: str):
    return health.check_nav_health(code)


# ============================================================
# 汇率
# ============================================================
@app.get("/api/screen/candidates")
def api_screen_candidates(days: int = 3, top_n: int = 5, fetch: bool = False):
    """对「新闻导向候选」跑**项目筛选规则**：估值分位 / 近1年收益 / 最大回撤。

    规则：分位 <50% + ret_1y >0 + 最大回撤 <50%；买入前额外 <30%。
    ⚠️ **只给事实与达标与否，不推荐买哪只** —— 买不买由接管 agent 判断。

    参数：fetch=True 才补抓缺失净值（慢，分钟级）；默认只读本地净值。
    """
    from backend.engine import screen_rules as _sr
    from backend.engine import screener as _sc

    conn = get_connection()
    try:
        days = max(1, min(days, 15))
        top_n = max(1, min(top_n, 20))
        cand = _sc.build_candidates(conn, days=days, top_n=top_n)
        codes, meta = [], {}
        for t in cand.get("themes", []):
            for f in t.get("funds", []):
                code = f.get("code")
                if code and code not in meta:
                    meta[code] = {"name": f.get("name"), "theme": t.get("theme")}
                    codes.append(code)
        fetched = _sr.ensure_nav(conn, codes) if (fetch and codes) else None
        items = [{**r, **meta.get(r["code"], {})} for r in _sr.evaluate_many(conn, codes)]
        items.sort(key=lambda x: (not x["data_ok"], not (x.get("pass_screen") or False),
                                  -(x.get("ret_1y") or 0)))
        return {
            "ok": True,
            "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "evaluated": len(items),
            "data_ok": sum(1 for i in items if i["data_ok"]),
            "passed": sum(1 for i in items if i.get("pass_screen")),
            "fetched": fetched, "items": items,
            "note": "只给筛选事实与达标判断，不推荐买哪只；数据不足的标 data_ok=false（不硬算）",
        }
    finally:
        conn.close()


@app.get("/api/rule-changes")
def api_rule_changes(limit: int = 50):
    """交易规则变化留痕：**申购状态 / 限购额度 / 赎回费什么时候变的**。

    规则不定时在变，需要机制预防。本端点给出变化证据链。
    kind：`purchase_status`（申购状态+日限额）/ `redeem_fee`（赎回费阶梯）。
    old_value 为 null = 首次记录到的基线，不是"从无到有"的变化。
    """
    from backend.engine import rule_watch as _rw

    conn = get_connection()
    try:
        return {"ok": True, "count": 0, "items": _rw.get_rule_changes(conn, limit=limit)}
    finally:
        conn.close()


@app.get("/api/review/calibration")
def api_review_calibration(days: int = 60, top_n: int = 30):
    """映射校准报告：把「事件 → 之后表现」归纳成统计，验证「新闻是主信号」这一核心假设。

    ⚠️ **只归纳统计事实，不自动改映射规则**（改不改由 AI/人判断）。
    verdict：`up` / `down` / `nodata`（没进过热点榜、无价格轨迹，不编造）。
    """
    from backend.engine import review as _rv

    conn = get_connection()
    try:
        days = max(7, min(days, 180))
        top_n = max(1, min(top_n, 50))
        return {"ok": True, **_rv.build_calibration(conn, days=days, top_n=top_n)}
    finally:
        conn.close()


@app.get("/api/fx")
def api_fx(days: int = Query(30), history_days: int = Query(180)):
    with conn() as c:
        latest = fx.latest_usd_cny(c)
    return {**latest, "history": [], "latest_date": latest.get("date"), "span_days": days}


@app.get("/api/rate/usd_cny")
def api_rate_usd_cny():
    with conn() as c:
        return fx.latest_usd_cny(c)


@app.get("/api/dividends")
def api_dividends(limit: int = Query(200, ge=1, le=1000)):
    with conn() as c:
        rows = dividend.get_dividends(c, limit=limit)
    return {"results": rows, "count": len(rows)}


@app.post("/api/fund/{code}/dividends/method")
def api_fund_dividend_method(code: str, method: str):
    """设置分红方式（cash=现金分红 / reinvest=红利再投）。"""
    with conn() as c:
        res = dividend.set_dividend_method(c, code, method)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("reason"))
    return res


@app.post("/api/fund/{code}/dividends/process")
def api_fund_dividend_process(code: str):
    """识别该基金历史分红并落库（幂等，重复调用不重复入账）。"""
    with conn() as c:
        return dividend.process_dividends(c, code)


# ============================================================
# AI 接口（降级安全）
# ============================================================
@app.post("/api/ai/chat")
async def api_ai_chat(req: AIChatRequest):
    try:
        reply = await llm_client.simple_chat(req.message, req.context)
        return {"content": reply, "configured": llm_client.LLMClient().is_configured()}
    except Exception as e:
        return {"content": f"[本地降级] 无法调用 LLM：{e}", "configured": False}


@app.post("/api/ai/summarize")
def api_ai_summarize(text: str = ""):
    return {"summary": "（摘要接口：需配置 LLM 密钥后启用）"}


@app.post("/api/ai/screener")
def api_ai_screener():
    return {"recommendations": []}


@app.post("/api/ai/discipline-check")
def api_ai_discipline_check():
    return {"alerts": []}


@app.get("/api/ai/tools")
def api_ai_tools():
    return {"tools": ai_tools.get_tools()}


@app.post("/api/ai/tools/call")
async def api_ai_tool_call(req: ToolCallRequest):
    try:
        return await ai_tools.call_tool(req.tool_name, req.params)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/config")
def api_ai_config():
    cfg = get_llm_config()
    return {"has_key": cfg["has_key"], "model": cfg["model"], "base_url": cfg["base_url"]}


@app.post("/api/ai/config")
def api_ai_config_set(req: LLMConfigRequest):
    import base64, hashlib, platform, uuid
    mac = uuid.getnode()
    hostname = platform.node()
    raw = f"{mac}|{hostname}|fund-sandbox-v1"
    key = hashlib.sha256(raw.encode()).digest()
    enc = base64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(req.api_key.encode()))).decode()
    cfg = {
        "encrypted_key": enc, "key_hint": req.model, "base_url": req.base_url, "model": req.model,
        "system_prompt": "你是 fund-sandbox 的基金投资助手。基于账户状态、持仓、新闻、情报回答问题。简洁、可执行；不确定就说'我不确定'。",
        "created_at": datetime.now().isoformat(),
    }
    (DATA_DIR / "llm_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "saved", "model": req.model}


@app.post("/api/ai/config/clear")
def api_ai_config_clear():
    p = DATA_DIR / "llm_config.json"
    if p.exists():
        p.unlink()
    return {"status": "cleared"}


@app.get("/api/ai/config/status")
def api_ai_config_status():
    cfg = get_llm_config()
    return {"has_key": cfg["has_key"], "model": cfg["model"], "base_url": cfg["base_url"]}


# ============================================================
# 设置（meta 表 + config 默认）
# ============================================================
_SETTINGS_SCHEMA = {
    "profit_take_pct": float, "drawdown_cut_pct": float, "stop_loss_pct": float,
    "max_single_position_pct": float, "max_orders_per_day": int, "max_amount_per_order": float,
    "max_drawdown_stop_pct": float, "purchase_discount": float,
}


@app.get("/api/settings")
def api_settings():
    defaults = {
        "profit_take_pct": get("rules", "profit_take_pct"),
        "drawdown_cut_pct": get("rules", "drawdown_cut_pct"),
        "stop_loss_pct": get("rules", "stop_loss_pct"),
        "max_single_position_pct": get("risk", "max_single_position_pct"),
        "max_orders_per_day": get("risk", "max_orders_per_day"),
        "max_amount_per_order": get("risk", "max_amount_per_order"),
        "max_drawdown_stop_pct": get("risk", "max_drawdown_stop_pct"),
        "purchase_discount": get("fee", "purchase_discount"),
        "heartbeat_timeout_hours": get("decision", "heartbeat_timeout_hours"),
        "official_start_month": get("capital", "official_start_month"),
    }
    with conn() as c:
        rows = c.execute("SELECT key, value FROM meta").fetchall()
    overrides = {r["key"]: r["value"] for r in rows}
    return {**defaults, **overrides}


@app.put("/api/settings")
def api_settings_put(req: SettingsRequest):
    with conn() as c:
        for k, v in req.settings.items():
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        c.commit()
    return {"ok": True, "updated": list(req.settings.keys())}


# ============================================================
# 恢复 / 快照
# ============================================================
@app.get("/api/recovery/snapshots")
def api_recovery_snapshots():
    return recovery.list_snapshots()


@app.post("/api/recovery/snapshot")
def api_recovery_snapshot():
    return recovery.create_snapshot()


@app.post("/api/recovery/restore")
def api_recovery_restore(name: str):
    return recovery.restore_snapshot(name)


@app.post("/api/recovery/prune")
def api_recovery_prune():
    return recovery.prune()


# ============================================================
# 心跳 / 外部打开
# ============================================================
@app.post("/api/heartbeat")
def api_heartbeat(source: str = "ai", note: str = ""):
    try:
        payload = {"last_seen": datetime.now().isoformat(), "source": source, "note": note}
        HEARTBEAT_FILE.write_text(json.dumps(payload), encoding="utf-8")
        return {"ok": True, **payload}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/open-external")
def api_open_external(path: str = ""):
    target = Path(path) if path else DATA_DIR
    return {"path": str(target), "exists": target.exists()}


# ============================================================
# 刷新（数据）
# ============================================================
@app.post("/api/refresh/nav")
def api_refresh_nav():
    try:
        return fetcher.refresh_nav_all()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/refresh/news")
def api_refresh_news():
    try:
        with conn() as c:
            saved = rss_news.fetch_market_news(c)
        return {"ok": True, "fetched": saved, "saved": saved}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/refresh/all")
def api_refresh_all():
    # ⚠️ 局部变量不得叫 news —— 会**遮蔽**模块级 import 的 `news` 模块，
    # 使 news.fetch_market_news() 变成 dict.fetch_market_news → AttributeError。
    # （改名后不再遮蔽）
    nav_res: dict = {}
    news_res: dict = {}
    try:
        nav_res = fetcher.refresh_nav_all()
    except Exception as e:
        nav_res = {"error": str(e)}
    try:
        with conn() as c:
            saved = rss_news.fetch_market_news(c)
        news_res = {"fetched": saved, "saved": saved}
    except Exception as e:
        news_res = {"error": str(e)}
    return {"nav": nav_res, "news": news_res}


# ============================================================
# 前端静态资源（frontend/dist）+ SPA 兜底 —— 必须在所有 API 路由之后注册
# Electron 窗口加载 http://127.0.0.1:8765/ ，由后端直接吐出构建好的前端
# ============================================================
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_INDEX = _DIST / "index.html"

if _INDEX.exists():
    _ASSETS = _DIST / "assets"
    if _ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    def _favicon():
        return FileResponse(str(_DIST / "favicon.svg"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str):
        # API 未匹配路径仍返回 404 JSON（不吞进前端）
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # 真实存在的静态文件直接返回，其余统一走 index.html（前端路由）
        candidate = (_DIST / full_path).resolve()
        if full_path and str(candidate).startswith(str(_DIST)) and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_INDEX))


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    host = get("decision", "host", "127.0.0.1")
    port = int(get("decision", "port", 8765))
    uvicorn.run(app, host=host, port=port, log_level="info")
