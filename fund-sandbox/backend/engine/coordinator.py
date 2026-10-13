"""混合运行协调：AI 在线时接管，离线时规则引擎兜底。

为什么需要这个模块
------------------
对话里的 AI（Claude）不可能 24 小时在线，但系统是常驻后台运行的。
如果 AI 一离线系统就停摆，会漏掉该做的时间敏感操作：
破纪律线该减仓的没减、定投该扣款的没扣、7 天罚期到期没提示。

所以需要一套交接机制：

    AI 在线  → AI 负责分析和决策，规则引擎降级为「执行者」+ 风控拦截
    AI 离线  → 规则引擎升格为「决策者」，按纪律线自主交易

判定"在不在"靠心跳：AI 每次操作刷新一个时间戳，
超过 config 的 heartbeat_timeout_hours 没有新心跳，就判定离线。

这不是三选一，而是分层——规则引擎永远在跑，只是职责不同。
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
    """是否处于「紧急停止」状态（人为暂停所有自动交易）。

    数据以 JSON 文件存储（与心跳文件同级），避免占用数据库表。
    文件不存在等价于未停止。
    """
    path = _emergency_path()
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("stop", False))
    except Exception:
        return False


def set_emergency_stop(on: bool, note: str = "") -> dict:
    """切换紧急停止状态。

    on=True  暂停所有自动交易（定投、规则引擎自动减仓等）
    on=False 恢复正常（由 AI 在线/离线逻辑与调度器节奏决定）
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
    否则会被判定为离线，规则引擎接管。

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


def current_mode() -> str:
    """返回当前实际生效的运行模式。

    config 里配置的是 mode，这里返回的是**结合心跳判断后的实际状态**：

        external   AI 在线，由其决策
        rule       AI 离线，规则引擎自主决策
        （mode 被显式设为 rule/external 时，原样返回，不做自动切换）
    """
    mode = get("decision", "mode", "auto")
    if mode != "auto":
        return mode
    return "external" if is_ai_online() else "rule"


def status() -> dict:
    """返回协调状态，供界面展示和数据健康面板使用。"""
    hb = read_heartbeat()
    mode = current_mode()
    timeout = get("decision", "heartbeat_timeout_hours", 6)

    idle_hours = None
    if hb:
        try:
            last = datetime.fromisoformat(hb["last_seen"])
            idle_hours = round((datetime.now() - last).total_seconds() / 3600, 2)
        except (KeyError, ValueError):
            pass

    return {
        "configured_mode": get("decision", "mode", "auto"),
        "effective_mode": mode,
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
    print("混合运行协调状态")
    print("=" * 78)
    print(f"  配置的模式    : {st['configured_mode']}")
    print(f"  实际生效模式  : {st['effective_mode']}")
    print(f"  AI 是否在线   : {'是' if st['ai_online'] else '否'}")
    print(f"  心跳超时阈值  : {st['heartbeat_timeout_hours']} 小时")
    print(f"  上次活跃      : {st['last_seen']}（来源 {st['last_source']}）")
    if st["idle_hours"] is not None:
        print(f"  距离上次      : {st['idle_hours']} 小时")
    print(f"  心跳文件      : {st['heartbeat_file']}")

    print("\n  说明：mode=auto 时，AI 离线超过阈值则规则引擎自动接管决策，")
    print("        保证定投扣款、纪律线减仓这类时间敏感操作不因对话关闭而漏掉。")
