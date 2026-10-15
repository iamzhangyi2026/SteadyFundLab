# -*- coding: utf-8 -*-
"""B 区读写。

只读函数供 AI 调用；写函数（导入确认、删除）只应由人经 API 触发。
"""
from __future__ import annotations

from datetime import datetime

from backend.real.db import get_connection


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row(r):
    return {k: r[k] for k in r.keys()} if r else None


def _rows(rs):
    return [{k: r[k] for k in r.keys()} for r in rs] if rs else []


# ---------------- 只读（AI 可调） ----------------
def list_holdings() -> list:
    c = get_connection()
    try:
        return _rows(c.execute(
            "SELECT h.*, n.nav_date, n.nav, n.change_pct, n.est_nav, n.est_change_pct, n.est_time, "
            "n.updated_at AS nav_updated_at "
            "FROM real_holdings h LEFT JOIN real_nav n ON h.fund_code = n.fund_code "
            "ORDER BY h.market_amount DESC"
        ).fetchall())
    finally:
        c.close()


def get_holding(fund_code: str):
    c = get_connection()
    try:
        return _row(c.execute(
            "SELECT h.*, n.nav_date, n.nav, n.change_pct, n.est_nav, n.est_change_pct, n.est_time, "
            "n.updated_at AS nav_updated_at "
            "FROM real_holdings h LEFT JOIN real_nav n ON h.fund_code = n.fund_code "
            "WHERE h.fund_code=?", (fund_code,)
        ).fetchone())
    finally:
        c.close()


def summary() -> dict:
    c = get_connection()
    try:
        r = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_amount),0) cost, "
            "COALESCE(SUM(market_amount),0) market, COALESCE(SUM(profit),0) profit "
            "FROM real_holdings"
        ).fetchone()
        return {
            "holdings_count": r["n"],
            "total_cost": round(r["cost"], 2),
            "total_market": round(r["market"], 2),
            "total_profit": round(r["profit"], 2),
        }
    finally:
        c.close()


def list_snapshots(limit: int = 90) -> list:
    c = get_connection()
    try:
        return _rows(c.execute(
            "SELECT * FROM real_snapshots ORDER BY snap_date DESC LIMIT ?", (limit,)
        ).fetchall())
    finally:
        c.close()


def get_import(import_id: int):
    c = get_connection()
    try:
        imp = _row(c.execute("SELECT * FROM real_imports WHERE id=?", (import_id,)).fetchone())
        if imp:
            imp["rows"] = _rows(c.execute(
                "SELECT * FROM real_import_rows WHERE import_id=? ORDER BY id", (import_id,)
            ).fetchall())
        return imp
    finally:
        c.close()


def list_imports(limit: int = 20) -> list:
    c = get_connection()
    try:
        return _rows(c.execute(
            "SELECT id, source, status, created_at, confirmed_at "
            "FROM real_imports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall())
    finally:
        c.close()


# ---------------- 写（人经 API 触发） ----------------
def create_import(source: str, raw_text: str | None = None) -> int:
    c = get_connection()
    try:
        cur = c.execute(
            "INSERT INTO real_imports(source, raw_text, status, created_at) "
            "VALUES (?,?, 'pending', ?)",
            (source, raw_text, _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def add_import_rows(import_id: int, rows: list) -> int:
    c = get_connection()
    try:
        n = 0
        for r in rows:
            c.execute(
                "INSERT INTO real_import_rows("
                "import_id, fund_code, fund_name, shares, cost_amount, market_amount, "
                "profit, profit_rate, needs_review, confirmed) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (import_id, r.get("fund_code"), r.get("fund_name"), r.get("shares"),
                 r.get("cost_amount"), r.get("market_amount"), r.get("profit"),
                 r.get("profit_rate"), 1 if r.get("needs_review") else 0),
            )
            n += 1
        c.commit()
        return n
    finally:
        c.close()


def confirm_import(import_id: int, rows: list, snap_date: str | None = None) -> dict:
    """人工确认后写入真实持仓。rows=[{fund_code,fund_name,shares,cost_amount,market_amount,profit,profit_rate}]"""
    c = get_connection()
    try:
        snap_date = snap_date or datetime.now().strftime("%Y-%m-%d")
        for r in rows:
            if not r.get("fund_code"):
                continue
            c.execute(
                "INSERT INTO real_holdings("
                "fund_code, fund_name, shares, cost_amount, market_amount, profit, "
                "profit_rate, snapshot_date, source, confirmed, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'ocr', 1, ?) "
                "ON CONFLICT(fund_code) DO UPDATE SET "
                "fund_name=excluded.fund_name, shares=excluded.shares, "
                "cost_amount=excluded.cost_amount, market_amount=excluded.market_amount, "
                "profit=excluded.profit, profit_rate=excluded.profit_rate, "
                "snapshot_date=excluded.snapshot_date, updated_at=excluded.updated_at",
                (r["fund_code"], r.get("fund_name"), r.get("shares"), r.get("cost_amount"),
                 r.get("market_amount"), r.get("profit"), r.get("profit_rate"),
                 snap_date, _now()),
            )
        c.execute("UPDATE real_imports SET status='confirmed', confirmed_at=? WHERE id=?",
                  (_now(), import_id))
        agg = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_amount),0) cost, "
            "COALESCE(SUM(market_amount),0) market, COALESCE(SUM(profit),0) profit "
            "FROM real_holdings"
        ).fetchone()
        c.execute(
            "INSERT INTO real_snapshots(snap_date,total_cost,total_market,total_profit,"
            "holdings_count,created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(snap_date) DO UPDATE SET total_cost=excluded.total_cost, "
            "total_market=excluded.total_market, total_profit=excluded.total_profit, "
            "holdings_count=excluded.holdings_count",
            (snap_date, agg["cost"], agg["market"], agg["profit"], agg["n"], _now()),
        )
        c.commit()
        return {"ok": True, "import_id": import_id, "holdings_count": agg["n"]}
    finally:
        c.close()


