# -*- coding: utf-8 -*-
"""
持仓监控：跑纪律检查 + 组合快照。
"""
from __future__ import annotations

from datetime import datetime

from ..db.schema import get_connection
from . import rules, portfolio


def monitor_positions(conn) -> dict:
    as_of = datetime.now().date().isoformat()
    alerts = rules.check_discipline_all(conn, as_of)
    snap = portfolio.snapshot_portfolio(conn)
    codes = set()
    for a in alerts:
        if "fund_code" in a:
            codes.add(a["fund_code"])
    triggered = [a for a in alerts if a.get("action_taken") == "alert_created"]
    return {
        "as_of": as_of,
        "positions_checked": len(codes),
        "alerts": alerts,
        "triggered_count": len(triggered),
        "snapshot": snap,
    }


if __name__ == "__main__":
    from ..db.schema import get_connection
    c = get_connection()
    try:
        print(monitor_positions(c))
    finally:
        c.close()
