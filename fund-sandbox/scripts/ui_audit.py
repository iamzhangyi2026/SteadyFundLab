# -*- coding: utf-8 -*-
r"""No-browser UI smoke: since this sandbox cannot run Chromium daemon
(agent-browser open gets SIGTERM), use stdlib only (urllib + re + json + pathlib)
to assert: (1) served HTML bundle matches dist/assets, (2) bundle contains
key UI strings, (3) all main API endpoints return 200.

Usage: runtime\python\python.exe scripts\ui_audit.py [--base http://127.0.0.1:8765]
Exit: 0 = all pass; 1 = any failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path


# 关键 UI 文案——每次改动前端交互文案时务必同步更新这里
EXPECTED_STRINGS = [
    ("开放申购", "申购状态枚举"),
    ("限大额", "申购状态枚举"),
    ("暂停申购", "申购状态枚举"),
    ("起购", "持仓列起购文案"),
    ("单日上限", "限大额醒目提示"),
    ("暂停申购·不可买入", "详情弹窗禁用按钮文案"),
    ("全部赎回", "OrderModal 全部赎回快捷按钮"),
    ("当前持有", "OrderModal 当前持有份额提示"),
    ("持仓", "持仓标签"),
    ("关注", "关注标签"),
    # 交易页：搜索结果双按钮 + 池卡购买
    ("购买", "Trade 搜索/池卡购买按钮"),
    ("已关注", "Trade 搜索已关注状态"),
    # 持仓页：关注搜索框
    ("搜索已关注基金", "Positions 关注搜索框 placeholder"),
    # 全局 ErrorBoundary（防 render 异常导致 Electron 白屏）
    ("页面出错了", "App.jsx ErrorBoundary fallback 文案"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    project_root = Path(__file__).resolve().parents[1]
    dist = project_root / "frontend" / "dist"

    failures: list[str] = []

    # ---- 1) 静态 dist 与 served HTML bundle 一致 ----
    try:
        with urllib.request.urlopen(base + "/", timeout=60) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[FAIL] 抓取 {base}/: {e}")
        return 1
    served = re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", html)
    if not served:
        print(f"[FAIL] served HTML 未含 index-*.js 引用：{html[:200]}...")
        return 1
    served_name = served.group(1)
    local_js = dist / "assets" / served_name
    print(f"[ OK ] served bundle = {served_name}")
    if not local_js.exists():
        failures.append(f"served bundle 在 dist 中不存在：{local_js}")

    # ---- 2) 关键 UI 文案必须在 bundle 里 ----
    if local_js.exists():
        bundle = local_js.read_text(encoding="utf-8", errors="replace")
        for needle, why in EXPECTED_STRINGS:
            if needle not in bundle:
                failures.append(f"dist/{served_name} 缺少关键文案「{needle}」（{why}）")
            else:
                print(f"[ OK ] 包含「{needle}」")
    # dist 与源码新鲜度
    if dist.exists():
        newest_src_mtime = max(
            (p.stat().st_mtime for p in (project_root / "frontend" / "src").rglob("*.jsx")),
            default=0,
        )
        newest_dist_mtime = max(
            (p.stat().st_mtime for p in dist.rglob("*.js")),
            default=0,
        )
        if newest_src_mtime > newest_dist_mtime:
            failures.append(
                f"frontend/src 新于 dist（src {newest_src_mtime:.0f} > dist {newest_dist_mtime:.0f}）"
                "—— 需要 vite build"
            )
        else:
            print(f"[ OK ] dist 新于或等于源码（dist={newest_dist_mtime:.0f}）")

    # ---- 3) 主接口全 200 ----
    endpoints = [
        "/api/health",
        "/api/account",
        "/api/funds",
        "/api/positions",
        "/api/orders?limit=10",
        "/api/dingtou/plans",
        "/api/analysis",
        "/api/exposure",
        "/api/boards?limit=6",
        "/api/news?limit=10",
        "/api/market-news?limit=10",
        "/api/intel?limit=10",
        "/api/settings",
        "/api/status",
        "/api/portfolio/curve?limit=30",
    ]
    for ep in endpoints:
        try:
            with urllib.request.urlopen(base + ep, timeout=60) as r:
                code = r.getcode()
                if code != 200:
                    failures.append(f"{ep} → HTTP {code}")
                else:
                    print(f"[ OK ] {ep}")
        except Exception as e:
            failures.append(f"{ep} → 异常 {e}")

    if failures:
        print("\n=========== 失败清单 ===========")
        for f in failures:
            print(" -", f)
        print(f"\n共 {len(failures)} 项失败")
        return 1
    print("\n✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())