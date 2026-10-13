"""订单与记账引擎：把一笔交易从下单走到确认。

真实的场外基金流程是「未知价交易」，和股票完全不同：

    T 日下单   → 不知道净值，只冻结资金/份额，订单 pending
    T+1 确认   → 净值公布，算出实际份额，建 lot、扣款、记账
    T+N 到账   → 赎回资金到账（品类不同天数不同）

所以 **submit 只建订单，不建 lot**；confirm 才真正算份额、扣钱、生成批次。
别按股票的「下单即成交」模型来写，否则份额和费用全错。

下单前要过的检查（按顺序，任一不过即拒绝）：
    1. 可投范围   场外？不是场内 ETF/REITs
    2. 交易状态   限购金额、暂停申购、暂停赎回、封闭期
    3. 资金/份额  买：现金够不够；卖：份额够不够
    4. 月度配额   定投+加仓+开新仓共享同一池子，不能超
    5. 风控边界   单只持仓上限、单笔上限、单日次数
    6. 罚期       赎回时提示是否踩 7 天惩罚费（提示，不硬性阻止）
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from backend.config import get, test_funds
from backend.engine import calendar, fees, holdings, ledger, status
from backend.engine import fund_pool


# =============================================================================
# 资金账本
# =============================================================================


def get_cash_balance(conn: sqlite3.Connection) -> float:
    """当前现金余额。

    余额不单独存字段，而是由流水累加得出——任何一笔钱都能追溯到来源，
    对不上账时可以逐笔核对。最后一笔的 balance_after 即当前余额。
    """
    row = conn.execute(
        "SELECT balance_after FROM cash_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["balance_after"] if row else 0.0


def append_ledger(
    conn: sqlite3.Connection,
    txn_type: str,
    amount: float,
    ref_order_id: int | None = None,
    note: str = "",
    txn_date: date | None = None,
) -> float:
    """记一笔资金流水，返回记账后的余额。

    txn_type: deposit(入金) | purchase(申购扣款) | redeem(赎回到账)
              fee(费用) | dividend(分红) | refund(撤单退款)

    本函数只 INSERT、不 commit——事务边界由调用方控制，
    这样 confirm_order 才能把「扣款 + 建批次 + 改订单状态」做成原子操作。
    """
    txn_date = txn_date or date.today()
    balance = get_cash_balance(conn) + amount
    conn.execute(
        """
        INSERT INTO cash_ledger
            (txn_date, txn_type, amount, balance_after, ref_order_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn_date.isoformat(),
            txn_type,
            amount,
            balance,
            ref_order_id,
            note,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return balance


# =============================================================================
# 检查
# =============================================================================


def _fund_meta(conn: sqlite3.Connection, fund_code: str) -> dict:
    """取基金档案。

    优先级与合并规则（修复 270042「5 元也能买」异常）：
      1) 先看 funds 表（用户搜索添加，添加时已从实时数据落库）。
      2) 起购/限购以实时抓取为准（「所有的都要同步真实的，不是固定」）：
         DB 是添加时的落库快照（旧版还被卫生地板抬过，如 270042 真实 5 元落库成 10），
         配置人工值也可能是旧认知下的固定值（270042 曾按「akshare 5 是脏数据」写成 10000）。
         优先级：实时抓取（拿到行即为准，None=清洗后的「不限」） > 配置显式 > DB 快照；
         akshare 拿不到该基金时才回退配置/DB。
      3) 起购金额 min_purchase 取真实值，不强制抬卫生地板——
         与交易池/搜索展示一致，避免「显示 5 元、下单按 10 元拦」自相矛盾。
    """
    cfg = next((f for f in test_funds() if f["code"] == fund_code), None)

    # 1) 用户搜索添加的基金（funds 表，添加时已从实时数据落库）
    try:
        row = conn.execute("SELECT * FROM funds WHERE code=?", (fund_code,)).fetchone()
    except Exception:
        row = None
    if row:
        d = dict(row)
        d["is_qdii"] = bool(d.get("is_qdii"))
        # 起购/限购以实时抓取为准（「所有的都要同步真实的，不是固定」）：
        # DB 是添加时的落库快照（旧版还被卫生地板抬过，如 270042 真实 5 元落库成 10），
        # 配置人工值也可能是旧认知下的固定值（270042 曾按「akshare 5 是脏数据」写成 10000）。
        # 优先级：实时抓取（拿到行即为准，None=清洗后的「不限」） > 配置显式 > DB 快照。
        pi = fund_pool.lookup_purchase(fund_code)
        if pi:
            if pi.get("min_purchase") is not None:
                d["min_purchase"] = pi["min_purchase"]
            d["purchase_limit"] = pi.get("purchase_limit")
        elif cfg:
            # akshare 拿不到该基金时才回退：配置显式 > DB 快照
            if cfg.get("min_purchase") is not None:
                d["min_purchase"] = cfg["min_purchase"]
            if cfg.get("purchase_limit") is not None:
                d["purchase_limit"] = cfg["purchase_limit"]
        return d
    # 2) 配置池（未添加到 funds 表）
    if cfg:
        m = dict(cfg)
        pi = fund_pool.lookup_purchase(fund_code)
        if pi:
            # 实时为准：None=清洗后的「不限」，同样是真值
            if pi.get("min_purchase") is not None:
                m["min_purchase"] = pi["min_purchase"]
            m["purchase_limit"] = pi.get("purchase_limit")
        # akshare 拿不到时保留配置值；起购不抬地板
        return m
    # 3) 兜底：funds 表和配置池都没有（如纯搜索、未添加的基金）——同样用实时抓取
    d = {"code": fund_code, "name": fund_code, "category": "混合型",
         "is_qdii": False, "purchase_fee_rate": 0.015,
         "min_purchase": None, "purchase_limit": None}
    pi = fund_pool.lookup_purchase(fund_code)
    if pi:
        if pi.get("min_purchase") is not None:
            d["min_purchase"] = pi["min_purchase"]
        d["purchase_limit"] = pi.get("purchase_limit")
    return d


def _month_quota_used(conn: sqlite3.Connection, year_month: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM quota_usage WHERE year_month = ?",
        (year_month,),
    ).fetchone()
    return row["s"] if row else 0.0


def _orders_today(conn: sqlite3.Connection, d: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE submit_date = ?", (d.isoformat(),)
    ).fetchone()
    return row["c"] if row else 0


def check_before_submit(conn: sqlite3.Connection, fund_code: str, side: str,
                        amount: float, cal: calendar.TradeCalendar) -> tuple[bool, str, dict]:
    """下单前检查。返回 (是否通过, 原因, 上下文信息)。"""
    meta = _fund_meta(conn, fund_code)
    # warnings：风控边界突破时的「纯提醒」列表，只记录不拦截（风控边界只是警告）
    info: dict = {"fund_name": meta.get("name", fund_code), "warnings": []}

    # 1. 可投范围
    ok, why = holdings.check_tradable(meta.get("category", ""), meta.get("name", ""))
    if not ok:
        return False, why, info

    # 2. 交易状态（限购/暂停/封闭期）
    st_raw = status.fetch_and_save(conn, fund_code, meta.get("is_qdii", False))
    if st_raw is None:
        return False, "无法获取该基金的交易状态，拒绝下单（拿不到就不猜）", info
    if side == "buy":
        # 传入人工维护的限购额作为兜底（接口只给「限大额」不给金额）
        ok, why = status.can_buy(st_raw, amount, meta.get("purchase_limit"))
    else:
        ok, why = status.can_sell(st_raw, None, date.today())
    if not ok:
        return False, why, info
    info["status"] = st_raw

    # 3. 资金 / 份额
    if side == "buy":
        cash = get_cash_balance(conn)
        if amount > cash:
            return False, f"可用资金 {cash:.2f} 元，不足以支付 {amount:.2f} 元", info
        # 3.5 最低购买金额（起购点）。很多基金有门槛（1/10/100 元不等），
        # 这是真实约束，必须拦在下单前，不能等下单失败才发现。
        min_p = meta.get("min_purchase") or 1.0
        if amount < min_p - 1e-9:
            return False, (
                f"单笔申购金额 {amount:.2f} 元低于该基金最低购买金额 "
                f"{min_p:.2f} 元（起购门槛，真实规则）"
            ), info
    else:
        pos = _position_shares(conn, fund_code)
        # 可赎回份额 = 持仓 − **在途赎回**（见 `_pending_sell_shares` 的说明）
        pending_sell = _pending_sell_shares(conn, fund_code)
        usable = pos - pending_sell
        if amount > usable + 1e-9:
            return False, (
                f"可赎回份额 {usable:.4f} 份（持仓 {pos:.4f} − 在途赎回 {pending_sell:.4f}），"
                f"不足以赎回 {amount:.4f} 份"
            ), info

    # 4. 可用预算（累积制，盈亏并入——这是买入的硬约束）
    #    **只对买入生效**。赎回不消耗预算：赎回款到账反而会增加可用资金。
    #    曾经这里对 sell 也校验，而 sell 的 amount 是「份额」不是「元」，
    #    拿份额去比预算金额，会把正常赎回和基金转换的转出单全部误拒。
    if side == "buy":
        # 延迟导入：budget 模块会反向 import orders，放这里避免循环依赖
        from backend.engine import budget as _budget

        avail = _budget.available_budget(conn)
        if amount > avail:
            return False, (
                f"可用预算 {avail:.2f} 元，不足以支付 {amount:.2f} 元。"
                f"可用 = 初始 + 累积新增 − 持仓占用 + 已实现盈亏"
            ), info
        info["budget_available"] = round(avail, 2)

    # 5. 风控边界——纯提醒，不拦截（风控边界只是警告，由人决定是否继续）。
    #    注意区分：下面的现金/预算/可投范围/交易状态/起购点/持有份额等是
    #    「订单本身是否有效、能否成交」的真实硬约束，不属于可配的风控边界，仍保持硬拦。
    max_per_order = get("risk", "max_amount_per_order", 500.0)
    if amount > max_per_order:
        info["warnings"].append(
            f"单笔金额 {amount:.2f} 元已超过风控上限 {max_per_order:.0f} 元（仅提醒，照常下单）"
        )
    max_orders = get("risk", "max_orders_per_day", 10)
    today_cnt = _orders_today(conn, date.today())
    if today_cnt >= max_orders:
        info["warnings"].append(
            f"今日已下单 {today_cnt} 笔，达到风控上限 {max_orders} 笔（仅提醒，照常下单）"
        )

    # 单只持仓占比上限（提醒用，不拦截）。
    # 必须在「买入后」计算，而不是用当前占比判断——
    # 因为这笔买入本身就会同时抬高该基金市值和总市值，
    # 用当前占比会漏掉「小额账户一次性重仓」的情况。
    if side == "buy":
        max_pct = get("risk", "max_single_position_pct", 25.0)
        current_total_mv = _total_market_value(conn)
        # 空仓时允许第一只基金建仓；建仓后再次买入才计算集中度上限。
        if current_total_mv > 0:
            fund_mv_after = _position_shares(conn, fund_code) * _latest_nav(
                conn, fund_code
            ) + amount
            total_mv_after = current_total_mv + amount
            pct_after = fund_mv_after / total_mv_after * 100
            if pct_after > max_pct:
                info["warnings"].append(
                    f"买入后 {fund_code} 占比将达 {pct_after:.1f}%，"
                    f"超过单只风控上限 {max_pct}%（仅提醒，照常下单）"
                )
            info["position_pct_after"] = round(pct_after, 1)
        else:
            info["position_pct_after"] = 100.0

    return True, "ok", info


def _position_shares(conn: sqlite3.Connection, fund_code: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(remaining_shares), 0) AS s FROM lots
        WHERE fund_code = ? AND status = 'active'
        """,
        (fund_code,),
    ).fetchone()
    return row["s"] if row else 0.0


def _pending_sell_shares(conn: sqlite3.Connection, fund_code: str) -> float:
    """在途赎回份额（已提交、尚未确认的 sell 单）。

    赎回单从**提交**到 **T+1 确认**之间，份额还没从 `lots` 里消耗 ——
    若只看持仓份额，同一批份额可以被**重复提交多笔赎回**（超额提交），
    确认时后一笔才因份额不足失败。真实世界里提交后份额即被冻结，故这里一并扣除。
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM orders "
        "WHERE fund_code = ? AND side = 'sell' AND status = 'pending'",
        (fund_code,),
    ).fetchone()
    return float(row["s"]) if row and row["s"] is not None else 0.0


