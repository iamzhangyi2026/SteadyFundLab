"""分红识别与处理。

为什么必须处理分红
------------------
分红会让单位净值除息下调（1.5 分红 0.1 后变 1.4）。引擎如果不识别，
会把它记成单日暴跌 6.7%，污染回撤曲线和波动率，AI 看到假信号还会决策错误。

识别原理
--------
累计净值 acc_nav 把历史分红加回去了，单位净值 nav 没有。
    累计每份分红 D(t) = acc_nav(t) − nav(t)
当 D 在某一天跳增（跳增额 / 前一日净值 ≥ dividend_gap_pct），
说明那天发生了除息，跳增量就是每份分红金额。

两种分红方式
------------
    cash     现金分红：分红金额以「dividend」流水入账，计入已实现盈亏
    reinvest 红利再投：按除息后净值把分红折算成份额，生成一个新批次

注意
----
历史某日的持有份额无法精确还原（lots 只存当前剩余份额），
因此处理分红时用**当前持有份额**近似估算分红总额。
这对于"当前持仓视角"的分红到账是合理的，但不是逐日历史精算。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from backend.config import get
from backend.engine import ledger, orders

CASH = "cash"
REINVEST = "reinvest"


def get_dividend_method(conn: sqlite3.Connection, fund_code: str) -> str:
    """取该基金的分红方式，未设置则返回默认（现金分红）。"""
    row = conn.execute(
        "SELECT dividend_method FROM funds WHERE code=?", (fund_code,)
    ).fetchone()
    m = row["dividend_method"] if row and row["dividend_method"] else None
    return m if m in (CASH, REINVEST) else CASH


def set_dividend_method(conn: sqlite3.Connection, fund_code: str,
                        method: str) -> dict:
    """设置分红方式。"""
    if method not in (CASH, REINVEST):
        return {"ok": False, "reason": f"分红方式必须是 {CASH}（现金分红）或 {REINVEST}（红利再投）"}
    cur = conn.execute(
        "UPDATE funds SET dividend_method=? WHERE code=?", (method, fund_code)
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "reason": "基金不在库中，请先添加该基金"}
    return {"ok": True, "fund_code": fund_code, "dividend_method": method}


def detect_dividends(conn: sqlite3.Connection, fund_code: str,
                     gap_pct: float | None = None) -> list[dict]:
    """从 nav_daily 识别分红事件（不落库，纯计算）。"""
    gap = gap_pct if gap_pct is not None else get("data_quality", "dividend_gap_pct", 3.0)
    rows = conn.execute(
        "SELECT nav_date, nav, acc_nav FROM nav_daily WHERE fund_code=? ORDER BY nav_date ASC",
        (fund_code,),
    ).fetchall()

    events: list[dict] = []
    prev: dict | None = None
    for r in rows:
        nav = r["nav"] or 0.0
        acc = r["acc_nav"] if r["acc_nav"] is not None else nav
        cum = acc - nav                      # 截至当日累计每份分红
        if prev is not None and prev["nav"] > 0:
            delta = cum - prev["cum"]
            if delta > 0:
                pct = delta / prev["nav"] * 100
                if pct >= gap:
                    events.append({
                        "ex_date": r["nav_date"],
                        "dividend_per_share": round(delta, 6),
                        "nav_before": round(prev["nav"], 4),
                        "nav_after": round(nav, 4),
                        "drop_pct": round(pct, 3),
                    })
        prev = {"nav": nav, "cum": cum}
    return events


def _held_shares(conn: sqlite3.Connection, fund_code: str) -> float:
    """当前持有份额（分红金额的分母）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(remaining_shares), 0) AS s FROM lots
        WHERE fund_code=? AND status='active' AND remaining_shares > 0
        """,
        (fund_code,),
    ).fetchone()
    return row["s"] if row else 0.0


def _create_dividend_order(conn: sqlite3.Connection, fund_code: str, ex_date: str,
                           amount: float, nav: float, shares: float) -> int:
    """为红利再投建一条已确认的订单，供 create_lot 关联（lots.order_id 外键非空）。"""
    now = datetime.now().isoformat(timespec="seconds")
    seq = conn.execute(
        "SELECT COUNT(*)+1 AS c FROM orders WHERE fund_code=? AND reason LIKE '红利再投%'",
        (fund_code,),
    ).fetchone()["c"]
    order_no = f"D{ex_date.replace('-', '')}{seq:03d}"
    cur = conn.execute(
        """
        INSERT INTO orders
            (order_no, fund_code, side, amount, submit_date, submit_time,
             nav_date, confirm_date, status, decision_source, reason,
             fee_purchase, deal_nav, deal_shares, deal_amount, created_at)
        VALUES (?, ?, 'buy', ?, ?, '00:00:00', ?, ?, 'confirmed', 'rule', ?,
                0, ?, ?, ?, ?)
        """,
        (order_no, fund_code, amount, ex_date, ex_date, ex_date,
         f"红利再投自动（{ex_date} 除息）", nav, shares, amount, now),
    )
    conn.commit()
    return cur.lastrowid


def process_dividends(conn: sqlite3.Connection, fund_code: str) -> dict:
    """识别分红 → 落库 dividends → 按分红方式处理（幂等，重复调用不重复入账）。"""
    method = get_dividend_method(conn, fund_code)
    events = detect_dividends(conn, fund_code)
    applied: list[dict] = []
    skipped: list[dict] = []

    for e in events:
        exist = conn.execute(
            "SELECT id FROM dividends WHERE fund_code=? AND ex_date=?",
            (fund_code, e["ex_date"]),
        ).fetchone()
        if exist:
            skipped.append({**e, "reason": "该除息日已处理过"})
            continue

        shares = _held_shares(conn, fund_code)
        amount = e["dividend_per_share"] * shares
        reinvest = 1 if method == REINVEST else 0

        conn.execute(
            """
            INSERT INTO dividends
                (fund_code, ex_date, dividend_per_share, nav_before, nav_after,
                 reinvest, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fund_code, e["ex_date"], e["dividend_per_share"], e["nav_before"],
             e["nav_after"], reinvest, "nav_daily_detect",
             datetime.now().isoformat(timespec="seconds")),
        )

        if amount > 0 and reinvest:
            # 红利再投：按除息后净值折算份额，新建批次（成本记为分红金额）
            nav = e["nav_after"] or e["nav_before"] or 0.0
            new_shares = amount / nav if nav > 0 else 0.0
            if new_shares > 0:
                oid = _create_dividend_order(conn, fund_code, e["ex_date"], amount,
                                             nav, new_shares)
                ledger.create_lot(conn, fund_code, oid, e["ex_date"], new_shares,
                                  nav, amount)
        elif amount > 0:
            # 现金分红：分红到账入账
            try:
                txn_d = date.fromisoformat(e["ex_date"])
            except ValueError:
                txn_d = None
            orders.append_ledger(
                conn, "dividend", amount,
                note=f"{fund_code} {e['ex_date']} 现金分红到账", txn_date=txn_d,
            )
        conn.commit()
        applied.append({**e, "shares": round(shares, 2),
                        "amount": round(amount, 2), "method": method})

    return {
        "fund_code": fund_code,
        "method": method,
        "detected": len(events),
        "applied": applied,
        "skipped": skipped,
    }


