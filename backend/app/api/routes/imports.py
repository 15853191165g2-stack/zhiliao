"""导入入口：公众号文章链接 / i大工通知文本。

这条链路和「定时抓取」是并列的两条路：
定时抓取是后端主动去拉列表（`/api/ingest/run`），
这里是客户端把内容推上来（微信没有列表接口，学校 APP 的通知只能从系统通知栏拿）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from app.db import get_session
from app.schemas import ApiResponse
from app.services.imports import import_article_url, import_texts

router = APIRouter(prefix="/import", tags=["import"])


class ArticleImportRequest(BaseModel):
    url: str = Field(..., description="公众号文章链接（微信里「··· → 复制链接」得到）")
    source_name: str = Field("微信公众号", description="归档到哪个来源下")
    summarize: bool = Field(True, description="是否立即跑 AI 摘要")
    ignore_robots: bool = Field(
        True,
        description=(
            "是否按「单篇手动导入」放行 robots.txt。默认 true，"
            "因为 mp.weixin.qq.com 的 robots.txt 是 Disallow: /，"
            "严格模式下列表里的公众号文章只能拿到标题无法拿到正文。"
            "想严格合规请传 false。"
        ),
    )


class NotifyEntry(BaseModel):
    title: str
    text: str | None = None
    url: str | None = None
    app: str | None = Field(None, description="通知来自哪个 App，如 i大工")
    published_at: str | None = Field(None, description="ISO 时间，留空取当前时间")


class NotifyImportRequest(BaseModel):
    entries: list[NotifyEntry]
    source_name: str = Field("i大工", description="归档到哪个来源下")
    summarize: bool = True


@router.post("/article", response_model=ApiResponse[dict])
async def import_article(
    payload: ArticleImportRequest,
    session: AsyncSession = Depends(get_session),
):
    """导入一篇公众号 / 任意文章链接。

    实测：mp.weixin.qq.com 的文章页用手机端 UA 可以直接抓到正文。
    **但微信不提供「列出某公众号全部文章」的接口**，所以只能一篇篇导，
    或者用第三方 RSS 桥接（见 sources/dlut_wechat.yaml）。
    """
    data = await import_article_url(
        session,
        payload.url,
        source_name=payload.source_name,
        summarize=payload.summarize,
        ignore_robots=payload.ignore_robots,
    )
    return ApiResponse.ok(data)


@router.post("/notify", response_model=ApiResponse[dict])
async def import_notifications(
    payload: NotifyImportRequest,
    session: AsyncSession = Depends(get_session),
):
    """批量导入客户端推上来的通知文本。

    Android：NotificationListenerService 捕获通知栏内容后 POST 上来
             （需要在系统设置里授予「通知使用权」）。
    iOS：系统沙盒不允许读取其他 App 的通知，改用「分享扩展」把标题+链接送上来，
         或截图后本地 OCR 成文本再送上。
    """
    data = await import_texts(
        session,
        [e.model_dump() for e in payload.entries],
        source_name=payload.source_name,
        summarize=payload.summarize,
    )
    return ApiResponse.ok(data)


@router.get("/capability", response_model=ApiResponse[dict])
async def import_capability():
    """把「哪些能自动接、哪些只能手动导」写清楚，直接给 App 端做引导文案用。"""
    return ApiResponse.ok(
        {
            "article_url": {
                "supported": True,
                "how": "微信内「··· → 复制链接」，粘贴到这里",
                "limitation": "微信无公开接口，无法按公众号批量拉取历史文章",
            },
            "wechat_official_account": {
                "supported": False,
                "reason": "微信未开放公众号文章列表接口",
                "alternatives": [
                    "单篇链接导入（/api/import/article）",
                    "第三方 RSS 桥接，在 sources/dlut_wechat.yaml 里填桥接地址",
                    "注意：第三方桥接服务本身可能随时失效或被封，属灰色地带",
                ],
            },
            "miniprogram": {
                "supported": False,
                "reason": "小程序页面在客户端内渲染，无对外可访问的数据源",
                "alternatives": ["分享卡片落地页链接导入", "截图 OCR 后走 /api/import/notify"],
            },
            "android_notification": {
                "supported": True,
                "how": "NotificationListenerService 读取通知栏 → POST /api/import/notify",
                "requirement": "用户需在系统设置中授予「通知使用权」",
            },
            "ios_notification": {
                "supported": False,
                "reason": "iOS 沙盒不允许第三方 App 读取其他 App 的通知（系统级限制）",
                "alternatives": ["分享扩展导入链接", "截图 → 本地 OCR → /api/import/notify"],
            },
        }
    )
