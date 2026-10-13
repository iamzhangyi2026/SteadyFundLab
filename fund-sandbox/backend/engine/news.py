# -*- coding: utf-8 -*-
"""
fund-sandbox 新闻抓取引擎
- 使用akshare获取财经新闻
- RSS源抓取
- 新闻分类和摘要
"""
from __future__ import annotations

import sqlite3
import json
import hashlib
from datetime import datetime
from typing import Any
from ..db.schema import get_connection


def fetch_market_news() -> list[dict]:
    """
    抓取市场新闻（使用akshare）
    :return: 新闻列表
    """
    news_list = []
    
    try:
        import akshare as ak
        
        # 获取财经新闻
        df = ak.stock_news_em(symbol="")
        
        if df is not None and len(df) > 0:
            for _, row in df.head(50).iterrows():
                news_list.append({
                    "title": str(row.get("新闻内容", "")),
                    "source": "东方财富",
                    "content": str(row.get("新闻内容", "")),
                    "published_at": str(row.get("发布时间", "")),
                    "url": str(row.get("新闻链接", "")),
                })
    
    except ImportError:
        # akshare未安装时使用模拟数据
        news_list = _get_mock_news()
    
    except Exception as e:
        print(f"新闻抓取失败: {e}")
        news_list = _get_mock_news()
    
    return news_list


def _get_mock_news() -> list[dict]:
    """模拟新闻数据"""
    return [
        {
            "title": "央行宣布降准0.25个百分点",
            "source": "模拟源",
            "content": "为支持实体经济发展，中国人民银行决定下调金融机构存款准备金率0.25个百分点。",
            "published_at": datetime.now().isoformat(),
            "url": "",
        },
        {
            "title": "A股市场震荡上行，科技股领涨",
            "source": "模拟源",
            "content": "今日A股市场震荡上行，创业板指涨1.2%，科技股板块表现活跃。",
            "published_at": datetime.now().isoformat(),
            "url": "",
        },
        {
            "title": "新能源汽车销量再创新高",
            "source": "模拟源",
            "content": "最新数据显示，本月新能源汽车销量同比增长40%，创历史新高。",
            "published_at": datetime.now().isoformat(),
            "url": "",
        },
    ]


def save_news(conn: sqlite3.Connection, news_list: list[dict]) -> int:
    """
    保存新闻到数据库
    :param conn: 数据库连接
    :param news_list: 新闻列表
    :return: 保存数量
    """
    saved = 0
    
    for news in news_list:
        # 生成去重键
        content_hash = hashlib.md5(news["title"].encode()).hexdigest()[:16]
        dedup_key = f"{news['source']}:{content_hash}"
        
        # 分类（简单规则）
        category = _classify_news(news["title"] + news["content"])
        
        try:
            conn.execute(
                """INSERT OR IGNORE INTO news_triage 
                   (dedup_key, title, source, category, summary, credibility, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedup_key,
                    news["title"],
                    news["source"],
                    category,
                    news.get("content", "")[:500],
                    "medium",
                    news.get("published_at", datetime.now().isoformat()),
                )
            )
            saved += 1
        except Exception as e:
            print(f"保存新闻失败: {e}")
    
    conn.commit()
    return saved


def _classify_news(text: str) -> str:
    """简单新闻分类。

    与 `triage._CAT_KW` 同步扩充「公司行动」类词，避免同一批新闻
    在写入路径（本函数）与修补路径（`triage._classify`）得到不同类别。
    ⚠️ 已知缺陷：兜底为 `market`，与 `triage._classify` 的 `other` 不一致。
    """
    text_lower = text.lower()

    if any(kw in text_lower for kw in ["政策", "央行", "降准", "加息", "监管"]):
        return "policy"
    elif any(kw in text_lower for kw in ["国际", "美国", "中东", "地缘"]):
        return "geo"
    elif any(kw in text_lower for kw in ["会议", "论坛", "峰会"]):
        return "conference"
    elif any(kw in text_lower for kw in [
        "公司", "财报", "业绩",
        "推出", "上线", "上架", "交付", "起诉", "诉讼", "首席执行官", "ceo",
        "收购", "债务", "订阅", "裁员", "罢工", "召回",
    ]):
        return "company"
    elif any(kw in text_lower for kw in ["基金", "定投", "净值"]):
        return "fund"
    elif any(kw in text_lower for kw in ["板块", "行业", "赛道"]):
        return "sector"
    elif any(kw in text_lower for kw in ["军事", "军演", "装备"]):
        return "military"
    else:
        return "market"


def refresh_news() -> dict[str, Any]:
    """
    刷新新闻数据
    :return: 执行结果
    """
    conn = get_connection()
    
    try:
        news_list = fetch_market_news()
        saved = save_news(conn, news_list)
        
        return {
            "fetched": len(news_list),
            "saved": saved,
            "total": conn.execute("SELECT COUNT(*) FROM news_triage").fetchone()[0],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    result = refresh_news()
    print(f"新闻刷新: {result}")
