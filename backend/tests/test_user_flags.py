"""用户状态（已读 / 收藏 / 置顶）的接口级测试。

这几个状态是「用户自己标出来的」，跟抓取、AI 都无关，
可以在临时库上跑，不需要联网。

守住三件容易被改坏的事：
  1. 置顶必须排在列表最前 —— 这是置顶功能唯一的可观察效果；
  2. 勾选框要能「取消已读」，不能只单向；
  3. 老库升级时要能自动补上 is_pinned 列 —— `create_all` 不会给已有表加列，
     用户库里已经躺着真实数据，迁移写错就是一升级就 `no such column`。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

# ⚠️ 环境变量（DATABASE_URL / ENABLE_SCHEDULER / AI_API_KEY）统一由 tests/conftest.py 设置。
#    这里**不要**再设一遍 —— `app.db` 在模块级就绑定了引擎，各文件各设一份会导致
#    「造数据写进库 B、接口从库 A 读」这种极难排查的错乱。详见 conftest.py 的说明。

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.main import app  # noqa: E402
from app.models import Notification, Source  # noqa: E402

_counter = 0


def seed_batch(titles: tuple[str, str, str] = ("最旧的一条", "中间的一条", "最新的一条")):
    """独立造一批数据（自己的 source + 3 条通知，时间递增）。

    用「一个测试一个 source」来隔离：所有断言都带 source_id 过滤，
    这样多个测试共用同一个库也不会互相干扰。

    ⚠️ dedup_key 是 UNIQUE 的，重复造数据必须换 key。
    ⚠️ 用独立的 engine 并在同一个 loop 里 dispose，避免跨事件循环复用连接池。
    """
    global _counter
    _counter += 1
    batch = _counter

    async def run():
        eng = create_async_engine(os.environ["DATABASE_URL"])
        sm = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with sm() as session:
                src = Source(name=f"测试来源{batch}", school="测试大学",
                             adapter="html_list", url=f"http://example.invalid/{batch}")
                session.add(src)
                await session.flush()

                rows = []
                for i, title in enumerate(titles):
                    rows.append(Notification(
                        source_id=src.id,
                        dedup_key=f"batch{batch}-{i}",
                        title=title,
                        published_at=datetime(2026, 1 + i * 4, 1, 12, 0, 0),
                        excerpt=f"{title} 的原文摘要",
                        summary_lines=[f"{title} 第1行", "第2行", "第3行"],
                    ))
                session.add_all(rows)
                await session.commit()
                return src.id, [r.id for r in rows]
        finally:
            await eng.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _list(client, **params) -> list[dict]:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/notifications?page_size=100&{qs}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    return body["data"]["items"]


def test_default_order_is_newest_first(client):
    sid, _ = seed_batch()
    titles = [i["title"] for i in _list(client, source_id=sid)]
    assert titles == ["最新的一条", "中间的一条", "最旧的一条"], titles


def test_pin_jumps_to_top_and_toggles_back(client):
    sid, ids = seed_batch()
    old_id = ids[0]                       # 最旧的一条

    r = client.post(f"/api/notifications/{old_id}/pin", json={})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["is_pinned"] is True

    titles = [i["title"] for i in _list(client, source_id=sid)]
    assert titles[0] == "最旧的一条", titles
    # 置顶不改变发布时间本身，只是排序变了
    assert _list(client, source_id=sid)[0]["published_at"].startswith("2026-01-01")

    # 再点一次 = 取消置顶
    r = client.post(f"/api/notifications/{old_id}/pin", json={})
    assert r.json()["data"]["is_pinned"] is False
    titles = [i["title"] for i in _list(client, source_id=sid)]
    assert titles[0] == "最新的一条", titles


def test_pin_explicit_value_is_not_treated_as_toggle(client):
    """显式传 is_pinned=true 两次，第二次不能被「取反」成取消。"""
    _, ids = seed_batch()
    nid = ids[1]
    assert client.post(f"/api/notifications/{nid}/pin", json={"is_pinned": True}
                       ).json()["data"]["is_pinned"] is True
    assert client.post(f"/api/notifications/{nid}/pin", json={"is_pinned": True}
                       ).json()["data"]["is_pinned"] is True
    assert client.post(f"/api/notifications/{nid}/pin", json={"is_pinned": False}
                       ).json()["data"]["is_pinned"] is False


def test_pinned_only_filter(client):
    sid, ids = seed_batch()
    client.post(f"/api/notifications/{ids[0]}/pin", json={"is_pinned": True})
    client.post(f"/api/notifications/{ids[1]}/pin", json={"is_pinned": True})

    rows = _list(client, source_id=sid, pinned_only="true")
    assert len(rows) == 2, [i["title"] for i in rows]
    assert all(i["is_pinned"] for i in rows)


def test_read_checkbox_can_be_checked_and_unchecked(client):
    sid, ids = seed_batch()
    nid = ids[0]

    assert client.get(f"/api/notifications/{nid}").json()["data"]["is_read"] is False

    assert client.post(f"/api/notifications/{nid}/read", json={"is_read": True}
                       ).json()["data"]["is_read"] is True
    # ★ 反勾选：勾选框必须能取消，不能只单向
    assert client.post(f"/api/notifications/{nid}/read", json={"is_read": False}
                       ).json()["data"]["is_read"] is False

    # 不传 body 时的老语义仍是「标为已读」
    assert client.post(f"/api/notifications/{nid}/read").json()["data"]["is_read"] is True


def test_unread_and_read_filters_are_exclusive(client):
    sid, ids = seed_batch()
    client.post(f"/api/notifications/{ids[0]}/read", json={"is_read": True})

    unread = _list(client, source_id=sid, unread_only="true")
    read = _list(client, source_id=sid, read_only="true")

    assert [i["title"] for i in read] == ["最旧的一条"]
    assert len(unread) == 2
    assert all(not i["is_read"] for i in unread)
    # 两个集合加起来正好是全集，且不相交
    assert {i["id"] for i in unread} & {i["id"] for i in read} == set()


def test_bulk_read_all(client):
    sid, _ = seed_batch()
    r = client.post("/api/notifications/read-all", json={})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["updated"] >= 3
    assert _list(client, source_id=sid, unread_only="true") == []


def test_bulk_read_by_ids_only_touches_given_ids(client):
    sid, ids = seed_batch()
    r = client.post("/api/notifications/read-all", json={"ids": [ids[0]], "is_read": True})
    assert r.json()["data"]["updated"] == 1

    read = _list(client, source_id=sid, read_only="true")
    assert [i["title"] for i in read] == ["最旧的一条"]


def test_bulk_can_also_mark_unread(client):
    """批量接口必须两个方向都能用。

    之前把基础条件写死成 `is_read=False`，结果「标为未读」一条也更新不到，
    而且 updated 永远是 0，看起来像接口没生效。
    """
    sid, ids = seed_batch()

    up = client.post("/api/notifications/read-all", json={"ids": ids, "is_read": True})
    assert up.json()["data"]["updated"] == 3
    assert _list(client, source_id=sid, unread_only="true") == []

    # 反方向
    down = client.post("/api/notifications/read-all", json={"ids": ids, "is_read": False})
    assert down.json()["data"]["updated"] == 3
    assert len(_list(client, source_id=sid, unread_only="true")) == 3

    # 再跑一遍同样的目标：没人需要改 → updated 必须是 0（幂等且 rowcount 有意义）
    again = client.post("/api/notifications/read-all", json={"ids": ids, "is_read": False})
    assert again.json()["data"]["updated"] == 0


def test_health_reports_unread_and_pinned(client):
    counts = client.get("/api/health").json()["data"]["counts"]
    assert "unread" in counts and "pinned" in counts
    assert isinstance(counts["unread"], int) and isinstance(counts["pinned"], int)


def test_migration_adds_pinned_column_to_existing_db():
    """模拟「升级前就存在的库」，并且带数据。

    这是最真实的风险点：`Base.metadata.create_all` 对已存在的表什么都不做，
    所以必须靠 _ensure_extra_columns 补列。跑两遍要幂等，且不能丢数据。
    """
    from app.db import _ensure_extra_columns

    path = Path(tempfile.mkdtemp(prefix="qz_mig_")) / "old.db"
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            # 老版本的表：没有 is_pinned，但有真实数据
            conn.execute(text(
                "CREATE TABLE notifications ("
                " id INTEGER PRIMARY KEY, title TEXT, is_read BOOLEAN DEFAULT 0)"
            ))
            conn.execute(text(
                "INSERT INTO notifications (id, title, is_read) "
                "VALUES (1, '升级前的老通知', 0), (2, '升级前的第二条', 1)"
            ))

        with eng.begin() as conn:
            _ensure_extra_columns(conn)
            _ensure_extra_columns(conn)          # 幂等：跑两次不能报错

        insp = inspect(eng)
        cols = {c["name"] for c in insp.get_columns("notifications")}
        assert "is_pinned" in cols
        assert "is_read" in cols                  # 老列不能被弄丢

        idx = {i["name"] for i in insp.get_indexes("notifications")}
        assert "ix_notifications_is_pinned" in idx

        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, title, is_pinned FROM notifications ORDER BY id"
            )).all()
        assert len(rows) == 2                     # 数据没丢
        assert rows[0][1] == "升级前的老通知"
        assert rows[0][2] in (0, False)           # 新列默认值 = 未置顶
    finally:
        eng.dispose()
