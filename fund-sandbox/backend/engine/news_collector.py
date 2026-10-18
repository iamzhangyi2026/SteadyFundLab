# -*- coding: utf-8 -*-
"""news_collector —— 恢复 news_feed 的抓取（基金公告 + 重仓股新闻）。

背景
----
`news_feed`（资讯页「基金公告 / 重仓股」的数据源）缺少写入方时，数据会停留在历史日期。
本模块负责恢复抓取，**旧数据一律保留**（后续还要用于总结分析）。

口径：抓取范围按持仓而非关注列表
------------------------------------
  - 抓取范围 = **真实持仓基金**（lots 有剩余份额的）+ 其前十大重仓股，
    与「交易池/关注列表」无关 —— /api/news 本来也只按持仓过滤。
  - 只做增量：表上有 `UNIQUE(fund_code, kind, title, pub_time)`，
    全部走 `INSERT OR IGNORE`，**绝不删除、不覆盖既有行**。

数据源（均来自东方财富，与库内 source 口径一致）
----------------------------------------------
  - `fund_announcement_personnel_em` + `fund_announcement_dividend_em`
    → kind='fund_announce'，source='东财基金公告'（这两个接口不返回原文链接，
    所以 url 为空 —— 与库里 61 条公告 url 全空的历史事实一致）。
  - `stock_news_em(symbol=股票)` → kind='stock_news'，带文章来源与原文链接。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

# 每只重仓股最多取几条个股新闻（akshare 默认返回 10 条）
STOCK_NEWS_LIMIT = 10
# 新闻正文入库截断长度（长文塞库没意义，标题+摘要已够阅读）
CONTENT_MAX = 400
# 基金公告（人事/分红）东财接口不返原文链接，给一个可点开的公告列表页，
# 否则资讯页「基金公告」永远没有「阅读全文」按钮（该 URL 返回 HTTP 200
# 且页面含「公告」）。
FUND_ANNOUNCE_URL = "https://fundf10.eastmoney.com/jjgg_{code}.html"


def held_funds(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    """当前需要抓公告/新闻的基金 = **A 区真实持仓** ∪ **B 区真实持仓**。

    新闻/公告是真实世界的公共资料，两个区共用一份（边界=**账本隔离、数据共用**）。
    A 区抓一次落公共表，B 区只读复用 —— 所以抓取范围必须覆盖 B 区持仓，
    否则 B 区弹窗的「催化事件」永远没有素材。

    B 区侧只读 `fund_code` / `fund_name`（不碰其账本、不写 B 区库）。
    """
    rows = conn.execute(
        "SELECT DISTINCT l.fund_code AS code, COALESCE(f.name, l.fund_code) AS name "
        "FROM lots l LEFT JOIN funds f ON f.code = l.fund_code "
        "WHERE l.status='active' AND l.remaining_shares > 0 "
        "ORDER BY l.fund_code"
    ).fetchall()
    out: Dict[str, str] = {r["code"]: r["name"] for r in rows}
    # 并入 B 区（真实仓位）持仓
    try:
        from backend.real.db import get_connection as _real_conn

        c = _real_conn()
        try:
            for r in c.execute("SELECT fund_code, fund_name FROM real_holdings").fetchall():
                out.setdefault(r["fund_code"], r["fund_name"] or r["fund_code"])
        finally:
            c.close()
    except Exception:
        pass
    return [{"code": k, "name": v} for k, v in sorted(out.items())]


def _is_stock_code(code: str) -> bool:
    """是不是 A 股代码（6 位数字）。债券/基金代码长度不同，剔掉免得白调个股新闻接口。"""
    c = str(code or "").strip()
    return len(c) == 6 and c.isdigit()


def holding_stocks(conn: sqlite3.Connection, fund_code: str) -> List[Dict[str, Any]]:
    """取该基金前十大重仓股。

    先读库里 fund_holdings（有就直接用，不重复联网）；没有再实时抓
    （akshare `fund_portfolio_hold_em`，报告期不确定，从最近几个季末倒序试
    到有数据为止）。抓不到就返回空 —— 不编造。
    债券型基金的"持仓"是债券（代码 9 位）、联接基金的持仓是 ETF，
    都不是个股，按 6 位数字过滤掉，避免拿债券/基金代码去调个股新闻接口。
    """
    try:
        rows = conn.execute(
            "SELECT stock_code, stock_name, weight FROM fund_holdings "
            "WHERE fund_code=? ORDER BY weight DESC LIMIT 10", (fund_code,)
        ).fetchall()
    except Exception:
        rows = []
    out = [
        {"code": r["stock_code"], "name": r["stock_name"] or r["stock_code"]}
        for r in rows if r["stock_code"] and _is_stock_code(r["stock_code"])
    ]
    if out:
        return out

    from datetime import date
    today = date.today()
    ends = []
    for y in (today.year, today.year - 1):
        ends.append(f"{y}0630")
        ends.append(f"{y}0331")
    from ..data import fetcher
    for rd in ends:
        try:
            hs = fetcher.fetch_holdings(fund_code, rd)
        except Exception:
            hs = []
        stocks = [h for h in (hs or []) if h.get("stock_code") and _is_stock_code(h["stock_code"])]
        if stocks:
            return [{"code": s["stock_code"],
                     "name": s.get("stock_name") or s["stock_code"]} for s in stocks[:10]]
    return []


def _insert(conn: sqlite3.Connection, row: Dict[str, Any]) -> int:
    """增量写入一条。命中 UNIQUE 即忽略（返回 0），绝不改既有行。"""
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_feed "
            "(fund_code, fund_name, kind, title, content, source, pub_time, url, "
            " related_stock, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("fund_code") or "", row.get("fund_name") or "", row["kind"],
                (row["title"] or "").strip(), (row.get("content") or "")[:CONTENT_MAX],
                row.get("source") or "", row.get("pub_time") or "",
                row.get("url") or "", row.get("related_stock") or "",
                row["fetched_at"],
            ),
        )
        return int(cur.rowcount or 0)
    except Exception:
        return 0


def refresh_news_feed(conn: sqlite3.Connection | None = None) -> Dict[str, Any]:
    """抓一轮：持仓基金的公告 + 重仓股新闻。只增量，不删旧。

    返回 {ok, funds, announce, stock_news, total_new, skipped, note}。
    单只基金 / 单个接口失败都不影响整批（各自 try 住，记进 skipped）。
    """
    from datetime import datetime

    own = False
    if conn is None:
        from ..db.schema import get_connection
        conn = get_connection()
        own = True
    try:
        ak = None  # 懒加载：抓不到就不抓，别让 import 失败拖垮整个刷新

        def _ak():
            nonlocal ak
            if ak is None:
                import akshare as _a  # noqa: PLC0415
                ak = _a
            return ak

        held = held_funds(conn)
        now = datetime.now().isoformat(timespec="seconds")
        stats: Dict[str, Any] = {
            "funds": len(held), "announce": 0, "stock_news": 0, "total_new": 0,
            "skipped": [], "ok": True,
        }
        if not held:
            stats["note"] = "当前无持仓基金，无需抓取"
            return stats

        for f in held:
            code, name = f["code"], f["name"]

            # 1) 基金公告（人事变更 + 分红）
            for fn_name in ("fund_announcement_personnel_em",
                            "fund_announcement_dividend_em"):
                try:
                    df = getattr(_ak(), fn_name)(symbol=code)
                except Exception as e:  # noqa: BLE001 单接口失败不拖垮整批
                    stats["skipped"].append(f"{code}/{fn_name}:{type(e).__name__}")
                    continue
                if df is None or getattr(df, "empty", True):
                    continue
                for _, r in df.iterrows():
                    title = str(r.get("公告标题") or "").strip()
                    if not title:
                        continue
                    # 东财公告接口不返原文链接；用基金公告列表页兜底，
                    # 让「阅读全文」可点开（见模块顶部 FUND_ANNOUNCE_URL 说明）。
                    announce_url = FUND_ANNOUNCE_URL.format(code=code)
                    stats["announce"] += _insert(conn, {
                        "fund_code": code, "fund_name": name, "kind": "fund_announce",
                        "title": title, "content": "", "source": "东财基金公告",
                        "pub_time": str(r.get("公告日期") or ""), "url": announce_url,
                        "related_stock": "", "fetched_at": now,
                    })

            # 2) 重仓股新闻
            for s in holding_stocks(conn, code):
                try:
                    df = _ak().stock_news_em(symbol=s["code"])
                except Exception as e:  # noqa: BLE001
                    stats["skipped"].append(f"{s['code']}/stock_news:{type(e).__name__}")
                    continue
                if df is None or getattr(df, "empty", True):
                    continue
                for _, r in df.head(STOCK_NEWS_LIMIT).iterrows():
                    title = str(r.get("新闻标题") or "").strip()
                    if not title:
                        continue
                    stats["stock_news"] += _insert(conn, {
                        "fund_code": code, "fund_name": name, "kind": "stock_news",
                        "title": title, "content": str(r.get("新闻内容") or ""),
                        "source": str(r.get("文章来源") or ""),
                        "pub_time": str(r.get("发布时间") or ""),
                        "url": str(r.get("新闻链接") or ""),
                        "related_stock": s["code"], "fetched_at": now,
                    })

        conn.commit()
        stats["total_new"] = stats["announce"] + stats["stock_news"]
        if not stats["total_new"]:
            stats["note"] = "本轮无新增（重复内容按 UNIQUE 忽略）"
        return stats
    except Exception as e:  # noqa: BLE001 顶层兜底：绝不抛给调用方
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "funds": 0, "announce": 0, "stock_news": 0,
                "total_new": 0, "skipped": [], "error": str(e)}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    from ..db.schema import get_connection
    _conn = get_connection()
    try:
        print(refresh_news_feed(_conn))
    finally:
        _conn.close()
