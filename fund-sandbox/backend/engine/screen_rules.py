# -*- coding: utf-8 -*-
"""
fund-sandbox 核心算法模块（脱敏版）
- 筛选规则：分位、回撤、收益率计算
- 数据来源：akshare + 东方财富
"""
from __future__ import annotations

import numpy as np
from typing import Optional


MIN_SERIES = 60  # 最小数据长度


def percentile(series: list[float]) -> Optional[float]:
    """
    计算当前值在历史序列中的百分位
    :param series: 历史净值序列（最新值在最后）
    :return: 百分位（0-100），数据不足返回None
    """
    if not series or len(series) < MIN_SERIES:
        return None
    
    current = series[-1]
    less_count = sum(1 for x in series[:-1] if x < current)
    return ((less_count + 1) / len(series)) * 100


def max_drawdown(series: list[float]) -> Optional[float]:
    """
    计算历史最大回撤
    :param series: 历史净值序列
    :return: 最大回撤百分比（负数），数据不足返回None
    """
    if not series or len(series) < MIN_SERIES:
        return None
    
    peak = series[0]
    max_dd = 0.0
    
    for nav in series:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    return -max_dd


def ret_1y(series: list[float]) -> Optional[float]:
    # ⚠️ 首值<=0 直接返回 None：历史净值出现异常 0/负值时不硬算，
    # 否则会算出荒谬的收益率（甚至除零）。宁可标"数据不足"。
    """近1年收益率"""
    if not series or len(series) < 69:  # 约250个交易日
        return None
    return (series[-1] / series[-69] - 1) * 100 if series[-69] > 0 else None


def ret_3m(series: list[float]) -> Optional[float]:
    """近3月收益率"""
    if not series or len(series) < 60:
        return None
    return (series[-1] / series[-60] - 1) * 100 if series[-60] > 0 else None


def ret_1m(series: list[float]) -> Optional[float]:
    """近1月收益率"""
    if not series or len(series) < 20:
        return None
    return (series[-1] / series[-20] - 1) * 100 if series[-20] > 0 else None


def ret_6m(series: list[float]) -> Optional[float]:
    """近6月收益率"""
    if not series or len(series) < 120:
        return None
    return (series[-1] / series[-120] - 1) * 100 if series[-120] > 0 else None


def annual_vol(series: list[float]) -> Optional[float]:
    """年化波动率"""
    if not series or len(series) < MIN_SERIES:
        return None
    
    returns = [(series[i] - series[i-1]) / series[i-1] 
               for i in range(1, len(series)) if series[i-1] > 0]
    
    if not returns:
        return None
    
    vol = float(np.std(returns)) * np.sqrt(250) * 100
    return vol


def calc_stats(nav_series: list[dict]) -> dict:
    """
    计算基金统计数据
    :param nav_series: 净值数据列表 [{"nav_date": "...", "nav": float, "acc_nav": float}, ...]
    :return: 统计指标
    """
    if not nav_series:
        return {}
    
    acc_navs = [s["acc_nav"] for s in nav_series if s.get("acc_nav")]
    
    return {
        "latest_date": nav_series[-1]["nav_date"] if nav_series else None,
        "latest_nav": nav_series[-1]["nav"] if nav_series else None,
        "acc_latest": acc_navs[-1] if acc_navs else None,
        "percentile": percentile(acc_navs) if acc_navs else None,
        "max_drawdown": max_drawdown(acc_navs) if acc_navs else None,
        "ret_1y": ret_1y(acc_navs) if acc_navs else None,
        "ret_3m": ret_3m(acc_navs) if acc_navs else None,
        "ret_1m": ret_1m(acc_navs) if acc_navs else None,
        "ret_6m": ret_6m(acc_navs) if acc_navs else None,
        "annual_vol": annual_vol(acc_navs) if acc_navs else None,
        "data_count": len(acc_navs),
    }


def pass_nav_gate(nav: float) -> bool:
    """
    净值闸门：过滤异常值
    :param nav: 净值
    :return: True表示通过
    """
    if nav is None or nav <= 0:
        return False
    # 正常基金净值范围：0.1 ~ 20.0
    return 0.1 <= nav <= 20.0


