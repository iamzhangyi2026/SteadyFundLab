# -*- coding: utf-8 -*-
"""B 区 OCR 导入管道：截图 → OCR → 结构化候选行（供人工核对）。

OCR 引擎：rapidocr-onnxruntime（本地离线，不联网）。未安装时抛明确错误，
前端降级为「人工粘贴文本」。

识别结果**一律不自动入库**，必须经人工确认——截图排版千差万别，
抽错一个数字就可能把真仓算错。不确定的行一律标 needs_review。
"""
from __future__ import annotations

import re

_engine = None


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def ocr_image(image_path: str) -> list[str]:
    """对截图做 OCR，返回识别出的文本行。"""
    if not ocr_available():
        raise RuntimeError("OCR 引擎未安装（rapidocr-onnxruntime），请改用人工文本录入")
    result, _ = _get_engine()(str(image_path))
    return [item[1] for item in (result or []) if len(item) > 1 and item[1]]


_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")

# 截图字段标签（支付宝 / 券商叫法不一，都收进来）
_LABELS = {
    "market_amount": ("市值", "持有金额", "参考市值", "金额"),
    "cost_amount": ("成本", "持仓成本", "成本金额"),
    "shares": ("份额", "持有份额"),
    "profit": ("浮动盈亏", "盈亏", "收益"),
    "profit_rate": ("收益率", "盈亏率", "涨幅"),
}


def _grab(seg: str, label: str):
    m = re.search(re.escape(label) + r"[^\d\-]{0,6}(-?\d[\d,]*\.?\d*)", seg)
    return float(m.group(1).replace(",", "")) if m else None


def parse_rows(lines: list[str]) -> list[dict]:
    """从文本行做启发式抽取，结果一律标 needs_review=1（必须人工核对）。

    先按字段标签（市值 / 成本 / 份额 / 盈亏）取值，取不到再按位置兜底。
    注意先剔除基金代码本身，否则「005827」会被当成一个数字。
    """
    rows = []
    for ln in lines:
        m = _CODE_RE.search(ln)
        if not m:
            continue
        code = m.group(1)
        seg = ln[: m.start()] + " " + ln[m.end():]  # 去掉代码，避免把代码当数字
        row: dict = {"fund_code": code, "needs_review": 1}

        name = re.sub(r"[\d,.\-+%:：]", " ", seg)
        for kw in ("市值", "持有金额", "参考市值", "金额", "成本", "持仓成本",
                   "份额", "持有份额", "浮动盈亏", "盈亏", "收益", "收益率", "盈亏率", "涨幅"):
            name = name.replace(kw, " ")
        row["fund_name"] = re.sub(r"\s+", " ", name).strip() or None

        for field, labels in _LABELS.items():
            for lb in labels:
                v = _grab(seg, lb)
                if v is not None:
                    row[field] = v
                    break

        # 位置兜底（仅当该行一个标签都没命中时）：placeholder 约定顺序
        # 「代码 名称 份额 成本 市值 盈亏 [收益率]」。
        # 百分比单独抓，并统一换成小数口径——库里 profit_rate 是小数，
        # 界面也按 profit_rate*100 显示，两者必须一致。
        if not any(k in row for k in _LABELS):
            nums = [float(x.replace(",", "")) for x in _NUM_RE.findall(seg)
                    if x not in ("", "-", ".", "-.")]
            for i, f in enumerate(("shares", "cost_amount", "market_amount", "profit")):
                if len(nums) > i:
                    row[f] = nums[i]
            pm = re.search(r"(-?\d+(?:\.\d+)?)\s*%", seg)
            if pm:
                row["profit_rate"] = float(pm.group(1)) / 100.0
        rows.append(row)
    return rows


def import_from_image(image_path: str) -> dict:
    lines = ocr_image(image_path)
    return {"source": "ocr", "raw_text": "\n".join(lines), "rows": parse_rows(lines)}


def import_from_text(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return {"source": "manual", "raw_text": text or "", "rows": parse_rows(lines)}
