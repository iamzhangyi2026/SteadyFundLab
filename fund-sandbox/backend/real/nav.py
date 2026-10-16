# -*- coding: utf-8 -*-
"""B 区行情：取真实持仓基金的净值序列与最新涨跌幅。

市场行情是公开数据，B 区**独立抓取**（不读 A 区库、不跨库），结果缓存进自己的 real_nav。
抓取失败返回空，绝不让界面崩。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# akshare 内部用 py_mini_racer（原生库），**并发调用会崩掉整个进程**——串行化。
_AK_LOCK = threading.Lock()


def _import_akshare():
    try:
        import akshare as ak
        return ak
    except Exception:
        return None


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def _fetch_df(code: str, indicator: str):
    ak = _import_akshare()
    if ak is None:
        return None
    try:
        with _AK_LOCK:
            return ak.fund_open_fund_info_em(symbol=code, indicator=indicator)
    except Exception:
        return None


def _cols(df):
    """返回 (日期列, 净值列)。「净值日期」也含「净值」，必须先排除含「日期」的列。"""
    col_date = next((c for c in df.columns if "日期" in c), None)
    col_nav = next((c for c in df.columns if "净值" in c and "日期" not in c), None)
    return col_date, col_nav


# ---------------------------------------------------------------------------
# 公共数据层：行情复用（账本分区 / 数据共用）
# ---------------------------------------------------------------------------
# 行情是真实世界的公共数据（东财同一份），**不该两个区各抓一遍**：谁需要谁登记，
# 抓到的落一份公共表，重复的直接用缓存（只有真实持仓与沙盒交易需要隔离账本，
#   其余资讯类资料两区共用）。
#
# 边界：**账本隔离**（真实持仓 = 真钱 / 沙盒 = 模拟），
#       **数据共用**（行情 / 新闻 / 规则 / 日历 —— 全是真实世界的资料）。
#
# 落点说明：公共行情表目前物理上在 A 区库 `fund_sandbox.db`（它是唯一抓取方：
# 有调度器 + 双源交叉验证）。B 区**只读**——不写、不做账本 JOIN。
_SHARED_DB = Path(__file__).resolve().parents[2] / "data" / "fund_sandbox.db"
# 公共行情最后一条距今超过这么多天，就认为陈旧 → 实时抓兜底（防 A 区尚未刷新时读到旧值）
_SHARED_STALE_DAYS = 10


def _read_shared_series(code: str) -> list[dict]:
    """只读公共行情表（`nav_daily`）。未命中 / 陈旧 / 异常一律返回 []，由调用方实时抓兜底。"""
    if not _SHARED_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{_SHARED_DB.as_posix()}?mode=ro", uri=True, timeout=3)
    except Exception:
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT nav_date, nav, acc_nav, daily_growth FROM nav_daily "
            "WHERE fund_code=? ORDER BY nav_date",
            (code,),
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    if not rows:
        return []
    out = [
        {"date": r["nav_date"], "nav": r["nav"], "growth": r["daily_growth"], "acc_nav": r["acc_nav"]}
        for r in rows
    ]
    # 陈旧保护：公共表太久没更新（A 区还没刷到这只）→ 交回调用方实时抓
    try:
        from datetime import date as _date

        if (_date.today() - _date.fromisoformat(out[-1]["date"])).days > _SHARED_STALE_DAYS:
            return []
    except Exception:
        pass
    return out


def fetch_series(code: str, limit: int | None = None, with_acc: bool = False) -> list[dict]:
    """取净值序列 [{date, nav, growth, acc_nav?}]，按日期升序。失败返回 []。

    **优先复用公共行情**（A 区 `nav_daily`，只读）—— 行情是真实世界的公共数据，
    只有一份（东财），A 区调度器每天已在抓；B 区再抓一遍就是同一份数据抓两次，
    既浪费又可能两边不一致。
    公共表未覆盖该基金时，才实时抓兜底（保证真实持仓永远有数据）。

    with_acc=True 时额外取累计净值（分红校准后口径），用于中长期收益 / 回撤计算。
    """
    shared = _read_shared_series(code)
    if shared:
        return shared[-limit:] if limit else shared

    df = _fetch_df(code, "单位净值走势")
    if df is None or df.empty:
        return []
    col_date, col_nav = _cols(df)
    col_g = next((c for c in df.columns if "增长" in c), None)
    if not col_date or not col_nav:
        return []

    acc_map: dict[str, float] = {}
    if with_acc:
        dfa = _fetch_df(code, "累计净值走势")
        if dfa is not None and not dfa.empty:
            cd, ca = _cols(dfa)
            if cd and ca:
                for _, row in dfa.iterrows():
                    d = str(row[cd])[:10]
                    v = _f(row[ca])
                    if d and v is not None:
                        acc_map[d] = v

    out = []
    for _, row in df.iterrows():
        d = str(row[col_date])[:10]
        nav = _f(row[col_nav])
        if not d or nav is None:
            continue
        item = {"date": d, "nav": nav, "growth": _f(row[col_g]) if col_g else None}
        if with_acc:
            item["acc_nav"] = acc_map.get(d)
        out.append(item)
    out.sort(key=lambda r: r["date"])
    return out[-limit:] if limit else out


def fetch_latest(code: str) -> dict | None:
    """取最新一行：{nav_date, nav, change_pct}；失败返回 None。"""
    s = fetch_series(code, limit=1)
    if not s:
        return None
    last = s[-1]
    return {"nav_date": last["date"], "nav": last["nav"], "change_pct": last["growth"]}


_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def fetch_estimates(codes: list) -> dict:
    """批量取**盘中估值**（新浪 fu_ 接口，一次请求多只）。

    返回 {code: {est_nav, base_nav, est_change_pct, est_time}}；失败返回 {}。
    字段（GBK）：0 名称, 1 时间, 2 估算净值, 3 单位净值, 4 累计净值, 5 ?, 6 估算涨幅%,
                7 日期, 8 ?, 9 ?
    """
    if not codes:
        return {}
    import requests

    url = "https://hq.sinajs.cn/list=" + ",".join("fu_" + c for c in codes)
    try:
        r = requests.get(url, headers=_SINA_HEADERS, timeout=12)
        r.encoding = "gbk"
    except Exception:
        return {}

    out: dict = {}
    for line in r.text.splitlines():
        if "hq_str_fu_" not in line or '"' not in line:
            continue
        code = line.split("hq_str_fu_")[1].split("=")[0].strip()
        parts = line.split('"')[1].split(",")
        if len(parts) < 7:
            continue
        est_time = f"{parts[7]} {parts[1]}".strip() if len(parts) > 7 else None
        out[code] = {
            "est_nav": _f(parts[2]),
            "base_nav": _f(parts[3]),
            "est_change_pct": _f(parts[6]),
            "est_time": est_time,
        }
    return out
