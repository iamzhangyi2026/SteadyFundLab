"""MiniRacer 隔离子进程工作体（被 _mini_racer_shield 拉起）。

只导入「原生」py_mini_racer —— 绝不导入 shield 自身，否则会递归拉子进程。
在子进程内持有单个真实 MiniRacer V8 上下文，按「4 字节长度前缀 + pickle」帧协议
接收 eval / call / execute 指令，回传结果或异常。

若 V8 在本进程内原生 abort（FATAL:partition_address_space.cc(243) Check failed），
本进程直接死，父进程读到 EOF / 管道断裂，按「取数失败」降级 —— uvicorn 主进程不受影响。
"""
from __future__ import annotations

import pickle
import struct
import sys

from py_mini_racer import MiniRacer


def _main() -> None:
    # 真实 V8 上下文。若构造阶段就原生崩溃，本进程直接死，由父进程兜底。
    ctx = MiniRacer()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        hdr = stdin.read(4)
        if not hdr:
            break
        (n,) = struct.unpack(">I", hdr)
        buf = stdin.read(n)
        op, args = pickle.loads(buf)
        try:
            # op ∈ {"eval", "call", "execute"}，args 为已展开的位置参数元组
            result = getattr(ctx, op)(*args)
            ok, payload = True, result
        except BaseException as exc:  # noqa: BLE001  把 JS/Python 异常回传，父进程原样抛出
            ok, payload = False, exc
        out = pickle.dumps((ok, payload))
        stdout.write(struct.pack(">I", len(out)) + out)
        stdout.flush()


if __name__ == "__main__":
    _main()
