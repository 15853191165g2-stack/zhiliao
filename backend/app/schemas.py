"""Pydantic 模型：API 出入参 + AI 固定输出契约。

★ `AISummary` 是全项目最硬的契约：AI 必须产出这个结构，落库前一定过校验。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.timeutil import to_local_iso

T = TypeVar("T")

Importance = Literal["urgent", "important", "normal"]
Category = Literal["教务", "考试", "奖助学金", "活动", "讲座", "就业", "后勤", "其他"]

CATEGORIES: tuple[str, ...] = ("教务", "考试", "奖助学金", "活动", "讲座", "就业", "后勤", "其他")
IMPORTANCE_LABELS = {"urgent": "紧急", "important": "重要", "normal": "一般"}


# --------------------------------------------------------------------------- #
# 统一响应包
# --------------------------------------------------------------------------- #
class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(cls, message: str, code: int = 1) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=None)


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


# --------------------------------------------------------------------------- #
# ★ AI 固定输出结构
# --------------------------------------------------------------------------- #
class KeyInfo(BaseModel):
    """从通知里抽取的关键信息。抽不到就 null，**不要编**。"""

    deadline: str | None = Field(None, description="截止时间，原文表述，如 2026-10-15 17:00")
    deadline_ts: datetime | None = Field(None, description="解析后的截止时间（UTC）")
    audience: str | None = Field(None, description="适用对象，如 2024级全体本科生")
    location: str | None = Field(None, description="地点")
    materials: list[str] = Field(default_factory=list, description="需要提交的材料")
    contact: str | None = Field(None, description="联系人 / 电话 / 邮箱")
    fee: str | None = Field(None, description="费用，如有")
    extra: dict[str, Any] = Field(default_factory=dict)


class AISummary(BaseModel):
    """AI 必须严格产出的 JSON。字段非法时降级而不是报错。"""

    summary_lines: list[str] = Field(default_factory=list, description="3 行摘要")
    key_info: KeyInfo = Field(default_factory=KeyInfo)
    importance: Importance = "normal"
    category: Category = "其他"
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    @field_validator("summary_lines", mode="before")
    @classmethod
    def _fix_lines(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [s for s in v.split("\n") if s.strip()]
        if not isinstance(v, list):
            return []
        lines = [str(x).strip().lstrip("-•* ").strip() for x in v]
        lines = [x for x in lines if x]
        return lines[:3]

    @field_validator("importance", mode="before")
    @classmethod
    def _fix_importance(cls, v: Any) -> str:
        s = str(v or "").strip().lower()
        mapping = {
            "urgent": "urgent", "紧急": "urgent", "高": "urgent", "high": "urgent",
            "important": "important", "重要": "important", "中": "important", "medium": "important",
            "normal": "normal", "一般": "normal", "低": "normal", "low": "normal",
        }
        return mapping.get(s, "normal")

    @field_validator("category", mode="before")
    @classmethod
    def _fix_category(cls, v: Any) -> str:
        s = str(v or "").strip()
        if s in CATEGORIES:
            return s
        for c in CATEGORIES:                       # 容错：「教务通知」→「教务」
            if c in s:
                return c
        return "其他"

    @field_validator("tags", mode="before")
    @classmethod
    def _fix_tags(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:8]

    @field_validator("confidence", mode="before")
    @classmethod
    def _fix_conf(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))

    @field_serializer("key_info")
    def _ser_key_info(self, v: KeyInfo) -> dict:  # pragma: no cover - 序列化辅助
        d = v.model_dump()
        d["deadline_ts"] = to_local_iso(v.deadline_ts)
        return d


# --------------------------------------------------------------------------- #
# 来源
# --------------------------------------------------------------------------- #
class SourceBase(BaseModel):
    name: str
    school: str = ""
    adapter: str = "html_list"
    url: str = ""
    category_hint: str = "其他"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class SourceCreate(SourceBase):
    pass


class SourceProbeIn(BaseModel):
    """探测一个网址：只填网址，其余交给后端猜。"""

    url: str


class SourceUpdate(BaseModel):
    name: str | None = None
    school: str | None = None
    adapter: str | None = None
    url: str | None = None
    category_hint: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class SourceOut(SourceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    last_fetched_at: datetime | None = None
    last_status: str = "pending"
    last_error: str | None = None
    item_count: int = 0

    @field_serializer("last_fetched_at")
    def _ser(self, v: datetime | None) -> str | None:
        return to_local_iso(v)


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int | None = None
    source_name: str | None = None
    school: str | None = None
    title: str
    url: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    excerpt: str | None = None
    summary_lines: list[str] | None = None
    key_info: dict[str, Any] | None = None
    importance: str = "normal"
    importance_label: str = "一般"
    category: str = "其他"
    tags: list[str] | None = None
    ai_status: str = "pending"
    is_read: bool = False
    is_starred: bool = False
    is_pinned: bool = False

    @field_serializer("published_at")
    def _ser_pub(self, v: datetime | None) -> str | None:
        return to_local_iso(v)


class NotificationDetail(NotificationOut):
    content_html: str | None = None
    content_text: str | None = None
    ai_model: str | None = None
    ai_error: str | None = None


class NotificationFlags(BaseModel):
    """局部更新用户状态。字段为 None = 不改这一项。"""

    is_read: bool | None = None
    is_starred: bool | None = None
    is_pinned: bool | None = None


class NotificationBulkFlags(BaseModel):
    """批量改已读。ids 为空 = 对当前筛选范围全量生效（由路由决定范围）。"""

    ids: list[int] = Field(default_factory=list, max_length=500)
    is_read: bool = True


# --------------------------------------------------------------------------- #
# 简报 / 对话
# --------------------------------------------------------------------------- #
class BriefSection(BaseModel):
    title: str
    items: list[str] = Field(default_factory=list)
    notification_ids: list[int] = Field(default_factory=list)


class BriefOut(BaseModel):
    date: str
    headline: str = ""
    greeting: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    sections: list[BriefSection] = Field(default_factory=list)
    speech_text: str = ""
    generated_by: str = "local"


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    days: int = Field(30, ge=1, le=365, description="检索最近多少天的通知")
    top_k: int = Field(12, ge=1, le=50)
    history: list[dict[str, str]] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    citations: list[NotificationOut] = Field(default_factory=list)
    generated_by: str = "local"
    # 这次回答实际用的哪份配置。界面上要显示出来 —— 「我以为在用 A，其实在跑 B」
    # 是接自定义模型后最容易出现的困惑，唯一解法是每次都把名字亮出来。
    model_name: str = ""
    model_source: str = "local"   # env | profile | local


# --------------------------------------------------------------------------- #
# 自定义大模型（AI 问答页的「模型」选择器）
# --------------------------------------------------------------------------- #
class LLMProfileIn(BaseModel):
    """新建一份自定义模型配置。

    ⚠️ `api_key` 允许传打码值（含 `****`）—— 表示「这次没改 key」，后端保留原值。
       前端编辑别的字段时不需要把明文 key 再拉回来一次，也就不会有明文 key
       在界面上多停留一秒。
    """

    name: str = Field(..., min_length=1, max_length=60)
    base_url: str = Field("", max_length=500)
    model: str = Field("", max_length=120)
    api_key: str = Field("", max_length=500)
    thinking: bool = False
    json_mode: bool = True


class LLMProfileUpdate(BaseModel):
    """部分更新：只传想改的字段。"""

    name: str | None = Field(None, min_length=1, max_length=60)
    base_url: str | None = Field(None, max_length=500)
    model: str | None = Field(None, max_length=120)
    api_key: str | None = Field(None, max_length=500)
    thinking: bool | None = None
    json_mode: bool | None = None


class LLMProfileOut(BaseModel):
    id: int
    name: str
    base_url: str
    model: str
    # 只回打码后的 key，永远不回明文
    api_key_masked: str = ""
    has_key: bool = False
    thinking: bool = False
    json_mode: bool = True
    is_active: bool = False
    # 最终真正会请求的地址。拼错了在这里就能看出来，不用去翻日志。
    endpoint: str = ""
    last_test_ok: bool | None = None
    last_test_at: datetime | None = None
    last_test_error: str | None = None


class LLMEnvOut(BaseModel):
    """内置默认（.env 那套）。它不是数据库行，但界面上要和自定义项并列显示。"""

    name: str = "默认"
    model: str = ""
    base_url: str = ""
    endpoint: str = ""
    api_key_masked: str = ""
    enabled: bool = False
    is_active: bool = False


class LLMListOut(BaseModel):
    items: list[LLMProfileOut] = Field(default_factory=list)
    env: LLMEnvOut = Field(default_factory=LLMEnvOut)
    active_id: int | None = None
    effective: str = ""
    effective_model: str = ""
    effective_source: str = "env"


class LLMTestIn(BaseModel):
    """测试连通性。可以不保存就测 —— 先试通再存，避免列表里堆一堆错的。

    编辑已有配置时带上 `profile_id`：key 框里显示的是打码值，后端会自动取库里的
    原文，所以「只改地址、没动 key」也能测。thinking / json_mode 用 None 表示
    「沿用库里那份」，传 bool 才算显式指定。
    """

    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=500)
    model: str = Field("", max_length=120)
    thinking: bool | None = None
    json_mode: bool | None = None
    profile_id: int | None = None


class LLMTestOut(BaseModel):
    ok: bool = False
    detail: str = ""
    endpoint: str = ""
    model: str = ""
    reply: str = ""
    duration_ms: float = 0.0


class LLMModelsIn(BaseModel):
    """问服务商要模型清单。同样支持「还没保存就先看有哪些模型」。

    编辑已有配置时传 `profile_id` + 打码 key，后端取库里原文。
    """

    base_url: str = Field("", max_length=500)
    api_key: str = Field("", max_length=500)
    profile_id: int | None = None


class LLMModelsOut(BaseModel):
    ok: bool = False
    items: list[str] = Field(default_factory=list)
    endpoint: str = ""
    detail: str = ""


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
class IngestResult(BaseModel):
    total_items: int = 0
    new_items: int = 0
    updated_items: int = 0
    summarized: int = 0
    skipped_by_robots: int = 0
    # 空壳保护命中次数：本次抓到的正文为空、且库里已有正文，于是没有覆盖。
    # 这个数字不为 0 通常意味着某个详情页抓取不稳定，值得看一眼。
    empty_kept: int = 0
    # 详情页抓取失败的条数（列表字段仍然入库了）
    detail_failures: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: float = 0.0
    # 阶段 3：本次抓取顺带推送的结果（没开推送时为 None）
    push: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 推送（阶段 3）
# --------------------------------------------------------------------------- #
class PushAckRequest(BaseModel):
    """客户端确认「这些通知我已经弹过系统通知了」。

    为什么要有这一步：`/push/pending` 只读不写。
    客户端拉到了但弹通知失败（权限被拒、进程被杀），
    如果拉取时就标记已推送，这条消息就**永久丢了**。
    所以标记动作必须由客户端显式确认。
    """

    ids: list[int] = Field(default_factory=list, max_length=500)


class PushTestRequest(BaseModel):
    text: str = "知了推送通道自检"


class PushRunRequest(BaseModel):
    """手动触发推送。

    force=true 会绕过「首次静默基线」和「推送时间窗」，
    用于「我想把库里的东西全推一遍试试」这类需求。
    """

    force: bool = False
    limit: int = Field(default=20, ge=1, le=200)
