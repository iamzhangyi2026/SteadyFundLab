"""规则引擎：AI 离线时的自主决策者。

定位
----
规则引擎**永远在跑**，但职责随 AI 是否在线而不同（见 coordinator.py）：

    AI 在线  → 执行者：只负责执行指令 + 风控拦截，不做主动决策
    AI 离线  → 决策者：按纪律线自主交易，保证时间敏感操作不漏掉

纪律线来源：长期实战迭代出来的规则，不是拍脑袋定的

    1. 盈利达到 +20%          → 视为已兑现，减仓
    2. 从近期高点回撤 ≥ 15%   → 减仓
    3. 亏损 ≥ 5% 且跑输基准   → 认错，清仓
    4. 定投扣款（每周固定额） → 按计划执行
    5. 赎回前检查罚期         → 避免在 7 天内赎回付 1.5% 惩罚费

为什么纪律线必须自动化
----------------------
这些操作有时间敏感性：破线当天就要处理，等下次开对话可能已经多跌了 10%。
靠人记着不现实，靠 AI 记着更不现实（对话一关就断）。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from backend.config import get
from backend.engine import ledger


# 纪律线阈值默认值。config.toml [rules] 为唯一真源，调用时实时读取（改配置即时生效，无需重启）
PROFIT_TAKE_PCT = 20.0      # 盈利达到该比例视为已兑现（默认，可被 config 覆盖）
DRAWDOWN_CUT_PCT = 15.0     # 从近期高点回撤该比例则减仓
STOP_LOSS_PCT = -5.0        # 亏损达到该比例且跑输基准则认错
LOOKBACK_DAYS = 60          # 计算近期高点的回看天数


def recent_high(conn: sqlite3.Connection, fund_code: str, as_of: date,
                lookback: int | None = None) -> float | None:
    """取近期最高净值。

    lookback 缺省时读 config.toml [rules].lookback_days（实时生效）。

    注意：必须用**累计净值**而不是单位净值。
    单位净值会因分红除息下调，分红那天会形成一个假的"低点"，
    用它算回撤会把分红误判成暴跌。
    """
    if lookback is None:
        lookback = int(get("rules", "lookback_days", LOOKBACK_DAYS))
    start = (as_of - timedelta(days=lookback)).isoformat()
    rows = conn.execute(
        """
        SELECT MAX(COALESCE(acc_nav, nav)) AS hi FROM nav_daily
        WHERE fund_code = ? AND nav_date >= ? AND nav_date <= ?
        """,
        (fund_code, start, as_of.isoformat()),
    ).fetchone()
    return rows["hi"] if rows and rows["hi"] is not None else None


def check_discipline(
    conn: sqlite3.Connection,
    fund_code: str,
    current_nav: float,
    as_of: date,
    benchmark_change_pct: float | None = None,
) -> list[dict]:
    """检查一只持仓是否触发纪律线。

    参数：
        current_nav          当前净值（累计净值优先）
        benchmark_change_pct 同期基准涨跌幅，用于判断"是否跑输"。
                             不传则跳过「认错」这条线的跑输判定

    返回：触发的纪律线列表，每条含 type / detail / action / suggested_ratio
    """
    # 纪律线阈值从 config.toml [rules] 实时读取（改配置即时生效，无需重启）
    profit_take = get("rules", "profit_take_pct", PROFIT_TAKE_PCT)
    drawdown_cut = get("rules", "drawdown_cut_pct", DRAWDOWN_CUT_PCT)
    stop_loss = get("rules", "stop_loss_pct", STOP_LOSS_PCT)
    lookback = int(get("rules", "lookback_days", LOOKBACK_DAYS))

    triggered: list[dict] = []
    pos = ledger.position_summary(conn, fund_code, current_nav)
    if pos["total_shares"] <= 0:
        return triggered

    pnl_pct = pos["profit_pct"]

    # 1. 盈利兑现
    if pnl_pct >= profit_take:
        triggered.append(
            {
                "type": "profit_take",
                "detail": f"浮盈 {pnl_pct:+.2f}%，达到兑现线 {profit_take}%",
                "action": "reducing",
                "suggested_ratio": 0.5,
                "note": "涨多反是兑现信号，不是加仓点。安全垫薄时要先落袋一部分",
            }
        )

    # 2. 回撤减仓
    hi = recent_high(conn, fund_code, as_of, lookback)
    if hi and hi > 0:
        dd = (current_nav - hi) / hi * 100
        if abs(dd) >= drawdown_cut:
            triggered.append(
                {
                    "type": "drawdown_cut",
                    "detail": (
                        f"从 {lookback} 日高点 {hi:.4f} 回撤 {abs(dd):.2f}%，"
                        f"达到减仓线 {drawdown_cut}%"
                    ),
                    "action": "reducing",
                    "suggested_ratio": 0.5,
                    "note": "回撤用累计净值计算，避免把分红除息误判成下跌",
                }
            )

    # 3. 认错清仓
    if pnl_pct <= stop_loss:
        underperform = (
            benchmark_change_pct is not None
            and pnl_pct < benchmark_change_pct
        )
        if benchmark_change_pct is None or underperform:
            triggered.append(
                {
                    "type": "admit_wrong",
                    "detail": (
                        f"亏损 {pnl_pct:.2f}%"
                        + (
                            f" 且跑输基准（基准 {benchmark_change_pct:+.2f}%）"
                            if benchmark_change_pct is not None
                            else ""
                        )
                    ),
                    "action": "clearing",
                    "suggested_ratio": 1.0,
                    "note": "投资逻辑证伪或持续弱势就该走，期限不是「不卖」的承诺",
                }
            )

    return triggered


def check_discipline_all(conn, as_of) -> list:
    """遍历所有活跃持仓，逐个检查纪律线，汇总触发项。供 monitor 演示用。"""
    alerts = []
    codes = [r["fund_code"] for r in conn.execute(
        "SELECT DISTINCT fund_code FROM lots WHERE status='active'").fetchall()]
    for code in codes:
        nav = conn.execute(
            "SELECT COALESCE(acc_nav, nav) AS v FROM nav_daily "
            "WHERE fund_code=? ORDER BY nav_date DESC LIMIT 1", (code,)
        ).fetchone()
        current = float(nav["v"]) if nav and nav["v"] else 0.0
        try:
            for a in check_discipline(conn, code, current, as_of):
                a["fund_code"] = code
                a["action_taken"] = "alert_created"
                alerts.append(a)
        except Exception as e:
            alerts.append({"fund_code": code, "error": str(e)})
    return alerts


def penalty_risk(conn: sqlite3.Connection, fund_code: str, as_of: date) -> dict:
    """检查当前赎回会不会踩到 7 天惩罚费。

    这是最容易白送钱的地方：赎回时按 FIFO 消耗批次，
    如果最早的批次还没满 7 天，就要付 1.5% 而不是 0.5%。
    """
    lots = ledger.holding_report(conn, fund_code, as_of)
    if not lots:
        return {"has_penalty_risk": False, "detail": "无持仓"}

    risky = [l for l in lots if l["is_penalty"]]
    if not risky:
        return {"has_penalty_risk": False, "detail": "所有批次均已满 7 天"}

    worst = min(l["days_to_penalty_free"] for l in risky)
    return {
        "has_penalty_risk": True,
        "risky_lots": len(risky),
        "days_to_wait": worst,
        "detail": (
            f"{len(risky)} 个批次仍在 7 天惩罚期内，"
            f"再等 {worst} 天可免罚（1.5% → 0.75%）"
        ),
    }


def plan_dingtou(
    funds: list[dict], weekly_amount: float | None = None
) -> list[dict]:
    """生成定投计划。

    按用户的真实节奏：单只 25 元/周 = 100 元/月。
    月配额 1000 元，因此单月最多覆盖 10 只（1000 ÷ 100）。
    """
    weekly_amount = weekly_amount or get("capital", "per_fund_weekly", 25.0)
    monthly_quota = get("capital", "monthly_quota", 1000.0)
    monthly_per_fund = weekly_amount * 4
    max_funds = int(monthly_quota // monthly_per_fund) if monthly_per_fund else 0

    plan = []
    for f in funds[:max_funds]:
        plan.append(
            {
                "fund_code": f["code"],
                "fund_name": f.get("name", ""),
                "amount": weekly_amount,
                "purpose": "dingtou",
                "note": f"周定投 {weekly_amount} 元（月计 {monthly_per_fund} 元）",
            }
        )
    return plan




def run_offline_cycle(
    conn: sqlite3.Connection, fund_codes: list[str], as_of: date
) -> dict:
    """离线周期主入口。AI 不在线时由调度器调用。

    返回本次周期的检查结果和建议动作，供调度器执行或记录。
    """
    actions: list[dict] = []
    for code in fund_codes:
        row = conn.execute(
            "SELECT COALESCE(acc_nav, nav) AS v FROM nav_daily "
            "WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1",
            (code,),
        ).fetchone()
        nav = float(row["v"]) if row and row["v"] else 0.0
        entry: dict = {"fund_code": code}
        if nav <= 0:
            entry["note"] = "无净值数据，跳过纪律检查"
            actions.append(entry)
            continue
        entry["nav"] = nav
        entry["lines"] = check_discipline(conn, code, nav, as_of)
        entry["penalty"] = penalty_risk(conn, code, as_of)
        actions.append(entry)
    return {"as_of": as_of.isoformat(), "actions": actions}


def total_assets(conn: sqlite3.Connection) -> float:
    """账户总资产 = 现金 + 持仓市值。"""
    from backend.engine import orders

    return orders.get_cash_balance(conn) + orders._total_market_value(conn)


def check_account_stop(conn: sqlite3.Connection) -> dict:
    """账户级回撤止损（峰值法）。

    为什么用峰值法而不是历史快照表：
        算回撤本该需要历史总资产序列，那要多建一张每日快照表。
        峰值法只需记一个「历史最高」数字，存 meta 表即可，
        效果等价（都是衡量从最高点回落多少），复杂度低得多。

    触发后仅**告警、不暂停自动交易**，由人或 AI 决定下一步，
    而不是自动清仓——自动割肉往往割在低点（风控边界只是提醒）。
    """
    total = total_assets(conn)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'peak_assets'"
    ).fetchone()
    peak = float(row["value"]) if row and row["value"] else 0.0

    if total > peak:
        peak = total
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('peak_assets', ?)",
            (str(round(peak, 2)),),
        )
        conn.commit()

    dd = (total - peak) / peak * 100 if peak > 0 else 0.0
    limit = get("risk", "max_drawdown_stop_pct", 20.0)
    triggered = dd < 0 and abs(dd) >= limit

    return {
        "total_assets": round(total, 2),
        "peak_assets": round(peak, 2),
        "drawdown_pct": round(dd, 2),
        "limit_pct": limit,
        "triggered": triggered,
        "note": (
        f"距最高点 {abs(dd):.2f}%，"
        + ("已触及止损线，仅提醒（不暂停自动交易）" if triggered else "未触及止损线")
        ),
    }


if __name__ == "__main__":
    from backend.db import get_connection

    print("=" * 78)
    print("规则引擎演示（离线模式下的自主决策）")
    print("=" * 78)

    # 内存库 + 造数据，不污染真实数据库
    conn = get_connection(":memory:")
    conn.executescript(
        """
        CREATE TABLE lots (
            lot_id INTEGER PRIMARY KEY AUTOINCREMENT, fund_code TEXT NOT NULL,
            order_id INTEGER NOT NULL, confirm_date TEXT NOT NULL,
            shares REAL NOT NULL, remaining_shares REAL NOT NULL,
            cost_nav REAL NOT NULL, cost_amount REAL NOT NULL,
            status TEXT DEFAULT 'active', created_at TEXT);
        CREATE TABLE nav_daily (
            fund_code TEXT NOT NULL, nav_date TEXT NOT NULL, nav REAL NOT NULL,
            acc_nav REAL, daily_growth REAL, source TEXT NOT NULL,
            verified INTEGER DEFAULT 0, is_qdii_lagged INTEGER DEFAULT 0,
            fetched_at TEXT NOT NULL, PRIMARY KEY (fund_code, nav_date));
        """
    )

    as_of = date(2026, 8, 28)
    # 场景：成本 1.00，当前累计净值 1.25（浮盈 25%），
    # 但 60 日高点曾到 1.50 → 回撤 16.7%，两条线同时触发
    conn.execute(
        "INSERT INTO lots (fund_code,order_id,confirm_date,shares,remaining_shares,"
        "cost_nav,cost_amount,status,created_at) VALUES ('DEMO',1,'2026-05-01',"
        "10000,10000,1.00,10000,'active','2026-05-01')"
    )
    for i in range(60):
        d = (as_of - timedelta(days=60 - i)).isoformat()
        nav = 1.00 + (i / 45) * 0.50 if i <= 45 else 1.50 - (i - 45) * 0.025
        conn.execute(
            "INSERT INTO nav_daily (fund_code,nav_date,nav,acc_nav,source,fetched_at)"
            " VALUES ('DEMO',?,?,?,'demo','2026-08-28')",
            (d, round(nav, 4), round(nav, 4)),
        )
    conn.commit()

    pos = ledger.position_summary(conn, "DEMO", 1.25)
    print(f"\n持仓：{pos['total_shares']:.0f} 份，成本 {pos['total_cost']:.2f} 元，"
          f"市值 {pos['market_value']:.2f} 元，浮盈 {pos['profit_pct']:+.2f}%")

    lines = check_discipline(conn, "DEMO", 1.25, as_of)
    print(f"\n触发纪律线 {len(lines)} 条：")
    for ln in lines:
        print(f"  [{ln['type']}] {ln['detail']}")
        print(f"      建议动作 {ln['action']}（比例 {ln['suggested_ratio']}）  {ln['note']}")

    print("\n罚期检查：")
    pr = penalty_risk(conn, "DEMO", as_of)
    print(f"  {pr['detail']}")

    print("\n定投计划：")
    funds = [{"code": "005827", "name": "易方达蓝筹精选混合"},
             {"code": "270042", "name": "广发纳斯达克100指数(QDII)"}]
    for p in plan_dingtou(funds):
        print(f"  {p['fund_code']} {p['fund_name']}：{p['amount']} 元/周  {p['note']}")

    conn.close()
