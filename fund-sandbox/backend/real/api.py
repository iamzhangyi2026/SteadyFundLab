# -*- coding: utf-8 -*-
"""B 区 API 路由（独立命名空间 /api/real/*）。

主 app 只 include 本 router，不在 A 区逻辑里写 B 区代码——保持边界清晰。
所有写操作（确认入库、删除）都视为「人」的动作；AI 不应调用这些写端点。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.real import store, ocr_import, session
from backend.real.db import init_db

SHOTS_DIR = Path(__file__).resolve().parents[2] / "data" / "real_shots"

router = APIRouter(prefix="/api/real", tags=["real"])

init_db()  # 幂等建表，保证 B 区库可用


@router.get("/holdings")
def list_holdings():
    return {"holdings": store.list_holdings(), "summary": store.summary()}


@router.get("/snapshots")
def list_snapshots(limit: int = 90):
    return {"snapshots": store.list_snapshots(limit)}


@router.get("/imports")
def list_imports(limit: int = 20):
    return {"imports": store.list_imports(limit)}


@router.get("/imports/{import_id}")
def get_import(import_id: int):
    imp = store.get_import(import_id)
    if not imp:
        raise HTTPException(404, "import not found")
    return imp


@router.get("/ocr-status")
def ocr_status():
    return {"available": ocr_import.ocr_available()}


@router.post("/imports/text")
def import_text(payload: dict):
    """人工粘贴文本 → 候选行（不自动入库）。"""
    res = ocr_import.import_from_text(payload.get("text", ""))
    iid = store.create_import(res["source"], res["raw_text"])
    store.add_import_rows(iid, res["rows"])
    return {"import_id": iid, **res}


@router.post("/imports/image")
async def import_image(file: UploadFile = File(...)):
    """上传截图 → OCR → 候选行（不自动入库）。截图用完即清。"""
    if not ocr_import.ocr_available():
        raise HTTPException(503, "OCR 引擎未安装，请改用文本录入")
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1] or ".png"
    p = SHOTS_DIR / f"{uuid.uuid4().hex}{ext}"
    p.write_bytes(await file.read())
    try:
        res = ocr_import.import_from_image(str(p))
    finally:
        try:
            p.unlink()
        except Exception:
            pass
    iid = store.create_import(res["source"], res["raw_text"])
    store.add_import_rows(iid, res["rows"])
    return {"import_id": iid, **res}


class ConfirmPayload(BaseModel):
    rows: list[dict]


@router.post("/imports/{import_id}/confirm")
def confirm(import_id: int, payload: ConfirmPayload):
    return store.confirm_import(import_id, payload.rows)


@router.post("/imports/{import_id}/discard")
def discard(import_id: int):
    return store.discard_import(import_id)


@router.delete("/holdings/{fund_code}")
def delete_holding(fund_code: str):
    return store.delete_holding(fund_code)


@router.post("/nav/refresh")
def refresh_nav():
    return store.refresh_nav()


@router.post("/estimates/refresh")
def refresh_estimates():
    """只刷新盘中估值（轻量，供前端定时轮询）。"""
    return store.refresh_estimates()


@router.post("/snapshots")
def create_snapshot():
    """手动落一条总览快照。"""
    return store.save_snapshot()


@router.get("/poll-window")
def poll_window():
    """当前是否应轮询（盘中 09:30-15:00 / 净值披露 20:00-24:00，按节假日判断）。"""
    return session.poll_window(store.list_holdings())


@router.get("/holdings/{fund_code}/detail")
def holding_detail(fund_code: str):
    """单只真实持仓的详情 + 深度分析（净值序列 / 指标 / 六段式分析文字）。

    分析由**代码规则**生成（`analysis._sections`，SOP Step 7 六段式），不调用外部大模型。
    """
    from backend.real import analysis

    holding = store.get_holding(fund_code)
    if not holding:
        raise HTTPException(404, "holding not found")
    res = analysis.analyze(fund_code, holding.get("fund_name"), holding)
    return {"holding": holding, **res}
