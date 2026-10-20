"""每日数据刷新脚本。

作用
----
抓真实数据 → 双源交叉验证 → 异常检测 → 落库 → 生成健康报告。

这是每天收盘后（20:00）要跑的主脚本，也是验证系统是否正常工作的主要手段：
跑完看输出，就知道数据源有没有挂、数据对不对、有没有异常需要 AI 定性。

用法
----
    runtime\python\python.exe scripts\\fetch_daily.py

退出码
------
    0  全部成功
    1  有数据源失效或数据异常需要处理
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import test_funds  # noqa: E402
from backend.data import fetcher  # noqa: E402
from backend.db import get_connection, init_db  # noqa: E402
from backend.engine.calendar import TradeCalendar  # noqa: E402

HEALTH_DIR = ROOT / "data" / "health"
ANOMALY_DIR = ROOT / "data" / "verdicts"


def line(char: str = "-", n: int = 78) -> str:
    return char * n


def main() -> int:
    print(line("="))
    print(f"fund-sandbox 每日刷新 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(line("="))

    init_db()
    conn = get_connection()

    # ---------------------------------------------------------------- 交易日历
    print("\n【1】交易日历")
    cal = TradeCalendar()
    cal.load()
    today = date.today()
    info = cal.info()
    print(f"  日历来源     : {info['source']}")
    print(f"  降级状态     : {'是（不可信，仅排除周末）' if cal.degraded else '否'}")
    print(f"  日历天数     : {info['count']}")
    print(f"  今天 {today} : {'交易日' if cal.is_trading_day(today) else '非交易日'}")
    print(f"  下一交易日   : {cal.next_trading_day(today)}")
    print(f"  上一交易日   : {cal.prev_trading_day(today)}")
    print(f"  当前可下单   : {'是' if cal.is_market_open_now() else '否（已过 15:00 或非交易日）'}")

    record_health = fetcher.record_health
    record_health(
        conn,
        source=info["source"],
        data_type="calendar",
        status="ok" if not cal.degraded else "suspicious",
        record_count=info["count"],
        message="交易日历加载" + ("（降级：仅排除周末）" if cal.degraded else ""),
    )

    # ------------------------------------------------------------------ 基金
    print("\n【2】基金净值")
    all_anomalies: list[dict] = []
    funds = test_funds()
    problem_found = False

    for f in funds:
        code = f["code"]
        name = f["name"]
        print(f"\n  ── {code} {name}" + ("  [QDII 净值 T+2 滞后]" if f.get("is_qdii") else ""))

        primary = fetcher.fetch_nav_em(code)
        if not primary:
            print("     主源抓取失败")
            record_health(
                conn, "em_fund_info", "nav", "failed", 0, f"{code} 主源返回空"
            )
            problem_found = True
            continue
        print(f"     主源        : {len(primary)} 条，"
              f"{primary[0]['date']} ~ {primary[-1]['date']}")

        secondary = fetcher.fetch_nav_daily_em(code)
        if secondary:
            print(f"     第二源      : {len(secondary)} 条，"
                  f"{secondary[0]['date']} ~ {secondary[-1]['date']}")
        else:
            print("     第二源      : 不可用（数据仍入库，但标记为未验证）")

        verified, conflicts = fetcher.cross_verify(primary, secondary)
        vcount = sum(1 for r in verified if r.get("verified"))
        print(f"     交叉验证    : {vcount} 条一致"
              + (f"，{len(conflicts)} 条冲突" if conflicts else ""))
        if conflicts:
            problem_found = True
            for c in conflicts[:3]:
                print(f"       ! {c['date']} 主源 {c['nav_primary']} vs "
                      f"第二源 {c['nav_secondary']}（差 {c['diff_pct']}%）")

        anomalies = fetcher.detect_anomalies(primary)
        if anomalies:
            print(f"     异常检测    : 发现 {len(anomalies)} 项，已写入待 AI 定性清单")
            for a in anomalies:
                all_anomalies.append({"fund_code": code, "fund_name": name, **a})
        else:
            print("     异常检测    : 无异常")

        # 净值闸门：越界的不入库
        clean = [r for r in verified if fetcher.pass_nav_gate(r["nav"])]
        blocked = len(verified) - len(clean)
        if blocked:
            print(f"     闸门拦截    : {blocked} 条越界数据未入库")

        n = fetcher.save_nav(conn, code, clean, "em_fund_info")
        print(f"     入库        : {n} 条")

        record_health(
            conn,
            "em_fund_info",
            "nav",
            "ok" if not conflicts else "suspicious",
            n,
            f"{code} 净值 {n} 条，交叉验证 {vcount} 条"
            + (f"，冲突 {len(conflicts)} 条" if conflicts else ""),
        )

        # 基金状态（限购检测 + 真实费率来源）
        st = fetcher.fetch_fund_status(code)
        if st:
            print(f"     申购状态    : {st.get('purchase_status') or '未知'}")
            print(f"     赎回状态    : {st.get('redeem_status') or '未知'}")
            print(f"     手续费字段  : {st.get('fee_raw') or '未知'}")
        else:
            print("     基金状态    : 未抓到（限购检测将不可用）")

        # 行业配置
        alloc = fetcher.fetch_industry_allocation(code)
        if alloc:
            top = sorted(alloc, key=lambda x: x["weight"], reverse=True)[:3]
            txt = "、".join(f"{a['industry']} {a['weight']}%" for a in top)
            print(f"     行业配置    : 前 3 —— {txt}")
        else:
            print("     行业配置    : 未抓到")

    # ---------------------------------------------------------------- 板块
    print("\n【3】板块数据")
    # force=True：本脚本的结果要写库留档，必须是最新的，不能复用页面展示用的缓存。
    boards, used_source = fetcher.fetch_boards(force=True)
    if boards:
        print(f"  数据源  : {used_source}")
        print(f"  板块数  : {len(boards)}")
        # 同花顺板块的净流入字段单位已经是「亿元」，直接使用，不要再除 1e8
        ranked = sorted(
            boards, key=lambda b: b.get("main_net_inflow") or 0, reverse=True
        )
        for title, group in (("资金流入前 3", ranked[:3]), ("资金流出前 3", ranked[-3:])):
            print(f"  {title}:")
            for b in group:
                inflow = b.get("main_net_inflow")
                chg = b.get("change_pct")
                print(f"    {b['board_name']:<10} "
                      f"净流入 {inflow if inflow is not None else 0:>8.2f} 亿  "
                      f"涨跌 {chg if chg is not None else 0:>6.2f}%")
        record_health(
            conn, used_source, "board", "ok", len(boards), f"板块 {len(boards)} 个"
        )
    else:
        print("  板块抓取失败：所有候选源均不可用")
        record_health(conn, "all", "board", "failed", 0, "板块所有源均失败")
        problem_found = True

    # ------------------------------------------------------------ 异常清单
    print("\n【4】异常清单（待 AI 定性）")
    if all_anomalies:
        ts = datetime.now().strftime("%Y-%m-%d")
        path = ANOMALY_DIR / f"anomalies-{ts}.json"
        fetcher.anomalies_to_json(all_anomalies, path)
        print(f"  共 {len(all_anomalies)} 项，已写入 {path}")
        for a in all_anomalies[:5]:
            print(f"    [{a['fund_code']}] {a['date']} {a['type']}: {a['detail']}")
        print("\n  处理约定：脚本只报异常不猜原因。请判断后输出 verdicts，")
        print("  action 取 accept(真实事件) / reject(脏数据) / rescrape(换源重抓)。")
    else:
        print("  无异常")

    conn.close()

    print("\n" + line("="))
    if problem_found:
        print("完成，但存在问题（数据源失效或数据冲突），需要 AI 介入处理")
        print(line("="))
        return 1
    print("完成，全部正常")
    print(line("="))
    return 0


if __name__ == "__main__":
    sys.exit(main())
