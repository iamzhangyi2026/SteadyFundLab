# -*- coding: utf-8 -*-
"""B 区基金分析：基于净值序列算指标 + 生成分析文字。

AI 权限：AI 可读真实持仓并**生成分析**（供人参考），
仍不可写入 / 交易 B 区。分析只讲事实与判断，不给买卖建议。

实现说明：本模块产出的「深度分析」是**用代码规则算出来的**
（SOP Step 7 六段式），**不调用外部大模型** —— 所以界面文案叫「深度分析」，
不挂「AI」名头（名实要相符）。
"""
from __future__ import annotations

import statistics
import time

from backend.real import nav as real_nav
from ..engine.deep_sections import build_sections  # 六段式公共实现（A/B 区共用）


def _fmt_money(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


def _fmt_num(v):
    try:
        f = float(v)
        return f"{f:.0f}" if f == int(f) else f"{f:,.2f}"
    except Exception:
        return str(v)


def _vals(series: list) -> list:
    """计算口径：优先累计净值（已把分红加回），否则单位净值。"""
    return [r.get("acc_nav") or r.get("nav") for r in series]


def _ret(vals: list, days: int):
    if len(vals) < 2:
        return None
    n = min(days, len(vals) - 1)
    a, b = vals[-1 - n], vals[-1]
    if not a:
        return None
    return round((b - a) / a * 100, 2)


def _drawdown(vals: list):
    """返回 (最大回撤%, 高点索引, 低点索引)。"""
    peak_i = 0
    peak = vals[0]
    mdd = 0.0
    best_peak_i = 0
    trough_i = 0
    for i, v in enumerate(vals):
        if v > peak:
            peak, peak_i = v, i
        if peak:
            dd = v / peak - 1.0
            if dd < mdd:
                mdd, trough_i, best_peak_i = dd, i, peak_i
    return round(mdd * 100, 2), best_peak_i, trough_i


def _vol(vals: list):
    rets = [(vals[i] / vals[i - 1] - 1.0) for i in range(1, len(vals)) if vals[i - 1]]
    rets = rets[-250:]
    if len(rets) < 5:
        return None
    return round(statistics.pstdev(rets) * (250 ** 0.5) * 100, 2)


def _recovery(vals: list, peak_i: int, trough_i: int):
    """回撤修复：低点之后多久回到前高。返回 (是否已修复, 天数)。"""
    if trough_i <= peak_i:
        return True, 0
    peak_v = vals[peak_i]
    for j in range(trough_i + 1, len(vals)):
        if vals[j] >= peak_v:
            return True, j - trough_i
    return False, len(vals) - 1 - trough_i


_CACHE: dict = {}
_TTL = 300.0  # 秒：同一只基金 5 分钟内复用净值 / 指标，少打 akshare


def _series_metrics(code: str):
    now = time.time()
    hit = _CACHE.get(code)
    if hit and now - hit["t"] < _TTL:
        return hit["series"], hit["metrics"]
    series = real_nav.fetch_series(code, with_acc=True)
    if not series:
        return [], {}
    vals = _vals(series)
    dates = [r["date"] for r in series]
    mdd, pi, ti = _drawdown(vals)
    recovered, rec_days = _recovery(vals, pi, ti)
    latest = series[-1]
    m = {
        "nav": latest["nav"],
        "nav_date": latest["date"],
        "change_pct": latest["growth"],
        "ret_1m": _ret(vals, 20),
        "ret_3m": _ret(vals, 60),
        "ret_6m": _ret(vals, 120),
        "ret_1y": _ret(vals, 250),
        "max_drawdown": mdd,
        "vol": _vol(vals),
        "history_days": len(series),
        "dd_peak_date": dates[pi],
        "dd_trough_date": dates[ti],
        "dd_decline_days": ti - pi,
        "dd_recovered": recovered,
        "dd_recovery_days": rec_days,
    }
    _CACHE[code] = {"t": now, "series": series, "metrics": m}
    return series, m


def analyze(code: str, name: str = "", holding: dict | None = None) -> dict:
    series, m = _series_metrics(code)
    if not series:
        return {"fund_code": code, "ok": False, "error": "抓不到净值数据",
                "series": [], "metrics": {}, "analysis": "", "sections": {}}
    vals = _vals(series)
    pct = _percentile(vals)
    if pct is not None:
        m["percentile"] = pct
    # 公共数据（新闻资讯 / 基金档案 / 费率规则）—— **只读复用**，见 real/shared.py。
    # 边界：账本隔离、数据共用；新闻与规则是真实世界资料，A 区抓一次、两区共用。
    from backend.real import shared

    news = shared.read_news(code)
    meta = shared.read_fund_meta(code)
    tiers = shared.redeem_tiers()
    dividends = shared.read_dividends(code)
    return {"fund_code": code, "ok": True, "series": series[-300:], "metrics": m,
            "analysis": _narrative(name or code, m, holding),
            "sections": _sections(name or code, m, holding, news=news, meta=meta,
                                  tiers=tiers, dividends=dividends)}


def _narrative(name: str, m: dict, holding: dict | None) -> str:
    out = []

    seg = []
    for lbl, k in (("近1月", "ret_1m"), ("近3月", "ret_3m"), ("近6月", "ret_6m"), ("近1年", "ret_1y")):
        v = m.get(k)
        if v is not None:
            seg.append(f"{lbl}{'涨' if v >= 0 else '跌'}{abs(v):.2f}%")
    if seg:
        r1y = m.get("ret_1y")
        trend = ""
        if r1y is not None:
            if r1y >= 10:
                trend = "，长期趋势向上。"
            elif r1y >= 0:
                trend = "，长期趋势偏平。"
            elif r1y >= -10:
                trend = "，长期趋势偏弱。"
            else:
                trend = "，长期表现较弱。"
        out.append("走势：" + "、".join(seg) + trend)

    mdd, vol = m.get("max_drawdown"), m.get("vol")
    if mdd is not None:
        risk = "偏高" if mdd <= -30 else ("中等" if mdd <= -15 else "较低")
        t = f"风险：历史最大回撤 {abs(mdd):.2f}%，回撤风险{risk}。"
        if vol is not None:
            t += f"年化波动率 {vol:.2f}%。"
        out.append(t)

    if m.get("dd_peak_date") and m.get("dd_trough_date"):
        head = (f"回撤修复：最深一次从 {m['dd_peak_date']} 高点跌到 {m['dd_trough_date']} 低点，"
                f"下跌 {m.get('dd_decline_days')} 个交易日，")
        if m.get("dd_recovered"):
            out.append(head + f"用 {m.get('dd_recovery_days')} 个交易日回到前高。")
        else:
            out.append(head + f"至今 {m.get('dd_recovery_days')} 个交易日仍未回到前高。")

    if holding and holding.get("profit") is not None:
        prof, pr = holding.get("profit"), holding.get("profit_rate")
        tail = f"（{pr * 100:.2f}%）" if pr is not None else ""
        out.append(
            f"持仓：你持有 {_fmt_num(holding.get('shares'))} 份，"
            f"成本 ¥{_fmt_money(holding.get('cost_amount'))}，"
            f"市值 ¥{_fmt_money(holding.get('market_amount'))}，"
            f"当前{'浮盈' if prof >= 0 else '浮亏'} ¥{_fmt_money(abs(prof))}{tail}。"
        )

    return "\n".join(out) if out else "暂无足够数据生成分析。"


def _sections(name: str, m: dict, holding: dict | None,
              news: list | None = None, meta: dict | None = None, tiers: list | None = None,
              dividends: list | None = None) -> dict:
    """六段式深度分析 —— 薄封装，转调公共模块 `backend/engine/deep_sections.py`。

    真实仓位（本模块）与模拟持仓（A 区）共用同一份实现，避免同一逻辑写两遍、改一处漏一处。
    """
    return build_sections(name, m, holding, news=news, meta=meta,
                          tiers=tiers, dividends=dividends)


def _percentile(vals: list):
    """累计净值的自身历史分位 —— 转调公共模块，A/B 区同一口径。"""
    from ..engine.deep_sections import percentile

    return percentile(vals)
