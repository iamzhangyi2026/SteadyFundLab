# -*- coding: utf-8 -*-
"""
board_alias —— 把板块展示名映射到规范名（canonical）。
无外部依赖，离线安全。未知名称原样返回。
"""
from __future__ import annotations

# 展示名/别称 -> 规范名。覆盖常见缩写、同义、错别字。
BOARD_ALIASES = {
    # 科技
    "AI": "人工智能",
    "人工智能": "人工智能",
    "ai": "人工智能",
    "半导体": "半导体",
    "芯片": "半导体",
    "集成电路": "半导体",
    "算力": "人工智能",
    "大模型": "人工智能",
    "机器人": "机器人",
    "人形机器人": "机器人",
    # 新能源
    "新能源": "新能源",
    "光伏": "光伏",
    "锂电": "锂电池",
    "锂电池": "锂电池",
    "新能源车": "新能源汽车",
    "新能源汽车": "新能源汽车",
    "储能": "储能",
    # 金融
    "券商": "证券",
    "证券": "证券",
    "银行": "银行",
    "保险": "保险",
    # 消费
    "白酒": "白酒",
    "食品饮料": "食品饮料",
    "消费": "消费",
    "医药": "医药生物",
    "医药生物": "医药生物",
    "医疗": "医药生物",
    # 周期
    "军工": "国防军工",
    "国防军工": "国防军工",
    "有色": "有色金属",
    "有色金属": "有色金属",
    "煤炭": "煤炭",
    "钢铁": "钢铁",
    "化工": "基础化工",
    "基础化工": "基础化工",
    "石油": "石油石化",
    "石油石化": "石油石化",
    # 地产/基建
    "地产": "房地产",
    "房地产": "房地产",
    "基建": "建筑装饰",
    "建筑": "建筑装饰",
    "建筑装饰": "建筑装饰",
}


def resolve_alias(name: str) -> str:
    """把板块展示名映射为规范名；未知则原样返回。"""
    if not name:
        return ""
    key = str(name).strip()
    if key in BOARD_ALIASES:
        return BOARD_ALIASES[key]
    # 大小写不敏感再试一次
    low = key.lower()
    for k, v in BOARD_ALIASES.items():
        if k.lower() == low:
            return v
    return key


# 已知板块名（别名 + 规范名）的小写集合，供 is_known_board 快速判断
_KNOWN_BOARD_LOWER: set[str] = {
    str(x).strip().lower() for x in list(BOARD_ALIASES.keys()) + list(BOARD_ALIASES.values())
    if str(x).strip()
}


# 运行时补充的板块名：从 sector_news 表惰性加载（同花顺行业 ~90 + 概念 N）
_EXTRA_BOARD_NAMES: set[str] | None = None  # 原始大小写
_EXTRA_BOARD_LOWER: set[str] | None = None  # 小写（供快速判断）


def _load_extra_boards() -> None:
    """惰性加载补充板块名（原始 + 小写两份），进程内缓存。

    为什么不硬编码：同花顺的行业/概念列表会调整，硬编码必然过期；
    而 `sector_news` 表里存的正是抓回来的板块名，直接用它最省事 —— 无网络、毫秒级。

    本表含 `INDUSTRY_ALIASES`（行业词表）全量词条，
    保证新事件的 `impact_boards` 覆盖率。
    """
    global _EXTRA_BOARD_NAMES, _EXTRA_BOARD_LOWER
    if _EXTRA_BOARD_NAMES is not None:
        return
    names: set[str] = set()
    try:
        from ..db.schema import get_connection

        conn = get_connection()
        try:
            for r in conn.execute("SELECT DISTINCT sector FROM sector_news").fetchall():
                v = str(r[0] or "").strip()
                if v:
                    names.add(v)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 取不到就退回静态表（宁可少认，不可报错）
        pass
    _EXTRA_BOARD_NAMES = names
    _EXTRA_BOARD_LOWER = {n.lower() for n in names}


_SORTED_BOARD_NAMES: list[str] = []


def reset_board_cache() -> None:
    """清空板块名缓存（新抓完板块数据后可调用，让新板块名立即生效）。"""
    global _EXTRA_BOARD_NAMES, _EXTRA_BOARD_LOWER
    _EXTRA_BOARD_NAMES = None
    _EXTRA_BOARD_LOWER = None
    _SORTED_BOARD_NAMES.clear()


def known_board_names(min_len: int = 2, max_len: int = 8) -> list[str]:
    """全部已知板块名（原始大小写，**长名优先**）—— 供「用词表扫文本」式匹配。

    为什么不用 `event_key.extract_event_keys` 的产物：它提取的是**短语**
    （如「固态电池订单大增」），长度远超板块名，`triage_news` 的 2-5 字过滤会把它们全滤掉
    → `impact_boards` 恒空。直接用词表扫文本更可靠。

    长名优先：避免「电池」抢先匹配掉「固态电池」。
    """
    if _SORTED_BOARD_NAMES:
        return _SORTED_BOARD_NAMES
    _load_extra_boards()
    names = set(BOARD_ALIASES.keys()) | set(BOARD_ALIASES.values()) | (_EXTRA_BOARD_NAMES or set())
    out = sorted(
        {str(n).strip() for n in names if min_len <= len(str(n).strip()) <= max_len},
        key=len,
        reverse=True,
    )
    _SORTED_BOARD_NAMES.extend(out)
    return _SORTED_BOARD_NAMES


def is_known_board(name: str) -> bool:
    """该词是否**确实**是已知板块（别名 / 规范名 / 已抓到的行业与概念名）。

    为什么需要它：`resolve_alias` 对未匹配的词**原样返回**（设计如此，便于透传），
    调用方若不判断，就会把「大超预期」「景气」这类普通词当成板块名写进
    `news_triage.impact_boards`。而该字段喂给
    `screener`（主题热度）与 `review`（主题复盘）→ 噪音会直接污染主题统计。
    """
    key = str(name or "").strip()
    if not key:
        return False
    _load_extra_boards()
    low = key.lower()
    return low in _KNOWN_BOARD_LOWER or low in (_EXTRA_BOARD_LOWER or set())


if __name__ == "__main__":
    for n in ["AI", "芯片", "券商", "未知板块X"]:
        print(n, "->", resolve_alias(n))
