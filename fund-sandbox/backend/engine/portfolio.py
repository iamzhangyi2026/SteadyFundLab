"""组合收益曲线：每日快照 + 曲线统计。

为什么用「每日快照」而不是实时回放历史
--------------------------------------
lots 表只存**当前**剩余份额，要还原历史上某一天的持仓份额，
必须回放从开户起所有订单与批次（买入建批、赎回 FIFO 消耗），
成本高且极易出错。

改成每天收盘后写一笔快照（市值 / 现金 / 总资产 / 成本 / 盈亏），
曲线就有了真实的历史轨迹，之后每天往后再累积一行即可。

快照口径
--------
    market_value   持仓市值 = Σ(剩余份额 × 最新净值)（累计净值口径，已把分红加回）
    total_cost     剩余份额对应的成本（按批次比例摊）
    cash           现金余额（由 cash_ledger 流水累加）
    total_assets   总资产 = 市值 + 现金
    cumulative_pnl 累计收益 = 浮动盈亏 + 已实现盈亏
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from backend.engine import orders
from backend.engine import budget
from backend.engine import money as _m

# 快照日期按本机时区记，与下单 / 订单号同一口径


def _portfolio_state(conn: sqlite3.Connection) -> dict:
    """当前组合状态：市值 / 成本 / 现金 / 盈亏。"""
    rows = conn.execute(
        """
        SELECT l.fund_code, l.remaining_shares, l.shares, l.cost_amount,
               (SELECT n.nav FROM nav_daily n
                WHERE n.fund_code = l.fund_code AND n.nav IS NOT NULL
                ORDER BY n.nav_date DESC LIMIT 1) AS nav
        FROM lots l
        WHERE l.status = 'active' AND l.remaining_shares > 0
        """
    ).fetchall()

    # 市值 / 成本按 Decimal 逐批计算（口径与 budget.used_in_positions、
    # ledger.position_summary、holdings.get_positions 一致：成本按原始份额折算），
    # 最后量化落地。float 链路在批次多、金额碎时会累积可见误差。
    mv = _m.ZERO
    cost = _m.ZERO
    for r in rows:
        nav_d = _m.D(r["nav"])
        if nav_d > 0:
            mv += _m.q_money(_m.D(r["remaining_shares"]) * nav_d)
        orig = _m.D(r["shares"])
        if orig > 0:
            cost += _m.q_money(
                _m.D(r["cost_amount"]) * _m.D(r["remaining_shares"]) / orig
            )

    cash = orders.get_cash_balance(conn)
    # 已实现盈亏由 budget 模块算（单一来源）；此处不吞异常——
    # 静默记 0 会把「算错」伪装成「没赚」，快照曲线整体失真。
    realized = budget.realized_pnl(conn)

    return {
        "market_value": float(_m.q_money(mv)),
        "total_cost": float(_m.q_money(cost)),
        "cash": round(cash, 2),
        "total_assets": round(float(_m.q_money(mv)) + cash, 2),
        "unrealized_pnl": float(_m.q_money(mv - cost)),
        "realized_pnl": round(realized, 2),
        "cumulative_pnl": float(_m.q_money(mv - cost + _m.D(realized))),
    }


def take_snapshot(conn: sqlite3.Connection, snap_date: date | None = None) -> dict:
    """记录当日组合快照。同一天重复调用会覆盖，不产生重复行。"""
    # 默认日期按本机时区取，与下单 / 订单号同一口径
    d = (snap_date or datetime.now().date()).isoformat()
    s = _portfolio_state(conn)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO portfolio_daily
            (snap_date, market_value, cash, total_assets, total_cost,
             unrealized_pnl, realized_pnl, cumulative_pnl, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snap_date) DO UPDATE SET
            market_value   = excluded.market_value,
            cash           = excluded.cash,
            total_assets   = excluded.total_assets,
            total_cost     = excluded.total_cost,
            unrealized_pnl = excluded.unrealized_pnl,
            realized_pnl   = excluded.realized_pnl,
            cumulative_pnl = excluded.cumulative_pnl,
            created_at     = excluded.created_at
        """,
        (d, s["market_value"], s["cash"], s["total_assets"], s["total_cost"],
         s["unrealized_pnl"], s["realized_pnl"], s["cumulative_pnl"], now),
    )
    conn.commit()
    return {"ok": True, "snap_date": d, **s}


def max_drawdown(values: list[float]) -> float:
    """最大回撤（%）：序列从历史峰值回落的最大幅度。"""
    peak: float | None = None
    mdd = 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak * 100
            if dd > mdd:
                mdd = dd
    return round(mdd, 2)


