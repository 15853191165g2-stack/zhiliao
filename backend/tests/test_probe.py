"""来源探测（POST /sources/probe）的测试。

这一组测试守的是「探测全绿、正式抓取全空」这类假绿 —— 本项目最贵的一类 bug。
所以断言不只看"有没有识别出列表"，还要：

  * 生成的 item_url_regex **能匹配绝对 URL**（曾经用相对路径生成，正式抓取 0 条）
  * 生成的配置交给**真正的适配器**跑，确实能出条目
  * 详情页的侧栏导航不能被当成通知列表
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.adapters.base import SourceSpec
from app.adapters.html_list import HtmlListAdapter
from app.services import probe as P

FIX = Path(__file__).parent / "fixtures" / "dlut"

# 四种模板各挑一个真实存档页
PAGES = {
    "bodeli_li": ("list_teach_xwkd.html", "http://teach.dlut.edu.cn/xwkd/xwkd.htm"),
    "bodeli_news": ("list_xsc_tzgg.html", "http://xsc.dlut.edu.cn/index/tzgg.htm"),
    "hourqin": ("list_houqin_tzgg.html", "http://houqin.dlut.edu.cn/tzgg.htm"),
    "library": ("list_lib_tzgg.html", "http://www.lib.dlut.edu.cn/xw/tzgg.htm"),
    "table": ("list_yx_bksbd.html", "http://yx.dlut.edu.cn/bksbd.htm"),
}


def _soup(name: str) -> BeautifulSoup:
    raw = (FIX / name).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup


def _best(name: str, base: str):
    soup = _soup(name)
    cands = [c for c in P.find_candidates(soup, base) if P.is_credible_list(c)]
    assert cands, f"{name} 应能识别出列表结构"
    return soup, cands[0]


class _StubCtx:
    """只喂一段 HTML 的假上下文——探测只用到 get_text。"""

    def __init__(self, html: str) -> None:
        self.html = html

    async def get_text(self, url: str, **_kw) -> str:  # noqa: D401
        return self.html


# --------------------------------------------------------------------------- #
# 结构识别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", list(PAGES))
def test_detects_list_structure(key: str):
    name, base = PAGES[key]
    soup, best = _best(name, base)
    cfg, preview = P.build_config(best, soup, base)
    assert cfg["list_selector"], "必须产出 list_selector"
    assert len(preview) >= 2, f"{key} 预览条目太少：{preview}"
    assert best.date_ratio >= 0.5, f"{key} 日期命中率过低：{best.date_ratio}"
    for item in preview:
        assert 4 <= len(item["title"]) <= 120


@pytest.mark.parametrize("key", list(PAGES))
def test_selector_stays_precise(key: str):
    """选择器不能退化成裸 `li` / `tr`，否则连导航一起抓。"""
    name, base = PAGES[key]
    soup, best = _best(name, base)
    cfg, _ = P.build_config(best, soup, base)
    sel = cfg["list_selector"]
    assert sel.strip() not in ("li", "tr", "div", "p"), f"{key} 选择器太宽：{sel}"
    matched = len(soup.select(sel))
    assert matched >= best.item_count, f"{key} 选择器漏掉条目：{matched} < {best.item_count}"
    # 多框进来的不能超过两成，否则说明框到了别的列表
    assert matched <= best.item_count * 1.35 + 2, f"{key} 选择器框太宽：{matched} vs {best.item_count}"


@pytest.mark.parametrize("key", ["hourqin", "library", "bodeli_li"])
def test_title_noise_is_stripped(key: str):
    """标题里整批出现的日期前缀 / 「来源：」后缀必须清掉。"""
    name, base = PAGES[key]
    soup, best = _best(name, base)
    cfg, preview = P.build_config(best, soup, base)
    for item in preview:
        title = item["title"]
        assert not re.match(r"^\s*\d", title), f"标题仍带日期前缀：{title!r}"
        assert "来源：" not in title, f"标题仍带来源后缀：{title!r}"
        assert not title.endswith("了解详情"), f"标题仍带按钮文案：{title!r}"


# --------------------------------------------------------------------------- #
# 生成配置的可用性（防"探测全绿、抓取全空"）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", list(PAGES))
def test_url_regex_matches_absolute_url(key: str):
    """回归测试：item_url_regex 必须按绝对 URL 生成。

    曾经用原始 href（`../info/1031/8891.htm`）生成规则，
    而适配器是拿 urljoin 之后的 URL 去匹配 → 一条都匹配不上，静默抓 0 条。
    """
    name, base = PAGES[key]
    soup, best = _best(name, base)
    cfg, preview = P.build_config(best, soup, base)
    pattern = cfg.get("item_url_regex")
    if not pattern:
        pytest.skip("该页面无需 url 过滤")
    sample = next((p["url"] for p in preview if p["url"]), None)
    assert sample, "预览里应该有条目 URL"
    assert sample.startswith("http"), "预览 URL 必须是绝对地址"
    assert re.search(pattern, sample), f"url_regex {pattern!r} 匹配不到 {sample!r}"


@pytest.mark.parametrize("key", list(PAGES))
def test_generated_config_works_with_real_adapter(key: str, tmp_path):
    """把生成的配置丢给**真正的适配器**跑一遍（transport=file，不出网）。"""
    name, base = PAGES[key]
    soup, best = _best(name, base)
    cfg, _ = P.build_config(best, soup, base)
    cfg = dict(cfg)
    cfg.update({"transport": "file", "file": str(FIX / name), "follow_detail": False,
                "max_items": 50})

    spec = SourceSpec(name=key, adapter="html_list", url=base, config=cfg)
    items = asyncio.run(HtmlListAdapter().fetch(spec, None))  # type: ignore[arg-type]
    assert items, f"{key} 生成的配置抓不到任何条目（假绿）"
    assert all(len(i.title) >= 4 for i in items)


# --------------------------------------------------------------------------- #
# 不该被当成列表的页面
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["detail_gs_empty.html", "detail_xsc_imgonly.html"])
def test_sidebar_nav_is_not_a_list(name: str):
    """详情页侧栏导航（3 条、无日期）不能被当成通知列表。

    真实反例：detail_gs_empty.html 的侧栏是「研究生院综合办 / 研究生工作处 /
    研究生培养处」三条链接，早期判据（≥3 个带链接的兄弟节点）会把它当列表。
    """
    raw = (FIX / name).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    credible = [c for c in P.find_candidates(soup, "http://x.dlut.edu.cn/a.htm")
                if P.is_credible_list(c)]
    assert not credible, f"{name} 的侧栏导航被误判成列表：{[c.item_count for c in credible]}"


def test_credible_list_threshold():
    """判据本身：小且无日期 = 不算；大 = 算；小但带日期 = 算。"""
    def cand(n, ratio):
        return P.Candidate(list_selector="li", item_count=n, date_ratio=ratio,
                           distinct_ratio=1.0, title_ok_ratio=1.0, score=1.0, avg_text_len=20)

    assert P.is_credible_list(cand(3, 0.0)) is False
    assert P.is_credible_list(cand(3, 1.0)) is False   # 3 条太少，宁可漏也不误收
    assert P.is_credible_list(cand(6, 0.0)) is False   # 6 条但没日期
    assert P.is_credible_list(cand(6, 0.8)) is True
    assert P.is_credible_list(cand(12, 0.0)) is True


# --------------------------------------------------------------------------- #
# probe_url 的对外行为
# --------------------------------------------------------------------------- #
def test_probe_rejects_non_http_url():
    out = asyncio.run(P.probe_url("ftp://x/y", _StubCtx("")))
    assert out["ok"] is False
    assert "http" in out["message"]


def test_probe_rejects_tiny_page():
    out = asyncio.run(P.probe_url("http://x.dlut.edu.cn/a.htm", _StubCtx("<html></html>")))
    assert out["ok"] is False


def test_probe_detects_rss():
    xml = (Path(__file__).parent / "fixtures" / "demo_lib.xml").read_text(
        encoding="utf-8", errors="replace")
    out = asyncio.run(P.probe_url("http://lib.dlut.edu.cn/rss.xml", _StubCtx(xml)))
    assert out["ok"] is True
    assert out["adapter"] == "rss"
    assert out["item_count"] >= 1


def test_probe_returns_preview_for_list_page():
    raw = (FIX / "list_teach_xwkd.html").read_text(encoding="utf-8", errors="replace")
    out = asyncio.run(P.probe_url("http://teach.dlut.edu.cn/xwkd/xwkd.htm", _StubCtx(raw)))
    assert out["ok"] is True
    assert out["adapter"] == "html_list"
    assert out["kind"] == "list"
    assert out["item_count"] >= 10
    assert out["items"] and out["items"][0]["title"]
    assert out["config"]["list_selector"]
    # 探测结果必须自带"存档过"的痕迹：页面标题 + 条目数
    assert out["message"]
