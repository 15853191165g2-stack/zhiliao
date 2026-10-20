"""200 响应里出现「429 / 1305」不该被当成限流。

## 背景（2026-10-19 实测）

`#137 集成电路学院迎新` 这一条，模型返回的是**完全合法的 JSON**（三行摘要都在），
日志却写着：

    AI 摘要失败，降级到本地规则：AI 调用最终失败：HTTP 200: {"choices":[...]}

原因在 `chat()` 里这一行：

```python
if resp.status_code == 429 or _is_rate_limited(resp.text):
```

`resp.text` 是**整个响应体**，里面既有状态码，也有模型生成的正文。
只要正文里出现 `429` / `1305` / `1302` 这类数字（日期、电话、编号、房间号都可能），
一个成功的 200 就会被判成「限流」→ 退避重试 4 次 → 最后抛错 → 降级本地规则。

**表面指标全绿**：没有异常冒出来、摘要还是 3 行、`ai_status=done`，
只有 `ai_model` 悄悄从 `glm-4.7-flash` 变成 `local-fallback`。

教训：限流是**传输层**的事实，只能从失败响应判断。
拿一个成功响应的正文去做关键词匹配，等于让模型自己决定要不要重试。
"""

from __future__ import annotations

import json

import pytest

from app.services import ai_client as ai_client_mod
from app.services.ai_client import AIClient, AIError


def _install_fake_transport(monkeypatch, *, status: int, body: str) -> list[int]:
    """把 httpx.AsyncClient 换成一个永远返回固定响应的假客户端。

    返回调用计数器，便于断言「重试了几次」。
    """
    calls: list[int] = []

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self.text = body

        def json(self):
            return json.loads(body)

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D107
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, *args, **kwargs):
            calls.append(1)
            return _Resp()

    monkeypatch.setattr(ai_client_mod.httpx, "AsyncClient", _Client)
    return calls


def _client() -> AIClient:
    return AIClient(
        ai_client_mod.Settings(ai_api_key="test-key", ai_base_url="http://example.invalid/v1")
    )


async def test_success_body_containing_429_is_not_treated_as_rate_limit(monkeypatch) -> None:
    """正文里带「1305 号」「429 元」这类数字时，200 依然是成功。"""
    content = '{"summary_lines":["报到地点：信息楼 1305 室","缴费 429 元","截止 1302 号窗口"]}'
    body = json.dumps(
        {
            "choices": [
                {"finish_reason": "stop", "message": {"content": content, "role": "assistant"}}
            ]
        },
        ensure_ascii=False,
    )
    calls = _install_fake_transport(monkeypatch, status=200, body=body)

    got = await _client().chat("system", "user")

    assert "1305" in got, "模型明明返回了内容，不能被吞掉"
    assert len(calls) == 1, f"200 不该重试，实际请求了 {len(calls)} 次"


async def test_429_still_retries_and_then_fails(monkeypatch) -> None:
    """反向保护：真的 429 必须继续退避重试（别为了修误判把限流逻辑改没了）。"""
    monkeypatch.setattr(ai_client_mod, "_RATE_LIMIT_WAIT", 0.0)  # 测试里不要真等 35 秒

    body = json.dumps({"error": {"code": "1305", "message": "该模型当前访问量过大"}}, ensure_ascii=False)
    calls = _install_fake_transport(monkeypatch, status=429, body=body)

    with pytest.raises(AIError):
        await _client().chat("system", "user")

    assert len(calls) == ai_client_mod._RATE_LIMIT_RETRIES, "429 应该按上限重试后再放弃"
