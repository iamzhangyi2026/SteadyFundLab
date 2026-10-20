# -*- coding: utf-8 -*-
"""
audit —— 审计日志。
审计行写入 data/audit.log 文件（文本追加），get_audit 从同一文件读取。
离线安全，绝不抛异常。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

try:
    from ..db.schema import get_db_path
    _AUDIT_LOG = get_db_path().resolve().parent / "audit.log"
except Exception:
    _AUDIT_LOG = Path("data/audit.log")


def audit_log(action: str, detail: str = "") -> Dict[str, Any]:
    """写入一条审计记录到 data/audit.log。"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}\t{action}\t{detail}\n"
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        return {"ok": True, "ts": ts, "action": action, "path": str(_AUDIT_LOG)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_audit(limit: int = 50) -> List[dict]:
    """读取最近的审计记录（从日志文件末尾取 limit 行）。"""
    try:
        if not _AUDIT_LOG.exists():
            return []
        lines = _AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        recent = lines[-int(limit):] if limit else lines
        out: List[dict] = []
        for ln in recent:
            parts = ln.split("\t", 2)
            out.append({
                "ts": parts[0] if len(parts) > 0 else "",
                "action": parts[1] if len(parts) > 1 else "",
                "detail": parts[2] if len(parts) > 2 else "",
            })
        return out
    except Exception:
        return []


if __name__ == "__main__":
    print(audit_log("demo_action", "demo detail"))
    print("last audit:", get_audit(3))
