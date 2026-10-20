"""推送通道与推送编排（阶段 3）。

## 为什么是「本地队列 + Webhook」而不是 FCM

现实约束（写在这里，免得以后有人以为是我们偷懒）：
  - **FCM**：国产 Android 普遍没有 Google 服务框架，装了也收不到；
    而且需要科学上网才能连 fcm.googleapis.com。对国内大学生用户等于不可用。
  - **厂商推送**（小米/华为/OPPO/vivo）：每个厂商都要开发者账号 + 应用上架审核，
    个人开发者基本拿不到；还需要各自的原生 SDK 配置。
  - **本地通知**：客户端自己定期拉、自己弹系统通知。零第三方依赖、离线可用、
    不需要任何审核。缺点是 App 被完全杀死期间收不到 —— 但这是**客户端平台限制**，
    不是实现偷懒，README 的「诚实的边界说明」里写清楚了。
  - **Webhook**：服务端把新通知 POST 到用户自己的地址。企业微信/钉钉机器人、
    Server酱、Bark(iOS) 全都是 webhook —— 这是国内**真的能用**的推送方式。

所以：`local` 是默认通道（一定能跑通），`webhook` 是可选增强，`fcm` 仅作占位。

## 一条通知什么时候算「该被推」

`PushService._eligible()` 里逐条判定，四个条件全过才推：
  1. `notified_at IS NULL` —— 没推过。**这是唯一的去重依据**，
     通知正文后续被更新时不会重置它，所以同一条不会被推第二遍。
     将来若要做「内容有实质更新则重推」，要另外加字段，别动这个。
  2. `ai_status == 'done'` —— 摘要已就绪，推出去的内容才带 3 行摘要
     （没有 AI Key 时本地规则兜底也会把 ai_status 置为 done，
      所以这条不等于「必须有 AI」）。
  3. 发布时间落在 `push_lookback_hours` 窗口内 —— 防止两种情况：
     第一次开推送把库里几百条历史积压轰出去；
     某次抓取迟到后把一周前的老通知当新闻推给用户。
  4. 重要度不低于 `push_min_importance`。

## 已知取舍（老实写出来）

- **不做通道级重试**。一轮里有任一通道成功就把 `notified_at` 标记上。
  所以如果 webhook 临时挂了、而本地通道正常，那条 webhook 就不会再补发。
  做通道级重试需要额外的 per-(通知, 通道) 状态表，当前量级不值得。
- **免打扰时段只拦外发通道**。本地通知不拦 —— 它是用户自己拉取的，
  拦住反而会让用户第二天打开 App 时看不到当时的消息。
- **推送失败不阻断抓取**。推送是外部动作，挂了不能连累「通知已经入库」这个事实。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.timeutil import now_utc
from app.models import AppMeta, Notification, PushLog, Source
from app.schemas import IMPORTANCE_LABELS

logger = logging.getLogger(__name__)

# 重要度排序：比较用
_IMPORTANCE_RANK = {"normal": 0, "important": 1, "urgent": 2}


class PushError(Exception):
    """推送配置或发送出错。"""


# --------------------------------------------------------------------------- #
# 消息载体
# --------------------------------------------------------------------------- #


@dataclass
class PushMessage:
    """一条待推送的通知（已脱去 ORM，方便单独测试与序列化）。"""

    notification_id: int
    title: str
    lines: list[str]
    importance: str
    category: str
    source_name: str
    url: str | None = None
    published_at: Any = None

    @property
    def importance_label(self) -> str:
        return IMPORTANCE_LABELS.get(self.importance, "一般")

    def one_line(self) -> str:
        """压成一行，给标题栏用。"""
        return f"【{self.source_name}】{self.title}"

    def as_text(self) -> str:
        """多行纯文本（跳过空白行，避免通知里出现空行）。"""
        body = "\n".join(x for x in self.lines if x.strip())
        head = f"【{self.source_name}·{self.importance_label}】{self.title}"
        parts = [head]
        if body:
            parts.append(body)
        if self.url:
            parts.append(self.url)
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.notification_id,
            "title": self.title,
            "lines": list(self.lines),
            "importance": self.importance,
            "importance_label": self.importance_label,
            "category": self.category,
            "source_name": self.source_name,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "text": self.as_text(),
        }


@dataclass
class PushResult:
    ok: bool
    channel: str
    detail: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    #: 由 PushService 回填：这个通道成功是否代表「可以消费掉这条通知」。
    #: 见 PushChannel.consume。
    consume: bool = True


# --------------------------------------------------------------------------- #
# 通道注册表（与 adapters/registry.py 同一套风格）
# --------------------------------------------------------------------------- #

_CHANNELS: dict[str, type["PushChannel"]] = {}


def register_channel(kind: str):
    """类装饰器：把通道注册到指定 kind。"""

    def deco(cls: type["PushChannel"]) -> type["PushChannel"]:
        cls.kind = kind
        _CHANNELS[kind] = cls
        return cls

    return deco


def available_channels() -> list[str]:
    return sorted(_CHANNELS.keys())


def get_channel(kind: str, settings: Settings) -> "PushChannel":
    if kind not in _CHANNELS:
        raise PushError(f"未知推送通道 '{kind}'，可用：{', '.join(available_channels())}")
    return _CHANNELS[kind](settings)


class PushChannel:
    """通道基类。子类只需实现 `send()`，并声明两个布尔属性。

    刻意把「该不该推」的逻辑留在 PushService，通道只负责「怎么送出去」。
    否则每加一个通道就要重抄一遍阈值/免打扰判断。
    """

    kind: str = "base"
    #: 是否属于「外发到第三方」（免打扰时段只拦这一类）
    is_external: bool = False
    #: 成功发送后是否**消费**掉这条通知（把 notified_at 标上）。
    #:
    #: ⚠️ 这是本模块最关键的一个区分，搞错会让功能看起来正常但实际收不到：
    #:   - 外发通道（webhook/fcm）—— `consume = True`。
    #:     消息真的送到你手机/群里了，就该把它标成「已处理」，否则每轮都重发。
    #:   - 本地通道 —— `consume = **False**`。
    #:     它的「发送」只是把消息留在队列里等客户端来拉。
    #:     如果它也标 notified_at，队列当场就被清空了，
    #:     客户端再调 `/push/pending` 什么都拿不到 —— 即「推送静默失效」。
    consume: bool = True
    #: 一句人话说明，给 /push/channels 展示
    note: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        """通道是否配置完整、可以真正发送。"""
        return True

    async def send(self, msg: PushMessage) -> PushResult:  # pragma: no cover - 抽象
        raise NotImplementedError


# ---- 通道 1：本地队列 ------------------------------------------------------- #


@register_channel("local")
class LocalChannel(PushChannel):
    """本地通知队列。

    它「发送」的动作就是让这条消息留在库里等着被客户端拉走
    （`GET /api/push/pending`），所以 send() 一定成功。
    客户端拉到后弹系统通知，再调 `POST /api/push/ack` 确认。

    ⚠️ 反转认知的一点：本地通道不是「没实现推送」的占位。
    对国内 Android 来说，它是**唯一稳定可用**的通道。
    """

    is_external = False
    # ★ 本地通道**不消费**队列 —— 见 PushChannel.consume 的说明。
    consume = False
    note = "本地通知：消息留在队列，由客户端拉取后弹系统通知（零依赖，离线可用）"

    async def send(self, msg: PushMessage) -> PushResult:
        return PushResult(ok=True, channel=self.kind, detail="已进入本地队列")


# ---- 通道 2：Webhook -------------------------------------------------------- #


def _is_loopback(url: str) -> bool:
    """判断是否指向本机。

    ⚠️ 本机联调时必须绕开系统代理：`HTTP_PROXY` 会把 127.0.0.1 也拦下来，
    表现为 502「后端没起来」，其实是被代理吞了（这个坑在 docs/02 里有记）。
    外部地址（企业微信等）则要**保留**代理，否则在需要代理的网络里发不出去。
    """
    lowered = (url or "").lower()
    return any(
        lowered.startswith(p) for p in ("http://127.0.0.1", "http://localhost", "http://[::1]")
    )


def _build_webhook_payload(kind: str, msg: PushMessage) -> tuple[dict[str, Any], str]:
    """返回 (载荷, content_type)。

    支持国内真正在用的几种机器人格式。加新格式就在这个 if 链里加一支 ——
    都在同一个小函数里，改起来一眼能看全。
    """
    text = msg.as_text()

    if kind == "wecom":
        # 企业微信群机器人：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
        lines = "\n".join(f"> {x}" for x in msg.lines if x.strip())
        content = f"### {msg.one_line()}\n{lines}"
        if msg.url:
            content += f"\n[查看原文]({msg.url})"
        return {"msgtype": "markdown", "markdown": {"content": content}}, "application/json"

    if kind == "dingtalk":
        # 钉钉自定义机器人：https://oapi.dingtalk.com/robot/send?access_token=xxx
        lines = "\n\n".join(f"- {x}" for x in msg.lines if x.strip())
        content = f"### {msg.one_line()}\n\n{lines}"
        if msg.url:
            content += f"\n\n[查看原文]({msg.url})"
        return (
            {"msgtype": "markdown", "markdown": {"title": msg.one_line(), "text": content}},
            "application/json",
        )

    if kind == "serverchan":
        # Server酱 Turbo：https://sctapi.ftqq.com/<SENDKEY>.send  走表单
        body = "\n\n".join(x for x in msg.lines if x.strip())
        if msg.url:
            body += f"\n\n[查看原文]({msg.url})"
        return {"title": msg.one_line(), "desp": body}, "application/x-www-form-urlencoded"

    if kind == "bark":
        # Bark(iOS)：https://api.day.app/<KEY>  这里 POST 到填的地址本身
        return (
            {
                "title": msg.one_line(),
                "body": "\n".join(x for x in msg.lines if x.strip()),
                "group": "知了",
                "url": msg.url or "",
            },
            "application/json",
        )

    # generic：本项目自定义的通用格式，方便对接自建服务
    return (
        {
            "app": "知了",
            "event": "notification.new",
            "notification": msg.as_dict(),
            "title": msg.one_line(),
            "text": text,
        },
        "application/json",
    )


@register_channel("webhook")
class WebhookChannel(PushChannel):
    """把新通知 POST 到用户自己填的地址。

    一个通道覆盖企业微信 / 钉钉 / Server酱 / Bark / 自建服务，
    靠 `push_webhook_kind` 切换载荷格式。
    """

    is_external = True
    note = "Webhook：把新通知 POST 到你填的地址（企业微信/钉钉机器人、Server酱、Bark、自建服务）"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool((self.settings.push_webhook_url or "").strip())

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            url = self.settings.push_webhook_url
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.push_webhook_timeout),
                follow_redirects=True,
                # 见 _is_loopback 的说明
                trust_env=not _is_loopback(url),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, msg: PushMessage) -> PushResult:
        url = (self.settings.push_webhook_url or "").strip()
        if not url:
            return PushResult(False, self.kind, error="未配置 PUSH_WEBHOOK_URL")

        payload, content_type = _build_webhook_payload(self.settings.push_webhook_kind, msg)
        started = time.perf_counter()
        last_err: str | None = None
        attempts = max(1, int(self.settings.push_retries) + 1)

        for i in range(attempts):
            try:
                client = await self._get_client()
                if content_type == "application/x-www-form-urlencoded":
                    resp = await client.post(url, data=payload)
                else:
                    resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    # 把响应体前 200 字带出来 —— 企业微信/钉钉失败原因都在 body 里，
                    # 只报状态码（比如一句 400）根本查不出来。
                    body = resp.text[:200].replace("\n", " ")
                    raise PushError(f"HTTP {resp.status_code}: {body}")
                # 这类机器人常常「HTTP 200 + 业务错误」，必须看 body
                if _webhook_business_error(resp):
                    raise PushError(f"接口返回业务错误：{resp.text[:200]}")
                return PushResult(
                    True, self.kind, detail=f"HTTP {resp.status_code}",
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"[:300]
                if i < attempts - 1:
                    wait = 1.0 * (2 ** i)
                    logger.warning("Webhook 推送失败，%.0fs 后重试（%d/%d）：%s",
                                   wait, i + 1, attempts - 1, last_err)
                    await asyncio.sleep(wait)

        return PushResult(
            False, self.kind, error=last_err,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _webhook_business_error(resp: httpx.Response) -> bool:
    """识别「HTTP 200 但业务失败」的响应。

    ⚠️ 这是个很容易漏的坑：企业微信返回 200 且 body 是
    `{"errcode": 93000, "errmsg": "invalid webhook url"}`；
    钉钉是 `{"errcode": 300001, ...}`；
    Server酱是 `{"code": 40001}`。
    只看 HTTP 状态码会把这些统统当成功，用户则「一直收不到又不知道为什么」。
    """
    if "json" not in (resp.headers.get("content-type") or "").lower():
        return False
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    for key in ("errcode", "code"):
        if key in data:
            try:
                return int(data[key]) != 0
            except (TypeError, ValueError):
                continue
    return False


# ---- 通道 3：FCM（占位，未验证）--------------------------------------------- #


@register_channel("fcm")
class FcmChannel(PushChannel):
    """Firebase Cloud Messaging —— **未验证的占位实现**。

    ⚠️ 老实说清楚：
      1. 本机没有 GCP 凭据，这段代码**从未真实发送成功过**，
         不能算「已验证可用」，只能算「按协议写好了」。
      2. 国内绝大多数 Android 手机没有 Google 服务框架，即使发送成功，
         设备侧「大概率收不到」。这不是实现问题，是生态问题。
      3. 启用它必须先在 GCP 建项目、拿服务账号，再用 OAuth2 换 access token
         （这里只接受现成的 token，不内置私钥签名，避免把证书塞进配置）。

    保留它的唯一目的是：接入文档留个位置，将来真有 GMS 用户时不必从零写。
    """

    is_external = True
    note = "FCM：⚠️ 未验证（本机无 GCP 凭据）；国内无 GMS 手机大概率收不到，仅作预留"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._token = getattr(settings, "push_fcm_access_token", "") or ""
        self._project = getattr(settings, "push_fcm_project_id", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._project)

    async def send(self, msg: PushMessage) -> PushResult:
        endpoint = (
            f"https://fcm.googleapis.com/v1/projects/{self._project}/messages:send"
        )
        payload = {
            "message": {
                "topic": getattr(self.settings, "push_fcm_topic", "quanzhi") or "quanzhi",
                "notification": {"title": msg.one_line(), "body": msg.lines[0] if msg.lines else ""},
                "data": {k: str(v) for k, v in msg.as_dict().items() if k != "lines"},
            }
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            if resp.status_code >= 400:
                return PushResult(False, self.kind,
                                  error=f"HTTP {resp.status_code}: {resp.text[:200]}")
            return PushResult(
                True, self.kind, detail="HTTP 200（未验证通道）",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except Exception as e:  # noqa: BLE001
            return PushResult(False, self.kind, error=f"{type(e).__name__}: {e}"[:300])


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #


def _three_lines(row: Notification) -> list[str]:
    """取 3 行摘要。

    summarize.py 已经保证「正好 3 行」（含本地规则兜底），
    这里只做**防御性**归一：万一历史数据或手工导入的行没有摘要，
    也要让推送内容有东西可显示，绝不能推一条空白出去。
    """
    lines = [str(x).strip() for x in (row.summary_lines or []) if str(x).strip()]
    if not lines:
        raw = (row.excerpt or row.content_text or row.title or "").strip()
        import re

        chunks = [c.strip() for c in re.split(r"[。！？\n；;]", raw) if c.strip()]
        lines = chunks[:3] or [(row.title or "（无标题）").strip()]
    while len(lines) < 3:
        lines.append("")
    return lines[:3]


def _effective_time(row: Notification):
    """条目的「时间」——优先用发布时间，没有就用入库时间。"""
    return row.published_at or row.created_at


class PushService:
    """判定「谁该被推」并把消息分发给各通道。"""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ---- 通道 ----
    def build_channels(self) -> tuple[list[PushChannel], list[str]]:
        """构造本次要用的通道。返回 (可用通道, 警告信息)。"""
        warnings: list[str] = []
        out: list[PushChannel] = []
        for name in self.settings.push_channel_list():
            try:
                ch = get_channel(name, self.settings)
            except PushError as e:
                warnings.append(str(e))
                continue
            if not ch.enabled:
                warnings.append(f"通道 {name} 已配置但未生效（缺参数），本次跳过")
                continue
            out.append(ch)
        return out, warnings

    def channels_report(self) -> list[dict[str, Any]]:
        """给 /push/channels 用的体检报告。"""
        enabled_names = set(self.settings.push_channel_list())
        report = []
        for kind in available_channels():
            ch = get_channel(kind, self.settings)
            report.append(
                {
                    "kind": kind,
                    "selected": kind in enabled_names,
                    "ready": ch.enabled,
                    "external": ch.is_external,
                    "consumes_queue": ch.consume,
                    "note": ch.note,
                }
            )
        return report

    # ---- 候选筛选 ----
    def _eligible_clause(self):
        """把「该不该推」的判定**下推到 SQL**，返回一组 where 条件。

        ⚠️ 为什么必须下推而不是只在 Python 里筛：`since_id` 游标模式下我们按
        id **升序**取前 N 条。如果取回来才发现都不合格（超出时间窗），
        客户端就拿不到任何消息、游标也就永远不前进 —— 卡死在那一批老条目上。
        SQL 层和 `_eligible()` 必须给出**完全一致**的答案，否则就会卡游标。
        """
        from sqlalchemy import func, or_

        cutoff = now_utc() - timedelta(hours=max(0, self.settings.push_lookback_hours))
        want = _IMPORTANCE_RANK.get(self.settings.push_min_importance, 0)
        allowed = [k for k, v in _IMPORTANCE_RANK.items() if v >= want]

        # 生效时间：有发布时间用发布时间，没有就用入库时间（与 _effective_time 一致）
        effective = func.coalesce(Notification.published_at, Notification.created_at)
        return [
            effective >= cutoff,
            or_(Notification.importance.is_(None), Notification.importance.in_(allowed)),
        ]

    async def _candidates(self, limit: int, *, force: bool) -> list[Notification]:
        stmt = select(Notification).where(
            Notification.notified_at.is_(None),
            Notification.ai_status == "done",
        )
        if not force:
            stmt = stmt.where(*self._eligible_clause())
        stmt = stmt.order_by(
            Notification.published_at.desc().nulls_last(),
            Notification.id.desc(),
        )
        rows = list((await self.session.execute(stmt.limit(max(1, limit)))).scalars().all())
        if force:
            return rows
        # 双保险：SQL 与 Python 判定理论上一致，真不一致时以 Python 为准并告警。
        kept = []
        for r in rows:
            ok, why = self._eligible(r)
            if ok:
                kept.append(r)
            else:
                logger.warning(
                    "推送判定不一致（SQL 通过 / Python 否决）：id=%s 原因=%s —— "
                    "检查 _eligible_clause 是否与 _eligible 同步", r.id, why,
                )
        return kept

    def _eligible(self, row: Notification) -> tuple[bool, str]:
        # 库里统一存 naive UTC（见 core/timeutil.py），所以这里直接比，不碰时区。
        cutoff = now_utc() - timedelta(hours=max(0, self.settings.push_lookback_hours))
        t = _effective_time(row)
        if t is not None and t < cutoff:
            return False, "超出推送时间窗"
        want = _IMPORTANCE_RANK.get(self.settings.push_min_importance, 0)
        if _IMPORTANCE_RANK.get(row.importance or "normal", 0) < want:
            return False, "重要度低于阈值"
        return True, ""

    def in_quiet_hours(self) -> bool:
        rng = self.settings.push_quiet_range()
        if rng is None:
            return False
        start, end = rng
        try:
            from zoneinfo import ZoneInfo

            local = now_utc().astimezone(ZoneInfo(self.settings.timezone))
        except Exception:  # noqa: BLE001
            local = now_utc()
        cur = local.hour * 60 + local.minute
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        # 跨零点，如 23:00-07:00
        return cur >= start or cur < end

    # ---- 主流程 ----
    async def push_pending(
        self, *, limit: int | None = None, force: bool = False
    ) -> dict[str, Any]:
        """把待推送的通知推出去。返回统计（会写库）。"""
        started = time.perf_counter()
        stats: dict[str, Any] = {
            "checked": 0,
            # sent/marked：外发通道真的送达了，且这条已被消费（不会再推）
            "sent": 0,
            "marked": 0,
            # queued：只进了本地队列，等客户端来拉（notified_at 仍为 NULL）
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "channels": [],
            "warnings": [],
            "baseline": False,
        }

        if not self.settings.push_enabled:
            stats["warnings"].append("PUSH_ENABLED=false，推送未启用")
            return stats

        channels, warns = self.build_channels()
        stats["warnings"].extend(warns)
        stats["channels"] = [c.kind for c in channels]
        if not channels:
            stats["warnings"].append("没有任何可用通道，推送跳过")
            return stats

        # ⚠️ 这里**故意不**做「首次静默基线」。基线是在应用启动时做的
        #    （见 main.py 的 lifespan），原因：
        #    抓取和推送是同一次 job 里的前后两步。如果在 push_pending 里做基线，
        #    它会顺手把**这一次刚抓到的新通知**也一起标记成「无需推送」——
        #    用户永远收不到自己刚订阅上那天的新消息，而且现象是「推送没反应」，
        #    极难联想到是基线吞的。放到启动时做，那时库里只有历史数据。
        rows = await self._candidates(limit or self.settings.push_batch_limit, force=force)
        stats["checked"] = len(rows)

        # 免打扰：只拦外发通道
        quiet = self.in_quiet_hours()
        live = [c for c in channels if not (quiet and c.is_external)]
        held = [c.kind for c in channels if quiet and c.is_external]
        if held:
            stats["warnings"].append(
                f"处于免打扰时段 {self.settings.push_quiet_hours}，暂缓外发通道：{', '.join(held)}"
            )

        for row in rows:
            msg = await self._to_message(row)
            results: list[PushResult] = []
            for ch in live:
                try:
                    res = await ch.send(msg)
                except Exception as e:  # noqa: BLE001
                    res = PushResult(False, ch.kind, error=f"{type(e).__name__}: {e}"[:300])
                # 把通道记在结果上，后面判断「消费型通道成功了吗」要用
                res.consume = ch.consume
                results.append(res)
                self.session.add(
                    PushLog(
                        notification_id=row.id,
                        dedup_key=row.dedup_key,
                        channel=ch.kind,
                        status="ok" if res.ok else "failed",
                        title=msg.title[:200],
                        detail=res.detail,
                        error=res.error,
                        duration_ms=res.duration_ms,
                    )
                )

            # ---- 标记逻辑（决定这条还会不会被再推 / 再被客户端拉到）----
            # 只有**消费型**通道成功，才把 notified_at 标上。
            # 本地通道成功不算 —— 它只是把消息留在队列里等客户端来拉，
            # 标上就等于把队列清空了，客户端再也拉不到（推送静默失效）。
            consuming_ok = any(r.ok for r in results if r.consume)
            queued = any(r.ok for r in results if not r.consume)

            if consuming_ok:
                row.notified_at = now_utc()
                row.notify_error = None
                stats["sent"] += 1
                stats["marked"] += 1
            elif queued:
                # 仅进了本地队列：保持 notified_at 为 NULL，等客户端拉取。
                # 这不计入 sent（没真正送达任何地方），也不计入 failed。
                stats["queued"] += 1
            else:
                # 全通道失败：**不**标记，下一轮会重试。
                errs = "; ".join(f"{r.channel}:{r.error}" for r in results if r.error)
                row.notify_error = errs[:500] or "全部通道发送失败"
                stats["failed"] += 1
                logger.warning("推送全部失败 notification#%s：%s", row.id, row.notify_error)

            # 收尾：把每个通道的 client 关掉（只有 webhook 会持有连接池）
            for ch in live:
                closer = getattr(ch, "aclose", None)
                if closer:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001
                        pass

        await self.session.commit()
        stats["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return stats

    async def _to_message(self, row: Notification) -> PushMessage:
        source_name = ""
        if row.source_id:
            src = await self.session.get(Source, row.source_id)
            if src:
                source_name = src.name or ""
        return PushMessage(
            notification_id=row.id,
            title=(row.title or "").strip() or "（无标题）",
            lines=_three_lines(row),
            importance=row.importance or "normal",
            category=row.category or "其他",
            source_name=source_name or "未知来源",
            url=row.url,
            published_at=row.published_at,
        )

    # ---- 首次基线 ----
    async def ensure_baseline(self) -> dict[str, Any]:
        """首次启用推送时对齐基线（幂等）。

        **只在应用启动时调用**（main.py 的 lifespan），理由见 push_pending 里的说明。

        做法：把当前所有 `notified_at IS NULL` 的历史条目标记为已通知，但不真发。
        之后只有新抓到的才会推。
        """
        key = "push_baseline_at"
        existing = await self.session.get(AppMeta, key)
        if existing is not None:
            return {"done": False, "marked": 0}

        from sqlalchemy import update

        result = await self.session.execute(
            update(Notification)
            .where(Notification.notified_at.is_(None))
            .values(notified_at=now_utc())
        )
        total = int(result.rowcount or 0)
        self.session.add(AppMeta(key=key, value=f"{now_utc().isoformat()}|{total}"))
        await self.session.commit()
        logger.info("推送基线已对齐：%d 条历史通知标记为无需推送", total)
        return {"done": True, "marked": total}

    # ---- 手动/测试 ----
    async def send_test(self, text: str = "知了推送通道自检") -> list[PushResult]:
        """给所有可用通道发一条测试消息（不落 notifications 表）。"""
        channels, _ = self.build_channels()
        msg = PushMessage(
            notification_id=0,
            title=text,
            lines=[
                f"发送时间 {now_utc().isoformat(timespec='seconds')}",
                f"通道 {', '.join(c.kind for c in channels) or '（无）'}",
                "如果你看到了这条，说明推送通道配置正确。",
            ],
            importance="normal",
            category="系统",
            source_name="知了自检",
            url=None,
            published_at=now_utc(),
        )
        results: list[PushResult] = []
        for ch in channels:
            try:
                results.append(await ch.send(msg))
            except Exception as e:  # noqa: BLE001
                results.append(PushResult(False, ch.kind, error=f"{type(e).__name__}: {e}"[:300]))
            closer = getattr(ch, "aclose", None)
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        return results

    # ---- 客户端拉取 ----
    async def pending_messages(
        self, limit: int = 20, *, since_id: int | None = None
    ) -> list[PushMessage]:
        """给客户端拉取的未推送消息。

        ## 两种取法，用途不同

        - **`since_id` 游标（推荐，多设备安全）**：客户端记住「我见过的最大 id」，
          每次问「比这个大还有哪些」。服务端**只读不写**，消息永远可重放。
          手机和电脑各自有各自的游标，互不抢。
        - **不带 `since_id`**：返回最新的若干条（按发布时间倒序）。
          适合「我就想看看现在排在前面的是什么」。

        ⚠️ 注意这些条目的 `notified_at` 是 NULL —— 本地通道不消费队列
        （见 PushChannel.consume），所以它们会一直留在这里，
        直到落了 48 小时时间窗。这是刻意的：客户端崩了也不会丢消息。
        """
        stmt = select(Notification).where(
            Notification.notified_at.is_(None),
            Notification.ai_status == "done",
            # ★ 与 _eligible 完全一致的 SQL 过滤，见 _eligible_clause 的说明
            *self._eligible_clause(),
        )
        if since_id is not None:
            # 游标模式：按 id 升序，保证不跳号（id 单调递增）
            stmt = stmt.where(Notification.id > int(since_id)).order_by(Notification.id.asc())
        else:
            stmt = stmt.order_by(
                Notification.published_at.desc().nulls_last(), Notification.id.desc()
            )
        stmt = stmt.limit(max(1, min(limit, 200)))

        rows = list((await self.session.execute(stmt)).scalars().all())
        return [await self._to_message(r) for r in rows]

    async def max_notification_id(self) -> int:
        """当前最大通知 id。客户端用它初始化游标 —— 这样首次启用时
        不会把库里的历史通知一次性全弹出来。"""
        from sqlalchemy import func

        return int(
            (await self.session.execute(select(func.max(Notification.id)))).scalar() or 0
        )

    async def ack(self, ids: list[int]) -> int:
        """客户端确认已收到。幂等：重复 ack 不会改变结果。"""
        if not ids:
            return 0
        from sqlalchemy import update

        result = await self.session.execute(
            update(Notification)
            .where(Notification.id.in_(ids), Notification.notified_at.is_(None))
            .values(notified_at=now_utc())
        )
        await self.session.commit()
        return int(result.rowcount or 0)

    async def stats(self) -> dict[str, Any]:
        """给 /push/config 与 /health 用的概览。"""
        from sqlalchemy import func

        unsent = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.notified_at.is_(None),
                        Notification.ai_status == "done",
                    )
                )
            ).scalar()
            or 0
        )
        baseline = await self.session.get(AppMeta, "push_baseline_at")
        failed = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(PushLog)
                    .where(PushLog.status == "failed")
                )
            ).scalar()
            or 0
        )
        return {
            "unsent": unsent,
            "baseline_at": (baseline.value.split("|")[0] if baseline else None),
            "baseline_count": (
                int(baseline.value.split("|")[1]) if baseline and "|" in baseline.value else 0
            ),
            "failed_logs": failed,
            "quiet_now": self.in_quiet_hours(),
        }


__all__ = [
    "FcmChannel",
    "LocalChannel",
    "PushChannel",
    "PushError",
    "PushMessage",
    "PushResult",
    "PushService",
    "WebhookChannel",
    "available_channels",
    "get_channel",
    "register_channel",
]
