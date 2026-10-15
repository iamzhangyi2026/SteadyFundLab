"""定投执行：按计划、固定金额，从可用预算扣除。

定投与灵活加仓的纪律性正好相反
--------------------------------
    定投      金额**固定**（每只基金各自设定），不受分析结论影响。
              分析只决定「投哪些基金」，不决定「投多少钱」。
              这是纪律工具的意义——用固定节奏摊平成本，避免择时。

    灵活加仓  金额**完全由分析决定**，不设固定档位。
              唯一的硬约束是不超过可用预算。

所以本模块不做任何分析判断，只负责「按计划、按纪律、不重复地扣款」。

幂等性
------
同一周期内（周=自然周，月=自然月），每只基金每个频率只投一次。
用 quota_usage 里 purpose='dingtou' 的记录判断本期是否已投，
重复调用不会重复下单。

    计划为空时的兼容
    ----------------
    若 dingtou_plan 表无任何启用计划，则 fallback 到全交易池（pool_funds，
    含配置池种子 + 用户添加），每只基金按固定金额定投，由预算上限约束总规模。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta

from backend.config import get
from backend.engine import budget, calendar, orders

# 定投执行互斥锁。
# 防止「手工立即执行」被连点、或与调度器巡检并发时，
# 两个请求同时通过"本期是否已投"检查而重复建单（check-then-act 竞态）。
_dt_lock = threading.Lock()


def period_start(frequency: str, as_of: date | None = None) -> date:
    """返回该期起始日：周定投=本周一，月定投=本月 1 号。"""
    as_of = as_of or date.today()
    if frequency == "monthly":
        return as_of.replace(day=1)
    return as_of - timedelta(days=as_of.weekday())


def already_invested_this_period(conn: sqlite3.Connection, fund_code: str,
                                 frequency: str, as_of: date | None = None) -> bool:
    """本期该基金是否已投过（按频率判断周期）。"""
    as_of = as_of or date.today()
    ps = period_start(frequency, as_of).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM quota_usage
        WHERE purpose = 'dingtou' AND fund_code = ? AND created_at >= ?
        """,
        (fund_code, ps),
    ).fetchone()
    return (row["c"] if row else 0) > 0


def _deduction_target(plan_weekday, day_of_month, frequency: str,
                      as_of: date, cal) -> "tuple[date | None, date | None]":
    """计算本期定投的实际扣款日（对齐支付宝定投规则）。

    返回 (ref_date, eff_date)：
        ref_date  计划所属周期的锚点日（用于幂等周期判定，归属「原计划那一周/月」）
        eff_date  实际应扣款日（计划日顺延到下一交易日；今天不是这天则跳过）

    规则：
        - 周定投：计划周几限定周一至周五（支付宝不支持周六周日，数据层即使残留 5/6 也忽略）；
                  以「最近一个 <= 今天 的计划周几」为 ref，顺延到下一交易日即为 eff。
        - 月定投：每月 day_of_month 号；今天未到该号则跳过；ref 取本月该号，顺延到下一交易日。
        - 遇非交易日（周末/法定节假日）eff 自动后移；极端长假期最多顺延 14 天防死循环。
        - eff 若落在下一周期（如长假期把周五顺延到下周一），仍归属原周期的 ref，
          与下一期各自的周期互不重叠——「顺延重合仅扣一次」，不会双扣。
    """
    if frequency == "weekly":
        wd = int(plan_weekday or 0)
        if wd >= 5:
            return None, None  # 支付宝定投仅工作日，忽略周六周日
        days_back = (as_of.weekday() - wd) % 7
        ref = as_of - timedelta(days=days_back)
    else:  # monthly
        dom = int(day_of_month or 1)
        if as_of.day < dom:
            return None, None  # 本月还没到扣款日
        ref = as_of.replace(day=dom)

    eff = ref
    guard = 0
    while not cal.is_trading_day(eff) and guard < 14:
        eff = eff + timedelta(days=1)
        guard += 1
    return ref, eff


# =============================================================================
# 计划 CRUD
# =============================================================================

def get_plans(conn: sqlite3.Connection) -> list[dict]:
    """列出全部定投计划（含暂停的）。"""
    rows = conn.execute(
        "SELECT * FROM dingtou_plan ORDER BY enabled DESC, fund_code"
    ).fetchall()
    return [dict(r) for r in rows]


