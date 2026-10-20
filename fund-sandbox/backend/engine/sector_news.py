# -*- coding: utf-8 -*-
"""
sector_news —— 行业/概念板块新闻与行情。
- get_sector_news(conn, board_type, limit): 读 sector_news
- refresh_sector_news(conn): 用 backend.data.fetcher.fetch_boards 抓取并写入 sector_news
离线安全：fetch_boards 离线返回 ([], 'none')，saved=0。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List

try:
    from ..data.fetcher import fetch_boards
except Exception:  # pragma: no cover
    fetch_boards = None


def get_sector_news(conn: sqlite3.Connection, board_type: str = "industry", limit: int = 40) -> List[dict]:
    """读取 sector_news（按 board_type、hot_rank 升序）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM sector_news WHERE board_type = ? "
            "ORDER BY CASE WHEN hot_rank IS NULL OR hot_rank=0 THEN 9999 ELSE hot_rank END ASC, "
            "change_pct DESC LIMIT ?",
            (board_type, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def refresh_sector_news(
    conn: sqlite3.Connection,
    board_type: str = "industry",
    concept_names: List[str] | None = None,
) -> Dict[str, Any]:
    """抓取板块行情并写入 sector_news。返回 {fetched, saved, source}。

    - `board_type="industry"`：抓行业板块
    - `board_type="concept"`：抓概念板块（须传 `concept_names`；调用方按**新闻热度**筛 Top N，
      见 `refresh_all_boards` —— 同花顺 375 个概念全量抓要 5 分钟）

    原函数签名无 `board_type`，却调用
    `fetch_boards(board_type="industry")` → **TypeError 被 except 吞掉**，
    板块刷新恒失败（数据停在 09-19）；概念侧完全无抓取代码。
    """
    fetched = 0
    saved = 0
    try:
        if fetch_boards is None:
            return {"fetched": 0, "saved": 0, "source": "unavailable"}
        boards, source = fetch_boards(board_type=board_type, concept_names=concept_names)
        fetched = len(boards)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for b in boards:
            try:
                conn.execute(
                    "INSERT INTO sector_news "
                    "(sector, sector_code, change_pct, net_inflow, leader_stock, leader_change_pct, "
                    "hot_rank, news_title, news_url, news_source, news_summary, fetched_at, board_type) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        # `fetch_boards` 返回的键是 board_name / board_code /
                        # main_net_inflow，原代码却取 sector / net_inflow → **全取空**，
                        # 结果 fetched=90 但几乎全部 INSERT 失败（saved 恒 1）。
                        str(b.get("board_name", "") or b.get("sector", "")),
                        str(b.get("board_code", "")),
                        float(b.get("change_pct", 0) or 0),
                        float(b.get("main_net_inflow", b.get("net_inflow", 0)) or 0),
                        "",
                        0.0,
                        0,
                        "",
                        "",
                        "",
                        "",
                        now,
                        board_type,
                    ),
                )
                saved += 1
            except Exception:
                continue
        conn.commit()
        return {"fetched": fetched, "saved": saved, "source": source}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"fetched": fetched, "saved": saved, "source": "error"}


def hot_concept_names(conn: sqlite3.Connection, limit: int = 10, days: int = 7) -> List[str]:
    """近 N 天新闻里出现最多的概念名 —— 用于概念板块抓取（**新闻导向**）。

    做法：用同花顺概念名词表（375 个）扫 `market_news.title`，统计命中频次取 Top N。
    **不依赖 `news_triage.impact_concepts`** —— 那是概念映射的产物，可能为空，
    用它会造成「鸡生蛋」；直接用标题匹配更稳。

    长名优先匹配（先试「固态电池」再试「电池」），每条标题最多取 3 个概念
    —— 与 `infer_concepts` 的概念映射设计一致。
    """
    try:
        from ..data.fetcher import fetch_concept_name_list

        names = fetch_concept_name_list()
    except Exception:  # noqa: BLE001
        return []
    if not names:
        return []
    names_sorted = sorted(names, key=len, reverse=True)
    try:
        rows = conn.execute(
            "SELECT title FROM market_news WHERE substr(pub_time, 1, 10) >= date('now', ?)",
            (f"-{int(days)} days",),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    cnt: Dict[str, int] = {}
    for r in rows:
        title = str(r[0] or "")
        hit = 0
        for n in names_sorted:
            if n in title:
                cnt[n] = cnt.get(n, 0) + 1
                hit += 1
                if hit >= 3:
                    break
    return [n for n, _ in sorted(cnt.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


def refresh_all_boards(conn: sqlite3.Connection) -> Dict[str, Any]:
    """抓**行业 + 概念**两类板块并落库（供调度器定时调用）。

    概念侧按「近 7 天新闻热度 Top 10」抓（新闻导向）。
    """
    ind = refresh_sector_news(conn, board_type="industry")
    names = hot_concept_names(conn)
    con = refresh_sector_news(conn, board_type="concept", concept_names=names)
    return {"industry": ind, "concept": con, "concept_names": names}


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("get_sector_news:", len(get_sector_news(conn, limit=5)))
        print("refresh_all_boards:", refresh_all_boards(conn))
    finally:
        conn.close()
