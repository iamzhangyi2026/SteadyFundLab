# -*- coding: utf-8 -*-
"""B 区轮询会话窗口：决定「现在这个时刻该不该拉数据」。

规则：
- 盘中    09:30–15:00  → 拉（估值实时变）
- 净值披露  20:00–24:00  → 拉（当日净值陆续公布）
- 其余时段 → 不拉

时段一律按本机时区判定，与 A 股交易时刻同口径。

交易日判断分两类：
- 国内交易日：复用 A 区交易所日历（含法定节假日与调休）
- 海外交易日：holidays 库（美 / 港），仅用于 QDII 基金

设计中为什么用"或"而不是"且"：同时持有境内基金与 QDII 时，
只要境内开市或海外开市，就都有新数据可拉——有一边在交易就该拉。
"""
from __future__ import annotations

from datetime import datetime, time as dtime

_CN_SESSION = (dtime(9, 30), dtime(15, 0))
_NAV_WINDOW = (dtime(20, 0), dtime(23, 59, 59))

# QDII 特征词：命中任一即视为海外基金
_QDII_HINTS = ("QDII", "纳斯达克", "标普", "道琼斯", "恒生", "港股", "美国", "日本",
               "全球", "原油", "黄金", "亚洲", "海外", "德国", "印度", "越南")


_CAL = None


def _cn_trading_day(d) -> bool:
    """国内是否交易日。日历不可用时退化为「非周末即交易日」。"""
    global _CAL
    try:
        from backend.engine.calendar import TradeCalendar

        if _CAL is None:
            _CAL = TradeCalendar()
            _CAL.load()
        return bool(_CAL.is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def _overseas_open(d) -> bool:
    """海外（美 / 港）是否有市场在交易。holidays 不可用时退化为非周末。"""
    try:
        import holidays

        us = holidays.US(years=[d.year])
        hk = holidays.HK(years=[d.year])
        return (d.weekday() < 5 and d not in us) or (d.weekday() < 5 and d not in hk)
    except Exception:
        return d.weekday() < 5


def _is_qdii(name: str) -> bool:
    return any(k in name for k in _QDII_HINTS)


def poll_window(holdings: list | None = None) -> dict:
    """返回当前是否应轮询，以及原因 / 下一次窗口开启时间。"""
    now = datetime.now()
    d = now.date()
    t = now.time()
    names = " ".join((h.get("fund_name") or "") for h in (holdings or []))
    has_qdii = _is_qdii(names)

    # 盘中：国内开市才有实时估值
    if _cn_trading_day(d) and _CN_SESSION[0] <= t < _CN_SESSION[1]:
        return _result(True, "盘中 · 实时估值", now, now)

    # 净值披露窗口：国内开市（境内基金净值）或 QDII 持仓且海外开市
    if _NAV_WINDOW[0] <= t < _NAV_WINDOW[1]:
        if _cn_trading_day(d):
            return _result(True, "净值披露 · 境内净值公布", now, now)
        if has_qdii and _overseas_open(d):
            return _result(True, "净值披露 · 海外市场交易中", now, now)
        return _result(False, "休市 · 当日无净值可拉", now, _next_open(now, has_qdii))

    return _result(False, "闭市 · 停止轮询", now, _next_open(now, has_qdii))


def _result(active: bool, reason: str, now: datetime, next_change: datetime) -> dict:
    return {
        "active": active,
        "reason": reason,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "next_change": next_change.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _next_open(now: datetime, has_qdii: bool = False) -> datetime:
    """下一次进入轮询窗口的时刻（供界面提示「下次开启于 X」）。"""
    from datetime import timedelta

    probe = now + timedelta(minutes=5)
    ends = now + timedelta(days=10)
    while probe < ends:
        d, t = probe.date(), probe.time()
        if _cn_trading_day(d) or (has_qdii and _overseas_open(d)):
            if _CN_SESSION[0] <= t < _CN_SESSION[1] or _NAV_WINDOW[0] <= t < _NAV_WINDOW[1]:
                return probe
        probe += timedelta(minutes=5)
    return probe
