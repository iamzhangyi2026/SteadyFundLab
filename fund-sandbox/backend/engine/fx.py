# -*- coding: utf-8 -*-
"""
汇率（fx）引擎：USD/CNY 等。

权威库 fx_rate 表：(pair TEXT, rate_date TEXT, rate REAL, source, fetched_at) 复合主键 (pair, rate_date)。
离线安全：表为空或查询失败时 latest_usd_cny 降级到 ~7.25。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from ..db.schema import get_connection, row_to_dict

DEFAULT_USD_CNY = 7.25


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def latest_usd_cny(conn: sqlite3.Connection) -> dict:
    """读取 USD/CNY 最新汇率；空表/异常时降级到 7.25。"""
    try:
        row = conn.execute(
            "SELECT * FROM fx_rate WHERE pair='USD/CNY' "
            "ORDER BY rate_date DESC, fetched_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        row = None
    if row:
        return {"rate": row["rate"], "date": row["rate_date"], "source": row["source"]}
    return {"rate": DEFAULT_USD_CNY, "date": None, "source": "default"}


def get_fx_rate(conn: sqlite3.Connection, pair: str = "USD/CNY", date: Optional[str] = None) -> dict:
    """取指定 pair（及可选日期）的汇率；缺省取最新。"""
    try:
        if date:
            row = conn.execute(
                "SELECT * FROM fx_rate WHERE pair=? AND rate_date=? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (pair, date),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fx_rate WHERE pair=? "
                "ORDER BY rate_date DESC, fetched_at DESC LIMIT 1",
                (pair,),
            ).fetchone()
    except Exception:
        row = None
    if row:
        return {"rate": row["rate"], "date": row["rate_date"], "source": row["source"]}
    return {"rate": None, "date": None, "source": None}


def record_fx(conn: sqlite3.Connection, pair: str, rate: float, rate_date: str, source: Optional[str] = None) -> dict:
    """写入一条汇率（复合主键冲突则更新）。"""
    now = _now()
    conn.execute(
        "INSERT INTO fx_rate (pair, rate_date, rate, source, fetched_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(pair, rate_date) DO UPDATE SET "
        "rate=excluded.rate, source=excluded.source, fetched_at=excluded.fetched_at",
        (pair, rate_date, float(rate), source, now),
    )
    conn.commit()
    return get_fx_rate(conn, pair, rate_date)


def usd_cny_trend(days: int = 30, conn=None) -> dict:
    """USD/CNY 近 days 天趋势。

    供 screen_rules 筛选时做汇率因子判断（不传 conn 则内部取连接）。
    返回 {ok, span_days, direction('up'/'down'/'flat'/'unknown'), chg_pct, rate_first, rate_last}。
    chg_pct 为 (末值-首值)/首值*100 的百分比（带符号）。
    数据不足（<2 点）降级 ok=False，避免 screen_rules 用默认 7.25 误算汇率贡献。
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        start = (datetime.now().date() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT rate FROM fx_rate WHERE pair='USD/CNY' AND rate_date >= ? "
            "ORDER BY rate_date ASC",
            (start,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
    if len(rows) < 2:
        return {"ok": False, "span_days": days, "direction": "unknown",
                "chg_pct": None, "reason": "fx_rate 数据不足(<2 点)"}
    first = float(rows[0]["rate"])
    last = float(rows[-1]["rate"])
    if first <= 0:
        return {"ok": False, "span_days": days, "direction": "unknown", "chg_pct": None}
    chg = (last - first) / first * 100
    direction = "up" if chg > 0 else ("down" if chg < 0 else "flat")
    return {
        "ok": True, "span_days": days, "direction": direction,
        "chg_pct": round(chg, 2), "rate_first": first, "rate_last": last,
    }


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("== latest_usd_cny ==")
        print(latest_usd_cny(conn))
        print("\n== get_fx_rate(USD/CNY) ==")
        print(get_fx_rate(conn, "USD/CNY"))
    finally:
        conn.close()
