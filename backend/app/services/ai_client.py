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
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.services.llm_config import LLMConfig, get_active, is_zhipu

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


def is_local_host(url: str) -> bool:
    """目标是不是本机 / 内网地址。是的话这次请求必须**绕开系统代理**。

    为什么必须单独判：Windows 上常配着系统代理（学校网尤其常见）。
    httpx 默认 `trust_env=True`，会把 `127.0.0.1:11434`（本地 Ollama）也塞给代理，
    代理自然连不上，返回 502 "upstream connect failed" ——
    用户看到的是「我的 Ollama 明明在跑，知了却说连不上」。
    实测本机就是这个现象（2026-10-24 用 127.0.0.1:9 验的）。
    """
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("10.") or host.startswith("192.168.") or host.endswith(".local"):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


# 「这个参数我不认识」的常见说法。只用于 400/422，且必须报错里点到名、
# 或者明确说了 unsupported/unknown/extra —— 不能因为正文里出现「400」这种数字就误摘。
_UNSUPPORTED_GENERIC = (
    "unsupported", "not support", "not supported", "unknown field", "unknown parameter",
    "unrecognized", "unexpected field", "extra fields", "invalid parameter", "无效参数",
    "不支持", "多余",
)


def _drop_unsupported(payload: dict[str, Any], text: str) -> str | None:
    """从请求体里摘掉对方不认的那个字段，返回摘掉的名字；没得摘返回 None。

    只处理两个已知会打架的字段：
      - `thinking`：智谱专属，别家见到会 400
      - `response_format`：多数支持，少数网关/开源模型不支持
    先看报错点名了谁；没点名但说了 unsupported 这类话，就按 thinking → response_format
    的顺序逐个摘，下一轮再进来摘第二个（外层循环还有余量）。
    """
    low = (text or "").lower()
    generic = any(k in low for k in _UNSUPPORTED_GENERIC)

    if "thinking" in payload and ("thinking" in low or generic):
        payload.pop("thinking", None)
        return "thinking"
    if "response_format" in payload and (
        "response_format" in low or "json_object" in low or "json mode" in low or generic
    ):
        payload.pop("response_format", None)
        return "response_format"
    return None


class AIClient:
    def __init__(self, settings: Settings | None = None, llm: LLMConfig | None = None) -> None:
        self.settings = settings or get_settings()
        # 不传 llm 就取「当前生效配置」：库里激活了自定义模型就用它，否则回落 .env。
        # 这样写是为了让 summarize / brief / chat 三个调用点**一处都不用改**，
        # 也能跟着用户在问答界面上的切换走。
        self.llm = llm or get_active(self.settings)

    @property
    def enabled(self) -> bool:
        return self.llm.enabled

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def model_label(self) -> str:
        return self.llm.label

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
        retry: bool = True,
    ) -> str:
        """发一次 chat 请求。

        `retry=False` 用于「测试连接」：连通性测试要回答的是**现在这一下通不通**，
        重试只会把 15 秒的失败拖成 30 秒，用户在界面上看到的就是转圈不动。
        实测校外访问校内网关时，60s 超时 × 2 次 = 121 秒，而前端 90 秒就断了 ——
        用户等 1 分半拿到的是「卡住」而不是「连不上」。
        """
        if not self.enabled:
            raise AIError("未配置 API Key")

        payload: dict[str, Any] = {
            "model": self.llm.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.ai_temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # thinking 只有智谱系认。别的服务商收到会 400，所以按域名判一次，
        # 而不是无脑发（用户填的 base_url 可能是任何 OpenAI 兼容服务）。
        if self.llm.thinking is False and is_zhipu(self.llm.endpoint):
            # 关掉思维链。GLM-4.7-Flash 默认先吐一大段 reasoning_content，
            # 且**计入 max_tokens** —— 实测会把额度吃光、content 返回空串。
            payload["thinking"] = {"type": "disabled"}
        if json_mode and self.llm.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.llm.api_key}",
            "Content-Type": "application/json",
        }
        url = self.llm.endpoint

        last_err: Exception | None = None
        wait = _RATE_LIMIT_WAIT

        for attempt in range(_RATE_LIMIT_RETRIES):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout or self.settings.ai_timeout),
                    # 本机/内网地址直连：否则会被系统代理拦成 502，
                    # 而用户根本想不到「代理」这一层（见 is_local_host 的注释）。
                    trust_env=not is_local_host(url),
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)

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
                    if retry and attempt < _RATE_LIMIT_RETRIES - 1:
                        logger.warning(
                            "AI 被限流（第 %d 次），%.0f 秒后重试：%s",
                            attempt + 1, wait, resp.text[:120],
                        )
                        await asyncio.sleep(wait)
                        wait *= 2
                        continue
                    raise last_err

                # ★ 自定义模型的「参数不兼容」兜底（2026-10-24 加）。
                #   用户填的任何 OpenAI 兼容服务，未必认 `thinking` / `response_format`：
                #   智谱认前者、多数学后者、有些网关两个都不认，直接 400。
                #   这里不猜是哪一家，而是**看报错内容把可疑参数摘掉重试** ——
                #   否则界面上的表现是「刚填完 key 就报 HTTP 400」，用户会以为是
                #   自己的 key 填错了，其实是请求体里多了个对方不认的字段。
                if resp.status_code in (400, 422):
                    dropped = _drop_unsupported(payload, resp.text)
                    if dropped:
                        logger.warning(
                            "模型不认参数 `%s`，摘掉后重试一次：%s",
                            dropped, resp.text[:120],
                        )
                        continue

                if resp.status_code >= 400:
                    raise AIError(f"HTTP {resp.status_code}: {resp.text[:300]}")

                data = resp.json()
                return self._pick_content(data)

            except (httpx.HTTPError, AIError, ValueError, KeyError) as e:
                last_err = e
                # 网络类错误只多试一次，且用 1.5 秒短退避。
                # ⚠️ 不要沿用 429 那套指数退避（5→10→20）：连接被拒、域名解析失败
                #    都不是「过一会儿就好」。实测 4 次重试会让「测试连接」转 13 秒，
                #    用户只会以为界面卡死了。
                if retry and attempt < 1:
                    logger.warning("AI 调用失败（第 %d 次）：%s", attempt + 1, e)
                    await asyncio.sleep(1.5)
                    continue
                break

        # ⚠️ httpx 的超时类异常 `str()` 是**空字符串**。
        #    直接 `f"…：{last_err}"` 会得到「AI 调用最终失败：」后面一片空白，
        #    界面上就是一句没头没尾的话（实测黑洞网关，probe_llm_model 5d）。
        #    补上类型名和「等了多少秒」，用户才知道是超时还是被拒。
        reason = str(last_err).strip()
        if not reason:
            wait = timeout or self.settings.ai_timeout
            reason = (f"{type(last_err).__name__ if last_err else '未知错误'}："
                      f"{wait:.0f} 秒内没有响应（网关不可达，或该服务只允许校内网访问）")
        raise AIError(f"AI 调用最终失败：{reason}")

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


