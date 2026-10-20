# -*- coding: utf-8 -*-
"""B 区（真实持仓）端到端回归检查。

脚本自己起一个「只挂 B 区路由 + 前端 dist」的临时服务，跑完自动关：
不依赖外部服务，也**不碰 8765**（那是用户在用的后端）。

覆盖：导入 → 人工确认 → 持仓 → 详情+深度分析 → 净值/估值刷新 → 轮询窗口
      → 快照 → 丢弃 → 404 → 清理，外加「前端↔后端接口契约」与「隔离规则」静态核对。

用法：  python scripts/e2e_real.py
"""
from __future__ import annotations

import glob
import os
import re
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.real.api import router as real_router  # noqa: E402

PORT = int(os.environ.get("E2E_PORT", "8810"))
BASE = f"http://127.0.0.1:{PORT}/api/real"
SITE = f"http://127.0.0.1:{PORT}"
DIST = ROOT / "frontend" / "dist"
FE_VIEW = ROOT / "frontend" / "src" / "views" / "RealPositions.jsx"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  → {detail}" if detail else ""))


def build_app() -> FastAPI:
    a = FastAPI()
    a.include_router(real_router)
    a.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @a.get("/favicon.svg")
    def favicon():
        return FileResponse(str(DIST / "favicon.svg"))

    @a.get("/{full_path:path}")
    def spa(full_path: str):
        cand = (DIST / full_path).resolve()
        if full_path and str(cand).startswith(str(DIST)) and cand.is_file():
            return FileResponse(str(cand))
        return FileResponse(str(DIST / "index.html"))

    return a


def start_server() -> uvicorn.Server:
    cfg = uvicorn.Config(build_app(), host="127.0.0.1", port=PORT, log_level="error")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(80):
        try:
            requests.get(f"{BASE}/ocr-status", timeout=2)
            return srv
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("E2E 服务启动失败")


