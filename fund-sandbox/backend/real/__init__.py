# -*- coding: utf-8 -*-
"""B 区 · 真实仓位（人接管 · 隔离区）。

与 A 区（虚拟沙盒）完全隔离：
- 独立数据库 data/real_position.db（不共用连接、不跨库 JOIN）
- 独立 API 命名空间 /api/real/*
- A 区的 scheduler / orders / ledger / rules 不得 import 本包

权限：人读写；AI 只读。
边界定义：账本隔离、数据共用。
"""

from backend.real.db import DB_PATH, get_connection, get_db_path, init_db

__all__ = ["DB_PATH", "get_connection", "get_db_path", "init_db"]
