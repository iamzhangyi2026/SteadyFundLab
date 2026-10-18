"""每日更新归档库（standalone 运行器）。

把运营库 fund_sandbox.db 里该留的新闻 / 数据 / 资讯沉淀进独立 corpus.db。
- 无需后端运行：直接读运营库、写归档库。
- 若后端正在跑，其 RSS 后台线程已把最新新闻写进运营库，本脚本同步时一并带走。
- 加 --fetch-news 可在同步前先连网补抓最新 RSS 财经新闻（后端没开时也能补齐）。

用法：
    python scripts/update_corpus.py                # 仅同步现有运营库内容
    python scripts/update_corpus.py --fetch-news   # 同步前先补抓 RSS
供每日自动化 / 系统计划任务调用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 项目根进 sys.path，保证 `import backend...` 可用
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db.schema import get_connection
from backend.engine import corpus


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fetch-news", action="store_true",
        help="同步前先连网补抓最新 RSS 财经新闻（会触发 rss_news.fetch_market_news）",
    )
    args = ap.parse_args()

    op = get_connection()
    try:
        if args.fetch_news:
            try:
                from backend.engine import rss_news

                # do_translate=False：归档存的是原文，翻译由常驻后处理线程负责，脚本不必干等
                n = rss_news.fetch_market_news(op, do_translate=False)
                print(f"[corpus] RSS 补抓新增 {n} 条")
            except Exception as e:  # noqa: BLE001
                print(f"[corpus] RSS 补抓失败（跳过）：{type(e).__name__}: {e}")
        p = corpus.init_corpus(op)
    finally:
        op.close()

    # 同步前关闭运营库连接，避免 ATTACH 同文件时锁竞争
    added = corpus.sync_from_operational()

    print(f"[corpus] 归档库：{p}")
    for t, n in added.items():
        print(f"  {t:24} +{n}")
    st = corpus.stats()
    print(f"[corpus] 当前合计 {st.get('_total')} 条")


if __name__ == "__main__":
    main()
