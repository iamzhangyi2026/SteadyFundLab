# -*- coding: utf-8 -*-
"""
fund-sandbox 基金池筛选引擎
- 全量基金筛选
- 候选标的排序
"""
from __future__ import annotations

import sqlite3
from typing import Any
from .screen_rules import screen_candidates, calc_stats
from ..config import test_funds, SCREEN_RULES
from ..db.schema import get_connection


def screen_all() -> list[dict]:
    """
    全量基金池筛选
    :return: 候选基金列表
    """
    conn = get_connection()
    
    try:
        # 获取所有基金净值统计
        cursor = conn.execute(
            """SELECT f.code as fund_code, 
                      f.name, f.category,
                      COUNT(n.nav_date) as nav_count,
                      MAX(n.nav_date) as latest_date
               FROM funds f
               LEFT JOIN nav_daily n ON f.code = n.fund_code
               GROUP BY f.code"""
        )
        fund_stats = []
        
        for row in cursor.fetchall():
            fund_code = row["fund_code"]
            
            # 获取完整净值序列
            cursor2 = conn.execute(
                "SELECT nav_date, nav, acc_nav FROM nav_daily WHERE fund_code = ? ORDER BY nav_date",
                (fund_code,)
            )
            nav_series = [dict(r) for r in cursor2.fetchall()]
            
            if len(nav_series) < 60:
                continue
                
            stats = calc_stats(nav_series)
            stats["fund_code"] = fund_code
            stats["name"] = row["name"]
            stats["category"] = row["category"]
            fund_stats.append(stats)
        
        # 执行筛选
        candidates = screen_candidates(fund_stats, SCREEN_RULES)
        
        return candidates
        
    finally:
        conn.close()


def get_pool_stats() -> dict[str, Any]:
    """获取基金池统计"""
    conn = get_connection()
    
    try:
        cursor = conn.execute("SELECT COUNT(*) as count FROM funds")
        total = cursor.fetchone()["count"]
        
        cursor = conn.execute("SELECT COUNT(DISTINCT fund_code) as count FROM nav_daily")
        with_nav = cursor.fetchone()["count"]
        
        candidates = screen_all()
        
        return {
            "total_funds": total,
            "with_nav_data": with_nav,
            "candidates": len(candidates),
            "candidate_codes": [c["fund_code"] for c in candidates[:5]],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    stats = get_pool_stats()
    print(f"基金池统计: {stats}")
    
    candidates = screen_all()
    print(f"候选标的: {len(candidates)} 只")
    for c in candidates[:3]:
        print(f"  {c['fund_code']}: 分位{c['percentile']:.1f}%, 近1年{c['ret_1y']:.2f}%")


# ===== 新闻导向候选挖掘 =====
# 链路：事件热度 Top 主题 → 板块资金验证 → 候选基金（附可买性 + 筛选事实）
# ⚠️ 只给事实与依据，**不推荐买哪只**（决策权在接管 agent）。

FUNDS_PER_THEME = 12


def _match_funds(conn, theme: str, limit: int = FUNDS_PER_THEME) -> list[dict]:
    """按主题关键词匹配候选基金（基金名称命中主题词）。"""
    kw = (theme or "").strip()
    if not kw:
        return []
    rows = conn.execute(
        "SELECT code, name, category FROM funds WHERE name LIKE ? ORDER BY code LIMIT ?",
        (f"%{kw}%", limit),
    ).fetchall()
    out = []
    for r in rows:
        code = str(r["code"] or "").strip()
        out.append({
            "code": code,
            "name": (r["name"] or "").strip(),
            "type": str(r["category"] or "").strip(),
            "availability": _availability(conn, code),
            "screening": _screen(conn, code),
        })
    return out


def _availability(conn, code: str) -> dict:
    """可买性事实：申购状态 / 日限额 / 起购 / 申购费 / 赎回费阶梯。

    数据来自 `fund_pool.lookup_purchase`（全市场购买配置表）+ `fund_fee`（赎回费）。
    取不到就留空 —— **不编造**。
    """
    try:
        from backend.engine import fund_pool

        pi = fund_pool.lookup_purchase(code)
    except Exception:  # noqa: BLE001
        pi = None
    if not pi:
        return {}
    try:
        from backend.engine import rule_watch

        tiers = rule_watch.redeem_tiers_of(conn, code)
    except Exception:  # noqa: BLE001
        tiers = None
    return {
        "purchase_status": pi.get("purchase_status") or "",
        "purchase_limit": pi.get("purchase_limit"),
        "min_purchase": pi.get("min_purchase"),
        "fee_rate": pi.get("fee_rate"),
        "next_open_date": pi.get("next_open_date"),
        "redeem_tiers": tiers,
    }


def _screen(conn, code: str) -> dict | None:
    """项目筛选规则结果（分位 / 近1年 / 最大回撤），只读本地净值。"""
    try:
        from backend.engine import screen_rules

        return screen_rules.evaluate_fund(conn, code)
    except Exception:  # noqa: BLE001
        return None


def build_candidates(conn, days: int = 3, top_n: int = 5) -> dict:
    """新闻导向候选挖掘。

    步骤：① 近 days 天事件最多的 Top 主题（news_triage.impact_boards）
          ② 该主题在 sector_news 里的资金流方向（作为"验证"事实）
          ③ 按主题名匹配候选基金（附可买性 + 筛选事实）

    ⚠️ 只给依据与事实，**不代替决策**。
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    since = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute(
        """
        SELECT impact_boards, COUNT(*) AS events
        FROM news_triage
        WHERE TRIM(COALESCE(impact_boards, '')) <> ''
          AND COALESCE(first_pub_time, processed_at) >= ?
        GROUP BY impact_boards ORDER BY events DESC LIMIT ?
        """,
        (since, top_n),
    ).fetchall()

    themes: list[dict] = []
    for r in rows:
        board = (r["impact_boards"] or "").split(",")[0].strip()
        flow = conn.execute(
            "SELECT sector, SUM(net_inflow) f FROM sector_news WHERE sector = ? GROUP BY sector",
            (board,),
        ).fetchone()
        themes.append({
            "theme": r["impact_boards"],
            "events": r["events"],
            "funds": _match_funds(conn, board),
            "flow_state": ("inflow" if (flow and flow["f"] and flow["f"] > 0)
                           else "outflow" if (flow and flow["f"] and flow["f"] < 0)
                           else "unknown"),
        })
    return {"as_of": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
            "days": days, "themes": themes}
