"""来源探测：给一个网址，判断它能不能抓、该用哪个适配器、选择器怎么写。

**为什么需要它**：`html_list` 适配器吃的是配置（list_selector / fields），
让用户直接写 CSS 选择器是不现实的。但「粘贴一个网址就能加源」又必须成立，
否则「添加来源」等于让用户去读 HTML。

所以这里做一件很窄的事：**打开这个网址，把页面里像"通知列表"的结构找出来，
用正式的解析器试跑一遍，把真实的标题摆给用户看**。用户确认的是
「这几条标题对不对」，而不是「选择器写得对不对」——后者他根本判断不了。

三条不可退让的约束：

1. **出网必须走 Fetcher**。robots 校验、同域限速、UA、重试都在那儿，
   这里自己 new 一个 httpx.Client 就等于绕过合规闸门（项目铁律第 4 条）。
2. **预览必须用正式解析器算**，不能另写一套。用 `HtmlListAdapter._parse_node`
   而不是重新实现一遍，是为了让「探测看到的 8 条」和「入库的 8 条」同源。
   另写一套的结果是探测全绿、真抓全空——这种假绿本项目已经踩过太多次。
3. **猜不出来就说猜不出来**。找不到列表就返回 `ok=False` 并说明原因，
   绝不返回一个「大概能跑」的配置让用户存进去，然后每天静默失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.adapters.base import FetchContext
from app.adapters.html_list import HtmlListAdapter
from app.core.timeutil import normalize_text, parse_dt

# 可能是「一条通知」的容器子节点
_ITEM_TAGS = ("li", "tr", "dd", "dt", "div", "p", "article", "section", "h3", "h4")
# 日期长相（先粗筛，再交给 parse_dt 确认；parse_dt 才是唯一标准）
_DATE_HINT = re.compile(
    r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}"
    r"|20\d{2}\s+\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}\s+20\d{2}[-/.]\d{1,2}"
)
# 详情页正文容器的常见写法（按命中优先级排）
DETAIL_SELECTORS = [
    "#vsb_content", ".v_news_content", "#vsb_content_1001", "#vsb_content_2",
    "#js_content", ".rich_media_content", ".TRS_Editor", "#content", ".content",
    ".article-content", ".article_content", "article", ".wp_articlecontent",
    "#artibody", ".show_content", ".detail-content", "main",
]
# 导航类噪声：这些 class/id 里的列表一般不是通知
_NOISE_HINT = re.compile(
    r"nav|menu|crumb|footer|header|sidebar|copyright|share|related|search|pager|page",
    re.I,
)


@dataclass
class Candidate:
    """一个「可能是通知列表」的结构。"""

    list_selector: str
    item_count: int
    date_ratio: float
    distinct_ratio: float
    title_ok_ratio: float
    score: float
    avg_text_len: float
    nodes: list[Tag] = field(default_factory=list, repr=False)


def _css_ok(token: str) -> bool:
    """类名/ID 能否直接写进 CSS 选择器。"""
    return bool(re.fullmatch(r"[A-Za-z_][-\w]*", token or ""))


def _tag_selector(el: Tag, prefer_id: bool = False) -> str:
    """给一个元素生成尽量短、尽量唯一的 CSS 选择器。"""
    name = str(el.name)
    el_id = el.get("id")
    if prefer_id and isinstance(el_id, str) and _css_ok(el_id):
        return f"#{el_id}"
    classes = [c for c in (el.get("class") or []) if _css_ok(c)]
    if classes:
        return name + "".join("." + c for c in classes[:2])
    return name


def _first_href(node: Tag) -> str | None:
    a = node.find("a", href=True)
    if a is None:
        return None
    href = str(a.get("href") or "").strip()
    if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
        return None
    return href


def _anchor_title(node: Tag) -> str:
    a = node.find("a", href=True)
    if a is None:
        return ""
    # 优先取 title 属性（列表常把完整标题放这里，a 文本会被截断加省略号）
    for raw in (a.get("title"), a.get_text(" ", strip=True)):
        text = normalize_text(str(raw or "")).strip()
        if len(text) >= 6:
            return text
    return ""


def _has_date(node: Tag) -> bool:
    text = node.get_text(" ", strip=True)
    for m in _DATE_HINT.finditer(text):
        if parse_dt(m.group(0)) is not None:
            return True
    return False


def find_candidates(soup: BeautifulSoup, base_url: str) -> list[Candidate]:
    """扫描整页，找出所有「多个兄弟节点、每个都带标题链接」的结构。"""
    out: list[Candidate] = []
    for container in soup.find_all(True):
        name = str(container.name)
        if name in ("html", "head", "script", "style", "table", "ul", "ol", "dl", "body"):
            # table/ul/dl 本身作为容器时子节点是 tr/li/dd，下面按子节点算；
            # body 允许但会被严重降权（整页当列表 = 抓一堆导航）
            if name in ("html", "head", "script", "style"):
                continue
        kids = [c for c in container.find_all(recursive=False) if isinstance(c, Tag)]
        if len(kids) < 3:
            continue
        items = [c for c in kids if str(c.name) in _ITEM_TAGS and _anchor_title(c)]
        if len(items) < 3:
            continue

        hrefs = [(_first_href(c) or "") for c in items]
        distinct = len({h for h in hrefs if h}) / max(1, len(hrefs))
        date_ratio = sum(1 for c in items if _has_date(c)) / len(items)
        titles = [_anchor_title(c) for c in items]
        title_ok = sum(1 for t in titles if 6 <= len(t) <= 90) / len(titles)
        avg_len = sum(len(t) for t in titles) / len(titles)

        # 结构分：条目数是基础，日期命中率是「这是通知列表」的最强信号，
        # 标题长度分布用来排除「正文段落被当成列表」。
        score = len(items) * (1.0 + 1.2 * date_ratio)
        score *= 0.35 + 0.65 * distinct
        score *= 0.3 + 0.7 * title_ok
        if avg_len > 120:
            score *= 0.35          # 一屏长段落，不是标题列表
        if name == "body":
            score *= 0.05          # 整页兜底，几乎必然是噪声
        if _NOISE_HINT.search(" ".join(
                [str(container.get("id") or "")] + [str(c) for c in (container.get("class") or [])]
        )):
            score *= 0.3

        if score <= 0:
            continue
        out.append(Candidate(
            list_selector="", item_count=len(items), date_ratio=date_ratio,
            distinct_ratio=distinct, title_ok_ratio=title_ok, score=score,
            avg_text_len=avg_len, nodes=items,
        ))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _selector_for(cand: Candidate, soup: BeautifulSoup) -> str:
    """把候选结构翻译成 html_list 的 list_selector。

    难点是「够精确又不脆」：`li` 会连导航一起抓，`div.list li` 才对，
    但 `div.list div ul li` 这种一路写到底的，站点一改中间层就失效。

    规则：只保留**带 class/id 的祖先**（最多两层），中间没有 class 的层省略——
    后代选择器（空格）本来就可以跨层匹配，所以 `div.list div ul li` 和
    `div.list li` 命中的是同一批节点，后者明显更耐用。
    最后用真实页面校验命中数，对不上就退回完整链路。
    """
    first = cand.nodes[0]
    item_sel = _tag_selector(first)
    item_tag = str(first.name)

    chain: list[str] = []          # 祖先，由近到远
    node = first.parent
    hops = 0
    while isinstance(node, Tag) and str(node.name) not in ("html", "body", "[document]") and hops < 4:
        sel = _tag_selector(node)
        chain.append(sel)
        node = node.parent
        hops += 1

    if not chain:
        return item_sel

    # 带 class / id 的祖先，最近的优先，最多保留两层
    classy = [s for s in chain if ("." in s or "#" in s)][:2]

    n = len(cand.nodes)
    tolerance = n + max(2, int(n * 0.2))

    def count(sel: str) -> int | None:
        try:
            return len(soup.select(sel))
        except Exception:  # noqa: BLE001
            return None

    options: list[str] = []
    if classy:
        options.append(" ".join([classy[-1], *classy[:-1][::-1], item_sel]))
    options.append(" ".join([*chain[::-1], item_sel]))
    options.append(item_sel)

    for sel in options:
        c = count(sel)
        if c is not None and n <= c <= tolerance:
            return sel
    # 都对不齐：取命中最接近条目数的那个，宁可多框一点也别漏
    best, best_gap = options[-1], None
    for sel in options:
        c = count(sel)
        if c is None:
            continue
        gap = abs(c - n)
        if best_gap is None or gap < best_gap:
            best, best_gap = sel, gap
    return best


def _date_selector_for(nodes: list[Tag]) -> str | None:
    """找出「日期单独待在那个节点里」时的选择器（比整行文本抠日期更稳）。"""
    counts: dict[str, int] = {}
    for node in nodes:
        for child in node.find_all(True):
            if str(child.name) == "a":
                continue
            text = normalize_text(child.get_text(" ", strip=True))
            if not _DATE_HINT.search(text or ""):
                continue
            # 只要这一层文本基本就是日期，别把整段正文当日期容器
            if len(text) > 32:
                continue
            sel = _tag_selector(child)
            if sel:
                counts[sel] = counts.get(sel, 0) + 1
    if not counts:
        return None
    best, hit = max(counts.items(), key=lambda kv: kv[1])
    return best if hit >= max(2, int(len(nodes) * 0.6)) else None


def _infer_url_regex(urls: list[str]) -> str | None:
    """从抓到的链接里反推「文章链接长什么样」，用来挡导航链接。

    只做一件事：把数字段替换成 \\d+，取出现最多的那个形状。
    推不出来就返回 None —— 宁可不过滤，也不要写一个把真文章也挡掉的规则。

    ⚠️ 必须传**绝对 URL**：适配器是拿 urljoin 之后的结果去匹配的。
    用原始 href（`../info/1031/8891.htm`）生成规则，正式抓取时
    `http://.../info/1031/8891.htm` 一条都匹配不上 —— 探测全绿、入库全空。
    """
    shapes: dict[str, int] = {}
    for u in urls:
        if not u:
            continue
        path = urlparse(u).path or ""
        if not re.search(r"\.(?:htm|html|shtml|php|aspx|jsp)$", path, re.I):
            continue
        shape = re.sub(r"\d+", r"\\d+", re.escape(path))
        shapes[shape] = shapes.get(shape, 0) + 1
    if not shapes:
        return None
    best, hit = max(shapes.items(), key=lambda kv: kv[1])
    # 至少 3 条同形状，且占全部链接的三成以上，才认为这是"文章链接的形状"
    if hit < 3 or hit < len(urls) * 0.3:
        return None
    return best


# 标题里"整批都带"的噪声：日期前缀、来源/详情后缀。
# 判定方式是「多数条目都命中」，不是「看到了就删」——只删一条会误伤真标题。
_LEAD_DATE = re.compile(
    r"^\s*(?:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"|20\d{2}\s+\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}\s+20\d{2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2})\s*"
)
_TAIL_NOISE = re.compile(
    r"\s*(?:来源[:：].*|阅读全文|阅读原文|查看详情|了解详情|了解更多|点击查看|详情|MORE|more)\s*$"
)
_TAIL_DATE = re.compile(r"\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\s*$")


def infer_title_cleanup(titles: list[str]) -> list[str]:
    """从一批标题里找出「一起出现」的噪声前后缀，生成 title_cleanup_regex。"""
    if len(titles) < 3:
        return []
    out: list[str] = []
    if sum(1 for t in titles if _LEAD_DATE.match(t)) >= max(2, len(titles) * 0.6):
        out.append(_LEAD_DATE.pattern)
    if sum(1 for t in titles if _TAIL_NOISE.search(t)) >= max(2, len(titles) * 0.6):
        out.append(_TAIL_NOISE.pattern)
    if sum(1 for t in titles if _TAIL_DATE.search(t)) >= max(2, len(titles) * 0.6):
        out.append(_TAIL_DATE.pattern)
    return out


def build_config(cand: Candidate, soup: BeautifulSoup, base_url: str) -> tuple[dict, list[dict]]:
    """由候选结构生成 html_list 配置，并**用正式解析器**算出预览条目。"""
    list_selector = _selector_for(cand, soup)
    date_sel = _date_selector_for(cand.nodes)

    fields: dict[str, str] = {"title": "a", "url": "a@href"}
    if date_sel:
        fields["date"] = date_sel

    cfg: dict = {
        "list_selector": list_selector,
        "fields": fields,
        "follow_detail": True,
        "detail": {"content_selector": list(DETAIL_SELECTORS)},
        "max_items": 20,
        "pagination": {"max_pages": 1},
    }

    # ---- 预览：直接调用正式适配器的解析函数 ----
    # 用同一个 _parse_node，是为了让「探测看到什么」＝「入库变成什么」。
    adapter = HtmlListAdapter()

    def parse(cleanup: list[str]) -> list[dict]:
        res: list[dict] = []
        for node in cand.nodes:
            item = adapter._parse_node(node, fields, base_url, cfg,
                                       adapter._compile_list(cleanup), None)
            if item is None or not item.is_usable:
                continue
            res.append({
                "title": item.title,
                "url": item.url or "",
                "date": item.published_at.isoformat() if item.published_at else None,
            })
        return res

    first_pass = parse([])
    # 标题里整批都带日期/来源时，先清洗再算一遍 —— 否则入库的标题是
    # 「21 2026-09 关于… 来源：」，卡片和 AI 摘要都会被这堆垃圾污染。
    cleanup = infer_title_cleanup([r["title"] for r in first_pass])
    if cleanup:
        cfg["title_cleanup_regex"] = cleanup
        items = parse(cleanup)
    else:
        items = first_pass

    regex = _infer_url_regex([r["url"] for r in items])
    if regex:
        cfg["item_url_regex"] = regex
    return cfg, items[:8]


def _page_title(soup: BeautifulSoup) -> str:
    for sel in ("h1", ".title", "#title", "title"):
        node = soup.select_one(sel)
        if node is not None:
            text = normalize_text(node.get_text(" ", strip=True))
            if text:
                return text[:80]
    return ""


def _looks_like_feed(raw: str) -> bool:
    head = raw[:400].lower()
    return ("<rss" in head) or ("<feed" in head) or ("<?xml" in head and "<channel" in raw[:2000].lower())


def _looks_like_article(soup: BeautifulSoup) -> tuple[bool, str | None]:
    """整页只有一篇东西时，判断它是不是文章页（返回命中的正文选择器）。"""
    for sel in DETAIL_SELECTORS:
        node = soup.select_one(sel)
        if node is None:
            continue
        text = normalize_text(node.get_text(" ", strip=True))
        if len(text) >= 200:
            return True, sel
    return False, None


def is_credible_list(cand: Candidate) -> bool:
    """这条候选够不够格当「通知列表」。

    只靠"≥3 个兄弟节点都带链接"太松 —— 详情页的侧边栏导航（综合办/工作处/培养处）
    正好 3 条、每条一个链接，会被当成列表抓进去。真实列表有两个特征：
    要么带日期，要么条数够多。两个都不满足就不是列表。
    """
    n = cand.item_count
    if n >= 4 and cand.date_ratio >= 0.5:
        return True
    return n >= 8


async def probe_url(url: str, ctx: FetchContext) -> dict:
    """探测一个网址。返回给前端的是**可直接展示的结论**，不要前端再猜。"""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "url": url, "message": "网址要以 http:// 或 https:// 开头"}

    try:
        html = await ctx.get_text(url)
    except Exception as e:  # noqa: BLE001
        # robots 拒绝 / DNS 失败 / 超时 / 4xx，都要如实告诉用户是哪一类。
        # 异常原文里常带换行和 httpx 的文档链接，压成一行再截断，否则弹窗里像报错堆栈。
        name = type(e).__name__
        detail = re.sub(r"\s+", " ", str(e)).strip()[:120]
        if "403" in detail or "401" in detail:
            hint = "这个地址需要登录或没有访问权限，知了不会绕过它。"
        elif "404" in detail:
            hint = "这个地址不存在（404），栏目的列表页可能换了位置。"
        elif name == "RobotsDenied":
            hint = "该网站的 robots.txt 不允许抓取这个地址，知了不会绕过它。"
        elif name == "FetchError":
            hint = "打不开这个地址，可能是网络不通、网址写错，或站点需要登录。"
        else:
            hint = "抓取失败。"
        return {"ok": False, "url": url, "error_type": name,
                "message": f"{hint}（{name}: {detail}）"}

    raw = html or ""
    if len(raw.strip()) < 200:
        return {"ok": False, "url": url, "message": "页面几乎是空的，可能被重定向到登录页或需要 JS 渲染。"}

    # ---- RSS / Atom ----
    if _looks_like_feed(raw):
        import feedparser

        feed = feedparser.parse(raw)
        entries = list(getattr(feed, "entries", []) or [])
        if entries:
            preview = [{
                "title": normalize_text(getattr(e, "title", "") or "")[:120],
                "url": getattr(e, "link", "") or "",
                "date": None,
            } for e in entries[:8]]
            return {
                "ok": True, "url": url, "adapter": "rss", "kind": "feed",
                "page_title": normalize_text(getattr(feed.feed, "title", "") or "")[:80],
                "item_count": len(entries), "items": preview,
                "config": {"max_items": 50},
                "message": f"识别为 RSS/Atom 订阅源，共 {len(entries)} 条。",
                "warnings": [],
            }
        return {"ok": False, "url": url, "message": "看起来是 RSS，但里面没有条目。"}

    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    page_title = _page_title(soup)
    base = url

    cands = [c for c in find_candidates(soup, base) if is_credible_list(c)]
    if cands:
        best = cands[0]
        cfg, preview = build_config(best, soup, base)
        # 预览条目太少说明选择器虽然命中了节点，但解析不出东西 —— 不许吹成成功
        if len(preview) >= 2:
            warnings: list[str] = []
            if best.date_ratio < 0.6:
                warnings.append("这个列表里多数条目没识别出日期，抓回来后发布日期可能为空。")
            if best.item_count < 5:
                warnings.append(f"只找到 {best.item_count} 条，可能是分页的其中一页或列表本身就短。")
            if best.avg_text_len > 90:
                warnings.append("列表项文字偏长，可能抓到了正文摘要而不是标题。")
            # 一页上有好几个差不多大的列表（典型是学校首页），说明这里不是栏目页
            rivals = [c for c in cands[1:] if c.score >= best.score * 0.8]
            if rivals:
                warnings.append(
                    f"这一页有 {len(rivals) + 1} 个大小相近的列表，可能选错。"
                    "建议换成具体栏目的「更多 / 列表」页面再探测一次。"
                )
            return {
                "ok": True, "url": url, "adapter": "html_list", "kind": "list",
                "page_title": page_title,
                "item_count": best.item_count,
                "items": preview,
                "config": cfg,
                "message": f"识别为通知列表，找到 {best.item_count} 条，"
                           f"其中约 {round(best.date_ratio * 100)}% 能识别出日期。",
                "warnings": warnings,
            }

    # ---- 单篇文章 ----
    is_article, content_sel = _looks_like_article(soup)
    if is_article:
        return {
            "ok": True, "url": url, "adapter": "article_url", "kind": "article",
            "page_title": page_title,
            "item_count": 1,
            "items": [{"title": page_title or "（无标题）", "url": url, "date": None}],
            "config": {"content_selector": content_sel} if content_sel else {},
            "message": "识别为单篇文章页（不是列表），可加入「单篇链接」来源。",
            "warnings": ["单篇文章只能抓到这一篇，不会自动跟进后续新文章。"],
        }

    return {
        "ok": False, "url": url, "page_title": page_title,
        "message": "没能从这一页认出通知列表。可能是：① 列表由 JS 动态加载；"
                   "② 这是首页而不是栏目列表页；③ 需要登录。"
                   "可以换成栏目的「更多/列表」页面再试一次。",
        "hint_url": urljoin(url, ""),
    }
