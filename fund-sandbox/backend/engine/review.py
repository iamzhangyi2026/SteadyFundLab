# -*- coding: utf-8 -*-
"""
fund-sandbox 组合回顾引擎
- 计算组合收益
- 分析持仓表现
- 生成回顾报告
（基于真实 schema：lots / nav_daily / holdings / ledger / portfolio_daily）
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
from ..db.schema import get_connection
from .holdings import get_positions
from .ledger import get_budget


def get_portfolio_summary(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """获取组合概况（基于真实持仓与账本）"""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        budget = get_budget(conn)
        market_value = float(budget.get("market_value") or 0)
        cash = float(budget.get("cash_balance") or 0)
        total_assets = market_value + cash

        raw_positions = get_positions(conn)
        positions = []
        for p in raw_positions:
            cost_amount = float(p.get("cost") or 0)
            market_value = float(p.get("current_value") or 0)
            profit = round(market_value - cost_amount, 2)
            profit_pct = round(profit / cost_amount * 100, 2) if cost_amount > 0 else 0.0
            positions.append({
                "fund_code": p.get("code"),
                "fund_name": p.get("fund_name"),
                "shares": p.get("shares"),
                "cost_amount": cost_amount,
                "market_value": market_value,
                "profit": profit,
                "profit_pct": profit_pct,
            })

        return {
            "total_assets": total_assets,
            "market_value": market_value,
            "cash": cash,
            "positions": positions,
            "position_count": len(positions),
        }
    finally:
        if own:
            conn.close()


def get_performance_history(
    conn: Optional[sqlite3.Connection] = None,
    days: int = 90
) -> list[dict]:
    """获取历史表现（基于 portfolio_daily 快照）"""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        start_date = (datetime.now()
                      - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT snap_date, market_value, unrealized_pnl, total_cost
               FROM portfolio_daily
               WHERE snap_date >= ?
               ORDER BY snap_date DESC""",
            (start_date,)
        ).fetchall()

        history = []
        for r in rows:
            mv = r["market_value"] or 0
            profit = r["unrealized_pnl"] or 0
            total_cost = r["total_cost"] or 0
            profit_pct = (profit / total_cost * 100) if total_cost > 0 else 0
            history.append({
                "date": r["snap_date"],
                "market_value": mv,
                "profit": profit,
                "profit_pct": round(profit_pct, 2),
            })
        return list(reversed(history))
    finally:
        if own:
            conn.close()


def generate_review_report(
    period: str = "weekly",
    conn: Optional[sqlite3.Connection] = None
) -> dict[str, Any]:
    """生成回顾报告"""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        portfolio = get_portfolio_summary(conn)

        days = 30 if period == "monthly" else 7

        history = get_performance_history(conn, days * 2)

        if len(history) >= 2:
            old = history[0]
            new = history[-1]
            period_return = ((new["market_value"] - old["market_value"]) / old["market_value"] * 100) if old["market_value"] > 0 else 0
        else:
            period_return = 0

        total_assets = portfolio.get("total_assets") or 0
        # 总收益率以「累计净投入」为基准：初始资金 + 历史累积新增 + 沙盒测试资金
        # （cash_ledger 入金类流水，本系统无出金/提现流水，赎回所得留在现金里计入总资产）。
        budget = get_budget(conn)
        net_invested = (float(budget.get("initial") or 0)
                        + float(budget.get("cumulative_added") or 0)
                        + float(budget.get("test_capital") or 0))
        total_return = ((total_assets - net_invested) / net_invested * 100) if net_invested > 0 else 0.0

        return {
            "period": period,
            "portfolio": portfolio,
            "period_return": round(period_return, 2),
            "total_return": round(total_return, 2),
            "generated_at": datetime.now().isoformat(),
        }
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    summary = get_portfolio_summary()
    print(f"组合概况: {summary}")

    report = generate_review_report()
    print(f"回顾报告: {report}")


# ===== 主题复盘与映射校准 =====
# 依赖：news_triage（impact_boards 主题、first_pub_time 首现日）
#       sector_news（board_type 行业/概念、sector、fetched_at 批次）
# 要点：
#   复盘 = 把「事件什么时候冒出来」与「那之后板块怎么走」摆在一起，**只给事实**。
#   校准 = 把上面这批数据归纳成统计，验证「新闻是主信号」这一核心假设。

DEFAULT_DAYS = 20
DEFAULT_TOP_N = 8