def _latest_nav(conn: sqlite3.Connection, fund_code: str) -> float:
    """取最新**单位**净值（市值口径）。

    ⚠️ 市值口径必须用**单位净值** `nav`，不能用累计净值 `acc_nav`。
    有历史分红的基金 acc_nav 明显大于 nav，用它算市值/份额会**高估净值、少给份额**
    （如 000059 份额少 37%、000015 少 24.5%）。
    """
    row = conn.execute(
        """
        SELECT nav AS v FROM nav_daily
        WHERE fund_code = ? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1
        """,
        (fund_code,),
    ).fetchone()
    return row["v"] if row and row["v"] else 0.0


def _total_market_value(conn: sqlite3.Connection) -> float:
    """全部持仓的市值合计。"""
    total = 0.0
    for row in conn.execute(
        "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
    ):
        code = row["fund_code"]
        total += _position_shares(conn, code) * _latest_nav(conn, code)
    return total


# =============================================================================
# 下单
# =============================================================================


def _next_order_no(conn: sqlite3.Connection, side: str) -> str:
    prefix = "B" if side == "buy" else "S"
    today = date.today().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE order_no LIKE ?",
        (f"{prefix}{today}%",),
    ).fetchone()
    return f"{prefix}{today}{row['c'] + 1:03d}"


