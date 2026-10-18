"""跟踪接管 AI 的心跳和人工紧急暂停状态。

心跳只用于展示在线状态，不会将交易决策交给规则引擎或内置模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from backend.config import get

DEFAULT_HEARTBEAT = (
    Path(__file__).resolve().parents[2] / "data" / "heartbeat.json"
)

DEFAULT_EMERGENCY = (
    Path(__file__).resolve().parents[2] / "data" / "emergency_stop.json"
)


def _heartbeat_path() -> Path:
    p = get("runtime", "heartbeat_file")
    if p:
        path = Path(p)
        # 相对路径按项目根解析
        return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path
    return DEFAULT_HEARTBEAT


def _emergency_path() -> Path:
    p = get("runtime", "emergency_stop_file")
    if p:
        path = Path(p)
        return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path
    return DEFAULT_EMERGENCY


def is_emergency_stop() -> bool:
    """检查人工设置的自动操作暂停标记。

    数据以 JSON 文件存储（与心跳文件同级），避免占用数据库表。
    文件不存在等价于未停止。
    """
    path = _emergency_path()
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("stop"), bool):
            return True
        return payload["stop"]
    except Exception:
        # 急停状态无法读取时按已暂停处理，避免损坏文件意外恢复自动操作。
        return True


def set_emergency_stop(on: bool, note: str = "") -> dict:
    """切换紧急停止状态。

    on=True  暂停预先设定的自动操作，例如定投。
    on=False 恢复预先设定的自动操作；交易决策仍由接管 AI 负责。
    """
    path = _emergency_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stop": bool(on),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def touch_heartbeat(source: str = "ai", note: str = "") -> dict:
    """刷新心跳。

    AI 每次参与系统运作（分析、下单、读数）都应该调用一次，
    以便界面展示最新活跃状态。

    source 取值：
        ai        对话中的 AI
        manual    人手动操作（也算活跃，但不代表 AI 在分析）
        scheduler 调度器自身的例行巡检（不计为 AI 活跃）
    """
    path = _heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_seen": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "note": note,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_heartbeat() -> dict | None:
    """读取心跳。文件不存在或解析失败返回 None（等价于从未活跃）。"""
    path = _heartbeat_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_ai_online(timeout_hours: float | None = None) -> bool:
    """AI 是否在线（在超时窗口内有过心跳）。

    注意：只有 source="ai" 的心跳才算 AI 在线。
    调度器自己的巡检（source="scheduler"）不能证明 AI 在。
    """
    hb = read_heartbeat()
    if not hb:
        return False
    if hb.get("source") != "ai":
        return False
    try:
        last = datetime.fromisoformat(hb["last_seen"])
    except (KeyError, ValueError):
        return False
    timeout = timeout_hours if timeout_hours is not None else get(
        "decision", "heartbeat_timeout_hours", 6
    )
    return datetime.now() - last < timedelta(hours=timeout)


def decision_source() -> str:
    """返回唯一决策归属；心跳状态不改变决策权。"""
    return "external"


def current_mode() -> str:
    """兼容旧调用方；运行中已不存在可切换的决策模式。"""
    return decision_source()


def status() -> dict:
    """返回协调状态，供界面展示和数据健康面板使用。"""
    hb = read_heartbeat()
    source = decision_source()
    timeout = get("decision", "heartbeat_timeout_hours", 6)

    idle_hours = None
    if hb:
        try:
            last = datetime.fromisoformat(hb["last_seen"])
            idle_hours = round((datetime.now() - last).total_seconds() / 3600, 2)
        except (KeyError, ValueError):
            pass

    return {
        "decision_source": source,
        # 兼容尚未迁移的调用方；此字段不再随心跳切换。
        "effective_mode": source,
        "ai_online": is_ai_online(),
        "heartbeat_timeout_hours": timeout,
        "last_seen": hb.get("last_seen") if hb else None,
        "last_source": hb.get("source") if hb else None,
        "idle_hours": idle_hours,
        "emergency_stop": is_emergency_stop(),
        "heartbeat_file": str(_heartbeat_path()),
    }


if __name__ == "__main__":
    st = status()
    print("=" * 78)
    print("运行状态")
    print("=" * 78)
    print(f"  决策归属      : {st['decision_source']}")
    print(f"  AI 是否在线   : {'是' if st['ai_online'] else '否'}")
    print(f"  心跳超时阈值  : {st['heartbeat_timeout_hours']} 小时")
    print(f"  上次活跃      : {st['last_seen']}（来源 {st['last_source']}）")
    if st["idle_hours"] is not None:
        print(f"  距离上次      : {st['idle_hours']} 小时")
    print(f"  心跳文件      : {st['heartbeat_file']}")

    print("\n  心跳只展示 AI 在线状态；AI 离线时不会切换决策源。")