def screen_candidates(nav_stats: list[dict], rules: dict) -> list[dict]:
    """
    基金池筛选
    :param nav_stats: 基金统计数据列表
    :param rules: 筛选规则
    :return: 候选基金列表
    """
    candidates = []
    
    for stat in nav_stats:
        fund_code = stat.get("fund_code")
        percentile_val = stat.get("percentile")
        ret_1y_val = stat.get("ret_1y")
        ret_3m_val = stat.get("ret_3m")
        annual_vol_val = stat.get("annual_vol")
        max_dd_val = stat.get("max_drawdown")
        
        if percentile_val is None or ret_1y_val is None:
            continue
        
        # 主轨：分位低 + 趋势向上
        main_pass = (percentile_val <= rules.get("main_track", {}).get("percentile_max", 50) and
                     ret_1y_val >= rules.get("main_track", {}).get("ret_1y_min", 0))
        
        # 备轨：分位高但回调后反弹
        backup_pass = (percentile_val >= rules.get("backup_track", {}).get("percentile_min", 70) and
                       ret_1y_val >= rules.get("backup_track", {}).get("ret_1y_min", 0) and
                       (ret_3m_val is None or ret_3m_val > rules.get("backup_track", {}).get("ret_3m_threshold", -10)))
        
        # 高风险放宽：高波动但回撤可控
        high_vol_relax = False
        if annual_vol_val and max_dd_val:
            high_vol_relax = (annual_vol_val <= rules.get("high_vol_relax", {}).get("annual_vol_max", 35) and
                              max_dd_val >= rules.get("high_vol_relax", {}).get("max_dd_min", -50))
        
        if main_pass or backup_pass or high_vol_relax:
            candidates.append({
                "fund_code": fund_code,
                "percentile": percentile_val,
                "ret_1y": ret_1y_val,
                "ret_3m": ret_3m_val,
                "annual_vol": annual_vol_val,
                "max_dd": max_dd_val,
                "track": "main" if main_pass else ("backup" if backup_pass else "high_vol"),
            })
    
    # 按分位排序
    candidates.sort(key=lambda x: x.get("percentile") or 100)
    return candidates


if __name__ == "__main__":
    # 测试
    test_series = [1.0, 2.0] + [2.0] * 30 + [1.0] + [1.0] * 27 + [2.0] * 5
    print(f"测试序列长度: {len(test_series)}")
    print(f"分位: {percentile(test_series)}")
    print(f"最大回撤: {max_drawdown(test_series)}")


# ===== 候选筛选执行器 =====

WINDOW_DAYS = 250
PCTL_MAX = 50.0        # 历史分位 < 50%
RET_1Y_MIN = 0.0       # 近 1 年收益 > 0
MDD_MAX = 50.0         # 最大回撤 < 50%
MDD_BUY_MAX = 30.0     # 买入前额外 < 30%