def _event_series(conn, days: int = DEFAULT_DAYS, top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """近 days 天里，按**事件数**排前的主题（来自 news_triage.impact_boards）。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT impact_boards,
               COUNT(*)                          AS events,
               MIN(COALESCE(first_pub_time, processed_at)) AS first_day
        FROM news_triage
        WHERE TRIM(COALESCE(impact_boards, '')) <> ''
          AND COALESCE(first_pub_time, processed_at) >= ?
        GROUP BY impact_boards
        ORDER BY events DESC
        LIMIT ?
        """,
        (since, top_n),
    ).fetchall()
    return [{"theme": r["impact_boards"], "events": r["events"],
             "first_day": (r["first_day"] or "")[:10]} for r in rows]


def _board_track(conn, board: str, from_date: str) -> list[tuple[str, float]]:
    """某板块自 from_date 起的价格轨迹（(日期, 累计涨跌幅%)），取 sector_news 日批次。

    口径：当日该板块各条新闻 change_pct 的简单平均（等权近似）。
    库内无成分股权重与复权因子，无法做净值加权；等权近似在成分结构
    变化或复权场景下会失真，此处如实标注。
    """
    rows = conn.execute(
        """
        SELECT substr(fetched_at, 1, 10) AS d, AVG(change_pct) AS chg
        FROM sector_news
        WHERE sector = ? AND substr(fetched_at, 1, 10) >= ?
        GROUP BY d ORDER BY d
        """,
        (board, from_date),
    ).fetchall()
    if not rows:
        return []
    base = 100.0
    out: list[tuple[str, float]] = []
    for r in rows:
        base *= (1 + (r["chg"] or 0) / 100.0)
        out.append((r["d"], round(base - 100.0, 2)))
    return out


def build_review(conn, days: int = DEFAULT_DAYS, top_n: int = DEFAULT_TOP_N) -> dict:
    """主题复盘：把「事件什么时候冒出来」与「那之后板块怎么走」摆在一起。

    ⚠️ **只给事实**（首次事件日 / 事件数 / 之后累计涨跌），不下"该不该买"的结论。
    """
    items: list[dict] = []
    for e in _event_series(conn, days=days, top_n=top_n):
        board = (e["theme"] or "").split(",")[0].strip()
        track = _board_track(conn, board, e["first_day"]) if (board and e["first_day"]) else []
        cum = track[-1][1] if track else None
        items.append({
            "theme": e["theme"], "events": e["events"], "first_day": e["first_day"],
            "has_data": bool(track), "cum_pct": cum, "track": track,
        })
    return {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": days, "items": items,
        "note": "只给「事件首现 → 之后板块表现」的事实，不判断该不该买；"
                "无价格轨迹的主题标 has_data=false（不编造）",
    }


def build_calibration(conn, days: int = 60, top_n: int = 30) -> dict:
    """映射校准：把复盘数据归纳成统计，验证「新闻是主信号」这一核心假设。

    输出 up / down / nodata 计数与简单平均累计涨跌。
    ⚠️ **只归纳统计事实，不自动改映射规则** —— 改不改由 AI/人判断。
    """
    base = build_review(conn, days=days, top_n=top_n)
    items = base["items"]
    up = down = nodata = 0
    cums: list[float] = []
    for it in items:
        if not it["has_data"] or it["cum_pct"] is None:
            nodata += 1
            it["verdict"] = "nodata"
            continue
        cums.append(it["cum_pct"])
        if it["cum_pct"] > 0:
            up += 1
            it["verdict"] = "up"
        else:
            down += 1
            it["verdict"] = "down"
    with_data = up + down
    avg_cum = round(sum(cums) / len(cums), 2) if cums else None
    concl = ("有价格轨迹的主题为 0，暂无法校准（先积累几日热点榜历史）"
             if with_data == 0 else
             f"{with_data} 个有轨迹的主题里，事件后累计上涨 {up} 个"
             f"（{up / with_data * 100:.0f}%）、下跌 {down} 个；简单平均 {avg_cum:+.2f}%。"
             "统计事实仅供参考，是否据此调整映射由 AI/人判断")
    return {
        "as_of": base["as_of"], "days": days, "total": len(items),
        "with_data": with_data, "up": up, "down": down,
        "flat": len(items) - with_data - nodata, "nodata": nodata,
        "avg_cum": avg_cum, "items": items, "conclusion": concl,
        "note": "只归纳统计事实，不自动改映射规则；无价格轨迹的主题标 nodata（不编造）",
    }
