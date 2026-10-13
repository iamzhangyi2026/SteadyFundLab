"""持仓穿透分析与可投范围校验。

三件事：

1. 可投范围校验（场外 vs 场内）
   本系统只买**场外**基金——不需要注册股票账户，在支付宝/天天基金就能直接买。
   这里的坑是 ETF：
       「XX ETF 联接」= 场外 feeder fund，可以直接定投，能买
       「XX ETF」      = 场内品种，需要股票账户，不能买
   两者名字只差两个字，必须靠类型字段判断，绝不能只看名称放行。

2. 行业暴露穿透
   为什么不能只看基金名称：风格漂移太常见了。
   名为「XX新兴成长」的基金可能重仓银行股，名为「XX消费」的可能满仓新能源。
   只有穿透到实际持仓，才知道钱真正压在哪些行业上。
   这直接决定「行业暴露度」和「同涨同跌集中度」是不是真的分散——
   如果三只名字不同的基金穿透后都重压同一个行业，那就是假分散。

3. 风格漂移与重叠度检测
   漂移 = 名称宣称的方向与实际持仓不符
   重叠 = 两只基金前十大重仓股高度重合（买了等于没分散）
"""

from __future__ import annotations

from backend.config import get
from backend.data import fetcher


# =============================================================================
# 可投范围校验
# =============================================================================


def check_tradable(fund_type: str, fund_name: str) -> tuple[bool, str]:
    """校验一只基金能否在本系统交易。

    参数：
        fund_type  基金类型字段（来自档案表或抓取结果）
        fund_name  基金名称

    返回：
        (是否可买, 原因)

    判定顺序：禁止名单 → 模糊词校验 → 允许名单
    """
    allowed = get("universe", "allowed_types", [])
    forbidden = get("universe", "forbidden_types", [])
    ambiguous = get("universe", "ambiguous_keywords", [])

    fund_type = (fund_type or "").strip()
    fund_name = (fund_name or "").strip()

    # 1. 明确禁止的品类
    for f in forbidden:
        if f and f in fund_type:
            return False, f"类型「{fund_type}」在禁止名单内（需要股票账户或非公募场外品种）"

    # 2. 名称含 ETF 但没有"联接"字样 → 极可能是场内品种
    #    这是最容易搞混的地方，不能只凭名称放行
    for kw in ambiguous:
        if kw and kw in fund_name and "联接" not in fund_name:
            return False, (
                f"名称含「{kw}」但无「联接」字样，可能是场内品种（需股票账户）。"
                f"场外可买的是「XX{kw}联接」，请人工确认后再加入自选池"
            )

    # 3. 类型必须在允许名单内
    if allowed:
        if not fund_type:
            return False, "缺少类型字段，无法确认是否场外品种，拒绝加入"
        if not any(a in fund_type for a in allowed if a):
            return False, f"类型「{fund_type}」不在可投范围 {allowed} 内"

    return True, "ok"


# =============================================================================
# 行业暴露穿透
# =============================================================================


def analyze_exposure(fund_code: str, year: int | None = None) -> dict:
    """分析一只基金的真实行业暴露。

    返回：
        {
          fund_code, report_date,
          allocation: [{industry, weight}],  # 按占比降序
          top_industry, top_weight,
          concentrated: bool,   # 单一行业是否过度集中
        }

    数据来自行业配置接口（比「抓重仓股 → 反查行业」更直接，
    也省去了自己维护股票到行业映射表的麻烦）。
    """
    alloc = fetcher.fetch_industry_allocation(fund_code, year)
    if not alloc:
        return {"fund_code": fund_code, "allocation": [], "note": "未抓到行业配置"}

    # 同一行业可能有多条，按行业名聚合
    merged: dict[str, float] = {}
    for a in alloc:
        merged[a["industry"]] = merged.get(a["industry"], 0.0) + a["weight"]

    ranked = sorted(
        [{"industry": k, "weight": round(v, 2)} for k, v in merged.items()],
        key=lambda x: x["weight"],
        reverse=True,
    )

    limit = get("analysis", "concentration_warn_pct", 50.0)
    top = ranked[0] if ranked else {"industry": "", "weight": 0}
    return {
        "fund_code": fund_code,
        "report_date": alloc[0].get("report_date", ""),
        "allocation": ranked,
        "top_industry": top["industry"],
        "top_weight": top["weight"],
        "concentrated": top["weight"] >= limit,
        "note": (
            f"最大单一行业 {top['industry']} 占 {top['weight']}%，"
            f"{'已超过' if top['weight'] >= limit else '未超过'}集中度阈值 {limit}%"
        ),
    }


