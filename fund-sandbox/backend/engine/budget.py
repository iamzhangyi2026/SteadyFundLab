"""资金预算：累积制，盈亏并入可用额度。

口径（实现见 available_budget()）
------------------------------------------------
    可用预算 = 初始资金 + 历史累积新增 + 测试资金 − 已用金额 + 投资收益 − 待确认冻结
    投资收益 = 累计赎回金额 + 累计分红 − 累计申购金额（可为负）
    测试资金 = 正式启动前注入的沙盒试玩资金，启动月自动转正为初始资金
    待确认冻结 = 已下单未确认（T+1）的申购金额，从可用中扣除防止透支

两个最容易搞错的地方
--------------------
1. **累积制，不是每月清零**
   每月新增 1000 是往池子里**累加**，不是「当月不用就作废」。
   所以第二个月：可用 = 1000(初始) + 2000(两月新增) − 已用 + 收益。

2. **盈亏反过来影响可用预算**
   赚到的钱可以继续投，亏掉的钱会削减额度。这是复利逻辑，不是固定额度。
   用户在 1000plan 里就是这么运作的：每月 1000 的真实节奏，
   盈亏直接决定下个月能投多少。

会计等价校验
------------
    可用预算 ≡ 现金余额 = 初始 + 累积新增 − 累计申购 + 累计赎回 + 累计分红

因为「已用金额」= 当前仍持仓的成本，「投资收益」= 已实现盈亏，
一减一加之后与现金流水口径完全相等。本模块提供 check_consistency()
来验证这两个口径是否对得上——对不上说明有账目错误，必须查。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from backend.config import get


# 资金流水的类型常量。分开设是为了能区分「初始资金」和「每月新增」，
# 否则累积新增没法算（两者都是入金，混在一起就分不出来了）。
TXN_INITIAL = "initial"        # 初始资金入账
TXN_MONTHLY = "monthly_add"    # 每月新增预算入账
TXN_PURCHASE = "purchase"      # 申购扣款
TXN_REDEEM = "redeem"          # 赎回到账
TXN_DIVIDEND = "dividend"      # 分红到账
TXN_FEE = "fee"                # 费用
TXN_REFUND = "refund"          # 撤单退款
TXN_TEST = "test_capital"       # 沙盒测试资金（正式启动月自动转正为初始资金）


def _official_start() -> str:
    """正式启动月（YYYY-MM）。此月之前不入账任何资金。"""
    return str(get("capital", "official_start_month", "2026-09"))


def _sum_by_type(conn: sqlite3.Connection, txn_type: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_ledger WHERE txn_type = ?",
        (txn_type,),
    ).fetchone()
    return row["s"] if row else 0.0


# =============================================================================
# 入账
# =============================================================================


def init_capital(conn: sqlite3.Connection) -> bool:
    """首次入金初始资金。已入过则跳过（幂等）。

    正式启动月闸门：当前月份早于 official_start_month 时不入账
    （测试期数据清零后，正式资金在到达启动月后才注入）。
    返回 True 表示本次真的入金了。
    """
    if date.today().strftime("%Y-%m") < _official_start():
        return False

    from backend.engine import orders

    exists = conn.execute(
        "SELECT COUNT(*) AS c FROM cash_ledger WHERE txn_type = ?", (TXN_INITIAL,)
    ).fetchone()["c"]
    if exists:
        return False

    amount = get("capital", "initial_cash", 1000.0)
    orders.append_ledger(
        conn, TXN_INITIAL, amount,
        note=f"初始资金 {amount:.0f} 元",
        txn_date=date.today(),
    )
    conn.commit()
    return True


def add_monthly_budget(conn: sqlite3.Connection, year_month: str | None = None) -> bool:
    """入账当月新增预算。**同月只入一次**（幂等）。

    关键约束——**创建月不入账**：
        初始资金入账的当月是「项目创建月」，月增从创建月的下一个月才起算。
        例如 2026-08-30 创建、初始资金记在 2026-08，则 2026-08 不入月增，
        第一笔月增在 2026-09（用户打开账户时自动入）。
        这样避免「项目刚建好当月就凭空多出一个月增」的错误。

    返回 True 表示本次真的入金了。
    """
    from backend.engine import orders

    ym = year_month or date.today().strftime("%Y-%m")
    # 正式启动月闸门：未到启动月绝不入账
    if ym < _official_start():
        return False
    # 创建月 = 正式启动月（official_start_month）；月增从该月的下一个月才起算。
    # 用官方启动月而非「首笔初始资金所在月」，是为了兼容 8 月先注入测试资金、
    # 9月转正的情况：转正后月增仍从 10 月起算，不会凭空多一个月增。
    create_ym = _official_start()
    if ym <= create_ym:
        return False
    exists = conn.execute(
        "SELECT COUNT(*) AS c FROM cash_ledger WHERE txn_type = ? AND note LIKE ?",
        (TXN_MONTHLY, f"%{ym}%"),
    ).fetchone()["c"]
    if exists:
        return False

    amount = get("capital", "monthly_quota", 1000.0)
    orders.append_ledger(
        conn, TXN_MONTHLY, amount,
        note=f"{ym} 月度新增预算",
        txn_date=date.today(),
    )
    conn.commit()
    return True




def inject_test_capital(conn: sqlite3.Connection, amount: float | None = None) -> bool:
    """注入沙盒测试资金（仅用于正式启动前的试玩）。

    幂等：已注入过则跳过。金额默认取 config 的 initial_cash。
    正式启动月（official_start_month）到达时由 rollover_test_capital 自动转正，
    无需手动清理。返回 True 表示本次真的注入了。
    """
    # 已到正式启动月：试玩注资不再适用，正式初始资金走 init_capital
    if date.today().strftime("%Y-%m") >= _official_start():
        return False

    from backend.engine import orders

    exists = conn.execute(
        "SELECT COUNT(*) AS c FROM cash_ledger WHERE txn_type = ?", (TXN_TEST,)
    ).fetchone()["c"]
    if exists:
        return False

    amt = amount if amount is not None else get("capital", "initial_cash", 1000.0)
    orders.append_ledger(
        conn, TXN_TEST, amt,
        note=f"沙盒测试资金 {amt:.0f} 元（正式启动自动转正）",
        txn_date=date.today(),
    )
    conn.commit()
    return True


def rollover_test_capital(conn: sqlite3.Connection) -> bool:
    """正式启动月翻转：把沙盒测试资金转正为正式初始资金。

    只改标签（test_capital → initial），不清空持仓、不动流水金额，
    因此 8 月的测试余额在 9月1日自然变成正式初始资金，
    月增仍从官方启动月的下一个月累加（见 add_monthly_budget 的 create_ym）。
    返回 True 表示本次真的转正了。
    """
    if date.today().strftime("%Y-%m") < _official_start():
        return False
    exists = conn.execute(
        "SELECT COUNT(*) AS c FROM cash_ledger WHERE txn_type = ?",
        (TXN_TEST,),
    ).fetchone()["c"]
    if not exists:
        return False
    amt = test_capital(conn)
    conn.execute(
        "UPDATE cash_ledger SET txn_type = ?, note = ? WHERE txn_type = ?",
        (TXN_INITIAL, f"初始资金（由沙盒测试资金转正）{amt:.0f} 元", TXN_TEST),
    )
    conn.commit()
    return True


# =============================================================================
# 口径分解
# =============================================================================


def initial_capital(conn: sqlite3.Connection) -> float:
    """初始资金。"""
    return _sum_by_type(conn, TXN_INITIAL)


def cumulative_added(conn: sqlite3.Connection) -> float:
    """历史累积新增（不含初始资金）。"""
    return _sum_by_type(conn, TXN_MONTHLY)


def test_capital(conn: sqlite3.Connection) -> float:
    """沙盒测试资金（正式启动月翻转时自动转正为初始资金，不清空）。"""
    return _sum_by_type(conn, TXN_TEST)


def used_in_positions(conn: sqlite3.Connection) -> float:
    """已用金额 = 当前仍持仓的成本。

    按批次剩余份额占原始份额的比例折算成本：
        批次成本 × (剩余份额 / 原��份额)
    部分赎回过的批次只算未赎回那部分的成本。
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN shares > 0
                 THEN cost_amount * (remaining_shares / shares)
                 ELSE 0 END
        ), 0) AS s
        FROM lots WHERE shares > 0
        """
    ).fetchone()
    return row["s"] if row else 0.0


def redeemed_cost(conn: sqlite3.Connection) -> float:
    """已赎回部分对应的原始成本。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN shares > 0
                 THEN cost_amount * ((shares - remaining_shares) / shares)
                 ELSE 0 END
        ), 0) AS s
        FROM lots WHERE shares > 0
        """
    ).fetchone()
    return row["s"] if row else 0.0


def realized_pnl(conn: sqlite3.Connection) -> float:
    """投资收益（已实现） = 累计赎回 + 累计分红 − 已赎回部分的成本。

    注意这里减去的是**已赎回部分的成本**，不是全部申购金额。
    仍持仓那部分的浮动盈亏不计入——它还没落袋，
    计入会让可用预算虚高（账面赚钱但赎回来才发现要付赎回费）。
    """
    redeemed = _sum_by_type(conn, TXN_REDEEM)
    dividend = _sum_by_type(conn, TXN_DIVIDEND)
    return redeemed + dividend - redeemed_cost(conn)


def unrealized_pnl(conn: sqlite3.Connection) -> float:
    """浮动盈亏（未实现），仅用于展示，不计入可用预算。"""
    total_mv = 0.0
    for row in conn.execute(
        "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
    ):
        code = row["fund_code"]
        nav_row = conn.execute(
            """
            SELECT nav AS v FROM nav_daily
            WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not nav_row:
            continue
        mv = conn.execute(
            """
            SELECT COALESCE(SUM(remaining_shares), 0) AS s FROM lots
            WHERE fund_code = ? AND remaining_shares > 0
            """,
            (code,),
        ).fetchone()["s"]
        total_mv += mv * nav_row["v"]
    return total_mv - used_in_positions(conn)


