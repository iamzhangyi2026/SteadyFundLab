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
    4. 可用预算   定投+加仓+开新仓共享同一池子，不能超（累积制，盈亏并入）
    5. 风控边界   单只持仓上限、单笔上限、单日次数
    6. 罚期       赎回时提示是否踩 7 天惩罚费（提示，不硬性阻止）
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from backend.config import get, test_funds
from backend.engine import calendar, fees, holdings, ledger, status
from backend.engine import fund_pool
from backend.engine import money as _m


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

    txn_type: initial(初始资金) | monthly_add(月度新增) | test_capital(沙盒测试资金)
              purchase(申购扣款) | redeem(赎回到账) | dividend(分红)
              fee(费用) | refund(撤单退款)

    金额与滚动余额均按 2 位（元）Decimal 量化后落库——
    balance_after 是账本的基准列，逐笔累加若走浮点会累积分币差。
    本函数只 INSERT、不 commit——事务边界由调用方控制，
    这样 confirm_order 才能把「扣款 + 建批次 + 改订单状态」做成原子操作。
    """
    txn_date = txn_date or datetime.now().date()
    amt = _m.q_money(amount)
    balance = _m.q_money(_m.D(get_cash_balance(conn)) + amt)
    conn.execute(
        """
        INSERT INTO cash_ledger
            (txn_date, txn_type, amount, balance_after, ref_order_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            txn_date.isoformat(),
            txn_type,
            float(amt),
            float(balance),
            ref_order_id,
            note,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return float(balance)


# =============================================================================
# 检查
# =============================================================================


def _fund_meta(conn: sqlite3.Connection, fund_code: str) -> dict:
    """取基金档案。

    优先级与合并规则：
      1) 先看 funds 表（用户搜索添加，添加时已从实时数据落库）。
      2) 起购/限购以实时抓取为准：
         优先级：实时抓取（拿到行即为准，None=清洗后的「不限」） > 配置显式 > DB 快照；
         实时接口拿不到该基金时才回退配置/DB。
      3) 起购金额 min_purchase 取真实值，不设卫生地板——
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
        # 起购/限购以实时抓取为准：实时（拿到行即为准，None=清洗后的「不限」）
        # > 配置显式 > DB 快照；DB 是添加时的落库快照，可能已过期。
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


def _orders_today(conn: sqlite3.Connection, d: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE submit_date = ?", (d.isoformat(),)
    ).fetchone()
    return row["c"] if row else 0


def check_before_submit(conn: sqlite3.Connection, fund_code: str, side: str,
                        amount: float, cal: calendar.TradeCalendar) -> tuple[bool, str, dict]:
    """下单前检查。返回 (是否通过, 原因, 上下文信息)。"""
    meta = _fund_meta(conn, fund_code)
    # warnings：风控边界突破时的「纯提醒」列表，只记录不拦截（风控边界仅为提醒，不拦截）
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
        ok, why = status.can_sell(st_raw)
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
        # 3.5 最短持有期（按批次锁定）：持有期基金每笔份额自确认日起锁定期满才能赎回。
        # 按 FIFO 预览这笔赎回会消耗的批次，只要有批次未到解禁日就整笔拒绝——
        # 份额不能「部分可赎」：锁定期内的批次动了就是违规。
        hold_days = _hold_period_days(conn, fund_code)
        if hold_days > 0:
            locked = _locked_lots(
                ledger.preview_consume_lots(conn, fund_code, amount), hold_days
            )
            if locked:
                return False, (
                    f"该基金有 {hold_days} 天最短持有期：按先进先出会消耗 "
                    f"{len(locked)} 个未解禁批次，最早 {locked[0]['unlock_date']} 解禁，"
                    f"暂不能赎回"
                ), info

    # 4. 可用预算（累积制，盈亏并入——这是买入的硬约束）
    #    **只对买入生效**。赎回不消耗预算：赎回款到账反而会增加可用资金。
    #    sell 的 amount 是「份额」不是「元」，拿份额比预算金额会误拒，故不校验。
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

    # 5. 风控边界——纯提醒，不拦截（风控边界仅为提醒，由人决定是否继续）。
    #    注意区分：下面的现金/预算/可投范围/交易状态/起购点/持有份额等是
    #    「订单本身是否有效、能否成交」的真实硬约束，不属于可配的风控边界，仍保持硬拦。
    max_per_order = get("risk", "max_amount_per_order", 500.0)
    if amount > max_per_order:
        info["warnings"].append(
            f"单笔金额 {amount:.2f} 元已超过风控上限 {max_per_order:.0f} 元（仅提醒，照常下单）"
        )
    max_orders = get("risk", "max_orders_per_day", 10)
    today_cnt = _orders_today(conn, datetime.now().date())
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
            nav_now = _latest_nav(conn, fund_code) or 0
            fund_mv_after = _position_shares(conn, fund_code) * nav_now + amount
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


def _hold_period_days(conn: sqlite3.Connection, fund_code: str) -> int:
    """该基金的最短持有期天数（0 = 无持有期限制）。

    数据来源：funds.hold_period_days（基金级属性）优先，
    其次最新一条 fund_status.hold_period_days 快照；两处都没有视为无限制。
    """
    days = 0
    for sql in (
        "SELECT hold_period_days FROM funds WHERE code=?",
        "SELECT hold_period_days FROM fund_status WHERE fund_code=? "
        "ORDER BY status_date DESC, id DESC LIMIT 1",
    ):
        try:
            row = conn.execute(sql, (fund_code,)).fetchone()
        except Exception:
            row = None
        if row:
            try:
                v = row["hold_period_days"]
            except (IndexError, KeyError):
                v = None
            if v:
                days = int(v)
                break
    return max(0, days)


def _locked_lots(preview: dict, hold_days: int) -> list[dict]:
    """FIFO 预览结果里仍处锁定期的批次：确认日 + 持有天数 > 今天。

    返回 [{confirm_date, unlock_date}]，按解禁日升序（最早解禁的在前）。
    持有期基金每笔份额自确认日起锁定期满才能赎——批次未解禁就动的，整笔拒绝。
    """
    today = datetime.now().date()
    locked = []
    for p in preview.get("consumed", []):
        cd = p.get("confirm_date")
        if not cd:
            continue
        try:
            unlock = date.fromisoformat(cd) + timedelta(days=hold_days)
        except ValueError:
            continue
        if unlock > today:
            locked.append({"confirm_date": cd, "unlock_date": unlock.isoformat()})
    return sorted(locked, key=lambda x: x["unlock_date"])


def _latest_nav(conn: sqlite3.Connection, fund_code: str) -> float | None:
    """取最新**单位**净值（市值口径）；查不到返回 None。

    市值口径用单位净值 nav（分红基金会高估份额），不取累计净值。
    有历史分红的基金 acc_nav 明显大于 nav，用它算市值会**高估净值、少给份额**。
    市值必须用**单位净值**。
    """
    row = conn.execute(
        """
        SELECT nav AS v FROM nav_daily
        WHERE fund_code = ? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1
        """,
        (fund_code,),
    ).fetchone()
    return row["v"] if row and row["v"] else None


def _total_market_value(conn: sqlite3.Connection) -> float:
    """全部持仓的市值合计。"""
    total = 0.0
    for row in conn.execute(
        "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
    ):
        code = row["fund_code"]
        total += _position_shares(conn, code) * (_latest_nav(conn, code) or 0)
    return total


# =============================================================================
# 下单
# =============================================================================


def _next_order_no(conn: sqlite3.Connection, side: str) -> str:
    prefix = "B" if side == "buy" else "S"
    # 订单号日期与 submit_date 同一口径（本机时区），否则跨时区部署时
    # 订单号前缀日期与订单归属日会差一天。
    today = datetime.now().date().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE order_no LIKE ?",
        (f"{prefix}{today}%",),
    ).fetchone()
    return f"{prefix}{today}{row['c'] + 1:03d}"


def submit_order(conn: sqlite3.Connection, fund_code: str, side: str,
                 amount: float, reason: str, decision_source: str = "external",
                 cal: calendar.TradeCalendar | None = None,
                 purpose: str = "buy",
                 fund_name: str | None = None,
                 fee_rate_discounted: bool = False) -> dict:
    # 注：fund_name / fee_rate_discounted 仅为兼容 OrderRequest 而接收；orders 表无 fund_name 列，
    # 名称可从 funds 表派生；funds.purchase_fee_rate 已是 akshare 折后费率，calc_purchase 一律
    # already_discounted=True，本字段不参与计费、绝不二次打折。
    """提交订单。

    参数：
        side       buy(申购，amount=金额) | sell(赎回，amount=份额)
        reason     决策理由。**外部 AI 下单必须填写**，空理由拒绝
        purpose    buy(一次性买入，默认) | dingtou(登记定投计划，首期申购计入本期)
                  | convert(基金转换的转入/转出单)。定投引擎显式传 dingtou；
                  其余调用方不传即一次性买入，不会误记进定投配额

    返回：{"ok": bool, "order_id"/"reason": ...}
    """
    cal = cal or calendar.TradeCalendar()
    cal.load()

    # 决策理由：外部 AI / 规则引擎下单必须填写（每笔都要能追溯为什么做）；
    # 人工手动操作由本人点击发起，不强制填理由（人工操作不需要购买理由）。
    if decision_source != "human" and not (reason or "").strip():
        return {"ok": False, "reason": "决策理由为空。非人工来源的下单必须说明依据，拒绝无理由下单"}

    # 「全额赎回」的精度夹紧：前端持仓份额是 4 位小数（`holdings.get_positions`
    # 里 `round(shares, 4)`），用户点「全部赎回」提交的正是这个四舍五入值，
    # 最多比真实持仓大 5e-5 份。这里把与可赎回份额相差不到 1e-4 的赎回额
    # 夹到精确值（视为全额赎回）：既不误拒，也不会真的多赎
    # （后续 FIFO 按精确份额消耗）。
    if side == "sell":
        usable = _position_shares(conn, fund_code) - _pending_sell_shares(conn, fund_code)
        if usable < amount <= usable + 1e-4:
            amount = usable

    # 买入金额量化到分后落库（ sell 的 amount 是份额，走上面的夹紧逻辑，不按元量化）——
    # 后续费用、配额、记账全部以量化后的金额为准，链路自洽。
    if side == "buy":
        amount = _m.money(amount)

    ok, why, info = check_before_submit(conn, fund_code, side, amount, cal)
    if not ok:
        return {"ok": False, "reason": why, "info": info}

    # 风控边界仅为提醒：非人工来源（定投/规则引擎/外部 AI）也要把提醒打到日志，
    # 便于事后回溯「当时为啥越界了」；人工来源由前端弹窗提示。
    for w in info.get("warnings", []):
        print(f"[风控提醒] {w}（来源={decision_source}）")

    # 下单时刻按本机时区取：15:00 截单、成交净值日、提交日期
    # 都以本机时区为准。
    now = datetime.now()
    timing = cal.describe_order_timing(now)
    nav_date = date.fromisoformat(timing["nav_date"])
    # 落库时间戳统一用本地墙钟格式（去时区后缀），与库内既有 naive 时间戳一致
    now_ts = now.replace(tzinfo=None)

    # 申购先算费用（外扣法），实际份额要等确认时按净值算。
    # 折后费率直接取 funds.purchase_fee_rate，传 already_discounted=True 防二次打折；
    # calc_purchase 返回 dict，按字段取值。
    if side == "buy":
        # funds.purchase_fee_rate 已是 akshare 折后费率，传 already_discounted=True 防二次打折
        # calc_purchase 返回 dict，按字段取值（返回类型是 dict 非元组）
        _fp = fees.calc_purchase(fund_code, amount, already_discounted=True)
        fee_purchase, net_amount = _fp["fee"], _fp["net_amount"]
    else:
        fee_purchase, net_amount = 0.0, 0.0

    # order_no 按「当日同前缀计数」生成，并发双提交（双击、AI 连发）会数出同一个号、
    # 撞 orders.order_no 的 UNIQUE 约束。以重试兜底：撞号就重新取号再插，
    # 5 次仍冲突才返回失败——不让第二笔请求直接 500 丢单。
    for _attempt in range(5):
        order_no = _next_order_no(conn, side)
        try:
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
                    now_ts.isoformat(timespec="seconds"),
                ),
            )
            break
        except sqlite3.IntegrityError:
            continue
    else:
        return {"ok": False, "reason": "订单号生成冲突（order_no 唯一约束），请重试"}
    order_id = cur.lastrowid

    # 记配额使用（三个用途共享同一池子）。
    # 与上面的订单 INSERT 同一个事务、只 commit 一次——分两次 commit 时
    # 中间崩溃会留下「有订单无配额」的孤儿记录。
    conn.execute(
        """
        INSERT INTO quota_usage (year_month, purpose, fund_code, amount,
                                 ref_order_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_ts.strftime("%Y-%m"),
            purpose,
            fund_code,
            amount if side == "buy" else 0.0,
            order_id,
            now_ts.isoformat(timespec="seconds"),
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


def _nav_for_confirm(conn: sqlite3.Connection, fund_code: str,
                     nav_date: date) -> tuple[float | None, str]:
    """取确认用的成交净值：只认订单净值日**当天**的单位净值。

    ⚠️ 必须精确匹配 nav_date：`nav_date <= ?` 取「小于等于里最新的一条」，
    净值日当天数据还没抓到（QDII、晚间刷新前、启动补确认）时会静默拿
    前一天净值成交——份额算错且永久落库。拿不到就拒绝确认，等下一轮刷新。

    成交净值还必须用**单位净值 nav** 而非累计净值：有历史分红的基金
    acc_nav 远大于 nav，用它算份额会少给。

    返回 (净值, 失败原因)；净值 None 表示当日暂无单位净值。
    """
    row = conn.execute(
        """
        SELECT nav AS v FROM nav_daily
        WHERE fund_code = ? AND nav_date = ? AND nav IS NOT NULL
        """,
        (fund_code, nav_date.isoformat()),
    ).fetchone()
    if not row or not row["v"]:
        return None, (f"{fund_code} 在 {nav_date} 暂无单位净值，无法确认"
                      f"（等待下一轮刷新，不拿旧净值成交）")
    return row["v"], ""


def _pair_order(conn: sqlite3.Connection, row, side: str):
    """取基金转换的配对单（related_order_id 指向的反向单），没有则返回 None。"""
    try:
        pair_id = row["related_order_id"]
    except (IndexError, KeyError):
        return None
    if not pair_id:
        return None
    pair = conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (pair_id,)
    ).fetchone()
    if not pair or pair["side"] != side:
        return None
    return pair


def confirm_order(conn: sqlite3.Connection, order_id: int,
                  cal: calendar.TradeCalendar | None = None) -> dict:
    """确认订单（T+1 净值公布后由调度器/启动补确认调用）。

    「未知价交易」的第二步：下单时不知道净值，此时才知道——
    算实际份额、建 lot、扣款记账。

    基金转换联动（related_order_id）：转换是「卖出 + 买入」一对单，
    两条腿各自确认会破坏「资金不过手」的等价性（只卖出不买入 = 卖飞了没买上；
    只买入不卖出 = 凭空多花一笔钱）。因此：
        - 确认**转入单（buy）**前先自查净值，再把它挂着的转出单（sell）确认掉；
          转出单暂无法确认（如净值未刷新）→ 两单都继续 pending，等下一轮；
          转出单已撤销/拒绝 → 本单一并撤销，转换不成立。
        - 确认**转出单（sell）**后，紧接着确认转入单（buy）；
          转入单无法确认 → 自动撤销转入单，赎回款正常到账（转换退回手动买入）。
    """
    cal = cal or calendar.TradeCalendar()
    cal.load()

    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": f"订单 {order_id} 不存在"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"订单状态为 {row['status']}，无需确认"}

    if row["side"] == "buy":
        # 先自查：转入单自己的净值没出来就别动转出单——两单一齐等下一轮刷新，
        # 避免「转出单已确认、转入单干等」的半转换状态。
        nav, why = _nav_for_confirm(
            conn, row["fund_code"], date.fromisoformat(row["nav_date"])
        )
        if nav is None:
            return {"ok": False, "reason": f"转入单暂无法确认，等待联动确认：{why}"}
        pair = _pair_order(conn, row, "sell")
        if pair is not None:
            if pair["status"] == "pending":
                # 先确认转出单：资金先落袋，转入单才有钱可扣（转换顺序不可反）
                try:
                    r = _confirm_core(conn, pair, cal)
                except Exception as exc:  # noqa: BLE001  配对单异常不外抛成主单 500
                    r = {"ok": False,
                         "reason": f"配对单确认异常：{type(exc).__name__}: {exc}"}
                if not r.get("ok"):
                    return {"ok": False,
                            "reason": f"转出单暂无法确认，本单等待联动确认：{r.get('reason')}"}
            elif pair["status"] in ("cancelled", "rejected"):
                _zh = {"cancelled": "撤销", "rejected": "拒绝"}
                _do_cancel(conn, order_id)
                return {"ok": False,
                        "reason": f"转出单已{_zh.get(pair['status'], pair['status'])}，"
                                  f"转换不成立，本单一并撤销"}

    result = _confirm_core(conn, row, cal)
    if not result.get("ok"):
        return result

    if row["side"] == "sell":
        pair = _pair_order(conn, row, "buy")
        if pair is not None and pair["status"] == "pending":
            try:
                r = _confirm_core(conn, pair, cal)
            except Exception as exc:  # noqa: BLE001  配对单异常不外抛成主单 500
                r = {"ok": False,
                     "reason": f"配对单确认异常：{type(exc).__name__}: {exc}"}
            if not r.get("ok"):
                # 转入单确认不了 → 自动撤销，赎回款正常到账（转换退回手动买入）
                _do_cancel(conn, pair["order_id"])
                result["note"] = "转入单无法确认已自动撤销，赎回款正常到账"
    return result


def _confirm_core(conn: sqlite3.Connection, row, cal) -> dict:
    """确认一笔订单的核心动作：算净值、算份额、扣款/到账、建/耗批次、改状态。

    与 `confirm_order` 分开：基金转换的两条腿要互相确认，
    联动逻辑（先确认谁、失败怎么办）全部放在 `confirm_order`，这里只跑单笔。
    """
    order_id = row["order_id"]
    fund_code = row["fund_code"]
    nav_date = date.fromisoformat(row["nav_date"])
    meta = _fund_meta(conn, fund_code)

    nav, why = _nav_for_confirm(conn, fund_code, nav_date)
    if nav is None:
        return {"ok": False, "reason": why}

    if row["side"] == "sell":
        shares = row["amount"]
        # 提交后到确认前，份额可能被其他赎回单消耗、或 FIFO 头部换成了未解禁的新批次，
        # 这里复校：份额要够，且要消耗的批次都已过最短持有期。
        preview = ledger.preview_consume_lots(conn, fund_code, shares)
        got = sum(p["shares"] for p in preview["consumed"])
        if got + 1e-3 < shares:
            return {"ok": False,
                    "reason": "持仓份额已不足（提交后被其他订单消耗），无法确认"}
        hold_days = _hold_period_days(conn, fund_code)
        if hold_days > 0:
            locked = _locked_lots(preview, hold_days)
            if locked:
                return {"ok": False,
                        "reason": f"按先进先出会消耗的批次仍在 {hold_days} 天持有期内，"
                                  f"最早 {locked[0]['unlock_date']} 解禁，无法确认"}

    with conn:
        if row["side"] == "buy":
            amount = row["amount"]
            # funds.purchase_fee_rate 已是 akshare 折后费率，传 already_discounted=True 防二次打折
            # calc_purchase 返回 dict，按字段取值（返回类型是 dict 非元组）
            _cp = fees.calc_purchase(fund_code, amount, already_discounted=True)
            fee, net = _cp["fee"], _cp["net_amount"]
            # 份额 = 净额 / 成交净值，Decimal 计算后量化到 4 位落库
            shares = _m.shares(_m.div(net, nav))

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
            result = {
                "ok": True, "order_id": order_id, "side": "buy",
                "nav": round(nav, 4), "shares": round(shares, 4),
                "fee": round(fee, 2), "lot_id": lot_id,
                "confirm_date": confirm_date.isoformat(),
            }
        else:
            # 赎回
            shares = row["amount"]
            # ⚠️ commit=False：`_confirm_core` 用 `with conn:` 包住整笔业务（扣款+消耗批次+改订单状态），
            # 内部函数若自行 commit 会把事务切碎 —— 后续步骤失败时前面已落库、无法整体回滚。
            consumed = ledger.consume_lots(conn, fund_code, shares, nav_date, nav, commit=False)
            gross = _m.money(sum(_m.D(c["amount"]) for c in consumed))
            total_fee = _m.money(sum(_m.D(c["fee"]) for c in consumed))
            net_amount = _m.money(_m.D(gross) - _m.D(total_fee))
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
            result = {
                "ok": True, "order_id": order_id, "side": "sell",
                "nav": round(nav, 4), "shares": round(shares, 4),
                "gross": round(gross, 2), "fee": round(total_fee, 2),
                "net_amount": round(net_amount, 2),
                "penalty": penalty, "arrival_date": arrival.isoformat(),
                "consumed_lots": consumed,
            }
    return result


def _do_cancel(conn: sqlite3.Connection, order_id: int) -> None:
    """把一笔 pending 订单置为 cancelled，释放配额，并连带撤掉转换配对单。

    转换的两条腿是一件事：撤掉转出单而转入单还挂着，会把「份额直接换」
    变成「只卖不买」——故仍 pending 的配对单一并撤销。
    引擎内部回滚（转换提交失败、确认联动失败）走本函数，不受截单窗口约束；
    用户撤单走 cancel_order（有截单窗口）。
    """
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not row or row["status"] != "pending":
        return
    opposite = "buy" if row["side"] == "sell" else "sell"
    conn.execute("UPDATE orders SET status='cancelled' WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM quota_usage WHERE ref_order_id = ?", (order_id,))
    pair = _pair_order(conn, row, opposite)
    if pair is not None and pair["status"] == "pending":
        conn.execute(
            "UPDATE orders SET status='cancelled' WHERE order_id=?", (pair["order_id"],)
        )
        conn.execute("DELETE FROM quota_usage WHERE ref_order_id = ?", (pair["order_id"],))
    conn.commit()


def cancel_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """撤单。仅限**提交当日 15:00 前**的未确认订单（全渠道一致的截单规则）。"""
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "订单不存在"}
    if row["status"] != "pending":
        return {"ok": False, "reason": f"订单已 {row['status']}，无法撤销"}
    # 截单窗口：只有提交当天、且未过 15:00 才能撤——次日已进入 T+1 确认流程。
    # 往日的挂单撤不掉；确认不了又清不掉的记录走 delete_order 的清理通道。
    # 「当天」按本机时区判定，与下单时写入的 submit_date 同一时区口径。
    today = datetime.now().date().isoformat()
    if row["submit_date"] != today:
        return {"ok": False, "reason": "只有提交当日可以撤单（次日已进入确认流程）"}
    if datetime.now().time() >= calendar.CUTOFF_TIME:
        return {"ok": False,
                "reason": f"已过当日 {calendar.CUTOFF_TIME.strftime('%H:%M')} 截单时间，不能撤单"}
    _do_cancel(conn, order_id)
    return {"ok": True, "order_id": order_id, "note": "已撤销并释放配额占用"}


