"""AI 问答。

流程：关键词召回相关通知 → 拼上下文 → 让模型只依据上下文回答 → 返回引用的通知列表。
没配 Key 时降级为「直接给你相关通知的清单」，同样有用，不装样子。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import to_out
from app.core.config import get_settings
from app.core.timeutil import SHANGHAI
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
    model_name = ""
    model_source = "local"
    answer = _local_answer(payload.question, hits)

    # AIClient 不传 llm 时自动取「当前生效配置」：库里激活了自定义模型就是它，
    # 否则回落 .env。所以用户刚在界面上切完模型，下一次提问立刻生效。
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
                model_name = ai.llm.name
                model_source = ai.llm.source
        except Exception as e:  # noqa: BLE001
            logger.warning("问答 AI 调用失败，返回本地结果：%s", e)
            answer = _local_answer(payload.question, hits) + f"\n\n（AI 暂时不可用：{str(e)[:120]}）"
    else:
        # 没配 Key 才说"未配置"。
        # 以前这句是硬编码在 _local_answer 里的，于是限流降级时会同时出现
        # 「当前未接入大模型」和「AI 暂时不可用：HTTP 429」两句互相打脸的话。
        answer += "\n\n（未配置 API Key，以上是原始条目，未经模型提炼）"

    return ApiResponse.ok(
        ChatResponse(
            answer=answer,
            citations=citations,
            generated_by=generated_by,
            model_name=model_name,
            model_source=model_source,
        )
    )


def _local_answer(question: str, hits) -> str:
    """本地兜底：不编造，只列证据。"""
    if not hits:
        return "最近的通知里没有找到相关内容。可以换个说法，或者在信息流里用关键词搜一下。"

    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    lines = ["我按关键词在最近的通知里找到了这几条，你直接看原文更准："]
    for i, n in enumerate(hits[:6], start=1):
        info = n.key_info or {}
        suffix = []
        if info.get("deadline"):
            # 过期的截止日要标出来，否则这份"待办清单"会推荐一件已经错过的事
            deadline = str(info["deadline"])
            mark = "（已过期）" if deadline[:10] < today else ""
            suffix.append(f"截止 {deadline}{mark}")
        if info.get("materials"):
            suffix.append("需交 " + "、".join(info["materials"][:3]))
        tail = f"（{'；'.join(suffix)}）" if suffix else ""
        date = n.published_at.strftime("%m-%d") if n.published_at else "--"
        lines.append(f"{i}. [{date}] {n.title}{tail}")
    return "\n".join(lines)
