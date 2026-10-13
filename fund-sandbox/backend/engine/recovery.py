# -*- coding: utf-8 -*-
"""
备份 / 恢复（recovery）引擎。

规则：
  - 全量快照：gzip 复制整库到 data/snapshots/fund_sandbox-YYYYMMDD-HHMMSS.db.gz
  - 用 sqlite backup API 取一致快照（兼容 WAL），再 gzip
  - 保留策略：keep_snapshots（份数）与 keep_days（天数）取更严格者
  - 恢复：把快照解压覆盖回 data/fund_sandbox.db；覆盖前自动先打一份当前库快照（安全网）

注意：权威库中没有 snapshots 表，故快照元数据以文件系统为准（不建表）。
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import get

ROOT = Path(__file__).resolve().parents[2]          # backend/engine -> fund-sandbox/
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "fund_sandbox.db"
SNAP_DIR = DATA_DIR / "snapshots"


def _now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure() -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)


def create_snapshot(conn: Optional[sqlite3.Connection] = None) -> dict:
    """gzip 复制整库为一份快照，随后按策略清理旧快照。返回快照信息。"""
    _ensure()
    ts = _now()
    name = f"fund_sandbox-{ts}.db.gz"
    out = SNAP_DIR / name
    own = False
    if conn is None:
        from ..db.schema import get_connection
        conn = get_connection()
        own = True
    tmp = SNAP_DIR / f".tmp-{ts}.db"
    try:
        try:
            # 一致快照：把当前连接状态完整拷到临时库
            dst = sqlite3.connect(str(tmp))
            conn.backup(dst)
            dst.close()
        except Exception:
            # backup API 不可用时退化为直接复制文件
            if tmp.exists():
                tmp.unlink()
            shutil.copyfile(str(DB_PATH), str(tmp))
        with open(tmp, "rb") as f_in, gzip.open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        if tmp.exists():
            tmp.unlink()
        if own and conn is not None:
            conn.close()

    # 必须在 prune 之前测量大小：prune 可能按保留策略删除刚生成的快照
    size = out.stat().st_size if out.exists() else 0
    prune_res = prune()
    return {
        "ok": True, "name": name, "path": str(out),
        "size": size, "pruned": prune_res,
    }


def list_snapshots() -> list:
    """列出快照（按 mtime 倒序，即最新在前），含 size 与 mtime。

    注意：快照文件名可能来自不同命名方案（fund_sandbox-* 与旧 op_*），
    不能按文件名排序，否则新快照会被误判为最旧而遭 prune 误删。必须以 mtime 排序。
    """
    _ensure()
    items = []
    for p in SNAP_DIR.glob("*.db.gz"):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def restore_snapshot(name: str) -> dict:
    """
    把指定快照恢复回 data/fund_sandbox.db（破坏性，但这是沙盒恢复工具，按契约执行）。

    安全网：覆盖前先对「当前库」自动打一份快照（pre_restore_snapshot），
    仅当 name 命中现有快照时才执行。
    """
    _ensure()
    existing = {s["name"] for s in list_snapshots()}
    if name not in existing:
        return {
            "ok": False,
            "error": f"snapshot not found: {name}",
            "available": sorted(existing),
        }
    src = SNAP_DIR / name

    # 安全网：先保留当前状态
    pre = create_snapshot()

    tmp = DATA_DIR / ".restore-tmp.db"
    with gzip.open(src, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    # 清掉旧的 WAL/SHM，避免与新库不一致
    for ext in ("-wal", "-shm"):
        extra = DB_PATH.with_name(DB_PATH.name + ext)
        if extra.exists():
            extra.unlink()
    shutil.move(str(tmp), str(DB_PATH))
    return {
        "ok": True,
        "restored": name,
        "pre_restore_snapshot": pre.get("name"),
    }


def prune() -> dict:
    """保留最新 keep_snapshots 份与最新 keep_days 天，删除其余。"""
    keep_n = int(get("backup", "keep_snapshots", 7))
    keep_days = int(get("backup", "keep_days", 7))
    snaps = list_snapshots()  # 最新在前
    now = datetime.now().timestamp()
    removed: List[str] = []
    kept: List[str] = []

    # 1) 按份数
    for s in snaps[keep_n:]:
        (SNAP_DIR / s["name"]).unlink()
        removed.append(s["name"])
    # 2) 按天数
    for s in list_snapshots():
        age_days = (now - s["mtime"]) / 86400.0
        if age_days > keep_days:
            (SNAP_DIR / s["name"]).unlink()
            removed.append(s["name"])
        else:
            kept.append(s["name"])
    return {
        "removed": removed, "kept": kept,
        "keep_snapshots": keep_n, "keep_days": keep_days,
    }


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("== 创建快照 ==")
        print(create_snapshot(conn))
        print("\n== 快照列表 ==")
        for s in list_snapshots():
            print(s)
    finally:
        conn.close()