def submit_order(conn: sqlite3.Connection, fund_code: str, side: str,
                 amount: float, reason: str, decision_source: str = "external",
                 cal: calendar.TradeCalendar | None = None,
                 purpose: str = "dingtou",
                 fund_name: str | None = None,
                 fee_rate_discounted: bool = False) -> dict:
    # 注：fund_name / fee_rate_discounted 仅为兼容 OrderRequest 而接收；orders 表无 fund_name 列，
    # 名称可从 funds 表派生；funds.purchase_fee_rate 已是折后费率，calc_purchase 一律
    # already_discounted=True，本字段不参与计费、绝不二次打折。
    """提交订单。

    参数：
        side       buy(申购，amount=金额) | sell(赎回，amount=份额)
        reason     决策理由。**外部 AI 下单必须填写**，空理由拒绝
        purpose    buy(一次性手动买入) | dingtou(登记定投计划，首期申购计入本期)

    返回：{"ok": bool, "order_id"/"reason": ...}
    """
    cal = cal or calendar.TradeCalendar()
    cal.load()

    # 决策理由：外部 AI / 规则引擎下单必须填写（每笔都要能追溯为什么做）；
    # 人工手动操作由本人点击发起，不强制填理由（人工操作不需要购买理由）。
    if decision_source != "human" and not (reason or "").strip():
        return {"ok": False, "reason": "决策理由为空。非人工来源的下单必须说明依据，拒绝无理由下单"}

    ok, why, info = check_before_submit(conn, fund_code, side, amount, cal)
    if not ok:
        return {"ok": False, "reason": why, "info": info}

    # 风控边界仅为提醒：非人工来源（定投/规则引擎/外部 AI）也要把提醒打到日志，
    # 便于事后回溯「当时为啥越界了」；人工来源由前端弹窗提示。
    for w in info.get("warnings", []):
        print(f"[风控提醒] {w}（来源={decision_source}）")

    now = datetime.now()
    timing = cal.describe_order_timing(now)
    nav_date = date.fromisoformat(timing["nav_date"])
    # 此处不取基金元数据：`check_before_submit` 已单独取过，
    # 复用其结果即可，避免重复库查询。

    # 申购先算费用（外扣法），实际份额要等确认时按净值算。
    # 申购费用统一走 `fees.calc_purchase`（费率由 funds 表读取），此处不重复取费率。
    if side == "buy":
        # 注意：funds.purchase_fee_rate 已是 akshare 折后费率，传 already_discounted=True 防二次打折
        # calc_purchase 返回 dict，按字段取值（返回类型是 dict 非元组）
        _fp = fees.calc_purchase(fund_code, amount, already_discounted=True)
        fee_purchase, net_amount = _fp["fee"], _fp["net_amount"]
    else:
        fee_purchase, net_amount = 0.0, 0.0

    order_no = _next_order_no(conn, side)
    cur = conn.execute(
        """
        INSERT INTO orders
            (order_no, fund_code, side, amount, submit_date, submit_time,
             nav_date, status, decision_source, reason, fee_purchase, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            order_no,
            fund_code,
            side,
            amount,
            now.date().isoformat(),
            now.strftime("%H:%M:%S"),
            nav_date.isoformat(),
            decision_source,
            reason.strip(),
            fee_purchase,
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    order_id = cur.lastrowid

    # 记配额使用（三个用途共享同一池子）
    conn.execute(
        """
        INSERT INTO quota_usage (year_month, purpose, fund_code, amount,
                                 ref_order_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            date.today().strftime("%Y-%m"),
            purpose,
            fund_code,
            amount if side == "buy" else 0.0,
            order_id,
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()

    return {
        "ok": True,
        "order_id": order_id,
        "order_no": order_no,
        "timing": timing,
        "fee_purchase": round(fee_purchase, 2),
        "net_amount": round(net_amount, 2) if side == "buy" else None,
        "warnings": info.get("warnings", []),
        "info": info,
    }


# =============================================================================
# 确认（T+1）
# =============================================================================


def confirm_order(conn: sqlite3.Connection, order_id: int,
                  cal: calendar.TradeCalendar | None = None) -> dict:
    """确认订单。净值公布后调用，算实际份额、建 lot、扣款记账。

    这是「未知价交易」的第二步：下单时不知道净值，此时才知道。
    """
    cal = cal or calendar.TradeCalendar()
    cal.load()

    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": f"订单 {order_id} 不存在"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"订单状态为 {row['status']}，无需确认"}

    fund_code = row["fund_code"]
    nav_date = date.fromisoformat(row["nav_date"])
    meta = _fund_meta(conn, fund_code)

    # ⚠️ 成交净值必须用**单位净值 nav**：
    # 原实现 `COALESCE(acc_nav, nav)` 累计优先 —— 有历史分红的基金 acc_nav 远大于 nav，
    # 而 `份额 = 净额 / 净值`，用偏大的净值算就会**少给份额**
    # （实测 000059 少 **37%**、000015 少 **24.5%**）。
    # 拿不到单位净值就**拒绝确认**，不再回退累计净值 —— 钱的事，拿不准就不猜。
    nav_row = conn.execute(
        """
        SELECT nav AS v FROM nav_daily
        WHERE fund_code = ? AND nav_date <= ? AND nav IS NOT NULL
        ORDER BY nav_date DESC LIMIT 1
        """,
        (fund_code, nav_date.isoformat()),
    ).fetchone()
    if not nav_row or not nav_row["v"]:
        return {"ok": False,
                "reason": f"{fund_code} 在 {nav_date} 及之前无**单位净值**，无法确认（不回退累计净值）"}
    nav = nav_row["v"]

    with conn:
        if row["side"] == "buy":
            amount = row["amount"]
            # 注意：funds.purchase_fee_rate 已是 akshare 折后费率，传 already_discounted=True 防二次打折
            # calc_purchase 返回 dict，按字段取值（返回类型是 dict 非元组）
            _cp = fees.calc_purchase(fund_code, amount, already_discounted=True)
            fee, net = _cp["fee"], _cp["net_amount"]
            shares = net / nav if nav else 0.0

            # 扣款记账
            append_ledger(
                conn, "purchase", -amount,
                ref_order_id=order_id,
                note=f"申购 {fund_code} {amount:.2f} 元（费 {fee:.2f}）",
            )
            # 建份额批次。确认日即持有期起算点
            confirm_date = cal.confirm_date(
                nav_date, 2 if meta.get("is_qdii") else 1
            )
            lot_id = ledger.create_lot(
                conn, fund_code, order_id, confirm_date, shares, nav, amount, commit=False
            )
            conn.execute(
                """
                UPDATE orders SET status='confirmed', deal_nav=?, deal_shares=?,
                    deal_amount=?, fee_purchase=?, confirm_date=?
                WHERE order_id=?
                """,
                (nav, shares, amount, fee, confirm_date.isoformat(), order_id),
            )
            return {
                "ok": True, "order_id": order_id, "side": "buy",
                "nav": round(nav, 4), "shares": round(shares, 4),
                "fee": round(fee, 2), "lot_id": lot_id,
                "confirm_date": confirm_date.isoformat(),
            }

        # 赎回
        shares = row["amount"]
        # ⚠️ commit=False：`confirm_order` 用 `with conn:` 包住整笔业务（扣款+消耗批次+改订单状态），
        # 内部函数若自行 commit 会把事务切碎 —— 后续步骤失败时前面已落库、无法整体回滚
        # （`consume_lots` 原在函数内 commit）。
        consumed = ledger.consume_lots(conn, fund_code, shares, nav_date, nav, commit=False)
        gross = sum(c["amount"] for c in consumed)
        total_fee = sum(c["fee"] for c in consumed)
        net_amount = gross - total_fee
        penalty = any(c["is_penalty"] for c in consumed)

        arrival = cal.arrival_date_by_category(nav_date, meta.get("category", "混合型"))
        append_ledger(
            conn, "redeem", net_amount,
            ref_order_id=order_id,
            note=(f"赎回 {fund_code} {shares:.2f} 份"
                  + ("（含 7 天惩罚费）" if penalty else "")),
        )
        conn.execute(
            """
            UPDATE orders SET status='confirmed', deal_nav=?, deal_shares=?,
                deal_amount=?, fee_redeem=?, penalty_flag=?, arrival_date=?
            WHERE order_id=?
            """,
            (nav, shares, net_amount, total_fee, 1 if penalty else 0,
             arrival.isoformat(), order_id),
        )
        return {
            "ok": True, "order_id": order_id, "side": "sell",
            "nav": round(nav, 4), "shares": round(shares, 4),
            "gross": round(gross, 2), "fee": round(total_fee, 2),
            "net_amount": round(net_amount, 2),
            "penalty": penalty, "arrival_date": arrival.isoformat(),
            "consumed_lots": consumed,
        }


def cancel_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """撤单。仅限未确认的订单（T 日 15:00 前可撤）。"""
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "订单不存在"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"订单已 {row['status']}，无法撤销"}
    conn.execute("UPDATE orders SET status='cancelled' WHERE order_id=?", (order_id,))
    # 退回配额占用
    conn.execute("DELETE FROM quota_usage WHERE ref_order_id = ?", (order_id,))
    conn.commit()
    return {"ok": True, "order_id": order_id, "note": "已撤销并释放配额占用"}


def convert_fund(conn: sqlite3.Connection, from_code: str, to_code: str,
                 shares: float, reason: str = "",
                 cal: calendar.TradeCalendar | None = None,
                 decision_source: str = "human") -> dict:
    """基金转换：赎回 from（按份额）→ 申购 to（按估算金额）。

    真实世界的转换是「份额直接换、资金不过手」，本沙盒账本由现金流驱动，
    因此拆成 sell + buy 两笔 pending 单，用 related_order_id 互相关联，
    账本层面（赎回入账 + 申购扣款）净现金流为零，与真实转换等价。

    转入失败会自动撤销转出单，避免「卖飞了却没买上」。
    """
    if from_code == to_code:
        return {"ok": False, "reason": "转出与转入基金不能相同"}
    if not (shares > 0):
        return {"ok": False, "reason": "转换份额必须大于 0"}

    cal = cal or calendar.TradeCalendar()
    cal.load()

    # 1) 转出：按份额赎回
    r_sell = submit_order(
        conn, fund_code=from_code, side="sell", amount=shares,
        reason=f"基金转换转出 → {to_code}" + (f"；{reason}" if reason else ""),
        decision_source=decision_source, cal=cal, purpose="convert",
    )
    if not r_sell.get("ok"):
        return {"ok": False, "reason": f"转出失败：{r_sell.get('reason', '未知原因')}"}
    sell_id = r_sell["order_id"]

    # 2) 估算转入金额：份额 × 最新净值（真实金额以 T+1 确认净值为准）
    nav = _latest_nav(conn, from_code)
    amount = round(shares * nav, 2)
    if amount <= 0:
        cancel_order(conn, sell_id)
        return {"ok": False, "reason": "无法估算转出金额（缺少净值），已撤销转出单"}

    # 3) 转入：按估算金额申购
    r_buy = submit_order(
        conn, fund_code=to_code, side="buy", amount=amount,
        reason=f"基金转换转入 ← {from_code}" + (f"；{reason}" if reason else ""),
        decision_source=decision_source, cal=cal, purpose="convert",
    )
    if not r_buy.get("ok"):
        # 转入失败 → 撤销转出单，保证不会只卖不买
        cancel_order(conn, sell_id)
        return {"ok": False,
                "reason": f"转入失败（已自动撤销转出单）：{r_buy.get('reason', '未知原因')}"}
    buy_id = r_buy["order_id"]

    # 4) 两笔订单互相关联，便于回看"这是一笔转换"
    conn.execute("UPDATE orders SET related_order_id=? WHERE order_id=?", (buy_id, sell_id))
    conn.execute("UPDATE orders SET related_order_id=? WHERE order_id=?", (sell_id, buy_id))
    conn.commit()

    return {
        "ok": True,
        "from_code": from_code, "to_code": to_code,
        "shares": shares, "estimated_amount": amount,
        "sell_order_id": sell_id, "buy_order_id": buy_id,
        "note": f"已建转出单 #{sell_id} 与转入单 #{buy_id}，T+1 按真实净值结算",
    }


if __name__ == "__main__":
    from backend.db import get_connection, init_db

    print("=" * 78)
    print("订单闭环演示（真实规则，非模拟数据）")
    print("=" * 78)

    init_db()
    conn = get_connection()
    cal = calendar.TradeCalendar()
    cal.load()

    # 入金：初始资金 + 当月新增预算（都是幂等的，重复跑不会重复入账）
    from backend.engine import budget as _budget

    if _budget.init_capital(conn):
        print(f"\n初始资金入账 {get('capital', 'initial_cash', 1000.0):.0f} 元")
    if _budget.add_monthly_budget(conn):
        print(f"当月新增预算入账 {get('capital', 'monthly_quota', 1000.0):.0f} 元")

    bd = _budget.breakdown(conn)
    print(f"\n可用预算 {bd['available']:.2f} 元"
          f"（初始 {bd['initial']:.0f} + 累积新增 {bd['cumulative_added']:.0f}"
          f" − 持仓占用 {bd['used_in_positions']:.2f}"
          f" + 已实现盈亏 {bd['realized_pnl']:.2f}）")
    print(f"现金余额 {bd['cash_balance']:.2f} 元")

    code = test_funds()[0]["code"]

    print("\n── 场景1：无理由下单（应被拒绝）")
    r = submit_order(conn, code, "buy", 100.0, reason="", cal=cal)
    print(f"  结果：{'通过' if r['ok'] else '拒绝'}  {r.get('reason')}")

    print("\n── 场景2：超额下单（应被风控拦截）")
    r = submit_order(conn, code, "buy", 99999.0,
                     reason="测试超额", cal=cal)
    print(f"  结果：{'通过' if r['ok'] else '拒绝'}  {r.get('reason')}")

    print("\n── 场景3：正常下单 100 元")
    r = submit_order(conn, code, "buy", 100.0,
                     reason="板块估值分位低，按定投节奏建仓", cal=cal)
    if r["ok"]:
        print(f"  订单号 {r['order_no']}（id={r['order_id']}）")
        print(f"  时间归属：{r['timing']['hint']}")
        print(f"  申购费 {r['fee_purchase']:.2f} 元，净申购 {r['net_amount']:.2f} 元")
        print("\n  确认订单（净值公布后）：")
        c = confirm_order(conn, r["order_id"], cal)
        if c["ok"]:
            print(f"    成交净值 {c['nav']}，获得份额 {c['shares']:.4f}")
            print(f"    确认日 {c['confirm_date']}（持有期起算）")
            print(f"    剩余现金 {get_cash_balance(conn):.2f} 元")
        else:
            print(f"    确认失败：{c['reason']}")
    else:
        print(f"  下单失败：{r['reason']}")

    print("\n── 当前持仓批次：")
    for lot in ledger.holding_report(conn, code, date.today()):
        print(f"    确认日 {lot['confirm_date']}  {lot['shares']:.4f} 份  "
              f"持有 {lot['holding_days']} 天  "
              f"{'还需 %d 天免罚' % lot['days_to_penalty_free'] if lot['days_to_penalty_free'] else '已免罚'}")

    conn.close()
    print("\n" + "=" * 78)
    print("说明：基金是「未知价交易」——下单时只冻结资金，确认时才按净值算份额。")
    print("      真实运行下确认由调度器在 T+1 触发，这里手动调用以演示闭环。")
    print("=" * 78)


def get_orders(conn: sqlite3.Connection, status: str | None = None,
               limit: int = 100) -> list[dict]:
    """订单列表（按 order_id 倒序）。status 可选过滤。供 /api/orders。"""
    sql = "SELECT * FROM orders"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY order_id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def confirm_orders(conn: sqlite3.Connection) -> dict:
    """确认所有 pending 订单（T+1 净值公布后由启动后处理与调度器调用）。"""
    rows = conn.execute(
        "SELECT order_id FROM orders WHERE status='pending' ORDER BY order_id"
    ).fetchall()
    results = [confirm_order(conn, r["order_id"]) for r in rows]
    ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    return {"total": len(results), "confirmed": ok, "results": results}
