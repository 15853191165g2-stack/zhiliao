"""数据库引擎与 Session。

开发用 SQLite，生产用 PostgreSQL，只差 `DATABASE_URL` 一个环境变量。
所有 JSON 字段用通用 JSON 类型：SQLite 落 TEXT，PG 落 JSONB。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # SQLite 在多线程/异步下需要放开线程检查
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """建表（阶段 1 用 create_all；后续接入 Alembic 做迁移）。

    ⚠️ `create_all` 只建**不存在**的表。给一张已经存在的表加新列，它什么都不会做。
    我们已经有用户在跑、库里躺着真实数据，所以这里补一个极轻量的「加列」迁移，
    否则升级后一查 is_pinned 就报 `no such column`。
    （正经做法是 Alembic；等表结构开始变复杂再换，现在保持零依赖。）
    """
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_extra_columns)


# 升级时新增的列 / 索引：{表名: [(列名, 建列 DDL), ...]}
# 只做「只增不改」——加列、加索引。删列/改类型不在这里做，风险太大。
#
# ⚠️ DDL 里用 `{ts}` 占位时间类型：SQLite 要 `DATETIME`，PostgreSQL 要 `TIMESTAMP`。
#    直接写死一个，换库那天就会在启动时报语法错误 —— 而且是在 init_db 里报，
#    表现为「服务起不来」，很难第一眼联想到是迁移 DDL。
_EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "notifications": [
        ("is_pinned", "BOOLEAN NOT NULL DEFAULT 0"),
        # 阶段 3：推送去重依据。已有数据全部补 NULL = 「还没推过」，
        # 但真正会不会被推还要过 services/push.py 的时间窗判断，
        # 所以升级后不会把历史积压一次性轰出去。
        ("notified_at", "{ts} NULL"),
        ("notify_error", "TEXT NULL"),
    ],
}

_EXTRA_INDEXES: list[tuple[str, str, str]] = [
    # (索引名, 表名, 列名)
    ("ix_notifications_is_pinned", "notifications", "is_pinned"),
    ("ix_notifications_notified_at", "notifications", "notified_at"),
]


def _timestamp_type(conn) -> str:
    """按方言给出时间列类型。"""
    name = getattr(getattr(conn, "dialect", None), "name", "sqlite")
    return "TIMESTAMP" if name in ("postgresql", "postgres") else "DATETIME"


def _ensure_extra_columns(conn) -> None:
    """给已存在的表补上新加的列与索引（幂等）。"""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    tables = set(insp.get_table_names())
    ts = _timestamp_type(conn)

    for table, columns in _EXTRA_COLUMNS.items():
        if table not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name in have:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.format(ts=ts)}"))

    for idx, table, column in _EXTRA_INDEXES:
        if table not in tables:
            continue
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} ({column})"))


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个 Session。"""
    async with SessionLocal() as session:
        yield session