async def list_remote_models(
    endpoint: str, api_key: str, timeout: float = 15.0
) -> dict[str, Any]:
    """问服务商「你有哪些模型」，给界面上的模型名做个下拉。

    为什么值得单独写：学校网关列出来 25 个模型（`GLM-4.6-W8A8`
    `DeepSeek-V3.1-W8A8` 这种带量化后缀的 id），让人手打是让人猜。
    猜错了只会得到 404 model not found，而界面上看不出是「名字错」还是「地址错」。

    这里只做一件事：把 `/chat/completions` 换成 `/models`，带同一个 Key 去 GET。
    失败**不抛异常**，把原因写在 `detail` 里返回 —— 下拉拿不到不该阻断「保存」，
    用户完全可以自己填模型名。
    """
    cfg_url = (endpoint or "").strip()
    if not cfg_url:
        return {"ok": False, "items": [], "endpoint": "", "detail": "没填接入地址"}
    root = cfg_url.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/messages", "/responses"):
        if root.lower().endswith(tail):
            root = root[: -len(tail)]
            break
    models_url = root.rstrip("/") + "/models"

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), trust_env=not is_local_host(models_url)
        ) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code in (401, 403):
                # 少数网关只认 Anthropic 那个头
                resp = await client.get(
                    models_url, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                )
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "items": [], "endpoint": models_url, "detail": f"{type(e).__name__}: {e}"}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "items": [],
            "endpoint": models_url,
            "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "items": [], "endpoint": models_url, "detail": "返回的不是 JSON"}

    raw = data.get("data") if isinstance(data, dict) else None
    if raw is None and isinstance(data, dict):
        raw = data.get("models")  # Anthropic 风格
    items: list[str] = []
    for row in raw or []:
        if isinstance(row, dict):
            mid = row.get("id") or row.get("name") or row.get("model")
            if isinstance(mid, str) and mid.strip():
                items.append(mid.strip())
        elif isinstance(row, str) and row.strip():
            items.append(row.strip())
    items = sorted(set(items), key=lambda x: x.lower())
    if not items:
        return {
            "ok": False,
            "items": [],
            "endpoint": models_url,
            "detail": "拿到了列表但里面没有模型 id（这个网关可能关掉了 /models）",
        }
    return {"ok": True, "items": items, "endpoint": models_url, "detail": ""}


async def check_ai(
    settings: Settings | None = None,
    llm: LLMConfig | None = None,
    *,
    timeout: float | None = None,
    retry: bool = True,
) -> dict[str, Any]:
    """给 `/api/health`、`/api/llm/test` 和 `scripts/check_ai.py` 用的连通性自检。

    返回里带上 name / source / endpoint，是因为「配了但没生效」最难查 ——
    用户以为是 key 的问题，实际是地址拼错了。把最终请求地址回显出来就能自证。

    ⚠️ 界面上的「测试连接」必须传 `timeout=15, retry=False`。
    默认的 60 秒 × 2 次尝试，在校外访问校内网关时会让按钮转 2 分钟，
    用户分不清是「在等」还是「卡死了」。
    """
    s = settings or get_settings()
    cfg = llm or get_active(s)
    client = AIClient(s, cfg)
    if not client.enabled:
        return {"enabled": False, "ok": False, "detail": "未配置 API Key，将使用本地规则摘要"}
    try:
        text = await client.chat(
            "你是测试助手，只回复 OK。", "回复 OK",
            max_tokens=16, timeout=timeout, retry=retry,
        )
        return {
            "enabled": True,
            "ok": True,
            "name": cfg.name,
            "model": cfg.model,
            "source": cfg.source,
            "endpoint": cfg.endpoint,
            "reply": text.strip()[:50],
        }
    except Exception as e:  # noqa: BLE001
        # httpx 的 ReadTimeout 的 str() 是**空字符串** —— 界面会显示
        # 「失败：AI 调用最终失败：」后面什么都没有，用户完全不知道发生了什么。
        # 实测（probe_llm_model 5d，黑洞网关）就是这么一片空白。
        detail = str(e).strip()
        if not detail:
            wait = timeout or s.ai_timeout
            detail = (f"{type(e).__name__}：{wait:.0f} 秒内没有响应"
                      f"（网关不可达，或该服务只允许校内网访问）")
        return {
            "enabled": True,
            "ok": False,
            "name": cfg.name,
            "model": cfg.model,
            "source": cfg.source,
            "endpoint": cfg.endpoint,
            "detail": detail[:300],
        }