def pending_committed(conn: sqlite3.Connection) -> float:
    """待确认订单已承诺、但尚未扣款的金额。

    为什么必须扣掉：
        场外基金是 T+1 确认，订单 pending 时还没记资金流水，
        但钱已经承诺出去了。如果不从可用预算中扣除，
        就能连续下多笔 pending 订单，总承诺远超可用额度——
        等 T+1 一起确认时直接透支。

        这是个隐蔽的坑：单笔检查都能过，累计起来才爆。
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS s FROM orders
        WHERE status = 'pending' AND side = 'buy'
        """
    ).fetchone()
    return row["s"] if row else 0.0


def available_budget(conn: sqlite3.Connection) -> float:
    """可用预算 = 初始资金 + 累积新增 − 已用金额 + 投资收益 − 待确认冻结。"""
    return (
        initial_capital(conn)
        + cumulative_added(conn)
        + test_capital(conn)
        - used_in_positions(conn)
        + realized_pnl(conn)
        - pending_committed(conn)
    )


def breakdown(conn: sqlite3.Connection) -> dict:
    """可用预算的完整分解，供界面展示和 AI 决策使用。"""
    from backend.engine import orders

    cash = orders.get_cash_balance(conn)
    avail = available_budget(conn)
    pending = pending_committed(conn)
    return {
        "initial": round(initial_capital(conn), 2),
        "cumulative_added": round(cumulative_added(conn), 2),
        "test_capital": round(test_capital(conn), 2),
        "used_in_positions": round(used_in_positions(conn), 2),
        "realized_pnl": round(realized_pnl(conn), 2),
        "unrealized_pnl": round(unrealized_pnl(conn), 2),
        "pending_committed": round(pending, 2),
        "available": round(avail, 2),
        "cash_balance": round(cash, 2),
        # 可用预算 = 现金 − 待确认冻结（pending 尚未扣现金）
        "consistent": abs(avail - (cash - pending)) < 0.01,
        "formula": "可用 = 初始 + 累积新增 − 持仓占用 + 已实现盈亏 − 待确认冻结",
    }


