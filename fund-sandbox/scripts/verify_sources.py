"""数据源体检脚本。

用途
----
akshare 是社区维护的库，接口会随源站改版而失效，且**失效时往往是静默的**
（返回 HTTP 200 但数据为空，或者字段名悄悄变了）。靠肉眼看界面发现不了，
必须定期检查。

什么时候跑：
    1. 升级 akshare 之后
    2. 发现界面数据不对劲时
    3. 定期巡检（比如每周一次）

输出
----
每个候选接口的可用性、返回条数、字段名。
字段名很重要——接口改版常常只改列名，导致取到 None 而不报错，
这类问题只有把列名打印出来才能发现。

用法：
    runtime\python\python.exe scripts\\verify_sources.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本可以直接 import backend 包
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import akshare as ak
except ImportError:
    print("错误：akshare 未安装。请先运行：")
    print("  runtime\python\python.exe -m pip install --no-cache-dir -r requirements.txt")
    sys.exit(1)

from backend.config import test_funds  # noqa: E402

TEST_FUND = test_funds()[0]["code"] if test_funds() else "040001"


def probe(label: str, fname: str, *args, **kwargs) -> dict:
    """探测一个接口能否正常返回数据。

    为什么用函数名字符串而不是直接传函数对象：
        akshare 升级后函数名会变（历史上 fund_portfolio_holdings_em
        就改成了 fund_portfolio_hold_em）。如果直接引用函数对象，
        接口一改名脚本就崩在 import 阶段，连其他接口的检查结果都看不到。
        用 getattr 取值，接口不存在时只标记这一项失效。
    """
    result = {
        "label": label,
        "fname": fname,
        "ok": False,
        "count": 0,
        "columns": [],
        "error": "",
    }
    fn = getattr(ak, fname, None)
    if fn is None:
        result["error"] = "接口不存在（akshare 已改名或移除）"
        return result
    try:
        df = fn(*args, **kwargs)
        if df is None:
            result["error"] = "返回 None"
            return result
        if hasattr(df, "empty") and df.empty:
            result["error"] = "返回空表（可能是参数不对或源站限流）"
            return result
        result["count"] = len(df)
        result["columns"] = list(df.columns)
        result["ok"] = True
    except Exception as e:  # noqa: BLE001 - 探测脚本需要捕获所有异常
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return result


def main() -> None:
    print("=" * 78)
    print(f"数据源体检 · akshare {getattr(ak, '__version__', '未知版本')}")
    print(f"测试基金代码: {TEST_FUND}")
    print("=" * 78)

    checks: list[dict] = []

    # --- 1. 基金净值 ---
    checks.append(
        probe(
            "基金净值·单位净值走势",
            "fund_open_fund_info_em",
            symbol=TEST_FUND,
            indicator="单位净值走势",
        )
    )
    checks.append(
        probe(
            "基金净值·累计净值走势",
            "fund_open_fund_info_em",
            symbol=TEST_FUND,
            indicator="累计净值走势",
        )
    )
    checks.append(
        probe("基金净值·全市场每日", "fund_open_fund_daily_em")
    )

    # --- 2. 板块 ---
    checks.append(probe("行业板块列表", "stock_board_industry_name_em"))
    checks.append(probe("概念板块列表", "stock_board_concept_name_em"))
    checks.append(probe("行业板块汇总(同花顺)", "stock_board_industry_summary_ths"))

    # --- 3. 基金持仓与行业配置 ---
    # 行业配置接口直接给出基金在各行业的占比，比从持仓股票反查行业省事得多
    checks.append(
        probe(
            "基金行业配置",
            "fund_portfolio_industry_allocation_em",
            symbol=TEST_FUND,
            date="20250630",
        )
    )
    checks.append(
        probe(
            "基金持仓明细",
            "fund_portfolio_hold_em",
            symbol=TEST_FUND,
            date="20250630",
        )
    )

    # --- 4. 交易日历 ---
    checks.append(probe("交易日历(新浪)", "tool_trade_date_hist_sina"))

    ok_count = 0
    for r in checks:
        if r["ok"]:
            ok_count += 1
            print(f"\n  [可用] {r['label']}  ({r['fname']})")
            print(f"         条数 {r['count']}")
            print(f"         列名 {r['columns']}")
        else:
            print(f"\n  [失效] {r['label']}  ({r['fname']})")
            print(f"         原因 {r['error']}")

    print("\n" + "=" * 78)
    print(f"体检结果：{ok_count} / {len(checks)} 个接口可用")
    if ok_count < len(checks):
        print("失效项需要换源或改代码。把这份输出交给 AI，据此调整 fetcher.py。")
    print("=" * 78)


if __name__ == "__main__":
    main()
