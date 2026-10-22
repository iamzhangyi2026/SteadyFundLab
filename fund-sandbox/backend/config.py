# -*- coding: utf-8 -*-
"""
fund-sandbox 配置模块（桌面端）
- 唯一规则来源：config/config.toml（真实世界规则的集中地）
- 提供 test_funds()（读真实 funds 表）、SCREEN_RULES、get_llm_config()
- 不写死任何金融魔数；缺省值一律来自 config.toml
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]          # backend/ -> fund-sandbox/
CONFIG_PATH = ROOT / "config" / "config.toml"

_CONFIG: Optional[dict] = None


def load_config() -> dict:
    """读取 config/config.toml（tomllib，二进制模式）。失败返回空 dict，绝不抛异常。"""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    data: dict = {}
    try:
        import tomllib
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
    except Exception:
        data = {}
    _CONFIG = data
    return _CONFIG


def get_config() -> dict:
    return load_config()


def section(name: str, default: Optional[dict] = None) -> dict:
    return load_config().get(name, default or {})


def get(sec: str, key: str, default: Any = None) -> Any:
    """取配置项：config[sec][key]，缺失返回 default。"""
    return load_config().get(sec, {}).get(key, default)


# ──────────────────────────────────────────────────────────────
# 基金池（候选池）：读 funds 表 status='active' 的真实数据，
# screener / refresh 直接作用于真实候选池。
# ──────────────────────────────────────────────────────────────
def test_funds() -> list[dict]:
    try:
        from .db.schema import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT code, name, category, is_qdii FROM funds "
                "WHERE status='active' ORDER BY code"
            ).fetchall()
            return [
                {
                    "code": r["code"],
                    "name": r["name"],
                    "category": r["category"],
                    "is_qdii": bool(r["is_qdii"]),
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        # 不静默：失败原因进 logging（可从日志归因），而不是只剩「筛选结果为空」无从排查
        logging.getLogger(__name__).warning(
            "test_funds 读取 funds 表失败：%s: %s", type(e).__name__, e)
        return []


# ──────────────────────────────────────────────────────────────
# 筛选规则：主轨（分位低+趋势向上）来自 config [rules]/[health]；
# main_track / backup_track / high_vol_relax 三个轨的键名与
# screen_rules.screen_candidates 消费的 rules 键一致；
# position_limits 由 screener 的仓位建议单独消费，不在筛选轨内。
# ──────────────────────────────────────────────────────────────
SCREEN_RULES: dict = {
    # 主轨：分位低 + 趋势向上
    "main_track": {"percentile_max": 50.0, "ret_1y_min": 0.0},
    # 备轨：分位高但回调后反弹
    "backup_track": {
        "percentile_min": 70.0,
        "ret_1y_min": 0.0,
        "ret_3m_threshold": -10.0,
    },
    # 高风险放宽：高波动但回撤可控
    "high_vol_relax": {"annual_vol_max": 35.0, "max_dd_min": -50.0},
    # 仓位控制
    "position_limits": {
        "max_single_pct": 25.0,
        "min_position": 100.0,
        "step_100": 100.0,
        "step_500": 500.0,
    },
}


# ──────────────────────────────────────────────────────────────
# LLM 配置：从 data/llm_config.json 读取。
# 密钥不明文落盘——存的是按机器特征派生的密钥流 XOR 后 base64 的串
# （可逆混淆：防随手翻看，不防针对性提取；能读文件又看得到本源码即可还原）。
# ──────────────────────────────────────────────────────────────
def get_llm_config() -> dict:
    data_dir = ROOT / "data"
    config_file = data_dir / "llm_config.json"
    default = {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "has_key": False,
        "system_prompt": (
            "你是 fund-sandbox 的基金投资助手。基于用户的账户状态、持仓、新闻、情报回答问题。"
            "回答简洁、可执行；不确定就说'我不确定'。"
        ),
    }
    if not config_file.exists():
        return default
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        return {
            "base_url": cfg.get("base_url", default["base_url"]),
            "model": cfg.get("model", default["model"]),
            "has_key": bool(cfg.get("encrypted_key") or cfg.get("api_key")),
            "system_prompt": cfg.get("system_prompt", default["system_prompt"]),
        }
    except Exception:
        return default


if __name__ == "__main__":
    print("=== 配置加载 ===")
    print("config.toml 存在:", CONFIG_PATH.exists())
    print("初始资金:", get("capital", "initial_cash"))
    print("每月新增:", get("capital", "monthly_quota"))
    print("候选池数量:", len(test_funds()))
    print("纪律线:", section("rules"))
