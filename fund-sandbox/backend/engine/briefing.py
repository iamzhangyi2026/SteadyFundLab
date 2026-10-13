"""每日决策简报（14:30 钩子）。

为什么需要这个
--------------
交易决策有时间窗口（15:00 截单），但对话里的 AI 不会一直在线。
如果在 14:30 就把市场快照、持仓状态、约束条件整理好，再配上一个明确的问题
（"今天的基金该怎么买？"），那么人一开对话，AI 读这份简报就能立刻作答。

它把「AI 主动分析」变成「回答一个材料齐备的问题」——门槛低得多，
也是当前技术条件下「唤醒 AI」最现实的形式：

    定时备材料 → 桌面提醒人 → 人开对话 → AI 读简报 → 输出决策 → 执行

真正的全自动（人不用开对话）需要接 LLM API 或平台定时任务能力，
属于另一种部署形态，接口已预留，不影响本模块。

简报不含任何模拟数据。抓不到就是抓不到，明确标注「无数据」。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from backend.config import get
from backend.engine import coordinator, ledger, rules, fund_pool

BRIEFING_DIR = Path(__file__).resolve().parents[2] / "data" / "briefing"
INTEL_DIR = Path(__file__).resolve().parents[2] / "intel"

# 钩子触发时刻。14:30 留出 30 分钟给 AI 作答、人确认、赶在 15:00 截单前完成
HOOK_TIME = "14:30"

DEFAULT_QUESTION = "今天的基金该怎么买？"


def _latest_nav(conn: sqlite3.Connection, fund_code: str) -> tuple[str, float] | None:
    """取该基金最新净值日与累计净值（累计净值优先，避免分红干扰）。"""
    row = conn.execute(
        """
        SELECT nav_date, COALESCE(acc_nav, nav) AS v FROM nav_daily
        WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1
        """,
        (fund_code,),
    ).fetchone()
    return (row["nav_date"], row["v"]) if row else None


def _month_quota_used(conn: sqlite3.Connection, year_month: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM quota_usage WHERE year_month = ?",
        (year_month,),
    ).fetchone()
    return row["s"] if row else 0.0


def _today_intel(as_of: date) -> list[str]:
    """读 intel/ 目录下当天的情报条目（AI 手写的，非爬虫产出）。"""
    if not INTEL_DIR.exists():
        return []
    out: list[str] = []
    for f in sorted(INTEL_DIR.glob("*.md")):
        if f.stem != as_of.isoformat():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("##"):
                out.append(line.lstrip("# ").strip())
    return out


def generate(conn: sqlite3.Connection, as_of: date | None = None,
             question: str = DEFAULT_QUESTION) -> dict:
    """生成决策简报。

    返回结构化字典，可直接落 json，也能渲染成 markdown 给人读。
    """
    as_of = as_of or date.today()
    coord = coordinator.status()
    funds = fund_pool.pool_funds(conn)

    # --- 持仓与纪律线 ---
    positions = []
    for f in funds:
        code, name = f["code"], f.get("name", "")
        nav_info = _latest_nav(conn, code)
        if not nav_info:
            positions.append(
                {"fund_code": code, "fund_name": name, "note": "无净值数据"}
            )
            continue
        nav_date, nav = nav_info
        pos = ledger.position_summary(conn, code, nav)
        lines = rules.check_discipline(conn, code, nav, as_of)
        pen = rules.penalty_risk(conn, code, as_of)
        positions.append(
            {
                "fund_code": code,
                "fund_name": name,
                "nav_date": nav_date,
                "nav": round(nav, 4),
                **pos,
                "discipline_lines": [
                    {"type": l["type"], "detail": l["detail"], "action": l["action"]}
                    for l in lines
                ],
                "penalty": pen,
            }
        )

    # --- 资金与配额 ---
    ym = as_of.strftime("%Y-%m")
    used = _month_quota_used(conn, ym)
    quota = get("capital", "monthly_quota", 1000.0)

    # --- 情报 ---
    intel = _today_intel(as_of)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "hook_time": HOOK_TIME,
        "question": question,
        "mode": {
            "effective": coord["effective_mode"],
            "ai_online": coord["ai_online"],
            "idle_hours": coord["idle_hours"],
        },
        "capital": {
            "monthly_quota": quota,
            "used_this_month": round(used, 2),
            "remaining": round(quota - used, 2),
            "per_fund_weekly": get("capital", "per_fund_weekly", 25.0),
        },
        "positions": positions,
        "intel_today": intel,
        "risk_limits": {
            "max_single_position_pct": get("risk", "max_single_position_pct"),
            "max_amount_per_order": get("risk", "max_amount_per_order"),
            "max_drawdown_stop_pct": get("risk", "max_drawdown_stop_pct"),
        },
    }


def to_markdown(b: dict) -> str:
    """渲染成给人（和 AI）读的 markdown。"""
    L: list[str] = []
    L.append(f"# 决策简报 · {b['as_of']} {b['hook_time']}")
    L.append("")
    L.append(f"生成时间：{b['generated_at']}")
    L.append(
        f"运行模式：{b['mode']['effective']}"
        f"（AI {'在线' if b['mode']['ai_online'] else '离线'}）"
    )
    L.append("")
    L.append("## 待决策问题")
    L.append("")
    L.append(f"**{b['question']}**")
    L.append("")

    c = b["capital"]
    L.append("## 资金与配额")
    L.append("")
    L.append(f"- 本月配额 {c['monthly_quota']:.0f} 元，已用 {c['used_this_month']:.0f} 元，"
             f"**剩余 {c['remaining']:.0f} 元**")
    L.append(f"- 定投单位 {c['per_fund_weekly']:.0f} 元/周（月计 {c['per_fund_weekly'] * 4:.0f} 元/只）")
    L.append("")

    L.append("## 持仓状态")
    L.append("")
    if not b["positions"]:
        L.append("（无持仓）")
    for p in b["positions"]:
        if "note" in p:
            L.append(f"- {p['fund_code']} {p.get('fund_name', '')}：{p['note']}")
            continue
        L.append(f"- **{p['fund_code']} {p['fund_name']}**")
        L.append(
            f"  - 净值 {p['nav']}（{p['nav_date']}）　"
            f"市值 {p['market_value']:.2f} 元　"
            f"盈亏 {p['profit_pct']:+.2f}%"
        )
        for ln in p.get("discipline_lines", []):
            L.append(f"  - ⚠ 触发 {ln['type']}：{ln['detail']} → 建议 {ln['action']}")
        pen = p.get("penalty", {})
        if pen.get("has_penalty_risk"):
            L.append(f"  - ⚠ {pen['detail']}")
    L.append("")

    if b["intel_today"]:
        L.append("## 当日情报")
        L.append("")
        for it in b["intel_today"]:
            L.append(f"- {it}")
        L.append("")

    r = b["risk_limits"]
    L.append("## 风控边界（不可越界）")
    L.append("")
    L.append(f"- 单只持仓上限 {r['max_single_position_pct']}%")
    L.append(f"- 单笔金额上限 {r['max_amount_per_order']} 元")
    L.append(f"- 最大回撤止损 {r['max_drawdown_stop_pct']}%")
    L.append("")
    L.append("---")
    L.append("")
    L.append("_本简报不含模拟数据。抓不到的项标注为「无数据」，不得编造填充。_")
    return "\n".join(L)


def save(b: dict, out_dir: Path | None = None) -> tuple[Path, Path]:
    """保存简报，返回 (markdown 路径, json 路径)。"""
    d = out_dir or BRIEFING_DIR
    d.mkdir(parents=True, exist_ok=True)
    md = d / f"briefing-{b['as_of']}.md"
    js = d / f"briefing-{b['as_of']}.json"
    md.write_text(to_markdown(b), encoding="utf-8")
    js.write_text(json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js


def notify(title: str, message: str) -> bool:
    """桌面弹窗提醒。

    用 ctypes 直接调 Windows 的 MessageBox，不用装任何依赖，
    也不会触发脚本执行策略拦截（PowerShell / cscript 在某些环境下会被拦）。

    失败时返回 False，不抛异常——弹窗只是辅助，不该影响主流程。
    """
    try:
        import ctypes

        MB_OK = 0x0
        MB_SETFOREGROUND = 0x00010000
        MB_TOPMOST = 0x00040000
        ctypes.windll.user32.MessageBoxW(
            0, message, title, MB_OK | MB_SETFOREGROUND | MB_TOPMOST
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    from backend.db import get_connection, init_db

    init_db()
    conn = get_connection()
    b = generate(conn)
    md, js = save(b)
    print("=" * 78)
    print(f"决策简报已生成 · {b['as_of']} {b['hook_time']}")
    print("=" * 78)
    print(f"  Markdown: {md}")
    print(f"  JSON    : {js}")
    print(f"  运行模式: {b['mode']['effective']}（AI {'在线' if b['mode']['ai_online'] else '离线'}）")
    print(f"  本月配额剩余: {b['capital']['remaining']:.0f} 元")
    print()
    print(to_markdown(b))
    print()
    # 默认不弹窗：MessageBox 是**模态**的，会阻塞等待用户点击，
    # 在后台调度或自动化场景下会让线程卡死。
    # 需要桌面提醒时显式加 --notify 参数（交互式运行才用）。
    if "--notify" in sys.argv:
        ok = notify(
            f"fund-sandbox {b['hook_time']}",
            f"{b['question']}\n\n简报已备好：{md.name}\n打开对话即可让 AI 作答。",
        )
        print(f"\n桌面提醒: {'已弹出' if ok else '未能弹出（不影响简报生成）'}")
    else:
        print("\n（未弹窗。加 --notify 可触发桌面提醒；"
              "后台调度请勿使用——MessageBox 是模态的，会阻塞线程）")
    conn.close()
