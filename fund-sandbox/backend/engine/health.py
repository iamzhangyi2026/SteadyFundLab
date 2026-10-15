# -*- coding: utf-8 -*-
"""
fund-sandbox 健康度引擎（脱敏版）
- 数据源健康检查
- 异常检测
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from ..config import get
from ..db.schema import get_connection, record_health
from .screen_rules import max_drawdown as _max_drawdown, percentile as _percentile


def check_nav_health(fund_code: str) -> dict[str, Any]:
    """
    检查单只基金的健康度
    :param fund_code: 基金代码
    :return: 健康度报告
    """
    conn = get_connection()
    
    try:
        # 最新净值日期
        cursor = conn.execute(
            "SELECT nav_date, nav, acc_nav FROM nav_daily WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1",
            (fund_code,)
        )
        latest = cursor.fetchone()
        
        if not latest:
            return {
                "fund_code": fund_code,
                "status": "no_data",
                "message": "暂无净值数据",
            }
        
        # 检查数据是否连续（最近7天）
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        
        cursor = conn.execute(
            """SELECT COUNT(*) as cnt FROM nav_daily 
               WHERE fund_code = ? AND nav_date >= ?""",
            (fund_code, seven_days_ago)
        )
        recent_count = cursor.fetchone()["cnt"]
        
        status = "ok" if recent_count >= 5 else "missing"
        
        return {
            "fund_code": fund_code,
            "status": status,
            "latest_date": latest["nav_date"],
            "latest_nav": latest["nav"],
            "recent_records": recent_count,
            "message": f"最新净值 {latest['nav_date']}: {latest['nav']}",
        }
    finally:
        conn.close()


def check_all_health() -> list[dict]:
    """检查所有基金的健康度"""
    conn = get_connection()
    
    try:
        cursor = conn.execute("SELECT code FROM funds")
        funds = [row["code"] for row in cursor.fetchall()]
        
        results = []
        for code in funds:
            health = check_nav_health(code)
            results.append(health)
        
        return results
    finally:
        conn.close()


def detect_anomalies(fund_code: str, window: int = 5) -> list[dict]:
    """
    检测净值异常
    :param fund_code: 基金代码
    :param window: 检查窗口
    :return: 异常列表
    """
    conn = get_connection()
    anomalies = []
    
    try:
        cursor = conn.execute(
            """SELECT nav_date, acc_nav FROM nav_daily 
               WHERE fund_code = ? ORDER BY nav_date DESC LIMIT ?""",
            (fund_code, window + 1)
        )
        navs = [dict(r) for r in cursor.fetchall()]
        
        if len(navs) < window + 1:
            return anomalies
        
        for i in range(len(navs) - 1):
            curr = navs[i]
            prev = navs[i + 1]
            
            if prev["acc_nav"] and prev["acc_nav"] > 0:
                change_pct = (curr["acc_nav"] - prev["acc_nav"]) / prev["acc_nav"] * 100
                
                # 单日涨跌超过5%视为异常
                if abs(change_pct) > 5:
                    anomalies.append({
                        "date": curr["nav_date"],
                        "type": "big_move",
                        "change_pct": round(change_pct, 2),
                        "detail": f"单日涨跌幅 {change_pct:.2f}%",
                    })
        
        return anomalies
    finally:
        conn.close()


# =============================================================================
# 持仓六维健康度
# -----------------------------------------------------------------------------
# 六维与权重来自 config/config.toml [health]（合计 100）：
#     趋势 25 / 持续性 20 / 相对强度 15 / 回撤 15 / 估值 10 / 舆情 15
# 四档 level：healthy / watch / alert / danger
# 四档 action：hold / watch / reduce / clear，且受**持续性硬门槛**约束：
#     分数低还要连续 N 天才允许升档，单日异常最多到 watch —— 防止"一跌就卖"。
#
# 口径统一：收益率 / 趋势 / 分位 / 回撤一律用**累计净值 acc_nav**；
# 单位净值 nav 只用于市值/份额/浮盈，不参与本模块任何计算。
#
# 为什么缺失维度要「剔除后归一化」而不是记 0 分：
#     某维没数据（如无新闻）若记 0，会系统性拉低总分，把正常持仓误判成 danger
#     进而触发减仓/清仓 —— 那是拿算法缺陷去惩罚持仓。正确做法是把它从权重池剔除。
# =============================================================================

DIMENSIONS = ("trend", "persist", "relative", "drawdown", "valuation", "news")


def _nav_series(conn: sqlite3.Connection, fund_code: str, limit: int = 900):
    """取该基金按日期**升序**的累计净值序列。

    说明：这里取 `COALESCE(acc_nav, nav)` —— 正常持仓都有 acc_nav，个别数据缺失
    才回退 nav，避免把一个数字也没有的行当 0 参与计算（那会算出荒谬的收益率）。
    """
    rows = conn.execute(
        "SELECT nav_date, COALESCE(acc_nav, nav) AS v FROM nav_daily "
        "WHERE fund_code=? AND COALESCE(acc_nav, nav) > 0 "
        "ORDER BY nav_date ASC LIMIT ?",
        (fund_code, limit),
    ).fetchall()
    dates = [r["nav_date"] for r in rows]
    vals = [float(r["v"]) for r in rows]
    return dates, vals


def _ret_over_days(dates: list[str], vals: list[float], days: int) -> float | None:
    """按**自然日**计算区间收益率（%）。窗口不足返回 None，绝不臆补数据。

    为什么不用 screen_rules.ret_1y：它取 series[-69]（约 3 个月）却自称「约 250 个
    交易日」，窗口语义自相矛盾。这里按日期精确取，口径明确可复核。
    """
    if len(vals) < 2:
        return None
    try:
        end = date.fromisoformat(dates[-1])
    except Exception:
        return None
    target = end - timedelta(days=days)
    for i, d in enumerate(dates):
        try:
            if date.fromisoformat(d) >= target:
                v0 = vals[i]
                return (vals[-1] / v0 - 1) * 100 if v0 > 0 else None
        except Exception:
            continue
    return None


def _weighted(dim_parts: list[tuple[float, float, str]]):
    """把若干 (权重, 得分, 理由) 子项合成本维 resonate 0~100 分。

    子项数据不全时**只对可用子项归一**，避免用看不见的 0 分惩罚基金。
    返回 (score, reason)；全部子项缺失返回 (None, reason)。
    """
    usable = [(w, g, r) for w, g, r in dim_parts if g is not None]
    if not usable:
        return None, "数据不足"
    tw = sum(w for w, _, _ in usable)
    score = sum(g for _, g, _ in usable) / tw * 100
    reason = "；".join(f"{r}({g:.0f}/{w:.0f})" for w, g, r in usable)
    return round(score, 1), reason


# ---------------------------------------------------------------- 各维度评分


def _score_trend(dates, vals):
    """趋势维（0~100）：能否站在均线上 + 多头排列 + 区间收益为正。"""
    parts: list[tuple[float, float, str]] = []
    last = vals[-1] if vals else None

    if len(vals) >= 20:
        ma20 = sum(vals[-20:]) / 20
        got = 40.0 if last >= ma20 else 12.0
        parts.append((40.0, got, f"净值{'站上' if last >= ma20 else '跌破'}MA20"))
    if len(vals) >= 60:
        ma20 = sum(vals[-20:]) / 20
        ma60 = sum(vals[-60:]) / 60
        got = 30.0 if ma20 >= ma60 else 8.0
        parts.append((30.0, got, f"MA20{'≥' if ma20 >= ma60 else '<'}MA60（{'多头' if ma20 >= ma60 else '空头'}排列）"))
    r1y = _ret_over_days(dates, vals, 365)
    if r1y is not None:
        got = 30.0 if r1y > 0 else (14.0 if r1y > -10 else 5.0)
        parts.append((30.0, got, f"近1年 {r1y:+.2f}%"))

    score, reason = _weighted(parts)
    return score, reason


def _persist_days(conn: sqlite3.Connection, fund_code: str, as_of: str) -> int:
    """连续预警天数：从 as_of **之前**的历史逐日回溯，遇到好转的一天即归零重计。

    这是「持续性」维的输入，也是四档处置的硬门槛依据。
    """
    try:
        rows = conn.execute(
            "SELECT as_of, level FROM position_health "
            "WHERE fund_code=? AND as_of < ? ORDER BY as_of DESC",
            (fund_code, as_of),
        ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        if r["level"] in ("alert", "danger"):
            n += 1
        else:
            break
    return n


def _score_persist(conn, fund_code, as_of):
    """持续性维（0~100）：连续预警越久分越低，到 config 的天数上限扣到 0。"""
    n = _persist_days(conn, fund_code, as_of)
    try:
        full = float(get("health", "persist_full_penalty", 5)) or 5.0
    except Exception:
        full = 5.0
    score = max(0.0, 100.0 - n * (100.0 / full))
    reason = f"连续预警 {n} 天（连续满 {full:.0f} 天扣至 0）"
    if n == 0:
        reason += "；无历史预警，按 0 天计（首次评估）"
    return round(score, 1), reason


def _score_relative(conn, dates, vals):
    """相对强度维（0~100）：本基金近1年收益在**全池同口径收益**中的百分位。

    库里没有 benchmark 表，所以用全市场横向对比代替对基准的超额收益——
    跑赢的基金占比越高，相对强度分越高。
    """
    mine = _ret_over_days(dates, vals, 365)
    if mine is None:
        return None, "相对强度：本基金近1年数据不足"

    peers: list[float] = []
    codes = [r["fund_code"] for r in conn.execute(
        "SELECT DISTINCT fund_code FROM nav_daily"
    ).fetchall()]
    for code in codes:
        d2, v2 = _nav_series(conn, code, limit=400)
        r = _ret_over_days(d2, v2, 365)
        if r is not None:
            peers.append(r)
    if len(peers) < 5:
        return None, "相对强度：全池可比对样本不足（<5 只）"

    below = sum(1 for x in peers if x < mine)
    pct = below / len(peers) * 100
    reason = f"近1年 {mine:+.2f}%，跑赢全池 {pct:.0f}%（样本 {len(peers)} 只）"
    return round(pct, 1), reason


def _score_drawdown(vals):
    """回撤维（0~100）：最大回撤越大分越低。50% 回撤 = 0 分（对齐筛选规则 max_dd<50%）。"""
    dd = _max_drawdown(vals)
    if dd is None:
        return None, "回撤：数据不足"
    abs_dd = abs(dd)
    score = max(0.0, min(100.0, 100.0 * (1 - abs_dd / 50.0)))
    return round(score, 1), f"历史最大回撤 {abs_dd:.2f}%（50% 记 0 分）"


def _score_valuation(vals):
    """估值维（0~100）：当前累计净值在自身历史中的百分位越低越「洼地」，分越高。"""
    p = _percentile(vals)
    if p is None:
        return None, "估值：历史序列不足，无法算分位"
    score = 100.0 - p
    return round(score, 1), f"净值历史分位 {p:.1f}%（越低越洼地）"


def _score_news(conn, fund_code, as_of, window_days: int = 30):
    """舆情维（0~100）：近 N 天相关新闻的正/负面加权；**无新闻不惩罚**（没有消息≠利空）。"""
    # 只按 related_fund 精确匹配，**不能用 hit_holdings=1 兜底**：
    # hit_holdings 仅表示"这条新闻命中了某个持仓"，并不指向具体哪只基金，
    # 用它匹配会把别的基金的新闻算到本基金头上（实测会让所有持仓舆情分都变成 100，
    # 维度彻底失去区分度）。宁可 keywords 命中不到返回 None 剔除该维，也不张冠李戴。
    try:
        rows = conn.execute(
            "SELECT impact_dir, major FROM news_triage "
            "WHERE related_fund = ? "
            "AND date(COALESCE(first_pub_time, processed_at)) >= date(?, ?)",
            (fund_code, as_of, f"-{int(window_days)} days"),
        ).fetchall()
    except sqlite3.Error:
        return None, "舆情：数据表不可用"

    if not rows:
        return None, f"舆情：近 {window_days} 天无相关新闻，不参与评分"

    score = 70.0
    pos = neg = 0
    for r in rows:
        impact = (r["impact_dir"] or "").lower()
        weight = 15.0 if r["major"] else 10.0
        if impact == "negative":
            neg += 1
            score -= weight + (8.0 if r["major"] else 0.0)
        elif impact == "positive":
            pos += 1
            score += weight
    score = max(0.0, min(100.0, score))
    return round(score, 1), f"近{window_days}天相关新闻 {len(rows)} 条（正面{pos}/负面{neg}）"


# ---------------------------------------------------------------------- 汇总


def _health_config() -> dict:
    """读 config [health]：权重与分级阈值。缺失给默认值，不中断评分。"""
    return {
        "weights": {
            "trend": float(get("health", "weight_trend", 25.0)),
            "persist": float(get("health", "weight_persist", 20.0)),
            "relative": float(get("health", "weight_relative", 15.0)),
            "drawdown": float(get("health", "weight_drawdown", 15.0)),
            "valuation": float(get("health", "weight_valuation", 10.0)),
            "news": float(get("health", "weight_news", 15.0)),
        },
        "healthy": float(get("health", "level_healthy", 70.0)),
        "watch": float(get("health", "level_watch", 55.0)),
        "alert": float(get("health", "level_alert", 35.0)),
        "reduce_days": int(get("health", "persist_days_for_reduce", 2)),
        "clear_days": int(get("health", "persist_days_for_clear", 3)),
    }


def _decide(score: float, persist_days: int, cfg: dict):
    """四档 level + 四档 action。action 受持续性硬门槛约束，防止单日异常就减/清仓。"""
    if score >= cfg["healthy"]:
        level = "healthy"
    elif score >= cfg["watch"]:
        level = "watch"
    elif score >= cfg["alert"]:
        level = "alert"
    else:
        level = "danger"

    if level == "healthy":
        action = "hold"
    elif level == "watch":
        action = "watch"
    elif level == "alert":
        action = "reduce" if persist_days >= cfg["reduce_days"] else "watch"
    else:  # danger
        if persist_days >= cfg["clear_days"]:
            action = "clear"
        elif persist_days >= cfg["reduce_days"]:
            action = "reduce"
        else:
            action = "watch"
    return level, action


def evaluate_position_health(fund_code: str, as_of: str | None = None,
                             conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """评估单只基金的六维健康度并**落库 position_health**（按 fund_code+as_of 幂等覆盖）。"""
    own = conn is None
    conn = conn or get_connection()
    try:
        as_of = as_of or date.today().isoformat()
        cfg = _health_config()
        dates, vals = _nav_series(conn, fund_code)

        name_row = conn.execute(
            "SELECT name FROM funds WHERE code=?", (fund_code,)
        ).fetchone()
        fund_name = name_row["name"] if name_row else None

        if len(vals) < 2:
            return {"fund_code": fund_code, "fund_name": fund_name, "as_of": as_of,
                    "ok": False, "reason": "净值数据不足，无法评估六维"}

        dims = {
            "trend": _score_trend(dates, vals),
            "persist": _score_persist(conn, fund_code, as_of),
            "relative": _score_relative(conn, dates, vals),
            "drawdown": _score_drawdown(vals),
            "valuation": _score_valuation(vals),
            "news": _score_news(conn, fund_code, as_of),
        }

        # 缺失维度从权重池剔除后归一化 —— 不用看不见的 0 分惩罚持仓
        usable = {k: v[0] for k, v in dims.items() if v[0] is not None}
        if not usable:
            return {"fund_code": fund_code, "fund_name": fund_name, "as_of": as_of,
                    "ok": False, "reason": "六个维度均无可用数据"}
        tw = sum(cfg["weights"][k] for k in usable)
        score = sum(usable[k] * cfg["weights"][k] for k in usable) / tw

        persist_days = _persist_days(conn, fund_code, as_of)
        level, action = _decide(score, persist_days, cfg)
        ratio = {"hold": 0.0, "watch": 0.0, "reduce": 0.5, "clear": 1.0}[action]
        missing = [k for k in DIMENSIONS if dims[k][0] is None]

        reasons = {
            "dimensions": {k: {"score": dims[k][0], "reason": dims[k][1]} for k in DIMENSIONS},
            "excluded": missing,
            "weight_used": round(tw, 1),
            "action_reason": (
                f"{level} 档，连续预警 {persist_days} 天"
                + ("（未达硬门槛，仅升级到 watch）" if level in ("alert", "danger") and action == "watch" else "")
            ),
        }

        conn.execute(
            "INSERT OR REPLACE INTO position_health "
            "(fund_code, fund_name, as_of, score, level, action, suggested_ratio, "
            " persist_days, d_trend, d_persist, d_relative, d_drawdown, d_valuation, "
            " d_news, reasons, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fund_code, fund_name, as_of, round(score, 2), level, action, ratio,
                persist_days,
                dims["trend"][0], dims["persist"][0], dims["relative"][0],
                dims["drawdown"][0], dims["valuation"][0], dims["news"][0],
                json.dumps(reasons, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()

        return {
            "fund_code": fund_code, "fund_name": fund_name, "as_of": as_of,
            "ok": True, "score": round(score, 2), "level": level, "action": action,
            "suggested_ratio": ratio, "persist_days": persist_days,
            "d_trend": dims["trend"][0], "d_persist": dims["persist"][0],
            "d_relative": dims["relative"][0], "d_drawdown": dims["drawdown"][0],
            "d_valuation": dims["valuation"][0], "d_news": dims["news"][0],
            "excluded_dimensions": missing,
            "reasons": reasons,
        }
    finally:
        if own:
            conn.close()


def assess_positions(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """对所有**当前持仓**（lots 尚有份额）跑一遍六维健康度。"""
    own = conn is None
    conn = conn or get_connection()
    try:
        as_of = date.today().isoformat()
        codes = [r["fund_code"] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
        ).fetchall()]
        items = []
        for code in codes:
            try:
                items.append(evaluate_position_health(code, as_of=as_of, conn=conn))
            except Exception as e:  # 单只异常不能连累整轮
                items.append({"fund_code": code, "ok": False, "reason": f"评估异常：{e}"})
        ok_items = [i for i in items if i.get("ok")]
        return {
            "total": len(items), "ok": len(ok_items), "missing": len(items) - len(ok_items),
            "items": items, "as_of": as_of,
        }
    finally:
        if own:
            conn.close()


def run_health_check() -> dict[str, Any]:
    """运行完整健康检查"""
    results = check_all_health()
    
    ok_count = sum(1 for r in results if r["status"] == "ok")
    missing_count = sum(1 for r in results if r["status"] == "missing")
    
    return {
        "total": len(results),
        "ok": ok_count,
        "missing": missing_count,
        "details": results,
    }


if __name__ == "__main__":
    result = run_health_check()
    print(f"健康检查: {result}")
