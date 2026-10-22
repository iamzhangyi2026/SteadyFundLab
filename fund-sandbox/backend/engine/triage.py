# -*- coding: utf-8 -*-
"""
triage —— 新闻分拣引擎。
- triage_news: 用简单规则把一段新闻文本解析为 {category, impact_dir, credibility, impact_boards, summary}
- get_triage / stream_triage: 读 news_triage
- run_triage: 对缺失分类的行重新打分（compact），返回计数
离线安全，绝不抛异常。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from ..db.schema import get_connection
from . import board_alias
# 板块映射不引入 `event_key` 候选词提取，直接用 board_alias 词表扫文本
# （候选词层会带来短语长度超标问题，见下方「板块映射」两层说明）。

CATEGORIES = [
    "macro", "policy", "geo", "company", "fund",
    "sector", "market", "military", "other",
]
CREDIBILITY_LEVELS = {"high", "medium", "low"}
IMPACT_DIRS = {"positive", "negative", "neutral"}

# 分类关键词（小写匹配）
#  扩充：① market 类补充「带幅度动词」映射
#   ② 补公司行动类词（公司行动类动态容易落 other）
# 原则：只加高特异性词，避免泛词误伤（如「国际/行业/份额/装备」在非对应语境误命中率太高，已收窄）。
_CAT_KW = {
    "policy": ["政策", "央行", "降准", "加息", "监管", "财政部", "国务院", "发文", "新规"],
    "geo": ["美国", "中东", "地缘", "欧盟", "制裁", "关税", "乌克兰", "俄罗斯"],
    "company": [
        "公司", "财报", "业绩", "营收", "利润", "董事长", "高管", "ipo", "并购",
        "推出", "上线", "上架", "交付", "起诉", "诉讼", "首席执行官", "ceo",
        "收购", "债务", "订阅", "裁员", "罢工", "召回",
    ],
    "fund": ["基金", "定投", "净值", "基金经理", "申购", "赎回"],
    "sector": ["板块", "赛道", "产业", "概念"],
    "market": [
        "股市", "a股", "港股", "美股", "指数", "涨跌", "收盘", "成交量", "行情",
        "涨超", "跌超", "站上", "跌破", "飙升", "跳水", "涨停", "跌停",
        "创新高", "历史新高", "涨幅", "跌幅", "股价",
    ],
    "military": ["军事", "军演", "国防", "导弹", "战机", "军队"],
    "macro": ["gdp", "通胀", "cpi", "ppi", "失业率", "经济", "利率", "非农", "衰退"],
}

# 影响方向关键词
_POS_KW = ["上涨", "利好", "增长", "提升", "超预期", "大涨", "复苏", "新高", "盈利", "上调", "突破"]
_NEG_KW = ["下跌", "利空", "下滑", "下降", "低于预期", "大跌", "暴跌", "亏损", "下调", "风险", "裁员", "违约"]

_SRC_HIGH = {"新华社", "人民日报", "央视", "央行", "证监会", "新华社客户端", "证券时报", "中国证券报"}


def _classify(text: str) -> str:
    low = text.lower()
    for cat, kws in _CAT_KW.items():
        if any(kw in low for kw in kws):
            return cat
    return "other"


def _impact_dir(text: str) -> str:
    low = text.lower()
    pos = sum(1 for k in _POS_KW if k in low)
    neg = sum(1 for k in _NEG_KW if k in low)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _credibility(text: str, source: str = "") -> str:
    if source and any(s in source for s in _SRC_HIGH):
        return "high"
    if re.search(r"\d", text) and any(k in text for k in ["公司", "央行", "工信部", "统计局"]):
        return "medium"
    return "low"


# 「超预期 / 不及预期」表述词
_SURPRISE_POS = ["超预期", "超出预期", "好于预期", "远超预期", "高于预期", "大超预期"]
_SURPRISE_NEG = ["不及预期", "低于预期", "差于预期", "远低于预期", "逊于预期"]


def detect_surprise(text: str) -> Optional[str]:
    """识别「超预期 / 不及预期」表述 → `"positive"` / `"negative"` / `None`。

    ⚠️ **只标表述方向，不猜预期值** —— 本项目没有结构化预期数据（如一致预期 EPS），
    编一个"预期值"再算偏离度就是造假。有表述就标，没有就 None。
    """
    t = str(text or "")
    if any(k in t for k in _SURPRISE_POS):
        return "positive"
    if any(k in t for k in _SURPRISE_NEG):
        return "negative"
    return None


def signal_strength(ev: Dict[str, Any]) -> Dict[str, object]:
    """事件信号强度 0–100 + 档位（强 / 中 / 弱）—— **只读展示，不含任何仓位 / 交易建议**。

    综合已落库的**事实字段**加权，不猜预期值（项目无结构化预期数据）：
    - 超预期表述（surprise）权重最高 —— 新闻的价值在「超预期」；
    - 可信度（credibility，多源交叉验证程度）次之；
    - 官方源（is_official）、有传导目标（impact_boards / impact_concepts，能连到行业才可分析）、
      方向明确（impact_dir 非 neutral）依次递补。

    为什么读时算而不落库：强度是展示派生量、权重可随时调；落库需全量回填、
    权重变更要再迁移，读时算零回填风险。
    """
    score = 0
    if ev.get("surprise") in ("positive", "negative"):
        score += 40
    score += {"high": 30, "medium": 15, "low": 0}.get(ev.get("credibility"), 0)
    if ev.get("is_official"):
        score += 15
    if ev.get("impact_boards") or ev.get("impact_concepts"):
        score += 10
    if ev.get("impact_dir") in ("positive", "negative"):
        score += 5
    level = "强" if score >= 70 else ("中" if score >= 40 else "弱")
    return {"signal_strength": score, "signal_level": level}


# =============================================================================
# 事件 → 行业映射 · 第①层（类别规则）
# =============================================================================
#
# 本映射为两层：第①层类别规则 + 第②层行业词反查。
# 映射表来源见事件-行业映射表。
#
# 为什么必须有第①层：`geo` / `policy` / `macro` / `conference` 这几类事件的**影响面由事件类型决定**，
# 而不是由文本里出现了哪个行业词决定 —— 例如「对华出口管制」本身不含任何行业词，
# 但它明确利好**半导体/军工/信创/稀土**（国产替代逻辑）。只靠词表扫文本永远抓不到这层因果。
#
# 本表只产出分析/提示，不做任何自动交易、不引入兜底。
EVENT_BOARD_RULES: Dict[str, List[tuple]] = {
    "geo": [
        (["制裁", "出口管制", "实体清单", "管制", "禁令"], ["半导体", "国防军工", "计算机", "稀土"]),
        (["中东", "霍尔木兹", "以色列", "伊朗", "红海"], ["石油石化", "国防军工", "贵金属"]),
        (["俄乌", "俄罗斯", "乌克兰"], ["石油石化", "燃气", "国防军工", "农产品"]),
        (["关税", "贸易摩擦", "贸易战"], ["家用电器", "纺织服饰"]),
        (["金砖", "上合", "去美元化", "本币结算"], ["贵金属", "计算机"]),
        (["OPEC", "减产", "增产"], ["石油石化"]),
    ],
    "macro": [
        (["非农", "美联储", "加息", "降息", "议息"], ["贵金属", "半导体"]),
        (["CPI", "PPI", "通胀"], ["食品饮料", "基础化工"]),
        (["PMI", "景气", "回升"], ["建筑材料", "机械设备", "基础化工"]),
        (["社融", "M2", "信贷", "流动性"], ["证券", "房地产", "银行"]),
    ],
    "policy": [
        (["降准", "降息", "MLF", "LPR", "宽松"], ["证券", "房地产", "银行"]),
        (["专项债", "基建", "投资加码"], ["建筑装饰", "建筑材料", "机械设备"]),
        (["大基金", "产业扶持", "补贴", "国产替代"], ["半导体", "电力设备"]),
        (["监管收紧", "反垄断", "整改"], ["计算机", "传媒"]),
    ],
    "conference": [
        (["两会", "十五五", "规划"], ["建筑装饰", "电力设备", "计算机"]),
        (["中央经济工作会议"], ["房地产", "食品饮料", "银行"]),
        (["G20", "APEC", "博鳌", "一带一路", "峰会"], ["建筑装饰", "机械设备"]),
    ],
    "military": [
        (["军演", "演习", "国防", "军费", "军贸"], ["国防军工"]),
        (["装备", "订单", "列装"], ["国防军工"]),
    ],
}

# 最多保留 3 个板块：「玻璃基板概念拉升」可能映射出多个板块，等于没重点，
# 下游还要逐个做资金验证，纯增噪声（沿用 `事件-行业映射表.md` 的质量约束）。
_MAX_BOARDS = 3


def _infer_boards_by_category(text: str, category: Optional[str]) -> List[str]:
    """第①层映射：按**事件类别**的触发词规则映射受影响板块。"""
    out: List[str] = []
    for triggers, boards in EVENT_BOARD_RULES.get(str(category or ""), []):
        if not any(t in text for t in triggers):
            continue
        for b in boards:
            if b not in out:
                out.append(b)
                if len(out) >= _MAX_BOARDS:
                    return out
    return out


def triage_news(text: str, category: Optional[str] = None) -> Dict[str, Any]:
    """解析一段新闻文本为结构化字段。任何异常都返回安全默认。"""
    try:
        text = str(text or "")
        summary = text[:120].strip()
        if category and category in CATEGORIES:
            cat = category
        else:
            cat = _classify(text)
        impact_dir = _impact_dir(text)
        credibility = _credibility(text)
        # ── 板块映射：两层──
        # ① 类别规则：事件类别（geo/macro/policy/conference/military）按触发词 → 板块。
        #    补的是「事件类型决定影响面」那层因果（如"出口管制"→半导体/军工/信创，文本里并无行业词）。
        # ② 行业词反查：文本直接出现行业词（锂电池/光伏/创新药…）→ 映射到该行业。
        #    直接扫词表，而非先提取候选短语：短语（如「固态电池订单大增」）会因长度超标
        #    被行业名匹配整体滤掉，导致 impact_boards 恒空。
        boards: List[str] = []
        for b in _infer_boards_by_category(text, cat):
            canonical = board_alias.resolve_alias(b)
            if canonical and canonical not in boards:
                boards.append(canonical)
        for name in board_alias.known_board_names():
            if len(boards) >= _MAX_BOARDS:
                break
            if name in text:
                canonical = board_alias.resolve_alias(name)
                if canonical and canonical not in boards:
                    boards.append(canonical)
        return {
            "category": cat,
            "impact_dir": impact_dir,
            "credibility": credibility,
            "impact_boards": boards,
            "summary": summary,
        }
    except Exception:
        return {
            "category": category or "other",
            "impact_dir": "neutral",
            "credibility": "low",
            "impact_boards": [],
            "summary": (text or "")[:120],
        }


def get_triage(conn: sqlite3.Connection, category: str = "all", limit: int = 300) -> List[dict]:
    """读取 news_triage。category='all' 不过滤。"""
    try:
        sql = "SELECT * FROM news_triage"
        params: tuple = ()
        if category and category != "all":
            sql += " WHERE category = ?"
            params = (category,)
        sql += " ORDER BY id DESC LIMIT ?"
        params = params + (int(limit),)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def stream_triage(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    """流式 UI 用：等价于 get_triage(all)。"""
    return get_triage(conn, category="all", limit=limit)


def run_triage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """对缺失 category / impact_dir 的行重新打分（compact）。返回计数。"""
    rescored = 0
    total = 0
    try:
        rows = conn.execute(
            "SELECT id, title, summary, sources, category, impact_dir, credibility "
            "FROM news_triage WHERE category IS NULL OR category='' OR impact_dir IS NULL OR impact_dir=''"
        ).fetchall()
        for r in rows:
            text = f"{r['title'] or ''} {r['summary'] or ''}"
            res = triage_news(text, category=r["category"])
            cat = r["category"] or res["category"]
            impact = r["impact_dir"] or res["impact_dir"]
            cred = r["credibility"] or res["credibility"]
            conn.execute(
                "UPDATE news_triage SET category=?, impact_dir=?, credibility=? WHERE id=?",
                (cat, impact, cred, r["id"]),
            )
            rescored += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM news_triage").fetchone()[0]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return {"rescored": rescored, "total": total}


if __name__ == "__main__":
    conn = get_connection()
    try:
        print("triage_news demo:", triage_news("央行宣布降准0.25个百分点，半导体板块受益"))
        print("get_triage count:", len(get_triage(conn, limit=5)))
        print("run_triage:", run_triage(conn))
    finally:
        conn.close()
