"""RSS / Atom 适配器。

用途：
  - 学校自己提供 RSS 的站点
  - 第三方 RSS 桥接出来的公众号源（用户在 App 里自行添加）

只做标准 feed 解析，不碰任何私有接口。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.adapters.base import AdapterError, FetchContext, RawItem, SourceSpec, read_local_text
from app.adapters.registry import register
from app.core.timeutil import normalize_text, to_utc_naive


@register("rss")
class RssAdapter:
    kind = "rss"

    async def fetch(self, spec: SourceSpec, ctx: FetchContext) -> list[RawItem]:
        cfg = spec.config or {}
        # `bridge_url` 是给「公众号 RSS 桥接」这种地址还没定下来的来源用的：
        # 先在 yaml 里占位、把来源关掉，等用户拿到桥接地址再启用来填。
        target = (spec.url or "").strip() or str(cfg.get("bridge_url") or "").strip()
        if not target:
            raise AdapterError(
                f"[{spec.name}] rss 适配器需要 url 或 config.bridge_url"
            )

        max_items = max(1, int(cfg.get("max_items", 50)))
        fetch_full = bool(cfg.get("fetch_full_text", False))
        encoding = cfg.get("charset") or cfg.get("encoding")

        if str(cfg.get("transport", "http")).lower() == "file":
            # 离线开发：直接读本地 feed 文件
            raw = read_local_text(str(cfg.get("file") or target), None)
        else:
            raw = await ctx.get_text(target, encoding=encoding)

        import feedparser  # 延迟导入，加快冷启动

        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise AdapterError(
                f"[{spec.name}] feed 解析失败：{getattr(feed, 'bozo_exception', 'unknown')}"
            )

        items: list[RawItem] = []
        for entry in feed.entries[:max_items]:
            title = normalize_text(entry.get("title") or "")
            if not title:
                continue

            link = entry.get("link") or None
            published = self._entry_time(entry)

            content_html = None
            content_text = None
            if entry.get("content"):
                content_html = entry["content"][0].get("value")
                content_text = normalize_text(BeautifulSoupText(content_html))
            elif entry.get("summary"):
                content_html = entry["summary"]
                content_text = normalize_text(BeautifulSoupText(content_html))

            item = RawItem(
                title=title,
                url=link,
                published_at=published,
                author=normalize_text(entry.get("author") or "") or None,
                content_html=content_html,
                content_text=content_text,
                category=spec.category_hint,
            ).clean()

            if fetch_full and link and not (content_text and len(content_text) > 200):
                await self._fill_from_link(item, ctx, cfg)

            items.append(item)

        return items

    @staticmethod
    def _entry_time(entry) -> datetime | None:
        """feedparser 的 *_parsed 已经是 **UTC** 的 struct_time。

        这里必须先标成 UTC 再归一化，否则会被当成北京时间二次偏移 8 小时。
        """
        for key in ("published_parsed", "updated_parsed", "created_parsed"):
            parsed = entry.get(key)
            if parsed:
                try:
                    aware = datetime(*parsed[:6], tzinfo=timezone.utc)
                    return to_utc_naive(aware)
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    async def _fill_from_link(item: RawItem, ctx: FetchContext, cfg: dict) -> None:
        """公众号 RSS 桥接常只给摘要，可选地进原文页取正文。"""
        from bs4 import BeautifulSoup

        try:
            html = await ctx.get_text(item.url)
        except Exception:  # noqa: BLE001
            return
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        selector = (cfg.get("detail") or {}).get("content_selector")
        selectors = [selector] if isinstance(selector, str) else list(selector or [])
        node = None
        for sel in selectors:
            if not sel:
                continue
            node = soup.select_one(sel)
            if node is not None:
                break
        if node is None:
            node = soup.body or soup
        item.content_html = str(node)
        item.content_text = normalize_text(node.get_text("\n", strip=True))


def BeautifulSoupText(html: str | None) -> str:  # noqa: N802 - 保持语义化命名
    if not html:
        return ""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
