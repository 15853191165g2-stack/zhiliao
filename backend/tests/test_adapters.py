"""适配器与服务的离线测试。

全部用本地样例文件，不联网、不需要数据库。
    pytest -q
"""

from __future__ import annotations

import pytest

from app.adapters.base import AdapterError, FetchContext, SourceSpec
from app.adapters.html_list import HtmlListAdapter
from app.adapters.rss import RssAdapter
from app.core.config import get_settings
from app.services.dedup import canonical_url, make_dedup_key, title_similarity
from app.services.summarize import SummarizeService


class _NoopFetcher:
    """离线测试绝不允许发网络请求，一旦发出就直接失败。"""

    async def get_text(self, url: str, encoding: str | None = None) -> str:
        raise AssertionError(f"离线测试不应该请求网络：{url}")

    async def get_bytes(self, url: str) -> bytes:
        raise AssertionError(f"离线测试不应该请求网络：{url}")


def _ctx() -> FetchContext:
    return FetchContext(fetcher=_NoopFetcher(), settings=get_settings())  # type: ignore[arg-type]


JWC_SPEC = SourceSpec.from_dict(
    {
        "name": "示例大学教务处通知",
        "school": "示例大学",
        "adapter": "html_list",
        "url": "tests/fixtures/demo_jwc.html",
        "category_hint": "教务",
        "config": {
            "transport": "file",
            "file": "tests/fixtures/demo_jwc.html",
            "url_base": "tests/fixtures/",
            "list_selector": "ul.news-list li",
            "fields": {"title": "a", "url": "a@href", "date": "span.date"},
            "date_format": "%Y-%m-%d",
            "follow_detail": True,
            "detail": {"content_selector": "div.v_news_content"},
            "max_items": 30,
        },
    }
)


# --------------------------------------------------------------------------- #
# html_list 适配器
# --------------------------------------------------------------------------- #
async def test_html_list_parses_all_items():
    items = await HtmlListAdapter().fetch(JWC_SPEC, _ctx())
    assert len(items) == 4, f"应解析出 4 条，实际 {len(items)}"


async def test_html_list_url_and_date():
    items = await HtmlListAdapter().fetch(JWC_SPEC, _ctx())
    first = next(i for i in items if "国家奖学金" in i.title)
    assert first.url == "tests/fixtures/demo_jwc_detail_1.html"
    # 2026-10-05 北京时间 -> 2026-10-04 16:00 UTC（库里统一存 naive UTC）
    assert first.published_at.strftime("%Y-%m-%d %H:%M") == "2026-10-04 16:00"


async def test_html_list_follows_detail():
    items = await HtmlListAdapter().fetch(JWC_SPEC, _ctx())
    first = next(i for i in items if "国家奖学金" in i.title)
    assert first.content_text and "国家奖学金" in first.content_text
    assert "逸夫楼305" in first.content_text


async def test_html_list_item_without_url_is_kept():
    """href="#" 的条目拿不到有效链接，但仍应入库（靠标题+日期去重）。"""
    items = await HtmlListAdapter().fetch(JWC_SPEC, _ctx())
    no_url = [i for i in items if i.url is None]
    assert len(no_url) == 1
    assert "校园安全工作" in no_url[0].title


async def test_html_list_bad_selector_raises():
    bad = SourceSpec.from_dict(
        {
            "name": "坏的配置",
            "adapter": "html_list",
            "url": "tests/fixtures/demo_jwc.html",
            "config": {
                "transport": "file",
                "list_selector": "ul.not-exist li",
                "fields": {"title": "a"},
            },
        }
    )
    with pytest.raises(AdapterError):
        await HtmlListAdapter().fetch(bad, _ctx())


async def test_html_list_missing_selector_raises():
    bad = SourceSpec.from_dict(
        {
            "name": "缺配置",
            "adapter": "html_list",
            "url": "tests/fixtures/demo_jwc.html",
            "config": {"transport": "file", "fields": {"title": "a"}},
        }
    )
    with pytest.raises(AdapterError):
        await HtmlListAdapter().fetch(bad, _ctx())


