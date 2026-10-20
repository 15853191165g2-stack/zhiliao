"""推送（阶段 3）的接口级 + 通道级测试。

## 为什么 webhook 要真的起一个 HTTP 服务来收

用 `unittest.mock` 把 `httpx` 打桩，只能证明「我们调了 post()」，
证明不了「载荷长什么样、content-type 对不对、企业微信那种
『HTTP 200 + 业务 errcode』有没有被识别成失败」。
所以这里起一个真的 `ThreadingHTTPServer` 监听 127.0.0.1，
把收到的原始 body 存下来再断言 —— 这是唯一能发现「发出去的东西不对」的办法。

## 环境
环境变量统一由 `tests/conftest.py` 设置（见那个文件里关于「两个测试文件
各设 DATABASE_URL 会互相打架」的说明）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.timeutil import now_utc
from app.main import app
from app.models import AppMeta, Notification, Source
from app.services.push import (
    PushMessage,
    PushService,
    _build_webhook_payload,
    available_channels,
    get_channel,
)

_counter = 0


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #


def seed_push_batch(
    *,
    count: int = 2,
    age_hours: float = 1.0,
    importance: str = "normal",
    ai_status: str = "done",
    lines: list[str] | None = None,
) -> tuple[int, list[int]]:
    """造一批「可推送」的通知：ai_status=done + 发布时间在窗口内。

    ⚠️ 用独立的 engine 且在同一 loop 里 dispose —— 跨事件循环复用连接池会炸。
    ⚠️ dedup_key 是 UNIQUE 的，每批必须换 key。
    """
    global _counter
    _counter += 1
    batch = _counter

    async def run():
        eng = create_async_engine(os.environ["DATABASE_URL"])
        sm = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with sm() as session:
                src = Source(
                    name=f"推送测试源{batch}",
                    school="测试大学",
                    adapter="html_list",
                    url=f"http://example.invalid/push/{batch}",
                )
                session.add(src)
                await session.flush()

                rows = []
                for i in range(count):
                    rows.append(
                        Notification(
                            source_id=src.id,
                            dedup_key=f"pushbatch{batch}-{i}",
                            title=f"推送测试通知 {batch}-{i}",
                            url=f"http://example.invalid/n/{batch}/{i}",
                            published_at=now_utc() - timedelta(hours=age_hours),
                            excerpt=f"第 {batch}-{i} 条的原文摘要",
                            summary_lines=lines
                            if lines is not None
                            else [f"{batch}-{i} 第1行", "第2行", "第3行"],
                            ai_status=ai_status,
                            importance=importance,
                            category="通知公告",
                        )
                    )
                session.add_all(rows)
                await session.commit()
                return src.id, [r.id for r in rows]
        finally:
            await eng.dispose()

    return asyncio.run(run())


def _run(coro):
    return asyncio.run(coro)


async def _with_service(fn):
    """在一个干净的 session 上跑 PushService 的某个操作。"""
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sm = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with sm() as session:
            return await fn(PushService(session, get_settings()))
    finally:
        await eng.dispose()


def _flags_of(ids: list[int]) -> dict[int, tuple]:
    """读回 (notified_at, notify_error)，用来断言「到底标记了没」。"""
    async def run():
        eng = create_async_engine(os.environ["DATABASE_URL"])
        sm = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with sm() as session:
                from sqlalchemy import select

                rows = (
                    (await session.execute(
                        select(Notification).where(Notification.id.in_(ids))
                    )).scalars().all()
                )
                return {r.id: (r.notified_at, r.notify_error) for r in rows}
        finally:
            await eng.dispose()

    return _run(run())


def _clear_baseline() -> None:
    """删掉「首次静默基线」标记，让相关用例可重复运行。"""
    async def run():
        eng = create_async_engine(os.environ["DATABASE_URL"])
        sm = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with sm() as session:
                row = await session.get(AppMeta, "push_baseline_at")
                if row is not None:
                    await session.delete(row)
                    await session.commit()
        finally:
            await eng.dispose()

    _run(run())


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

# 会被用例改动的推送配置。用例结束后原样还原，避免互相污染。
_MUTABLE = (
    "push_channels",
    "push_enabled",
    "push_webhook_url",
    "push_webhook_kind",
    "push_lookback_hours",
    "push_batch_limit",
    "push_min_importance",
    "push_quiet_hours",
    "push_silent_first_run",
    "push_retries",
)


@pytest.fixture
def settings():
    s = get_settings()
    saved = {k: getattr(s, k) for k in _MUTABLE}
    # 默认值：只开本地通道，避免别的用例不小心触发外发
    s.push_enabled = True
    s.push_channels = "local"
    s.push_webhook_url = ""
    s.push_webhook_kind = "generic"
    s.push_lookback_hours = 48
    s.push_batch_limit = 20
    s.push_min_importance = "normal"
    s.push_quiet_hours = ""
    s.push_silent_first_run = False
    s.push_retries = 0
    try:
        yield s
    finally:
        for k, v in saved.items():
            setattr(s, k, v)


class _Receiver:
    """真的收 webhook 的本地 HTTP 服务。

    `reply_body` / `reply_status` 可以让它模拟"HTTP 200 但业务报错"，
    这是企业微信/钉钉最常见的失败形态，必须能测到。
    """

    def __init__(self, reply_status: int = 200, reply_body: bytes = b'{"ok":true}'):
        self.requests: list[dict] = []
        self.reply_status = reply_status
        self.reply_body = reply_body
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                receiver.requests.append(
                    {
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type", ""),
                        "body": body,
                        "json": _try_json(body),
                    }
                )
                self.send_response(receiver.reply_status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(receiver.reply_body)))
                self.end_headers()
                self.wfile.write(receiver.reply_body)

            def log_message(self, *args):  # 静音，别污染 pytest 输出
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _try_json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture
def receiver():
    r = _Receiver()
    try:
        yield r
    finally:
        r.stop()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# 1. 通道注册表
# --------------------------------------------------------------------------- #


def test_three_channels_registered():
    kinds = available_channels()
    assert "local" in kinds
    assert "webhook" in kinds
    assert "fcm" in kinds


def test_local_channel_is_always_ready(settings):
    ch = get_channel("local", settings)
    assert ch.enabled is True
    assert ch.is_external is False        # 本地通道不该被免打扰拦


def test_webhook_channel_not_ready_without_url(settings):
    settings.push_webhook_url = ""
    assert get_channel("webhook", settings).enabled is False
    settings.push_webhook_url = "http://127.0.0.1:1/x"
    assert get_channel("webhook", settings).enabled is True


def test_fcm_is_marked_as_external(settings):
    """FCM 是外发通道（要过免打扰），且没凭据时不该被当成可用。"""
    ch = get_channel("fcm", settings)
    assert ch.is_external is True
    assert ch.enabled is False
    assert "未验证" in ch.note


def test_unknown_channel_raises(settings):
    from app.services.push import PushError

    with pytest.raises(PushError):
        get_channel("telegram", settings)


def test_config_ignores_unknown_and_duplicate_channel_names(settings):
    settings.push_channels = "local, local ,nope,webhook"
    assert settings.push_channel_list() == ["local", "nope", "webhook"]


# --------------------------------------------------------------------------- #
# 2. 载荷格式（纯函数）
# --------------------------------------------------------------------------- #


def _msg() -> PushMessage:
    return PushMessage(
        notification_id=1,
        title="关于选课的通知",
        lines=["第一行摘要", "第二行摘要", "第三行摘要"],
        importance="important",
        category="教学",
        source_name="教务处",
        url="http://example.invalid/a",
        published_at=None,
    )


def test_generic_payload_shape():
    payload, ct = _build_webhook_payload("generic", _msg())
    assert ct == "application/json"
    assert payload["event"] == "notification.new"
    assert payload["notification"]["id"] == 1
    assert payload["notification"]["lines"] == ["第一行摘要", "第二行摘要", "第三行摘要"]


def test_wecom_payload_is_markdown():
    """企业微信机器人只认 {"msgtype": "markdown", "markdown": {"content": ...}}。"""
    payload, ct = _build_webhook_payload("wecom", _msg())
    assert ct == "application/json"
    assert payload["msgtype"] == "markdown"
    assert "关于选课的通知" in payload["markdown"]["content"]


def test_dingtalk_payload_requires_title():
    payload, _ = _build_webhook_payload("dingtalk", _msg())
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "【教务处】关于选课的通知"


def test_serverchan_payload_is_form():
    payload, ct = _build_webhook_payload("serverchan", _msg())
    assert ct == "application/x-www-form-urlencoded"
    assert set(payload) == {"title", "desp"}


def test_bark_payload_has_group():
    payload, _ = _build_webhook_payload("bark", _msg())
    assert payload["group"] == "知了"
    assert payload["title"] == "【教务处】关于选课的通知"


# --------------------------------------------------------------------------- #
# 3. webhook 真的发出去（本地服务收）
# --------------------------------------------------------------------------- #


def test_webhook_really_posts_generic_json(settings, receiver):
    settings.push_channels = "webhook"
    settings.push_webhook_kind = "generic"
    settings.push_webhook_url = receiver.url

    results = _run(_with_service(lambda svc: svc.send_test("测试消息")))
    assert len(results) == 1 and results[0].ok, results[0].error

    assert len(receiver.requests) == 1
    got = receiver.requests[0]
    assert got["path"] == "/hook"
    assert "application/json" in got["content_type"]
    assert got["json"]["title"]                       # 有标题
    assert "测试消息" in json.dumps(got["json"], ensure_ascii=False)


def test_webhook_posts_wecom_format(settings, receiver):
    settings.push_channels = "webhook"
    settings.push_webhook_kind = "wecom"
    settings.push_webhook_url = receiver.url

    _run(_with_service(lambda svc: svc.send_test("企业微信格式")))
    body = receiver.requests[0]["json"]
    assert body["msgtype"] == "markdown"
    assert "企业微信格式" in body["markdown"]["content"]


def test_webhook_200_with_business_error_is_failure(settings):
    """★ 最容易漏的一个坑。

    企业微信/钉钉失败时 HTTP 状态码**仍然是 200**，
    错误藏在 body 的 errcode 里。只看状态码会把失败当成功，
    用户「一直收不到通知、日志里却全是 ok」。
    """
    bad = _Receiver(reply_body=b'{"errcode":93000,"errmsg":"invalid webhook url"}')
    try:
        settings.push_channels = "webhook"
        settings.push_webhook_url = bad.url
        results = _run(_with_service(lambda svc: svc.send_test("会失败的")))
        assert results[0].ok is False
        assert "93000" in (results[0].error or "")
    finally:
        bad.stop()


def test_webhook_http_error_reports_body(settings):
    bad = _Receiver(reply_status=500, reply_body=b"boom")
    try:
        settings.push_channels = "webhook"
        settings.push_webhook_url = bad.url
        results = _run(_with_service(lambda svc: svc.send_test("会 500")))
        assert results[0].ok is False
        assert "500" in (results[0].error or "")
    finally:
        bad.stop()


def test_webhook_unreachable_does_not_raise(settings):
    """目标不可达时必须返回 failed 结果，不能把异常抛上去打断抓取。"""
    settings.push_channels = "webhook"
    settings.push_webhook_url = "http://127.0.0.1:1/nothing"   # 必然拒连
    results = _run(_with_service(lambda svc: svc.send_test("打不通")))
    assert results[0].ok is False
    assert results[0].error


# --------------------------------------------------------------------------- #
# 4. 谁该被推
# --------------------------------------------------------------------------- #


def test_lookback_window_filters_old_items(settings):
    """超过时间窗的老通知不该被推。

    这条规则同时防两件事：第一次开推送轰出几百条积压；
    抓取迟到后把一周前的老通知当新闻推。
    """
    settings.push_lookback_hours = 6
    _, old_ids = seed_push_batch(count=1, age_hours=72)
    _, new_ids = seed_push_batch(count=1, age_hours=1)

    msgs = _run(_with_service(lambda svc: svc.pending_messages(limit=200)))
    got = {m.notification_id for m in msgs}
    assert not (set(old_ids) & got), "72 小时前的老通知不该出现在待推送里"
    assert set(new_ids) & got, "1 小时前的新通知应该要推"


def test_importance_threshold(settings):
    settings.push_min_importance = "important"
    _, low_ids = seed_push_batch(count=1, importance="normal")
    _, high_ids = seed_push_batch(count=1, importance="urgent")

    msgs = _run(_with_service(lambda svc: svc.pending_messages(limit=200)))
    got = {m.notification_id for m in msgs}
    assert not (set(low_ids) & got)
    assert set(high_ids) & got


def test_pending_skips_items_without_summary(settings):
    """ai_status 还不是 done 的条目不推 —— 否则推出去没有 3 行摘要。"""
    _, ids = seed_push_batch(count=1, ai_status="pending")
    msgs = _run(_with_service(lambda svc: svc.pending_messages(limit=200)))
    assert not ({m.notification_id for m in msgs} & set(ids))


def test_quiet_hours_only_blocks_external(settings, receiver):
    """★ 免打扰只拦外发通道。

    本地通知是用户自己拉取的，拦它没有意义 ——
    拦住反而会让用户第二天打开 App 时看不到当时的消息。
    """
    from zoneinfo import ZoneInfo

    # 造一个「此刻一定落在区间内」的免打扰时段，不依赖测试运行的钟点
    local = now_utc().astimezone(ZoneInfo(settings.timezone))
    settings.push_quiet_hours = (
        f"{local.hour:02d}:{local.minute:02d}-{(local.hour + 1) % 24:02d}:00"
    )
    settings.push_channels = "local,webhook"
    settings.push_webhook_url = receiver.url

    _, ids = seed_push_batch(count=1)
    stats = _run(_with_service(lambda svc: svc.push_pending()))

    # 本地通道照发（进入队列，等客户端拉）
    assert stats["queued"] >= 1, stats
    assert receiver.requests == [], f"免打扰时段不该外发，却收到了 {len(receiver.requests)} 个请求"
    assert any("免打扰" in w for w in stats["warnings"]), stats["warnings"]
    # 队列没被消费 —— 用户第二天打开 App 仍然能看到这条
    assert _flags_of(ids)[ids[0]][0] is None


def test_quiet_hours_range_parsing(settings):
    settings.push_quiet_hours = "23:00-07:00"
    assert settings.push_quiet_range() == (23 * 60, 7 * 60)
    settings.push_quiet_hours = "  08:30 - 12:15 "
    assert settings.push_quiet_range() == (8 * 60 + 30, 12 * 60 + 15)
    for bad in ("", "23:00", "25:00-07:00", "aa-bb", "23:99-07:00"):
        settings.push_quiet_hours = bad
        assert settings.push_quiet_range() is None, bad


# --------------------------------------------------------------------------- #
# 5. 本地队列：拉取只读、确认才写
# --------------------------------------------------------------------------- #


def test_pending_endpoint_is_readonly(client, settings):
    """★ 拉取**不能**标记已推送。

    否则客户端拉到之后弹通知失败（权限被拒 / 进程被杀），
    这条消息就永久丢了 —— 因为下次不会再被拉出来。
    """
    _, ids = seed_push_batch(count=1)
    r = client.get("/api/push/pending?limit=200")
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert any(it["id"] == ids[0] for it in items)

    # 拉完再拉一次，还在
    r2 = client.get("/api/push/pending?limit=200")
    assert any(it["id"] == ids[0] for it in r2.json()["data"]["items"])

    # 而且库里没被标记
    assert _flags_of(ids)[ids[0]][0] is None


def test_pending_payload_carries_three_lines(client, settings):
    _, ids = seed_push_batch(count=1, lines=["甲", "乙", "丙"])
    r = client.get("/api/push/pending?limit=200")
    item = next(it for it in r.json()["data"]["items"] if it["id"] == ids[0])
    assert item["lines"] == ["甲", "乙", "丙"]
    assert item["text"]                       # 有可直接展示的文本
    assert item["importance_label"]           # 有中文重要度


def test_ack_marks_notified_and_is_idempotent(client, settings):
    _, ids = seed_push_batch(count=2)
    first = client.post("/api/push/ack", json={"ids": ids}).json()["data"]
    assert first["updated"] == 2
    assert _flags_of(ids)[ids[0]][0] is not None

    again = client.post("/api/push/ack", json={"ids": ids}).json()["data"]
    assert again["updated"] == 0, "重复确认不该重复计数"

    # 确认之后就不该再被拉出来
    items = client.get("/api/push/pending?limit=200").json()["data"]["items"]
    assert not ({it["id"] for it in items} & set(ids))


def test_ack_with_empty_ids_is_noop(client):
    assert client.post("/api/push/ack", json={"ids": []}).json()["data"]["updated"] == 0


# --------------------------------------------------------------------------- #
# 6. push_pending 的标记语义
# --------------------------------------------------------------------------- #


def test_local_channel_does_not_consume_queue(settings):
    """★ 本地通道**不能**消费队列。

    这是最容易写错、而且错了以后「看起来一切正常」的一处：
    本地通道的「发送」只是把消息留在队列里等客户端拉。
    如果它也算发送成功、把 notified_at 标上，队列当场清空 ——
    客户端之后调 /push/pending 永远拿到空数组，
    表现就是「推送静默失效」，而服务端日志里全是成功。
    """
    settings.push_channels = "local"
    _, ids = seed_push_batch(count=2)
    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))

    assert stats["queued"] >= 2, stats
    assert stats["sent"] == 0, "本地通道不该计入 sent（没真正送达任何地方）"
    flags = _flags_of(ids)
    assert all(flags[i][0] is None for i in ids), "本地通道消费掉了队列 = 客户端拉不到"


def test_external_channel_consumes_queue(settings, receiver):
    """外发通道成功 = 真的送到你手机/群里了，应该消费掉，否则每轮重发。"""
    settings.push_channels = "webhook"
    settings.push_webhook_url = receiver.url
    _, ids = seed_push_batch(count=1)
    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))

    assert stats["sent"] >= 1
    assert stats["queued"] == 0
    assert _flags_of(ids)[ids[0]][0] is not None


def test_local_plus_webhook_marks_because_webhook_consumes(settings, receiver):
    settings.push_channels = "local,webhook"
    settings.push_webhook_url = receiver.url
    _, ids = seed_push_batch(count=1)
    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))
    assert stats["sent"] >= 1
    assert _flags_of(ids)[ids[0]][0] is not None


def test_all_channels_failed_keeps_item_for_retry(settings):
    """★ 全部通道失败时**不能**标记 notified_at。

    标记了就等于「我们宣称推过了」，而用户根本没收到。
    不标记则下一轮会重试 —— 这正好验证了「失败可自愈」。
    """
    settings.push_channels = "webhook"
    settings.push_webhook_url = "http://127.0.0.1:1/dead"
    _, ids = seed_push_batch(count=1)

    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))
    assert stats["failed"] >= 1
    notified_at, err = _flags_of(ids)[ids[0]]
    assert notified_at is None, "发送全失败却标记成已推送 = 谎报"
    assert err, "失败原因要落库，否则用户问『为什么没收到』没法查"


def test_push_logs_are_written(client, settings, receiver):
    settings.push_channels = "webhook"
    settings.push_webhook_url = receiver.url
    _, ids = seed_push_batch(count=1)
    _run(_with_service(lambda svc: svc.push_pending(limit=200)))

    logs = client.get("/api/push/logs?limit=20").json()["data"]
    mine = [x for x in logs if x["notification_id"] == ids[0]]
    assert mine, "推送必须留下审计记录"
    assert mine[0]["channel"] == "webhook"
    assert mine[0]["status"] == "ok"


def test_failed_push_is_logged_with_error(client, settings):
    settings.push_channels = "webhook"
    settings.push_webhook_url = "http://127.0.0.1:1/dead"
    _, ids = seed_push_batch(count=1)
    _run(_with_service(lambda svc: svc.push_pending(limit=200)))

    logs = client.get("/api/push/logs?limit=50&status=failed").json()["data"]
    mine = [x for x in logs if x["notification_id"] == ids[0]]
    assert mine
    assert mine[0]["error"]


def test_push_disabled_is_a_noop(settings, client):
    """关掉推送后不该写任何推送日志，也不该有任何通道被调用。"""
    settings.push_enabled = False
    before = len(client.get("/api/push/logs?limit=200").json()["data"])
    _, ids = seed_push_batch(count=1)

    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))
    assert stats["sent"] == 0 and stats["queued"] == 0 and stats["failed"] == 0
    assert _flags_of(ids)[ids[0]][0] is None
    assert any("未启用" in w for w in stats["warnings"])

    after = len(client.get("/api/push/logs?limit=200").json()["data"])
    assert after == before, "推送关着却写了审计日志"


# --------------------------------------------------------------------------- #
# 6b. 客户端游标（多设备安全）
# --------------------------------------------------------------------------- #


def test_since_id_cursor_returns_only_newer(client, settings):
    """★ 游标模式：客户端问「比 X 大的还有哪些」。

    为什么不用 ack：`notified_at` 是**全局**的，一台设备 ack 了，
    另一台就再也拿不到这条了 —— 手机和电脑会互相抢消息。
    游标存在客户端本地，各设备互不干扰。
    """
    _, ids_a = seed_push_batch(count=1)
    base = max(ids_a)
    r = client.get(f"/api/push/pending?since_id={base}&limit=50").json()["data"]
    assert not ({it["id"] for it in r["items"]} & set(ids_a)), "游标之前的条目不该再返回"

    _, ids_b = seed_push_batch(count=2)
    r2 = client.get(f"/api/push/pending?since_id={base}&limit=50").json()["data"]
    got = {it["id"] for it in r2["items"]}
    assert set(ids_b) & got
    assert r2["next_since_id"] >= max(ids_b)


def test_pending_reports_max_id_for_cursor_initialization(client, settings):
    """首次启用时客户端要能拿到 max_id 来初始化游标，
    否则一打开就会把库里所有历史通知全弹一遍。"""
    _, ids = seed_push_batch(count=1)
    d = client.get("/api/push/pending?limit=1").json()["data"]
    assert d["max_id"] >= max(ids)


def test_pending_cursor_does_not_stall_on_filtered_items(client, settings):
    """★ 被过滤掉的条目不能把游标卡住。

    这是个真实的坑：游标模式按 id 升序取前 N 条。
    如果时间窗过滤只写在 Python 里，取回来的老条目会被全部丢掉、
    客户端拿不到任何东西 → 游标永远不前进 → **永久卡死**。
    所以过滤必须下推到 SQL，测试里造一条「窗口外」的再验证能越过它。
    """
    settings.push_lookback_hours = 6
    _, old_ids = seed_push_batch(count=1, age_hours=200)     # 窗口外
    base = min(old_ids) - 1

    _, new_ids = seed_push_batch(count=1, age_hours=1)       # 窗口内
    r = client.get(f"/api/push/pending?since_id={base}&limit=50").json()["data"]
    got = {it["id"] for it in r["items"]}
    assert set(new_ids) & got, "游标被窗口外的老条目挡住了"
    assert not (set(old_ids) & got)


# --------------------------------------------------------------------------- #
# 7. 首次静默基线
# --------------------------------------------------------------------------- #


def test_first_run_baseline_swallows_backlog(settings):
    """★ 第一次开推送时，库里的历史积压要被「静默吞掉」。

    否则用户一开启推送，手机立刻被 150+ 条历史通知刷屏。

    ⚠️ 基线现在是在**应用启动时**做的（main.py 的 lifespan），
    不再由 push_pending 触发 —— 因为抓取和推送是同一次 job 的前后两步，
    放在推送里做会把「这一次刚抓到的新通知」也一起吞掉，
    表现是「订阅了却一条都收不到」。
    这里直接调 ensure_baseline() 验证它本身的行为。
    """
    _clear_baseline()
    _, ids = seed_push_batch(count=2, age_hours=1)

    res = _run(_with_service(lambda svc: svc.ensure_baseline()))
    assert res["done"] is True
    assert res["marked"] >= 2
    # 积压被标记为「无需推送」，且一条都没发出去
    assert all(_flags_of(ids)[i][0] is not None for i in ids)

    # 幂等：第二次调用不该再标一遍
    res2 = _run(_with_service(lambda svc: svc.ensure_baseline()))
    assert res2["done"] is False
    assert res2["marked"] == 0


def test_baseline_does_not_run_inside_push_pending(settings):
    """★ 守住上面那个坑：push_pending 里绝不能偷偷做基线。

    否则「启动 → 抓取 → 推送」这条链路上，同一次 job 刚抓到的新通知
    会被当成历史积压一起吞掉，用户永远收不到。
    """
    _clear_baseline()
    settings.push_silent_first_run = True
    settings.push_channels = "local,webhook"
    _, ids = seed_push_batch(count=1, age_hours=1)

    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200)))
    assert stats["baseline"] is False, "push_pending 不该触发基线"
    # 新通知必须留在队列里能被客户端拉到
    msgs = _run(_with_service(lambda svc: svc.pending_messages(limit=200)))
    assert {m.notification_id for m in msgs} & set(ids)


def test_force_bypasses_window(settings, receiver):
    """手动 force 推一遍时，时间窗不该拦。"""
    settings.push_lookback_hours = 1
    settings.push_channels = "webhook"
    settings.push_webhook_url = receiver.url      # 不配地址 = 没通道，会被直接跳过
    _, ids = seed_push_batch(count=1, age_hours=100)

    # 先证明不给 force 时它确实被时间窗挡住
    normal = _run(_with_service(lambda svc: svc.push_pending(limit=200)))
    assert _flags_of(ids)[ids[0]][0] is None, "100 小时前的条目不该被常规推送捞出来"
    assert ids[0] not in {m.notification_id for m in _run(
        _with_service(lambda svc: svc.pending_messages(limit=200)))}

    stats = _run(_with_service(lambda svc: svc.push_pending(limit=200, force=True)))
    assert stats["checked"] >= 1, "force 时应该无视时间窗把它捞出来"
    assert _flags_of(ids)[ids[0]][0] is not None


# --------------------------------------------------------------------------- #
# 8. 3 行摘要的兜底
# --------------------------------------------------------------------------- #


def test_three_lines_fallback_when_summary_missing(settings):
    """ai_status=done 却没有摘要时，也要产出 3 行而不是空白。

    理论上不该发生（summarize.py 保证正好 3 行），
    但历史数据/手工导入的行可能没有 —— 推一条空白通知出去最伤体验。
    """
    _, ids = seed_push_batch(count=1, lines=[])
    msgs = _run(_with_service(lambda svc: svc.pending_messages(limit=200)))
    msg = next((m for m in msgs if m.notification_id == ids[0]), None)
    assert msg is not None
    assert len(msg.lines) == 3
    assert any(x.strip() for x in msg.lines), "至少有一行非空"


def test_message_text_skips_blank_lines():
    m = PushMessage(
        notification_id=1, title="T", lines=["有内容", "", ""],
        importance="normal", category="其他", source_name="来源",
    )
    text = m.as_text()
    assert "有内容" in text
    assert "\n\n" not in text            # 不留空行


# --------------------------------------------------------------------------- #
# 9. 接口与健康检查
# --------------------------------------------------------------------------- #


def test_push_config_endpoint_masks_secret(client, settings):
    """★ 配置接口不能回显完整 webhook 地址。

    企业微信/钉钉/Server酱 的地址里带的就是密钥，回显等于泄密。
    """
    settings.push_webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRET"
    body = client.get("/api/push/config").json()["data"]
    dumped = json.dumps(body, ensure_ascii=False)
    assert "SUPERSECRET" not in dumped, "webhook 密钥被回显了"
    assert body["webhook"]["configured"] is True


def test_push_config_reports_channels_and_state(client, settings):
    body = client.get("/api/push/config").json()["data"]
    assert body["enabled"] is True
    assert "local" in body["available_channels"]
    assert body["lookback_hours"] >= 1
    assert "unsent" in body
    assert body["quiet_now"] in (True, False)


def test_channels_endpoint(client, settings):
    rows = client.get("/api/push/channels").json()["data"]
    kinds = {r["kind"] for r in rows}
    assert {"local", "webhook", "fcm"} <= kinds
    local = next(r for r in rows if r["kind"] == "local")
    assert local["external"] is False
    assert local["ready"] is True
    # 本地通道不消费队列 —— 客户端要能从这里看出来，否则很难理解为什么消息一直在
    assert local["consumes_queue"] is False
    webhook = next(r for r in rows if r["kind"] == "webhook")
    assert webhook["consumes_queue"] is True


def test_push_test_endpoint_reports_per_channel(client, settings, receiver):
    settings.push_channels = "local,webhook"
    settings.push_webhook_url = receiver.url
    body = client.post("/api/push/test", json={"text": "自检"}).json()["data"]
    assert body["ok"] is True
    by = {r["channel"]: r for r in body["results"]}
    assert by["local"]["ok"] is True
    assert by["webhook"]["ok"] is True
    assert len(receiver.requests) == 1


def test_health_reports_push_state(client, settings):
    body = client.get("/api/health").json()["data"]
    assert "push" in body
    assert body["push"]["enabled"] is True
    assert "unsent" in body["push"]
    assert "channels" in body["push"]


def test_ingest_config_exposes_push(client):
    body = client.get("/api/ingest/config").json()["data"]
    assert "push" in body
    assert "channels" in body["push"]


def test_scheduler_status_exposes_cron(client):
    body = client.get("/api/health").json()["data"]
    assert body["scheduler"]["enabled"] is False      # 测试环境关了调度器
    assert body["cron"]


# --------------------------------------------------------------------------- #
# 10. 迁移
# --------------------------------------------------------------------------- #


def test_migration_adds_push_columns_to_existing_db():
    """★ 老库升级。

    `create_all` 不会给已存在的表加列，用户库里躺着 150+ 条真实数据，
    迁移写错就是「一升级就 no such column」。
    """
    import tempfile
    from pathlib import Path

    from app.db import _ensure_extra_columns

    path = Path(tempfile.mkdtemp(prefix="qz_pushmig_")) / "old.db"
    eng = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE notifications ("
                " id INTEGER PRIMARY KEY, title TEXT, is_read BOOLEAN DEFAULT 0)"
            ))
            conn.execute(text(
                "INSERT INTO notifications (id, title, is_read) VALUES"
                " (1, '升级前的老通知', 0), (2, '升级前的第二条', 1), (3, '第三条', 0)"
            ))

        with eng.begin() as conn:
            _ensure_extra_columns(conn)
            _ensure_extra_columns(conn)         # 幂等

        insp = inspect(eng)
        cols = {c["name"] for c in insp.get_columns("notifications")}
        assert {"notified_at", "notify_error"} <= cols
        assert "is_read" in cols                # 老列没被弄丢

        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, title, notified_at FROM notifications ORDER BY id"
            )).all()
        assert len(rows) == 3                   # 数据一条没少
        assert rows[0][1] == "升级前的老通知"
        assert rows[0][2] is None               # 新列默认 NULL = 还没推过
    finally:
        eng.dispose()
