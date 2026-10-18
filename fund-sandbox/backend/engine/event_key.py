# -*- coding: utf-8 -*-
"""
event_key —— 从新闻/情报文本中抽取关键词与实体（简单规则，无需外部依赖）。
离线安全：纯正则，绝不抛异常。
"""
from __future__ import annotations

import re
from typing import List

# 截取 2-8 个连续中文（可作为候选实体/板块）
_CN_RE = re.compile(r"[一-龥]{2,8}")
# 英文/数字实体（含 . _ - 的公司/代码）
_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._\-]{1,24}")
# 常见金融实体前缀，用于提升权重（这里只做保留，不强制）
_STOPWORDS = {
    "我们", "他们", "市场", "今日", "昨日", "表示", "宣布", "报道", "消息", "根据",
    "公司", "中国", "美国", "数据", "显示", "方面", "已经", "可能", "以及", "对于",
    "一个", "这一", "目前", "相关", "基金", "股票", "板块", "投资", "预计", "由于",
}


def extract_event_keys(text: str) -> List[str]:
    """从文本中抽取关键词/实体列表（去重、按出现顺序）。"""
    if not text:
        return []
    text = str(text)
    keys: List[str] = []
    seen = set()

    # 中文候选
    for m in _CN_RE.findall(text):
        if m in _STOPWORDS:
            continue
        if m not in seen:
            seen.add(m)
            keys.append(m)

    # 英文/代码候选（去小写重复）
    for m in _EN_RE.findall(text):
        low = m.lower()
        if low in seen:
            continue
        seen.add(low)
        keys.append(m)

    return keys


if __name__ == "__main__":
    sample = "Apple's stock rises after iPhone reveal; 央行宣布降准0.25个百分点，半导体板块受益。"
    print("KEYS:", extract_event_keys(sample))
