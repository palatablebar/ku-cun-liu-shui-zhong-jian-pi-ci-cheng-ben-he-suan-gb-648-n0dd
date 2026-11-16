"""采信入口：导入出库流水或期初余额，应用增量补丁。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from ...errors import DomainError

router = APIRouter(prefix="/api/movements", tags=["采信"])


async def _payload(request: Request, sample: str, file: UploadFile | None,
                   default_sample: str) -> tuple:
    from .service import resolve_sample

    settings = request.app.state.settings
    if file is not None and file.filename:
        return file.filename, await file.read()
    name = sample or default_sample
    if name:
        return resolve_sample(settings, name)
    raise DomainError("E_INPUT", "需要上传文件或指定样例", 400)


@router.post("/import")
async def import_movements(
    request: Request,
    sample: str = Form("movements_dirty.csv"),
    file: UploadFile | None = File(None),
):
    from ... import pipeline

    filename, raw = await _payload(request, sample, file, "movements_dirty.csv")
    return pipeline.import_movements(
        request.app.state.settings, request.app.state.store, filename, raw)


@router.post("/patch")
async def patch_movements(
    request: Request,
    sample: str = Form(""),
    file: UploadFile | None = File(None),
):
    """增量补丁：只含变化行的流水文件，与当前基线合并后重跑后半链。"""
    from ... import pipeline

    filename, raw = await _payload(request, sample, file, "")
    return pipeline.apply_patch(
        request.app.state.settings, request.app.state.store, filename, raw)


@router.get("/patches")
def list_patches(request: Request, limit: int = 20):
    store = request.app.state.store
    items = store.patches(limit)
    return {
        "count": len(items),
        "patches": [
            {
                "run_id": item["run_id"],
                "filename": item["filename"],
                "applied_at": item["applied_at"],
                "patch_sha256": item["patch_sha256"],
                "adopted": {
                    name: len(item["report"].get("adopted", {}).get(name, []))
                    for name in ("added", "upgraded", "conflict", "stale",
                                 "unchanged", "invalid", "resolved")
                },
                "rejudged": len(item["report"].get("rejudged", [])),
            }
            for item in items
        ],
    }


@router.get("/patches/{run_id}")
def patch_detail(request: Request, run_id: str):
    item = request.app.state.store.patch(run_id)
    if not item:
        raise DomainError("E_PATCH_NOT_FOUND", f"没有该补丁批次：{run_id}", 404)
    return item


@router.post("/opening")
async def import_opening(
    request: Request,
    sample: str = Form("opening_balance.csv"),
    file: UploadFile | None = File(None),
):
    from ... import pipeline

    filename, raw = await _payload(request, sample, file, "opening_balance.csv")
    return pipeline.import_opening(
        request.app.state.settings, request.app.state.store, filename, raw)


@router.get("")
def list_movements(request: Request, status: str = "", reason: str = ""):
    store = request.app.state.store
    run = store.latest_run()
    if not run:
        return {"run_id": None, "rows": [], "count": 0}
    rows = store.movements(run["run_id"], status=status or None, reason=reason or None)
    return {"run_id": run["run_id"], "count": len(rows), "rows": rows}
