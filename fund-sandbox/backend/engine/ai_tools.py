# -*- coding: utf-8 -*-
"""
fund-sandbox AI工具集成（脱敏版）
- 基金分析工具
- 新闻摘要工具
- 筛选工具
- 纪律检查工具
"""
from __future__ import annotations

import json
from typing import Any
from .llm_client import simple_chat


# ============================================================
# 工具定义
# ============================================================
TOOLS = {
    "analyze_fund": {
        "name": "analyze_fund",
        "description": "分析单只基金的估值、趋势、风险",
        "parameters": {
            "type": "object",
            "properties": {
                "fund_code": {"type": "string", "description": "基金代码"},
                "fund_name": {"type": "string", "description": "基金名称"},
            },
            "required": ["fund_code"],
        },
    },
    "summarize_news": {
        "name": "summarize_news",
        "description": "对新闻进行摘要，提取关键信息",
        "parameters": {
            "type": "object",
            "properties": {
                "news_text": {"type": "string", "description": "新闻内容"},
                "focus": {"type": "string", "description": "关注点，如'行业影响'、'政策导向'"},
            },
            "required": ["news_text"],
        },
    },
    "screen_candidates": {
        "name": "screen_candidates",
        "description": "在基金池中筛选候选标的",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回数量"},
                "criteria": {"type": "string", "description": "筛选条件"},
            },
            "required": [],
        },
    },
    "check_discipline": {
        "name": "check_discipline",
        "description": "检查交易决策是否符合投资纪律",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "操作类型: buy/sell/hold"},
                "fund_code": {"type": "string", "description": "基金代码"},
                "amount": {"type": "number", "description": "金额"},
                "current_position_pct": {"type": "number", "description": "当前仓位百分比"},
            },
            "required": ["action"],
        },
    },
}


# ============================================================
# 工具实现
# ============================================================
async def call_tool(tool_name: str, params: dict) -> dict:
    """
    调用AI工具
    :param tool_name: 工具名称
    :param params: 工具参数
    :return: 工具执行结果
    """
    if tool_name == "analyze_fund":
        return await _analyze_fund(params)
    elif tool_name == "summarize_news":
        return await _summarize_news(params)
    elif tool_name == "screen_candidates":
        return await _screen_candidates(params)
    elif tool_name == "check_discipline":
        return await _check_discipline(params)
    else:
        return {"error": f"未知工具: {tool_name}"}


async def _analyze_fund(params: dict) -> dict:
    """分析单只基金"""
    fund_code = params.get("fund_code", "")
    fund_name = params.get("fund_name", "")
    
    # 这里调用LLM进行分析
    prompt = f"""请分析基金 {fund_code} {fund_name}：
1. 当前估值水平（历史分位）
2. 近期趋势（近3月、近1年收益率）
3. 主要风险点
4. 投资建议

请给出简洁的分析报告。"""
    
    content = await simple_chat(prompt)
    # 此处固定写 "tool": "analyze_fund"（tool_name 是上层传入变量，不应回写）
    # `execute_ai_tool(tool_name, params)` 的参数，**不是本函数的**，
    # 进这个函数就是 NameError（该工具从来没跑通过）。
    return {
        "tool": "analyze_fund",
        "fund_code": fund_code,
        "analysis": content,
    }


async def _summarize_news(params: dict) -> dict:
    """新闻摘要"""
    news_text = params.get("news_text", "")
    focus = params.get("focus", "")
    
    prompt = f"""请对以下财经新闻进行摘要：

{news_text}

关注点：{focus or '整体影响'}

请提取关键信息，控制在200字以内。"""
    
    content = await simple_chat(prompt)
    return {
        "tool": "summarize_news",
        "summary": content,
    }


async def _screen_candidates(params: dict) -> dict:
    """基金池筛选"""
    limit = params.get("limit", 5)
    criteria = params.get("criteria", "")
    
    prompt = f"""请在基金池中筛选候选标的：
筛选条件：{criteria or '估值洼地+趋势向上'}
返回数量：{limit}

请列出基金代码、名称、关键指标。"""
    
    content = await simple_chat(prompt)
    return {
        "tool": "screen_candidates",
        "candidates": content,
    }


async def _check_discipline(params: dict) -> dict:
    """检查交易纪律"""
    action = params.get("action", "")
    fund_code = params.get("fund_code", "")
    amount = params.get("amount", 0)
    current_position_pct = params.get("current_position_pct", 0)
    
    prompt = f"""请检查以下交易决策是否符合投资纪律：
操作：{action}
基金：{fund_code}
金额：{amount}元
当前仓位：{current_position_pct}%

检查要点：
1. 单只基金仓位是否超过25%
2. 高波动基金是否控制小仓
3. 是否存在追涨杀跌行为

请给出结论和建议。"""
    
    content = await simple_chat(prompt)
    return {
        "tool": "check_discipline",
        "result": content,
    }


def get_tools() -> list[dict]:
    """返回所有可用工具定义"""
    return TOOLS


if __name__ == "__main__":
    import asyncio
    
    async def test():
        result = await call_tool("analyze_fund", {"fund_code": "000001"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    asyncio.run(test())
