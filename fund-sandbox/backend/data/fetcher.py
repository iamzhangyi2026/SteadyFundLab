"""akshare 数据抓取 + 双源交叉验证 + 脏数据拦截。

设计原则（来自实战教训，每一条都对应踩过的坑）：

1. 单源不采信
   任何数据必须 ≥2 个独立源交叉一致才进分析层。不一致就标 VERIFY，
   宁可不用也不硬编。

2. 脏数据物理拦截
   净值必须通过合理性闸门才允许入库。历史上出现过 -599% 这种离谱值，
   闸门是最后一道防线。

3. 脚本只报异常，不猜原因
   净值单日 -40% 可能是分红（真实事件）也可能是接口返回了脏数据，
   脚本没有能力区分，只能放进异常清单交给 AI 定性。

4. 绝不静默失败
   拿不到数据就明确标「无数据」，绝不拿旧值冒充新值。
   HTTP 200 但返回 0 条，等同于失败。

接口可用性（akshare 实际，akshare 1.18.94）
------------------------------------------------
可用：
    fund_open_fund_info_em              单只基金历史净值（单位/累计）
    fund_open_fund_daily_em             全市场当日净值+申购状态+赎回状态+手续费
    stock_board_industry_summary_ths    同花顺行业板块（涨跌幅、净流入）
    fund_portfolio_hold_em              基金持仓明细（date 用 20250630 格式）
    fund_portfolio_industry_allocation_em  基金行业配置（date 用**年份**如 2025）
    tool_trade_date_hist_sina           交易日历

已失效（保留调用代码，待源站恢复后自动启用）：
    stock_board_industry_name_em        东财行业板块 — 连接被拒绝
    stock_board_concept_name_em         东财概念板块 — 连接被拒绝

⚠️ 接口会变。发现数据异常时先跑 scripts/verify_sources.py 体检，
   拿到真实接口名和列名再来改这里，不要凭记忆改。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import get

# 净值合理性闸门。区间外一律拒收
NAV_GATE_MIN = get("data_quality", "nav_min", 0.01)
NAV_GATE_MAX = get("data_quality", "nav_max", 1000.0)
CROSS_TOLERANCE = get("data_quality", "cross_tolerance_pct", 1.0)
JUMP_THRESHOLD = get("data_quality", "nav_jump_threshold_pct", 10.0)
STALE_DAYS = get("data_quality", "stale_nav_days", 3)

# 分红识别阈值：单位净值变动与累计净值变动相差多少个百分点，
# 就判定为「高度疑似分红除息」。
# 取 3 个百分点是因为：真实市场波动下两者应当同步（差值接近 0），
# 只有分红/拆分才会让差值显著拉开。分红案例中差值可达 17 个百分点。
DIVIDEND_GAP_HINT = get("data_quality", "dividend_gap_pct", 3.0)


def _import_akshare():
    """延迟导入 akshare。

    为什么延迟：本模块在没有装 akshare 的环境下也能被导入
    （比如只跑数据库测试时），导入失败不该炸掉整个包。
    """
    try:
        import akshare as ak

        return ak
    except ImportError:
        return None


# 全市场每日表的缓存。这个接口一次返回 2.4 万只基金，拉取要好几秒，
# 而净值的第二源和基金状态都要用它。不做缓存的话一次刷新要拉两遍，
# 既慢又增加被源站限流的风险。
_daily_cache: dict[str, Any] = {"df": None, "ts": 0.0}
_DAILY_CACHE_TTL = 600  # 秒。10 分钟内复用，超过则重新拉取
# 抓取串行锁：全市场表一次 2.4 万只、实拉约 28s。若不串行，启动预热线程与首单
# 并发各拉一遍会互相拖慢、抢源站，单次可能逼近 60s 超时。加锁后并发调用者等锁，
# 取锁后二次检查缓存（可能另一线程刚拉完），命中即复用，避免重复拉取。
_daily_fetch_lock = threading.Lock()
# 单次拉取硬超时。源站抽风/被限流时不能让请求线程无限挂起，
# 否则会直接表现为「下单没反应」。超时即放弃，交给定居/落库的降级逻辑处理。
# ak.fund_open_fund_daily_em 全市场表实拉约 28s，原 25s 偏紧会超时失败，
# 调到 60s 留足余量（启动预热/续热也走这个超时）。
_DAILY_FETCH_TIMEOUT = get("data_quality", "daily_fetch_timeout", 60.0)


def _timed_call(fn, timeout: float):
    """在守护线程里跑可能阻塞的 akshare 调用，超时即放弃。

    返回调用结果；超时或抛异常都返回 None 并打印原因。
    这样上层（状态抓取、净值）不会因为源站卡死而把整条请求链拖挂。
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001  源站异常统一降级为「没拿到」
            box["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        print(f"[fetcher] akshare 调用超时（>{timeout:.0f}s），已放弃，避免阻塞请求线程")
        return None
    if "e" in box:
        print(f"[fetcher] akshare 调用异常: {type(box['e']).__name__}: {box['e']}")
        return None
    return box.get("v")


def _get_daily_df(timeout: float | None = None, force: bool = False):
    """获取全市场每日净值表（带缓存 + 超时保护）。

    命中缓存（10 分钟内）直接返回；否则重新拉取，但单次拉取带硬超时——
    超时就返回 None，让调用方降级，而不是让下单这类同步请求卡死。

    timeout：单次拉取的硬超时（秒）。传 None 用全局 _DAILY_FETCH_TIMEOUT。
    force：True 时忽略 TTL 强制重新拉取（供后台续热调用，保证缓存永不过期）。
    """
    ak = _import_akshare()
    if ak is None:
        return None
    now = time.time()
    if not force and _daily_cache["df"] is not None and now - _daily_cache["ts"] < _DAILY_CACHE_TTL:
        return _daily_cache["df"]
    # 缓存未命中（或强制刷新）：串行化抓取，避免并发多次拉全市场大表互相拖慢/抢源站。
    with _daily_fetch_lock:
        # 二次检查：取锁期间可能另一线程刚拉完，直接复用，避免重复抓取。
        now = time.time()
        if not force and _daily_cache["df"] is not None and now - _daily_cache["ts"] < _DAILY_CACHE_TTL:
            return _daily_cache["df"]
        eff_timeout = timeout if timeout is not None else _DAILY_FETCH_TIMEOUT
        try:
            df = _timed_call(ak.fund_open_fund_daily_em, eff_timeout)
        except Exception as exc:  # noqa: BLE001  理论上 _timed_call 已吞掉，这里再兜一层
            print(f"[fetcher] 全市场净值表抓取异常: {type(exc).__name__}: {exc}")
            return None
        if df is None:
            return None
        if getattr(df, "empty", False):
            return None
        _daily_cache["df"] = df
        _daily_cache["ts"] = now
        return df


def warm_daily_cache(force: bool = False) -> bool:
    """主动预热/续热全市场每日表缓存（供后台线程调用，不阻塞请求线程）。

    成功返回 True。失败（网络/源站/超时）返回 False，不致命——
    下单路径会按既有降级逻辑处理（命中旧缓存或兜底拒单）。

    force=True 时忽略 10 分钟 TTL 强制重拉，用于周期性续热，
    保证运行期内缓存永不过期 → 下单永远秒回、不阻塞、不拒单。
    """
    try:
        df = _get_daily_df(timeout=_DAILY_FETCH_TIMEOUT, force=force)
    except Exception as e:  # noqa: BLE001
        print(f"[fetcher] 全市场表预热异常: {type(e).__name__}: {e}")
        return False
    return df is not None and not getattr(df, "empty", False)


def start_daily_cache_rewarmer(interval: float = 540.0):
    """后台守护线程：周期性强制续热全市场每日表。

    下单状态依赖这张表的热缓存（TTL=600s）。若不续热，缓存过期后
    首次下单会在短超时内失败被拒。本线程每 interval 秒（默认 540 < 600，
    留 60s 余量）强制重拉一次，使缓存始终新鲜 → 运行期内下单永远秒回。

    fire-and-forget：失败只记日志，不致命，下一轮重试。
    """
    def _loop() -> None:
        while True:
            try:
                warm_daily_cache(force=True)
            except Exception as e:  # noqa: BLE001
                print(f"[fetcher] 周期性续热异常（下一轮重试）: {type(e).__name__}: {e}")
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()


def pass_nav_gate(nav: float) -> bool:
    """净值合理性闸门。"""
    return NAV_GATE_MIN < nav < NAV_GATE_MAX


def _to_float(v: Any) -> float | None:
    """安全转 float。akshare 返回的类型经常是字符串或带百分号，统一处理。"""
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if v in ("", "-", "--"):
                return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_date(v: Any) -> str | None:
    """统一日期为 YYYY-MM-DD 字符串。"""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


# =============================================================================
# 净值抓取
# =============================================================================


def fetch_nav_em(fund_code: str) -> list[dict]:
    """东财源净值（主源）。

    返回 [{date, nav, acc_nav, growth}]，**按日期升序**。

    排序为什么必须显式做：
        akshare 部分接口默认按日期降序返回。下游做长周期统计时如果直接取
        首尾元素当起止点，方向就反了——历史上因此把某基金 5 年收益
        -20.2% 算成了 +25.4%。显式排序是强制要求，不能依赖接口的默认顺序。
    """
    ak = _import_akshare()
    if ak is None:
        return []

    out: list[dict] = []
    try:
        df_unit = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        df_acc = ak.fund_open_fund_info_em(symbol=fund_code, indicator="累计净值走势")
        if df_unit is None or df_unit.empty:
            return []

        col_date = next((c for c in df_unit.columns if "日期" in c), None)
        # 注意：不能简单用 "净值" in c 匹配净值列。
        # 列名 "净值日期" 同样含有 "净值"，且排在 "单位净值" 前面，
        # 会被优先选中，导致后续 float(日期字符串) 全部失败、返回空结果。
        # 必须排除含 "日期" 的列。
        col_nav = next(
            (c for c in df_unit.columns if "净值" in c and "日期" not in c), None
        )
        if not col_date or not col_nav:
            return []

        # 累计净值单独一张表，按日期建索引，用于识别分红除息
        acc_map: dict[str, float] = {}
        if df_acc is not None and not df_acc.empty:
            c_d = next((c for c in df_acc.columns if "日期" in c), None)
            # 同上：排除 "净值日期"，只取真正的净值列
            c_a = next(
                (c for c in df_acc.columns if "净值" in c and "日期" not in c), None
            )
            if c_d and c_a:
                for _, row in df_acc.iterrows():
                    d = _norm_date(row[c_d])
                    v = _to_float(row[c_a])
                    if d and v is not None:
                        acc_map[d] = v

        for _, row in df_unit.iterrows():
            d = _norm_date(row[col_date])
            nav = _to_float(row[col_nav])
            if not d or nav is None:
                continue
            out.append(
                {
                    "date": d,
                    "nav": nav,
                    "acc_nav": acc_map.get(d),
                    "growth": None,
                }
            )
    except Exception:
        return []

    out.sort(key=lambda r: r["date"])
    return out


def fetch_nav_daily_em(fund_code: str) -> list[dict]:
    """全市场当日净值表（第二源）。

    这个接口一次返回全市场 2 万+ 只基金的当日与前一交易日净值，
    并且附带**申购状态、赎回状态、手续费**——后两项是限购检测和费率
    计算的重要数据来源，主源接口不提供。

    因为要拉全市场，调用较慢（几秒），一天最多调一次，不要放进循环。

    列名是动态的，形如 "2026-08-28-单位净值"，
    所以必须动态解析列名，不能写死。
    """
    df = _get_daily_df()
    if df is None:
        return []
    try:
        row = df[df["基金代码"] == fund_code]
        if row.empty:
            return []
        row = row.iloc[0]

        out = []
        # 形如 YYYY-MM-DD-单位净值 的列，按日期提取
        for col in df.columns:
            if col.endswith("-单位净值"):
                date_str = col[:10]
                nav = _to_float(row[col])
                if nav is None:
                    continue
                out.append({"date": date_str, "nav": nav})
        out.sort(key=lambda r: r["date"])
        return out
    except Exception:
        return []


def fetch_fund_status(fund_code: str, timeout: float | None = None) -> dict:
    """抓取基金的申购/赎回状态与手续费。

    用途：
        - 申购状态：限购、暂停申购时直接拒单，不能等下单失败才发现
        - 手续费：拿到基金真实的原始申购费率，用于按折扣计算实际费用
          （支付宝是部分基金 1.25 折起，必须按各基金实际费率算，不能写死）

    timeout：透传给底层全市场表抓取，下单路径传短超时避免阻塞。
    返回 {} 表示没抓到，调用方应据此降级而不是编造。
    """
    df = _get_daily_df(timeout=timeout)
    if df is None:
        return {}
    try:
        row = df[df["基金代码"] == fund_code]
        if row.empty:
            return {}
        r = row.iloc[0]
        return {
            "purchase_status": str(r.get("申购状态", "")).strip(),
            "redeem_status": str(r.get("赎回状态", "")).strip(),
            "fee_raw": str(r.get("手续费", "")).strip(),
            "name": str(r.get("基金简称", "")).strip(),
        }
    except Exception:
        return {}


# =============================================================================
# 板块抓取
# =============================================================================


def fetch_boards(board_type: str = "industry", concept_names: list[str] | None = None) -> tuple[list[dict], str]:
    """抓取板块行情。返回 (板块列表, 实际使用的数据源名)。

    board_type：
      - `"industry"`（默认）：行业板块 —— 同花顺汇总为主、东财为备
      - `"concept"`：概念板块 —— 同花顺概念详情，**按传入的 concept_names 逐个抓**。
        同花顺有 375 个概念，全量逐个抓约 5 分钟；由调用方**按新闻热度筛 Top N** 传入
        （10 个约 8 秒），与项目「新闻导向找方向」的思路一致。

     补：原签名无参、且只抓行业 —— 而 `sector_news.refresh_sector_news`
    一直按 `fetch_boards(board_type="industry")` 调用 → **TypeError 被 except 吞掉**，
    板块刷新恒失败（数据停在 09-19）；概念侧更是完全没有抓取代码。

    为什么行业有两个源：
        东财的股票板块接口（stock_board_industry_name_em）
        连接被拒绝，同花顺的接口可用。单一依赖任何一个源，
        都会在某天源站抽风时全盘失效。这里同花顺为主、东财为备，
        主源挂了自动切备用源，并在返回值里标明实际用的是哪个，
        便于记入数据健康日志。
    """
    if board_type == "concept":
        return _fetch_concept_boards(concept_names or [])

    ak = _import_akshare()
    if ak is None:
        return [], "none"

    # 主源：同花顺行业汇总（含涨跌幅与净流入）
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is not None and not df.empty:
            out = []
            for _, row in df.iterrows():
                name = str(row.get("板块", "")).strip()
                if not name:
                    continue
                out.append(
                    {
                        "board_code": f"THS_{name}",
                        "board_name": name,
                        "change_pct": _to_float(row.get("涨跌幅")),
                        "main_net_inflow": _to_float(row.get("净流入")),
                    }
                )
            if out:
                return out, "ths_industry_summary"
    except Exception:
        pass

    # 备用源：东财行业板块
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            out = []
            for _, row in df.iterrows():
                code = str(row.get("板块代码", "")).strip()
                name = str(row.get("板块名称", "")).strip()
                if not code:
                    continue
                out.append(
                    {
                        "board_code": code,
                        "board_name": name,
                        "change_pct": _to_float(row.get("涨跌幅")),
                        "main_net_inflow": _to_float(row.get("主力净流入-净额")),
                    }
                )
            if out:
                return out, "em_industry_name"
    except Exception:
        pass

    return [], "failed"


def _fetch_concept_boards(names: list[str]) -> tuple[list[dict], str]:
    """抓**概念**板块行情（同花顺 `stock_board_concept_info_ths`，逐个概念取实时行情）。

     补齐概念板块行情抓取（`sector_news` 仅覆盖 industry 分支，概念板块另行抓取）。
    只抓传入的 names —— 同花顺有 375 个概念，全量逐个抓约 5 分钟，
    故由调用方**按新闻热度筛 Top N** 传入（新闻导向）。
    """
    ak = _import_akshare()
    if ak is None:
        return [], "none"

    out: list[dict] = []
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        try:
            df = ak.stock_board_concept_info_ths(symbol=name)
            if df is None or df.empty:
                continue
            # 接口返回两列「项目 / 值」的明细表，转 dict 取值
            kv = dict(zip(df["项目"], df["值"]))
            pct = kv.get("板块涨幅")
            inflow = kv.get("资金净流入(亿)")
            out.append(
                {
                    "board_code": f"THS_C_{name}",
                    "board_name": name,
                    "change_pct": _to_float(str(pct).replace("%", "")) if pct is not None else None,
                    "main_net_inflow": _to_float(inflow),
                }
            )
        except Exception:  # noqa: BLE001 单个概念失败不影响其余
            continue
    return out, ("ths_concept_info" if out else "failed")


_concept_names_cache: dict = {"names": [], "ts": 0.0}
_CONCEPT_NAMES_TTL = 24 * 3600


def fetch_concept_name_list() -> list[str]:
    """同花顺**全部概念名**（约 375 个）。带 24h 缓存（概念列表变动很慢）。

    用途：把新闻标题匹配到概念名，筛出「当前热门概念」再抓其行情
    （全量 375 个逐个抓要 5 分钟，只抓热门的 10 个约 8 秒）。
    """
    now = time.time()
    if _concept_names_cache["names"] and now - _concept_names_cache["ts"] < _CONCEPT_NAMES_TTL:
        return _concept_names_cache["names"]
    ak = _import_akshare()
    if ak is None:
        return _concept_names_cache["names"]  # 抓不到时退回旧缓存（宁可用旧的，也别返回空）
    try:
        df = ak.stock_board_concept_name_ths()
        names = [str(x).strip() for x in df["name"].tolist() if str(x).strip()]
        if names:
            _concept_names_cache["names"] = names
            _concept_names_cache["ts"] = now
        return names
    except Exception:  # noqa: BLE001
        return _concept_names_cache["names"]


# =============================================================================
# 持仓与行业配置
# =============================================================================


def fetch_industry_allocation(fund_code: str, year: int | None = None) -> list[dict]:
    """基金行业配置。

    直接给出基金在各行业的占比，比「抓持仓股票 → 反查所属行业」省事得多，
    也避免了自己维护股票到行业映射表的麻烦。

    注意 date 参数要传**年份**（如 2025），传 20250630 会报
    "Length mismatch" —— 这是 akshare 内部解析的问题，不是数据不存在。
    """
    ak = _import_akshare()
    if ak is None:
        return []
    year = year or datetime.now().year
    try:
        df = ak.fund_portfolio_industry_allocation_em(
            symbol=fund_code, date=str(year)
        )
        if df is None or df.empty:
            return []

        # 接口返回的是该基金**多个报告期**的行业配置，不止指定年份。
        # 必须按「截止时间」取最新一期，否则不同年份的数据会混在一起——
        # 示例：040001 返回 42 条，里面有两个"制造业"（43.78% 与 38.19%），
        # 分属 2025 和 2024 两期。不筛就会算出重复且矛盾的行业占比。
        date_col = next((c for c in df.columns if "时间" in c), None)
        report_date = ""
        if date_col:
            df = df.copy()
            df[date_col] = df[date_col].astype(str)
            latest = df[date_col].max()
            df = df[df[date_col] == latest]
            report_date = str(latest)[:10]

        out = []
        for _, row in df.iterrows():
            name = str(row.get("行业类别", "")).strip()
            weight = _to_float(row.get("占净值比例"))
            if not name or weight is None:
                continue
            out.append(
                {"industry": name, "weight": weight, "report_date": report_date}
            )
        return out
    except Exception:
        return []


def fetch_holdings(fund_code: str, report_date: str = "20250630") -> list[dict]:
    """基金持仓明细（季报）。date 格式为 YYYYMMDD。"""
    ak = _import_akshare()
    if ak is None:
        return []
    try:
        df = ak.fund_portfolio_hold_em(symbol=fund_code, date=report_date)
        if df is None or df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            out.append(
                {
                    "stock_code": str(row.get("股票代码", "")).strip(),
                    "stock_name": str(row.get("股票名称", "")).strip(),
                    "weight": _to_float(row.get("占净值比例")),
                    "report_date": str(row.get("季度", report_date)),
                }
            )
        return out
    except Exception:
        return []


# =============================================================================
# 交叉验证与异常检测
# =============================================================================


def cross_verify(
    primary: list[dict], secondary: list[dict], tolerance: float = CROSS_TOLERANCE
) -> tuple[list[dict], list[dict]]:
    """双源交叉验证。

    返回 (记录列表, 冲突清单)

    关键：必须**先对齐日期**再比对。
    两个源更新速度不同（第二源常滞后 2~3 天），
    如果各取各的「最新一条」直接比，会把「日期差」误判成「数据错」。
    """
    if not secondary:
        # 第二源不可用：数据仍可入库，但必须标记为未验证
        for r in primary:
            r["verified"] = 0
        return primary, []

    sec_map = {r["date"]: r["nav"] for r in secondary}
    conflicts: list[dict] = []

    for r in primary:
        sval = sec_map.get(r["date"])
        if sval is None:
            # 第二源还没覆盖这一天，不是冲突，只是尚未验证
            r["verified"] = 0
            continue
        diff_pct = abs(r["nav"] - sval) / sval * 100 if sval else 0
        if diff_pct <= tolerance:
            r["verified"] = 1
        else:
            r["verified"] = 0
            conflicts.append(
                {
                    "date": r["date"],
                    "nav_primary": r["nav"],
                    "nav_secondary": sval,
                    "diff_pct": round(diff_pct, 4),
                    "detail": "双源不一致，需三源裁决或人工确认，不得直接使用",
                }
            )
    return primary, conflicts


def detect_anomalies(records: list[dict]) -> list[dict]:
    """异常检测（统计层）。

    只负责发现异常并描述现象，**不判断原因**。
    「这是分红还是脏数据」属于语义判断，交给 AI 或人。

    检测项：
        1. 净值越过合理性闸门
        2. 单日跳变超过阈值（分红除息会造成跳变，是真实事件）
        3. 净值连续多日未变化（疑似接口未更新）
    """
    anomalies: list[dict] = []
    if not records:
        return anomalies

    # ① 合理性闸门：区间外的净值物理上不可能，属脏数据
    for r in records:
        nav = _to_float(r.get("nav"))
        if nav is None or not pass_nav_gate(nav):
            anomalies.append(
                {
                    "date": r.get("date"),
                    "type": "nav_gate",
                    "detail": (f"净值 {nav} 越出合理性闸门 "
                               f"[{NAV_GATE_MIN}, {NAV_GATE_MAX}]，判定为脏数据"),
                    "value": nav,
                }
            )

    # ② 单日跳变：分红除息会让单位净值单日大跌而累计净值几乎不动，
    #    两者「背离 / 同步」是区分类别的关键线索——这里只描述，不替 AI 定性。
    #    背离判定阈值（单位与累计变动相差的百分点）见 DIV_JUDGE_GAP。
    DIV_JUDGE_GAP = 5.0
    for prev, cur in zip(records, records[1:]):
        nav_p, nav_c = _to_float(prev.get("nav")), _to_float(cur.get("nav"))
        acc_p, acc_c = _to_float(prev.get("acc_nav")), _to_float(cur.get("acc_nav"))
        if not (nav_p and nav_c) or nav_p <= 0:
            continue
        pct = (nav_c - nav_p) / nav_p * 100
        if abs(pct) < JUMP_THRESHOLD:
            continue
        if acc_p and acc_c and acc_p > 0:
            acc_pct = (acc_c - acc_p) / acc_p * 100
            diff = pct - acc_pct
            if abs(diff) >= DIV_JUDGE_GAP:
                judge = (f"两者相差 {diff:.2f} 个百分点 → "
                         f"高度疑似分红除息（真实事件，非脏数据），建议 accept")
            else:
                judge = "单位与累计同步变动 → 倾向真实市场波动，建议 accept"
            detail = (f"单日变动 {pct:+.2f}%（{nav_p} → {nav_c}）；"
                      f"累计净值同期 {acc_pct:+.2f}%。{judge}")
        else:
            detail = (f"单日变动 {pct:+.2f}%（{nav_p} → {nav_c}）；"
                      f"无累计净值可比，建议人工复核")
        anomalies.append(
            {
                "date": cur.get("date"),
                "type": "nav_jump",
                "detail": detail,
                "value": round(pct, 2),
            }
        )

    # ③ 净值停滞：连续多日纹丝不动，通常是接口没更新，不是市场静止
    run, run_val = 1, None

    def _flush_stale(at_date: str) -> None:
        if run >= STALE_DAYS:
            anomalies.append(
                {
                    "date": at_date,
                    "type": "nav_stale",
                    "detail": f"净值连续 {run} 天未变化（{run_val}），疑似接口未更新",
                    "value": run_val,
                }
            )

    for prev, cur in zip(records, records[1:]):
        nav_c = _to_float(cur.get("nav"))
        if nav_c is not None and nav_c == _to_float(prev.get("nav")):
            run += 1
            run_val = nav_c
        else:
            _flush_stale(prev.get("date"))
            run, run_val = 1, None
    _flush_stale(records[-1].get("date"))
    return anomalies


# =============================================================================
# 落库
# =============================================================================


def save_nav(conn, fund_code: str, records: list[dict], source: str) -> int:
    """净值落库。同 (基金, 日期) 覆盖写入，不产生重复行。

    落库前过滤越出合理性闸门 [NAV_GATE_MIN, NAV_GATE_MAX] 的记录——
    闸门在此物理生效，脏净值不入库。
    调用方若已过滤则此过滤为幂等，不影响正常数据。
    """
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for r in records:
        if not pass_nav_gate(r["nav"]):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO nav_daily
                (fund_code, nav_date, nav, acc_nav, daily_growth,
                 source, verified, is_qdii_lagged, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                fund_code,
                r["date"],
                r["nav"],
                r.get("acc_nav"),
                r.get("growth"),
                source,
                r.get("verified", 0),
                now,
            ),
        )
        count += 1
    conn.commit()
    return count


