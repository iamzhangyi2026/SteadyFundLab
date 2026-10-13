"""数据库层：schema 定义与连接管理。

对外暴露：
    init_db()        建库建表（幂等，可重复执行）
    get_connection() 获取连接（已开启外键约束、row_factory=Row）

所有表结构的定义都在 schema.py 里，改表请改那里，不要在别处写 CREATE TABLE。
"""

from backend.db.schema import DB_PATH, SCHEMA_VERSION, get_connection, init_db

__all__ = ["init_db", "get_connection", "DB_PATH", "SCHEMA_VERSION"]
