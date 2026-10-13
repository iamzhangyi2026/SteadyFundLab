# -*- coding: utf-8 -*-
"""
time_lag —— 计算事件发生时点到对外发布时点的时滞（lag）。
输入 list of {event_time, publish_time}（字符串，尽量 ISO / 常见格式）。
输出 list of {event_time, publish_time, lag_seconds, lag_minutes}。
离线安全，解析失败的行返回 None 标记。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

_FMTS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
]


def _parse(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    s = str(ts).strip()
    if not s:
        return None
    # 处理带有 +08:00 但 %z 解析失败的简化
    s2 = s.replace("Z", "+0000") if s.endswith("Z") else s
    for fmt in _FMTS:
        try:
            return datetime.strptime(s2, fmt)
        except Exception:
            continue
    # 最后尝试 fromisoformat
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def detect_time_lag(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """计算每条事件的时滞。无法解析的时间返回 lag_seconds=None。"""
    out: List[Dict[str, Any]] = []
    for ev in events or []:
        try:
            et = _parse(ev.get("event_time"))
            pt = _parse(ev.get("publish_time"))
            if et is None or pt is None:
                out.append({
                    "event_time": ev.get("event_time"),
                    "publish_time": ev.get("publish_time"),
                    "lag_seconds": None,
                    "lag_minutes": None,
                })
                continue
            lag = (pt - et).total_seconds()
            out.append({
                "event_time": ev.get("event_time"),
                "publish_time": ev.get("publish_time"),
                "lag_seconds": lag,
                "lag_minutes": round(lag / 60.0, 2),
            })
        except Exception:
            out.append({
                "event_time": ev.get("event_time"),
                "publish_time": ev.get("publish_time"),
                "lag_seconds": None,
                "lag_minutes": None,
            })
    return out


if __name__ == "__main__":
    sample = [
        {"event_time": "2026-09-10 20:00:00", "publish_time": "2026-09-10 20:03:55"},
        {"event_time": "2026-09-10 18:00:00", "publish_time": "bad"},
    ]
    for r in detect_time_lag(sample):
        print(r)
