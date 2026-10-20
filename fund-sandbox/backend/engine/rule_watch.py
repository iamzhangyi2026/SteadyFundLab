# -*- coding: utf-8 -*-
"""
规则观察（rule_watch）引擎：监测 fund_status 变化。

对比每个 fund_code 的「最新」与「上一条」fund_status 快照，
对 purchase_status / redeem_status 的变动写入 rule_change_log。
返回本次检测到的变动列表（离线安全）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from ..db.schema import rows_to_dicts

WATCH_FIELDS = ("purchase_status", "redeem_status")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def watch_rules(conn) -> list:
    """
    比较最新 fund_status 与上一条快照，发现变化则写入 rule_change_log。
    返回 [{fund_code, kind, old_value, new_value}, ...]
    """
    detected: List[Dict[str, Any]] = []
    try:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM fund_status"
        ).fetchall()]
    except Exception:
        return detected

    for code in codes:
        rows = conn.execute(
            "SELECT * FROM fund_status WHERE fund_code=? "
            "ORDER BY status_date DESC, id DESC LIMIT 2",
            (code,),
        ).fetchall()
        if len(rows) < 2:
            continue
        latest, prev = rows[0], rows[1]
        for kind in WATCH_FIELDS:
            old_v = prev[kind]
            new_v = latest[kind]
            if new_v is None:
                continue
            if old_v == new_v:
                continue

            # 去重：若最近一次日志已是同一个新值，则不再重复写入
            last = conn.execute(
                "SELECT new_value FROM rule_change_log "
                "WHERE fund_code=? AND kind=? ORDER BY id DESC LIMIT 1",
                (code, kind),
            ).fetchone()
            if last and last[0] == new_v:
                continue

            conn.execute(
                "INSERT INTO rule_change_log "
                "(fund_code, kind, old_value, new_value, detected_at) "
                "VALUES (?,?,?,?,?)",
                (code, kind, old_v, new_v, _now()),
            )
            detected.append({
                "fund_code": code, "kind": kind,
                "old_value": old_v, "new_value": new_v,
            })
    if detected:
        conn.commit()
    return detected


def get_rule_changes(conn, limit: int = 20) -> list:
    """最近 limit 条规则变动日志。"""
    rows = conn.execute(
        "SELECT * FROM rule_change_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows_to_dicts(rows)


if __name__ == "__main__":
    from ..db.schema import get_connection
    conn = get_connection()
    try:
        print("== watch_rules ==")
        print(watch_rules(conn))
        print("\n== get_rule_changes ==")
        for r in get_rule_changes(conn, 5):
            print(r)
    finally:
        conn.close()


# ===== 费率同步与申购状态监测 =====

FEE_TTL_DAYS = 7


def sync_fund_fees(conn, codes, force: bool = False, ttl_days: int = FEE_TTL_DAYS) -> dict:
    """同步若干只基金的赎回费阶梯，返回 {fetched, ok, changed, failed}。

    变化检测：新抓到的档位与库里旧值不一致 → 写一条 rule_change_log(kind='redeem_fee')。
    """
    from datetime import datetime, timedelta
    import json
    import re

    stat = {"fetched": 0, "ok": 0, "changed": 0, "failed": 0}
    cutoff = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")

    for code in dict.fromkeys(str(c).strip() for c in codes if str(c).strip()):
        old = conn.execute("SELECT redeem_tiers, fetched_at FROM fund_fee WHERE fund_code = ?", (code,)).fetchone()
        if not force and old and (old[1] or "") >= cutoff:
            stat["ok"] += 1
            continue
        got = _fetch_redeem_tiers(code)
        if not got:
            stat["failed"] += 1
            continue
        raw, tiers = got
        new_json = json.dumps(tiers, ensure_ascii=False)
        if old and old[0] and old[0] != new_json:
            conn.execute(
                "INSERT INTO rule_change_log (fund_code, kind, old_value, new_value, detected_at) VALUES (?,?,?,?,?)",
                (code, "redeem_fee", old[0], new_json, _now()),
            )
            stat["changed"] += 1
        conn.execute(
            "INSERT INTO fund_fee (fund_code, redeem_tiers, redeem_raw, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(fund_code) DO UPDATE SET redeem_tiers=excluded.redeem_tiers, "
            "redeem_raw=excluded.redeem_raw, fetched_at=excluded.fetched_at",
            (code, new_json, raw, _now()),
        )
        stat["fetched"] += 1
        stat["ok"] += 1
    conn.commit()
    return stat


def _fetch_redeem_tiers(code):
    """抓一只基金的赎回费阶梯 → (原始文本, 档位列表)；抓不到返回 None。"""
    import json
    import re

    try:
        import akshare as ak

        df = ak.fund_fee_em(symbol=code, indicator="赎回费率")
    except Exception as exc:  # noqa: BLE001
        print(f"[rule_watch] 赎回费抓取失败 {code}: {type(exc).__name__}: {exc}")
        return None
    if df is None or getattr(df, "empty", True):
        return None
    tiers = []
    for _, r in df.iterrows():
        label = str(r.get("适用期限") or "").strip()
        try:
            rate = float(str(r.get("赎回费率")).strip().rstrip("%")) / 100.0
        except Exception:  # noqa: BLE001
            rate = None
        if not label:
            continue
        nums = [int(n) for n in re.findall(r"(\d+)", label)]
        dmin = dmax = None
        if nums:
            if "小于" in label and "大于" not in label:
                dmin, dmax = 0, nums[0]
            elif "大于等于" in label and "小于" in label:
                dmin, dmax = nums[0], (nums[1] if len(nums) > 1 else None)
            elif "大于等于" in label or "大于" in label:
                dmin, dmax = nums[0], None
        tiers.append({"label": label, "days_min": dmin, "days_max": dmax, "rate": rate})
    if not tiers:
        return None
    return df.to_string(index=False), tiers


def redeem_tiers_of(conn, code) -> list | None:
    """读某只基金已缓存的赎回费档位；没同步过返回 None（**不编造**）。"""
    import json

    try:
        row = conn.execute("SELECT redeem_tiers FROM fund_fee WHERE fund_code = ?", (str(code),)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None


def watch_purchase_status(conn, codes) -> int:
    """检测这些基金的申购状态/限购额度变化并留痕，返回变化条数。"""
    from backend.engine import fund_pool

    changed = 0
    for code in dict.fromkeys(str(c).strip() for c in codes if str(c).strip()):
        try:
            pi = fund_pool.lookup_purchase(code)
        except Exception:  # noqa: BLE001
            continue
        if not pi:
            continue
        cur = f"{pi.get('purchase_status') or ''}|{pi.get('purchase_limit') or ''}"
        row = conn.execute(
            "SELECT new_value FROM rule_change_log WHERE fund_code = ? AND kind = 'purchase_status' "
            "ORDER BY id DESC LIMIT 1", (code,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO rule_change_log (fund_code, kind, old_value, new_value, detected_at) VALUES (?,?,?,?,?)",
                (code, "purchase_status", None, cur, _now()),
            )
            continue
        if row[0] != cur:
            conn.execute(
                "INSERT INTO rule_change_log (fund_code, kind, old_value, new_value, detected_at) VALUES (?,?,?,?,?)",
                (code, "purchase_status", row[0], cur, _now()),
            )
            changed += 1
    conn.commit()
    return changed
