"""通用 HTML 列表抓取器（配置驱动）。

学校官网 / 教务处 / 学院 / 图书馆的通知列表页，绝大多数是同一个套路：
一张列表 → 一堆 <li> 或 <tr> → 里面一个 <a> 标题 + 一个日期。

所以这里把「套路」做成配置，`sources/*.yaml` 里改选择器就行，不用写代码。
遇到真不规则的页面（JS 渲染 / 需要登录），再单独写新适配器，或给 html_list
加一个 transport 实现。

支持的 transport：
  - http : 正常抓网页（默认）
  - file : 读本地 HTML 文件，用于离线开发与回归测试

配置项（全部可选，除 list_selector / fields.title）：
  list_selector      : str   列表项选择器，命中一个节点 = 一条通知
  fields             : dict  title / date / url / author / excerpt 的取值选择器
                       `a` 取文本，`a@href` 取属性
  item_url_regex     : str   只保留 URL 命中该正则的条目（挡掉导航栏里的假链接）
  title_cleanup_regex: str|list  标题清洗，匹配到的部分直接删除
  date_extract_regex : str   从「整行文本」里抠日期（日期被拆到兄弟节点时用）
  date_format        : str   strptime 格式，优先于内置解析
  max_items          : int   单来源最多取多少条（默认 50）
  follow_detail      : bool  是否进详情页补正文（默认 False）
  detail             : dict  content_selector（str|list，支持回退链）、
                             date_selector、author_selector
  pagination         : dict  mode=path_index 时按 {path_template} 枚举页码；
                             也可用 next_selector 走「下一页」链接
  transport          : str   http | file
  encoding           : str   覆盖编码（填 auto 表示自动嗅探）
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.adapters.base import AdapterError, FetchContext, RawItem, SourceSpec, read_local_text
from app.adapters.registry import register
from app.core.config import BASE_DIR
from app.core.timeutil import normalize_text, parse_dt

# 正文「看起来为空」的判定阈值：低于这个字数且没有图片/表格，就算空壳。
# 之所以要看图片：纯图片海报页的正文纯文本长度天然是 0，那是合法内容，
# 不能当成抓取失败（大工学生处、后勤处、研究生院都有这类页面）。
EMPTY_TEXT_THRESHOLD = 20
_MEDIA_HINT = re.compile(r"<(img|table|video|iframe|embed)\b", re.I)


@register("html_list")
class HtmlListAdapter:
    """从 HTML 列表页抓通知。靠 yaml 配置适配任意学校站点。"""

    kind = "html_list"

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def fetch(self, spec: SourceSpec, ctx: FetchContext) -> list[RawItem]:
        cfg = spec.config or {}
        list_selector = str(cfg.get("list_selector") or "").strip()
        if not list_selector:
            raise AdapterError(f"[{spec.name}] html_list 缺少必填配置 list_selector")

        fields: dict[str, str] = cfg.get("fields") or {}
        if not fields.get("title"):
            raise AdapterError(f"[{spec.name}] html_list 缺少 fields.title")

        url_base = str(cfg.get("url_base") or spec.url or "")
        max_pages = max(1, int((cfg.get("pagination") or {}).get("max_pages", 1)))
        max_items = max(1, int(cfg.get("max_items", 50)))
        transport = str(cfg.get("transport", "http")).lower()
        follow_detail = bool(cfg.get("follow_detail", False))
        item_url_re = self._compile(cfg.get("item_url_regex"))
        cleanup_res = self._compile_list(cfg.get("title_cleanup_regex"))

        results: list[RawItem] = []
        seen_urls: set[str] = set()
        page_url: str | None = spec.url or cfg.get("file")
        local_base: Path | None = None
        first_soup: BeautifulSoup | None = None
        pages_fetched = 0

        for page_no in range(max_pages):
            if not page_url:
                break

            try:
                html, resolved = await self._load(
                    ctx, transport, page_url, cfg, local_base=None, use_file_override=True
                )
            except Exception:
                # 第 1 页失败 = 整个来源失败，必须往上报；后续页失败 = 翻到底了，正常收尾
                if page_no == 0:
                    raise
                break

            if transport == "file" and resolved is not None:
                local_base = resolved.parent
            if not html:
                break

            soup = BeautifulSoup(html, "lxml")
            if first_soup is None:
                first_soup = soup
            pages_fetched += 1

            nodes = soup.select(list_selector)
            if not nodes and page_no == 0:
                raise AdapterError(
                    f"[{spec.name}] 选择器 '{list_selector}' 匹配到 0 条，"
                    f"页面结构可能已变化，请检查 sources/{spec.name}.yaml"
                )

            accepted_this_page = 0
            for node in nodes:
                item = self._parse_node(node, fields, url_base, cfg, cleanup_res, item_url_re)
                if item is None or not item.is_usable:
                    continue
                if item.url and item.url in seen_urls:
                    continue
                if item.url:
                    seen_urls.add(item.url)
                results.append(item)
                accepted_this_page += 1
                if len(results) >= max_items:
                    break

            if len(results) >= max_items:
                break

            if page_no + 1 < max_pages:
                if pages_fetched > 1 and accepted_this_page == 0 \
                        and bool((cfg.get("pagination") or {}).get("stop_on_empty", True)):
                    break
                # page_no 是 0-based，而配置里的页码是 1-based（第 1 页就是列表页本身），
                # 所以"下一页"的页码是 page_no + 2。
                next_url = self._next_page_url(
                    cfg, page_no + 2, first_soup, url_base or page_url, soup
                )
                if not next_url or next_url == page_url:
                    break
                page_url = next_url

        # ---- 详情页：补齐正文（AI 摘要的质量主要靠这一步）----
        if follow_detail and results:
            for item in results:
                if item.content_text:
                    continue
                await self._fill_detail(item, cfg, ctx, transport, local_base)

        return results

    # ------------------------------------------------------------------ #
    # 列表项解析
    # ------------------------------------------------------------------ #
    def _parse_node(
        self,
        node: Tag,
        fields: dict[str, str],
        url_base: str,
        cfg: dict,
        cleanup_res: list[re.Pattern[str]],
        item_url_re: re.Pattern[str] | None,
    ) -> RawItem | None:
        title = self._extract(node, fields["title"])
        if not title:
            return None
        title = self._clean_title(normalize_text(title), cleanup_res)
        if not title:
            return None

        raw_url = self._extract_multi(node, [fields.get("url"), "a@href", fields["title"]])
        url = self._absolutize(raw_url, url_base)
        if item_url_re is not None and not (url and item_url_re.search(url)):
            return None
        if url and not self._looks_like_url(url):
            url = None

        # 日期取值：先试 fields.date 指向的节点，再退到整行文本。
        # 两种情况都很常见——大工迎新网把日期放在兄弟 <td> 里，
        # 后勤处把「日」和「年-月」拆成两个 <p>，图书馆拆成一个 <time> 里的两个 span。
        published = None
        candidates: list[str] = []
        if fields.get("date"):
            raw_date = self._extract(node, fields["date"])
            if raw_date:
                candidates.append(raw_date)
        candidates.append(node.get_text(" ", strip=True))
        for raw in candidates:
            for piece in self._date_pieces(raw, cfg):
                published = parse_dt(piece, fmt=cfg.get("date_format"))
                if published is not None:
                    break
            if published is not None:
                break

        author = self._extract(node, fields["author"]) if fields.get("author") else None

        excerpt = None
        if fields.get("excerpt"):
            raw_excerpt = self._extract(node, fields["excerpt"])
            excerpt = normalize_text(raw_excerpt)[:200] if raw_excerpt else None

        return RawItem(
            title=title,
            url=url,
            published_at=published,
            author=normalize_text(author) if author else None,
            content_text=None,
            extra={"excerpt": excerpt} if excerpt else {},
        ).clean()

    @staticmethod
    def _date_pieces(raw: str | None, cfg: dict) -> list[str]:
        """把一段文本切成若干「待解析」的日期候选串。

        配了 `date_extract_regex` 就先用它抠出规整的一段（比如从
        「21 2026-09 关于…」里抠出「21 2026-09」），再原样兜一次。
        """
        if not raw:
            return []
        out: list[str] = []
        pattern = cfg.get("date_extract_regex") or cfg.get("date_regex")
        if pattern:
            m = re.search(pattern, raw)
            if m:
                out.append(m.group(0))
        out.append(raw)
        return out

    @staticmethod
    def _clean_title(title: str, cleanup_res: list[re.Pattern[str]]) -> str:
        out = title.strip()
        for rex in cleanup_res:
            out = rex.sub("", out)
        return out.strip(" ·•\t\r\n")

    @staticmethod
    def _compile(pattern: str | None) -> re.Pattern[str] | None:
        if not pattern:
            return None
        try:
            return re.compile(pattern)
        except re.error as e:
            raise AdapterError(f"非法正则 {pattern!r}：{e}") from e

    @classmethod
    def _compile_list(cls, patterns: str | list[str] | None) -> list[re.Pattern[str]]:
        if not patterns:
            return []
        seq = [patterns] if isinstance(patterns, str) else list(patterns)
        return [p for p in (cls._compile(x) for x in seq) if p is not None]

    @staticmethod
    def _extract(node: Tag, selector: str | None) -> str | None:
        """支持 `div.date` 取文本，`a@href` 取属性。"""
        if not selector:
            return None
        selector = selector.strip()
        if "@" in selector:
            sel, attr = selector.rsplit("@", 1)
            target: Tag | None = node.select_one(sel) if sel.strip() else node
            if target is None:
                return None
            value = target.get(attr.strip())
        else:
            target = node.select_one(selector)
            if target is None:
                return None
            value = target.get_text(" ", strip=True)
        if value is None:
            return None
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        return str(value).strip() or None

    @classmethod
    def _extract_multi(cls, node: Tag, selectors: list[str | None]) -> str | None:
        for sel in selectors:
            if not sel:
                continue
            val = cls._extract(node, sel)
            if val:
                return val
        return None

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        t = text.strip().lower()
        if t.startswith(("http://", "https://", "/", "./", "../", "?")):
            return True
        return t.endswith((".htm", ".html", ".shtml", ".php", ".aspx", ".jsp", ".pdf"))

    @staticmethod
    def _absolutize(url: str | None, base: str) -> str | None:
        if not url:
            return None
        url = url.strip()
        if url.lower().startswith(("javascript:", "mailto:", "#")):
            return None
        if not base:
            return url
        return urljoin(base, url)

    # ------------------------------------------------------------------ #
    # 分页
    # ------------------------------------------------------------------ #
    def _next_page_url(
        self,
        cfg: dict,
        next_page_no: int,
        first_soup: BeautifulSoup | None,
        url_base: str,
        current_soup: BeautifulSoup,
    ) -> str | None:
        """`next_page_no` 是**即将抓取的那一页的 1-based 页码**（第 1 页 = 列表页 URL）。

        对 `path_index` 模式，页码要再映射成文件名里的数字，见下面注释。
        """
        pag = cfg.get("pagination") or {}
        mode = str(pag.get("mode") or "").strip()

        if mode == "path_index":
            tpl = str(pag.get("path_template") or "").strip()
            if not tpl:
                return None
            max_index = pag.get("max_index")
            if not max_index:
                # 从第 1 页的页码控件里取最大页码。
                # ★ 必须严格按 path_template 的形状去匹配，不能写成通用的
                #   `/(\d+)\.htm` —— 那样会把文章链接 info/1031/8891.htm 里的
                #   8891 当成页码（踩过，翻页直接跳到 tzgg/8890.htm 这种垃圾地址）。
                max_index = self._discover_max_index(first_soup or current_soup, tpl)
            try:
                max_index = int(max_index)
            except (TypeError, ValueError):
                return None
            if max_index < 1:
                return None
            descending = bool(pag.get("index_desc", True))
            # 博达的反向编号（用实测数据推的）：
            #   页码 1 = tzgg.htm（无数字后缀）
            #   页码 2 = tzgg/4.htm   页码 3 = tzgg/3.htm … 页码 5 = tzgg/1.htm
            # 即 页码 k (k>=2) 对应 N = max_index - (k - 1)。max_index=5 时 k=2 → 4。
            # 注意 max_index 是"页码控件里出现过的最大数字"，它本身可能是个空页，
            # 所以这里不去校验它，而是靠 404 / 空页自动收敛。
            index = max_index - (next_page_no - 1) if descending else next_page_no
            if index < 1:
                return None
            return urljoin(url_base, tpl.format(n=index))

        sel = pag.get("next_selector")
        if not sel:
            return None
        node = current_soup.select_one(sel)
        if not node or not node.has_attr("href"):
            return None
        return urljoin(url_base, str(node.get("href")))

    @staticmethod
    def _discover_max_index(soup: BeautifulSoup, path_template: str) -> int | None:
        """从页码控件里取最大页码。

        匹配必须严格按 `path_template` 的形状（把 `{n}` 换成捕获组），
        否则文章链接里的数字会被误当成页码。
        """
        tpl_re = re.compile(
            re.escape(path_template).replace(re.escape("{n}"), r"(\d+)")
        )
        best: int | None = None
        for a in soup.find_all("a", href=True):
            m = tpl_re.search(str(a["href"]))
            if not m:
                continue
            v = int(m.group(1))
            if best is None or v > best:
                best = v
        return best

    # ------------------------------------------------------------------ #
    # 详情页
    # ------------------------------------------------------------------ #
    async def _fill_detail(
        self,
        item: RawItem,
        cfg: dict,
        ctx: FetchContext,
        transport: str,
        local_base: Path | None,
    ) -> None:
        """进详情页补正文。

        抓不到就**记下来并保留列表已知的字段**，不做静默丢弃：
        列表页的标题/日期/链接本身就有价值，不该因为详情页抽风而整条丢掉。
        """
        if not item.url:
            return
        detail_cfg = cfg.get("detail") or {}

        try:
            html, _ = await self._load(
                ctx,
                transport,
                item.url,
                cfg,
                local_base=local_base,
                use_file_override=False,
                ignore_robots=bool(cfg.get("ignore_robots")),
            )
        except Exception as e:  # noqa: BLE001
            item.extra["detail_failed"] = True
            item.extra["detail_error"] = f"{type(e).__name__}: {e}"[:200]
            return
        if not html:
            item.extra["detail_failed"] = True
            item.extra["detail_error"] = "详情页返回空内容"
            return

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # 正文容器支持回退链：第一个命中的就用，不做「文本长度门槛」过滤
        # （纯图片海报页的文本长度天然为 0，加了门槛反而会掉进兜底抓到整页外壳）
        selectors = detail_cfg.get("content_selector")
        node = None
        for sel in self._as_list(selectors):
            node = soup.select_one(sel)
            if node is not None:
                item.extra["detail_selector"] = sel
                break
        if node is None:
            # 没配或没命中：只回退到 <body>，并显式标记，便于事后排查
            node = soup.body or soup
            item.extra["detail_selector"] = "<body>"
            item.extra["detail_fallback"] = True

        html_str = str(node)
        text = normalize_text(node.get_text("\n", strip=True))
        item.content_html = html_str
        item.content_text = text
        if len(text) < EMPTY_TEXT_THRESHOLD and not _MEDIA_HINT.search(html_str):
            item.extra["content_empty"] = True

        if not item.published_at and detail_cfg.get("date_selector"):
            for sel in self._as_list(detail_cfg.get("date_selector")):
                raw = self._extract(soup, sel)
                item.published_at = parse_dt(raw)
                if item.published_at:
                    break

        if not item.author and detail_cfg.get("author_selector"):
            raw = self._extract(soup, detail_cfg.get("author_selector"))
            item.author = normalize_text(raw) if raw else None

    @staticmethod
    def _as_list(value: str | list[str] | None) -> list[str]:
        if not value:
            return []
        seq = [value] if isinstance(value, str) else list(value)
        return [str(v).strip() for v in seq if str(v).strip()]

    # ------------------------------------------------------------------ #
    # 取页面
    # ------------------------------------------------------------------ #
    async def _load(
        self,
        ctx: FetchContext,
        transport: str,
        url: str,
        cfg: dict,
        local_base: Path | None,
        use_file_override: bool,
        ignore_robots: bool = False,
    ) -> tuple[str, Path | None]:
        if transport == "file":
            raw = (cfg.get("file") or url) if use_file_override else url
            if not raw:
                return "", None
            p = Path(raw)
            if p.is_absolute():
                candidates = [p]
            else:
                # 依次尝试：列表页同级目录 → backend 根目录 → 原样
                candidates = ([local_base / raw] if local_base else []) + [BASE_DIR / raw, p]
            chosen = next((c for c in candidates if c.exists()), candidates[0])
            return read_local_text(str(chosen), None), chosen

        encoding = cfg.get("encoding")
        if encoding and str(encoding).lower() == "auto":
            encoding = None
        return await ctx.get_text(url, encoding=encoding, ignore_robots=ignore_robots), None
