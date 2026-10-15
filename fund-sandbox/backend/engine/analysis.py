"""组合分析：把持仓穿透、板块信号、情报汇总成「可视化数据 + 人读叙述」。

设计约定（与全局一致）：
- 只有资金是虚拟的；净值、持仓、行业配置、板块资金流、基金状态
  全部来自真实世界（akshare），抓取失败就标「无数据」，绝不编造。
- 本模块只做「取数 + 组装 + 写人话」，不替用户做买卖决定。
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from backend.config import get
from backend.data import fetcher
from backend.engine import holdings as _holdings
from backend.engine import calendar as _cal, fund_pool

# 行业配置抓取较慢（每次调 akshare），按基金缓存到本地文件，24 小时内复用。
# 注意：不放进 fund_holdings 表——那张表存的是个股持仓，与行业配置字段不同。
_EXPOSURE_CACHE = Path(__file__).resolve().parents[2] / "data" / "exposure_cache.json"
_EXPOSURE_TTL = 86400


def _load_cache() -> dict:
    try:
        if _EXPOSURE_CACHE.exists():
            return json.loads(_EXPOSURE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _EXPOSURE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _EXPOSURE_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# =============================================================================
# 板块信号
# =============================================================================


def build_boards_payload(
    limit: int = 6,
    board_type: str = "industry",
    concept_names: list[str] | None = None,
) -> dict:
    """板块资金信号载荷，含数值（亿元）供图表使用。

    估值分位依赖东财板块接口（当前失效），因此只给资金流方向与幅度，
    不判断便宜/昂贵——宁可少一个维度也不拿没有的数据编结论。

    board_type：
      - `"industry"`（默认）：行业板块
      - `"concept"`：概念板块（须传 `concept_names`，由调用方按新闻热度筛 Top N）
    """
    boards, source = fetcher.fetch_boards(board_type=board_type, concept_names=concept_names)
    # 板块资金流是实时行情，反映最近一个交易日；用于前端显示数据日期。
    # 取「截至今天的最近交易日」：若今天就是交易日则取今天，否则回退到上一交易日。
    try:
        # 板块资金流是实时行情，反映最近一个交易日（含今天）：取今天，若今天非交易日则回退到上一交易日。
        # prev_trading_day 从 d-1 起算，故对「今天是否交易日」做判断后取较近者。
        cal = _cal.TradeCalendar()
        today = date.today()
        as_of = (today if cal.is_trading_day(today) else cal.prev_trading_day(today)).isoformat()
    except Exception:
        as_of = date.today().isoformat()
    if not boards:
        return {
            "count": 0, "items": [], "source": source, "as_of": as_of,
            "note": "板块数据源全部不可用，不展示任何信号",
        }

    ranked = sorted(
        boards, key=lambda b: b.get("main_net_inflow") or 0, reverse=True
    )
    # 去重：同花顺偶尔同一板块返回多条，按名称保留金额最大的一条
    seen: dict[str, dict] = {}
    for b in ranked:
        name = b.get("board_name")
        if name in seen:
            if (b.get("main_net_inflow") or 0) > (seen[name].get("main_net_inflow") or 0):
                seen[name] = b
            continue
        seen[name] = b
    ranked = list(seen.values())

    if limit and limit > 0:
        # 默认视图：热门(流入前 N) + 危险(流出前 N) 两榜
        items = []
        hot_names: set[str] = set()
        for b in ranked[:limit]:
            v = b.get("main_net_inflow") or 0
            hot_names.add(b["board_name"])
            items.append({
                "name": b["board_name"], "kind": "hot",
                "value": round(v, 2),
                "text": "钱进来 %.2f 亿" % v,
            })
        # 流出榜：跳过已进流入榜的板块，避免板块总数较少时同一板块同时出现在两侧。
        for b in ranked[-limit:]:
            if b["board_name"] in hot_names:
                continue
            v = b.get("main_net_inflow") or 0
            items.append({
                "name": b["board_name"], "kind": "risk",
                "value": round(v, 2),
                "text": "钱走了 %.2f 亿" % abs(v),
            })
        return {
            "count": len(items), "source": source, "all": False, "as_of": as_of, "items": items,
            "note": "估值分位需东财板块接口（当前失效），暂不判断便宜/昂贵，只展示资金流方向",
        }
    # 全量视图(limit<=0)：所有板块按主力净流入排序，正=流入、负=流出，附涨跌幅。
    items = []
    for b in ranked:
        v = b.get("main_net_inflow") or 0
        cp = b.get("change_pct")
        items.append({
            "name": b["board_name"],
            "kind": "hot" if v >= 0 else "risk",
            "value": round(v, 2),
            "change_pct": round(cp, 2) if isinstance(cp, (int, float)) else cp,
            "text": ("钱进来 %.2f 亿" % v) if v >= 0 else ("钱走了 %.2f 亿" % abs(v)),
        })
    return {
        "count": len(items), "source": source, "all": True, "as_of": as_of, "items": items,
        "note": "全部板块按主力净流入排序（亿元），正负表示资金流入/流出",
    }


# =============================================================================
# 持仓穿透：行业暴露 + 风格漂移
# =============================================================================


def exposure_for(conn, fund_code: str, fund_name: str) -> dict:
    """单基金的行业暴露与风格漂移。结果按基金缓存（24h 复用），避免每次重抓。"""
    cache = _load_cache()
    entry = cache.get(fund_code)
    now = time.time()
    if entry and (now - entry.get("ts", 0)) < _EXPOSURE_TTL and "exp" in entry:
        exp = entry["exp"]
    else:
        exp = _holdings.analyze_exposure(fund_code)
        cache[fund_code] = {"ts": now, "exp": exp}
        _save_cache(cache)

    drift = _holdings.detect_style_drift(fund_name, exp.get("allocation", []))
    return {**exp, "fund_code": fund_code, "fund_name": fund_name, "drift": drift}


def held_funds(conn) -> list[dict]:
    """当前**真正持有**的基金（lots 有剩余份额的）——持仓穿透的驱动口径。

    历史实现先遍历「交易池」再按份额过滤（`for f in pool_funds(): if shares<=0: continue`），
    导致两个后果：
      ① 持仓里有、池里没有的基金，行业暴露里完全不出现；
      ② 交易池被清空后，明明有持仓，整块却显示「暂无持仓」。
    持仓是比「关注」更强的信号 —— 穿透必须以持仓为准，与交易池无关。
    """
    rows = conn.execute(
        "SELECT DISTINCT l.fund_code AS code, "
        "       COALESCE(f.name, l.fund_code) AS name "
        "FROM lots l LEFT JOIN funds f ON f.code = l.fund_code "
        "WHERE l.status='active' AND l.remaining_shares > 0 "
        "ORDER BY l.fund_code"
    ).fetchall()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def portfolio_exposure(conn) -> dict:
    """组合层面行业暴露：按当前持仓市值加权合并各基金行业占比。"""
    items = []
    for f in held_funds(conn):
        code = f["code"]
        shares = conn.execute(
            "SELECT COALESCE(SUM(remaining_shares), 0) AS s FROM lots "
            "WHERE fund_code=? AND status='active'",
            (code,),
        ).fetchone()["s"]
        if shares <= 0:
            continue
        exp = exposure_for(conn, code, f["name"])
        if not exp.get("allocation"):
            continue
        nav_row = conn.execute(
            "SELECT nav AS v FROM nav_daily "
            "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1",
            (code,),
        ).fetchone()
        nav = nav_row["v"] if nav_row and nav_row["v"] else 0
        mv = shares * nav
        for a in exp["allocation"]:
            items.append({"industry": a["industry"], "weight": a["weight"], "mv": mv})

    total_mv = sum(i["mv"] for i in items) or 1.0
    merged: dict[str, float] = {}
    for i in items:
        merged[i["industry"]] = merged.get(i["industry"], 0.0) + i["weight"] / 100.0 * i["mv"]
    alloc = sorted(
        [{"industry": k, "weight": round(v / total_mv * 100, 2)} for k, v in merged.items()],
        key=lambda x: x["weight"], reverse=True,
    )
    return {"total_mv": round(total_mv, 2), "allocation": alloc}


def build_exposure_payload(conn) -> dict:
    per_fund = []
    for f in held_funds(conn):
        code, name = f["code"], f["name"]
        shares = conn.execute(
            "SELECT COALESCE(SUM(remaining_shares), 0) AS s FROM lots "
            "WHERE fund_code=? AND status='active'",
            (code,),
        ).fetchone()["s"]
        if shares <= 0:
            continue
        per_fund.append(exposure_for(conn, code, name))
    return {"per_fund": per_fund, "portfolio": portfolio_exposure(conn)}


# =============================================================================
# 净值序列与统计
# =============================================================================


def nav_series(conn, fund_code: str) -> list[dict]:
    rows = conn.execute(
        "SELECT nav_date, nav, acc_nav FROM nav_daily WHERE fund_code=? "
        "ORDER BY nav_date ASC",
        (fund_code,),
    ).fetchall()
    return [
        {"date": r["nav_date"], "nav": r["nav"], "acc_nav": r["acc_nav"]}
        for r in rows
    ]


def _window_return(acc: list[float], dates: list[str], days: int) -> float | None:
    """从最新一天往前推 days 个自然日，取首个不晚于该日的点的收益率。"""
    if len(acc) < 2:
        return None
    latest = acc[-1]
    target = (date.fromisoformat(dates[-1]) - timedelta(days=days)).isoformat()
    idx = 0
    for i, d in enumerate(dates):
        if d >= target:
            idx = i
            break
    if idx == 0:
        return None
    base = acc[idx]
    if not base:
        return None
    return (latest / base - 1.0) * 100.0


def nav_stats(series: list[dict]) -> dict:
    """基于累计净值（已把分红加回）计算中长期收益、最大回撤、年化波动率。"""
    pts = [(r["date"], r.get("acc_nav") or r.get("nav")) for r in series if r.get("acc_nav") or r.get("nav")]
    if len(pts) < 2:
        return {}
    dates = [p[0] for p in pts]
    acc = [p[1] for p in pts]

    r1m = _window_return(acc, dates, 30)
    r3m = _window_return(acc, dates, 90)
    r6m = _window_return(acc, dates, 180)
    r1y = _window_return(acc, dates, 365)

    # 最大回撤
    peak = acc[0]
    mdd = 0.0
    for v in acc:
        if v > peak:
            peak = v
        dd = v / peak - 1.0 if peak else 0.0
        if dd < mdd:
            mdd = dd

    # 年化波动率
    rets = [(acc[i] / acc[i - 1] - 1.0) for i in range(1, len(acc)) if acc[i - 1]]
    vol = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        vol = (var ** 0.5) * (252 ** 0.5) * 100.0

    return {
        "latest_acc_nav": round(acc[-1], 4),
        "latest_date": dates[-1],
        "ret_1m": round(r1m, 2) if r1m is not None else None,
        "ret_3m": round(r3m, 2) if r3m is not None else None,
        "ret_6m": round(r6m, 2) if r6m is not None else None,
        "ret_1y": round(r1y, 2) if r1y is not None else None,
        "max_drawdown": round(mdd * 100, 2),
        "annual_vol": round(vol, 2),
        "history_days": len(acc),
    }


# =============================================================================
# 人读叙述
# =============================================================================


def build_narrative(boards: dict, exposures: list[dict], intel_items: list[dict]) -> dict:
    """组装人读叙述。

    返回 `{per_fund: {fund_code: [段落...]}, global: [段落...]}`：
    - **逐只基金**的集中度 / 风格漂移提示 → 归入 `per_fund[code]`，
      由前端渲染进该基金的「持仓穿透·行业暴露」子卡（叙述与数据对齐，不再单独成卡）。
    - **组合层面**的（持仓总览 / 同涨同跌 / 板块资金流 / 情报库状态）→ 归入 `global`，
      由前端渲染进「组合行业暴露」卡。

    拆分方式：叙述按关联对象拆分到持仓穿透与行业暴露里，
    做到「说到哪只基金、就在哪只基金下面」。
    """
    per_fund: dict[str, list[str]] = {e["fund_code"]: [] for e in (exposures or [])}
    global_paras: list[str] = []

    held = exposures or []
    if held:
        names = "、".join(e["fund_name"] for e in held)
        global_paras.append(
            f"当前组合共持有 {len(held)} 只基金：{names}。"
            f"以下分析均基于真实持仓与真实行情，资金为虚拟。"
        )

    for e in held:
        code = e["fund_code"]
        if not e.get("allocation"):
            per_fund[code].append(
                f"ℹ️ {e['fund_name']} 暂无行业配置数据（抓取未覆盖），穿透分析跳过。"
            )
            continue
        if e.get("concentrated"):
            per_fund[code].append(
                f"⚠️ 集中度：{e['fund_name']} 最大单一行业「{e['top_industry']}」"
                f"占 {e['top_weight']}%，已超 {get('analysis', 'concentration_warn_pct', 50.0)}% 阈值，"
                f"该行业波动会直接主导这只基金。"
            )
        d = e.get("drift", {})
        if d.get("drifted"):
            per_fund[code].append(
                f"⚠️ 风格漂移：{e['fund_name']} 名称指向「{d.get('claimed_industry')}」，"
                f"但实际持仓中该方向仅 {d.get('actual_pct')}%，名不副实，不能按名字理解这只基金。"
            )
        elif d.get("claimed_industry"):
            per_fund[code].append(
                f"✅ 风格一致：{e['fund_name']} 名称指向「{d.get('claimed_industry')}」，"
                f"实际持仓中该方向占 {d.get('actual_pct')}%，名实相符。"
            )

    # 组合层面的行业重叠提示（涉及多只基金，归 global）
    top_by_fund = [e for e in held if e.get("top_industry")]
    if len(top_by_fund) >= 2:
        tops = {e["fund_name"]: e["top_industry"] for e in top_by_fund}
        if len(set(tops.values())) < len(tops):
            dup = [k for k, v in tops.items() if list(tops.values()).count(v) > 1]
            if dup:
                global_paras.append(
                    f"⚠️ 同涨同跌：{('、'.join(dup))} 的最大行业相同，"
                    f"表面分散实则押注同一方向，分散效果有限。"
                )

    if boards and boards.get("items"):
        hot = [b["name"] for b in boards["items"] if b["kind"] == "hot"]
        risk = [b["name"] for b in boards["items"] if b["kind"] == "risk"]
        if hot:
            global_paras.append("板块资金近期流入：" + "、".join(hot) + "。")
        if risk:
            global_paras.append("板块资金近期流出：" + "、".join(risk) + "。")
        if boards.get("note"):
            global_paras.append(boards["note"])

    if intel_items:
        disc = [i for i in intel_items if i.get("status") == "disclosed"]
        land = [i for i in intel_items if i.get("status") == "landed"]
        global_paras.append(
            f"情报库共 {len(intel_items)} 条：披露态（放风）{len(disc)} 条、"
            f"已落地 {len(land)} 条。放风不代表落地，据此下单需等待细则。"
        )

    if not global_paras and not any(per_fund.values()):
        global_paras.append("暂无足够数据生成分析，请先抓取行业配置与板块信号（运行时自动获取真实数据）。")
    return {"per_fund": per_fund, "global": global_paras}


# ===== 分析辅助函数 =====

def analyze_fund(fund_code: str) -> dict:
    """单只基金的**综合分析**，供 `GET /api/analyze` 与 AI 工具 `analyze_fund` 使用。

    内容：净值统计（分位 / 区间收益 / 最大回撤）+ 行业暴露 + 基本信息。
    ⚠️ **只给事实与指标，不给买卖建议** —— 决策权在接管 agent（项目边界）。
    """
    from ..db.schema import get_connection

    conn = get_connection()
    try:
        f = conn.execute(
            "SELECT code, name, category FROM funds WHERE code = ?", (fund_code,)
        ).fetchone()
        if not f:
            return {"fund_code": fund_code, "error": "基金不存在"}

        series = nav_series(conn, fund_code)
        stats = nav_stats(series) if series else {}
        try:
            expo = exposure_for(conn, fund_code, f["name"] or "")
        except Exception:  # noqa: BLE001 — 暴露算不出不该让整个分析失败
            expo = {}

        return {
            "fund_code": fund_code,
            "fund_name": f["name"] or "",
            "category": f["category"] or "",
            "nav_points": len(series),
            **stats,
            "exposure": expo,
        }
    finally:
        conn.close()