def add_plan(conn: sqlite3.Connection, fund_code: str, amount: float,
             frequency: str = "weekly", weekday: int = 0, day_of_month: int = 1,
             fund_name: str | None = None, enabled: int = 1) -> dict:
    """新增定投计划。同一基金+频率唯一。"""
    if frequency not in ("weekly", "monthly"):
        return {"ok": False, "reason": "frequency 必须是 weekly 或 monthly"}
    if amount <= 0:
        return {"ok": False, "reason": "金额必须大于 0"}
    if not fund_name:
        r = conn.execute("SELECT name FROM funds WHERE code=?", (fund_code,)).fetchone()
        fund_name = r["name"] if r else fund_code
    try:
        cur = conn.execute(
            """
            INSERT INTO dingtou_plan
                (fund_code, fund_name, amount, frequency, weekday, day_of_month,
                 enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fund_code, fund_name, amount, frequency, weekday, day_of_month,
             enabled, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return {"ok": True, "plan_id": cur.lastrowid}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"ok": False, "reason": "该基金已存在同频率计划"}


def update_plan(conn: sqlite3.Connection, plan_id: int, **fields) -> dict:
    """更新计划字段（amount/frequency/weekday/day_of_month/enabled/fund_name）。"""
    allowed = {"amount", "frequency", "weekday", "day_of_month", "enabled", "fund_name"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return {"ok": False, "reason": "无可更新字段"}
    if "frequency" in sets and sets["frequency"] not in ("weekly", "monthly"):
        return {"ok": False, "reason": "frequency 必须是 weekly 或 monthly"}
    if "amount" in sets and sets["amount"] <= 0:
        return {"ok": False, "reason": "金额必须大于 0"}
    placeholders = ", ".join(f"{k}=?" for k in sets)
    cur = conn.execute(
        f"UPDATE dingtou_plan SET {placeholders} WHERE id=?",
        tuple(sets.values()) + (plan_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "reason": "计划不存在"}
    return {"ok": True, "plan_id": plan_id}


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> dict:
    """删除计划。"""
    cur = conn.execute("DELETE FROM dingtou_plan WHERE id=?", (plan_id,))
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "reason": "计划不存在"}
    return {"ok": True, "plan_id": plan_id}


def _resolve_pool(conn: sqlite3.Connection, funds: list[dict] | None):
    """定投池来源：显式传入 > 计划表(启用) > **空**。

    没有任何启用的定投计划时**不做回退**，定投池为空：
    候选池是「值得关注的基金」，**不等于「用户同意定投的基金」**，
    回退到候选池会让自动定投买入用户从未同意定投的基金。

    「定投是唯一自动执行操作」的正确含义是：**定投计划一旦由人/agent 建立，其扣款自动执行** ——
    而不是「没有计划时就把候选池全买一遍」。故无计划时返回空，什么都不做。
    """
    if funds is not None:
        return funds
    rows = conn.execute(
        "SELECT * FROM dingtou_plan WHERE enabled=1"
    ).fetchall()
    return [dict(r) for r in rows]


def execute_dingtou(conn: sqlite3.Connection, funds: list[dict] | None = None,
                    as_of: date | None = None,
                    cal: calendar.TradeCalendar | None = None,
                    reason: str = "定投，按纪律执行（金额固定，不因行情改变）") -> dict:
    """执行本期定投（按计划表或默认池，每只基金固定金额，幂等）。

    返回：
        {"period": 本期起始日, "ok": [...], "skipped": [...]}
    """
    with _dt_lock:
        as_of = as_of or date.today()
        cal = cal or calendar.TradeCalendar()
        cal.load()

        pool = _resolve_pool(conn, funds)
        if not pool:
            return {"period_start": period_start("weekly", as_of).isoformat(),
                    "ok": [], "skipped": [], "budget_after": round(budget.available_budget(conn), 2)}

        ok_list: list[dict] = []
        skip_list: list[dict] = []

        for p in pool:
            code = p.get("fund_code") or p.get("code")
            name = p.get("fund_name") or p.get("name") or code
            freq = p.get("frequency", "weekly")
            amount = p.get("amount") or get("capital", "per_fund_weekly", 25.0)
            plan_id = p.get("id")

            # 对齐支付宝定投规则计算实际扣款日：
            #   周定投限定周一至周五（不支持周末），月定投看每月几号；
            #   扣款日若遇非交易日（周末/法定节假日），自动顺延至下一个交易日；
            #   顺延后归属「原计划所属周期」，避免与下一期重复扣款（顺延重合仅扣一次）。
            ref, eff = _deduction_target(p.get("weekday"), p.get("day_of_month"), freq, as_of, cal)
            if ref is None or as_of != eff:
                continue  # 今天不是该计划的扣款日（含顺延后首个工作日），到那天再执行
            period = period_start(freq, ref).isoformat()

            def _release():
                """释放本期抢占标记，允许后续重试（下单失败/预算不足时调用）。"""
                if plan_id is not None:
                    conn.execute(
                        "UPDATE dingtou_plan SET last_invested_period=NULL WHERE id=?",
                        (plan_id,),
                    )
                    conn.commit()

            # ① 历史兼容：quota_usage 里已记过本期（按原计划所属周期锚点）则跳过
            if already_invested_this_period(conn, code, freq, ref):
                skip_list.append({"fund_code": code, "name": name, "reason": "本期已投"})
                continue

            # ② CAS 原子抢占本期执行权。
            #    SQLite 写操作串行化，并发请求里只有一个能 UPDATE 成功，
            #    彻底杜绝「先查后建」竞态造成的同一期重复扣款。
            if plan_id is not None:
                cur = conn.execute(
                    """
                    UPDATE dingtou_plan SET last_invested_period=?
                    WHERE id=? AND COALESCE(last_invested_period, '') <> ?
                    """,
                    (period, plan_id, period),
                )
                conn.commit()
                if cur.rowcount == 0:
                    skip_list.append({"fund_code": code, "name": name, "reason": "本期已投"})
                    continue

            # ③ 预算检查。不足则释放标记，本期不投（下期可再投）
            avail = budget.available_budget(conn)
            if amount > avail:
                _release()
                skip_list.append({
                    "fund_code": code, "name": name,
                    "reason": f"可用预算 {avail:.2f} 元不足 {amount:.0f} 元，本期跳过",
                })
                continue

            # ④ 建单
            r = orders.submit_order(
                conn,
                fund_code=code,
                side="buy",
                amount=amount,
                reason=reason,
                decision_source="rule",
                cal=cal,
                purpose="dingtou",
            )
            if r.get("ok"):
                ok_list.append({
                    "fund_code": code, "name": name,
                    "order_no": r["order_no"], "amount": amount,
                    "fee": r["fee_purchase"],
                    "timing": r["timing"]["hint"],
                })
            else:
                # 下单失败释放标记，允许修复后重试
                _release()
                skip_list.append({
                    "fund_code": code, "name": name,
                    "reason": r.get("reason", "下单失败"),
                })

        return {
            "period_start": period_start(pool[0].get("frequency", "weekly") if pool else "weekly", as_of).isoformat(),
            "ok": ok_list,
            "skipped": skip_list,
            "budget_after": round(budget.available_budget(conn), 2),
        }


if __name__ == "__main__":
    from backend.db import get_connection, init_db

    init_db()
    conn = get_connection()
    budget.init_capital(conn)
    budget.add_monthly_budget(conn)

    print("=" * 78)
    print("定投执行演示")
    print("=" * 78)
    print(f"\n执行前可用预算：{budget.available_budget(conn):.2f} 元")

    add_plan(conn, "005827", 25.0, "weekly")
    r = execute_dingtou(conn)
    print(f"\n本期（{r['period_start']}）成功 {len(r['ok'])} 笔：")
    for o in r["ok"]:
        print(f"    {o['fund_code']} {o['name']}  订单 {o['order_no']}  {o['amount']:.0f} 元")
    if r["skipped"]:
        print(f"\n  跳过 {len(r['skipped'])} 笔：")
        for s in r["skipped"]:
            print(f"    {s['fund_code']}：{s['reason']}")
    print(f"\n执行后可用预算：{r['budget_after']:.2f} 元")
    print("=" * 78)
    conn.close()


# main.py 路由 /api/dingtou/run 调用 run_dingtou；旧引擎命名为 execute_dingtou
run_dingtou = execute_dingtou
