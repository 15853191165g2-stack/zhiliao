"""「正文抓不到时不许让 AI 编」的守卫。

## 背景（2026-10-18 实测）

大工校级通知里有很多是公众号链接（`mp.weixin.qq.com`），robots 是 `Disallow: /`，
详情一律跳过 —— 于是 `content_text` 为空。

原来的实现不管正文空不空都调 GLM。模型手上只有一行标题，就把
「关于2026年中秋节、国庆节放假安排的通知」编成了：

    1. 大连理工大学发布2026年中秋国庆放假安排
    2. 9月25日（周五）至10月7日（周三）放假调休
    3. 10月8日（周四）正常上课，请提前安排行程

**这三行没有一句来自原文**，但看起来完全像真的。用户照着订票就出事了。
这比「摘要为空」危险得多 —— 空摘要至少不会骗人。

修法：正文短于 `_MIN_AI_CONTENT_CHARS` 就不进 AI 分支，直接用本地规则
（第 2、3 行是「原文信息较少，建议点开看全文」），并把 `model` 标成
`local-no-content`，让调用方/统计能看出这批是「没正文的」。
"""

from __future__ import annotations

import pytest

from app.services.summarize import _FALLBACK_LINE, SummarizeService


@pytest.fixture()
def svc() -> SummarizeService:
    return SummarizeService()


async def test_empty_content_skips_ai(svc: SummarizeService) -> None:
    res = await svc.summarize(
        title="关于2026年中秋节、国庆节放假安排的通知",
        content=None,
        source_name="大连理工大学·通知公告",
    )
    assert res.model == "local-no-content", "正文为空时必须走本地规则，不能调 AI"

    lines = res.summary.summary_lines
    assert len(lines) == 3, "前端卡片按 3 行排版，缺一行会错位"
    # 第 2、3 行只能是诚实兜底 —— 不允许凭空补出日期/安排
    assert lines[1] == _FALLBACK_LINE
    assert lines[2] == _FALLBACK_LINE

    # 标题里没有具体日期，摘要里也不许冒出来（这些正是当初编造的内容）
    blob = "".join(lines)
    for invented in ("9月25日", "10月7日", "10月8日", "调休", "正常上课"):
        assert invented not in blob, f"摘要里不该出现编造的「{invented}」"


async def test_whitespace_content_skips_ai(svc: SummarizeService) -> None:
    """详情页抓回来一堆空白字符时同样要拦住 —— 别让 AI 拿空白当正文。"""
    res = await svc.summarize(title="2026级新生报到须知", content="   \n\t  \n ")
    assert res.model == "local-no-content"


async def test_inaccessible_page_content_skips_ai(svc: SummarizeService) -> None:
    """博达「您无权访问此页面」那种情况：正文只剩一句系统提示，也不该调 AI。"""
    res = await svc.summarize(
        title="（项目通知）第八届“浪漫之旅”海峡两岸文化研习营（赴台营）招募营员通知",
        # ★ 这段必须是**库里真实的样子**（多行）。早先写成单行「系统提示 抱歉 您无权访问此页面」，
        #   切句后只剩一句，第二行走的是 _FALLBACK_LINE —— 断言无论如何都通过，
        #   属于「假绿」。多行版本才会让「您无权访问此页面」落到 second sentence 上。
        content="系统提示\n抱歉\n可能是由下列问题导致的：\n您无权访问此页面",
    )
    assert res.model == "local-no-content"

    lines = res.summary.summary_lines
    assert len(lines) == 3, "前端卡片按 3 行排版"

    # 2026-10-19 补：只断言「不调 AI」还不够。
    # 本地规则会把「您无权访问此页面」原样当成第二行摘要展示 ——
    # 用户点开看到的是系统提示，不是通知内容。实测库里 14 条港澳台项目通知都这样。
    blob = "".join(lines)
    for noise in ("无权访问", "系统提示", "抱歉"):
        assert noise not in blob, f"摘要里不该出现提示页文字「{noise}」"
    assert "需登录" in res.summary.tags, "标出来源才方便用户筛掉这批"


def test_error_page_detector() -> None:
    """提示页识别：短提示命中，正常正文不误杀。"""
    from app.services.summarize import _looks_like_error_page

    assert _looks_like_error_page("系统提示 抱歉 可能是由下列问题导致的：您无权访问此页面") is True
    assert _looks_like_error_page("404 Not Found") is True

    # 「请先登录」不在强特征里 —— 通知里教用户登录太常见，不能因此废掉整篇。
    assert _looks_like_error_page("请先登录校园门户后下载附件。") is False
    # 长正文里顺带出现提示词也不算（一篇两千字的通知不可能整篇是提示语）。
    long_text = "报名前请先登录校园门户。" + "本次项目面向全校本科生开放。" * 40
    assert _looks_like_error_page(long_text) is False