def discard_import(import_id: int) -> dict:
    c = get_connection()
    try:
        c.execute("UPDATE real_imports SET status='discarded' WHERE id=?", (import_id,))
        c.commit()
        return {"ok": True, "import_id": import_id}
    finally:
        c.close()


def delete_holding(fund_code: str) -> dict:
    c = get_connection()
    try:
        c.execute("DELETE FROM real_holdings WHERE fund_code=?", (fund_code,))
        c.commit()
        return {"ok": True, "fund_code": fund_code}
    finally:
        c.close()


def refresh_nav(codes: list | None = None) -> dict:
    """抓取真实持仓基金的最新净值与**盘中估值**，写入 real_nav。codes 为空则取全部持仓。"""
    from backend.real import nav as real_nav

    c = get_connection()
    try:
        if not codes:
            codes = [r["fund_code"] for r in c.execute("SELECT fund_code FROM real_holdings").fetchall()]
        ests = real_nav.fetch_estimates(codes)  # 一次批量请求
        ok = 0
        for code in codes:
            info = real_nav.fetch_latest(code)
            if not info:
                continue
            e = ests.get(code, {})
            c.execute(
                "INSERT INTO real_nav(fund_code, nav_date, nav, change_pct, est_nav, est_change_pct, est_time, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(fund_code) DO UPDATE SET "
                "nav_date=excluded.nav_date, nav=excluded.nav, change_pct=excluded.change_pct, "
                "est_nav=COALESCE(excluded.est_nav, real_nav.est_nav), "
                "est_change_pct=COALESCE(excluded.est_change_pct, real_nav.est_change_pct), "
                "est_time=COALESCE(excluded.est_time, real_nav.est_time), "
                "updated_at=excluded.updated_at",
                (code, info["nav_date"], info["nav"], info["change_pct"],
                 e.get("est_nav"), e.get("est_change_pct"), e.get("est_time"), _now()),
            )
            ok += 1
        c.commit()
        return {"ok": True, "refreshed": ok, "total": len(codes), "estimated": len(ests)}
    finally:
        c.close()


def refresh_estimates(codes: list | None = None) -> dict:
    """只刷新**盘中估值**（新浪批量，快），不动净值。供前端定时轮询。"""
    from backend.real import nav as real_nav

    c = get_connection()
    try:
        if not codes:
            codes = [r["fund_code"] for r in c.execute("SELECT fund_code FROM real_holdings").fetchall()]
        ests = real_nav.fetch_estimates(codes)
        n = 0
        for code, e in ests.items():
            c.execute(
                "INSERT INTO real_nav(fund_code, est_nav, est_change_pct, est_time, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(fund_code) DO UPDATE SET "
                "est_nav=excluded.est_nav, est_change_pct=excluded.est_change_pct, "
                "est_time=excluded.est_time, updated_at=excluded.updated_at",
                (code, e.get("est_nav"), e.get("est_change_pct"), e.get("est_time"), _now()),
            )
            n += 1
        c.commit()
        return {"ok": True, "refreshed": n, "total": len(codes)}
    finally:
        c.close()


def save_snapshot(snap_date: str | None = None) -> dict:
    """手动落一条总览快照（按当前持仓汇总），供事后对比。"""
    c = get_connection()
    try:
        snap_date = snap_date or datetime.now().strftime("%Y-%m-%d")
        agg = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_amount),0) cost, "
            "COALESCE(SUM(market_amount),0) market, COALESCE(SUM(profit),0) profit "
            "FROM real_holdings"
        ).fetchone()
        c.execute(
            "INSERT INTO real_snapshots(snap_date,total_cost,total_market,total_profit,"
            "holdings_count,created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(snap_date) DO UPDATE SET total_cost=excluded.total_cost, "
            "total_market=excluded.total_market, total_profit=excluded.total_profit, "
            "holdings_count=excluded.holdings_count, created_at=excluded.created_at",
            (snap_date, agg["cost"], agg["market"], agg["profit"], agg["n"], _now()),
        )
        c.commit()
        return {"ok": True, "snap_date": snap_date, "total_cost": agg["cost"],
                "total_market": agg["market"], "total_profit": agg["profit"],
                "holdings_count": agg["n"]}
    finally:
        c.close()
