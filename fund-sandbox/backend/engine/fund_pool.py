"""基金自选池：搜索、抓取购买规则、抓取历史净值、维护交易池（funds 表）。

设计要点：

1. 购买规则（购买起点 / 日累计限额 / 申购状态 / 费率）与名称 / 类型实时抓取：
   真实世界很多基金有最低购买金额（1 元 / 10 元 / 100 元不等），
   不抓就可能在下单时才发现「低于起购金额」——这跟「限购」一样是真实约束，
   必须提前知道并拦在下单前。

2. 用户能自己搜索并添加基金，添加时把购买规则和历史净值一起抓回来，
   否则加进来一只「没净值、没规则」的空壳基金，既不能看曲线也不能正确下单。

数据源（接口名以 akshare 实际签名为准）：
    fund_name_em()        全市场场外基金清单（代码/简称/类型/拼音），用于搜索
    fund_purchase_em()    全市场购买配置（购买起点/日累计限额/申购状态/手续费），用于规则
    fetcher.fetch_nav_em  单只历史净值（单位/累计），复用已有落库逻辑

设计：
    - 交易池即 funds 表 status='active' 的全部基金，每只都经 add_fund 入库。
    - 购买规则优先取实时抓取的 fund_purchase_em（含购买起点），抓不到才退回档案表 / 默认值。
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

from backend.config import test_funds
from backend.data import fetcher

# 全市场表缓存。fund_name_em / fund_purchase_em 一次拉 2.7 万只基金，要好几秒，
# 搜索 / 添加频繁调用，10 分钟内复用，避免重复拉取和被源站限流。
_name_cache: dict[str, Any] = {"df": None, "ts": 0.0}
_purchase_cache: dict[str, Any] = {"df": None, "ts": 0.0}
_CACHE_TTL = 600  # 秒
# 拉全市场表很慢（11~22s）且源站易限流，并发重复拉会互相拖慢甚至超时。
# 用锁保证任一时刻只拉一次，其余等待复用同一结果（double-checked locking）。
_warm_lock = threading.Lock()


# 本地磁盘缓存：把全市场表持久化到 data/market_cache.db，跨重启秒回。
# 否则每次重启都要从东财重新拉 22 秒，开屏后搜索要等很久。
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MARKET_CACHE_DB = os.path.join(_PROJ_ROOT, "data", "market_cache.db")
_LOCAL_CACHE_TTL = 86400  # 本地缓存 1 天有效，超时后台刷新（不阻塞开屏）


def _local_cache_get(table: str):
    """从本地磁盘缓存取全市场表；过期/缺失返回 None。

    载荷用 JSON（不用 pickle）：缓存文件被篡改时最坏是解析失败回退重抓，
    不会反序列化执行任意代码。
    """
    if not os.path.exists(_MARKET_CACHE_DB):
        return None
    try:
        c = sqlite3.connect(_MARKET_CACHE_DB)
        row = c.execute(
            "SELECT payload, ts FROM market_cache WHERE name=?", (table,)
        ).fetchone()
        c.close()
        if not row:
            return None
        payload, ts = row
        if time.time() - ts > _LOCAL_CACHE_TTL:
            return None  # 过期，交给调用方重新抓取
        return _df_from_json(payload)
    except Exception:
        return None


def _df_to_json(df) -> str:
    """DataFrame → JSON 字符串（split 方向，保留列名与索引）。"""
    return df.to_json(orient="split", date_format="iso")


def _df_from_json(s):
    """JSON 字符串 → DataFrame。"""
    import pandas as pd

    return pd.read_json(io.StringIO(s), orient="split")


def _local_cache_set(table: str, df) -> None:
    """把全市场表写入本地磁盘缓存（抓到即存，供后续重启秒回）。"""
    try:
        os.makedirs(os.path.dirname(_MARKET_CACHE_DB), exist_ok=True)
        c = sqlite3.connect(_MARKET_CACHE_DB)
        c.execute(
            "CREATE TABLE IF NOT EXISTS market_cache("
            "name TEXT PRIMARY KEY, payload BLOB, ts REAL)"
        )
        c.execute(
            "INSERT INTO market_cache(name, payload, ts) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, ts=excluded.ts",
            (table, _df_to_json(df), time.time()),
        )
        c.commit()
        c.close()
    except Exception:
        pass  # 缓存写入失败不影响主流程


def _ak():
    """延迟导入 akshare，避免无网络 / 未安装环境下导入即炸。"""
    try:
        import akshare as ak

        return ak
    except ImportError:
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if v in ("", "-", "--", "NaT"):
                return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_date(v: Any) -> str | None:
    if v is None:
        return None
    # pandas NaT 也有 strftime 属性但调用会抛错，必须 try 兜底
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%Y-%m-%d")
        except Exception:
            return None
    s = str(v).strip()
    if s in ("", "NaT", "nan", "None", "nat"):
        return None
    return s[:10] if len(s) >= 10 else None


def get_name_df(force: bool = False):
    """全市场基金清单（内存+本地磁盘双缓存）。force=True 忽略缓存重拉东财。"""
    ak = _ak()
    if ak is None:
        return None
    now = time.time()
    if not force and _name_cache["df"] is not None and now - _name_cache["ts"] < _CACHE_TTL:
        return _name_cache["df"]
    # 磁盘缓存命中先进快路径，不进锁：重启后首个请求不必等可能很慢的锁
    if not force:
        local = _local_cache_get("name")
        if local is not None:
            _name_cache["df"] = local
            _name_cache["ts"] = now
            return local
    with _warm_lock:
        now = time.time()
        if not force and _name_cache["df"] is not None and now - _name_cache["ts"] < _CACHE_TTL:
            return _name_cache["df"]
        # 强制刷新：直接重拉东财（覆盖新上市基金 / 规则变更）
        if force:
            try:
                df = ak.fund_name_em()
            except Exception:
                df = None
            if df is not None and not df.empty:
                _name_cache["df"] = df
                _name_cache["ts"] = now
                _local_cache_set("name", df)
                return df
            # 拉取失败/空 → 退回本地或内存旧值（宁可旧数据也不要崩）
            old = _local_cache_get("name") or _name_cache["df"]
            if old is not None:
                _name_cache["df"] = old
                _name_cache["ts"] = now
            return old
        # 非强制：内存未命中 → 先试本地磁盘缓存（跨重启秒回，无需重新拉东财）
        local = _local_cache_get("name")
        if local is not None:
            _name_cache["df"] = local
            _name_cache["ts"] = now
            return local
        try:
            df = ak.fund_name_em()
        except Exception:
            return _name_cache["df"]  # 过期缓存也优于没有
        if df is None or df.empty:
            return None
        _name_cache["df"] = df
        _name_cache["ts"] = now
        _local_cache_set("name", df)  # 抓到即落盘，下次重启秒回
        return df


def get_purchase_df(force: bool = False):
    """全市场购买配置（内存+本地磁盘双缓存）。force=True 忽略缓存重拉东财。"""
    ak = _ak()
    if ak is None:
        return None
    now = time.time()
    if not force and _purchase_cache["df"] is not None and now - _purchase_cache["ts"] < _CACHE_TTL:
        return _purchase_cache["df"]
    # 磁盘缓存命中先进快路径，不进锁（同 get_name_df）
    if not force:
        local = _local_cache_get("purchase")
        if local is not None:
            _purchase_cache["df"] = local
            _purchase_cache["ts"] = now
            return local
    with _warm_lock:
        now = time.time()
        if not force and _purchase_cache["df"] is not None and now - _purchase_cache["ts"] < _CACHE_TTL:
            return _purchase_cache["df"]
        # 强制刷新：直接重拉东财
        if force:
            try:
                df = ak.fund_purchase_em()
            except Exception:
                df = None
            if df is not None and not df.empty:
                _purchase_cache["df"] = df
                _purchase_cache["ts"] = now
                _local_cache_set("purchase", df)
                return df
            old = _local_cache_get("purchase") or _purchase_cache["df"]
            if old is not None:
                _purchase_cache["df"] = old
                _purchase_cache["ts"] = now
            return old
        # 非强制：内存未命中 → 先试本地磁盘缓存（跨重启秒回）
        local = _local_cache_get("purchase")
        if local is not None:
            _purchase_cache["df"] = local
            _purchase_cache["ts"] = now
            return local
        try:
            df = ak.fund_purchase_em()
        except Exception:
            return _purchase_cache["df"]
        if df is None or df.empty:
            return None
        _purchase_cache["df"] = df
        _purchase_cache["ts"] = now
        _local_cache_set("purchase", df)  # 抓到即落盘，下次重启秒回
        return df


def refresh_market_data() -> dict:
    """强制刷新全市场表（新基金 / 规则变更后手动触发）。返回命中行数。"""
    get_name_df(force=True)
    get_purchase_df(force=True)
    n = len(_name_cache["df"]) if _name_cache["df"] is not None else 0
    p = len(_purchase_cache["df"]) if _purchase_cache["df"] is not None else 0
    return {"ok": True, "name_rows": n, "purchase_rows": p,
            "note": f"已刷新全市场表：基金 {n} 只 / 购买规则 {p} 只"}


def lookup_purchase(fund_code: str) -> dict | None:
    """从缓存的购买配置表里取一只基金的购买规则。

    返回 {min_purchase, purchase_limit, purchase_status, redeem_status,
          fee_rate, next_open_date} 或 None（抓不到）。
    """
    df = get_purchase_df()
    if df is None:
        return None
    try:
        row = df[df["基金代码"].astype(str) == fund_code]
    except Exception:
        return None
    if row.empty:
        return None
    r = row.iloc[0]
    fee_raw = _to_float(r.get("手续费"))
    # 手续费列是「折扣后申购费率」的百分比数值（如 0.15 表示 0.15%），转小数。
    # 申购费法定上限 ~1.5%，该值若 >5 视为异常不采信（宁可回退默认）。
    fee_rate = (fee_raw / 100.0) if (fee_raw is not None and 0 < fee_raw <= 5) else None
    return {
        "min_purchase": _to_float(r.get("购买起点")),
        # akshare 对「无限制」基金返回极大值（如 1e11），视为不限，避免前端显示荒谬数字
        "purchase_limit": (pl if (pl := _to_float(r.get("日累计限定金额"))) is not None and pl <= 1e9 else None),
        "purchase_status": str(r.get("申购状态", "")).strip() or None,
        "redeem_status": str(r.get("赎回状态", "")).strip() or None,
        "fee_rate": fee_rate,
        "next_open_date": _norm_date(r.get("下一开放日")),
    }


def search_funds(q: str, limit: int = 20) -> dict:
    """按代码 / 名称 / 拼音缩写搜索基金。

    返回 {ok, count, items, note}。items 每项含购买规则（购买起点 / 状态 / 费率），
    满足「查找时就能看到购买规则」——不用等添加。
    """
    df = get_name_df()
    if df is None:
        return {"ok": False, "count": 0, "items": [],
                "note": "akshare unavailable：无法加载全市场基金清单（检查网络 / 依赖）"}
    raw = (q or "").strip()
    q = raw.lower()
    if not q:
        return {"ok": True, "count": 0, "items": [], "note": "请输入基金代码或名称关键词"}
    try:
        mask = (
            df["基金代码"].astype(str).str.lower().str.contains(q, na=False)
            | df["基金简称"].astype(str).str.lower().str.contains(q, na=False)
            | df["拼音缩写"].astype(str).str.lower().str.contains(q, na=False)
        )
        sub = df[mask].head(limit)
    except Exception:
        return {"ok": False, "count": 0, "items": [], "note": "搜索过程出错"}

    # 按 6 位代码精确搜索却 0 命中 → 极可能是当天新上市基金，缓存未覆盖。
    # 强制刷新全市场表后重试一次，保证新基金按代码可搜到。
    if len(sub) == 0 and re.fullmatch(r"\d{6}", raw):
        get_name_df(force=True)
        get_purchase_df(force=True)
        df2 = get_name_df()
        if df2 is not None:
            try:
                mask2 = df2["基金代码"].astype(str).str.lower().str.contains(q, na=False)
                sub = df2[mask2].head(limit)
            except Exception:
                pass

    items = []
    for _, r in sub.iterrows():
        code = str(r.get("基金代码", "")).strip()
        name = str(r.get("基金简称", "")).strip()
        category = str(r.get("基金类型", "")).strip()
        pi = lookup_purchase(code) or {}
        items.append(
            {
                "code": code,
                "name": name,
                "category": category,
                "is_qdii": _is_qdii(category, name),
                "min_purchase": pi.get("min_purchase"),
                "purchase_limit": pi.get("purchase_limit"),
                "purchase_status": pi.get("purchase_status"),
                "redeem_status": pi.get("redeem_status"),
                "fee_rate": pi.get("fee_rate"),
            }
        )
    return {"ok": True, "count": len(items), "items": items,
            "note": f"匹配到 {len(items)} 只（已附购买规则）"}


def _is_qdii(category: str, name: str) -> bool:
    """判断是否为 QDII（净值 T+2 滞后）。"""
    cat = (category or "").upper()
    nm = (name or "").upper()
    return "QDII" in cat or "QDII" in nm or "海外" in cat


# =============================================================================
# 交易池合并
# =============================================================================


def pool_funds(conn: sqlite3.Connection) -> list[dict]:
    """交易池（funds 表 status='active' 的全部基金）。

    池内每只基金都是经 add_fund 入库的，故统一 added=True
    （前端「官方」标签与关注列表都以它判定）。

    每只基金附上购买规则（购买起点优先实时抓取，其次档案表 / 默认 1 元）。
    已软删（status='delisted'）的代码会从池里过滤掉——「移除」因此真正生效。
    """
    out: list[dict] = []

    # 已软删（status='delisted'）的基金：用户从交易池移除的。
    # pool_funds 据此过滤，使「移除」对池内所有基金生效。
    try:
        delisted = {
            r["code"]
            for r in conn.execute("SELECT code FROM funds WHERE status = 'delisted'").fetchall()
        }
    except Exception:
        delisted = set()

    for f in test_funds():
        code = f["code"]
        if code in delisted:
            continue
        m = dict(f)
        pi = lookup_purchase(code)
        if pi:
            # 起购/限购都直接用实时抓取值（lookup_purchase 已清洗 1e11 等荒谬值），
            # 不回退配置旧值、不抬卫生地板，否则会带出固定的 10 元地板或 1e11
            m["min_purchase"] = pi.get("min_purchase")
            m["purchase_limit"] = pi.get("purchase_limit")
            # 申购/赎回状态：前端 Trade/Positions 展示需要，缺失会全部显示"状态未知"
            m["purchase_status"] = pi.get("purchase_status") or m.get("purchase_status")
            m["redeem_status"] = pi.get("redeem_status") or m.get("redeem_status")
            m["fee_rate"] = pi.get("fee_rate")
        else:
            m.setdefault("fee_rate", None)
        m["added"] = True
        out.append(m)

    return out


def held_position_funds(conn: sqlite3.Connection) -> list[dict]:
    """当前**实际有持仓**的基金（remaining_shares>0），供账户/持仓页遍历。

    与 pool_funds 的区别：这里只取「真持了仓的」，名字/起购从 funds 表取，
    **完全不碰全市场表（fund_name_em / fund_purchase_em）**。首屏账户页因此零 akshare
    依赖，避免为「有持仓的几只基金」去串行拉 2.7 万只的全市场表。
    遍历后只保留 total_shares>0 的。
    """
    rows = conn.execute(
        "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
    ).fetchall()
    codes = [r["fund_code"] for r in rows]
    if not codes:
        return []
    meta: dict[str, dict] = {}
    for r in conn.execute("SELECT code, name, min_purchase, is_qdii FROM funds"):
        meta[r["code"]] = dict(r)
    cfg = {f["code"]: f for f in test_funds()}
    out: list[dict] = []
    for code in codes:
        m = meta.get(code) or {}
        c = cfg.get(code) or {}
        out.append(
            {
                "code": code,
                "name": (m.get("name") or c.get("name") or code),
                "min_purchase": m.get("min_purchase") or c.get("min_purchase"),
                "is_qdii": bool(m.get("is_qdii") or c.get("is_qdii", False)),
            }
        )
    return out


# =============================================================================
# 添加基金：解析 + 抓规则 + 抓历史净值 + 落库
# =============================================================================


def add_fund(conn: sqlite3.Connection, fund_code: str) -> dict:
    """搜索并添加一只基金到交易池。

    一步到位：解析名称 / 类型 → 抓购买规则 → 抓历史净值落 nav_daily → 写 funds 表。
    返回 {ok, code, name, category, is_qdii, min_purchase, purchase_limit,
          fee_rate, nav_count, note}。
    """
    fund_code = (fund_code or "").strip()
    if not fund_code:
        return {"ok": False, "note": "基金代码为空"}

    # 1) 名称 / 类型：从全市场清单按代码精确匹配
    name_df = get_name_df()
    name = None
    category = None
    if name_df is not None:
        try:
            row = name_df[name_df["基金代码"].astype(str) == fund_code]
            if not row.empty:
                r = row.iloc[0]
                name = str(r.get("基金简称", "")).strip() or fund_code
                category = str(r.get("基金类型", "")).strip()
        except Exception:
            pass
    # 缓存未命中 → 当天新上市基金可能不在本地缓存里，强制刷新全市场表重试一次
    if not name:
        get_name_df(force=True)
        get_purchase_df(force=True)
        name_df = get_name_df()
        if name_df is not None:
            try:
                row = name_df[name_df["基金代码"].astype(str) == fund_code]
                if not row.empty:
                    r = row.iloc[0]
                    name = str(r.get("基金简称", "")).strip() or fund_code
                    category = str(r.get("基金类型", "")).strip()
            except Exception:
                pass
    if not name:
        return {"ok": False, "note": f"未在全市场清单中找到 {fund_code}，请确认代码正确"}

    is_qdii = _is_qdii(category, name)

    # 2) 购买规则——实时抓取为准：落库的是真实快照，
    #    配置显式值只在 akshare 拿不到该基金时兜底。
    pi = lookup_purchase(fund_code) or {}
    min_purchase = pi.get("min_purchase")
    purchase_limit = pi.get("purchase_limit")
    fee_rate = pi.get("fee_rate")
    if not pi:
        cfg = next((f for f in test_funds() if f["code"] == fund_code), None)
        if cfg:
            if cfg.get("min_purchase") is not None:
                min_purchase = cfg["min_purchase"]
            if cfg.get("purchase_limit") is not None:
                purchase_limit = cfg["purchase_limit"]
    # 起购/限购用真实值：不抬卫生地板（fixed 10），与交易池/搜索显示、下单校验一致
    purchase_status = pi.get("purchase_status")
    redeem_status = pi.get("redeem_status")
    next_open_date = pi.get("next_open_date")

    now = datetime.now().isoformat(timespec="seconds")

    # 3) 先抓历史净值（只读网络，不写库）。全量可能上千行，截取到近 ~5 年，
    #    足够画曲线与算近1年收益，又不会在添加时卡太久。
    #    顺序很关键：净值落不了库就不写 funds——否则会留下「active 但无净值」
    #    的空壳基金（不能交易、无曲线、前端展示异常），且没有任何告警。
    nav_count = 0
    try:
        records = fetcher.fetch_nav_em(fund_code)
        if records:
            records = records[-1200:]
    except Exception as e:
        records = None
        nav_err = f"净值抓取失败：{e}"
    else:
        nav_err = "" if records else "净值源返回空（新基金可能暂无历史净值）"

    if records:
        nav_count = fetcher.save_nav(conn, fund_code, records, "em_lsjz")
    if not records or nav_count == 0:
        # 全部记录被合理性闸门过滤掉（nav_count=0）同样视为无净值
        why = nav_err or (f"历史净值 {len(records)} 条全部未通过合理性闸门"
                          if records else "净值源返回空")
        fetcher.record_health(
            conn, "em_lsjz", "nav_history", "failed", 0,
            f"添加 {fund_code} {name} 中止：{why}",
        )
        return {"ok": False, "code": fund_code, "name": name,
                "note": f"未添加「{name}」（{fund_code}）：{why}。"
                        f"不留无净值空壳，请稍后重试"}

    # 4) 落 funds 表（UPSERT：已存在则更新规则）——净值已落库，此刻写入不会成空壳
    conn.execute(
        """
        INSERT INTO funds
            (code, name, short_name, category, is_qdii, purchase_fee_rate,
             min_purchase, purchase_limit, period_type, next_open_date,
             status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name, short_name=excluded.short_name,
            category=excluded.category, is_qdii=excluded.is_qdii,
            purchase_fee_rate=excluded.purchase_fee_rate,
            min_purchase=excluded.min_purchase, purchase_limit=excluded.purchase_limit,
            period_type=excluded.period_type, next_open_date=excluded.next_open_date,
            status='active', updated_at=excluded.updated_at
        """,
        (
            fund_code, name, name, category, 1 if is_qdii else 0,
            fee_rate, min_purchase, purchase_limit,
            ("持有期" if "持有期" in (purchase_status or "") else None),
            next_open_date, now,
        ),
    )
    conn.commit()

    # 5) 记录健康日志，便于界面 / AI 看到抓取情况
    fetcher.record_health(
        conn, "em_purchase", "purchase_rule",
        "ok" if pi else "failed", 1 if pi else 0,
        f"添加 {fund_code} {name}：购买起点 {min_purchase}",
    )
    fetcher.record_health(
        conn, "em_lsjz", "nav_history", "ok", nav_count,
        f"添加 {fund_code} {name}：历史净值 {nav_count} 条",
    )

    return {
        "ok": True,
        "code": fund_code,
        "name": name,
        "category": category,
        "is_qdii": is_qdii,
        "min_purchase": min_purchase,
        "purchase_limit": purchase_limit,
        "fee_rate": fee_rate,
        "purchase_status": purchase_status,
        "redeem_status": redeem_status,
        "nav_count": nav_count,
        "note": (
            f"已添加「{name}」（{fund_code}），购买起点 {min_purchase} 元，"
            f"已抓取历史净值 {nav_count} 条"
        ),
    }


def remove_fund(conn: sqlite3.Connection, fund_code: str) -> dict:
    """从交易池移除一只基金（软删除：置 status='delisted'）。

    pool_funds 会据此跳过 delisted 的代码，所以「移除」生效；
    nav 历史与基金档案保留，后续重新添加（add_fund 会 reset 为 active）可一键复活。
    """
    fund_code = (fund_code or "").strip()
    if not fund_code:
        return {"ok": False, "note": "基金代码为空"}
    cur = conn.execute("SELECT code, name FROM funds WHERE code = ?", (fund_code,))
    row = cur.fetchone()
    if row is None:
        return {"ok": False, "note": f"交易池中未找到 {fund_code}"}
    conn.execute(
        "UPDATE funds SET status = 'delisted', updated_at = ? WHERE code = ?",
        (datetime.now().isoformat(timespec="seconds"), fund_code),
    )
    conn.commit()
    return {"ok": True, "code": fund_code}


if __name__ == "__main__":
    from backend.db import get_connection, init_db

    init_db()
    conn = get_connection()
    print("=" * 78)
    print("基金自选池演示")
    print("=" * 78)
    print("\n── 搜索「沪深300」")
    import json

    for it in search_funds("沪深300")["items"][:5]:
        print(f"  {it['code']} {it['name']} [{it['category']}] "
              f"起购 {it['min_purchase']} 状态 {it['purchase_status']} 费 {it['fee_rate']}")
    print("\n── 交易池")
    for f in pool_funds(conn):
        tag = "添加" if f.get("added") else "未知"
        print(f"  [{tag}] {f['code']} {f.get('name')} 起购 {f.get('min_purchase')}")
    print("\n── 添加一只基金并抓净值")
    # 用搜索结果里第一只做演示
    res = add_fund(conn, "110020")  # 易方达沪深300ETF联接（如存在）
    print(json.dumps(res, ensure_ascii=False, indent=2))
    conn.close()