def detect_style_drift(
    fund_name: str, allocation: list[dict], threshold: float | None = None
) -> dict:
    """检测风格漂移：名称宣称的方向 vs 实际持仓。

    逻辑：
        1. 从基金名称里找出宣称的行业（用 config 的 name_industry_hints）
        2. 用该行业的关键词去匹配实际持仓的行业名，累加占比
        3. 实际占比低于阈值 → 判定漂移

    例：名为「XX消费精选」，但实际持仓中消费方向只占 12% → 漂移，名称不可信。
    """
    hints = get("analysis", "name_industry_hints", [])
    threshold = threshold if threshold is not None else get(
        "analysis", "drift_warn_pct", 50.0
    )

    claimed = None
    keywords: list[str] = []
    for h in hints:
        for kw in h.get("keywords", []):
            if kw and kw in fund_name:
                claimed = h.get("industry")
                keywords = list(h.get("keywords", []))
                break
        if claimed:
            break

    if not claimed:
        return {
            "claimed_industry": None,
            "drifted": False,
            "actual_pct": 0.0,
            "note": "名称中没有明确的行业指向词，不做漂移判断",
        }

    # 用宣称行业的关键词去匹配实际行业名
    actual = sum(
        a["weight"]
        for a in allocation
        if any(k in a["industry"] for k in keywords if k)
    )

    drifted = actual < threshold
    return {
        "claimed_industry": claimed,
        "actual_pct": round(actual, 2),
        "threshold": threshold,
        "drifted": drifted,
        "note": (
            f"名称指向「{claimed}」，实际持仓中该方向占 {actual:.2f}%"
            + ("—— 与名称严重不符，不可按名称理解这只基金" if drifted else "")
        ),
    }


# =============================================================================
# 重叠度
# =============================================================================


def calc_overlap(holdings_a: list[dict], holdings_b: list[dict]) -> dict:
    """计算两只基金的重仓股重叠程度。

    用途：识别「假分散」。买入多只名字不同的基金，
    如果穿透后前十大重仓股高度重合，实际风险敞口等同于只买了一只。
    """
    if not holdings_a or not holdings_b:
        return {"overlap_count": 0, "overlapped": False, "stocks": []}

    a_by_code = {
        h.get("stock_code"): h.get("stock_name")
        for h in holdings_a
        if h.get("stock_code")
    }
    b_codes = {h.get("stock_code") for h in holdings_b if h.get("stock_code")}
    common = [
        {"stock_code": c, "stock_name": a_by_code[c]}
        for c in a_by_code
        if c in b_codes
    ]

    limit = get("analysis", "overlap_warn_count", 3)
    return {
        "overlap_count": len(common),
        "overlapped": len(common) >= limit,
        "stocks": common,
    }


def save_holdings(conn, fund_code: str, holdings: list[dict], source: str) -> int:
    """持仓明细落库。"""
    from datetime import datetime

    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for h in holdings:
        report_date = h.get("report_date")
        stock_code = h.get("stock_code")
        if not report_date or not stock_code:
            continue
        try:
            weight = float(h.get("weight")) if h.get("weight") is not None else None
        except (TypeError, ValueError):
            weight = None
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO fund_holdings
                (fund_code, report_date, stock_code, stock_name,
                 weight, board_code, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fund_code, report_date, stock_code,
             h.get("stock_name"), weight,
             h.get("board_code"), source, now),
        )
        count += cur.rowcount  # INSERT OR IGNORE 冲突时 rowcount=0
    conn.commit()
    return count







