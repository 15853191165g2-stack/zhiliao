"""ORM 模型。

三张表：sources（数据源）/ notifications（通知）/ fetch_logs（抓取日志）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.timeutil import now_utc


class Base(DeclarativeBase):
    pass


class Source(Base):
    """一个数据源 = 一份 yaml 配置。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    school: Mapped[str] = mapped_column(String(120), default="", index=True)
    adapter: Mapped[str] = mapped_column(String(50), nullable=False)
    url: Mapped[str] = mapped_column(Text, default="")
    category_hint: Mapped[str] = mapped_column(String(30), default="其他")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("name", "url", name="uq_source_name_url"),)


class Notification(Base):
    """一条通知。AI 产物直接落在同一行，读取详情时零额外查询。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # 去重主键
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # 原文
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI 产物（结构见 schemas.AISummary）
    summary_lines: Mapped[list | None] = mapped_column(JSON, nullable=True)
    key_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    importance: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    category: Mapped[str] = mapped_column(String(30), default="其他", index=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    ai_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    ai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ai_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 用户状态
    # is_pinned：用户手动置顶。列表排序时置顶的永远排在最前（见 notifications 路由）。
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # ---- 推送 ----
    # notified_at：这条通知**已经推送出去**的时间。NULL = 还没推。
    # 它是推送去重的唯一依据：内容后续被更新时**不会**重置它，
    # 所以同一条通知不会被推第二遍（改需求时注意，见 services/push.py 顶部说明）。
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # 最近一次推送失败原因。外发通道（webhook）失败时记录在这里，
    # 但不影响 notified_at —— 本地队列该收到还是要收到。
    notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)

    source: Mapped[Source | None] = relationship(back_populates="notifications")

    __table_args__ = (
        Index("ix_notif_pub_desc", published_at.desc()),
        Index("ix_notif_source_pub", "source_id", "published_at"),
    )


class FetchLog(Base):
    """每次抓取的审计记录。合规与排障都靠它。"""

    __tablename__ = "fetch_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_name: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PushLog(Base):
    """每次「把一条通知送进某个通道」的审计记录。

    为什么逐条记而不是只记个汇总：推送是个**会失败的外部动作**，
    出问题时必须能回答「这条到底推没推出去、走的哪个通道、报了什么错」。
    只留一个计数器的话，用户说「我没收到」时无从查起。
    """

    __tablename__ = "push_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notification_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dedup_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel: Mapped[str] = mapped_column(String(40), default="", index=True)
    # ok / failed / skipped
    status: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class AppMeta(Base):
    """极简键值表，用来存「跨重启要保持的小状态」。

    目前只用于记录推送基线时间（第一次启用推送时把历史积压挡在窗口外，
    否则一开推送就会把库里几百条旧通知一次性轰出去）。
    刻意不做成通用配置中心 —— 配置走 .env，这里只放运行期状态。
    """

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)
