"""AI 问答。

流程：关键词召回相关通知 → 拼上下文 → 让模型只依据上下文回答 → 返回引用的通知列表。
没配 Key 时降级为「直接给你相关通知的清单」，同样有用，不装样子。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import to_out
from app.core.config import get_settings
from app.db import get_session
from app.schemas import ApiResponse, ChatRequest, ChatResponse
from app.services.ai_client import AIClient
from app.services.prompts import get_prompt_library
from app.services.search import build_context, search_notifications, source_name_map

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ApiResponse[ChatResponse])
async def chat(
    payload: ChatRequest,
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    hits = await search_notifications(
        session, payload.question, days=payload.days, top_k=payload.top_k
    )
    names = await source_name_map(session)
    context = build_context(hits, names)
    citations = [to_out(n) for n in hits]

    generated_by = "local"
    answer = _local_answer(payload.question, hits)

    ai = AIClient(settings)
    if ai.enabled:
        try:
            rendered = get_prompt_library().render(
                "qa", "v1", context=context, question=payload.question
            )
            system, user = get_prompt_library().split_system_user(rendered)
            text = await ai.chat(system, user, max_tokens=900)
            if text.strip():
                answer = text.strip()
                generated_by = ai.model
        except Exception as e:  # noqa: BLE001
            logger.warning("问答 AI 调用失败，返回本地结果：%s", e)
            answer = _local_answer(payload.question, hits) + f"\n\n（AI 暂时不可用：{str(e)[:120]}）"

    return ApiResponse.ok(
        ChatResponse(answer=answer, citations=citations, generated_by=generated_by)
    )


def _local_answer(question: str, hits) -> str:
    """本地兜底：不编造，只列证据。"""
    if not hits:
        return "最近的通知里没有找到相关内容。可以换个说法，或者在信息流里用关键词搜一下。"

    lines = ["我按关键词在最近的通知里找到了这几条，你直接看原文更准："]
    for i, n in enumerate(hits[:6], start=1):
        info = n.key_info or {}
        suffix = []
        if info.get("deadline"):
            suffix.append(f"截止 {info['deadline']}")
        if info.get("materials"):
            suffix.append("需交 " + "、".join(info["materials"][:3]))
        tail = f"（{'；'.join(suffix)}）" if suffix else ""
        date = n.published_at.strftime("%m-%d") if n.published_at else "--"
        lines.append(f"{i}. [{date}] {n.title}{tail}")
    lines.append("\n（当前未接入大模型，以上是原始条目；配置 AI_API_KEY 后我会直接给你答案。）")
    return "\n".join(lines)
