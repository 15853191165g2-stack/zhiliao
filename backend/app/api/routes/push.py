"""推送接口（阶段 3）。

分工：
  - `/push/pending` + `/push/ack` —— **客户端**用。拉取未推送消息、弹通知、回执确认。
    刻意拆成两步（拉取只读、确认才写），见 schemas.PushAckRequest 的说明。
  - `/push/run` + `/push/test` —— **服务端/调试**用。手动推一遍、发一条自检消息。
  - `/push/channels` + `/push/config` —— 让人一眼看清「现在到底会走哪些通道、配好了没」。
  - `/push/logs` —— 审计。用户说「我没收到」时，靠它回答「推没推、报了什么错」。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import get_session
from app.models import PushLog
from app.schemas import ApiResponse, PushAckRequest, PushRunRequest, PushTestRequest
from app.services.push import PushService
from app.core.timeutil import to_local_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", tags=["push"])


def _mask_url(url: str) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return "(已配置，格式无法解析)"
    host = p.hostname or ""
    return f"{p.scheme}://{host}/…（密钥已隐藏，共 {len(url)} 字符）"


@router.get("/config", response_model=ApiResponse[dict])
async def push_config(session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    svc = PushService(session, settings)
    stats = await svc.stats()
    return ApiResponse.ok(
        {
            "enabled": settings.push_enabled,
            "channels": settings.push_channel_list(),
            "available_channels": [c["kind"] for c in svc.channels_report()],
            "lookback_hours": settings.push_lookback_hours,
            "batch_limit": settings.push_batch_limit,
            "min_importance": settings.push_min_importance,
            "quiet_hours": settings.push_quiet_hours or None,
            "quiet_now": stats["quiet_now"],
            "silent_first_run": settings.push_silent_first_run,
            "unsent": stats["unsent"],
            "baseline_at": to_local_iso(_parse_iso(stats["baseline_at"])),
            "baseline_count": stats["baseline_count"],
            "failed_logs": stats["failed_logs"],
            "webhook": {
                "configured": bool(settings.push_webhook_url),
                "kind": settings.push_webhook_kind,
                # ⚠️ 不返回完整 URL —— 企业微信/钉钉/Server酱 的地址里带的就是密钥，
                #    回显等于把密钥写进客户端日志。只给域名 + 长度。
                "target": _mask_url(settings.push_webhook_url),
            },
        }
    )


def _parse_iso(text: str | None):
    """AppMeta 里存的是 naive UTC ISO。解析失败不抛，返回 None。"""
    if not text:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@router.get("/channels", response_model=ApiResponse[list[dict]])
async def push_channels(session: AsyncSession = Depends(get_session)):
    """通道体检：每个通道「被选中了吗 / 参数齐了吗 / 是外发吗 / 干什么的」。"""
    svc = PushService(session, get_settings())
    return ApiResponse.ok(svc.channels_report())


@router.get("/pending", response_model=ApiResponse[dict])
async def push_pending(
    limit: int = Query(20, ge=1, le=200),
    since_id: int | None = Query(
        None,
        ge=0,
        description=(
            "客户端游标：只看 id 比它大的。**推荐多设备都这么用** —— "
            "服务端只读不写，各设备互不抢消息。"
            "不传则返回最新的若干条。"
        ),
    ),
    session: AsyncSession = Depends(get_session),
):
    """客户端拉取未推送的消息。

    两种用法：
      - **游标模式（推荐）**：首次先不带 `since_id` 调一次，把返回的 `max_id`
        存下来当游标（**不要展示这一批** —— 那是历史消息）；之后每次带
        `since_id=游标` 拉新消息，拉到后把游标推进到返回的 `next_since_id`。
      - 不给游标：直接返回最新几条，适合调试。

    ⚠️ 本接口**只读**，不会标记任何东西。想让消息不再重复出现，
    用游标（推荐）或者调 `/push/ack`。
    """
    svc = PushService(session, get_settings())
    msgs = await svc.pending_messages(limit=limit, since_id=since_id)
    max_id = await svc.max_notification_id()
    # 游标模式下，客户端要把游标推到「这一批里最大的 id」；
    # 没拉到东西时保持原样。
    next_cursor = max((m.notification_id for m in msgs), default=None)
    return ApiResponse.ok(
        {
            "items": [m.as_dict() for m in msgs],
            "total": len(msgs),
            "max_id": max_id,
            "next_since_id": next_cursor if next_cursor is not None else since_id,
            "quiet_now": svc.in_quiet_hours(),
        }
    )


@router.post("/ack", response_model=ApiResponse[dict])
async def push_ack(
    body: PushAckRequest,
    session: AsyncSession = Depends(get_session),
):
    """客户端确认已弹过通知。幂等：重复确认不会改变结果。"""
    svc = PushService(session, get_settings())
    n = await svc.ack(body.ids)
    return ApiResponse.ok({"updated": n, "requested": len(body.ids)})


@router.post("/run", response_model=ApiResponse[dict])
async def push_run(
    body: PushRunRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """手动触发一次推送（走真实通道）。"""
    payload = body or PushRunRequest()
    svc = PushService(session, get_settings())
    stats = await svc.push_pending(limit=payload.limit, force=payload.force)
    return ApiResponse.ok(stats)


@router.post("/test", response_model=ApiResponse[dict])
async def push_test(
    body: PushTestRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """给所有已启用通道发一条自检消息，返回每个通道的真实结果。

    用它来验证「企业微信机器人地址配对了吗」这类问题 ——
    不需要等真有新通知。
    """
    payload = body or PushTestRequest()
    svc = PushService(session, get_settings())
    results = await svc.send_test(payload.text)
    ok = any(r.ok for r in results)
    return ApiResponse.ok(
        {
            "ok": ok,
            "results": [
                {
                    "channel": r.channel,
                    "ok": r.ok,
                    "detail": r.detail,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                }
                for r in results
            ],
        },
        message="ok" if ok else "没有任何通道发送成功",
    )


@router.get("/logs", response_model=ApiResponse[list[dict]])
async def push_logs(
    limit: int = Query(30, ge=1, le=300),
    status: str | None = Query(None, description="按状态过滤：ok / failed"),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(PushLog).order_by(PushLog.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(PushLog.status == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return ApiResponse.ok(
        [
            {
                "id": r.id,
                "notification_id": r.notification_id,
                "channel": r.channel,
                "status": r.status,
                "title": r.title,
                "detail": r.detail,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "created_at": to_local_iso(r.created_at),
            }
            for r in rows
        ]
    )
