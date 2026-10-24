"""自定义大模型配置（/api/llm）。

这里测的重点不是「接口能返回 200」，而是四条容易静默出错的规则：
  1. 明文 key 永不出现在任何响应里（列表、详情、测试结果都不行）
  2. 编辑其它字段时，打码值回传不能被当成新 key 存进去
     ——写反了的表现是「改了个名字，模型突然 401」
  3. 删掉正在用的配置必须回落 .env，不能留悬空 active
  4. 请求体里的 `thinking` / `response_format` 只在报错点名时才摘，
     随便一个 400 都摘就会出现「明明参数没问题却报模型不支持」
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import llm_config
from app.services.ai_client import _drop_unsupported

FAKE_KEY = "sk-test-0123456789abcdef"


@pytest.fixture()
def client():
    """每个用例都从「没有任何自定义配置」开始。

    整场测试共用一个临时库（见 conftest），不清就会撞上 name 唯一约束 ——
    报错看着像业务 bug，其实是上一个用例留下的行。
    收尾还要回落 .env 并清进程缓存，否则会影响别的测试文件里的 AI 断言。
    """
    with TestClient(app) as c:
        yield c
        for row in c.get("/api/llm/profiles").json()["data"]["items"]:
            c.delete(f"/api/llm/profiles/{row['id']}")
        c.post("/api/llm/profiles/deactivate")
    llm_config.reset_cache()


def _create(client, name="我的模型", activate=False, **kw) -> int:
    body = {
        "name": name,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": FAKE_KEY,
    }
    body.update(kw)
    # activate 是 query 参数，不是 body 字段 —— 塞进 body 会被 Pydantic 静默忽略，
    # 表现为「建成功了但没启用」，很难第一眼看出是测试写错了。
    resp = client.post(
        "/api/llm/profiles",
        json=body,
        params={"activate": "true" if activate else "false"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# --------------------------------------------------------------------------- #
# 地址规整
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        (
            "https://open.bigmodel.cn/api/paas/v4",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
        (
            "https://x.com/v1/chat/completions",
            "https://x.com/v1/chat/completions",
        ),
        ("api.moonshot.cn", "https://api.moonshot.cn/v1/chat/completions"),
        # 整条粘别的协议的地址：学校 AI 网关的文档就写 /v1/messages（实测）
        (
            "http://aigw.dlut.edu.cn/v1/messages",
            "http://aigw.dlut.edu.cn/v1/chat/completions",
        ),
        ("http://aigw.dlut.edu.cn/v1/messages/", "http://aigw.dlut.edu.cn/v1/chat/completions"),
        (
            "https://api.openai.com/v1/completions",
            "https://api.openai.com/v1/chat/completions",
        ),
        (
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/chat/completions",
        ),
        # 已经是对的就别再动它（别拼成 /chat/completions/chat/completions）
        ("", ""),
    ],
)
def test_normalize_endpoint(given, expected):
    assert llm_config.normalize_endpoint(given) == expected


def test_normalize_endpoint_is_idempotent():
    """规整两次结果必须一样 —— 否则用户把显示出来的地址再存一遍就坏了。"""
    for raw in [
        "http://aigw.dlut.edu.cn/v1/messages",
        "https://api.deepseek.com",
        "https://api.openai.com/v1/completions",
    ]:
        once = llm_config.normalize_endpoint(raw)
        assert llm_config.normalize_endpoint(once) == once


def test_mask_key_never_leaks_middle():
    masked = llm_config.mask_key(FAKE_KEY)
    assert "****" in masked
    assert FAKE_KEY not in masked
    # 短 key 全遮，别露一半
    assert llm_config.mask_key("sk-1") == llm_config.MASK


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_list_and_hide_key(client):
    pid = _create(client)
    data = client.get("/api/llm/profiles").json()["data"]

    row = next(i for i in data["items"] if i["id"] == pid)
    assert row["has_key"] is True
    assert "****" in row["api_key_masked"]
    assert row["endpoint"].endswith("/v1/chat/completions")
    # ★ 最硬的一条：整个响应体里不能出现明文 key
    assert FAKE_KEY not in client.get("/api/llm/profiles").text


def test_duplicate_name_rejected(client):
    _create(client, name="重名测试")
    resp = client.post(
        "/api/llm/profiles",
        json={
            "name": "重名测试",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": FAKE_KEY,
        },
    )
    assert resp.status_code == 400
    assert "重名" in resp.json()["detail"]


def test_update_other_fields_keeps_original_key(client):
    """打码值回传 = 没改 key。写反了这里会变成 key 被存成 `sk-****cdef`。"""
    pid = _create(client)
    masked = client.get("/api/llm/profiles").json()["data"]["items"][0]["api_key_masked"]

    resp = client.patch(
        f"/api/llm/profiles/{pid}",
        json={"name": "改了名字", "api_key": masked},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"]
    assert row["name"] == "改了名字"
    assert row["has_key"] is True                 # key 还在
    assert row["api_key_masked"] == masked        # 而且没被改成打码串本身

    # 真改 key 才换
    resp2 = client.patch(f"/api/llm/profiles/{pid}", json={"api_key": "sk-new-9999888877776666"})
    assert resp2.json()["data"]["api_key_masked"].startswith("sk-n")
    assert resp2.json()["data"]["api_key_masked"] != masked


def test_create_requires_core_fields(client):
    resp = client.post(
        "/api/llm/profiles",
        json={"name": "缺模型", "base_url": "https://x.com", "model": "", "api_key": FAKE_KEY},
    )
    assert resp.status_code == 400
    assert "模型名" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# 激活 / 回落
# --------------------------------------------------------------------------- #
def test_activate_switches_effective_config(client):
    pid = _create(client, activate=True)
    data = client.get("/api/llm/profiles").json()["data"]
    assert data["active_id"] == pid
    assert data["effective_source"] == "profile"
    assert llm_config.get_active().profile_id == pid

    # 同一时刻只能有一个激活
    pid2 = _create(client, name="第二个")
    client.post(f"/api/llm/profiles/{pid2}/activate")
    data2 = client.get("/api/llm/profiles").json()["data"]
    assert data2["active_id"] == pid2
    assert sum(1 for i in data2["items"] if i["is_active"]) == 1


def test_deactivate_falls_back_to_env(client):
    _create(client, activate=True)
    assert llm_config.get_active().source == "profile"

    data = client.post("/api/llm/profiles/deactivate").json()["data"]
    assert data["active_id"] is None
    assert data["effective_source"] == "env"
    assert llm_config.get_active().source == "env"
    assert data["env"]["is_active"] is True


def test_delete_active_falls_back(client):
    """删掉正在用的配置不能留悬空 active —— 否则下次提问会打到不存在的行上。"""
    pid = _create(client, activate=True)
    resp = client.delete(f"/api/llm/profiles/{pid}").json()["data"]
    assert resp["fell_back_to_env"] is True
    assert llm_config.get_active().source == "env"

    data = client.get("/api/llm/profiles").json()["data"]
    assert data["active_id"] is None
    assert all(i["id"] != pid for i in data["items"])


def test_patch_cannot_clear_key(client):
    """空字符串不能把已有 key 抹掉。

    界面上 key 输入框留空提交是很常见的误操作；如果真清了，
    下一次提问会突然变成「未配置」，而用户以为自己什么都没改。
    """
    pid = _create(client)
    client.patch(f"/api/llm/profiles/{pid}", json={"api_key": ""})
    row = client.get("/api/llm/profiles").json()["data"]["items"][0]
    assert row["has_key"] is True


# --------------------------------------------------------------------------- #
# 测试连接（不打外网）
# --------------------------------------------------------------------------- #
def test_connection_without_key_is_honest(client):
    resp = client.post("/api/llm/test", json={"base_url": "https://api.deepseek.com", "model": "x"})
    data = resp.json()["data"]
    assert data["ok"] is False
    assert "API Key" in data["detail"]
    # 地址照样回显，用户能看出拼接对不对
    assert data["endpoint"].endswith("/v1/chat/completions")


def test_connection_reports_endpoint_on_failure(client, monkeypatch):
    """失败时也要回显最终请求地址和模型名 —— 这才是排障入口。

    这里把 check_ai 换成桩：真发请求会走 4 次重试 + 退避，一个用例要十几秒，
    而且跑测试的机器未必能出网。路由层逻辑（回显、写回 last_test_*）照样覆盖。
    """
    from app.api.routes import llm as llm_route

    seen: dict = {}

    async def fake_check(settings, cfg, **kw):
        seen.update(kw)          # 顺手把超时契约钉住，见下方断言
        return {
            "ok": False,
            "detail": "模拟：401 无效的 key",
            "endpoint": cfg.endpoint,
            "model": cfg.model,
        }

    monkeypatch.setattr(llm_route, "check_ai", fake_check)
    resp = client.post(
        "/api/llm/test",
        json={
            "base_url": "https://api.deepseek.com/v1",
            "api_key": FAKE_KEY,
            "model": "deepseek-chat",
        },
    )
    # 「测试连接」必须走 15 秒 + 不重试。
    # 默认的 60s × 2 次会让按钮转 2 分钟，而前端 20 秒就断了 ——
    # 用户看到的是「卡住」而不是「连不上」，实测踩过（2026-10-24）。
    assert seen == {"timeout": 15.0, "retry": False}, seen
    data = resp.json()["data"]
    assert data["endpoint"] == "https://api.deepseek.com/v1/chat/completions"
    assert data["model"] == "deepseek-chat"
    assert data["ok"] is False
    assert "401" in data["detail"]


def test_connection_result_written_back(client, monkeypatch):
    """测过就该在列表里留下痕迹（绿点/红点），省得用户每次重测。"""
    from app.api.routes import llm as llm_route

    async def fake_ok(settings, cfg, **kw):
        return {"ok": True, "reply": "OK", "endpoint": cfg.endpoint, "model": cfg.model}

    monkeypatch.setattr(llm_route, "check_ai", fake_ok)
    pid = _create(client)
    client.post(
        "/api/llm/test",
        json={"api_key": FAKE_KEY, "profile_id": pid},
    )
    row = client.get("/api/llm/profiles").json()["data"]["items"][0]
    assert row["last_test_ok"] is True
    assert row["last_test_at"] is not None


# --------------------------------------------------------------------------- #
# 请求参数兼容性兜底（纯函数，不联网）
# --------------------------------------------------------------------------- #
def test_drop_unsupported_only_when_named():
    """普通 400 不许乱摘参数，否则会把正常请求搞坏。"""
    payload = {"model": "x", "thinking": {"type": "disabled"}}
    assert _drop_unsupported(payload, '{"error":"model not found"}') is None
    assert "thinking" in payload


def test_drop_thinking_when_named():
    payload = {"model": "x", "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}}
    assert _drop_unsupported(payload, '{"error":"unknown field thinking"}') == "thinking"
    assert "thinking" not in payload
    assert "response_format" in payload


def test_drop_response_format_when_named():
    payload = {"model": "x", "response_format": {"type": "json_object"}}
    assert _drop_unsupported(payload, '{"error":"json_object is not supported"}') == "response_format"
    assert "response_format" not in payload


def test_drop_nothing_when_payload_clean():
    assert _drop_unsupported({"model": "x"}, "unsupported") is None


# --------------------------------------------------------------------------- #
# 本机地址要绕开系统代理
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:11434/v1/chat/completions", True),
        ("http://localhost:8000/v1/chat/completions", True),
        ("http://192.168.1.20:1234/v1/chat/completions", True),
        ("http://10.0.0.5:8000/v1/chat/completions", True),
        ("http://172.16.3.4:8000/v1/chat/completions", True),
        ("http://172.32.3.4:8000/v1/chat/completions", False),  # 172.32 是公网
        ("https://api.deepseek.com/v1/chat/completions", False),
        ("https://open.bigmodel.cn/api/paas/v4/chat/completions", False),
        ("", False),
    ],
)
def test_is_local_host(url, expected):
    """本地 Ollama 那类地址必须直连。

    不判的话，系统代理会把 127.0.0.1 也拦下来返回 502，
    用户看到的是「我的 Ollama 明明在跑」，却怎么都连不上（2026-10-24 实测）。
    """
    from app.services.ai_client import is_local_host

    assert is_local_host(url) is expected
