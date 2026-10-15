# -*- coding: utf-8 -*-
"""
现金流水账与预算（money-safe）。

流水记账约定（与真实 cash_ledger 数据一致）：
- 流入类（init/monthly_quota/redeem/dividend/refund/cancel_refund）记正数
- 申购 purchase 记负数（扣款）
- balance_after 为滚动余额，每次写入重算
"""
from __future__ import annotations

from datetime import date, datetime

from ..db.schema import get_connection
from ..config import get
from .holdings import _latest_nav
from . import fees

# 流入类：金额记正；purchase 记负
INFLOW_TYPES = {"init", "monthly_quota", "redeem", "dividend", "refund", "cancel_refund"}


def record_cash(conn, txn_type, amount, ref_order_id=None, note="", txn_date=None,
                commit: bool = True) -> dict:
    """记录一笔现金流水，返回写入行。balance_after 基于最新余额滚动。

    `commit=False` 供**事务型调用方**使用（如 `orders.confirm_order`）：
    由调用方在整笔业务完成后统一提交，中途失败可整体回滚。
    """
    prev = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_bal = float(prev["balance_after"]) if prev else 0.0
    delta = abs(amount) if txn_type != "purchase" else -abs(amount)
    new_bal = round(prev_bal + delta, 4)
    if txn_date is None:
        txn_date = date.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "INSERT INTO cash_ledger (txn_date, txn_type, amount, balance_after, ref_order_id, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (txn_date, txn_type, delta, new_bal, ref_order_id, note, now),
    )
    if commit:
        conn.commit()
    return {
        "id": cur.lastrowid,
        "txn_type": txn_type,
        "amount": delta,
        "balance_after": new_bal,
        "txn_date": txn_date,
        "ref_order_id": ref_order_id,
        "note": note,
    }


def months_since(start_month: str) -> int:
    """从 official_start_month 到当前月份经过的完整月数（>=0，累积制不清零）。"""
    if not start_month:
        return 0
    try:
        sy, sm = start_month.split("-")[:2]
        start = int(sy) * 12 + int(sm)
        today = date.today()
        cur = today.year * 12 + today.month
        return max(0, cur - start)
    except Exception:
        return 0


def get_budget(conn) -> dict:
    """
    预算快照。
    - initial = config [capital].initial_cash
    - cumulative_added = monthly_quota * months_since official_start_month
    - cash_balance = 最新 cash_ledger.balance_after（无流水时用公式）
    - available = cash_balance
    - used_in_positions = 活跃 lots 的 cost_amount 之和
    - pending_committed = 待确认买单的 amount 之和
    - realized_pnl / unrealized_pnl 由流水与持仓推导
    """
    initial = float(get("capital", "initial_cash", 0.0))
    monthly = float(get("capital", "monthly_quota", 0.0))
    start_month = get("capital", "official_start_month", "")
    months = months_since(start_month)
    cumulative_added = round(monthly * months, 4)

    row = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cash_balance = float(row["balance_after"]) if row is not None else (initial + cumulative_added)

    used = conn.execute(
        "SELECT COALESCE(SUM(cost_amount),0) FROM lots WHERE status='active'"
    ).fetchone()[0] or 0.0
    used = float(used)

    pending = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='pending' AND side='buy'"
    ).fetchone()[0] or 0.0
    pending_committed = float(pending)

    market_value = 0.0
    for l in conn.execute(
        "SELECT fund_code, SUM(remaining_shares) rs, SUM(cost_amount) cost "
        "FROM lots WHERE status='active' GROUP BY fund_code"
    ):
        nav = _latest_nav(conn, l["fund_code"])
        mv = float(l["rs"]) * nav if nav is not None else 0.0
        market_value += mv
    market_value = round(market_value, 4)
    unrealized_pnl = round(market_value - used, 4)
    realized_pnl = round(cash_balance - initial - cumulative_added + used, 4)

    return {
        "initial": initial,
        "cumulative_added": cumulative_added,
        "used_in_positions": used,
        "pending_committed": pending_committed,
        "available": cash_balance,
        "cash_balance": cash_balance,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "market_value": market_value,
    }


