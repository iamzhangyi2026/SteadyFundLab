# -*- coding: utf-8 -*-
"""
fund-sandbox 引擎模块初始化
"""
# 暴露 backend.engine.db，便于 backend.engine.db.schema 直接访问（兼容调用约定）
from .. import db  # noqa: F401

from .screen_rules import (
    percentile,
    max_drawdown,
    ret_1y,
    ret_3m,
    ret_1m,
    ret_6m,
    annual_vol,
    calc_stats,
    pass_nav_gate,
    screen_candidates,
)
from .calendar import TradeCalendar
from .llm_client import LLMClient, simple_chat
from .ai_tools import call_tool, get_tools
from .analysis import analyze_fund  # batch_analyze 全项目无人调用，已移除
from .review import get_portfolio_summary, generate_review_report
from .screener import screen_all, get_pool_stats
from .lag import detect_valley_signals, detect_trend_reversal
from .news import fetch_market_news, refresh_news
from .health import check_nav_health, run_health_check
from .corpus import init_corpus, sync_from_operational, corpus_stats
from .scheduler import Scheduler, get_scheduler, start_scheduler


__all__ = [
    # 算法
    "percentile",
    "max_drawdown",
    "ret_1y",
    "ret_3m",
    "ret_1m",
    "ret_6m",
    "annual_vol",
    "calc_stats",
    "pass_nav_gate",
    "screen_candidates",
    # 日历
    "TradeCalendar",
    # LLM
    "LLMClient",
    "simple_chat",
    # AI工具
    "call_tool",
    "get_tools",
    # 分析
    "analyze_fund",
    "batch_analyze",
    # 回顾
    "get_portfolio_summary",
    "generate_review_report",
    # 筛选
    "screen_all",
    "get_pool_stats",
    # 滞后信号
    "detect_valley_signals",
    "detect_trend_reversal",
    # 新闻
    "fetch_market_news",
    "refresh_news",
    # 健康
    "check_nav_health",
    "run_health_check",
    # 归档
    "init_corpus",
    "sync_from_operational",
    "corpus_stats",
    # 调度
    "Scheduler",
    "get_scheduler",
    "start_scheduler",
]
