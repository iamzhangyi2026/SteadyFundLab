# -*- coding: utf-8 -*-
"""B 区独立数据库连接。

与 A 区 fund_sandbox.db **物理分离**：不同文件、不同连接、不跨库 JOIN。
这是隔离原则（存储分离）的落点。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "real_position.db"

SCHEMA_VERSION = 1


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """获取 B 区连接：row_factory=Row、开启外键、写锁等待 30s。

    与 A 区连接参数保持一致，避免两区行为不一致带来的排查成本。
    """
    path = db_path or DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db_path() -> Path:
    return DB_PATH


def init_db(db_path: Path | None = None) -> Path:
    """建 B 区表（幂等，可重复执行）。"""
    from backend.real.schema import TABLES

    path = db_path or DB_PATH
    conn = get_connection(path)
    try:
        for ddl in TABLES.values():
            conn.execute(ddl)
        # 增量补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列
        for col, typ in (("est_nav", "REAL"), ("est_change_pct", "REAL"), ("est_time", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE real_nav ADD COLUMN {col} {typ}")
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return path
