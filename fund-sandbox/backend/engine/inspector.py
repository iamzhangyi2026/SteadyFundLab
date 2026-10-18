# -*- coding: utf-8 -*-
"""
巡检（inspector）引擎：每日 09:00 健康检查。

检查项（每项写入一条 data_health，source='inspector'）：
  1. nav_daily 新鲜度（最新净值日期 vs 今天）
  2. funds 候选池数量（status='active'）
  3. orders 待处理订单数量
  4. cash_ledger 最新现金余额
返回汇总摘要。
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, List

from ..db.schema import record_health, rows_to_dicts


def _today() -> date:
    return datetime.now().date()


def _diff_days(latest: str) -> int:
    try:
        d = datetime.strptime(str(latest)[:10], "%Y-%m-%d").date()
        return (_today() - d).days
    except Exception:
        return 9999


def run_inspection(conn) -> dict:
    """执行健康检查，写入多条 data_health 行，返回汇总。"""
    summary: Dict[str, Any] = {}

    # 1) nav_daily 新鲜度
    try:
        latest = conn.execute("SELECT MAX(nav_date) FROM nav_daily").fetchone()[0]
        n = conn.execute("SELECT COUNT(*) FROM nav_daily").fetchone()[0]
    except Exception:
        latest, n = None, 0
    if not latest:
        status, msg = "fail", "nav_daily 无数据"
    else:
        diff = _diff_days(latest)
        if diff <= 1:
            status, msg = "ok", f"最新净值日期 {latest}（{diff} 天前）"
        elif diff <= 7:
            status, msg = "warn", f"净值偏旧：最新 {latest}（{diff} 天前）"
        else:
            status, msg = "fail", f"净值严重滞后：最新 {latest}（{diff} 天前）"
    record_health(conn, "inspector", "nav_daily", status, n, msg)
    summary["nav_daily"] = {"latest": latest, "count": n, "status": status, "message": msg}

    # 2) funds 候选池
    try:
        active = conn.execute("SELECT COUNT(*) FROM funds WHERE status='active'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM funds").fetchone()[0]
    except Exception:
        active, total = 0, 0
    status = "ok" if active > 0 else "fail"
    msg = f"active={active}, total={total}"
    record_health(conn, "inspector", "funds", status, active, msg)
    summary["funds"] = {"active": active, "total": total, "status": status}

    # 3) orders 待处理
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0]
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    except Exception:
        pending, total_orders = 0, 0
    status = "ok" if pending == 0 else "warn"
    msg = f"pending={pending}, total={total_orders}"
    record_health(conn, "inspector", "orders", status, pending, msg)
    summary["orders"] = {"pending": pending, "total": total_orders, "status": status}

    # 4) cash_ledger 余额
    try:
        bal = conn.execute(
            "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        balance = bal[0] if bal else None
        n_ledger = conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0]
    except Exception:
        balance, n_ledger = None, 0
    if balance is None:
        status, msg = "warn", "cash_ledger 无余额记录"
    elif balance < 0:
        status, msg = "fail", f"现金余额为负：{balance}"
    else:
        status, msg = "ok", f"当前余额 {balance}"
    record_health(conn, "inspector", "cash_ledger", status, n_ledger, msg)
    summary["cash_ledger"] = {"balance": balance, "count": n_ledger, "status": status}

    return {"source": "inspector", "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "checks": summary}


def get_inspection(conn, limit: int = 20) -> list:
    """最近 limit 条 inspector 健康检查记录。"""
    rows = conn.execute(
        "SELECT * FROM data_health WHERE source='inspector' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows_to_dicts(rows)


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("== run_inspection ==")
        print(run_inspection(conn))
        print("\n== get_inspection ==")
        for r in get_inspection(conn, 5):
            print(r)
    finally:
        conn.close()