def preview_consume_lots(conn, code, shares) -> dict:
    """
    FIFO 只读预览：给定基金与拟赎回份额，按确认日升序消耗活跃批次。
    返回 {consumed:[{lot_id,shares,holding_days,cost}], total_cost}（不写库）。
    """
    rows = conn.execute(
        "SELECT lot_id, remaining_shares, cost_nav, cost_amount, confirm_date FROM lots "
        "WHERE fund_code=? AND status='active' AND remaining_shares>0 "
        "ORDER BY confirm_date ASC, lot_id ASC",
        (code,),
    ).fetchall()
    consumed = []
    total_cost = 0.0
    remain = float(shares)
    today = date.today()
    for r in rows:
        if remain <= 1e-9:
            break
        avail = float(r["remaining_shares"])
        take = min(remain, avail)
        holding_days = 0
        try:
            cd = date.fromisoformat(r["confirm_date"])
            holding_days = (today - cd).days
        except Exception:
            holding_days = 0
        per_share_cost = (float(r["cost_amount"]) / avail) if avail > 0 else 0.0
        cost = take * per_share_cost
        consumed.append({
            "lot_id": r["lot_id"],
            "shares": round(take, 4),
            "holding_days": holding_days,
            "cost": round(cost, 4),
        })
        total_cost += cost
        remain -= take
    return {"consumed": consumed, "total_cost": round(total_cost, 4)}


def position_summary(conn, fund_code: str, current_nav: float) -> dict:
    """单只基金持仓汇总（供纪律线检查用）。

    current_nav 由调用方按用途传入：纪律线应传**累计净值 acc_nav**。
    返回 {total_shares, total_cost, market_value, profit_pct}。

    口径（纪律线用 acc_nav、市值/浮盈用 nav）：
    - market_value = 份额 × current_nav（纪律线口径用累计净值）
    - profit_pct   = (current_nav − 成本累计净值) / 成本累计净值 × 100
      成本累计净值按各批次确认日查 nav_daily.acc_nav（无则回退 unit nav），
      再按份额加权 —— 这才是含分红再投的真实总收益，符合「纪律线用 acc_nav」。
    """
    rows = conn.execute(
        "SELECT shares, remaining_shares, cost_amount, cost_nav, confirm_date "
        "FROM lots WHERE fund_code=? AND status='active' AND remaining_shares>0",
        (fund_code,),
    ).fetchall()
    total_shares = sum(float(r["remaining_shares"]) for r in rows)
    # 部分赎回后只把剩余份额对应的成本计入持仓，避免继续使用整批原始成本。
    total_cost = sum(
        float(r["cost_amount"]) * float(r["remaining_shares"]) / float(r["shares"])
        for r in rows
        if float(r["shares"] or 0) > 0
    )
    if total_shares <= 0:
        return {"total_shares": 0.0, "total_cost": 0.0, "market_value": 0.0, "profit_pct": 0.0}

    # 成本累计净值（份额加权）
    weighted_cost_acc = 0.0
    for r in rows:
        sh = float(r["remaining_shares"])
        cd = r["confirm_date"]
        cost_acc = None
        if cd:
            try:
                row_acc = conn.execute(
                    "SELECT COALESCE(acc_nav, nav) AS a FROM nav_daily "
                    "WHERE fund_code=? AND nav_date <= ? ORDER BY nav_date DESC LIMIT 1",
                    (fund_code, cd),
                ).fetchone()
                if row_acc and row_acc["a"] is not None:
                    cost_acc = float(row_acc["a"])
            except Exception:
                cost_acc = None
        if cost_acc is None:
            cost_acc = float(r["cost_nav"]) if r["cost_nav"] else 0.0
        weighted_cost_acc += sh * cost_acc

    avg_cost_acc = weighted_cost_acc / total_shares if total_shares else 0.0
    market_value = total_shares * current_nav
    profit_pct = ((current_nav - avg_cost_acc) / avg_cost_acc * 100) if avg_cost_acc else 0.0
    return {
        "total_shares": round(total_shares, 4),
        "total_cost": round(total_cost, 2),
        "market_value": round(market_value, 2),
        "profit_pct": round(profit_pct, 2),
    }


def create_lot(conn, fund_code, order_id, confirm_date, shares, nav, amount,
               commit: bool = True) -> int:
    """申购/红利再投确认后建一个份额批次（lot）。返回 lot_id。

    `commit=False` 供事务型调用方使用（见 `record_cash` 说明）。
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

    返回 consumed 列表，每元素含 lot_id/shares/holding_days/amount(成本)/fee/is_penalty。
    amount 为该批次被消耗部分的成本金额；fee 由赎回费阶梯（自然日）计算。

    `commit=False` 供事务型调用方使用（见 `record_cash` 说明）。
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
        market_amount = take * nav   # 赎回市值 = 赎回份额 × 赎回日单位净值（市值/成交用 nav）
        fr = fees.calc_redeem_fee(fund_code, market_amount, holding_days)
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
