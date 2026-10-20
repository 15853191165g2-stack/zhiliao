"""单篇链接导入适配器（article_url）。

用途：**微信没有公开接口**，拿不到「某个公众号的最新文章列表」。
能拿到的只有两种东西：
  1. 用户在微信里点「···→ 复制链接」得到的**单篇文章 URL**
  2. 第三方 RSS 桥接服务（见 `rss` 适配器 + sources/dlut_wechat.yaml）

本适配器负责第 1 种：给一个文章 URL，抓回标题/正文/公众号名/发布时间。

实测（2026-10）：
  - `https://mp.weixin.qq.com/s/xxxx` 用手机端 UA 可以直接抓到完整正文，
    未触发风控页；正文在 `#js_content`，标题在 `#activity-name`，
    公众号名在 `#js_name`
  - **发布时间不在 DOM 里**（`#publish_time` 是空的），只存在于页面内联 JS 的
    `var create_time = "1789608474"` 里，所以用 `date_regex_raw` 从原始 HTML 里抠

这个适配器不限于微信：任何「一篇一个 URL」的页面（比如小程序分享出来的
落地页、学校 APP 分享出来的详情页）都可以用它导入。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.adapters.base import AdapterError, FetchContext, RawItem, SourceSpec
from app.adapters.registry import register
from app.core.timeutil import normalize_text, parse_dt

# 微信文章的默认选择器（配置里可以覆盖）
DEFAULT_TITLE_SELECTORS = ("#activity-name", "h1.rich_media_title", "h1", "title")
DEFAULT_CONTENT_SELECTORS = ("#js_content", ".rich_media_content", "article", "main")
DEFAULT_AUTHOR_SELECTORS = ("#js_name", ".rich_media_meta_nickname")
DEFAULT_DATE_SELECTORS = ("#publish_time", "em#publish_time", ".rich_media_meta_text")

# 从内联 JS 里抠发布时间戳：var create_time = "1789608474"
DEFAULT_DATE_REGEX_RAW = r'var\s+(?:create_time|ct)\s*=\s*[\'"](?P<ts>\d{9,13})[\'"]'

# 抓到的页面里出现这些字样，多半是被风控/登录墙拦了，要明确报错而不是存空数据
_BLOCK_HINTS = ("环境异常", "去验证", "请在微信客户端打开链接", "访问过于频繁", "参数错误")


@register("article_url")
class ArticleUrlAdapter:
    """把单个/多个文章 URL 变成统一的通知条目。"""

    kind = "article_url"

    async def fetch(self, spec: SourceSpec, ctx: FetchContext) -> list[RawItem]:
        cfg = spec.config or {}
        urls = self._target_urls(spec, cfg)
        if not urls:
            raise AdapterError(f"[{spec.name}] article_url 需要 url 或 config.urls")

        results: list[RawItem] = []
        failures: list[str] = []
        # 公众号文章的正文抓取默认**遵守 robots.txt**。而 mp.weixin.qq.com 的
        # robots.txt 是 `Disallow: /`，所以默认情况下这里一定抓不到正文。
        # 只有用户在配置里显式写 `ignore_robots: true` 才会放行 ——
        # 那个开关的语义是「这是我手动粘贴过来的单篇链接，我知情并接受」。
        ignore_robots = bool(cfg.get("ignore_robots", False))
        for url in urls:
            try:
                html = await ctx.get_text(
                    url, encoding=cfg.get("encoding"), ignore_robots=ignore_robots
                )
            except Exception as e:  # noqa: BLE001
                failures.append(f"{url} -> {type(e).__name__}: {e}"[:200])
                continue

            item = self._parse(url, html, spec, cfg)
            if item is None:
                failures.append(f"{url} -> 页面被风控拦截或正文为空，未导入")
                continue
            results.append(item)

        if not results and failures:
            raise AdapterError(f"[{spec.name}] 全部导入失败：" + "；".join(failures))
        # 部分失败不影响其他链接，但要留下痕迹，避免"怎么少导了一条"无从查起
        if failures and results:
            results[0].extra.setdefault("import_warnings", []).extend(failures)
        return results

    # ------------------------------------------------------------------ #
    @staticmethod
    def _target_urls(spec: SourceSpec, cfg: dict) -> list[str]:
        raw = cfg.get("urls")
        urls: list[str] = []
        if isinstance(raw, str):
            urls = [raw]
        elif isinstance(raw, (list, tuple)):
            urls = [str(x) for x in raw]
        if spec.url:
            urls.insert(0, spec.url)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            u = (u or "").strip()
            if not u or u in seen:
                continue
            if not urlparse(u).scheme.startswith("http"):
                continue
            seen.add(u)
            out.append(u)
        return out

    def _parse(
        self, url: str, html: str, spec: SourceSpec, cfg: dict
    ) -> RawItem | None:
        if not html:
            return None
        # 风控/登录墙：正文选择器一定命不中，且页面文本里会带提示语
        head_text = normalize_text(BeautifulSoup(html[:6000], "lxml").get_text(" ", strip=True))
        if any(h in head_text for h in _BLOCK_HINTS) and not re.search(
            r'id="js_content"', html
        ):
            return None

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title = self._first_text(soup, cfg.get("title_selector"), DEFAULT_TITLE_SELECTORS)
        title = normalize_text(title or "")
        if not title:
            return None

        content_node = self._first_node(soup, cfg.get("content_selector"), DEFAULT_CONTENT_SELECTORS)
        html_str = str(content_node) if content_node is not None else ""
        text = normalize_text(content_node.get_text("\n", strip=True)) if content_node else ""

        author = self._first_text(soup, cfg.get("author_selector"), DEFAULT_AUTHOR_SELECTORS)

        published = None
        for sel in self._as_list(cfg.get("date_selector")) or list(DEFAULT_DATE_SELECTORS):
            raw = soup.select_one(sel)
            if raw is not None:
                published = parse_dt(raw.get_text(" ", strip=True))
                if published:
                    break
        if published is None:
            published = self._date_from_raw(html, cfg)

        return RawItem(
            title=title,
            url=url,
            published_at=published,
            author=normalize_text(author) if author else None,
            content_html=html_str or None,
            content_text=text or None,
            category=spec.category_hint or "其他",
            extra={"imported": True, "import_kind": "article_url"},
        ).clean()

    @staticmethod
    def _date_from_raw(html: str, cfg: dict) -> datetime | None:
        """从原始 HTML 里抠发布时间。支持 unix 时间戳与普通日期串。"""
        pattern = cfg.get("date_regex_raw") or DEFAULT_DATE_REGEX_RAW
        try:
            m = re.search(pattern, html)
        except re.error:
            return None
        if not m:
            return None
        raw = (m.groupdict().get("ts") or m.group(1) or "").strip()
        if not raw:
            return None
        if raw.isdigit() and len(raw) in (9, 10, 13):
            ts = int(raw)
            if len(raw) == 13:
                ts //= 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
        return parse_dt(raw)

    @classmethod
    def _first_text(cls, soup: BeautifulSoup, custom: object, defaults: tuple[str, ...]) -> str | None:
        for sel in cls._as_list(custom) or list(defaults):
            node = soup.select_one(sel)
            if node is not None:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return None

    @classmethod
    def _first_node(cls, soup: BeautifulSoup, custom: object, defaults: tuple[str, ...]):
        for sel in cls._as_list(custom) or list(defaults):
            node = soup.select_one(sel)
            if node is not None:
                return node
        return None

    @staticmethod
    def _as_list(value: object) -> list[str]:
        if not value:
            return []
        seq = [value] if isinstance(value, str) else list(value)  # type: ignore[arg-type]
        return [str(v).strip() for v in seq if str(v).strip()]