def record_health(
    conn,
    source: str,
    data_type: str,
    status: str,
    record_count: int = 0,
    message: str = "",
    last_success_at: str | None = None,
) -> None:
    """写入数据健康日志。每次抓取后都要调用，失败的抓取尤其要记录。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO data_health
            (check_date, source, data_type, status, last_success_at,
             record_count, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now[:10], source, data_type, status, last_success_at, record_count, message, now),
    )
    conn.commit()


def anomalies_to_json(anomalies: list[dict], path) -> None:
    """异常清单落盘，供 AI 读取定性。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(anomalies),
        "note": "脚本只报异常不猜原因。请逐条判断是真实市场事件还是数据故障，"
                "并在 verdicts 目录输出 action: accept / reject / rescrape",
        "items": anomalies,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# =====  性能优化 =====

def warm_akshare() -> float:
    """预热 akshare（**进程内只此一次真正 import**，之后调用走缓存）。

    为什么单独做这一步：首次 `_import_akshare()` 约 **2.5 秒**
    （pandas / numpy / 大量子模块），任何第一次触发抓取的请求都会「莫名卡住几秒」。
    预热把这段耗时挪到启动后的后台线程，不占启动时间、也不拖慢用户请求。
    返回耗时（秒），便于观测。
    """
    t0 = time.time()
    _import_akshare()
    return time.time() - t0


