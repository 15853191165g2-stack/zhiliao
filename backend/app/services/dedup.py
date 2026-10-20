"""去重与内容变更检测。

规则（对应 docs/01-architecture.md 第 4 节）：
  1. dedup_key 由「规范化 URL」决定；无 URL 时退化为「标题 + 日期」
  2. 规范化会剥掉 utm_* / from / spm 等跟踪参数，避免同一篇文章多个 key
  3. content_hash 变化 = 原文被编辑过 → 重新跑 AI 摘要
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.core.timeutil import normalize_text

# 常见跟踪参数（含微信/微博/百度等）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "from_source", "source", "spm", "spm_id_from", "share_source",
    "share_medium", "sharer", "scene", "clicktime", "enterid", "wxshare",
    "tdsourcetag", "sjsd", "fbclid", "gclid", "yclid", "vd_source",
    "_t", "timestamp", "rand", "t",
}

# 微信文章链接里必须保留的参数，去掉会 404
KEEP_PARAMS_ALWAYS = {"__biz", "mid", "idx", "sn", "chksm"}


def canonical_url(url: str | None) -> str:
    """URL 归一化：排序查询参数、剔除跟踪参数、去 fragment、统一 host 大小写。"""
    if not url:
        return ""
    url = url.strip()
    try:
        p = urlparse(url)
    except ValueError:
        return url

    host = (p.netloc or "").lower()
    path = p.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS or k in KEEP_PARAMS_ALWAYS
    ]
    query = urlencode(sorted(kept))

    return urlunparse((p.scheme.lower(), host, path, "", query, ""))


def make_dedup_key(
    url: str | None,
    title: str = "",
    published_at: datetime | None = None,
) -> str:
    """生成去重键。优先 URL，其次标题+日期。"""
    canon = canonical_url(url)
    if canon:
        basis = canon
    else:
        norm_title = re.sub(r"[\s\u3000\W_]+", "", normalize_text(title)).lower()
        if published_at is not None:
            basis = f"{norm_title}|{published_at.strftime('%Y-%m-%d')}"
        else:
            # 用标题的字符集合做粗粒度兜底，避免完全无法去重
            basis = norm_title
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def content_hash(text: str | None) -> str:
    """正文指纹，用于检测原文被悄悄改过。"""
    norm = re.sub(r"\s+", "", normalize_text(text))
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# 正文里出现这些标签，说明「没文字」不等于「没内容」（纯图片海报页）
_MEDIA_IN_HTML = re.compile(r"<(img|table|video|iframe|embed|audio)\b", re.I)

# 纯文本短于这个长度、且没有图片/表格，就判定为「空壳」
EMPTY_TEXT_THRESHOLD = 20


def is_shell_content(html: str | None, text: str | None) -> bool:
    """判断这次抓到的正文是不是「空壳」。

    为什么必须有这个判定：详情页请求失败、站点改版、或页面本身就是空的时候，
    适配器可能返回 `""` 或一个只剩外包装的 `<div>`。如果入库时无脑覆盖，
    一次网络抖动就会**永久清掉库里已有的正文**，还会被误记成「原文被修改」。
    """
    if _MEDIA_IN_HTML.search(html or ""):
        return False
    return len(re.sub(r"\s+", "", text or "")) < EMPTY_TEXT_THRESHOLD


def structural_tags(html: str | None, text: str | None) -> list[str]:
    """给「正文不能直接读」的通知打上结构标记，供 App 提示用户。

    大工实测到的两种情况，都不是抓取失败，但都影响 AI 摘要质量：
      - 纯图片通知：正文全在 <img> 里（迎新网院系风采、学生处值班通知）
        → 文本是空的，AI 只能靠标题；App 应该提示「点击看原图」
      - 源站正文本来就是空的：详情页里只有 <p></p>（研究生院、后勤处各有一条）
        → 连图片都没有，App 应该提示「原文无正文」
    """
    html = html or ""
    body = re.sub(r"\s+", "", text or "")
    has_media = bool(_MEDIA_IN_HTML.search(html))
    if not html and not body:
        return ["正文缺失"]
    if len(body) < EMPTY_TEXT_THRESHOLD:
        if has_media:
            return ["图片通知"]
        return ["正文为空"]
    return []


def title_similarity(a: str, b: str) -> float:
    """标题 Jaccard 相似度（字符 2-gram），供阶段 2 的近似去重使用。"""

    def grams(s: str) -> set[str]:
        s = re.sub(r"[\s\u3000\W_]+", "", s)
        return {s[i : i + 2] for i in range(max(0, len(s) - 1))} or ({s} if s else set())

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def make_excerpt(text: str | None, limit: int = 120) -> str | None:
    norm = normalize_text(text)
    if not norm:
        return None
    return norm[:limit] + ("…" if len(norm) > limit else "")
