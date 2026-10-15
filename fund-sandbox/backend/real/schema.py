# -*- coding: utf-8 -*-
"""B 区（真实仓位）表结构。

独立于 A 区 schema.py：两区不共享任何表。
所有金额用 REAL（元），比例存小数（0.015 = 1.5%），与 A 区口径一致。
"""

TABLES: dict[str, str] = {}

TABLES["real_holdings"] = """
-- 真实持仓（当前）。人工确认后写入；对 AI 只读。
CREATE TABLE IF NOT EXISTS real_holdings (
    fund_code     TEXT PRIMARY KEY,   -- 基金代码
    fund_name     TEXT,               -- 基金名称
    shares        REAL,               -- 持有份额
    cost_amount   REAL,               -- 成本金额（元）
    market_amount REAL,               -- 市值（元）
    profit        REAL,               -- 浮动盈亏（元）
    profit_rate   REAL,               -- 盈亏率（小数）
    snapshot_date TEXT,               -- 数据所属日期
    source        TEXT,               -- ocr | manual
    confirmed     INTEGER DEFAULT 1,  -- 是否已人工确认（入库前提）
    updated_at    TEXT
);
"""

TABLES["real_imports"] = """
-- 导入批次：每次上传截图 / 粘贴文本生成一条。确认前不落 holdings。
CREATE TABLE IF NOT EXISTS real_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,                    -- ocr | manual
    raw_text     TEXT,                    -- OCR / 粘贴原文，供人工核对
    status       TEXT DEFAULT 'pending',  -- pending | confirmed | discarded
    created_at   TEXT,
    confirmed_at TEXT
);
"""

TABLES["real_import_rows"] = """
-- 导入候选行：人工逐条核对后确认。
CREATE TABLE IF NOT EXISTS real_import_rows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id     INTEGER NOT NULL REFERENCES real_imports(id) ON DELETE CASCADE,
    fund_code     TEXT,
    fund_name     TEXT,
    shares        REAL,
    cost_amount   REAL,
    market_amount REAL,
    profit        REAL,
    profit_rate   REAL,
    needs_review  INTEGER DEFAULT 0,  -- 1=OCR 识别不确定，需人工复核
    confirmed     INTEGER DEFAULT 0   -- 1=人工确认采用该行
);
"""

TABLES["real_snapshots"] = """
-- 每日总览快照（人工确认后落一条）。
CREATE TABLE IF NOT EXISTS real_snapshots (
    snap_date      TEXT PRIMARY KEY,
    total_cost     REAL,
    total_market   REAL,
    total_profit   REAL,
    holdings_count INTEGER,
    created_at     TEXT
);
"""

TABLES["real_nav"] = """
-- 真实持仓基金的最新行情缓存（B 区独立抓取，不读 A 区库）。
CREATE TABLE IF NOT EXISTS real_nav (
    fund_code      TEXT PRIMARY KEY,
    nav_date       TEXT,   -- 净值日期
    nav            REAL,   -- 单位净值
    change_pct     REAL,   -- 当日涨跌幅（%）
    est_nav        REAL,   -- 盘中估算净值（新浪）
    est_change_pct REAL,   -- 盘中估算涨幅（%）
    est_time       TEXT,   -- 估值时间
    updated_at     TEXT
);
"""
