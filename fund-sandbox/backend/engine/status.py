"""基金交易状态与海外市场节假日。

这一管的东西都不是静态配置，而是**每天会变的**：

    限购 → 基金公司按规模/外汇额度随时调整，今天限 1 万明天可能限 1 千
    暂停申购 → 可能突然发公告，也可能突然恢复
    封闭期 → 新基金成立初期，不能买也不能卖
    持有期 → 能买，但每笔份额锁定期满才能卖（如 1 年持有期）
    定期开放 → 只在固定开放期能申赎，错过等下一期
    海外节假日 → QDII 因境外市场休市而暂停，日历性、可预期

前五类靠每日抓取状态字段获取（fund_open_fund_daily_em 的申购状态/赎回状态）。
最后一类靠境外市场节假日日历计算。

**这两类不能混为一谈**：限购是规模控制手段，海外节假日是日历性休市，
处置逻辑完全不同——前者要校验金额，后者要校验日期。

关于数据真实性：节假日全部按公开规则计算（美股用公历规则可精确算出），
不编造、不用假数据填充。港股农历节日需要农历库，当前仅覆盖公历固定节日，
已明确标注不完整，宁可漏报也不谎报。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from backend.config import get
from backend.data import fetcher

# 快照日期、QDII 日历判断统一按本机时区——与下单、订单号同一口径。
def _today() -> date:
    return datetime.now().date()

# 下单路径的「交易状态」抓取硬超时（秒）。
# 交易状态来自全市场每日净值大表（ak.fund_open_fund_daily_em：一次返回约 2.4 万只、
# 实拉约 28s）。应用启动后会后台预热、运行期每 9 分钟续热，正常下单直接命中热缓存、
# 秒回不阻塞；此超时仅作冷启动/续热间隙兜底——必须大于真实拉取耗时（约 28s），
# 否则超时返回空、库内又无历史快照时会直接拒单。
# 可在 config.toml 的 [data_quality] order_status_timeout 调。
_ORDER_STATUS_TIMEOUT = float(get("data_quality", "order_status_timeout", 60.0))

# 历史状态快照的**最长可采信天数**：
# 实时抓取失败时，超过这个天数的快照不再复用 —— 申购状态在真实世界**随时可变**
# （限购额每日调整、暂停/恢复频繁），用过期快照判断会**误拒**（旧快照写"暂停申购"、
# 实际早已恢复开放）或**误放行**（旧快照"开放"、实际已暂停）。
_MAX_SNAPSHOT_AGE_DAYS = int(get("data_quality", "status_snapshot_max_age_days", 7))


# =============================================================================
# 境外市场节假日
# =============================================================================


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """取某月第 n 个星期几。weekday: 0=周一 ... 6=周日。n=-1 表示最后一个。"""
    if n > 0:
        d = date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + (n - 1) * 7)
    # 最后一个：取该月最后一天再往前找
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """节假日落在周末时的调整规则：周六→前一周五，周日→后一周一。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def easter(year: int) -> date:
    """复活节日期（Anonymous Gregorian algorithm，公历通用算法）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    """美股休市日。全部是公历规则，可精确计算。

    覆盖纽交所/纳斯达克的九个主要休市日。
    注意：实际休市安排以交易所公告为准（如临时哀悼日会加休），
    这里覆盖的是每年固定的部分。
    """
    hs = {
        _observed(date(year, 1, 1)),          # 元旦
        _nth_weekday(year, 1, 0, 3),          # 马丁·路德·金日（1月第3周一）
        _nth_weekday(year, 2, 0, 3),          # 总统日（2月第3周一）
        easter(year) - timedelta(days=2),     # 耶稣受难日
        _nth_weekday(year, 5, 0, -1),         # 阵亡将士纪念日（5月最后周一）
        _observed(date(year, 7, 4)),          # 独立日
        _nth_weekday(year, 9, 0, 1),          # 劳动节（9月第1周一）
        _nth_weekday(year, 11, 3, 4),         # 感恩节（11月第4周四）
        _observed(date(year, 12, 25)),        # 圣诞节
    }
    return hs


def hk_market_holidays(year: int) -> set[date]:
    """港股休市日（**不完整，仅覆盖公历固定节日**）。

     limitation：春节、清明、端午、中秋等农历节日需要农历数据才能计算，
     当前未实现。宁可漏报也不编造——漏报只是少提醒一次，
     谎报一个不存在的休市日会导致错误地禁止交易。

     若要补全，可引入农历库（如 lunardate）后扩展本函数。
    """
    return {
        date(year, 1, 1),    # 元旦
        date(year, 5, 1),    # 劳动节
        date(year, 7, 1),    # 香港特别行政区成立纪念日
        date(year, 10, 1),   # 国庆节
        date(year, 12, 25),  # 圣诞节
        date(year, 12, 26),  # 节礼日
    }


def is_overseas_holiday(d: date, market: str = "us") -> bool:
    """某日是否为境外市场休市日。market: us | hk；其余取值视为非休市日。"""
    if market == "us":
        return d in us_market_holidays(d.year)
    if market == "hk":
        return d in hk_market_holidays(d.year)
    return False


def next_overseas_holiday(d: date, market: str = "us") -> date | None:
    """返回 d 之后最近的境外休市日。用于提前提示 QDII 将暂停交易。

    market 只认 us / hk；其余取值（含 None）没有可查的假日表，返回 None。
    """
    if market not in ("us", "hk"):
        return None
    hs = (
        us_market_holidays(d.year) | us_market_holidays(d.year + 1)
        if market == "us"
        else hk_market_holidays(d.year) | hk_market_holidays(d.year + 1)
    )
    future = sorted(h for h in hs if h > d)
    return future[0] if future else None


# =============================================================================
# 状态解析
# =============================================================================

PURCHASE_CLOSED_KEYS = ["暂停申购", "停止申购", "封闭期", "暂停交易", "不可申购"]
PURCHASE_LIMIT_KEYS = ["限大额", "限额", "限制金额", "限购"]
PURCHASE_OPEN_KEYS = ["开放申购", "开放"]
REDEEM_CLOSED_KEYS = ["暂停赎回", "停止赎回", "封闭期", "不可赎回", "暂停交易"]
REDEEM_OPEN_KEYS = ["开放赎回", "开放"]

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万|亿元|亿|元)")


def _extract_limit(text: str) -> float | None:
    """从状态文本里提取限购金额。

    例："限大额（单日累计申购金额不超过 1000 元）" → 1000.0
    提取不到返回 None —— 这时必须保守处理（提示人工确认），
    不能当作"无限额"。
    """
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "万" or unit == "万元":
        return val * 10_000
    if unit == "亿" or unit == "亿元":
        return val * 100_000_000
    return val


def parse_status(raw_purchase: str, raw_redeem: str) -> dict:
    """把接口返回的原始状态文本解析成结构化状态。

    返回：
        {
          purchase: open|limited|closed|unknown,
          redeem:   open|closed|unknown,
          purchase_limit: 金额或 None,
          limit_unknown: True 表示识别为限购但拿不到具体金额
        }
    """
    p = (raw_purchase or "").strip()
    r = (raw_redeem or "").strip()

    if any(k in p for k in PURCHASE_CLOSED_KEYS):
        purchase = "closed"
    elif any(k in p for k in PURCHASE_LIMIT_KEYS):
        purchase = "limited"
    elif any(k in p for k in PURCHASE_OPEN_KEYS):
        purchase = "open"
    else:
        purchase = "unknown"

    if any(k in r for k in REDEEM_CLOSED_KEYS):
        redeem = "closed"
    elif any(k in r for k in REDEEM_OPEN_KEYS):
        redeem = "open"
    else:
        redeem = "unknown"

    limit = _extract_limit(p) if purchase == "limited" else None
    return {
        "purchase": purchase,
        "redeem": redeem,
        "purchase_limit": limit,
        "limit_unknown": purchase == "limited" and limit is None,
        "raw_purchase": p,
        "raw_redeem": r,
    }


# =============================================================================
# 下单前校验
# =============================================================================


def can_buy(st: dict, amount: float,
            fallback_limit: float | None = None) -> tuple[bool, str]:
    """能否买入指定金额。

    fallback_limit：人工维护的限购额。
    接口返回的申购状态只有「限大额」三个字，不含具体金额，
    而限购额是真实约束，不能当它不存在。让人把查到的限额维护进配置，
    系统优先用接口解析值、其次用人工值，都没有才拒绝。
    """
    if st["purchase"] == "closed":
        return False, f"该基金当前暂停申购或处于封闭期（{st['raw_purchase']}），不能买入"
    if st["purchase"] == "unknown":
        return False, f"申购状态未知（{st['raw_purchase'] or '空'}），拒绝下单（拿不到就不猜）"
    if st["purchase"] == "limited":
        # 优先用接口解析出的金额，解析不到再退回人工维护值。
        # 两者都没有才拒绝 —— 限购是真实约束，不能当它不存在，
        # 但也不能因为拿不到数字就完全无法交易。
        # 注意用 is None 判断而非 or：接口解析出的 0（限购 0 元）是有效值，
        # 不能被 or 当成「没解析到」而误用人工兜底值。
        limit = st["purchase_limit"]
        if limit is None:
            limit = fallback_limit
        if limit is None:
            return False, (
                f"该基金限购，但接口未给出金额且未配置人工限额（{st['raw_purchase']}）。"
                f"请在 config.toml 的 [[funds]] 中填写 purchase_limit "
                f"（从支付宝基金页面核对实际公示），否则拒绝下单"
            )
        if limit <= 0:
            # 限额为 0 或负数 = 实际不可申购（数据异常或已停止受理）。
            # 不能因为 amount > limit 恰好成立就复用后面的金额比较话术，
            # 这里给出准确理由。
            return False, (
                f"该基金限购额为 {limit}（非正数，实际不可申购或数据异常），不能买入"
            )
        if amount > limit:
            return False, f"申购金额 {amount:.2f} 元超过单日限额 {limit:.0f} 元"
    return True, "ok"


def can_sell(st: dict) -> tuple[bool, str]:
    """基金级赎回状态门禁：暂停赎回 / 封闭期 / 状态未知一律拒绝。

    按批次的**最短持有期锁定不在这里判**——本函数拿不到批次数据，
    由 `orders.check_before_submit`（提交时）与 `orders._confirm_core`
    （确认时）按 FIFO 预览逐批校验（hold_period_days + 批次确认日）。
    """
    if st["redeem"] == "closed":
        return False, f"该基金当前暂停赎回或处于封闭期（{st['raw_redeem']}），不能卖出"
    if st["redeem"] == "unknown":
        return False, f"赎回状态未知（{st['raw_redeem'] or '空'}），拒绝下单（拿不到就不猜）"
    return True, "ok"


def check_qdii_calendar(fund_code: str, is_qdii: bool, d: date,
                        market: str = "us") -> dict:
    """QDII 海外节假日检查。

    与限购/暂停申购是**不同维度**的问题：
    这里是境外市场休市导致的交易日历暂停，日历性、可预期，
    应提前提示而不是等到下单失败。
    market 只认 us / hk，其余取值（含 None）视为不适用。
    """
    if not is_qdii:
        return {"applicable": False, "halted": False}
    if market not in ("us", "hk"):
        return {"applicable": False, "halted": False}
    halted = is_overseas_holiday(d, market)
    nxt = next_overseas_holiday(d, market)
    return {
        "applicable": True,
        "market": market,
        "halted": halted,
        "next_holiday": nxt.isoformat() if nxt else None,
        "days_to_next": (nxt - d).days if nxt else None,
        "detail": (
            f"境外市场（{market.upper()}）今日休市，该 QDII 暂停交易"
            if halted
            else f"境外市场正常；下次休市 {nxt}（{ (nxt - d).days if nxt else '?'} 天后）"
        ),
    }


# =============================================================================
# 抓取与落库
# =============================================================================


def _status_from_row(row, is_qdii: bool, market: str) -> dict:
    """把 fund_status 表里的一行结构化状态，转成与实时抓取一致的返回结构。

    实时抓取返回的是「开放申购」这类原始文本，表里存的是已解析的
    open/closed/limited + 限额 + note（note 里带了原始文本）。
    这里回填原始文本，保证 can_buy/can_sell 在降级路径下也能给出可读的拒绝理由。
    """
    # 兼容 sqlite3.Row 与 dict 两种入参
    row = dict(row) if not isinstance(row, dict) else row
    note = row.get("note") or ""
    raw_purchase = _extract_raw_from_note(note, "申购") or ""
    raw_redeem = _extract_raw_from_note(note, "赎回") or ""
    purchase = row.get("purchase_status") or "unknown"
    redeem = row.get("redeem_status") or "unknown"
    # QDII 日历按**快照日期**判断（而不是今天）：缓存快照回答的是
    # 「status_date 那天海外是否休市」，与 purchase/redeem 同一天的口径；
    # status_date 缺失才退今天。market 归一化，None/未知值按不适用处理。
    snap = row.get("status_date")
    try:
        snap_d = date.fromisoformat(str(snap)[:10]) if snap else _today()
    except ValueError:
        snap_d = _today()
    return {
        "purchase": purchase,
        "redeem": redeem,
        "purchase_limit": row.get("purchase_limit"),
        "limit_unknown": purchase == "limited" and row.get("purchase_limit") is None,
        "raw_purchase": raw_purchase,
        "raw_redeem": raw_redeem,
        "qdii": check_qdii_calendar(row.get("fund_code", ""), is_qdii, snap_d, market),
        "from_cache": True,
    }


def _extract_raw_from_note(note: str, label: str) -> str | None:
    """从 fund_status.note 的「申购[开放申购]」里抠出原始文本。"""
    key = f"{label}["
    i = note.find(key)
    if i < 0:
        return None
    j = note.find("]", i)
    if j < 0:
        return None
    return note[i + len(key):j]


def fetch_and_save(conn: sqlite3.Connection, fund_code: str, is_qdii: bool = False,
                   market: str = "us", timeout: float | None = None) -> dict | None:
    """抓取一只基金的交易状态并落库，返回解析后的状态。

    抓不到时的降级策略（关键，避免「下单没反应」）：
        1) 今天已经有快照 → 直接复用，绝不碰网络（下单路径因此永远秒回）；
        2) 实时抓取失败/超时 → 退而用够新的历史快照（天数上限见
           _MAX_SNAPSHOT_AGE_DAYS），标注 from_cache，
           让「源站抖动」不至于把一笔本来能做的交易硬拒掉；
        3) 连历史快照都没有（全新基金、从未抓过）→ 才返回 None，
           由调用方拒绝下单（拿不到就不猜，但这是真正的未知，不是网络抖动）。

    timeout：实时抓取硬超时（秒）。传 None 用 _ORDER_STATUS_TIMEOUT（默认 60s）。
    网络不可达时这个时间到了就立刻走降级，不再让同步下单请求干等全局 25s。
    """
    today = _today().isoformat()

    # 0) 优先用今天已有的快照——下单是高频同步操作，不应为它再拉一次全市场表
    cached_today = conn.execute(
        "SELECT * FROM fund_status WHERE fund_code = ? AND status_date = ? "
        "ORDER BY id DESC LIMIT 1",
        (fund_code, today),
    ).fetchone()
    if cached_today:
        return _status_from_row(cached_today, is_qdii, market)

    # 1) 实时抓取
    eff_timeout = timeout if timeout is not None else _ORDER_STATUS_TIMEOUT
    try:
        raw = fetcher.fetch_fund_status(fund_code, timeout=eff_timeout)
    except Exception as exc:  # noqa: BLE001  网络/源站异常当作「抓不到」，降级处理，不让下单接口 500
        print(f"[status] {fund_code} 状态抓取异常: {type(exc).__name__}: {exc}")
        raw = None
    if raw:
        st = parse_status(raw.get("purchase_status", ""), raw.get("redeem_status", ""))
        qd = check_qdii_calendar(fund_code, is_qdii, _today(), market)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO fund_status
                (fund_code, status_date, purchase_status, redeem_status,
                 purchase_limit, raw_fee, note, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fund_code,
                today,
                st["purchase"],
                st["redeem"],
                st["purchase_limit"],
                raw.get("fee_raw", ""),
                f"原始状态：申购[{st['raw_purchase']}] 赎回[{st['raw_redeem']}]"
                + (f"；{qd['detail']}" if qd["applicable"] else ""),
                "em_daily",
                now,
            ),
        )
        conn.commit()
        return {**st, **raw, "qdii": qd}

    # 2) 实时没抓到（网络抖动/被限流）——退而用历史快照，**但必须够新**。
    #    申购状态随时可变，用过期快照判断会误拒或误放行。故设时效上限，
    #    超过 `_MAX_SNAPSHOT_AGE_DAYS` 天即视为不可信 → 宁可返回 None 让调用方拒绝
    #    （「拿不到就不猜」），也不拿过期数据替用户做交易决定。
    cutoff = (_today() - timedelta(days=_MAX_SNAPSHOT_AGE_DAYS)).isoformat()
    cached_any = conn.execute(
        "SELECT * FROM fund_status WHERE fund_code = ? AND status_date >= ? "
        "ORDER BY status_date DESC, id DESC LIMIT 1",
        (fund_code, cutoff),
    ).fetchone()
    if cached_any:
        print(
            f"[status] {fund_code} 实时抓取失败，复用 {cached_any['status_date']} 的快照"
            f"（{_MAX_SNAPSHOT_AGE_DAYS} 天内），标注 from_cache"
        )
        return _status_from_row(cached_any, is_qdii, market)

    # 3) 真·未知（连够新的快照都没有）：返回 None，由调用方拒绝下单
    return None


def latest_status(conn: sqlite3.Connection, fund_code: str) -> dict | None:
    """取最近一次状态快照。"""
    row = conn.execute(
        """
        SELECT * FROM fund_status WHERE fund_code = ?
        ORDER BY status_date DESC, id DESC LIMIT 1
        """,
        (fund_code,),
    ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    from backend.config import test_funds
    from backend.db import get_connection, init_db

    print("=" * 78)
    print("基金交易状态演示（真实数据）")
    print("=" * 78)

    init_db()
    conn = get_connection()

    for f in test_funds():
        code, name = f["code"], f["name"]
        st = fetch_and_save(conn, code, f.get("is_qdii", False))
        if not st:
            print(f"\n  {code} {name}: 状态抓取失败 → 应拒绝下单")
            continue
        print(f"\n  {code} {name}")
        print(f"    申购状态: {st['purchase']:<8} 限额 {st['purchase_limit']}")
        print(f"    赎回状态: {st['redeem']}")
        print(f"    原始文本: 申购[{st['raw_purchase']}] 赎回[{st['raw_redeem']}]")
        if st["limit_unknown"]:
            print("    ⚠ 识别为限购但拿不到具体金额，下单前需人工确认")
        if st["qdii"]["applicable"]:
            print(f"    海外日历: {st['qdii']['detail']}")

        for amt in (100.0, 100_000.0):
            ok, why = can_buy(st, amt)
            print(f"    买入 {amt:>9.0f} 元 → {'允许' if ok else '拒绝'}：{why}")

    print("\n" + "=" * 78)
    print("境外市场节假日（按公历规则计算，非编造）")
    print("=" * 78)
    y = _today().year
    print(f"\n  美股 {y} 年休市日（前 10 个）：")
    for d in sorted(us_market_holidays(y))[:10]:
        print(f"    {d}  {['周一','周二','周三','周四','周五','周六','周日'][d.weekday()]}")
    nxt = next_overseas_holiday(_today(), "us")
    print(f"\n  下一个美股休市日：{nxt}（{(nxt - _today()).days} 天后）")
    print("  港股仅覆盖公历固定节日，农历节日（春节/清明/端午/中秋）未实现 —— 宁可漏报不谎报")

    conn.close()
