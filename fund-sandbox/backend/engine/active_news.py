# -*- coding: utf-8 -*-
"""
active_news —— 待推送/活跃新闻（来自 news_feed）。
注意：news_feed 表中没有 pushed 列，因此按“全部、按 id 倒序”返回（含未推送与已推送）。
离线安全。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List


def get_active_news(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    """从 news_feed 读取最新新闻（无 pushed 列，直接全量倒序）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM news_feed ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        items = get_active_news(conn, 5)
        print("active_news count:", len(items))
        if items:
            print("sample:", items[0].get("title"))
    finally:
        conn.close()
