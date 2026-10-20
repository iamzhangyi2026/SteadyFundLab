# -*- coding: utf-8 -*-
"""
基金费率计算（申购 / 赎回），离线安全。
口径（来自 config/config.toml）：
- 外扣法申购：net = amount / (1 + fee_rate); fee = amount - net
- 管理费/托管费已在净值日扣，不在本模块复算
- 赎回费阶梯：优先用 fund_fee 表里该基金的逐基金阶梯（rule_watch 同步过才有），
  没有才回退 config [fee].redeem_tiers；命中口径为左闭右开区间
  [days_min, days_max)，即 holding_days < max_holding_days 的第一条档
  （正好持有 7 天不收惩罚费，与 penalty_free_days 的判定一致）
- penalty=true 档的 max_holding_days 即「免罚门槛」，penalty_free_days() 由其派生
- 金额 / 费率全程 Decimal 计算，量化后返回（金额 2 位、费率 8 位）
"""
from __future__ import annotations

from ..db.schema import get_connection
from ..config import get, section
from .money import D, ONE, money, q_money, q_rate


def _get_redeem_tiers_raw() -> list:
    return section("fee", {}).get("redeem_tiers", [])


def get_redeem_tiers() -> list:
    """返回配置中的赎回费阶梯列表（dict），按 max_holding_days 升序。

    排序是必须的：命中逻辑是「第一条 max_holding_days >= holding_days 的档」，
    靠 config 手写顺序的话，档位哪天被改乱（两条颠倒），费用会静默算错。
    """
    out = []
    for t in _get_redeem_tiers_raw():
        out.append({
            "max_holding_days": t.get("max_holding_days"),
            "rate": float(t.get("rate", 0.0)),
            "penalty": bool(t.get("penalty", False)),
            "note": t.get("note", ""),
        })
    out.sort(key=lambda t: (t["max_holding_days"] is None,
                            t["max_holding_days"] or 0))
    return out


def penalty_free_days() -> int:
    """罚期天数：第一个 penalty=true 档的 max_holding_days，默认 7。"""
    for t in get_redeem_tiers():
        if t.get("penalty"):
            return int(t["max_holding_days"])
    return 7


def _normalize_tiers(tiers: list) -> list:
    """把两种来源的阶梯统一成 (days_min, days_max, rate)，按 days_min 升序。

    config 全局阶梯只有上限 max_holding_days；逐基金阶梯（fund_fee）是
    [days_min, days_max) 区间。统一后才能用同一套命中逻辑。
    """
    out = []
    for t in tiers:
        if "max_holding_days" in t:
            out.append((None, t.get("max_holding_days"), float(t.get("rate", 0.0))))
        else:
            rate = t.get("rate")
            if rate is None:
                continue  # 抓取时没解析出费率的档位不可信，跳过
            out.append((t.get("days_min"), t.get("days_max"), float(rate)))
    out.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0))
    return out


def _match_tier(tiers: list, holding_days: int) -> float | None:
    """按持有天数命中阶梯：days_min <= holding_days < days_max。返回费率或 None。"""
    for dmin, dmax, rate in tiers:
        if dmin is not None and holding_days < dmin:
            continue
        if dmax is not None and holding_days >= dmax:
            continue
        return rate
    return None


def _per_fund_tiers(conn, code) -> list:
    """读 fund_fee 表里该基金的逐基金赎回费阶梯；没有/解析失败返回 []。"""
    if not conn or not code:
        return []
    from backend.engine import rule_watch  # 延迟导入避免循环依赖

    try:
        return rule_watch.redeem_tiers_of(conn, code) or []
    except Exception:
        return []


def calc_purchase(code, amount, already_discounted=True, conn=None) -> dict:
    """
    申购费（外扣法）。
    fee_rate_raw: 基金原始申购费率（funds.purchase_fee_rate），缺失用 default_purchase_fee_rate
    fee_rate: 实际适用费率
        - already_discounted=True（默认）：funds.purchase_fee_rate 视为已打折，fee_rate = 原始费率
        - False：fee_rate = 原始费率 * config [fee].purchase_discount
          （仅用于费率确定未打折的数据源；对已折扣费率再打折会把费用算小一个量级）
    外扣法：net = amount/(1+fee_rate); fee = amount - net
    全程 Decimal 计算，量化后返回（费率 8 位、金额 2 位）。
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
        amount_d = D(amount)
        raw_rate_d = D(raw_rate)
        discount = D(get("fee", "purchase_discount", 0.125))
        fee_rate_d = raw_rate_d if already_discounted else raw_rate_d * discount
        fee_rate_d = q_rate(fee_rate_d)
        denom = ONE + fee_rate_d
        # 净额先量化到分，费用 = 总额 − 净额：三者（amount / fee / net）严格自洽，
        # 不会出现「费 + 净 ≠ 总额」的分币差。份额按量化后的净额算（与基金公司
        # 先定净申购金额再除净值的口径一致）。
        net = q_money(amount_d / denom) if denom else amount_d
        fee = amount_d - net
        return {
            "fee": money(fee),
            "fee_rate": float(fee_rate_d),
            "fee_rate_raw": float(q_rate(raw_rate_d)),
            "net_amount": money(net),
            "amount": money(amount_d),
        }
    finally:
        if own:
            conn.close()


def calc_redeem_fee(code, amount, holding_days, conn=None) -> dict:
    """
    赎回费：按自然持有天数命中赎回费阶梯。
    优先用 fund_fee 表里该基金的逐基金阶梯（rule_watch 同步过才有），
    没有才回退 config 全局阶梯——C 类份额费率与全局阶梯不同，
    全走全局阶梯会算错。逐基金档位覆盖不到该持有天数时也回退全局阶梯
    （宁可多收不可漏收，money-safe）。
    返回 {fee, fee_rate, penalty, tier_source}
    """
    own = conn is None
    if own and code:
        conn = get_connection()
    try:
        rate = None
        tier_source = "config"
        fund_tiers = _normalize_tiers(_per_fund_tiers(conn, code))
        if fund_tiers:
            rate = _match_tier(fund_tiers, holding_days)
            if rate is not None:
                tier_source = "fund_fee"
        if rate is None:
            rate = _match_tier(_normalize_tiers(get_redeem_tiers()), holding_days)
        penalty = holding_days < penalty_free_days()
        if rate is None:
            rate = 0.0
        fee = D(amount) * D(rate)
        return {
            "fee": money(fee),
            "fee_rate": rate,
            "penalty": penalty,
            "tier_source": tier_source,
        }
    finally:
        if own and conn is not None:
            conn.close()


if __name__ == "__main__":
    from ..db.schema import get_connection
    c = get_connection()
    try:
        print("calc_purchase(040001,1000):", calc_purchase("040001", 1000))
        print("calc_redeem_fee(040001,1000,3):", calc_redeem_fee("040001", 1000, 3))
        print("penalty_free_days():", penalty_free_days())
        print("redeem_tiers:", get_redeem_tiers())
    finally:
        c.close()