def main() -> int:
    srv = start_server()
    print(f"\nB 区端到端检查｜临时服务 {SITE}（跑完自动关闭）\n")
    try:
        print("=== 1. OCR 状态 ===")
        r = requests.get(f"{BASE}/ocr-status", timeout=20).json()
        check("GET /ocr-status", isinstance(r, dict) and "available" in r, str(r)[:50])

        print("\n=== 2. 文本导入（一行一只，不自动入库）===")
        line = "000001 华夏成长混合 1234.56 3000 3123.45 123.45 3.95%"
        print(f"   输入: {line}")
        j = requests.post(f"{BASE}/imports/text", json={"text": line}, timeout=30).json()
        iid, rows = j.get("import_id"), j.get("rows", [])
        check("POST /imports/text 返回候选行", bool(iid) and len(rows) == 1, f"import_id={iid}")
        row = rows[0] if rows else {}
        print("   解析结果:", row)
        check("份额解析正确", row.get("shares") == 1234.56, f"shares={row.get('shares')}")
        check("成本解析正确", row.get("cost_amount") == 3000.0, f"cost={row.get('cost_amount')}")
        check("市值解析正确", row.get("market_amount") == 3123.45, f"market={row.get('market_amount')}")
        check("盈亏解析正确", row.get("profit") == 123.45, f"profit={row.get('profit')}")
        check("收益率存小数口径", row.get("profit_rate") == 0.0395, f"rate={row.get('profit_rate')}")
        check("全部标记 needs_review", row.get("needs_review") == 1)

        print("\n=== 3. 读取导入批次 ===")
        j = requests.get(f"{BASE}/imports/{iid}", timeout=20).json()
        check("GET /imports/{id} 为 pending", j.get("status") == "pending", f"status={j.get('status')}")

        print("\n=== 4. 人工确认入库 ===")
        payload = {"rows": [{"fund_code": "000001", "fund_name": "华夏成长混合", "shares": 1234.56,
                            "cost_amount": 3000.0, "market_amount": 3123.45, "profit": 123.45,
                            "profit_rate": 0.0395}]}
        j = requests.post(f"{BASE}/imports/{iid}/confirm", json=payload, timeout=30).json()
        check("POST /imports/{id}/confirm", j.get("holdings_count") == 4, str(j)[:70])

        print("\n=== 5. 持仓列表 ===")
        j = requests.get(f"{BASE}/holdings", timeout=30).json()
        hs = j.get("holdings", [])
        test_h = next((h for h in hs if h["fund_code"] == "000001"), None)
        check("导入后出现在持仓", test_h is not None, f"共 {len(hs)} 只")
        check("持仓带估值字段(est_*)", bool(test_h) and "est_nav" in test_h and "est_time" in test_h,
              f"est_nav={(test_h or {}).get('est_nav')} est_chg={(test_h or {}).get('est_change_pct')}")
        check("summary 汇总正确", (j.get("summary") or {}).get("holdings_count") == len(hs),
              str(j.get("summary")))

        print("\n=== 6. 单只详情 + 深度分析 ===")
        j = requests.get(f"{BASE}/holdings/000001/detail", timeout=180).json()
        m = j.get("metrics", {})
        check("detail 返回 ok", j.get("ok") is True, str(j.get("error", ""))[:40])
        check("指标齐全", all(k in m for k in ("ret_1m", "ret_3m", "ret_6m", "ret_1y",
                                             "max_drawdown", "vol", "history_days")),
              f"ret_1m={m.get('ret_1m')} ret_1y={m.get('ret_1y')} mdd={m.get('max_drawdown')} vol={m.get('vol')}")
        check("净值序列充足", len(j.get("series", [])) > 50, f"{len(j.get('series', []))} 点")
        check("深度分析含四段（兼容文本）", all(k in j.get("analysis", "") for k in ("走势", "风险", "回撤修复", "持仓")),
              j.get("analysis", "")[:56].replace("\n", " / "))

        print("\n=== 7. 刷新：净值 / 估值 / 轮询窗口 ===")
        j = requests.post(f"{BASE}/nav/refresh", timeout=240).json()
        check("POST /nav/refresh", j.get("ok") is True, str(j))
        j = requests.post(f"{BASE}/estimates/refresh", timeout=60).json()
        check("POST /estimates/refresh", j.get("ok") is True and j.get("refreshed", 0) >= 1, str(j))
        j = requests.get(f"{BASE}/poll-window", timeout=30).json()
        check("GET /poll-window 判定可用", {"active", "reason", "next_change"} <= set(j),
              f"active={j.get('active')} | {j.get('reason')} | 下次开启={j.get('next_change')}")

        print("\n=== 8. 快照：手动生成（snap_date 为主键，同日覆盖）===")
        n0 = len(requests.get(f"{BASE}/snapshots?limit=5", timeout=20).json().get("snapshots", []))
        j1 = requests.post(f"{BASE}/snapshots", timeout=30).json()
        check("POST /snapshots 落一条", j1.get("ok") is True and j1.get("holdings_count") == 4,
              str(j1)[:70])
        snaps = requests.get(f"{BASE}/snapshots?limit=5", timeout=20).json().get("snapshots", [])
        today_row = next((s for s in snaps if s.get("snap_date") == j1.get("snap_date")), None)
        check("今日快照存在且数值一致", today_row is not None
              and abs(today_row.get("total_market", 0) - j1.get("total_market", -1)) < 0.01
              and today_row.get("holdings_count") == 4, f"总数 {n0} → {len(snaps)}（同日覆盖）")

        print("\n=== 9. 丢弃导入（另建一条再丢）===")
        j = requests.post(f"{BASE}/imports/text", json={"text": "000001 华夏成长混合 10 100 110"},
                          timeout=30).json()
        j = requests.post(f"{BASE}/imports/{j.get('import_id')}/discard", timeout=20).json()
        check("POST /imports/{id}/discard", j.get("ok") is True, str(j)[:50])

        print("\n=== 10. 异常路径 ===")
        r = requests.get(f"{BASE}/holdings/999999/detail", timeout=20)
        check("detail 不存在 → 404", r.status_code == 404, f"status={r.status_code}")

        print("\n=== 11. 清理：删除测试持仓 ===")
        j = requests.delete(f"{BASE}/holdings/000001", timeout=20).json()
        check("DELETE /holdings/{code}", j.get("ok") is True, str(j)[:40])
        # 测试持仓被删今日快照会失真，重建一次
        requests.post(f"{BASE}/snapshots", timeout=30)
        j = requests.get(f"{BASE}/holdings", timeout=30).json()
        check("删除后无残留", not any(h["fund_code"] == "000001" for h in j.get("holdings", [])),
              f"剩余 {len(j.get('holdings', []))} 只")

        print("\n=== 12. 前端 ↔ 后端接口契约（openapi 核对）===")
        spec = requests.get(f"{SITE}/openapi.json", timeout=20).json()
        # 路径参数名归一化：{fund_code} / {import_id} 统一成 {}，只比结构
        backend_norm = {re.sub(r"\{[^}]*\}", "{}", p) for p in spec.get("paths", {})}
        fe = FE_VIEW.read_text(encoding="utf-8")
        called = {"/api/real/" + re.sub(r"\$\{[^}]*\}", "{}", r0.split("?")[0])
                  for r0 in re.findall(r"\$\{API\}/([A-Za-z0-9_/.${}\?=&-]+)", fe)}
        check("前端调用的路径都有后端路由", called <= backend_norm,
              f"缺 {sorted(called - backend_norm)}")

        print("\n=== 13. 隔离规则静态核对（账本隔离 / 数据共用）===")
        # 边界：只隔离**账本**（真钱 vs 模拟）；行情/新闻/规则等真实资料共用一份。
        # A 区不得引用 B 区**业务逻辑**；唯一例外是只读 B 区持仓代码（把公共行情抓全）。
        bad_a = []
        for p in sorted(glob.glob(str(ROOT / "backend/engine/*.py"))) + \
                 sorted(glob.glob(str(ROOT / "backend/data/*.py"))):
            for ln in open(p, encoding="utf-8"):
                code = ln.split("#")[0]
                if "backend.real" not in code:
                    continue
                if "backend.real.db" in code:   # 允许：只读连接，取持仓代码
                    continue
                bad_a.append(f"{os.path.basename(p)}: {code.strip()[:60]}")
        check("A 区仅允许只读 B 区持仓代码（backend.real.db）", not bad_a, str(bad_a))

        # B 区可**只读**公共数据（允许出现 A 区库路径），但不得写它、不得 JOIN 账本表。
        bad_write, ledger_join = [], []
        for f in glob.glob(str(ROOT / "backend/real/*.py")):
            code_only = re.sub(r'""".*?"""', "", open(f, encoding="utf-8").read(), flags=re.S)
            for ln in code_only.splitlines():
                code = ln.split("#")[0]
                up = code.upper()
                if "fund_sandbox.db" in code and any(w in up for w in ("INSERT", "UPDATE", "DELETE")):
                    bad_write.append(f"{os.path.basename(f)}: {code.strip()[:60]}")
                if "JOIN" in up and any(t in up for t in ("LOTS", "ORDERS", "BUDGET")):
                    ledger_join.append(f"{os.path.basename(f)}: {code.strip()[:60]}")
        check("B 区对 A 区库只读（无写语句）", not bad_write, str(bad_write))
        check("B 区未 JOIN A 区账本表", not ledger_join, str(ledger_join))
    finally:
        srv.should_exit = True
        time.sleep(1)

    print(f"\n{'=' * 46}")
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    print("端到端检查全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
