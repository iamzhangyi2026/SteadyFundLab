# -*- coding: utf-8 -*-
"""
基金费率计算（申购 / 赎回），离线安全。
口径（来自 config/config.toml ）：
- 外扣法申购：net = amount / (1 + fee_rate); fee = amount - net
- 管理费/托管费已在净值日扣，不在本模块复算
- 赎回费阶梯：from config [fee].redeem_tiers，命中第一条 max_holding_days >= holding_days
- penalty=true 档的 max_holding_days 即「免罚门槛」，penalty_free_days() 由其派生
"""
from __future__ import annotations

from ..db.schema import get_connection
from ..config import get, section


def _get_redeem_tiers_raw() -> list:
    return section("fee", {}).get("redeem_tiers", [])


def get_redeem_tiers() -> list:
    """返回配置中的赎回费阶梯列表（dict）。"""
    out = []
    for t in _get_redeem_tiers_raw():
        out.append({
            "max_holding_days": t.get("max_holding_days"),
            "rate": float(t.get("rate", 0.0)),
            "penalty": bool(t.get("penalty", False)),
            "note": t.get("note", ""),
        })
    return out


def penalty_free_days() -> int:
    """罚期天数：第一个 penalty=true 档的 max_holding_days，默认 7。"""
    for t in get_redeem_tiers():
        if t.get("penalty"):
            return int(t["max_holding_days"])
    return 7


def calc_purchase(code, amount, already_discounted=False, conn=None) -> dict:
    """
    申购费（外扣法）。
    fee_rate_raw: 基金原始申购费率（funds.purchase_fee_rate），缺失用 default_purchase_fee_rate
    fee_rate: 实际适用费率
        - already_discounted=True：视为已打折，fee_rate = 原始费率
        - 否则：fee_rate = 原始费率 * config [fee].purchase_discount
    外扣法：net = amount/(1+fee_rate); fee = amount - net
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT purchase_fee_rate FROM funds WHERE code=?", (code,)
        ).fetchone()
        raw_rate = None
        if row is not None:
            raw_rate = row["purchase_fee_rate"]
        if raw_rate is None:
            raw_rate = float(get("fee", "default_purchase_fee_rate", 0.015))
        discount = float(get("fee", "purchase_discount", 0.125))
        fee_rate = raw_rate if already_discounted else raw_rate * discount
        fee_rate = round(fee_rate, 8)
        denom = 1.0 + fee_rate
        net = amount / denom if denom else amount
        fee = amount - net
        return {
            "fee": round(fee, 4),
            "fee_rate": fee_rate,
            "fee_rate_raw": raw_rate,
            "net_amount": round(net, 4),
            "amount": amount,
        }
    finally:
        if own:
            conn.close()


def calc_redeem_fee(code, amount, holding_days, conn=None) -> dict:
    """
    赎回费：按自然持有天数命中赎回费阶梯。
    返回 {fee, fee_rate, penalty}
    """
    tiers = get_redeem_tiers()
    rate = 0.0
    penalty = False
    for t in tiers:
        mhd = t.get("max_holding_days")
        if mhd is not None and holding_days < int(mhd):
            rate = float(t.get("rate", 0.0))
            penalty = bool(t.get("penalty", False))
            break
    fee = amount * rate
    return {
        "fee": round(fee, 4),
        "fee_rate": rate,
        "penalty": penalty,
    }


if __name__ == "__main__":
    from ..db.schema import get_connection
    c = get_connection()
    try:
        print("calc_purchase(161725,1000):", calc_purchase("161725", 1000))
        print("calc_redeem_fee(161725,1000,3):", calc_redeem_fee("161725", 1000, 3))
        print("penalty_free_days():", penalty_free_days())
        print("redeem_tiers:", get_redeem_tiers())
    finally:
        c.close()
