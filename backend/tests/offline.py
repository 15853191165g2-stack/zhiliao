"""离线回放测试台。

核心手法：把本地存档的 HTML 当"网络"喂给**原封不动的生产适配器**，
不 stub 适配器本身，只 stub 网络这一层。这样测的是真代码路径。

三个纪律（踩过坑才总结出来的）：
  1. 未收录的 URL **必须抛异常**，精确复现真实 404 —— 否则翻页逻辑
     的终止条件在离线环境里测不出来（大工的博达分页正是靠 404 收敛的）。
  2. 详情页存档不够时，用**合成页**把离线台撑到全量，否则下游（分页、
     去重、增量）根本没法验。合成正文里要写明"这是合成页"。
  3. 合成内容的去重 key **不能按 URL 生成**，否则"同一篇通知挂在两个栏目"
     （URL 不同、正文相同）这种跨栏目重复就复现不出来了。
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.base import FetchContext
from app.core.config import get_settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dlut"

# --------------------------------------------------------------------------- #
# URL -> 存档文件
# --------------------------------------------------------------------------- #
LIST_MAP: dict[str, str] = {
    "https://yx.dlut.edu.cn/index/bksbd.htm": "list_yx_bksbd.html",
    "https://yx.dlut.edu.cn/index/yxfc.htm": "list_yx_yxfc.html",
    "https://yx.dlut.edu.cn/": "list_yx_home.html",
    "http://teach.dlut.edu.cn/xwkd/xwkd.htm": "list_teach_xwkd.html",
    "http://xsc.dlut.edu.cn/index/tzgg.htm": "list_xsc_tzgg.html",
    "http://gs.dlut.edu.cn/gzdt1.htm": "list_gs_gzdt.html",
    "http://houqin.dlut.edu.cn/tzgg.htm": "list_houqin_tzgg.html",
    "http://houqin.dlut.edu.cn/tzgg/4.htm": "list_houqin_p2.html",
    "http://its.dlut.edu.cn/index/zxtz.htm": "list_its_zxtz.html",
    "http://www.lib.dlut.edu.cn/xw/tzgg.htm": "list_lib_tzgg.html",
}

DETAIL_MAP: dict[str, str] = {
    "https://yx.dlut.edu.cn/info/9990/90897.htm": "detail_yx_90897.html",
    "https://yx.dlut.edu.cn/info/10092/90837.htm": "detail_yx_imgonly.html",
    "https://yx.dlut.edu.cn/info/9990/90877.htm": "detail_yx_empty.html",
    "http://xsc.dlut.edu.cn/info/1042/7112.htm": "detail_xsc_imgonly.html",
    "http://houqin.dlut.edu.cn/info/1031/8891.htm": "detail_houqin_empty.html",
    "http://gs.dlut.edu.cn/info/1005/50392.htm": "detail_gs_empty.html",
    "http://its.dlut.edu.cn/info/1011/337598.htm": "detail_its_337598.html",
    "http://www.lib.dlut.edu.cn/info/1049/11319.htm": "detail_lib_11319.html",
    "http://teach.dlut.edu.cn/info/1015/17758.htm": "detail_teach_17758.html",
    "https://news.dlut.edu.cn/info/1022/136495.htm": "detail_news_136495.html",
    "https://mp.weixin.qq.com/s/YBRGjCy7hCZDeCyiu5N_lQ": "detail_wechat_article.html",
}

URL_MAP: dict[str, str] = {**LIST_MAP, **DETAIL_MAP}


class OfflineNetworkError(RuntimeError):
    """离线台里的"网络错误"，语义等价于真实环境里的 404 / 超时。"""


# --------------------------------------------------------------------------- #
# 合成页：结构照抄真实模板，只是内容换成占位文字
# --------------------------------------------------------------------------- #
def synth_detail(url: str, host_hint: str = "dlut") -> str:
    """合成一个"结构正确但内容是占位"的详情页。

    结构来自真实存档：`<div id="vsb_content"><div class="v_news_content">…`
    这样正文选择器、正文长度判定、去重指纹都会走真实分支。
    """
    if "mp.weixin.qq.com" in url:
        return (
            "<html><head><title></title></head><body>"
            '<h1 id="activity-name">离线回放合成页·公众号</h1>'
            '<a id="js_name">离线回放合成公众号</a>'
            '<div id="js_content"><p>离线回放合成页，不代表真实内容，仅用于验证抓取链路。</p>'
            + "<p>本页由测试台合成，结构对齐真实公众号文章模板。</p>" * 4
            + "</div>"
            '<script>var create_time = "1789608474";</script>'
            "</body></html>"
        )
    body = "".join(
        f"<p>离线回放合成页，不代表真实内容，仅用于验证抓取链路。第 {i} 段。</p>"
        for i in range(1, 7)
    )
    return (
        f"<html><head><title>离线回放合成页</title></head><body>"
        f'<div class="details"><h1 align="center">离线回放合成页</h1>'
        f'<div align="center">发布时间：2026-01-01</div><hr/>'
        f'<div id="vsb_content"><div class="v_news_content">{body}</div></div>'
        f"</div></body></html>"
    )


class OfflineFetcher:
    """假 Fetcher：只实现适配器用到的那几个方法。

    签名必须和生产代码完全一致（含 `ignore_robots`），否则测的不是真实调用路径。
    """

    def __init__(self, synthesize_missing: bool = False, allow: set[str] | None = None) -> None:
        self.synthesize_missing = synthesize_missing
        self.allow = allow
        self.hits: list[str] = []
        self.stats = {
            "requests": 0,
            "blocked_by_robots": 0,
            "robots_overridden": 0,
            "errors": 0,
        }

    async def get_text(
        self, url: str, *, encoding: str | None = None, ignore_robots: bool = False
    ) -> str:
        self.hits.append(url)
        # 白名单模式下，白名单外的 URL 直接当 404，用来验证"抓不到就走别的路"
        if self.allow is not None and url not in self.allow:
            raise OfflineNetworkError(f"404 Not Found（离线台未放行）: {url}")

        local = URL_MAP.get(url)
        if local is not None:
            path = FIXTURES / local
            if path.exists():
                self.stats["requests"] += 1
                return path.read_bytes().decode("utf-8", errors="replace")

        if self.synthesize_missing and "/info/" in url:
            self.stats["requests"] += 1
            return synth_detail(url)

        raise OfflineNetworkError(f"404 Not Found（离线台无存档）: {url}")

    async def get_bytes(self, url: str) -> bytes:
        return (await self.get_text(url)).encode("utf-8")


def make_ctx(synthesize_missing: bool = False, allow: set[str] | None = None) -> tuple[
    FetchContext, OfflineFetcher
]:
    fetcher = OfflineFetcher(synthesize_missing=synthesize_missing, allow=allow)
    ctx = FetchContext(fetcher=fetcher, settings=get_settings())  # type: ignore[arg-type]
    return ctx, fetcher
