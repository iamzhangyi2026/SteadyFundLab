# -*- coding: utf-8 -*-
"""
news_quality —— 新闻质量评分。
- score_quality(item): 对单条新闻（dict，含 title/summary/source/quality 等）打 0-100 分
- get_low_quality(conn): 从 market_news 取低分新闻
离线安全。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

# 已知可信来源（权重加分）
_GOOD_SOURCES = {
    "新华社", "人民日报", "央视", "央视新闻", "证券时报", "中国证券报", "经济日报",
    "财新", "36氪", "澎湃", "第一财经", "华尔街见闻", "东方财富", "中国新闻网",
    "MarketWatch", "CNBC", "Reuters", "Bloomberg", "WSJ", "FT",
}
# 低质/聚合源（扣分）
_WEAK_SOURCES = {"未知", "匿名", "网友", "转载", "营销号"}

# 低分阈值（与 market_news.quality 列语义一致：分值低=质量差）
LOW_QUALITY_THRESHOLD = 60


def score_quality(item: Dict[str, Any]) -> Dict[str, Any]:
    """对单条新闻打分。返回 {quality, quality_reason}。"""
    try:
        title = str(item.get("title") or item.get("title_zh") or "")
        summary = str(item.get("summary") or item.get("summary_zh") or item.get("content") or "")
        source = str(item.get("source") or "")
        reasons: List[str] = []
        score = 50  # 基线

        # 标题长度
        if len(title) >= 8:
            score += 10
            reasons.append("标题完整")
        elif len(title) == 0:
            score -= 20
            reasons.append("标题缺失")
        else:
            score -= 5
            reasons.append("标题偏短")

        # 摘要/正文长度
        if len(summary) >= 40:
            score += 15
            reasons.append("正文充分")
        elif len(summary) == 0:
            score -= 15
            reasons.append("正文缺失")
        else:
            score -= 5
            reasons.append("正文偏短")

        # 有具体数字/日期 -> 更像事实
        if any(ch.isdigit() for ch in (title + summary)):
            score += 10
            reasons.append("含事实数据")

        # 来源
        if source in _GOOD_SOURCES:
            score += 15
            reasons.append("可信来源")
        elif source in _WEAK_SOURCES:
            score -= 20
            reasons.append("来源存疑")
        elif source == "":
            score -= 10
            reasons.append("无来源")

        # 含链接（url）加分
        if item.get("url") or item.get("link"):
            score += 5
            reasons.append("有原文链接")

        # 官方标记
        if item.get("is_official"):
            score += 5
            reasons.append("官方发布")

        score = max(0, min(100, int(score)))
        return {"quality": score, "quality_reason": "；".join(reasons) if reasons else "默认"}
    except Exception:
        return {"quality": 0, "quality_reason": "评分异常"}


def get_low_quality(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    """从 market_news 取低质量新闻（quality 缺失或低于阈值）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM market_news "
            "WHERE quality IS NULL OR quality = '' OR CAST(quality AS INTEGER) < ? "
            "ORDER BY id DESC LIMIT ?",
            (LOW_QUALITY_THRESHOLD, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("score sample:", score_quality({"title": "央行降准0.25个百分点", "summary": "为支持实体经济，央行决定下调存准率0.25个百分点。", "source": "新华社"}))
        print("low quality count:", len(get_low_quality(conn, 5)))
    finally:
        conn.close()