def curve(conn: sqlite3.Connection, limit: int = 365) -> dict:
    """组合曲线序列 + 区间统计（累计收益率、最大回撤）。"""
    rows = conn.execute(
        """
        SELECT snap_date, market_value, cash, total_assets, total_cost,
               unrealized_pnl, realized_pnl, cumulative_pnl
        FROM portfolio_daily ORDER BY snap_date DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    # 先按时间倒序取「最近 N 天」，再翻回升序——保证曲线从左到右按时间推进、
    # stats 的 assets[0]/assets[-1] 分别是区间首尾，而不是落到历史窗口。
    rows.reverse()
    items = [dict(r) for r in rows]

    stats = {
        "points": len(items),
        "max_drawdown_pct": 0.0,
        "total_return_pct": 0.0,
        "start_assets": None,
        "end_assets": None,
        "latest_cumulative_pnl": None,
    }
    if items:
        assets = [float(r["total_assets"]) for r in items]
        stats["start_assets"] = round(assets[0], 2)
        stats["end_assets"] = round(assets[-1], 2)
        stats["max_drawdown_pct"] = max_drawdown(assets)
        if assets[0] > 0:
            stats["total_return_pct"] = round(
                (assets[-1] - assets[0]) / assets[0] * 100, 2
            )
        stats["latest_cumulative_pnl"] = items[-1]["cumulative_pnl"]

    return {
        "count": len(items),
        "items": items,
        "stats": stats,
        "note": "曲线来自 portfolio_daily 每日快照（每日收盘后自动记录，也可手动触发）",
    }


def time_weighted_return(conn: sqlite3.Connection, fund_code: str,
                         start_date: str | None = None,
                         end_date: str | None = None) -> dict:
    """单只基金持仓期间的时间加权收益率（净值增长率口径）。

    与成本法（profit_pct）的区别
    ----------------------------
        成本法  金额加权，受你加减仓的**时点和金额**影响。
                低位大额加仓会拉高收益率，高位加仓会拉低，
                所以它衡量的是"你这笔钱赚了多少"。

        TWR    只看基金净值本身的增长，剔除申赎时点影响，
                衡量"这只基金本身表现如何"。两者不等价，
                组合诊断时要分开看（赚得少可能是择时问题，不是选品问题）。

    口径：用**累计净值** acc_nav（已把分红加回），避免分红除息造成假跌。
    起止默认取「首个批次确认日 → 最新净值日」。
    """
    if start_date is None:
        r = conn.execute(
            "SELECT MIN(confirm_date) AS d FROM lots WHERE fund_code=? AND status='active'",
            (fund_code,),
        ).fetchone()
        start_date = r["d"] if r and r["d"] else None
    if not start_date:
        return {"ok": False, "reason": "无持仓批次，无法计算 TWR"}

    q = """
        SELECT nav_date, COALESCE(acc_nav, nav) AS v FROM nav_daily
        WHERE fund_code = ? AND nav_date >= ?
    """
    params: list = [fund_code, start_date]
    if end_date:
        q += " AND nav_date <= ?"
        params.append(end_date)
    q += " ORDER BY nav_date ASC"

    rows = conn.execute(q, params).fetchall()
    if len(rows) < 2:
        return {"ok": False, "reason": "净值数据不足（至少需要 2 个交易日）"}

    v0, d0 = rows[0]["v"], rows[0]["nav_date"]
    v1, d1 = rows[-1]["v"], rows[-1]["nav_date"]
    if not v0 or v0 <= 0:
        return {"ok": False, "reason": "起始净值无效"}

    twr = (v1 / v0 - 1) * 100
    days = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
    annualized = None
    # 年化要求起止净值均为正：净值非正（脏数据 / 停牌异常）时幂运算会出
    # 复数或除零错误，此时宁缺毋滥——TWR 本身仍可读。
    if days > 0 and v0 > 0 and v1 > 0:
        years = days / 365.0
        annualized = ((v1 / v0) ** (1 / years) - 1) * 100

    return {
        "ok": True,
        "fund_code": fund_code,
        "start_date": d0, "end_date": d1,
        "start_nav": round(v0, 4), "end_nav": round(v1, 4),
        "twr_pct": round(twr, 2),
        "annualized_pct": round(annualized, 2) if annualized is not None else None,
        "days": days,
    }


# ---------------------------------------------------------------------------
# 与 main.py 路由契约对齐的别名 / 公开封装
# ---------------------------------------------------------------------------

# main.py 路由 /api/portfolio/curve 调用 get_portfolio_curve
get_portfolio_curve = curve

# main.py 路由 /api/portfolio/snapshot 调用 snapshot_portfolio
snapshot_portfolio = take_snapshot


def get_portfolio_summary(conn: sqlite3.Connection) -> dict:
    """当前组合概要：市值 / 成本 / 现金 / 盈亏 + 持仓数。供 /api/portfolio/summary。"""
    s = _portfolio_state(conn)
    n_holdings = conn.execute(
        "SELECT COUNT(*) AS c FROM lots WHERE status='active' AND remaining_shares > 0"
    ).fetchone()["c"]
    s["holdings_count"] = n_holdings
    return s
