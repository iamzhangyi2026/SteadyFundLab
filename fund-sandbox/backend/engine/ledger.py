# -*- coding: utf-8 -*-
"""
现金流水账与份额批次（money-safe）。

流水记账约定（词汇表与 `engine.budget` 的常量一致）：
- 流入类记正数：initial(初始资金) | monthly_add(月度新增) | test_capital(沙盒测试资金)
                | redeem(赎回到账) | dividend(分红) | refund(撤单退款)
- 申购 purchase 记负数（扣款）
- balance_after 为滚动余额，每次写入重算

预算口径见 `get_budget`——与 `engine.budget.available_budget` 完全一致，
界面展示与下单校验共用同一套数，不允许出现两个口径。
"""
from __future__ import annotations

from datetime import date, datetime

from .holdings import _latest_nav
from . import fees
from . import money as _m


def get_budget(conn) -> dict:
    """预算快照。口径与 `engine.budget.available_budget` **完全一致**——
    界面展示与下单校验共用同一套数：

    - initial / cumulative_added / test_capital：读实际资金流水，不按 config×月数推
    - used_in_positions：按批次剩余份额折算，部分赎回后只算未赎回部分成本
    - pending_committed：待确认买单金额（已承诺未扣款）
    - available = initial + cumulative_added + test_capital
                 − used_in_positions + realized_pnl − pending_committed
      （必须扣待确认冻结，否则界面比引擎实际允许买的多，用户看着能买、一点就被拒）
    - realized_pnl = 累计赎回 + 累计分红 − 已赎回部分成本
    """
    from backend.engine import budget as _budget  # 延迟导入避免循环依赖

    market_value = 0.0
    for l in conn.execute(
        "SELECT fund_code, SUM(remaining_shares) rs FROM lots WHERE status='active' GROUP BY fund_code"
    ):
        nav = _latest_nav(conn, l["fund_code"])
        mv = float(l["rs"]) * nav if nav is not None else 0.0
        market_value += mv
    market_value = round(market_value, 4)
    used = _budget.used_in_positions(conn)

    row = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cash_balance = float(row["balance_after"]) if row is not None else 0.0

    return {
        "initial": _budget.initial_capital(conn),
        "cumulative_added": _budget.cumulative_added(conn),
        "test_capital": _budget.test_capital(conn),
        "used_in_positions": used,
        "pending_committed": _budget.pending_committed(conn),
        "available": _budget.available_budget(conn),
        "cash_balance": cash_balance,
        "realized_pnl": _budget.realized_pnl(conn),
        "unrealized_pnl": round(market_value - used, 4),
        "market_value": market_value,
    }


def preview_consume_lots(conn, code, shares) -> dict:
    """
    FIFO 只读预览：给定基金与拟赎回份额，按确认日升序消耗活跃批次。
    返回 {consumed:[{lot_id,confirm_date,shares,holding_days,cost}], total_cost}（不写库）。
    confirm_date 供调用方做最短持有期锁定判断（见 orders._locked_lots）。
    成本口径与 used_in_positions / position_summary 一致：按批次**原始份额**
    折算（cost_amount / shares × 取用份额）——部分赎回过的批次 cost_amount
    仍是整批成本，除剩余份额会把成本高估。
    """
    rows = conn.execute(
        "SELECT lot_id, remaining_shares, shares, cost_nav, cost_amount, confirm_date FROM lots "
        "WHERE fund_code=? AND status='active' AND remaining_shares>0 "
        "ORDER BY confirm_date ASC, lot_id ASC",
        (code,),
    ).fetchall()
    consumed = []
    total_cost = _m.ZERO
    remain = _m.D(shares)
    today = datetime.now().date()
    dust = _m.D("0.00000001")
    for r in rows:
        if remain <= dust:
            break
        avail = _m.D(r["remaining_shares"])
        orig = _m.D(r["shares"])
        take = min(remain, avail)
        holding_days = 0
        try:
            cd = date.fromisoformat(r["confirm_date"])
            holding_days = (today - cd).days
        except Exception:
            holding_days = 0
        per_share_cost = _m.div(r["cost_amount"], orig)
        cost = _m.q_money(take * per_share_cost)
        consumed.append({
            "lot_id": r["lot_id"],
            "confirm_date": r["confirm_date"],
            "shares": _m.shares(take),
            "holding_days": holding_days,
            "cost": float(cost),
        })
        total_cost += cost
        remain -= take
    return {"consumed": consumed, "total_cost": float(_m.q_money(total_cost))}


