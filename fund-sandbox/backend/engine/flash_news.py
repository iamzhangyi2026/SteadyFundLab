# -*- coding: utf-8 -*-
"""
flash_news —— 闪电 / 快讯新闻。

两条职责：
- `fetch_flash_news(conn)`：抓**盘中快讯**（财联社电报 / 东财全球快讯 / 同花顺快讯）落 `market_news`。
- `get_flash_news(conn, limit)`：从 `market_news` 读最近新闻（按 pub_time 倒序）。

**为什么快讯单独一路（不走 RSS）**：这三家官网**无可用 RSS**
（财联社 / 证券时报 / 上证报的 RSS 路径实测 404），但 akshare 有现成快讯接口 —— 实测全部可用
。快讯桶与 RSS 桶并列，二者都落 `market_news`，
下游（事件提取 / 信息时差 / 主题热度）统一消费。

⚠️ `fetch_flash_news` 基于接口 `stock_info_global_*`，
数据源清单与 `market_news` 保持一致。

离线安全。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from typing import Any, List

SUMMARY_MAX_LEN = 300

# 快讯接口清单：(akshare 函数名, 源名, 标题列, 摘要列)
_FLASH_SOURCES = [
    ("stock_info_global_cls", "财联社电报", "标题", "内容"),
    ("stock_info_global_em", "东财全球快讯", "标题", "摘要"),
    ("stock_info_global_ths", "同花顺快讯", "标题", "内容"),
]


def _flash_pub_time(row: Any, fallback: str) -> str:
    """快讯发布时间：财联社是「发布日期 + 发布时间」两列，东财/同花顺是单列「发布时间」。"""
    d = row.get("发布日期")
    t = row.get("发布时间")
    if d is not None and t is not None:
        return f"{d} {t}"[:19]
    if t is not None:
        return str(t)[:19]
    return fallback


def _flash_link(row: Any, source: str, title: str) -> str:
    """快讯去重键（`market_news.link` 是 UNIQUE 且 NOT NULL）。

    东财 / 同花顺带原文链接，直接用；
    **财联社接口不返回链接** → 用 `flash://<源名>/<标题哈希>` 合成稳定键，
    保证同一条快讯重复抓取时被 UNIQUE 约束挡住，不会重复入库。
    """
    raw = str(row.get("链接", "") or "").strip()
    if raw:
        return raw
    h = hashlib.md5(title.encode("utf-8")).hexdigest()[:16]
    return f"flash://{source}/{h}"


def fetch_flash_news(conn: sqlite3.Connection) -> int:
    """抓三个快讯源并落 `market_news`，返回本次新写入条数。"""
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return 0

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for fn_name, source, t_col, s_col in _FLASH_SOURCES:
        try:
            df = getattr(ak, fn_name)()
        except Exception as exc:  # noqa: BLE001 单源失败不影响其余
            print(f"[快讯] {source} 抓取失败: {type(exc).__name__}: {exc}")
            continue
        if df is None or getattr(df, "empty", True):
            continue
        n = 0
        for _, row in df.iterrows():
            title = str(row.get(t_col, "") or "").strip()
            if not title:
                continue
            summary = str(row.get(s_col, "") or "").strip()[:SUMMARY_MAX_LEN]
            link = _flash_link(row, source, title)
            pub_time = _flash_pub_time(row, fetched_at)
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO market_news "
                    "(title, link, summary, source, source_url, pub_time, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (title, link, summary, source, "", pub_time, fetched_at),
                )
                n += cur.rowcount
            except Exception as exc:  # noqa: BLE001
                print(f"[快讯] 入库失败 {title[:26]}: {type(exc).__name__}: {exc}")
        conn.commit()
        inserted += n
        print(f"[快讯] {source} 抓取完成，本次新增 {n} 条")
    return inserted


def get_flash_news(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    """从 market_news 读取最近新闻。"""
    try:
        # pub_time 为 TEXT，按字典序倒序；缺失行排末尾
        rows = conn.execute(
            "SELECT * FROM market_news "
            "ORDER BY (CASE WHEN pub_time IS NULL OR pub_time='' THEN '0' ELSE pub_time END) DESC, id DESC "
            "LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


if __name__ == "__main__":
    from ..db.schema import get_connection

    conn = get_connection()
    try:
        print("fetch_flash_news:", fetch_flash_news(conn))
        items = get_flash_news(conn, 5)
        print("flash_news count:", len(items))
        if items:
            print("latest:", items[0].get("title"))
    finally:
        conn.close()