def check_consistency(conn: sqlite3.Connection) -> dict:
    """校验「预算口径」与「现金流口径」是否一致。

    这两个算法路径完全不同（一个从持仓批次算、一个从流水累加），
    如果结果对不上，说明某处账目记错了，必须查清楚而不是将就用。
    """
    from backend.engine import orders

    avail = available_budget(conn)
    cash = orders.get_cash_balance(conn)
    pending = pending_committed(conn)
    # pending 订单还没扣现金，所以现金侧要减去才是真正可动用的钱
    expected = cash - pending
    diff = avail - expected
    return {
        "budget_side": round(avail, 2),
        "cash_side": round(cash, 2),
        "pending_committed": round(pending, 2),
        "expected_available": round(expected, 2),
        "diff": round(diff, 2),
        "ok": abs(diff) < 0.01,
        "note": (
            "两个口径一致"
            if abs(diff) < 0.01
            else f"账目不一致！差额 {diff:.2f} 元，需逐笔核对 cash_ledger 与 lots"
        ),
    }


if __name__ == "__main__":
    from backend.db import get_connection, init_db

    init_db()
    conn = get_connection()

    init_capital(conn)
    add_monthly_budget(conn)

    b = breakdown(conn)
    print("=" * 78)
    print("资金预算（累积制）")
    print("=" * 78)
    print(f"  初始资金        {b['initial']:>10.2f} 元")
    print(f"  历史累积新增    {b['cumulative_added']:>10.2f} 元")
    print(f"  持仓占用（已用）{b['used_in_positions']:>10.2f} 元")
    print(f"  已实现盈亏      {b['realized_pnl']:>10.2f} 元")
    print(f"  浮动盈亏        {b['unrealized_pnl']:>10.2f} 元  （不计入可用）")
    print("  " + "-" * 46)
    print(f"  可用预算        {b['available']:>10.2f} 元")
    print(f"  现金余额        {b['cash_balance']:>10.2f} 元")
    print()
    print(f"  公式：{b['formula']}")
    print()

    ck = check_consistency(conn)
    print(f"  口径校验：{ck['note']}")
    if not ck["ok"]:
        print(f"    预算侧 {ck['budget_side']} vs 现金侧 {ck['cash_side']}")

    print("\n" + "=" * 78)
    print("说明：每月新增 1000 是往池子里累加，不是当月清零。")
    print("      盈亏会反过来影响可用预算——赚了能多投，亏了额度缩水。")
    print("=" * 78)
    conn.close()
