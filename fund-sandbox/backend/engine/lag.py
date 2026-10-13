# -*- coding: utf-8 -*-
"""
fund-sandbox 滞后信号检测引擎
- 检测估值洼地信号
- 监控趋势反转
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from .screen_rules import calc_stats, percentile
from ..db.schema import get_connection


def detect_valley_signals(min_days: int = 5) -> list[dict]:
    """
    检测估值洼地信号
    :param min_days: 最近多少天内出现信号
    :return: 信号列表
    """
    conn = get_connection()
    signals = []
    
    try:
        # 获取所有有净值数据的基金
        cursor = conn.execute(
            """SELECT DISTINCT fund_code FROM nav_daily 
               WHERE nav_date >= date('now', '-30 days')"""
        )
        fund_codes = [row["fund_code"] for row in cursor.fetchall()]
        
        for code in fund_codes:
            # 获取近期净值
            cursor = conn.execute(
                """SELECT nav_date, acc_nav FROM nav_daily 
                   WHERE fund_code = ? AND nav_date >= date('now', ?)
                   ORDER BY nav_date DESC""",
                (code, f'-{min_days} days')
            )
            recent_navs = [dict(r) for r in cursor.fetchall()]
            
            if len(recent_navs) < 5:
                continue
            
            # 检查是否有信号
            latest = recent_navs[0]["acc_nav"]
            
            # 获取完整历史计算分位
            cursor = conn.execute(
                "SELECT acc_nav FROM nav_daily WHERE fund_code = ? ORDER BY nav_date",
                (code,)
            )
            full_series = [r["acc_nav"] for r in cursor.fetchall() if r["acc_nav"]]
            
            pct = percentile(full_series)
            
            if pct and pct < 30:
                # 低估值信号
                signals.append({
                    "fund_code": code,
                    "signal_type": "valley",
                    "percentile": round(pct, 1),
                    "latest_nav": latest,
                    "detected_at": datetime.now().isoformat(),
                })
        
        return signals
        
    finally:
        conn.close()


def detect_trend_reversal(fund_code: str, window: int = 20) -> dict:
    """
    检测趋势反转信号
    :param fund_code: 基金代码
    :param window: 窗口大小
    :return: 反转信号
    """
    conn = get_connection()
    
    try:
        cursor = conn.execute(
            """SELECT nav_date, acc_nav FROM nav_daily 
               WHERE fund_code = ? ORDER BY nav_date DESC LIMIT ?""",
            (fund_code, window)
        )
        navs = [dict(r) for r in cursor.fetchall()]
        
        if len(navs) < window:
            return {"fund_code": fund_code, "signal": None}
        
        # 检查近N日是否持续上涨/下跌
        values = [n["acc_nav"] for n in reversed(navs)]
        
        # 计算动量
        momentum = (values[-1] - values[0]) / values[0] * 100 if values[0] > 0 else 0
        
        signal = "reversal_up" if momentum > 5 else ("reversal_down" if momentum < -5 else "stable")
        
        return {
            "fund_code": fund_code,
            "signal": signal,
            "momentum": round(momentum, 2),
            "period": f"{values[0]} -> {values[-1]}",
        }
        
    finally:
        conn.close()


if __name__ == "__main__":
    signals = detect_valley_signals()
    print(f"洼地信号: {len(signals)} 只")
    for s in signals[:3]:
        print(f"  {s['fund_code']}: 分位{s['percentile']}%")
