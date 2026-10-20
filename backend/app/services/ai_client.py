"""大模型调用层（智谱 GLM-4.7-Flash，OpenAI 兼容协议）。

设计要点：
  - 只依赖 OpenAI 兼容协议，换供应商只改 AI_BASE_URL / AI_MODEL
  - `chat_json()` 保证一定返回 dict：剥离 ```json 围栏 → 截取最外层 {} → 再解析
  - 没有 API Key 时 `enabled=False`，上层自动降级到本地规则摘要，链路照跑
  - **限流会自动退避重试**：GLM 免费档并发很低，会出现 429 / code 1302 / 1305。
    这里做指数退避（5→10→20→40 秒），跟 summarize.py 的策略保持一致，
    避免「抓取能过、问答一下就被限流打回本地兜底」这种体验割裂。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# ---- 限流识别与退避参数 ----
_RATE_LIMIT_WAIT = 5.0          # 首次退避基数（秒）
_RATE_LIMIT_RETRIES = 4         # 遇到限流最多重试几次
_RATE_LIMIT_HINTS = (
    "429", "1302", "1305",
    "访问量过大", "速率限制", "rate limit", "too many", "concurrency",
)


class AIError(RuntimeError):
    pass


def _is_rate_limited(text: str) -> bool:
    low = (text or "").lower()
    return any(h.lower() in low for h in _RATE_LIMIT_HINTS)


class AIClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.ai_enabled

    @property
    def model(self) -> str:
        return self.settings.ai_model

    # ------------------------------------------------------------------ #
    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int = 1200,
        timeout: float | None = None,
        json_mode: bool = False,
    ) -> str:
        if not self.enabled:
            raise AIError("未配置 AI_API_KEY")

        payload: dict[str, Any] = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.ai_temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self.settings.ai_thinking is False:
            # 关掉思维链。GLM-4.7-Flash 默认先吐一大段 reasoning_content，
            # 且**计入 max_tokens** —— 实测会把额度吃光、content 返回空串。
            payload["thinking"] = {"type": "disabled"}
        if json_mode and self.settings.ai_use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
        }

        last_err: Exception | None = None
        wait = _RATE_LIMIT_WAIT

        for attempt in range(_RATE_LIMIT_RETRIES):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout or self.settings.ai_timeout)
                ) as client:
                    resp = await client.post(
                        self.settings.ai_base_url, json=payload, headers=headers
                    )

                # ★ 只在**失败响应**上判断限流（2026-10-19 修）。
                #   以前这里写的是 `if resp.status_code == 429 or _is_rate_limited(resp.text)`，
                #   而 resp.text 里带着模型生成的正文 —— 正文里出现「429 / 1305 / 1302」
                #   这类数字（日期、电话、编号）时，一个**完全合法的 200 响应**会被判成限流，
                #   于是重试 4 次、最后报「AI 调用最终失败：HTTP 200」并降级成本地规则。
                #   实测 #137「集成电路学院迎新」就是这么被降级的：模型明明已经返回了
                #   三行摘要，日志里却写着失败。
                if resp.status_code == 200:
                    data = resp.json()
                    return self._pick_content(data)

                if resp.status_code == 429 or _is_rate_limited(resp.text):
                    # 免费档限流：退避后重试，不要一次失败就放弃
                    last_err = AIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    if attempt < _RATE_LIMIT_RETRIES - 1:
                        logger.warning(
                            "AI 被限流（第 %d 次），%.0f 秒后重试：%s",
                            attempt + 1, wait, resp.text[:120],
                        )
                        await asyncio.sleep(wait)
                        wait *= 2
                        continue
                    raise last_err

                if resp.status_code >= 400:
                    raise AIError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                data = resp.json()
                return self._pick_content(data)

            except (httpx.HTTPError, AIError, ValueError, KeyError) as e:
                last_err = e
                # 网络类错误也给一次短退避，避免瞬时抖动直接降级
                if attempt < _RATE_LIMIT_RETRIES - 1:
                    logger.warning("AI 调用失败（第 %d 次）：%s", attempt + 1, e)
                    await asyncio.sleep(min(wait, 10.0))
                    continue
                break

        raise AIError(f"AI 调用最终失败：{last_err}")

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        text = await self.chat(
            system, user, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        parsed = extract_json(text)
        if parsed is None:
            raise AIError(f"模型未返回合法 JSON：{text[:300]}")
        return parsed

    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick_content(data: dict[str, Any]) -> str:
        """兼容 OpenAI 风格与个别网关的变体返回。

        ⚠️ 推理模型的坑：`glm-4.7-flash` 响应里除了 `message.content`，
        还有一个 `message.reasoning_content`（思维链）。它**计入 max_tokens**。
        如果额度被思维链吃光，`content` 会是空字符串、`finish_reason == "length"`。
        这时必须给出**可诊断**的报错，而不是含糊的「取不出文本」。
        """
        if isinstance(data.get("choices"), list) and data["choices"]:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):  # 多模态分片
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if content:
                return str(content)
            if choice.get("text"):
                return str(choice["text"])

            finish = choice.get("finish_reason")
            reasoning = str(msg.get("reasoning_content") or "")
            if finish == "length":
                if reasoning:
                    raise AIError(
                        f"输出被 max_tokens 截断：思维链已消耗 {len(reasoning)} 字，"
                        f"`content` 为空。请关闭 thinking（AI_THINKING=false）"
                        f"或调大 max_tokens。"
                    )
                raise AIError("输出被 max_tokens 截断（finish_reason=length），请调大 max_tokens。")
        if data.get("output_text"):
            return str(data["output_text"])
        raise AIError(f"无法从响应中取出文本：{json.dumps(data, ensure_ascii=False)[:300]}")


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里稳准狠地抠出 JSON 对象。"""
    if not text:
        return None
    candidates: list[str] = []

    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 截取最外层花括号
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        snippet = text[start : end + 1]
        # 顺手修掉常见的尾逗号
        snippet = re.sub(r",\s*([}\]])", r"\1", snippet)
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            try:
                obj = json.loads(snippet.replace("'", '"'))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                return None
    return None


async def check_ai(settings: Settings | None = None) -> dict[str, Any]:
    """给 `/api/health` 和 `scripts/check_ai.py` 用的连通性自检。"""
    s = settings or get_settings()
    client = AIClient(s)
    if not client.enabled:
        return {"enabled": False, "ok": False, "detail": "未配置 AI_API_KEY，将使用本地规则摘要"}
    try:
        text = await client.chat("你是测试助手，只回复 OK。", "回复 OK", max_tokens=16)
        return {"enabled": True, "ok": True, "model": s.ai_model, "reply": text.strip()[:50]}
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "ok": False, "model": s.ai_model, "detail": str(e)[:300]}
