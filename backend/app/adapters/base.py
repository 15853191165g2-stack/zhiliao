"""适配器协议定义。

**这是「新增一所学校只需写一份配置」能成立的根基**：
适配器只负责把来源页面 → 标准 `RawItem` 列表，不碰数据库、不碰 AI。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from app.core.config import BASE_DIR
from app.core.timeutil import normalize_text

if TYPE_CHECKING:  # 只用于类型标注，避免运行期循环导入
    from app.core.config import Settings
    from app.services.fetch import Fetcher


@dataclass(slots=True)
class SourceSpec:
    """一条来源配置（来自 yaml 或数据库）。"""

    name: str
    adapter: str
    url: str = ""
    school: str = ""
    category_hint: str = "其他"
    config: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceSpec":
        cfg = dict(raw)
        return cls(
            id=cfg.pop("id", None),
            name=str(cfg.pop("name", "")).strip(),
            adapter=str(cfg.pop("adapter", "html_list")).strip(),
            url=str(cfg.pop("url", "") or "").strip(),
            school=str(cfg.pop("school", "") or "").strip(),
            category_hint=str(cfg.pop("category_hint", "其他") or "其他").strip(),
            config=cfg.pop("config", {}) or {},
        )

    @classmethod
    def from_orm(cls, row: Any) -> "SourceSpec":
        return cls(
            id=row.id,
            name=row.name,
            adapter=row.adapter,
            url=row.url or "",
            school=row.school or "",
            category_hint=row.category_hint or "其他",
            config=row.config or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "school": self.school,
            "adapter": self.adapter,
            "url": self.url,
            "category_hint": self.category_hint,
            "config": self.config,
        }


@dataclass(slots=True)
class RawItem:
    """统一抓取产物。所有适配器的出口都是它。"""

    title: str
    url: str | None = None
    published_at: datetime | None = None       # naive UTC
    author: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    category: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def clean(self) -> "RawItem":
        self.title = normalize_text(self.title)
        if self.content_text:
            self.content_text = normalize_text(self.content_text)
        if self.author:
            self.author = normalize_text(self.author)
        return self

    @property
    def is_usable(self) -> bool:
        return bool(self.title and len(self.title) >= 4)


class AdapterError(RuntimeError):
    """适配器内部错误（配置写错、页面结构变了等）。"""


def read_local_text(raw_path: str, base_dir: Path | None = None) -> str:
    """读本地 HTML/XML 文件（离线开发与回归测试用）。

    相对路径优先相对 `base_dir`（一般是列表页所在目录）解析，
    这样详情页链接 `detail1.html` 能直接对上。
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = (base_dir / raw_path) if base_dir else (BASE_DIR / raw_path)
    if not p.exists():
        raise AdapterError(f"本地样例文件不存在：{p}")
    data = p.read_bytes()
    for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@dataclass
class FetchContext:
    """注入给适配器的运行环境。

    适配器不自己创建 HTTP 客户端，一律通过它取数据——
    这样 robots 校验、频控、重试、UA 就统一在一处，不会有漏网的请求。
    """

    fetcher: "Fetcher"
    settings: "Settings"

    async def get_text(
        self, url: str, *, encoding: str | None = None, ignore_robots: bool = False
    ) -> str:
        return await self.fetcher.get_text(url, encoding=encoding, ignore_robots=ignore_robots)

    async def get_bytes(self, url: str) -> bytes:
        return await self.fetcher.get_bytes(url)


@runtime_checkable
class SourceAdapter(Protocol):
    """适配器接口。实现它 + 用 @register 注册即可被配置引用。"""

    kind: ClassVar[str]

    async def fetch(self, spec: SourceSpec, ctx: FetchContext) -> list[RawItem]:
        ...
