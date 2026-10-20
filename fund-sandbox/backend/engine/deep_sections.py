# -*- coding: utf-8 -*-
"""**六段式深度分析**（SOP Step 7）—— 公共模块，A 区 / B 区共用。

为什么单独抽出来：真实仓位（B 区）与模拟持仓（A 区）都要「点击基金 → 弹出深度分析」，
且格式必须**完全一致**。若两边各写一份，就会像曾经的行情抓取一样出现「同一逻辑两处实现」，
改一处漏一处。本模块只做**纯函数计算**：调用方负责取数（净值指标 / 持仓 / 新闻 / 档案 /
费率阶梯 / 分红公告），这里只负责按 SOP 格式组织成六段。

边界：不碰任何数据库、不抓网络 —— 取数由各区的调用方做。
      B 区（真实仓位）调用时，取数走 `backend/real/shared.py`（公共数据只读）。

准则：**禁止简写** —— 不许只给「买 / 不买」一句话结论，
必须给完整六段：结论 / 数据 / 催化 / 分化 / 信号 / 建议。

界面准则：**UI 不做过度解释，只标识功能** ——
本模块产出的字符串**全部会直接显示给用户**，所以只写功能标识与结论，
不写实现原理 / 口径来源 / 内部黑话（那类说明写代码注释，见各处 docstring）。
"""
from __future__ import annotations


def _fmt_money(v):
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v)


# 申购状态：库里存英文枚举，界面用中文。A 区（api/main.py）、B 区（real/shared.py）
# 与深度分析共用同一份映射，避免三处各写一遍、改一处漏两处。
PS_ZH = {"open": "开放申购", "limited": "限大额", "closed": "暂停申购", "unknown": ""}


def _pct_s(v):
    """百分比格式化：None → '—'。"""
    return f"{v:.2f}%" if v is not None else "—"


def percentile(vals: list):
    """当前净值在自身历史序列中的分位（%）—— 「位置」的代理指标。

    口径：基金没有 PE/PB，取自身历史分位，只代表净值位置高低；
    债基这类长期单边上行的品种分位天然偏高，解读时不能当股票估值用。
    调用方也可自行算好后塞进 `m["percentile"]`。
    """
    if len(vals) < 20:
        return None
    latest = vals[-1]
    below = sum(1 for v in vals if v <= latest)
    return round(below / len(vals) * 100, 2)


def _verdict(zone: str, trend: str) -> str:
    """「位置 × 趋势」→ 定性表（先结论后数据）。"""
    table = {
        ("低估区", "趋势向上"): "可关注（低估 + 趋势确认）",
        ("低估区", "趋势偏平"): "观察（低估但拐点未确认）",
        ("低估区", "趋势偏弱"): "左侧未确认（低估但仍在走弱）",
        ("低估区", "趋势向下"): "回避（低估陷阱风险）",
        ("中位区", "趋势向上"): "持有（中位 + 趋势向上）",
        ("中位区", "趋势偏平"): "持有观察",
        ("中位区", "趋势偏弱"): "减仓观察",
        ("中位区", "趋势向下"): "回避",
        ("高估区", "趋势向上"): "谨慎持有（位置偏高）",
        ("高估区", "趋势偏平"): "谨慎（位置偏高 + 动能不足）",
        ("高估区", "趋势偏弱"): "回避（高估 + 转弱）",
        ("高估区", "趋势向下"): "回避（高估 + 下行）",
    }
    return table.get((zone, trend), "位置或趋势不明，暂不判断")


