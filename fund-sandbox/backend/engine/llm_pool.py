# -*- coding: utf-8 -*-
"""
llm_pool —— 轻量 LLM 调用池（离线安全）。
- call_llm(prompt, context=""): 优先用 backend.engine.llm_client.simple_chat；不可用时返回桩字符串
- pool_status(): 返回池状态
绝不抛异常。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


def call_llm(prompt: str, context: str = "") -> str:
    """调用 LLM。成功返回文本；不可用/异常返回桩字符串。"""
    try:
        from .llm_client import simple_chat
        # simple_chat 是 async；在同步上下文中运行
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # 已有运行中的事件循环，退化为桩，避免嵌套循环报错
            return _stub(prompt)
        return asyncio.run(simple_chat(str(prompt), str(context)))
    except Exception:
        return _stub(prompt)


def _stub(prompt: str) -> str:
    return f"[stub] 当前环境暂未配置可用 LLM，已忽略请求：{(str(prompt)[:60])}"


def pool_status() -> Dict[str, Any]:
    """返回 LLM 池状态（是否配置、模型名等）。"""
    try:
        from .llm_client import LLMClient
        client = LLMClient()
        configured = bool(client.is_configured())
        return {
            "available": True,
            "configured": configured,
            "model": getattr(client, "_model", "unknown"),
            "base_url": getattr(client, "_base_url", "unknown"),
            "pool_size": 1,
        }
    except Exception:
        return {"available": False, "configured": False, "model": None, "pool_size": 0}


if __name__ == "__main__":
    print("pool_status:", pool_status())
    print("call_llm:", call_llm("用一句话总结今天的市场", "A股震荡"))
