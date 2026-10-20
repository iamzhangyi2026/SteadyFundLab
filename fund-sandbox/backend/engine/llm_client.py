# -*- coding: utf-8 -*-
"""
fund-sandbox LLM 客户端
- 不硬编码 API 密钥
- 从 data/llm_config.json 读取密钥（机器特征派生的密钥流 XOR + base64 混淆存储）
- 支持多模型路由
"""
from __future__ import annotations

import json
import base64
import hashlib
import platform
import uuid
from pathlib import Path
from typing import Optional


class LLMClient:
    """LLM客户端，支持智谱GLM等模型"""
    
    def __init__(self):
        self._config_path = self._get_config_path()
        self._api_key: Optional[str] = None
        self._base_url: str = "https://open.bigmodel.cn/api/paas/v4"
        self._model: str = "glm-4-flash"
        self._system_prompt: str = ""
        self._load_config()
    
    def _get_config_path(self) -> Path:
        """获取配置文件路径"""
        import os
        if os.path.exists("/data/data/com.termux"):
            return Path("/data/data/com.termux/files/home/storage/shared/fund-sandbox/data/llm_config.json")
        return Path(__file__).resolve().parents[3] / "data" / "llm_config.json"
    
    def _load_config(self) -> None:
        """加载 LLM 配置（还原 API 密钥）"""
        if not self._config_path.exists():
            return

        try:
            cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
            self._base_url = cfg.get("base_url", self._base_url)
            self._model = cfg.get("model", self._model)
            self._system_prompt = cfg.get("system_prompt", "")

            # 还原 API 密钥（按机器特征派生密钥流，与写入端同一算法）
            if "encrypted_key" in cfg:
                self._api_key = self._decrypt_key(cfg["encrypted_key"])
        except Exception:
            pass

    @staticmethod
    def _decrypt_key(encrypted: str) -> Optional[str]:
        """还原 API 密钥（XOR 混淆的逆运算，与 api_ai_config_set 写入端同一算法）"""
        try:
            mac = uuid.getnode()
            hostname = platform.node()
            raw = f"{mac}|{hostname}|fund-sandbox-v1"
            key = hashlib.sha256(raw.encode("utf-8")).digest()
            
            enc = base64.b64decode(encrypted.encode("ascii"))
            out = bytearray()
            for i, b in enumerate(enc):
                out.append(b ^ key[i % len(key)])
            return bytes(out).decode("utf-8")
        except Exception:
            return None
    
    def is_configured(self) -> bool:
        """检查是否已配置"""
        return self._api_key is not None
    
    async def chat(self, messages: list[dict], temperature: float = 0.7) -> dict:
        """
        发送对话请求
        :param messages: [{"role": "user/assistant/system", "content": "..."}]
        :param temperature: 温度参数
        :return: {"content": "...", "model": "..."}
        """
        if not self._api_key:
            return {"error": "LLM未配置", "content": "请先在设置中配置AI密钥"}
        
        # 添加系统提示
        if self._system_prompt and not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": self._system_prompt}] + messages
        
        # 构造请求
        import httpx
        
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return {
                    "content": data["choices"][0]["message"]["content"],
                    "model": data.get("model", self._model),
                }
        except Exception as e:
            return {"error": str(e), "content": "AI服务暂时不可用，请稍后重试"}
    
    async def call_tool(self, tool_name: str, tool_input: dict) -> str:
        """
        调用工具（用于Function Calling）
        :param tool_name: 工具名称
        :param tool_input: 工具输入参数
        :return: 工具输出结果
        """
        # 这里返回模拟结果，实际使用时需要对接具体工具
        return json.dumps({"tool": tool_name, "result": "执行成功"})


async def simple_chat(prompt: str, context: str = "") -> str:
    """
    简单对话接口
    :param prompt: 用户提示
    :param context: 上下文信息
    :return: AI回复
    """
    client = LLMClient()
    
    if not client.is_configured():
        # 未配置时返回模拟回复
        return _mock_response(prompt, context)
    
    messages = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": prompt})
    
    result = await client.chat(messages)
    return result.get("content", "抱歉，我无法回答这个问题")


def _mock_response(prompt: str, context: str = "") -> str:
    """未配置时的模拟回复"""
    import random
    
    responses = [
        "根据当前数据，这只基金表现稳健，建议持续关注。",
        "市场波动较大，建议保持定投节奏，不要追涨杀跌。",
        "从历史数据看，这类基金适合长期持有，短期波动不必过于在意。",
        "当前估值处于合理区间，可根据个人风险偏好适当配置。",
        "建议您分散投资，不要过度集中在单一行业或主题。",
    ]
    
    return random.choice(responses)


if __name__ == "__main__":
    client = LLMClient()
    print(f"LLM配置状态: {'已配置' if client.is_configured() else '未配置'}")
    print(f"模型: {client._model}")
    print(f"Base URL: {client._base_url}")
