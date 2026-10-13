# -*- coding: utf-8 -*-
"""数据一致性校验。

为什么要有这个脚本：
    数据一致性错误**不报错**，只有对着数据核对才露出来。

    所以把「改动后必须成立的不变量」写成断言，随时可跑 —— **改完数据/逻辑后跑一次**，
    比靠记忆可靠。用法：

        runtime\\python\\python.exe scripts\\check_consistency.py

    退出码 0 = 全部通过；1 = 有断言失败（会打印明细）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "fund_sandbox.db"

# (说明, SQL, 期望值)  —— 期望值 None 表示"只要不为 0 就算失败"
CHECKS: list[tuple[str, str]] = [
    ("market 类不应带板块映射（行情播报不是事件，映射会污染传导链）",
     "SELECT COUNT(*) FROM news_triage WHERE category='market' "
     "AND (TRIM(COALESCE(impact_boards,''))<>'' OR TRIM(COALESCE(impact_concepts,''))<>'')"),
    ("geo 类必须 100% 含官媒（只认官媒口径）",
     "SELECT COUNT(*) FROM news_triage WHERE category='geo' AND is_official=0"),
    ("conference 类必须 100% 含官媒",
     "SELECT COUNT(*) FROM news_triage WHERE category='conference' AND is_official=0"),
    ("事件指纹不得重复",
     "SELECT COUNT(*) FROM (SELECT dedup_key FROM news_triage GROUP BY dedup_key HAVING COUNT(*)>1)"),
    ("事件标题不得为空",
     "SELECT COUNT(*) FROM news_triage WHERE TRIM(COALESCE(title,''))=''"),
    ("事件可信度不得为空",
     "SELECT COUNT(*) FROM news_triage WHERE TRIM(COALESCE(credibility,''))=''"),
    ("sector_news 板块类型不得为空（industry/concept）",
     "SELECT COUNT(*) FROM sector_news WHERE board_type IS NULL OR TRIM(board_type)=''"),
    ("fund_fee 不得有空档位记录",
     "SELECT COUNT(*) FROM fund_fee WHERE TRIM(COALESCE(redeem_tiers,''))=''"),
    ("nav_daily 不得有非正净值",
     "SELECT COUNT(*) FROM nav_daily WHERE nav IS NOT NULL AND nav<=0"),
    ("news_triage 不得有非法 category",
     "SELECT COUNT(*) FROM news_triage WHERE category NOT IN "
     "('macro','policy','geo','conference','company','fund','holding','sector','market','military','other')"),
]


def main() -> int:
    if not DB.exists():
        print(f"[check] 找不到数据库：{DB}")
        return 1
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    failed = 0
    print(f"[check] 一致性校验（{DB.name}）")
    for desc, sql in CHECKS:
        try:
            n = conn.execute(sql).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR  {desc}\n         SQL 执行失败：{type(exc).__name__}: {exc}")
            failed += 1
            continue
        if n:
            print(f"  FAIL   {desc}  → {n} 条不符合")
            failed += 1
        else:
            print(f"  ok     {desc}")
    conn.close()

    print()
    if failed:
        print(f"[check] {failed} 项不通过 —— 请先修数据再继续（别让脏数据进入决策链）")
        return 1
    print("[check] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
