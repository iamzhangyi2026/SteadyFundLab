# -*- coding: utf-8 -*-
"""
macro_data —— 政府宏观数据（抓取 + 读取）。

设计（依据 `docs/项目/资讯源补充清单.md` 定案）：
- 宏观数据在项目里**就是一类新闻**（「新闻 = 主信号」），因此**落 `market_news`**
  （`source = 政府宏观数据`），与 RSS 桶 / 快讯桶并列，下游事件提取与主题热度统一消费；
- 数据源走 **akshare 宏观接口**（不爬政府网站 —— 政府站点多无标准 RSS）。

宏观指标共 6 项，逐接口拉取后合并写入 `market_news`。

离线安全。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List

_SOURCE = "政府宏观数据"  # 与 market_news 历史数据同源名，保证前后一致

# (指标名, akshare 函数, 期次列, 取值列, 单位)
_MACRO_SERIES = [
    ("CPI", "macro_china_cpi", "月份", "全国-同比增长", "%"),
    ("PPI", "macro_china_ppi", "月份", "当月同比增长", "%"),
    ("GDP", "macro_china_gdp", "季度", "国内生产总值-同比增长", "%"),
    ("M2", "macro_china_money_supply", "月份", "货币和准货币(M2)-同比增长", "%"),
    ("PMI", "macro_china_pmi", "月份", "制造业-指数", ""),
    ("社融", "macro_china_shrzgm", "月份", "社会融资规模增量", "亿元"),
]


def _latest(df: Any, period_col: str) -> Any:
    """取**最新一期**：先按期次排序再取末行。

    ⚠️ 不能依赖接口默认顺序：6 个宏观接口里 5 个是**降序**（最新在前）、
    社融是**升序**，直接 `iloc[-1]` 会取到 2006 年的老数据（LESSONS #1 同类坑：
    长周期统计方向取反）。
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        d = df.copy()
        d["_period"] = d[period_col].astype(str)
        return d.sort_values("_period").iloc[-1]
    except Exception:  # noqa: BLE001 排序失败就退回原序末行
        return df.iloc[-1]


def fetch_macro(conn: sqlite3.Connection) -> Dict[str, Any]:
    """抓 6 项宏观指标并落 `market_news`，返回 {指标: 值}。

    按期次去重：`link = macro://<指标>/<期次>` —— 同一期数据重复抓不会重复入库
    （`market_news.link` 是 UNIQUE）。
    """
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return {}

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[str, Any] = {}
    saved = 0
    for name, fn_name, p_col, v_col, unit in _MACRO_SERIES:
        try:
            df = getattr(ak, fn_name)()
            row = _latest(df, p_col)
            if row is None:
                continue
            period = str(row.get(p_col, "")).strip()
            value = row.get(v_col)
            if period == "" or value is None:
                continue
            out[name] = {"period": period, "value": value}
            title = f"{name} {period}：{value}{unit}"
            summary = f"政府宏观数据 · {name} · {period} 期 · 同比/指数 {value}{unit}"
            link = f"macro://{name}/{period}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO market_news "
                "(title, link, summary, source, source_url, pub_time, fetched_at, is_official) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (title, link, summary, _SOURCE, "akshare:macro_china", fetched_at, fetched_at),
            )
            saved += cur.rowcount
        except Exception as exc:  # noqa: BLE001 单指标失败不影响其余
            print(f"[宏观] {name} 抓取失败: {type(exc).__name__}: {exc}")
    conn.commit()
    if saved:
        print(f"[宏观] 落库 {saved} 条（{_SOURCE}）")
    return out


def get_macro(conn: sqlite3.Connection) -> Dict[str, Any]:
    """读最近一轮宏观数据（来自 `market_news` 的「政府宏观数据」源）。"""
    try:
        rows: List[Any] = conn.execute(
            "SELECT title, summary, pub_time FROM market_news "
            "WHERE source = ? ORDER BY pub_time DESC, id DESC LIMIT 12",
            (_SOURCE,),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
    except Exception:  # noqa: BLE001
        return {"items": [], "count": 0}


if __name__ == "__main__":
    from ..db.schema import get_connection

    conn = get_connection()
    try:
        print("fetch_macro:", fetch_macro(conn))
        print("get_macro:", get_macro(conn))
    finally:
        conn.close()
