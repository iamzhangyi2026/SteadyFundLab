"""A 股交易日历与交易时点判定。

为什么不需要自己维护节假日表：
    交易所的休市安排（法定节假日 + 调休补班）每年由交易所公布，手工维护极易出错且
    需要每年更新。akshare 的 tool_trade_date_hist_sina 直接返回交易所的官方交易日历，
    已经包含节假日和调休信息，比自己硬编码可靠得多。

    本模块的做法是：优先取交易所官方日历 → 缓存到本地 → 网络不通时用缓存 →
    缓存也没有时降级为「仅排除周末」并标记 degraded。

为什么要标记 degraded：
    降级状态下无法识别法定节假日，会把春节、国庆当成交易日。这属于「静默失败」的
    高危场景，必须让调用方知道当前日历不可信，而不是默默返回一个看似正常的答案。

15:00 截单规则（支付宝/天天基金/所有渠道一致）：
    T 日 15:00 前下单 → 按 T 日净值成交
    T 日 15:00 后下单 → 顺延到 T+1 交易日，按 T+1 净值成交
    非交易日下单     → 顺延到下一交易日
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..config import get, section

# 15:00 截单时刻。这是全行业统一的分水岭，不是某个平台的特殊规则
CUTOFF_TIME = time(15, 0)

# 截单、开市门控等「当前时刻」判定统一按本机时区（datetime.now()）解释。


def _redeem_arrival_days(category: str = "") -> int:
    """赎回资金到账延迟天数（交易日）——规则一律来自 config，代码不写死。

    读取顺序（对应 config [trading]）：
        1. redeem_arrival_rules：[{keyword, days}...]，按序匹配基金「类型」字段，命中即用
        2. redeem_arrival_days ：所有规则都没命中时的兜底
        3. 代码级最後兜底 3（仅当 config 缺键，正常不会发生）

    为什么不写死在代码里：各品类清算周期差异极大（货币 T+1、纯债 T+2、混合/股票/指数
    T+3、QDII T+5），且具体以基金合同为准。写死会导致改 config 不生效，
    违反「规则来源约定：config/config.toml 才是权威`。

    单一来源：arrival_date_by_category() 与 arrival_date() 共用本函数，避免各写一套走偏。
    """
    rules = section("trading", {}).get("redeem_arrival_rules", []) or []
    fallback = int(get("trading", "redeem_arrival_days", 3))
    c = (category or "").strip()
    for r in rules:
        kw = str(r.get("keyword") or "").strip()
        # 大小写不敏感：类型字段里 QDII 常写作 QDII / qdii 两种形态
        if kw and kw.lower() in c.lower():
            return int(r.get("days", fallback))
    return fallback

# 交易日历缓存文件
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "trade_calendar.json"


class TradeCalendar:
    """交易日历。

    用法：
        cal = TradeCalendar()
        cal.load()                          # 加载（网络优先，缓存兜底）
        cal.is_trading_day(date(2026, 8, 28))
        cal.resolve_nav_date(datetime(...))  # 考虑 15:00 截单后的成交净值日
    """

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or DEFAULT_CACHE
        self._days: set[str] = set()          # 交易日集合，元素为 "YYYY-MM-DD"
        self.source: str = "unloaded"         # sina(网络) | cache | weekend_fallback
        self.degraded: bool = False           # True 表示日历不可信（仅排除了周末）
        self.loaded: bool = False
        # 缓存新鲜度上限（天）：超过则不再直接采信，转走网络刷新
        self.max_cache_age_days = 30

    # ------------------------------------------------------------------ 加载

    def load(self, force_refresh: bool = False) -> None:
        """加载交易日历。

        优先级：
            1. 网络（akshare 取交易所官方日历）→ 成功后写入缓存
            2. 本地缓存
            3. 兜底：仅排除周末，置 degraded=True

        为什么网络失败不直接抛异常：
            本地软件应该尽量可用。日历降级时后续调用仍能跑，只是结果不可信，
            界面上会明确标出 degraded 状态，由人或 AI 决定是否采信。
        """
        if not force_refresh and self._load_cache():
            self.source = "cache"
            self.loaded = True
            return

        if self._load_from_network():
            self._save_cache()
            self.source = "sina"
            self.degraded = False
            self.loaded = True
            return

        # 网络与缓存都不可用，降级
        self.source = "weekend_fallback"
        self.degraded = True
        self.loaded = True

    def _load_from_network(self) -> bool:
        """从 akshare 拉取交易所官方交易日历。

        返回 True 表示成功。任何异常都吞掉并返回 False——
        抓取失败是常态（限流、改版、断网），不该让整个系统崩掉。
        """
        try:
            import akshare as ak  # 局部导入：没装 akshare 时本模块仍可降级使用
        except ImportError:
            return False

        try:
            df = ak.tool_trade_date_hist_sina()
            # 返回结构是单列 DataFrame，列名 trade_date，值为 datetime
            days = {d.strftime("%Y-%m-%d") for d in df["trade_date"]}
            if len(days) < 100:  # 少于 100 天说明数据异常，拒绝使用
                return False
            self._days = days
            return True
        except Exception:
            return False

    def _load_cache(self) -> bool:
        """从本地缓存加载（**带新鲜度检查**）。

        超过 `max_cache_age_days` 天未更新的缓存不再直接采信，转走网络刷新
        （网络也不可用时仍会落到 `weekend_fallback` 降级，并在界面上标 `degraded`）。
        """
        if not self.cache_path.exists():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            days = set(data.get("trading_days", []))
            if len(days) < 100:
                return False
            updated_at = data.get("updated_at")
            if updated_at:
                try:
                    age_days = (datetime.now() - datetime.fromisoformat(updated_at)).days
                    if age_days > self.max_cache_age_days:
                        print(
                            f"[calendar] 缓存已 {age_days} 天未更新"
                            f"（上限 {self.max_cache_age_days} 天），改走网络刷新"
                        )
                        return False
                except Exception:  # noqa: BLE001 时间戳解析失败则不拦（保持原行为）
                    pass
            self._days = days
            self.degraded = data.get("degraded", False)
            return True
        except Exception:
            return False

    def _save_cache(self) -> None:
        """写入本地缓存。"""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self._days),
            "degraded": self.degraded,
            "trading_days": sorted(self._days),
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    # -------------------------------------------------------------- 基础查询

    def _ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    @staticmethod
    def _key(d: date | datetime) -> str:
        if isinstance(d, datetime):
            d = d.date()
        return d.strftime("%Y-%m-%d")

    def is_trading_day(self, d: date | datetime) -> bool:
        """是否为交易日。

        日历降级时退化为「非周末即交易日」，此时无法识别法定节假日，
        调用方应检查 self.degraded 后再决定是否采信结果。
        """
        self._ensure_loaded()
        if self.source == "weekend_fallback":
            if isinstance(d, datetime):
                d = d.date()
            return d.weekday() < 5
        return self._key(d) in self._days

    def next_trading_day(self, d: date, n: int = 1) -> date:
        """返回 d 之后的第 n 个交易日（d 本身是交易日则不计入）。

        n=1 即「下一个交易日」，用于 15:00 后顺延、T+1 份额确认等场景。
        """
        self._ensure_loaded()
        cur = d
        remaining = n
        guard = 0
        while remaining > 0:
            cur = cur + timedelta(days=1)
            if self.is_trading_day(cur):
                remaining -= 1
            guard += 1
            if guard > 60:  # 死循环保护：连续 60 天都不是交易日，说明数据有问题
                raise RuntimeError(
                    "交易日历异常：连续 60 天未找到交易日，请检查日历数据"
                )
        return cur

    def prev_trading_day(self, d: date) -> date:
        """返回 d 之前的最近一个交易日。"""
        self._ensure_loaded()
        cur = d - timedelta(days=1)
        guard = 0
        while not self.is_trading_day(cur):
            cur = cur - timedelta(days=1)
            guard += 1
            if guard > 60:
                raise RuntimeError(
                    "交易日历异常：连续 60 天未找到交易日，请检查日历数据"
                )
        return cur

    # ---------------------------------------------------------- 交易时点判定

    def resolve_nav_date(self, submit_dt: datetime) -> date:
        """根据下单时刻，确定这笔订单按哪天的净值成交。

        规则（全渠道一致）：
            - 交易日且 15:00 前 → 当日净值
            - 交易日但 15:00 后 → 顺延至下一交易日
            - 非交易日          → 顺延至下一交易日

        关于节假日能不能下单（最容易混淆的一点）：
            **能提交，但不成交。** 支付宝、天天基金等平台 7×24 都可以提交
            申购赎回申请，但周末和法定节假日提交的申请一律顺延到下一交易日，
            且节假日期间净值不更新。「能提交」不等于「当天成交」，规则没有例外。
            非交易时段提交的申请，通常可在下一个交易日 15:00 前撤销。

        注意：这里返回的是「净值日期」，不是「确认日期」。
        份额确认还要再推一个交易日（见 confirm_date）。
        """
        self._ensure_loaded()
        d = submit_dt.date()
        if self.is_trading_day(d) and submit_dt.time() < CUTOFF_TIME:
            return d
        return self.next_trading_day(d, 1)

    def describe_order_timing(self, submit_dt: datetime) -> dict:
        """描述一笔订单的时间归属，供界面提示使用。

        把「能不能提交」和「按哪天净值成交」分开讲清楚，
        避免误以为节假日下单是按当天价格成交。

        返回：
            can_submit  是否可提交（恒为 True，平台 7×24 接受申请）
            nav_date    实际成交净值日
            deferred    是否被顺延
            reason      顺延原因（「已过 15:00」/「非交易日」/空）
            hint        给界面显示的一句话说明
        """
        self._ensure_loaded()
        d = submit_dt.date()
        is_td = self.is_trading_day(d)
        before_cutoff = submit_dt.time() < CUTOFF_TIME

        if is_td and before_cutoff:
            nav_date = d
            deferred, reason = False, ""
            hint = f"交易日 15:00 前提交，按今日（{d}）净值成交"
        else:
            nav_date = self.next_trading_day(d, 1)
            deferred = True
            reason = "非交易日" if not is_td else "已过 15:00"
            hint = (
                f"可以提交，但{reason}，申请顺延至 {nav_date} 处理并按当日净值成交"
                f"（节假日期间净值不更新）"
            )

        return {
            "can_submit": True,
            "submit_date": d.isoformat(),
            "nav_date": nav_date.isoformat(),
            "deferred": deferred,
            "reason": reason,
            "hint": hint,
        }

    def arrival_date_by_category(
        self, nav_date: date, category: str = "混合型"
    ) -> date:
        """按基金品类计算赎回到账日。真实交易链路走的就是本方法（orders.py 赎回确认调用）。

        不同品类的清算周期差异很大，统一写死一个天数是错的：
            货币基金            T+1（快速赎回可实时到账，单日限额通常 1 万）
            短债 / 纯债基金     T+1 ~ T+2
            混合 / 股票 / 指数  T+3
            QDII 海外基金       T+4 ~ T+7（受跨境清算与境外休市影响）

        天数由 _redeem_arrival_days() 从 config 读：[trading].redeem_arrival_rules
        按 keyword 匹配品类、[trading].redeem_arrival_days 兜底。具体以基金合同为准。
        """
        return self.next_trading_day(nav_date, _redeem_arrival_days(category))

    def confirm_date(self, nav_date: date, delay_days: int = 1) -> date:
        """份额确认日。普通基金 T+1，QDII 因时差通常是 T+2。

        delay_days 由调用方从 config 读入后传入：
            trading.confirm_delay_days        普通场外基金（默认 1）
            trading.qdii_confirm_delay_days   QDII（默认 2）
        """
        return self.next_trading_day(nav_date, delay_days)

    def arrival_date(self, nav_date: date, delay_days: int | None = None) -> date:
        """赎回资金到账日。已知品类时请优先用 arrival_date_by_category（按品类更准）。

        不传 delay_days 时用 config [trading].redeem_arrival_days 的兜底天数。
        """
        days = _redeem_arrival_days() if delay_days is None else int(delay_days)
        return self.next_trading_day(nav_date, days)

    def is_market_open_now(self, now: datetime | None = None) -> bool:
        """当前是否处于可下单时段（交易日 15:00 前）。用于开市门控。

        默认取「当前时刻」时按本机时区解释：15:00 截单等规则都按本机时区为准。
        """
        now = now or datetime.now()
        return self.is_trading_day(now) and now.time() < CUTOFF_TIME

    # ---------------------------------------------------------------- 信息

    def info(self) -> dict:
        """返回日历状态，供数据健康度面板展示。"""
        return {
            "source": self.source,
            "degraded": self.degraded,
            "count": len(self._days),
            "cache_file": str(self.cache_path),
        }


if __name__ == "__main__":
    cal = TradeCalendar()
    cal.load()
    print(f"日历来源: {cal.source}  降级: {cal.degraded}  天数: {len(cal._days)}")

    today = datetime.now().date()
    print(f"\n今天 {today} 是否交易日: {cal.is_trading_day(today)}")
    print(f"下一交易日: {cal.next_trading_day(today)}")
    print(f"上一交易日: {cal.prev_trading_day(today)}")

    # 15:00 截单演示：同一天不同时刻下单，成交净值日不同
    d = cal.next_trading_day(today)
    morning = datetime.combine(d, time(10, 30))
    evening = datetime.combine(d, time(16, 0))
    print(f"\n交易日 {d}:")
    print(f"  10:30 下单 → 净值日 {cal.resolve_nav_date(morning)}")
    print(f"  16:00 下单 → 净值日 {cal.resolve_nav_date(evening)}（顺延）")
    print(f"  份额确认日 → {cal.confirm_date(cal.resolve_nav_date(morning))}")
    print(f"  赎回到账日 → {cal.arrival_date(cal.resolve_nav_date(morning))}")
    print(f"\n当前是否可下单: {cal.is_market_open_now()}")