if __name__ == "__main__":
    from backend.config import test_funds

    print("=" * 78)
    print("可投范围校验演示")
    print("=" * 78)
    cases = [
        ("ETF联接", "华夏国证半导体芯片ETF联接C"),
        ("ETF", "华夏国证半导体芯片ETF"),
        ("混合型", "易方达蓝筹精选混合"),
        ("QDII", "广发纳斯达克100指数(QDII)"),
        ("", "某某精选混合"),
    ]
    for t, n in cases:
        ok, reason = check_tradable(t, n)
        flag = "可买" if ok else "拒绝"
        print(f"  [{flag}] {n}")
        print(f"         类型={t or '(空)'}  {reason}")

    print("\n" + "=" * 78)
    print("持仓穿透演示（真实数据）")
    print("=" * 78)
    for f in test_funds():
        code, name = f["code"], f["name"]
        exp = analyze_exposure(code)
        if not exp.get("allocation"):
            print(f"\n  {code} {name}: {exp.get('note')}")
            continue
        print(f"\n  {code} {name}")
        print(f"    报告期 {exp.get('report_date')}  {exp.get('note')}")
        for a in exp["allocation"][:5]:
            print(f"      {a['industry']:<12} {a['weight']:>6.2f}%")
        drift = detect_style_drift(name, exp["allocation"])
        print(f"    漂移检测: {drift['note']}")


# ===== 辅助函数 =====

def _latest_nav(conn, fund_code: str) -> float:
    """最新**单位**净值；查不到返回 0.0。

    ⚠️ 用 `nav` 而不是 `acc_nav`（累计净值）：有分红的基金两者差很多，
    拿累计净值算市值会**高估**（与 confirm_order 取成交净值是同一个道理）。
    """
    row = conn.execute(
        "SELECT nav FROM nav_daily WHERE fund_code=? AND nav IS NOT NULL "
        "ORDER BY nav_date DESC LIMIT 1",
        (fund_code,),
    ).fetchone()
    return float(row["nav"]) if row else 0.0


def get_positions(conn) -> list[dict]:
    """当前持仓（**按基金聚合**），供 /api/positions、/api/fund/{code}、/api/exposure 使用。

    口径：活跃批次（remaining_shares > 0）汇总 + 最新单位净值算市值。
    返回每条含 `code` / `fund_name` / `category` / `shares` / `cost` /
    `current_nav` / `current_value`。
    """
    rows = conn.execute(
        """
        SELECT l.fund_code,
               f.name,
               f.category,
               SUM(l.remaining_shares) AS shares,
               SUM(l.cost_amount)      AS cost
        FROM lots l
        LEFT JOIN funds f ON f.code = l.fund_code
        WHERE l.status = 'active' AND l.remaining_shares > 0
        GROUP BY l.fund_code
        """
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        nav = _latest_nav(conn, r["fund_code"])
        shares = float(r["shares"] or 0)
        out.append({
            "code": r["fund_code"],
            "fund_name": r["name"] or "",
            "category": r["category"] or "",
            "shares": round(shares, 4),
            "cost": round(float(r["cost"] or 0), 2),
            "current_nav": round(nav, 4),
            "current_value": round(shares * nav, 2),
        })
    return out


def add_fund(code: str, name: str = "", category: str = "", reason: str = "",
             reactivate: bool = False, conn=None) -> dict:
    """向基金档案表添加 / 复活一只基金（候选池入口）。

    - 已存在且未退市：更新名称/类型，置为 active。
    - 已退市：默认拒绝（delisted 不自动复活），除非 reactivate=True。
    - 不存在：新建 active 记录。
    reason 由路由层强制必填（候选池添加须填 reason），此处仅回显记录。
    """
    from datetime import datetime
    from backend.db import get_connection

    own = False
    if conn is None:
        conn = get_connection()
        own = True
    try:
        code = (code or "").strip()
        if not code:
            return {"ok": False, "reason": "基金代码不能为空"}
        now = datetime.now().isoformat(timespec="seconds")
        row = conn.execute("SELECT code, status FROM funds WHERE code=?", (code,)).fetchone()
        if row:
            if row["status"] == "delisted" and not reactivate:
                return {"ok": False, "fund_code": code, "status": "delisted",
                        "reason": f"基金 {code} 已退市，需设置 reactivate=True 才能复活"}
            conn.execute(
                "UPDATE funds SET name=COALESCE(NULLIF(?, ''), name), "
                "category=COALESCE(NULLIF(?, ''), category), status='active', "
                "updated_at=? WHERE code=?",
                (name, category, now, code),
            )
            conn.commit()
            return {"ok": True, "fund_code": code, "status": "active",
                    "action": "updated", "reason": reason}
        conn.execute(
            "INSERT INTO funds (code, name, category, status, updated_at) VALUES (?, ?, ?, 'active', ?)",
            (code, name or code, category, now),
        )
        conn.commit()
        return {"ok": True, "fund_code": code, "status": "active",
                "action": "inserted", "reason": reason}
    finally:
        if own:
            conn.close()
