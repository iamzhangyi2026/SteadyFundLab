# -*- coding: utf-8 -*-
"""
金额 / 份额 / 费率计算的 Decimal 量化基座。

浮点是二进制近似表示，`0.1 + 0.2 != 0.3`，误差在账本里逐笔累积，
对账时差几分钱查无此案。本模块把「算」与「存」分开：

- 算：链路内一律 Decimal，中途不落 float；
- 存：量化到约定位数后转 float 写入（SQLite REAL 列保持不动——
  装机库 REAL→TEXT 迁移风险不成比例；量化后的值在 double 值级精确，
  修的正是计算链上的累积误差）。

量化位数（ROUND_HALF_UP，与基金公司份额 / 费用口径一致）：
- money  2 位（元）
- shares 4 位（份）
- rate   8 位（费率）

入参宽容：None / 空串 / 脏值按 0 处理，不向调用链抛异常
（流水管道里一个脏字段不该炸掉整笔业务）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_EXP = Decimal("0.01")
SHARES_EXP = Decimal("0.0001")
RATE_EXP = Decimal("0.00000001")

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value) -> Decimal:
    """任意入参转 Decimal。float 经 str 中转，避免二进制伪影
    （float(0.1) 直接转 Decimal 会得到 0.10000000000000000555...）。
    """
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool 是 int 子类，先挡掉，True 不当 1 元
        return ONE if value else ZERO
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ZERO
        try:
            return Decimal(s)
        except InvalidOperation:
            return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def q_money(value) -> Decimal:
    """量化到 2 位（元），ROUND_HALF_UP。"""
    return D(value).quantize(MONEY_EXP, rounding=ROUND_HALF_UP)


def q_shares(value) -> Decimal:
    """量化到 4 位（份），ROUND_HALF_UP。"""
    return D(value).quantize(SHARES_EXP, rounding=ROUND_HALF_UP)


def q_rate(value) -> Decimal:
    """量化到 8 位（费率），ROUND_HALF_UP。"""
    return D(value).quantize(RATE_EXP, rounding=ROUND_HALF_UP)


def money(value) -> float:
    """量化到 2 位后转 float（写入 REAL 列用）。"""
    return float(q_money(value))


def shares(value) -> float:
    """量化到 4 位后转 float。"""
    return float(q_shares(value))


def rate(value) -> float:
    """量化到 8 位后转 float。"""
    return float(q_rate(value))


def div(a, b) -> Decimal:
    """安全除法：b 为 0 时返回 0（脏分母不炸链路）。"""
    bd = D(b)
    if bd == 0:
        return ZERO
    return D(a) / bd


def pct(part, base) -> float:
    """百分比 = part / base × 100，量化到 2 位；base 为 0 时返回 0。"""
    based = D(base)
    if based == 0:
        return 0.0
    return float((D(part) / based * 100).quantize(MONEY_EXP, rounding=ROUND_HALF_UP))