def delete_order(conn: sqlite3.Connection, order_id: int) -> dict:
    """删除订单**记录**（仅限未确认的单）。

    已确认/已结算的单不能删——它们已产生份额批次或资金流水，删除会破坏账实一致。
    因此只允许删除 pending / cancelled / rejected 的单，且该单不能挂有任何份额批次。

    pending 单的删除等同撤单（都是放掉一笔未成交的承诺），故同样受截单窗口约束：
    当日 15:00 前可直接删；当日截单后不能删（请走撤单规则）；
    往日的挂单记录仍可删——那些单撤单窗口已过，删除是它们的清理通道。
    """
    row = conn.execute(
        "SELECT status, submit_date FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "订单不存在"}
    status = row["status"]
    if status in ("confirmed", "settled"):
        return {"ok": False, "reason": f"订单已 {status}，不能删除（会影响持仓与资金账实一致）"}
    if status == "pending" and row["submit_date"] == datetime.now().date().isoformat():
        if datetime.now().time() >= calendar.CUTOFF_TIME:
            return {"ok": False,
                    "reason": f"已过当日 {calendar.CUTOFF_TIME.strftime('%H:%M')} 截单时间，"
                              f"当日挂单不能删除（等同撤单）；往日挂单记录可直接删除"}
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM lots WHERE order_id = ?", (order_id,)
    ).fetchone()["n"]
    if n:
        return {"ok": False, "reason": "该订单已产生份额批次，不能删除"}
    conn.execute("DELETE FROM quota_usage WHERE ref_order_id = ?", (order_id,))
    conn.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    conn.commit()
    return {"ok": True, "order_id": order_id, "note": "已删除订单记录"}


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
    nav = _latest_nav(conn, from_code) or 0
    amount = round(shares * nav, 2)
    if amount <= 0:
        _do_cancel(conn, sell_id)
        return {"ok": False, "reason": "无法估算转出金额（缺少净值），已撤销转出单"}

    # 3) 转入：按估算金额申购
    r_buy = submit_order(
        conn, fund_code=to_code, side="buy", amount=amount,
        reason=f"基金转换转入 ← {from_code}" + (f"；{reason}" if reason else ""),
        decision_source=decision_source, cal=cal, purpose="convert",
    )
    if not r_buy.get("ok"):
        # 转入失败 → 撤销转出单，保证不会只卖不买。
        # 走 _do_cancel 而非 cancel_order：这是引擎内部回滚，不受用户截单窗口约束。
        _do_cancel(conn, sell_id)
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
    for lot in ledger.holding_report(conn, code, datetime.now().date()):
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
    """订单列表（按 order_id 倒序）。status 可选过滤。供 /api/orders。

    订单表自身不存基金名称，fund_name 从 funds 表 LEFT JOIN 取，
    这样历史订单与新订单都能显示基金名称，而非只有编号。
    """
    sql = (
        "SELECT o.order_id, o.order_no, o.fund_code, "
        "COALESCE(f.name, o.fund_code) AS fund_name, "
        "o.side, o.amount, o.shares, o.submit_date, o.submit_time, "
        "o.nav_date, o.confirm_date, o.arrival_date, o.status, "
        "o.reject_reason, o.decision_source, o.reason, o.fee_purchase, "
        "o.fee_redeem, o.penalty_flag, o.deal_nav, o.deal_shares, "
        "o.deal_amount, o.related_order_id, o.created_at, o.updated_at "
        "FROM orders o LEFT JOIN funds f ON f.code = o.fund_code"
    )
    params: list = []
    if status:
        sql += " WHERE o.status = ?"
        params.append(status)
    sql += " ORDER BY o.order_id DESC LIMIT ?"
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
