# -*- coding: utf-8 -*-
"""
fund-sandbox 归档库引擎
- 定期归档运营库数据
- 保持运营库轻量
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from ..db.schema import get_connection


def _get_corpus_path() -> Path:
    """获取归档库路径"""
    import os
    if os.path.exists("/data/data/com.termux"):
        data_dir = Path("/data/data/com.termux/files/home/storage/shared/fund-sandbox/data")
    else:
        data_dir = Path(__file__).resolve().parents[3] / "data"
    
    return data_dir / "corpus.db"


def init_corpus() -> str:
    """初始化归档库"""
    corpus_path = _get_corpus_path()
    corpus_conn = sqlite3.connect(str(corpus_path))
    corpus_conn.row_factory = sqlite3.Row
    
    # 创建表结构（与运营库相同）
    corpus_conn.execute("""
    CREATE TABLE IF NOT EXISTS news_triage (
        id INTEGER PRIMARY KEY,
        dedup_key TEXT,
        title TEXT,
        source TEXT,
        category TEXT,
        summary TEXT,
        impact_boards TEXT,
        impact_concepts TEXT,
        credibility TEXT,
        is_official INTEGER,
        published_at TEXT,
        archived_at TEXT
    )
    """)
    
    corpus_conn.execute("""
    CREATE TABLE IF NOT EXISTS sector_news (
        id INTEGER PRIMARY KEY,
        board_code TEXT,
        board_name TEXT,
        board_type TEXT,
        news_title TEXT,
        news_summary TEXT,
        source TEXT,
        published_at TEXT,
        archived_at TEXT
    )
    """)
    
    corpus_conn.commit()
    corpus_conn.close()
    
    return str(corpus_path)


def sync_from_operational() -> dict[str, int]:
    """
    从运营库同步数据到归档库
    :return: 同步统计
    """
    op_conn = get_connection()
    corpus_path = _get_corpus_path()
    corpus_conn = sqlite3.connect(str(corpus_path))
    
    added = {}
    
    try:
        # 同步新闻
        cursor = op_conn.execute(
            "SELECT * FROM news_triage WHERE id NOT IN (SELECT id FROM news_triage)"
        )
        rows = cursor.fetchall()
        
        for row in rows:
            corpus_conn.execute(
                """INSERT OR REPLACE INTO news_triage 
                   (id, dedup_key, title, source, category, summary, 
                    impact_boards, impact_concepts, credibility, is_official, 
                    published_at, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*row, row[0])  # 追加archived_at
            )
        
        added["news"] = len(rows)
        
        # 同步板块新闻
        cursor = op_conn.execute("SELECT * FROM sector_news")
        rows = cursor.fetchall()
        
        for row in rows:
            corpus_conn.execute(
                """INSERT OR REPLACE INTO sector_news
                   (id, board_code, board_name, board_type, news_title, 
                    news_summary, source, published_at, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*row, row[0])
            )
        
        added["sector_news"] = len(rows)
        
        corpus_conn.commit()
        
    finally:
        op_conn.close()
        corpus_conn.close()
    
    return added


def corpus_stats() -> dict[str, int]:
    """归档库统计"""
    corpus_path = _get_corpus_path()
    
    if not corpus_path.exists():
        return {"_total": 0}
    
    conn = sqlite3.connect(str(corpus_path))
    
    try:
        news_count = conn.execute("SELECT COUNT(*) FROM news_triage").fetchone()[0]
        sector_count = conn.execute("SELECT COUNT(*) FROM sector_news").fetchone()[0]
        
        return {
            "_total": news_count + sector_count,
            "news": news_count,
            "sector_news": sector_count,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    path = init_corpus()
    print(f"归档库: {path}")
    
    stats = corpus_stats()
    print(f"归档统计: {stats}")
