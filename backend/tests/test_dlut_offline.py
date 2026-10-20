"""大连理工各来源的选择器回归测试（离线回放）。

跑法：`pytest tests/test_dlut_offline.py -v`

这些断言的**数字全部来自真实页面实测**，不是拍的。站点改版时这些测试会红，
这正是它们存在的意义：把"哪一天悄悄抓空了"变成一次明确的失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.base import AdapterError, SourceSpec
from app.adapters.registry import adapter_for, load_builtin
from app.core.timeutil import to_local_dt
from app.services.dedup import is_shell_content, structural_tags
from app.services.source_config import load_yaml_sources
from tests.offline import URL_MAP, make_ctx, synth_detail


@pytest.fixture(scope="module", autouse=True)
def _load():
    load_builtin()


def spec_for(name: str) -> SourceSpec:
    """从真实的 sources/*.yaml 里取配置，保证测的就是线上那份配置。"""
    for entry in load_yaml_sources():
        if entry.get("name") == name:
            return SourceSpec.from_dict({k: v for k, v in entry.items() if not k.startswith("_")})
    raise AssertionError(f"sources/*.yaml 里找不到来源：{name}")


async def run_source(name: str, **kw):
    spec = spec_for(name)
    ctx, fetcher = make_ctx(**kw)
    items = await adapter_for(spec).fetch(spec, ctx)
    return items, fetcher


# --------------------------------------------------------------------------- #
# 1. 迎新网 4 个栏目：条目数、日期、链接
# --------------------------------------------------------------------------- #
async def test_yx_bksbd_all_fields_present():
    items, _ = await run_source(
        "迎新网·本科生报到", synthesize_missing=True
    )
    assert len(items) == 11, "实测 11 条，少了就是选择器漏了"
    assert all(i.url and "yx.dlut.edu.cn/info/" in i.url for i in items)
    assert all(i.published_at is not None for i in items), "日期在兄弟 td 里，必须全部取到"
    # 日期解析成真实时间，不是 None 也不是写死的
    first = items[0]
    assert first.title == "2026级新生医疗保险投保指南"
    assert to_local_dt(first.published_at).strftime("%Y-%m-%d") == "2026-08-10"


async def test_yx_yxfc_has_20_items():
    items, _ = await run_source("迎新网·院系风采", synthesize_missing=True)
    assert len(items) == 20, "该栏目正好铺满 20 条，是「最近 20 条」的验收样本"
    assert all(i.published_at is not None for i in items)


async def test_yx_home_aggregates_20_items():
    items, _ = await run_source("迎新网·首页聚合", synthesize_missing=True)
    assert len(items) == 20
    # 首页用 <li><a><span>标题</span><span>日期</span></a></li>，标题不能带上日期
    assert all("2026-" not in i.title for i in items), "标题里混进日期说明取了整行文本"
    assert all(i.published_at is not None for i in items)


# --------------------------------------------------------------------------- #
# 2. 后勤处：日期被拆成「日」+「年-月」，且分页是反向编号
# --------------------------------------------------------------------------- #
async def test_houqin_split_date_parsed():
    items, fetcher = await run_source("后勤处·通知公告", synthesize_missing=True)
    # 第 1 页 15 行 + 第 2 页 5 行 = 20 行（max_pages: 2）
    assert len(items) == 20, f"两页合计应 20 条，实际 {len(items)} 条"
    assert all(i.published_at is not None for i in items), \
        "日期是 p.day(21) + p.year-month(2026-09) 拼出来的，不能为空"
    first = items[0]
    assert first.title == "关于2026年下半年后勤处领导接待日安排的通知"
    assert to_local_dt(first.published_at).strftime("%Y-%m-%d") == "2026-09-21", \
        "21 + 2026-09 必须解析成 2026-09-21，而不是 2026-09-01"


async def test_houqin_reverse_pagination_order_and_stop():
    """博达分页的反向编号：第2页是 {栏目}/4.htm，而页码控件里的 5.htm 是坏链。"""
    items, fetcher = await run_source("后勤处·通知公告", synthesize_missing=True)
    assert "http://houqin.dlut.edu.cn/tzgg.htm" in fetcher.hits
    assert "http://houqin.dlut.edu.cn/tzgg/4.htm" in fetcher.hits, "应翻到第 2 页"
    assert "http://houqin.dlut.edu.cn/tzgg/5.htm" not in fetcher.hits, \
        "5.htm 是空页，绝不能信页码控件的 href"


async def test_houqin_wechat_links_kept():
    """后勤处一半内容直接挂公众号链接，不能因为白名单把它过滤掉。"""
    items, _ = await run_source("后勤处·通知公告", synthesize_missing=True)
    wechat = [i for i in items if i.url and "mp.weixin.qq.com" in i.url]
    assert len(wechat) >= 10, f"实测 12 条公众号直链，只拿到 {len(wechat)} 条"


# --------------------------------------------------------------------------- #
# 3. 学生处：标题带「·」前缀、日期是「年月日」、正文是纯图片
# --------------------------------------------------------------------------- #
async def test_xsc_title_cleanup_and_chinese_date():
    items, _ = await run_source("学生处·通知公告", synthesize_missing=True)
    assert len(items) == 14
    assert all(not i.title.startswith("·") for i in items), "标题的「·」前缀必须清掉"
    assert all(i.published_at is not None for i in items), "「2026年07月20日」要能解析"
    first = items[0]
    assert first.title == "关于党委学生工作部2026年暑假值班安排的通知"
    assert to_local_dt(first.published_at).strftime("%Y-%m-%d") == "2026-07-20"


async def test_xsc_image_page_content_preserved_not_discarded():
    """学生处值班通知：一句话 + 一张大图（图片里才是真正的值班表）。

    重点不是"判定成什么标签"，而是**内容不能被丢掉**：
    正文里必须有 <img>，且不能被空壳保护逻辑当成空壳。
    """
    items, _ = await run_source("学生处·通知公告", synthesize_missing=False)
    target = next(
        i for i in items if i.url == "http://xsc.dlut.edu.cn/info/1042/7112.htm"
    )
    assert target.extra.get("detail_failed") is None, "这不是抓取失败"
    assert "<img" in (target.content_html or ""), "图片必须留在 content_html 里"
    assert target.content_text, "有一句引导文字也要留下"
    assert is_shell_content(target.content_html, target.content_text) is False


async def test_yx_image_only_page_flagged_as_image_notice():
    """纯图片页：正文一个字都没有，只有 2 张 <img>（迎新网院系风采）。

    这种页面的文本长度天然为 0，是**合法内容**而不是抓取失败 ——
    早期版本的"文本长度门槛"会把这类页面判成失败，正是要防的坑。
    """
    items, _ = await run_source("迎新网·院系风采", synthesize_missing=True)
    target = next(i for i in items if i.url == "https://yx.dlut.edu.cn/info/10092/90837.htm")
    assert target.extra.get("detail_failed") is None
    assert not (target.content_text or "").strip()
    assert target.content_html and target.content_html.count("<img") >= 1
    assert is_shell_content(target.content_html, target.content_text) is False
    assert structural_tags(target.content_html, target.content_text) == ["图片通知"]


@pytest.mark.parametrize(
    ("html", "text", "expected"),
    [
        ("", "", ["正文缺失"]),
        ("<div><p></p></div>", "", ["正文为空"]),
        ('<div><img src="a.jpg"></div>', "", ["图片通知"]),
        # 一句话 + 一张图（学生处值班通知就是这种）：实质内容在图片里，
        # 所以仍然标「图片通知」，提醒用户别只看这句摘要
        ('<div><img src="a.jpg"></div>', "现将值班安排通知如下，详见下图。", ["图片通知"]),
        ("<p>" + "正常正文" * 20 + "</p>", "正常正文" * 20, []),
    ],
)
def test_structural_tags_decides_by_media_not_by_length_alone(
    html: str, text: str, expected: list[str]
):
    """长度门槛必须与"有没有图片"配合，否则纯图片页会被误判。"""
    assert structural_tags(html, text) == expected


# --------------------------------------------------------------------------- #
# 4. 源站本身就是空正文的两条（研究生院 / 后勤处）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("研究生院·工作动态", "http://gs.dlut.edu.cn/info/1005/50392.htm"),
        ("后勤处·通知公告", "http://houqin.dlut.edu.cn/info/1031/8891.htm"),
    ],
)
async def test_source_side_empty_body_flagged(name: str, url: str):
    items, _ = await run_source(name, synthesize_missing=False)
    target = next((i for i in items if i.url == url), None)
    if target is None:
        pytest.skip("该条已不在列表首页")
    assert structural_tags(target.content_html, target.content_text) == ["正文为空"]
    assert is_shell_content(target.content_html, target.content_text) is True


# --------------------------------------------------------------------------- #
# 5. 网信中心：必须避开 ul.dropdown-menu 里的导航项
# --------------------------------------------------------------------------- #
async def test_its_avoids_nav_dropdown():
    items, _ = await run_source("网信中心·中心通知", synthesize_missing=True)
    titles = {i.title for i in items}
    assert "核心机房管理办法" not in titles, "这是导航菜单里的条目，不该当通知抓"
    assert "宿舍网络设备间管理" not in titles
    assert len(items) == 19, "div.list 下实测 19 条"
    assert all(i.published_at is not None for i in items)


# --------------------------------------------------------------------------- #
# 6. 图书馆：日期被拆成 <time><span>2026</span>09-22</time>，标题在 h4
# --------------------------------------------------------------------------- #
async def test_lib_split_date_and_h4_title():
    items, _ = await run_source("图书馆·通知公告", synthesize_missing=True)
    assert len(items) == 12
    assert all(i.published_at is not None for i in items)
    assert all("了解详情" not in i.title for i in items), "标题要取 h4，不能取整行"
    first = items[0]
    assert to_local_dt(first.published_at).strftime("%Y-%m-%d") == "2026-09-22"


# --------------------------------------------------------------------------- #
# 7. 教务处 / 研究生院
# --------------------------------------------------------------------------- #
async def test_teach_news_list():
    items, _ = await run_source("教务处·新闻快递", synthesize_missing=True)
    assert len(items) == 20
    assert all(i.published_at is not None for i in items)
    assert all(i.url and "teach.dlut.edu.cn/info/" in i.url for i in items)


async def test_gs_bracket_date_and_mixed_hosts():
    items, _ = await run_source("研究生院·工作动态", synthesize_missing=True)
    assert len(items) == 18
    assert all(i.published_at is not None for i in items), "「[2026-04-29]」要能解析"
    hosts = {i.url.split("/")[2] for i in items if i.url}
    assert "news.dlut.edu.cn" in hosts, "外部新闻网的条目也要收"
    assert "gs.dlut.edu.cn" in hosts


# --------------------------------------------------------------------------- #
# 8. 详情页抓不到时，列表已知字段必须保留（不能整条丢）
# --------------------------------------------------------------------------- #
async def test_detail_failure_keeps_list_fields():
    items, _ = await run_source("迎新网·本科生报到", synthesize_missing=False)
    # 只放行了 3 个详情页存档，其余必然失败
    failed = [i for i in items if i.extra.get("detail_failed")]
    assert failed, "应该有条目详情抓不到"
    for item in failed:
        assert item.title, "标题必须保留"
        assert item.url, "链接必须保留"
        assert item.published_at is not None, "列表页给的日期必须保留"
        assert not item.content_text, "抓不到就不该编内容"
    # 有存档的那条正常拿到正文
    ok = next(i for i in items if i.url.endswith("/9990/90897.htm"))
    assert ok.extra.get("detail_failed") is None
    assert len(ok.content_text or "") > 1000


# --------------------------------------------------------------------------- #
# 9. 选择器写错时要"响亮地失败"，而不是静默返回 0 条
# --------------------------------------------------------------------------- #
async def test_wrong_selector_raises_loudly():
    spec = spec_for("迎新网·本科生报到")
    spec.config = dict(spec.config)
    spec.config["list_selector"] = "div.this-class-does-not-exist tr"
    ctx, _ = make_ctx(synthesize_missing=True)
    with pytest.raises(AdapterError, match="匹配到 0 条"):
        await adapter_for(spec).fetch(spec, ctx)


# --------------------------------------------------------------------------- #
# 10. 离线台自身的纪律：未收录 URL 必须抛错（不然翻页终止条件测不出来）
# --------------------------------------------------------------------------- #
async def test_offline_bed_raises_on_unknown_url():
    from tests.offline import OfflineNetworkError, OfflineFetcher

    f = OfflineFetcher()
    with pytest.raises(OfflineNetworkError):
        await f.get_text("https://yx.dlut.edu.cn/index/not-archived.htm")


def test_archive_is_complete_for_declared_urls():
    from tests.offline import DETAIL_MAP, FIXTURES

    missing = [v for v in URL_MAP.values() if not (FIXTURES / v).exists()]
    assert not missing, f"下列存档缺失：{missing}"
    assert len(DETAIL_MAP) >= 10


def test_synth_page_structures_match_real_templates():
    real = (Path(__file__).parent / "fixtures" / "dlut" / "detail_yx_90897.html").read_text(
        encoding="utf-8"
    )
    synth = synth_detail("https://yx.dlut.edu.cn/info/1/2.htm")
    for marker in ('id="vsb_content"', 'class="v_news_content"'):
        assert marker in real and marker in synth, f"合成页缺少真实模板标记 {marker}"
    assert "合成页" in synth, "合成内容必须自我声明，不能冒充真实数据"
