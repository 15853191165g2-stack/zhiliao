"""合规抓取器。

三件事只在这一层做，保证没有任何请求能绕过去：
  1. robots.txt 校验（默认开启，命中 Disallow 直接跳过并记录）
  2. 同域名最小请求间隔 + 全局并发上限
  3. 统一 UA（带联系方式）+ 失败指数退避重试
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """网络层失败。"""


class RobotsDenied(FetchError):
    """被目标站点的 robots.txt 明确禁止。"""


# --------------------------------------------------------------------------- #
# 编码识别：国内学校站点 gb2312/gbk 很常见，不能只赌 utf-8
# --------------------------------------------------------------------------- #
def decode_bytes(data: bytes, declared: str | None = None) -> str:
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    # 从 <meta charset> 里捞
    head = data[:2048].lower()
    for marker in (b'charset="', b"charset='", b"charset="):
        idx = head.find(marker)
        if idx != -1:
            tail = head[idx + len(marker) : idx + len(marker) + 24]
            name = tail.split(b'"')[0].split(b"'")[0].split(b">")[0].split(b";")[0]
            try:
                candidates.append(name.decode("ascii").strip())
            except UnicodeDecodeError:
                pass
            break
    candidates += ["utf-8", "gb18030", "gbk", "big5", "latin-1"]

    seen: set[str] = set()
    for enc in candidates:
        enc = (enc or "").strip().lower()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 频控
# --------------------------------------------------------------------------- #
class Throttler:
    """同域名串行 + 最小间隔。防止把人家站点打出问题。"""

    def __init__(self, min_interval: float) -> None:
        self._min = max(0.0, min_interval)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            delta = self._min - (now - self._last.get(host, 0.0))
            if delta > 0:
                await asyncio.sleep(delta)
            self._last[host] = time.monotonic()


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #
class RobotsCache:
    def __init__(self, ttl: float = 86400.0) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}

    async def allowed(self, url: str, user_agent: str, client: httpx.AsyncClient, verify: bool) -> bool:
        if not verify:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return True  # 本地文件等非网络地址，不适用

        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = await self._get_parser(origin, client)
        if parser is None:
            return True  # 拿不到 robots.txt（404/超时）按通行处理

        ua_token = (user_agent or "*").split("/")[0].strip() or "*"
        try:
            allowed = parser.can_fetch(ua_token, url) or parser.can_fetch("*", url)
        except Exception:  # noqa: BLE001
            return True
        if not allowed:
            logger.warning("robots.txt 禁止抓取，已跳过：%s", url)
        return allowed

    async def _get_parser(
        self, origin: str, client: httpx.AsyncClient
    ) -> urllib.robotparser.RobotFileParser | None:
        now = time.monotonic()
        cached = self._cache.get(origin)
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            resp = await client.get(f"{origin}/robots.txt")
            if resp.status_code == 200 and resp.content:
                text = decode_bytes(resp.content, resp.encoding)
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
        except Exception as e:  # noqa: BLE001
            logger.debug("获取 robots.txt 失败（按通行处理）：%s / %s", origin, e)
            parser = None

        self._cache[origin] = (now, parser)
        return parser


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #
class Fetcher:
    """所有出网请求的唯一入口。用完记得 aclose()。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._robots = RobotsCache()
        self._throttler = Throttler(settings.fetch_min_interval)
        self._sem = asyncio.Semaphore(max(1, settings.fetch_concurrency))
        self.stats = {
            "requests": 0,
            "blocked_by_robots": 0,
            "robots_overridden": 0,
            "errors": 0,
        }

    # ---- 生命周期 ----
    async def start(self) -> "Fetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.fetch_timeout),
                follow_redirects=True,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                },
            )
        return self

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "Fetcher":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---- 取数据 ----
    async def get_bytes(self, url: str, *, params: dict | None = None) -> bytes:
        resp = await self._request(url, params=params)
        return resp.content

    async def get_text(
        self, url: str, *, encoding: str | None = None, ignore_robots: bool = False
    ) -> str:
        resp = await self._request(url, ignore_robots=ignore_robots)
        return decode_bytes(resp.content, encoding or resp.encoding)

    async def get_response(self, url: str, *, params: dict | None = None) -> httpx.Response:
        return await self._request(url, params=params)

    async def _request(
        self, url: str, *, params: dict | None = None, ignore_robots: bool = False
    ) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None

        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            if ignore_robots:
                # 白名单式放行：**只有调用方显式传 true 才会走到这里**。
                # 目前唯一的用途是「用户手动粘贴的单篇文章链接」
                # （例如 mp.weixin.qq.com 的 robots.txt 是 Disallow: /，
                #  不显式放行就永远拿不到正文）。
                # 每次放行都计数，方便在运行日志里看到到底绕过了多少次。
                if self.settings.respect_robots:
                    self.stats["robots_overridden"] += 1
            else:
                ok = await self._robots.allowed(
                    url, self.settings.user_agent, self._client, self.settings.respect_robots
                )
                if not ok:
                    self.stats["blocked_by_robots"] += 1
                    raise RobotsDenied(f"robots.txt 禁止抓取：{url}")
            await self._throttler.wait(parsed.netloc)

        last_err: Exception | None = None
        async with self._sem:
            for attempt in range(self.settings.fetch_retries + 1):
                try:
                    self.stats["requests"] += 1
                    resp = await self._client.get(url, params=params)
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise FetchError(f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    return resp
                except (httpx.HTTPError, FetchError) as e:
                    last_err = e
                    if attempt < self.settings.fetch_retries:
                        await asyncio.sleep(2 ** attempt)
        self.stats["errors"] += 1
        raise FetchError(f"请求失败 {url}：{last_err}")