# --------------------------------------------------------------------------- #
# rss 适配器
# --------------------------------------------------------------------------- #
async def test_rss_parses_items():
    spec = SourceSpec.from_dict(
        {
            "name": "示例大学图书馆通知",
            "adapter": "rss",
            "url": "tests/fixtures/demo_lib.xml",
            "category_hint": "后勤",
            "config": {"transport": "file", "file": "tests/fixtures/demo_lib.xml"},
        }
    )
    items = await RssAdapter().fetch(spec, _ctx())
    assert len(items) == 2
    assert items[0].published_at is not None
    assert "图书馆" in items[0].title
    assert items[0].content_text and "闭馆" in items[0].content_text


# --------------------------------------------------------------------------- #
# GLM 客户端：推理模型的截断诊断
# --------------------------------------------------------------------------- #
def test_pick_content_reports_truncated_reasoning_model():
    """GLM-4.7-Flash 是推理模型，思维链计入 max_tokens。

    实测（2026-10）：max_tokens=120 且开启 thinking 时，
    返回 `finish_reason="length"`、`content=""`、`reasoning_content` 有 256 字。
    旧代码只会报含糊的「无法从响应中取出文本」，很难定位。
    现在必须能明确指出是「思维链吃光了额度」。
    """
    from app.services.ai_client import AIClient, AIError

    data = {
        "choices": [
            {
                "finish_reason": "length",
                "index": 0,
                "message": {"content": "", "reasoning_content": "1. 分析请求：……" * 20},
            }
        ]
    }
    try:
        AIClient._pick_content(data)
    except AIError as e:
        msg = str(e)
        assert "截断" in msg or "max_tokens" in msg
        assert "thinking" in msg  # 要给出可操作的修复方向
    else:
        raise AssertionError("截断场景必须抛 AIError")


def test_pick_content_returns_normal_content():
    from app.services.ai_client import AIClient

    data = {"choices": [{"finish_reason": "stop", "message": {"content": '{"a":1}'}}]}
    assert AIClient._pick_content(data) == '{"a":1}'


def test_rate_limit_detection():
    from app.services.summarize import _is_rate_limited

    assert _is_rate_limited(Exception('HTTP 429: {"code":"1305","message":"该模型当前访问量过大"}'))
    assert not _is_rate_limited(Exception("模型未返回合法 JSON：{abc"))
    assert not _is_rate_limited(Exception("Connection timeout"))


def test_ai_client_disables_thinking_by_default(tmp_path):
    """构造 payload 时必须带上 thinking=disabled，否则思维链会吃掉额度。"""
    from app.core.config import Settings

    s = Settings(ai_api_key="fake-key-for-payload-test")
    assert s.ai_thinking is False


# --------------------------------------------------------------------------- #
# 适配器注册表
# --------------------------------------------------------------------------- #def test_registry_lists_builtin_adapters():
    from app.adapters.registry import available_kinds, get_adapter

    kinds = available_kinds()
    assert "html_list" in kinds and "rss" in kinds
    assert get_adapter("html_list").kind == "html_list"

    with pytest.raises(AdapterError):
        get_adapter("not_exists")


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
def test_canonical_url_strips_tracking():
    a = canonical_url("https://jwc.example.edu.cn/tzgg/1.htm?utm_source=wechat&id=7#top")
    b = canonical_url("https://jwc.example.edu.cn/tzgg/1.htm?id=7")
    assert a == b


def test_canonical_url_keeps_wechat_params():
    """微信文章的 __biz/mid/idx/sn 去掉就会 404，必须保留。"""
    u = "https://mp.weixin.qq.com/s?__biz=MzA1&mid=123&idx=1&sn=abc&from=timeline"
    c = canonical_url(u)
    assert "__biz=MzA1" in c and "mid=123" in c and "from=" not in c


def test_dedup_key_same_for_tracking_variants():
    k1 = make_dedup_key("https://x.edu.cn/a.htm?utm_source=wx", "标题")
    k2 = make_dedup_key("https://x.edu.cn/a.htm", "标题")
    assert k1 == k2


def test_dedup_key_falls_back_to_title():
    assert len(make_dedup_key(None, "关于国庆放假的通知")) == 40


def test_title_similarity():
    assert title_similarity("国家奖学金评审通知", "国家奖学金评审工作的通知") >= 0.5
    assert title_similarity("选课安排", "图书馆开放时间") < 0.2


