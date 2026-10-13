# -*- coding: utf-8 -*-
"""
fund-sandbox 黑匣子日志（可靠性底座 P0）
- 日志落 data/logs/backend-YYYY-MM-DD.log，滚动 30 天
- _Tee 镜像 stdout/stderr 到日志 + 控制台
- [CRASH] 标记未捕获异常（sys.excepthook）
- faulthandler 写 fault.log
- .session-<name>.json 心跳（每 30s 更新）
- 原则：日志失败绝不影响业务
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_KEEP_DAYS = 30
_HEARTBEAT_SECONDS = 30
_session_name = "backend"
_lock = threading.Lock()


class _Tee:
    """同时写原流与日志文件；写失败静默忽略，不影响业务。"""

    def __init__(self, stream, sink):
        self._stream = stream
        self._sink = sink

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._sink.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            self._sink.flush()
        except Exception:
            pass


def _log_path(name: str) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    return LOG_DIR / f"{name}-{day}.log"


def _rotate() -> None:
    """删除 30 天前的日志文件。"""
    try:
        cutoff = datetime.now().timestamp() - _KEEP_DAYS * 86400
        for p in LOG_DIR.glob("*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _write_session(name: str) -> None:
    try:
        path = DATA_DIR / f".session-{name}.json"
        payload = {
            "name": name,
            "alive": True,
            "ts": datetime.now().isoformat(),
        }
        path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    except Exception:
        pass


def _heartbeat(name: str) -> None:
    while True:
        try:
            _write_session(name)
        except Exception:
            pass
        # 用 Event 等待，避免 busy
        threading.Event().wait(_HEARTBEAT_SECONDS)


def _excepthook(exc_type, exc_val, exc_tb):
    try:
        msg = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] [CRASH] {msg}\n"
        with open(_log_path(_session_name), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def setup(name: str = "backend") -> None:
    """须最早调用。安装日志镜像、崩溃钩子、心跳。任何异常都吞掉。"""
    global _session_name
    _session_name = name
    try:
        logf = open(_log_path(name), "a", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, logf)
        sys.stderr = _Tee(sys.stderr, logf)
    except Exception:
        pass
    try:
        sys.excepthook = _excepthook
    except Exception:
        pass
    try:
        faulthandler.enable()
        faulthandler.dump_traceback_later(60, exit=False)
        fault_path = LOG_DIR / "fault.log"
        with open(fault_path, "a", encoding="utf-8") as fh:
            faulthandler.enable(fh)
    except Exception:
        pass
    try:
        _rotate()
        _write_session(name)
        t = threading.Thread(target=_heartbeat, args=(name,), daemon=True)
        t.start()
    except Exception:
        pass
