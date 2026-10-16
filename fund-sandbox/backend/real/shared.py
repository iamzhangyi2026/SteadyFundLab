# -*- coding: utf-8 -*-
"""B 区对**公共数据层**的只读访问。

边界：
  **账本隔离，数据共用** —— 只有真实持仓（真钱）与沙盒交易（模拟）需要分开；
  行情 / 新闻资讯 / 基金规则 / 交易日历等**真实世界的资料**两区共用一份。

本模块只做三件事，全部**只读**：
  1. `read_news()`      —— 复用 A 区已抓的基金公告 / 重仓股新闻（`news_feed`）
  2. `read_fund_meta()` —— 复用基金档案（申购状态 / 费率 / 起购 / 分红方式）
  3. `redeem_rule()`    —— 复用真实赎回费阶梯（`engine.fees`，纯逻辑、不碰库）

**不做**：不写 A 区库、不 JOIN A 区账本表（lots / orders / budget）、不碰 B 区账本。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# 公共数据物理上暂寄 A 区库（A 区是唯一抓取方：有调度器 + 双源交叉验证）；
# 逻辑上属公共数据层，后续可迁出为独立库。B 区**只读**。
_SHARED_DB = Path(__file__).resolve().parents[2] / "data" / "fund_sandbox.db"

from ..engine.deep_sections import PS_ZH as _PS_ZH  # 公共映射：A/B 区同一份


def _ro_conn():
    """只读连接公共库；不可用返回 None。"""
    if not _SHARED_DB.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{_SHARED_DB.as_posix()}?mode=ro", uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        return c
    except Exception:
        return None


def read_news(code: str, limit: int = 12) -> list[dict]:
    """该基金的公告 / 重仓股新闻（只读 `news_feed`），按发布时间倒序。

    用于深度分析第三段「催化事件」—— 新闻是公共资料，A 区抓一次、B 区只读复用。
    """
    c = _ro_conn()
    if c is None:
        return []
    try:
        rows = c.execute(
            "SELECT kind, title, source, pub_time, url FROM news_feed "
            "WHERE fund_code=? ORDER BY pub_time DESC, id DESC LIMIT ?",
            (code, limit),
        ).fetchall()
    except Exception:
        return []
    finally:
        c.close()
    return [
        {"kind": r["kind"], "title": r["title"], "source": r["source"],
         "pub_time": r["pub_time"], "url": r["url"]}
        for r in rows
    ]


def read_fund_meta(code: str) -> dict:
    """基金档案（只读）：名称 / 类型 / 申购费率 / 起购 / 限购 / 分红方式 / 申购状态（中文）。"""
    c = _ro_conn()
    if c is None:
        return {}
    try:
        r = c.execute(
            "SELECT f.name, f.category, f.purchase_fee_rate, f.min_purchase, f.purchase_limit, "
            "f.dividend_method, "
            "(SELECT fs.purchase_status FROM fund_status fs WHERE fs.fund_code=f.code "
            " ORDER BY fs.status_date DESC, fs.id DESC LIMIT 1) AS purchase_status "
            "FROM funds f WHERE f.code=?",
            (code,),
        ).fetchone()
    except Exception:
        return {}
    finally:
        c.close()
    if not r:
        return {}
    out = {k: r[k] for k in r.keys()}
    # 库内存的是英文枚举（open/limited/closed），与 A 区 `/api/funds` 同口径转中文展示
    out["purchase_status"] = _PS_ZH.get((out.get("purchase_status") or "").strip(), out.get("purchase_status"))
    return out


def read_dividends(code: str, limit: int = 6) -> list[dict]:
    """该基金的**分红公告**（只读公共表 `news_feed`，`kind='fund_announce'`）。

    分红公告是真实世界的资料（东财 `fund_announcement_dividend_em`，见
    `engine/dividend.fetch_dividend_announcements`），A 区抓一次、两区共用。

    ⚠️ 口径：公告**只有标题与日期，不含每份分红金额**（金额要打开公告正文，或由累计净值差反推）。
    所以这里只回答「有没有分红、哪天公告的」，不报金额。
    """
    c = _ro_conn()
    if c is None:
        return []
    try:
        rows = c.execute(
            "SELECT title, pub_time, source, url FROM news_feed "
            "WHERE fund_code=? AND kind='fund_announce' "
            "AND (title LIKE '%分红%' OR title LIKE '%收益分配%' OR title LIKE '%利润分配%') "
            "ORDER BY pub_time DESC LIMIT ?",
            (code, limit),
        ).fetchall()
    except Exception:
        return []
    finally:
        c.close()
    return [
        {"title": r["title"], "pub_time": r["pub_time"], "source": r["source"], "url": r["url"]}
        for r in rows
    ]


def redeem_rule(amount: float, holding_days: int) -> dict:
    """真实赎回费阶梯（复用 A 区 `engine.fees` 的**纯逻辑** —— 它只读 config、不碰任何库）。

    返回 {fee, fee_rate, penalty, penalty_free_days}；引擎不可用时返回 {}。
    """
    try:
        from backend.engine import fees

        r = fees.calc_redeem_fee("", amount, holding_days)
        r["penalty_free_days"] = fees.penalty_free_days()
        return r
    except Exception:
        return {}


def redeem_tiers() -> list[dict]:
    """赎回费阶梯全表（真实规则，供界面展示"持有多久收多少"）。"""
    try:
        from backend.engine import fees

        return fees.get_redeem_tiers()
    except Exception:
        return []