def position_summary(conn, fund_code: str, current_nav: float) -> dict:
    """单只基金持仓汇总（供纪律线检查用）。

    current_nav 由调用方按用途传入：纪律线应传**累计净值 acc_nav**。
    返回 {total_shares, total_cost, market_value, profit_pct}。

    口径（纪律线用 acc_nav、市值/浮盈用 nav）：
    - market_value = 份额 × current_nav（纪律线口径用累计净值）
    - profit_pct   = (current_nav − 成本累计净值) / 成本累计净值 × 100
      成本累计净值按各批次确认日**精确**查 nav_daily.acc_nav（当日无记录则
      回退该批次成交净值 cost_nav，不回退更早日期——净值日缺失时用旧价会
      让纪律线的盈亏失真），再按份额加权 —— 这才是含分红再投的真实总收益，
      符合「纪律线用 acc_nav」。
    """
    rows = conn.execute(
        "SELECT shares, remaining_shares, cost_amount, cost_nav, confirm_date "
        "FROM lots WHERE fund_code=? AND status='active' AND remaining_shares>0",
        (fund_code,),
    ).fetchall()
    total_shares = _m.D(sum(_m.D(r["remaining_shares"]) for r in rows))
    # 部分赎回后只把剩余份额对应的成本计入持仓，避免继续使用整批原始成本。
    total_cost = sum(
        (_m.D(r["cost_amount"]) * _m.D(r["remaining_shares"]) / _m.D(r["shares"])
         for r in rows if _m.D(r["shares"]) > 0),
        _m.ZERO,
    )
    if total_shares <= 0:
        return {"total_shares": 0.0, "total_cost": 0.0, "market_value": 0.0, "profit_pct": 0.0}

    # 成本累计净值（份额加权）
    weighted_cost_acc = _m.ZERO
    for r in rows:
        sh = _m.D(r["remaining_shares"])
        cd = r["confirm_date"]
        cost_acc = None
        if cd:
            try:
                row_acc = conn.execute(
                    "SELECT COALESCE(acc_nav, nav) AS a FROM nav_daily "
                    "WHERE fund_code=? AND nav_date = ?",
                    (fund_code, cd),
                ).fetchone()
                if row_acc and row_acc["a"] is not None:
                    cost_acc = _m.D(row_acc["a"])
            except Exception:
                cost_acc = None
        if cost_acc is None:
            cost_acc = _m.D(r["cost_nav"]) if r["cost_nav"] else _m.ZERO
        weighted_cost_acc += sh * cost_acc

    nav_d = _m.D(current_nav)
    avg_cost_acc = _m.div(weighted_cost_acc, total_shares)
    market_value = total_shares * nav_d
    profit_pct = _m.pct(nav_d - avg_cost_acc, avg_cost_acc)
    return {
        "total_shares": _m.shares(total_shares),
        "total_cost": _m.money(total_cost),
        "market_value": _m.money(market_value),
        "profit_pct": profit_pct,
    }


