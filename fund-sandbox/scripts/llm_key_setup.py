"""
fund-sandbox LLM Key 配置脚本

用法：
    runtime\python\python.exe scripts\llm_key_setup.py

作用：
    1. 接收你输入的 LLM API Key
    2. 用本机机器特征（MAC + 计算机名）做 XOR 加密
    3. 存到 data/llm_config.json
    4. 后端 _call_llm_api 自动读取

特点：
    - Key 永远不以明文形式出现在任何地方
    - 加密文件只能在本机解开（换电脑失效，需重新配置）
    - 不依赖任何第三方库（纯 Python 标准库）
    - 默认配置智谱 GLM-4-Flash（完全免费）
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import platform
import uuid
from datetime import datetime
from pathlib import Path

# 配置常量
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "glm-4-flash"
DEFAULT_SYSTEM_PROMPT = (
    "你是 fund-sandbox 的基金投资助手。基于用户的账户状态、持仓、新闻、情报回答问题。"
    "回答简洁、可执行；不确定就说'我不确定'。"
)
CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "llm_config.json"


def machine_key() -> bytes:
    """基于本机硬件特征生成稳定密钥（32字节）。"""
    mac = uuid.getnode()
    hostname = platform.node()
    raw = f"{mac}|{hostname}|fund-sandbox-v1"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def obfuscate(plaintext: str) -> str:
    """XOR 加密 + base64 编码。"""
    raw = plaintext.encode("utf-8")
    key = machine_key()
    out = bytearray()
    for i, b in enumerate(raw):
        out.append(b ^ key[i % len(key)])
    return base64.b64encode(bytes(out)).decode("ascii")


def deobfuscate(encoded: str) -> str:
    """解密。"""
    enc = base64.b64decode(encoded.encode("ascii"))
    key = machine_key()
    out = bytearray()
    for i, b in enumerate(enc):
        out.append(b ^ key[i % len(key)])
    return bytes(out).decode("utf-8")


def setup_key(api_key: str, base_url: str = BASE_URL, model: str = MODEL) -> None:
    """保存加密的 Key 到 data/llm_config.json。"""
    config = {
        "encrypted_key": obfuscate(api_key),
        "key_hint": model,
        "base_url": base_url,
        "model": model,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "machine_bound": True,
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Key 已加密保存到 {CONFIG_PATH}")
    print(f"   模型: {model}")
    print(f"   Base: {base_url}")
    print(f"   机器绑定: 是（仅本机可解开）")


def verify() -> None:
    """验证 Key 是否能正常解密。"""
    if not CONFIG_PATH.exists():
        print("❌ 配置文件不存在，请先 setup")
        return
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        recovered = deobfuscate(cfg["encrypted_key"])
        print(f"✅ 验证通过：解密后的 Key = {recovered[:8]}...{recovered[-4:]}")
        print(f"   模型: {cfg.get('model')}")
        print(f"   创建时间: {cfg.get('created_at')}")
    except Exception as e:
        print(f"❌ 验证失败：{e}")


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="fund-sandbox LLM Key 配置")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="配置新的 Key")
    p_setup.add_argument("--key", help="API Key（不传则交互式输入）")
    p_setup.add_argument("--base", default=BASE_URL, help="API base URL")
    p_setup.add_argument("--model", default=MODEL, help="模型名")

    p_verify = sub.add_parser("verify", help="验证当前 Key")

    args = p.parse_args()

    if args.cmd == "setup":
        key = args.key
        if not key:
            key = getpass.getpass("请输入 API Key（输入隐藏）：").strip()
        if not key:
            print("❌ Key 不能为空")
            return
        setup_key(key, args.base, args.model)
    elif args.cmd == "verify":
        verify()


if __name__ == "__main__":
    main()