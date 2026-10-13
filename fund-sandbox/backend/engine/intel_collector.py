"""情报自动采集：从已抓取的广义财经 RSS(market_news) 里，
用关键词启发式提取政策 / 地缘 / 军事 / 会议 / 宏观 / 公司 六类事件，
自动写入 intel_events，减少人工空白表单录入。

设计原则（宁可漏报不谎报）：
- 只有标题+摘要里出现足够强的类别关键词才入库；
- 影响方向采用「正向/负向关键词计数」+ 冲突时取中性；
- 状态统一为 disclosed（披露态），RSS 新闻不等于政策落地；
- 来源保留 RSS 源名+原文链接，便于回溯；
- 用 title+event_date 去重，同一条新闻不会重复入库。

注意：这是规则级初筛，质量不如 AI 判断；发现明显错误时，
分析页仍保留手动补录表单用于修正/补充。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable

# ---------------------------------------------------------------------------
# 关键词库。只匹配明确、低频误判的词；宁可少也不滥。
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "policy": [
        "政策", "央行", "降准", "降息", "加息", "财政政策", "货币政策",
        "监管", "证监会", "国务院", "发改委", "住建部", "商务部",
        "税收", "减税", "税制", "信贷", "MLF", "LPR", "逆回购",
    ],
    "geo": [
        "地缘", "中美", "中欧", "中俄", "台海", "俄乌", "中东", "半岛",
        "制裁", "关税", "贸易", "贸易战", "外交", "访华", "访问", "会晤",
        "争端", "摩擦", "断供", "脱钩",
    ],
    "military": [
        "军事", "军演", "演习", "冲突", "战争", "武装", "导弹", "军队",
        "国防", "战机", "舰艇", "无人机", "空袭", "交火",
    ],
    "conference": [
        "会议", "两会", "政治局", "常委会", "国常会", "发布会",
        "峰会", "论坛", "座谈", "工作会议", "全会", "代表大会",
        "G20", "APEC", "博鳌", "达沃斯",
    ],
    "macro": [
        "宏观", "GDP", "PMI", "CPI", "PPI", "通胀", "通缩", "失业率",
        "就业", "消费", "投资", "出口", "进口", "顺差", "逆差",
        "经济增长", "衰退", "复苏", "景气", "工业增加", "社零",
    ],
    "company": [
        "公司", "企业", "业绩", "财报", "季报", "年报", "盈利", "亏损",
        "营收", "净利润", "业绩预增", "业绩预减", "暴雷", "并购",
        "重组", "定增", "回购", "股权激励", "高管", "董事长",
        # 公司行动类（与 triage._CAT_KW 保持同步）
        "推出", "上线", "上架", "交付", "起诉", "诉讼", "首席执行官", "ceo",
        "收购", "债务", "订阅", "裁员", "罢工", "召回",
    ],
}

# 按特异性排序，用于同分时决定类别；越靠前越优先
CATEGORY_PRIORITY = ["policy", "conference", "geo", "military", "macro", "company"]

POSITIVE_KEYWORDS = [
    "利好", "上涨", "增长", "复苏", "扩张", "宽松", "刺激", "降准", "降息",
    "超预期", "强劲", "回暖", "企稳", "改善", "提振", "支撑", "加码",
    "盈利增长", "业绩预增", "扭亏", "创新高",
]

NEGATIVE_KEYWORDS = [
    "利空", "下跌", "衰退", "萎缩", "收紧", "加息", "风险", "危机",
    "恶化", "不及预期", "暴雷", "亏损", "下滑", "回落", "承压", "拖累",
    "冲突升级", "制裁", "断供", "战争", "衰退", "通缩", "失业",
]

# 北京时间
_TZ = timezone(timedelta(hours=8))


def _normalize(text: str | None) -> str:
    """把文本转成便于关键词匹配的形式：中文/英文/数字保留，其余变空格，去重。"""
    if not text:
        return ""
    text = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", " ", str(text))
    return re.sub(r"\s+", " ", text)


def _score(text: str, keywords: Iterable[str]) -> int:
    """统计 keywords 在 text 中的命中次数（整词匹配，不区分大小写）。"""
    score = 0
    for kw in keywords:
        pat = re.compile(re.escape(kw), re.IGNORECASE)
        score += len(pat.findall(text))
    return score


def _classify(text: str) -> tuple[str | None, str]:
    """返回 (category, impact_dir)。category 为 None 表示不归类，不入库。"""
    scores = {cat: _score(text, kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    max_score = max(scores.values())
    if max_score == 0:
        return None, "neutral"

    # 取最高分；同分按 CATEGORY_PRIORITY 优先级
    best = max(CATEGORY_PRIORITY, key=lambda c: (scores[c], CATEGORY_PRIORITY.index(c)))

    pos = _score(text, POSITIVE_KEYWORDS)
    neg = _score(text, NEGATIVE_KEYWORDS)
    if pos > neg:
        impact = "positive"
    elif neg > pos:
        impact = "negative"
    else:
        impact = "neutral"
    return best, impact


def _title_date(pub_time: str | None) -> str:
    """从 market_news.pub_time 取日期部分；解析失败返回今天。"""
    if not pub_time:
        return datetime.now(_TZ).strftime("%Y-%m-%d")
    try:
        return str(pub_time)[:10]
    except Exception:  # noqa: BLE001
        return datetime.now(_TZ).strftime("%Y-%m-%d")


def _infer_concepts(title: str, max_n: int = 3) -> str:
    """从标题匹配同花顺概念名，返回逗号分隔串（长名优先，最多 max_n 个）。

    本函数为 `news_triage.impact_concepts` 的写入方，
    保证该字段持续更新。
    长名优先是为了避免「电池」抢先匹配掉「固态电池」。
    """
    try:
        from ..data.fetcher import fetch_concept_name_list

        names = fetch_concept_name_list()
    except Exception:  # noqa: BLE001
        return ""
    if not names:
        return ""
    hit: list[str] = []
    for n in sorted(names, key=len, reverse=True):
        if n in title:
            hit.append(n)
            if len(hit) >= max_n:
                break
    return ",".join(hit)


def gate_category(cat: str, is_official: bool) -> str:
    """类别闸门：`geo` / `conference` **只认官媒**，非官媒一律降级为 `other`。

    为什么：这两类属「官方口径」类（外交/地缘表态、官方会议），非官媒来源的说法
    不能当官方口径用。**写入路径与回填脚本共用同一个函数**

    `gate_category` 守门：
    过滤非官媒的 geo/conference 事件，保证一致性。
    """
    if cat in ("geo", "conference") and not is_official:
        return "other"
    return cat


def collect_from_market_news(
    conn: sqlite3.Connection,
    lookback_days: int = 2,
    dry_run: bool = False,
) -> int:
    """扫描最近 lookback_days 天的 market_news，自动提取情报事件。

    返回本次实际写入（或 dry_run 时「本会写入」）的条数。
    """
    since = (datetime.now(_TZ) - timedelta(days=lookback_days)).strftime("%Y-%m-%d 00:00:00")
    rows = conn.execute(
        """
        SELECT id, title, summary, source, link, pub_time, is_official
        FROM market_news
        WHERE pub_time >= ?
        ORDER BY pub_time DESC
        """,
        (since,),
    ).fetchall()

    inserted = 0
    for row in rows:
        title = row["title"] or ""
        summary = row["summary"] or ""
        combined = _normalize(title + " " + summary)
        if not combined:
            continue

        category, impact_dir = _classify(combined)
        if not category:
            continue

        event_date = _title_date(row["pub_time"])

        # 去重：**两张表各自独立判重**（用途不同，不能互相挡住）
        #   - intel_events：旧表，`/api/intel` 读它
        #   - news_triage ：主表，前端「事件」页读它
        dup = conn.execute(
            "SELECT 1 FROM intel_events WHERE title = ? AND event_date = ? LIMIT 1",
            (title, event_date),
        ).fetchone()
        # ⚠️ news_triage.dedup_key **无 UNIQUE 约束** → 不能用 INSERT OR IGNORE，必须先查后插
        dedup = hashlib.md5(title.encode("utf-8")).hexdigest()
        dup_triage = conn.execute(
            "SELECT 1 FROM news_triage WHERE dedup_key = ? LIMIT 1", (dedup,)
        ).fetchone()
        if dup and dup_triage:
            continue

        detail = summary.strip() or title.strip()
        source = f"auto:rss:{row['source'] or 'unknown'} | {row['link'] or ''}".strip(" | ")
        now = datetime.now(_TZ).isoformat(timespec="seconds")

        if not dry_run:
            if not dup:
                conn.execute(
                    """
                    INSERT INTO intel_events
                        (event_date, category, title, detail, status,
                         impact_boards, impact_dir, source, cross_verified,
                         created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'AI', ?)
                    """,
                    (
                        event_date,
                        category,
                        title,
                        detail,
                        "disclosed",
                        "",
                        impact_dir,
                        source,
                        now,
                    ),
                )
            if not dup_triage:
                # 写入主表 news_triage
                # 字段统一走 `triage_news`（与修补路径同一口径），并补齐
                # impact_boards（行业映射）/ impact_concepts（概念映射）/ surprise（超预期）
                from .triage import detect_surprise, triage_news

                t = triage_news(f"{title} {detail}")
                surprise = detect_surprise(f"{title} {detail}")
                # 官媒闸门：geo / conference 只认官媒，非官媒降级 other
                is_off = int(row["is_official"] or 0)
                cat_t = gate_category(t["category"], bool(is_off))
                # market 类**不做板块/概念映射**（行情播报不是事件，映射会污染传导链 —— 与一致性不变量一致）
                if cat_t == "market":
                    boards_t, concepts_t = "", ""
                else:
                    boards_t = ",".join(t["impact_boards"])
                    concepts_t = _infer_concepts(title)
                conn.execute(
                    """
                    INSERT INTO news_triage
                        (dedup_key, title, summary, sources, category, impact_dir,
                         status, credibility, first_pub_time, processed_at,
                         impact_boards, created_by, impact_concepts, surprise, is_official)
                    VALUES (?, ?, ?, ?, ?, ?, 'disclosed', ?, ?, ?, ?, 'AI', ?, ?, ?)
                    """,
                    (
                        dedup,
                        title,
                        detail,
                        row["source"] or "",
                        cat_t,
                        t["impact_dir"],
                        t["credibility"],
                        row["pub_time"] or event_date,
                        now,
                        boards_t,
                        concepts_t,
                        surprise,
                        is_off,
                    ),
                )
            conn.commit()
        inserted += 1

    return inserted


def get_intel_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """情报事件列表（供 /api/intel 读取展示）。

    返回时**拆分来源**为 `source_name` / `source_url`
    （库里的 `source` 形如 `auto:rss:财联社 | https://...`，整串当正文显示会顶爆版面）。
    **不动库值** —— URL 是唯一的回溯线索，必须原样保留。
    """
    rows = conn.execute(
        "SELECT * FROM intel_events ORDER BY event_date DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(split_source(d.get("source")))
        out.append(d)
    return out


def split_source(raw: str | None) -> dict:
    """把 `auto:rss:源名 | url` 拆成 `{source_name, source_url}`。

    只做**展示层**拆分，不改库值。
    """
    s = str(raw or "").strip()
    name, url = s, ""
    if " | " in s:
        name, url = s.split(" | ", 1)
    for prefix in ("auto:rss:", "auto:", "rss:"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return {"source_name": name.strip(), "source_url": url.strip()}


def collect_intel(conn: sqlite3.Connection, lookback_days: int = 2) -> dict:
    """触发一次自动情报采集（包装 collect_from_market_news）。供 /api/intel/collect。"""
    n = collect_from_market_news(conn, lookback_days=lookback_days, dry_run=False)
    return {
        "ok": True,
        "collected": n,
        "note": "从 market_news 自动提取政策/地缘/宏观等事件写入 intel_events",
    }
