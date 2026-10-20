"""来源管理。对应 App 的「来源管理」页。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import available_kinds, get_adapter
from app.api.serializers import to_source_out
from app.core.config import get_settings
from app.db import get_session
from app.models import Source
from app.schemas import ApiResponse, SourceCreate, SourceOut, SourceUpdate
from app.services.ingest import IngestService
from app.services.source_config import dump_source_to_yaml, sync_yaml_to_db

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=ApiResponse[list[SourceOut]])
async def list_sources(
    enabled_only: bool = False,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Source).order_by(Source.enabled.desc(), Source.id)
    if enabled_only:
        stmt = stmt.where(Source.enabled.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return ApiResponse.ok([to_source_out(s) for s in rows])


@router.get("/adapters", response_model=ApiResponse[list[dict]])
async def list_adapters():
    """列出可用适配器，App 端「添加来源」下拉框用。"""
    out = []
    for kind in available_kinds():
        adapter = get_adapter(kind)
        out.append(
            {
                "kind": kind,
                "doc": (adapter.__doc__ or "").strip().splitlines()[0] if adapter.__doc__ else "",
            }
        )
    return ApiResponse.ok(out)


@router.post("", response_model=ApiResponse[SourceOut])
async def create_source(
    payload: SourceCreate,
    session: AsyncSession = Depends(get_session),
):
    if payload.adapter not in available_kinds():
        raise HTTPException(400, f"未知适配器：{payload.adapter}")
    row = Source(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ApiResponse.ok(to_source_out(row))


@router.patch("/{source_id}", response_model=ApiResponse[SourceOut])
async def update_source(
    source_id: int,
    payload: SourceUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = await _get_or_404(session, source_id)
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(row)
    return ApiResponse.ok(to_source_out(row))


@router.delete("/{source_id}", response_model=ApiResponse[dict])
async def delete_source(
    source_id: int,
    keep_notifications: bool = Query(True, description="true 只删来源，保留已抓到的通知"),
    session: AsyncSession = Depends(get_session),
):
    row = await _get_or_404(session, source_id)
    if keep_notifications:
        from sqlalchemy import update

        from app.models import Notification

        await session.execute(
            update(Notification)
            .where(Notification.source_id == source_id)
            .values(source_id=None)
        )
        await session.delete(row)
    else:
        await session.delete(row)  # 级联删除通知
    await session.commit()
    return ApiResponse.ok({"deleted": source_id, "kept_notifications": keep_notifications})


@router.post("/{source_id}/fetch", response_model=ApiResponse[dict])
async def fetch_one(
    source_id: int,
    session: AsyncSession = Depends(get_session),
):
    await _get_or_404(session, source_id)
    service = IngestService(session)
    result = await service.run(source_ids=[source_id])
    return ApiResponse.ok(result.model_dump())


@router.post("/reload", response_model=ApiResponse[dict])
async def reload_from_yaml(
    session: AsyncSession = Depends(get_session),
):
    """重新读取 sources/*.yaml 并同步到数据库。

    这是「新增一所学校只需写一份配置」的入口：
    写好 yaml，调一次这个接口，App 里就有了。
    """
    stats = await sync_yaml_to_db(session)
    return ApiResponse.ok(stats)


@router.post("/{source_id}/export", response_model=ApiResponse[dict])
async def export_source(
    source_id: int,
    session: AsyncSession = Depends(get_session),
):
    """导出成 yaml，方便分享给同学。"""
    row = await _get_or_404(session, source_id)
    path: Path = dump_source_to_yaml(row)
    return ApiResponse.ok({"file": str(path), "root": str(get_settings().sources_dir)})


async def _get_or_404(session: AsyncSession, source_id: int) -> Source:
    row = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"来源不存在：{source_id}")
    return row