def fetch_nav_many(codes, workers: int = 6) -> dict:
    """**并发**抓取多只基金净值（线程池，网络 IO 密集 → 线程有效）。

    为什么不用串行：单只稳态 ~0.7~0.9s，134 只串行 ≈ 2 分钟；
    6 并发可压到 **~20 秒**。失败的单只不影响其他（各自独立返回）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    codes = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    result: dict = {}
    if not codes:
        return result
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as ex:
        futs = {ex.submit(fetch_nav_em, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                result[c] = f.result()
            except Exception:  # noqa: BLE001 — 单只失败不能拖垮整批
                result[c] = []
    return result


def refresh_nav_all() -> dict:
    """刷新「候选池 + 持仓」的净值（**只刷需要的，不抓全市场**）。

    本函数是重建事故的遗留缺口 —— `api/main.py` 有 3 处调用
    （`/api/funds/refresh`、`/api/refresh/nav`、`/api/refresh/all`），但 fetcher 里
    一直没有实现，导致这三个端点恒返回 `has no attribute 'refresh_nav_all'`。

    刷新范围口径与调度器每日补净值一致（见 `scheduler`）：
      - 候选池 = `funds` 表**非 delisted** 的基金
      - 持仓   = `lots` 表 `status='active'` 且 `remaining_shares > 0` 的基金
    全市场约 2.4 万只逐只抓既慢又会被限流，而项目只需要这两类。

    单只失败不影响整批（`fetch_nav_many` 各自独立返回）；结果写 `data_health`
    留痕，`/api/health` 可见。返回 `{ok, total, updated, failed, rows}`。
    """
    from ..db.schema import get_connection  # data → db 方向合法；函数内导入避免加载期耦合

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT code AS c FROM funds WHERE status != 'delisted' "
            "UNION "
            "SELECT DISTINCT fund_code AS c FROM lots "
            "WHERE status = 'active' AND remaining_shares > 0"
        ).fetchall()
        codes = [r["c"] for r in rows]
        if not codes:
            record_health(conn, "em_refresh", "nav", "ok", 0,
                          "refresh_nav_all：候选池与持仓均为空，无需刷新")
            return {"ok": True, "total": 0, "updated": 0, "failed": 0, "rows": 0}

        res = fetch_nav_many(codes)
        updated = failed = total_rows = 0
        for code, records in res.items():
            if not records:
                failed += 1
                continue
            n = save_nav(conn, code, records, "em_refresh")
            if n > 0:
                updated += 1
                total_rows += n
            else:
                failed += 1

        # 有更新即 ok；一只都没成功才 failed（部分失败仍算 ok，避免误报）
        status = "ok" if updated else "failed"
        record_health(conn, "em_refresh", "nav", status, total_rows,
                      f"refresh_nav_all：{updated}/{len(codes)} 只更新（{total_rows} 行），{failed} 只失败")
        return {"ok": True, "total": len(codes), "updated": updated,
                "failed": failed, "rows": total_rows}
    finally:
        conn.close()