# --------------------------------------------------------------------------- #
# 本地摘要兜底
# --------------------------------------------------------------------------- #
_SCHOLARSHIP_TEXT = (
    "请符合条件的同学于2026年10月15日17:00前将申请材料提交至所在学院"
    "学生工作办公室（逸夫楼305）。需提交的材料包括：国家奖学金申请表、"
    "上一学年成绩单原件、获奖证明复印件。联系人：李老师，联系电话：010-62781234。"
    "逾期不再受理。"
)


async def test_local_summarizer_extracts_key_info():
    svc = SummarizeService()
    svc.settings.ai_api_key = ""  # 强制走本地规则
    svc.ai.settings.ai_api_key = ""

    res = await svc.summarize(
        title="关于开展国家奖学金评审工作的通知",
        content=_SCHOLARSHIP_TEXT,
        source_name="教务处",
        category_hint="教务",
    )
    s = res.summary
    assert res.model == "local"
    assert len(s.summary_lines) == 3
    assert s.key_info.deadline and "10月15日" in s.key_info.deadline
    assert s.key_info.deadline_ts is not None, "截止时间要能解析成真实时间，否则加不了日历"
    assert s.key_info.location and "逸夫楼305" in s.key_info.location
    assert any("申请表" in m for m in s.key_info.materials)
    assert s.key_info.contact and "李老师" in s.key_info.contact
    assert s.importance == "urgent"      # 命中「逾期不」
    assert s.category == "奖助学金"


async def test_local_summarizer_always_three_lines():
    """正文为空时也必须给满 3 行——前端卡片按 3 行排版。"""
    svc = SummarizeService()
    svc.settings.ai_api_key = ""
    svc.ai.settings.ai_api_key = ""

    res = await svc.summarize(title="关于举办学术讲座的通知", content="")
    assert len(res.summary.summary_lines) == 3
    assert res.summary.key_info.deadline is None
    assert res.summary.category == "讲座"


async def test_local_summarizer_deadline_patterns():
    svc = SummarizeService()
    svc.settings.ai_api_key = ""
    svc.ai.settings.ai_api_key = ""

    cases = [
        ("报名截止时间为10月20日12:00", "10月20日"),
        ("请于2026年11月1日前提交材料", "2026年11月1日"),
        ("材料提交截止：2026-12-31", "2026-12-31"),
    ]
    for text, expected in cases:
        res = await svc.summarize(title="测试通知标题够长", content=text)
        got = res.summary.key_info.deadline or ""
        assert expected in got, f"{text!r} 应解析出 {expected}，实际 {got!r}"


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def test_extract_keywords():
    from app.services.search import extract_keywords

    kws = extract_keywords("这周有哪些截止的事项？奖学金要交什么材料")
    assert "奖学金" in kws
    assert any("截止" in k for k in kws)
    assert "这周" not in kws


def test_guess_days():
    from app.services.search import guess_days

    assert guess_days("这周有什么通知") == 7
    assert guess_days("随便问问") == 30


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def test_parse_dt_formats():
    from app.core.timeutil import parse_dt, to_local_iso

    for raw in ("2026-10-15", "2026年10月15日", "2026/10/15", "10月15日 17:00"):
        dt = parse_dt(raw)
        assert dt is not None, f"{raw} 应能解析"
    dt = parse_dt("2026-10-15 17:00")
    assert to_local_iso(dt).startswith("2026-10-15T17:00")


def test_parse_dt_returns_none_for_garbage():
    from app.core.timeutil import parse_dt

    assert parse_dt("另行通知") is None
    assert parse_dt("") is None


def test_parse_dt_yearless_month_day_does_not_crash():
    """无年份的「9月1日」走 md 分支，**不能**因为 tz-aware/naive 相减而崩。

    踩过的坑：`today = datetime.now(SHANGHAI)` 是 aware，而构造出的 dt 是 naive，
    `(dt - today).days` 抛 TypeError。迎新网《新生家长指南》正文里有「9月1日」，
    整条摘要直接失败。
    """
    from app.core.timeutil import parse_dt

    dt = parse_dt("9月1日")
    assert dt is not None
    dt2 = parse_dt("12月25日")
    assert dt2 is not None  # 跨年修正分支也要能跑

