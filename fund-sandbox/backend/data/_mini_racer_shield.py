"""py_mini_racer 原生 V8 崩溃隔离层（shield）。

问题
----
py_mini_racer 0.14.1 自带的 V8 在 Windows 上偶发
`FATAL:partition_address_space.cc(243) Check failed` 原生 abort()，
直接杀死整个进程。akshare 在抓取基金净值 / 持仓 / 行业配置 / 基金清单 /
公告 / 资讯以及交易日历时，都会经 MiniRacer 跑一段 JS 反爬解密，
因此**任意一次 akshare 取数**都可能拖死 uvicorn 主进程。该崩溃是 native
abort()，不是 Python 异常 —— 既有的线程超时守卫（_timed_call）只能防「卡死」，
防不了「进程被带走」。

方案
----
monkeypatch py_mini_racer.MiniRacer：每次实例化改为拉起一个独立子进程，
在子进程里跑真实 V8；eval / call / execute 三个方法按长度前缀帧协议转发。
子进程内 V8 原生崩溃只会杀死子进程，父进程（uvicorn）毫发无伤，
akshare 调用方按「取数失败」降级返回空 / None —— 服务稳定常驻。

- 零调用点改动：akshare 与 backend 全部 fetch 代码无需任何修改。
- eval → execute 共享同一 V8 上下文（每个 MiniRacer 实例对应一个常驻子进程），
  完全等价于原生语义。
- 必须在任何 akshare 取数之前调用 install()（在 backend.api.main 模块顶部）。
"""
from __future__ import annotations

import pickle
import struct
import subprocess
import sys
import threading
from pathlib import Path

import py_mini_racer  # 仅用于保存真实类引用 + 改写模块属性

_REAL_MINI_RACER = py_mini_racer.MiniRacer
_WORKER = Path(__file__).resolve().with_name("_mini_racer_worker.py")


class ShieldedMiniRacer:
    """MiniRacer 的 drop-in 替身：把 JS 执行委托给隔离子进程。"""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ARG002
        self._proc = subprocess.Popen(
            [sys.executable, str(_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._lock = threading.Lock()
        self._closed = False

    def _rpc(self, op: str, args: tuple) -> object:
        with self._lock:
            if self._closed or self._proc.poll() is not None:
                raise RuntimeError("MiniRacer 隔离子进程不可用（已退出）")
            body = pickle.dumps((op, args))
            frame = struct.pack(">I", len(body)) + body
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(frame)
                self._proc.stdin.flush()
                assert self._proc.stdout is not None
                hdr = self._proc.stdout.read(4)
                if not hdr:
                    raise RuntimeError("MiniRacer 隔离子进程已终止（无响应）")
                (n,) = struct.unpack(">I", hdr)
                buf = self._proc.stdout.read(n)
                ok, payload = pickle.loads(buf)
            except (BrokenPipeError, OSError, EOFError, ValueError) as exc:
                # 子进程原生崩溃 / 管道断裂：这正是我们要隔离的目标，绝不能外泄到主进程
                self._close()
                raise RuntimeError(f"MiniRacer 隔离子进程崩溃: {exc}") from exc
            if not ok:
                raise payload  # 原样抛出子进程里的 JS / Python 异常
            return payload

    def eval(self, code, *args, **kwargs) -> object:  # noqa: ARG2
        return self._rpc("eval", (code,))

    def call(self, name, *args, **kwargs) -> object:  # noqa: ARG2
        return self._rpc("call", (name, *args))

    def execute(self, expr, *args, **kwargs) -> object:  # noqa: ARG2
        return self._rpc("execute", (expr,))

    def _close(self) -> None:
        self._closed = True
        try:
            self._proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def __del__(self) -> None:
        self._close()


def install() -> None:
    """安装隔离层：替换 py_mini_racer.MiniRacer，并修补已绑定的引用。

    必须在任何 akshare 取数之前调用（在 backend.api.main 模块顶部调用即可）。
    """
    py_mini_racer.MiniRacer = ShieldedMiniRacer  # type: ignore[assignment]
    # 保险：有些模块可能在导入时 `from py_mini_racer import MiniRacer` 绑定了真实类，
    # 一并替换为替身，确保全链路隔离。
    import sys as _sys

    for mod in list(_sys.modules.values()):
        try:
            if getattr(mod, "MiniRacer", None) is _REAL_MINI_RACER:
                mod.MiniRacer = ShieldedMiniRacer
        except Exception:  # noqa: BLE001
            pass
    print("[shield] py_mini_racer 已隔离到子进程（V8 原生崩溃不再拖死后端）")