def _nav_series(conn, code: str, limit: int = WINDOW_DAYS) -> list[float]:
    """最近 limit 条净值（升序），**优先累计净值**（分红除息会形成假下跌）。"""
    try:
        rows = conn.execute(
            "SELECT COALESCE(acc_nav, nav) AS v FROM nav_daily "
            "WHERE fund_code = ? AND COALESCE(acc_nav, nav) IS NOT NULL "
            "ORDER BY nav_date DESC LIMIT ?", (code, limit)
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [float(r[0]) for r in reversed(rows)]


def share_class_of(conn, code: str) -> tuple[str, str]:
    """QDII 份额币种口径：(kind, note)。'' 表示非 QDII。

    人民币份额净值**含**汇兑损益、美元份额**不含** —— 不标出来会把「汇率驱动」
    误读成「标的趋势」（两种份额近 1 年差 7.43 个百分点）。
    """
    try:
        row = conn.execute("SELECT name, is_qdii FROM funds WHERE code = ?", (str(code),)).fetchone()
    except Exception:  # noqa: BLE001
        return "", ""
    if not row or not row[1]:
        return "", ""
    name = str(row[0] or "")
    if "美元" in name:
        return "usd", "QDII·美元份额：净值以美元计，**不含人民币汇兑损益**（汇率风险自担）"
    if "人民币" in name:
        return "rmb", "QDII·人民币份额：净值**已含汇兑损益**（人民币升值会拖累净值表现）"
    return "qdii", "QDII：净值已含汇兑损益（名称未标明币种，按人民币口径理解）"


def evaluate_fund(conn, code: str) -> dict:
    """对单只基金跑项目筛选规则，返回**事实 + 达标与否**（不推荐买哪只）。"""
    vals = _nav_series(conn, code)
    p = percentile(vals)
    r = ret_1y(vals)
    d = max_drawdown(vals)
    sc_kind, sc_note = share_class_of(conn, code)

    reasons: list[str] = []
    if p is None or r is None or d is None:
        return {
            "code": code, "nav_days": len(vals),
            "percentile": p, "ret_1y": r, "max_dd": d,
            "pass_screen": None, "pass_buy": None,
            "reasons": [f"净值序列仅 {len(vals)} 条（需 ≥{MIN_SERIES}），数据不足"],
            "data_ok": False, "share_class": sc_kind, "currency_note": sc_note,
        }

    ok_p, ok_r, ok_d = p < PCTL_MAX, r > RET_1Y_MIN, abs(d) < MDD_MAX
    pass_screen = ok_p and ok_r and ok_d
    pass_buy = pass_screen and abs(d) < MDD_BUY_MAX

    reasons.append(f"历史分位 {p:.1f}%（{'<50 ✓' if ok_p else '≥50 ✗'}）")
    reasons.append(f"近1年收益 {r:+.2f}%（{'>0 ✓' if ok_r else '≤0 ✗'}）")
    reasons.append(f"最大回撤 {d:.1f}%（{'<50 ✓' if ok_d else '≥50 ✗'}）")
    if pass_screen and not pass_buy:
        reasons.append(f"买入前还要求回撤 <{MDD_BUY_MAX:.0f}%（当前 {abs(d):.1f}%，暂不满足）")

    if sc_note:
        reasons.append(sc_note)
        try:
            from backend.engine import fx

            t30 = fx.usd_cny_trend(days=30)
            if t30.get("ok"):
                reasons.append(f"汇率因子：近 {t30.get('span_days') or 30} 天 {t30['direction']}（USD/CNY {t30['chg_pct']:+.2f}%）")
            if sc_kind in ("rmb", "qdii"):
                t1y = fx.usd_cny_trend(days=365)
                if t1y.get("ok") and t1y.get("chg_pct") is not None and abs(1 + t1y['chg_pct'] / 100) > 1e-6:
                    und = (1 + r / 100.0) / (1 + t1y["chg_pct"] / 100.0) - 1
                    reasons.append(f"收益分解（近1年）：标的本身 {und * 100:+.2f}% ＋ 汇率贡献 {t1y['chg_pct']:+.2f}% ＝ 人民币口径 {r:+.2f}%")
        except Exception:  # noqa: BLE001
            pass

    return {
        "code": code, "nav_days": len(vals),
        "percentile": p, "ret_1y": r, "max_dd": d,
        "pass_screen": pass_screen, "pass_buy": pass_buy,
        "reasons": reasons, "data_ok": True,
        "share_class": sc_kind, "currency_note": sc_note,
    }


def evaluate_many(conn, codes) -> list[dict]:
    """批量评估（**只读本地净值**，抓取由 ensure_nav 在后台做）。"""
    return [evaluate_fund(conn, c) for c in dict.fromkeys(codes)]


def ensure_nav(conn, codes, min_days: int = WINDOW_DAYS) -> dict:
    """确保这些基金有足够净值（不足则抓取）。**只抓传入的**。"""
    from backend.data import fetcher

    stat = {"fetched": 0, "ok": 0, "failed": 0}
    for code in dict.fromkeys(str(c).strip() for c in codes if str(c).strip()):
        try:
            have = conn.execute("SELECT COUNT(*) FROM nav_daily WHERE fund_code = ?", (code,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            have = 0
        if have >= min_days:
            stat["ok"] += 1
            continue
        try:
            recs = fetcher.fetch_nav_em(code)
            if recs:
                fetcher.save_nav(conn, code, recs, "eastmoney")
                stat["fetched"] += 1
                stat["ok"] += 1
            else:
                stat["failed"] += 1
        except Exception:  # noqa: BLE001
            stat["failed"] += 1
    return stat


# ⚠️ ret_1y 口径说明：
# 固定倒第 N 个点的取法不可靠 —— 当序列长度不是 250 时，
# 它取的既不是窗口首值、也不是"近 1 年"，口径会随序列变长越偏越多。
# 本模块 evaluate_fund 传的是 **近 250 条窗口**，"近 1 年"就用**窗口首值**。
def ret_1y(series: list[float]) -> Optional[float]:
    """近 1 年收益率（%）：以**窗口首值**为基准。

    首值 <= 0（异常净值）时返回 None —— 不硬算、不除零。
    """
    if not series or len(series) < MIN_SERIES:
        return None
    first = series[0]
    if first <= 0:
        return None
    return (series[-1] / first - 1) * 100
