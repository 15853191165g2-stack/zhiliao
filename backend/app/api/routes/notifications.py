"""信息流与详情。首页、筛选、搜索、收藏、已读、置顶都在这。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.serializers import to_detail, to_out
from app.core.timeutil import now_utc
from app.db import get_session
from app.models import Notification
from app.schemas import (
    ApiResponse,
    NotificationBulkFlags,
    NotificationDetail,
    NotificationFlags,
    NotificationOut,
    Page,
)
from app.services.ingest import IngestService

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _apply_filters(
    stmt,
    *,
    source_id: int | None,
    category: str | None,
    importance: str | None,
    q: str | None,
    unread_only: bool,
    read_only: bool,
    starred_only: bool,
    pinned_only: bool,
    days: int | None,
):
    if source_id is not None:
        stmt = stmt.where(Notification.source_id == source_id)
    if category:
        stmt = stmt.where(Notification.category == category)
    if importance:
        stmt = stmt.where(Notification.importance == importance)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    if read_only:
        stmt = stmt.where(Notification.is_read.is_(True))
    if starred_only:
        stmt = stmt.where(Notification.is_starred.is_(True))
    if pinned_only:
        stmt = stmt.where(Notification.is_pinned.is_(True))
    if days:
        stmt = stmt.where(Notification.created_at >= now_utc() - timedelta(days=days))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Notification.title.like(like),
                Notification.excerpt.like(like),
                Notification.content_text.like(like),
            )
        )
    return stmt


@router.get("", response_model=ApiResponse[Page[NotificationOut]])
async def list_notifications(
    source_id: int | None = None,
    category: str | None = None,
    importance: str | None = None,
    q: str | None = None,
    unread_only: bool = False,
    read_only: bool = False,
    starred_only: bool = False,
    pinned_only: bool = False,
    days: int | None = Query(None, ge=1, le=3650),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    filters = dict(
        source_id=source_id,
        category=category,
        importance=importance,
        q=q,
        unread_only=unread_only,
        read_only=read_only,
        starred_only=starred_only,
        pinned_only=pinned_only,
        days=days,
    )

    count_stmt = _apply_filters(
        select(func.count()).select_from(Notification), **filters
    )
    total = int((await session.execute(count_stmt)).scalar() or 0)

    stmt = (
        _apply_filters(
            select(Notification).options(selectinload(Notification.source)), **filters
        )
        # ★ 置顶优先：置顶的永远浮在最前，其余按发布时间倒序。
        #   SQLite 里 Boolean 落 0/1，desc() 就是「有置顶的在前面」。
        .order_by(
            Notification.is_pinned.desc(),
            Notification.published_at.desc().nulls_last(),
            Notification.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return ApiResponse.ok(
        Page[NotificationOut](
            items=[to_out(n) for n in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get("/{notification_id}", response_model=ApiResponse[NotificationDetail])
async def get_notification(
    notification_id: int,
    session: AsyncSession = Depends(get_session),
):
    row = await _get_or_404(session, notification_id)
    return ApiResponse.ok(to_detail(row))


@router.post("/{notification_id}/read", response_model=ApiResponse[NotificationDetail])
async def mark_read(
    notification_id: int,
    flags: NotificationFlags | None = None,
    session: AsyncSession = Depends(get_session),
):
    row = await _get_or_404(session, notification_id)
    row.is_read = True if flags is None or flags.is_read is None else flags.is_read
    await session.commit()
    await session.refresh(row, attribute_names=["source"])
    return ApiResponse.ok(to_detail(row))


@router.post("/{notification_id}/star", response_model=ApiResponse[NotificationDetail])
async def toggle_star(
    notification_id: int,
    flags: NotificationFlags | None = None,
    session: AsyncSession = Depends(get_session),
):
    row = await _get_or_404(session, notification_id)
    row.is_starred = (
        (not row.is_starred) if flags is None or flags.is_starred is None else flags.is_starred
    )
    await session.commit()
    await session.refresh(row, attribute_names=["source"])
    return ApiResponse.ok(to_detail(row))


@router.post("/{notification_id}/pin", response_model=ApiResponse[NotificationDetail])
async def toggle_pin(
    notification_id: int,
    flags: NotificationFlags | None = None,
    session: AsyncSession = Depends(get_session),
):
    """置顶 / 取消置顶。

    不传 body 或 is_pinned 为 null → 取反（和 /star 一致的语义）。
    置顶只是「排在列表最前」，不会改动 published_at，也不影响其它筛选。
    """
    row = await _get_or_404(session, notification_id)
    row.is_pinned = (
        (not row.is_pinned) if flags is None or flags.is_pinned is None else flags.is_pinned
    )
    await session.commit()
    await session.refresh(row, attribute_names=["source"])
    return ApiResponse.ok(to_detail(row))


@router.post("/read-all", response_model=ApiResponse[dict])
async def mark_all_read(
    body: NotificationBulkFlags,
    session: AsyncSession = Depends(get_session),
):
    """批量改已读状态。

    - 传了 ids  → 只处理这些 id（上限 500，防止一次改爆）。
    - ids 为空 → 处理**全部**状态需要变更的通知。
    路径故意用 `/read-all` 这种单段形式，避免和 `/{notification_id}/read` 抢匹配。

    ⚠️ 基础条件是「状态与目标相反」，不是写死 `is_read=False`：
       否则标为未读时一条都更新不到，而且 `updated` 会永远是 0/看起来没生效。
    """
    from sqlalchemy import update

    stmt = update(Notification).where(Notification.is_read.is_(not body.is_read))
    if body.ids:
        stmt = stmt.where(Notification.id.in_(body.ids))
    result = await session.execute(stmt.values(is_read=body.is_read))
    await session.commit()
    return ApiResponse.ok({"updated": int(result.rowcount or 0), "is_read": body.is_read})


@router.post("/{notification_id}/summarize", response_model=ApiResponse[NotificationDetail])
async def resummarize(
    notification_id: int,
    session: AsyncSession = Depends(get_session),
):
    """手动重跑 AI。原文被改过、或上次摘要失败时用。"""
    row = await _get_or_404(session, notification_id)
    service = IngestService(session)
    await service.summarize_one(row)
    await session.commit()
    await session.refresh(row, attribute_names=["source"])
    return ApiResponse.ok(to_detail(row))


async def _get_or_404(session: AsyncSession, notification_id: int) -> Notification:
    row = (
        await session.execute(
            select(Notification)
            .options(selectinload(Notification.source))
            .where(Notification.id == notification_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"通知不存在：{notification_id}")
    return row