def list_dividends(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """分红记录列表。"""
    rows = conn.execute(
        "SELECT * FROM dividends ORDER BY ex_date DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# main.py 路由 /api/dividends 调用 get_dividends；旧引擎命名为 list_dividends
get_dividends = list_dividends


# =====  新增：拉分红公告（「分红方式/时间由基金公告定」） =====

def fetch_dividend_announcements(fund_code: str) -> list[dict]:
    """拉取基金分红公告（东方财富），返回 [{ann_date, title, report_id}]。

    ⚠️ **公告只有标题与日期，不含每份分红金额**（金额要打开公告正文才有）。
    所以这里只回答「**有没有分红、哪天公告的**」—— 金额另走 `detect_dividends`
    （净值口径：`acc_nav - nav` 跳变）或需人工/AI 核对，**不猜**。
    """
    try:
        import akshare as ak

        df = ak.fund_announcement_dividend_em(symbol=fund_code)
    except Exception as exc:  # noqa: BLE001
        print(f"[dividend] 分红公告拉取失败 {fund_code}: {type(exc).__name__}: {exc}")
        return []
    if df is None or getattr(df, "empty", True):
        return []

    out: list[dict] = []
    for _, r in df.iterrows():
        title = str(r.get("公告标题") or "").strip()
        if not title:
            continue
        out.append({
            "ann_date": str(r.get("公告日期") or "")[:10],
            "title": title,
            "report_id": str(r.get("报告ID") or "").strip(),
        })
    return out


def process_held_dividends(conn) -> dict:
    """对**持仓基金**跑一次分红处理（公告确认事件 + 净值算金额 + 记账）。

    ⚠️ 只处理**持仓基金**（lots.remaining_shares>0），**不抓全市场** ——
    分红只影响**已持有的**基金的账和分红方式，全量抓取既慢又无必要。

    返回 {funds, announced_only, detected, applied, skipped}：
      announced_only = 有公告但净值无金额（acc_nav 缺失）→ **待人工/AI 核对金额**，不编造。
    """
    stat = {"funds": 0, "announced_only": 0, "detected": 0, "applied": 0, "skipped": 0}
    try:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM lots WHERE status='active' AND remaining_shares>0"
        ).fetchall()]
    except Exception:  # noqa: BLE001
        return stat

    for code in codes:
        stat["funds"] += 1
        try:
            before = {e["ex_date"] for e in detect_dividends(conn, code)}
            if not before:
                ann = fetch_dividend_announcements(code)
                recent = [a for a in ann if a["ann_date"] >= "2025-01-01"]
                if recent:
                    stat["announced_only"] += 1
                    print(f"[dividend] {code} 有分红公告 {len(recent)} 条，但净值检测不到金额"
                          f"（acc_nav 缺失）→ 待人工/AI 核对，不自动记账")
                else:
                    stat["skipped"] += 1
                continue
            stat["detected"] += len(before)
            r = process_dividends(conn, code)
            stat["applied"] += len(r.get("applied", []))
        except Exception as exc:  # noqa: BLE001
            print(f"[dividend] {code} 处理失败: {type(exc).__name__}: {exc}")
            stat["skipped"] += 1
    return stat