def build_sections(name: str, m: dict, holding: dict | None,
                   news: list | None = None, meta: dict | None = None,
                   tiers: list | None = None, dividends: list | None = None) -> dict:
    """生成六段式深度分析。

    :param name:     基金名称（弹窗标题用）
    :param m:        净值指标 dict —— 需含 nav / nav_date / change_pct / ret_1m / ret_3m /
                     ret_6m / ret_1y / max_drawdown / vol / history_days / percentile /
                     dd_peak_date / dd_trough_date / dd_decline_days / dd_recovered /
                     dd_recovery_days（缺失的键按无数据处理，不抛异常）
    :param holding:  持仓 dict —— {shares, cost_amount, market_amount, profit, profit_rate}；
                     没有持仓可传 None
    :param news:     公告 / 重仓股新闻列表（[{title, pub_time, source, kind}]）
    :param meta:     基金档案 dict（purchase_status / purchase_fee_rate / dividend_method）
    :param tiers:    赎回费阶梯（[{max_holding_days, rate, penalty, note}]）
    :param dividends: 分红公告列表（[{title, pub_time, source, url}]）
    :return: 六段结构化 dict，直接喂给前端 `DeepAnalysis` 组件渲染

    数据纪律：调用方给什么就写什么，**本函数不编造任何数据**；
    某段没有输入就如实写「暂无」，不留占位假内容。
    """
    pct = m.get("percentile")
    r1y, r3m = m.get("ret_1y"), m.get("ret_3m")
    mdd, vol = m.get("max_drawdown"), m.get("vol")

    # ---- 一、核心结论（位置 × 趋势）----
    zone = ("位置未知" if pct is None else
            "高估区" if pct >= 80 else "中位区" if pct >= 40 else "低估区")
    if r1y is None:
        trend, arrow = "趋势未知", ""
    elif r1y >= 10:
        trend, arrow = "趋势向上", "↑"
    elif r1y >= 0:
        trend, arrow = "趋势偏平", "→"
    elif r1y >= -10:
        trend, arrow = "趋势偏弱", "↘"
    else:
        trend, arrow = "趋势向下", "↓"
    verdict = f"{zone} + {trend} = {_verdict(zone, trend)}"

    bits = []
    if r1y is not None:
        bits.append(f"近1年{'涨' if r1y >= 0 else '跌'}{abs(r1y):.2f}%")
    if r3m is not None:
        bits.append(f"近3月{'涨' if r3m >= 0 else '跌'}{abs(r3m):.2f}%")
    if mdd is not None:
        bits.append(f"全历史最大回撤{abs(mdd):.2f}%")
    if pct is not None:
        bits.append(f"累计净值处历史 {pct:.1f}% 分位")
    one_line = (f"{name}：" + "、".join(bits) + "。") if bits else f"{name}：净值数据不足，暂无法定性。"

    # ---- 二、硬指标 ----
    def row(label, value, change, source="净值"):
        return {"indicator": label, "value": value, "change": change, "source": source}

    rows = [
        row("最新单位净值", _fmt_num(m.get("nav")), f"截至 {m.get('nav_date') or '—'}"),
        row("当日涨跌", _pct_s(m.get("change_pct")), "日"),
        row("近1月 / 近3月", f"{_pct_s(m.get('ret_1m'))} / {_pct_s(m.get('ret_3m'))}", "区间收益"),
        row("近6月 / 近1年", f"{_pct_s(m.get('ret_6m'))} / {_pct_s(m.get('ret_1y'))}", "区间收益"),
        row("最大回撤（全历史）", f"{abs(mdd):.2f}%" if mdd is not None else "—",
            f"{m.get('dd_peak_date') or '—'} → {m.get('dd_trough_date') or '—'}"),
        row("年化波动率", _pct_s(vol), "近 250 个交易日"),
        row("历史长度", f"{m.get('history_days') or 0} 个交易日", "样本越长越可信"),
        row("累计净值历史分位", _pct_s(pct), "自身历史位置（非股票估值）"),
    ]
    facts = []
    if mdd is not None:
        risk = "偏高" if mdd <= -30 else ("中等" if mdd <= -15 else "较低")
        facts.append(f"回撤风险{risk}：全历史最深回撤 {abs(mdd):.2f}%"
                     + (f"，年化波动 {vol:.2f}%。" if vol is not None else "。"))
    if m.get("dd_peak_date"):
        facts.append(
            f"最深回撤自 {m['dd_peak_date']} 跌至 {m['dd_trough_date']}，下跌 {m.get('dd_decline_days')} 个交易日，"
            + (f"用 {m.get('dd_recovery_days')} 个交易日修复。"
               if m.get("dd_recovered") else
               f"至今 {m.get('dd_recovery_days')} 个交易日仍未收复。")
        )
    if pct is not None:
        facts.append(
            f"累计净值处自身历史 {pct:.1f}% 分位"
            + ("（接近历史高位区）" if pct >= 80 else "（历史中位区）" if pct >= 40 else "（历史偏低区）")
        )

    # ---- 三、催化事件（公告 / 新闻，按发布时间倒序）----
    catalyst: dict = {"note": "", "items": []}
    if news:
        catalyst["items"] = [
            {
                "event": n.get("title") or "",
                "impact": "",
                "certainty": n.get("pub_time") or "",
                "data": (n.get("source") or "资讯")
                        + ("（基金公告）" if n.get("kind") == "fund_announce" else "（重仓股新闻）"),
            }
            for n in news[:8]
        ]
        catalyst["note"] = "按发布时间倒序。"
    else:
        catalyst["note"] = "暂无相关公告或新闻。"

    # ---- 四、估值与业绩 ----
    # 标的一律用**短标签**：表格第一列若被长文案占满，浏览器会把它压成逐字竖排。
    val_rows = []
    if pct is not None:
        val_rows.append({"target": "本基金", "metric": "累计净值历史分位",
                         "value": f"{pct:.2f}%", "note": "自身历史位置"})
    val_rows.append({"target": "本基金", "metric": "近 1 年收益",
                     "value": _pct_s(r1y), "note": "与自身历史比较"})
    if holding and holding.get("profit_rate") is not None:
        val_rows.append({
            "target": "你的持仓", "metric": "持有收益率",
            "value": f"{holding['profit_rate'] * 100:.2f}%",
            "note": f"成本 ¥{_fmt_money(holding.get('cost_amount'))} → 市值 ¥{_fmt_money(holding.get('market_amount'))}",
        })
    if meta:
        if meta.get("purchase_status"):
            val_rows.append({"target": "基金档案", "metric": "申购状态",
                             "value": str(meta["purchase_status"]),
                             "note": "全市场申购表"})
        if meta.get("purchase_fee_rate") is not None:
            val_rows.append({"target": "基金档案", "metric": "申购费率",
                             "value": f"{float(meta['purchase_fee_rate']) * 100:.2f}%",
                             "note": "原始费率，实收按折扣"})
        if meta.get("dividend_method"):
            val_rows.append({"target": "基金档案", "metric": "分红方式",
                             "value": str(meta["dividend_method"]),
                             "note": "影响成本与份额"})
    if tiers:
        seg = "；".join(
            f"<{t['max_holding_days']}天 {t['rate'] * 100:.2f}%"
            for t in tiers if t.get("max_holding_days") is not None
        )
        if seg:
            val_rows.append({"target": "赎回费", "metric": "真实阶梯",
                             "value": seg, "note": "自然日，命中第一档即止"})
    if dividends:
        latest = dividends[0]
        val_rows.append({
            "target": "分红记录", "metric": f"近 {len(dividends)} 次",
            "value": latest.get("pub_time") or "—",
            "note": "最近一次：" + (latest.get("title") or "")[:30],
        })
    if pct is None:
        val_conclusion = "净值历史不足，暂无法判断位置。"
    else:
        tail = ("净值处历史高位区，需警惕均值回归。" if pct >= 80 else
                "净值处历史偏低区，若趋势同步转正即为左侧特征。" if pct < 40 else
                "净值处历史中位区，方向性不强。")
        val_conclusion = "仅就自身历史而言：" + tail

    # ---- 五、关注信号（阈值型，本地可验证）----
    signals = []
    if pct is not None:
        signals.append({
            "priority": "★★★★★", "signal": "累计净值历史分位",
            "threshold": "突破 90% → 位置偏高、注意回撤；跌回 40% 以下 → 回到偏低区",
            "status": f"当前 {pct:.1f}%",
        })
    if mdd is not None:
        signals.append({
            "priority": "★★★★☆", "signal": "最大回撤是否加深",
            "threshold": f"回撤深于当前 {abs(mdd):.2f}% → 风险等级上调，需重新评估",
            "status": f"当前 {abs(mdd):.2f}%（{'偏高' if mdd <= -30 else '中等' if mdd <= -15 else '较低'}）",
        })
    signals.append({
        "priority": "★★★☆☆", "signal": "近 1 年收益方向",
        "threshold": "由负转正且连续两季为正 → 趋势确认；由正转负 → 趋势走弱",
        "status": f"当前 {_pct_s(r1y)}",
    })

    # ---- 六、投资建议（非推荐）+ 风险点 ----
    research = [
        "先确认本基金在你整体仓位里的定位（底仓 / 卫星），再决定是否调整（仅供判断参考）。",
        "把「历史分位 + 近 1 年方向」合起来看：分位低但趋势仍向下 = 左侧未确认；分位高且趋势转弱 = 风险更大。",
        "同类对比与产业景气可看「分析」页。",
    ]
    risks = []
    if mdd is not None:
        risks.append(f"回撤风险：全历史最大回撤 {abs(mdd):.2f}%"
                     + (f"，年化波动 {vol:.2f}%。" if vol is not None else "。"))
    if pct is not None and pct >= 80:
        risks.append("位置风险：累计净值处历史高分位，均值回归压力不可忽视。")
    if holding and holding.get("profit") is not None and holding["profit"] < 0:
        risks.append(f"浮亏风险：当前浮亏 ¥{_fmt_money(abs(holding['profit']))}，需复核持有逻辑是否仍成立。")
    risks.append("只看净值与公告，可能漏掉产业景气与同类排名的变化。")

    return {
        "conclusion": {"verdict": verdict, "one_line": one_line, "trend_arrow": arrow},
        "data": {"caption": "净值与持仓", "rows": rows, "facts": facts},
        "catalyst": catalyst,
        "valuation": {"caption": "估值与业绩", "rows": val_rows, "conclusion": val_conclusion},
        "signals": signals,
        "advice": {"caption": "投资建议（非推荐，仅供判断参考）",
                   "research": research, "risks": risks},
        "sources": [f"截至 {m.get('nav_date') or '—'}"],
    }


def _fmt_num(v):
    try:
        f = float(v)
        return f"{f:.0f}" if f == int(f) else f"{f:,.2f}"
    except Exception:
        return str(v)
