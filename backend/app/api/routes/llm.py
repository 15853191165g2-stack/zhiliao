"""自定义大模型配置接口。对应「AI 问答」页上的模型选择器。

为什么要有这组接口：.env 只能启动时读一次，用户想在界面上换个模型就得重启进程。
这里把配置挪进库，支持「内置默认 + 任意 OpenAI 兼容服务」多份配置随时切换。

三条设计约束（都是被真实场景逼出来的）：
  1. **永远不回明文 key**。列表/详情只给 `api_key_masked`；编辑时把打码值原样回传
     表示「没改」，后端保留原值。前端不需要把明文 key 再拉回来一次。
  2. **改完立刻生效**。每次写操作后调 `reload_active()` 刷新进程内缓存，
     否则用户切了模型、下一次提问还是跑旧的那家 —— 且界面上看不出来。
  3. **删除激活项要回落**。删掉正在用的 profile 必须回到 .env 默认，
     不能留下一个「active_id 指向不存在的行」的悬空状态。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db import get_session
from app.models import LLMProfile
from app.schemas import (
    ApiResponse,
    LLMEnvOut,
    LLMListOut,
    LLMModelsIn,
    LLMModelsOut,
    LLMProfileIn,
    LLMProfileOut,
    LLMProfileUpdate,
    LLMTestIn,
    LLMTestOut,
)
from app.services import llm_config
from app.services.ai_client import check_ai, list_remote_models

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/llm", tags=["llm"])


def _to_out(row: LLMProfile) -> LLMProfileOut:
    cfg = llm_config.config_from_profile(row)
    return LLMProfileOut(
        id=row.id,
        name=row.name,
        base_url=row.base_url or "",
        model=row.model or "",
        api_key_masked=cfg.masked_key,
        has_key=cfg.enabled,
        thinking=bool(row.thinking),
        json_mode=bool(row.json_mode),
        is_active=bool(row.is_active),
        endpoint=cfg.endpoint,
        last_test_ok=row.last_test_ok,
        last_test_at=row.last_test_at,
        last_test_error=row.last_test_error,
    )


async def _list_payload(session: AsyncSession) -> LLMListOut:
    rows = (
        await session.execute(select(LLMProfile).order_by(LLMProfile.id))
    ).scalars().all()
    settings = get_settings()
    env_cfg = llm_config.config_from_settings(settings)
    active = next((r for r in rows if r.is_active), None)
    effective = llm_config.get_active(settings)
    return LLMListOut(
        items=[_to_out(r) for r in rows],
        env=LLMEnvOut(
            name=llm_config.DEFAULT_PROFILE_NAME,
            model=env_cfg.model,
            base_url=env_cfg.base_url,
            endpoint=env_cfg.endpoint,
            api_key_masked=env_cfg.masked_key,
            enabled=env_cfg.enabled,
            is_active=active is None,
        ),
        active_id=active.id if active else None,
        effective=effective.label,
        effective_model=effective.model,
        effective_source=effective.source,
    )


@router.get("/profiles", response_model=ApiResponse[LLMListOut])
async def list_profiles(session: AsyncSession = Depends(get_session)):
    return ApiResponse.ok(await _list_payload(session))


@router.post("/profiles", response_model=ApiResponse[LLMProfileOut])
async def create_profile(
    payload: LLMProfileIn,
    activate: bool = Query(False, description="建完是否立即启用"),
    session: AsyncSession = Depends(get_session),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "配置名不能为空")
    if not payload.model.strip():
        raise HTTPException(400, "模型名不能为空（例如 deepseek-chat / gpt-4o-mini）")
    if not payload.base_url.strip():
        raise HTTPException(400, "接入地址不能为空")
    if not payload.api_key.strip():
        raise HTTPException(400, "API Key 不能为空")

    row = LLMProfile(
        name=name,
        base_url=payload.base_url.strip(),
        model=payload.model.strip(),
        api_key=payload.api_key.strip(),
        thinking=payload.thinking,
        json_mode=payload.json_mode,
        is_active=False,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, f"已经有一个叫「{name}」的配置了，换个名字")
    await session.refresh(row)

    if activate:
        await _set_active(session, row)
        await session.refresh(row)

    return ApiResponse.ok(_to_out(row), message="已保存")


@router.patch("/profiles/{profile_id}", response_model=ApiResponse[LLMProfileOut])
async def update_profile(
    profile_id: int,
    payload: LLMProfileUpdate,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(LLMProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"配置不存在：{profile_id}")

    data = payload.model_dump(exclude_unset=True)
    # key 传的是打码值 = 用户没动它，保留原 key 不动。
    # （前端编辑别的字段时，输入框里显示的就是打码值）
    if "api_key" in data:
        new_key = (data.pop("api_key") or "").strip()
        if new_key and not llm_config.is_masked(new_key):
            row.api_key = new_key

    for field in ("name", "base_url", "model", "thinking", "json_mode"):
        if field in data and data[field] is not None:
            value = data[field].strip() if isinstance(data[field], str) else data[field]
            setattr(row, field, value)

    if not (row.name or "").strip() or not (row.model or "").strip():
        raise HTTPException(400, "配置名和模型名都不能为空")

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "配置名重复，换一个")
    await session.refresh(row)

    # 改的是当前正在用的那份，立刻刷新内存里的生效配置；
    # 否则会出现「界面显示已改，实际提问还走旧参数」，且日志里看不出来。
    if row.is_active:
        await llm_config.reload_active()
    return ApiResponse.ok(_to_out(row), message="已更新")


@router.delete("/profiles/{profile_id}", response_model=ApiResponse[dict])
async def delete_profile(
    profile_id: int,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(LLMProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"配置不存在：{profile_id}")
    was_active = bool(row.is_active)
    await session.delete(row)
    await session.commit()

    # 删掉正在用的那份必须回落 .env，不能留悬空状态
    if was_active:
        await llm_config.reload_active()
    return ApiResponse.ok({"deleted": profile_id, "fell_back_to_env": was_active})


async def _set_active(session: AsyncSession, row: LLMProfile) -> None:
    """把 row 设为唯一激活项。先全置 False 再置 True，避免出现两个 active。"""
    await session.execute(update(LLMProfile).values(is_active=False))
    row.is_active = True
    await session.commit()
    await llm_config.reload_active()


@router.post("/profiles/{profile_id}/activate", response_model=ApiResponse[LLMListOut])
async def activate_profile(
    profile_id: int,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(LLMProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"配置不存在：{profile_id}")
    if not (row.api_key or "").strip():
        raise HTTPException(400, "这份配置没有 API Key，启用也用不了")
    await _set_active(session, row)
    return ApiResponse.ok(await _list_payload(session), message=f"已切换到「{row.name}」")


@router.post("/profiles/deactivate", response_model=ApiResponse[LLMListOut])
async def deactivate(session: AsyncSession = Depends(get_session)):
    """切回 .env 里的默认配置。"""
    await session.execute(update(LLMProfile).values(is_active=False))
    await session.commit()
    await llm_config.reload_active()
    return ApiResponse.ok(await _list_payload(session), message="已切回默认")


@router.post("/test", response_model=ApiResponse[LLMTestOut])
async def test_connection(
    payload: LLMTestIn,
    session: AsyncSession = Depends(get_session),
):
    """真发一次请求验通。**不保存也能测** —— 先试通再存，列表里不会堆一堆错的。

    编辑已有配置时前端传 `profile_id`，key 框里是打码值，这里自动取库里原文，
    所以「只改地址、key 没动」的情况下也能测。
    """
    base = payload.base_url.strip()
    key = payload.api_key.strip()
    model = payload.model.strip()
    thinking = payload.thinking
    json_mode = payload.json_mode

    if payload.profile_id:
        row = await session.get(LLMProfile, payload.profile_id)
        if row is not None:
            base = base or (row.base_url or "")
            model = model or (row.model or "")
            if not key or llm_config.is_masked(key):
                key = row.api_key or ""
            if thinking is None:
                thinking = bool(row.thinking)
            if json_mode is None:
                json_mode = bool(row.json_mode)

    if not key:
        return ApiResponse.ok(
            LLMTestOut(ok=False, detail="没有可用的 API Key", endpoint=llm_config.normalize_endpoint(base))
        )
    if not model:
        return ApiResponse.ok(
            LLMTestOut(ok=False, detail="模型名不能为空", endpoint=llm_config.normalize_endpoint(base))
        )

    cfg = llm_config.LLMConfig(
        name="测试",
        base_url=base,
        api_key=key,
        model=model,
        thinking=bool(thinking) if thinking is not None else False,
        json_mode=bool(json_mode) if json_mode is not None else True,
        source="profile",
        profile_id=payload.profile_id,
    )

    started = time.perf_counter()
    # 15 秒、不重试：这是「点一下告诉我通不通」，不是「帮我把请求送到」。
    # 默认配置（60s × 2 次）在校外打校内网关时会转 2 分钟，
    # 而前端 90 秒就断了 —— 用户等来的是「卡住」，不是「连不上」。
    result = await check_ai(get_settings(), cfg, timeout=15.0, retry=False)
    elapsed = (time.perf_counter() - started) * 1000

    # 顺手把测试结果写回该配置，界面能直接显示上次是否通过
    if payload.profile_id:
        row = await session.get(LLMProfile, payload.profile_id)
        if row is not None:
            from app.core.timeutil import now_utc

            row.last_test_ok = bool(result.get("ok"))
            row.last_test_at = now_utc()
            row.last_test_error = None if result.get("ok") else str(result.get("detail") or "")[:300]
            await session.commit()

    return ApiResponse.ok(
        LLMTestOut(
            ok=bool(result.get("ok")),
            detail=str(result.get("detail") or ("连接成功" if result.get("ok") else "")),
            endpoint=str(result.get("endpoint") or cfg.endpoint),
            model=cfg.model,
            reply=str(result.get("reply") or ""),
            duration_ms=round(elapsed, 1),
        )
    )


@router.post("/models", response_model=ApiResponse[LLMModelsOut])
async def list_models(
    payload: LLMModelsIn,
    session: AsyncSession = Depends(get_session),
):
    """问服务商有哪些模型，给「模型名」那一栏做个下拉。

    学校网关一次列 25 个模型，id 长这样：`GLM-4.6-W8A8` / `DeepSeek-V3.1-W8A8`。
    让人手打就是让人猜，猜错了只会得到 404 model not found —— 界面上分不清
    是「名字错」还是「地址错」。

    拿不到列表**不算失败**：返回 ok=False + 原因，用户仍然可以自己填模型名保存。
    """
    base = payload.base_url.strip()
    key = payload.api_key.strip()

    if payload.profile_id:
        row = await session.get(LLMProfile, payload.profile_id)
        if row is not None:
            base = base or (row.base_url or "")
            if not key or llm_config.is_masked(key):
                key = row.api_key or ""

    endpoint = llm_config.normalize_endpoint(base)
    if not key:
        return ApiResponse.ok(LLMModelsOut(ok=False, endpoint=endpoint, detail="没有可用的 API Key"))

    res = await list_remote_models(endpoint, key)
    return ApiResponse.ok(
        LLMModelsOut(
            ok=bool(res.get("ok")),
            items=list(res.get("items") or []),
            endpoint=str(res.get("endpoint") or endpoint),
            detail=str(res.get("detail") or ""),
        )
    )