def create_lot(conn, fund_code, order_id, confirm_date, shares, nav, amount,
               commit: bool = True) -> int:
    """申购/红利再投确认后建一个份额批次（lot）。返回 lot_id。

    `commit=False` 供事务型调用方使用（如 `orders._confirm_core` 的 `with conn:`
    整笔业务）：由调用方统一提交，中途失败可整体回滚。
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        """INSERT INTO lots
           (fund_code, order_id, confirm_date, shares, remaining_shares,
            cost_nav, cost_amount, status, created_at)
           VALUES (?,?,?,?,?,?,?, 'active', ?)""",
        (fund_code, order_id, confirm_date, shares, shares, nav, amount, now),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def consume_lots(conn, fund_code, shares, nav_date, nav, commit: bool = True) -> list:
    """赎回 FIFO 消耗批次（写库）。按 confirm_date 升序消耗 active lots。

    返回 consumed 列表，每元素含 lot_id/shares/holding_days/amount/fee/is_penalty。
    amount 为该批次被消耗份额的**市值**（份额 × 赎回日单位净值，调用方用作 gross），
    不是成本；成本口径见 `preview_consume_lots` 的 total_cost。fee 由赎回费阶梯
    （自然日：nav_date − confirm_date）计算——优先用 fund_fee 表里该基金的逐基金
    阶梯（同步过才有），没有才回退全局阶梯。

    `commit=False` 供事务型调用方使用（见 `create_lot` 说明）。
    """
    rows = conn.execute(
        "SELECT lot_id, remaining_shares, cost_nav, cost_amount, confirm_date FROM lots "
        "WHERE fund_code=? AND status='active' AND remaining_shares>0 "
        "ORDER BY confirm_date ASC, lot_id ASC",
        (fund_code,),
    ).fetchall()
    consumed = []
    remain = float(shares)
    nav_d = None
    try:
        nav_d = date.fromisoformat(nav_date)
    except Exception:
        nav_d = None
    for r in rows:
        if remain <= 1e-9:
            break
        avail = float(r["remaining_shares"])
        take = min(remain, avail)
        holding_days = 0
        if nav_d is not None and r["confirm_date"]:
            try:
                holding_days = (nav_d - date.fromisoformat(r["confirm_date"])).days
            except Exception:
                holding_days = 0
        market_amount = _m.money(take * nav)   # 赎回市值 = 赎回份额 × 赎回日单位净值（市值/成交用 nav）
        fr = fees.calc_redeem_fee(fund_code, market_amount, holding_days, conn=conn)
        new_remain = round(avail - take, 6)
        if new_remain <= 1e-9:
            conn.execute(
                "UPDATE lots SET remaining_shares=0, status='exhausted' WHERE lot_id=?",
                (r["lot_id"],),
            )
        else:
            conn.execute(
                "UPDATE lots SET remaining_shares=? WHERE lot_id=?",
                (new_remain, r["lot_id"]),
            )
        consumed.append({
            "lot_id": r["lot_id"],
            "shares": round(take, 4),
            "holding_days": holding_days,
            "amount": round(market_amount, 4),   # 赎回总额（市值口径），orders.py 用作 gross
            "fee": fr["fee"],
            "is_penalty": fr["penalty"],
        })
        remain -= take
    if commit:
        conn.commit()
    return consumed


def holding_report(conn, fund_code, as_of) -> list:
    """当前持仓批次报告（只读）。as_of 为 date 对象。

    每元素含 confirm_date/shares/holding_days/is_penalty/days_to_penalty_free。
    is_penalty 与赎回费阶梯一致：持有天数 < penalty_free_days() 视为惩罚期。
    """
    rows = conn.execute(
        "SELECT fund_code, lot_id, remaining_shares, cost_nav, cost_amount, confirm_date FROM lots "
        "WHERE fund_code=? AND status='active' AND remaining_shares>0 "
        "ORDER BY confirm_date ASC, lot_id ASC",
        (fund_code,),
    ).fetchall()
    penalty_days = fees.penalty_free_days()
    out = []
    for r in rows:
        holding_days = 0
        if r["confirm_date"]:
            try:
                holding_days = (as_of - date.fromisoformat(r["confirm_date"])).days
            except Exception:
                holding_days = 0
        is_penalty = holding_days < penalty_days
        days_to_penalty_free = max(0, penalty_days - holding_days) if is_penalty else 0
        # 赎回费率按持有天数阶梯算（amount=0 不影响 fee_rate，只影响金额），供前端 LOTS 卡「现在卖的话」列
        fr = fees.calc_redeem_fee(r["fund_code"], 0.0, holding_days, conn=conn)
        fee_rate = fr.get("fee_rate", 0.0)
        out.append({
            "lot_id": r["lot_id"],
            "confirm_date": r["confirm_date"],
            "shares": float(r["remaining_shares"]),
            "holding_days": holding_days,
            "is_penalty": is_penalty,
            "days_to_penalty_free": days_to_penalty_free,
            "fee_rate": fee_rate,
        })
    return out


if __name__ == "__main__":
    from ..db.schema import get_connection
    c = get_connection()
    try:
        print("get_budget:", get_budget(c))
    finally:
        c.close()
