"""运行时大模型配置：把「.env 默认」和「用户自建 profile」统一成一份 LLMConfig。

设计要点（都是为了不骗人）：
  - **回落链只有一条**：库里没有激活的 profile → 用 .env。所以在界面上「切回默认」
    一定拿到的就是你原来那份配置，不会出现「切了但不知道在用谁」。
  - **不猜模型能力，猜了就兜底**：自定义服务的 base_url 千奇百怪，`/v1` 要不要补、
    `thinking` 认不认、`response_format` 支不支持，全都不确定。所以
    `normalize_endpoint()` 只做可预期的拼接，真正的兼容性交给
    `AIClient` 里的「参数不兼容就摘掉重试」兜底。
  - **缓存是进程级的**：桌面版是单进程，够用。多 worker 部署时每个进程的缓存不同步，
    要一致性就得每次查库（见 `reload_active()`）。

⚠️ 为什么需要缓存而不是每个请求查库：`summarize.py` 在后台任务里跑，
   拿不到请求级的 AsyncSession；让它为了取一个模型名去开 session 不值得。
   所以配置在启动时加载进内存，界面改完调 `reload_active()` 刷新。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 打码标记。前端把带这个标记的 key 原样回传时，视为「没改过」，后端保留原值。
MASK = "****"
DEFAULT_PROFILE_NAME = "默认"


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LLMConfig:
    """一份可用的模型接入配置。AIClient 只认它，不关心它从哪儿来。"""

    name: str
    base_url: str
    api_key: str
    model: str
    thinking: bool = False
    json_mode: bool = True
    source: str = "env"           # env | profile
    profile_id: int | None = None

    @property
    def enabled(self) -> bool:
        """有 key 才叫能用。没有就老老实实降级到本地规则，不假装。"""
        return bool((self.api_key or "").strip())

    @property
    def endpoint(self) -> str:
        return normalize_endpoint(self.base_url)

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)

    @property
    def label(self) -> str:
        """界面上显示的名字，如「默认 · glm-4.7-flash」。"""
        if not self.model:
            return self.name
        return f"{self.name} · {self.model}"


def config_from_settings(settings: Settings | None = None) -> LLMConfig:
    """.env 里那套，作为「默认」选项。"""
    s = settings or get_settings()
    return LLMConfig(
        name=DEFAULT_PROFILE_NAME,
        base_url=s.ai_base_url,
        api_key=s.ai_api_key,
        model=s.ai_model,
        thinking=s.ai_thinking,
        json_mode=s.ai_use_json_mode,
        source="env",
        profile_id=None,
    )


def config_from_profile(row) -> LLMConfig:
    """ORM 行 → LLMConfig。行对象只需有那几个属性，测试里可以塞简单对象。"""
    return LLMConfig(
        name=row.name or "自定义",
        base_url=row.base_url or "",
        api_key=row.api_key or "",
        model=row.model or "",
        thinking=bool(row.thinking),
        json_mode=bool(row.json_mode),
        source="profile",
        profile_id=getattr(row, "id", None),
    )


# --------------------------------------------------------------------------- #
# 地址与 key 的规整
# --------------------------------------------------------------------------- #
_VERSION_TAIL = re.compile(r"/v\d+$", re.IGNORECASE)


def normalize_endpoint(base_url: str) -> str:
    """把用户填的接入地址补成完整的 chat/completions 地址。

    用户填的东西长这样（都是真实存在的写法）：
        https://api.deepseek.com                    → .../v1/chat/completions
        https://api.deepseek.com/v1                 → .../v1/chat/completions
        https://open.bigmodel.cn/api/paas/v4        → .../api/paas/v4/chat/completions
        https://x.com/v1/chat/completions           → 原样
        api.moonshot.cn                             → https://api.moonshot.cn/v1/chat/completions

    规则只有三条，宁可少猜：以 /chat/completions 结尾就原样；以版本号结尾就补
    /chat/completions；其余补 /v1/chat/completions。补错了怎么办 ——
    测试连接按钮会把**最终实际请求的地址**显示出来，一眼能看出错在哪。
    """
    url = (base_url or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    low = url.lower()
    if low.endswith("/chat/completions"):
        return url

    path = urlsplit(url).path.rstrip("/")
    if not path:
        return url + "/v1/chat/completions"
    if _VERSION_TAIL.search(path):
        return url + "/chat/completions"
    return url + "/chat/completions"


def mask_key(key: str) -> str:
    """对外展示用。短 key 直接全遮，别露一半。"""
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return MASK
    return f"{text[:4]}{MASK}{text[-4:]}"


def is_masked(key: str) -> bool:
    """前端回传的是不是打码值（= 用户没改 key）。"""
    return MASK in (key or "")


def is_zhipu(endpoint: str) -> bool:
    """是不是智谱系 —— 决定能不能发 `thinking` 字段。

    GLM-4.7-Flash 是推理模型，不关 thinking 会把 max_tokens 吃光（docs/02 #52）。
    但别的服务商收到 `thinking` 会直接 400，所以不能一律发。
    """
    low = (endpoint or "").lower()
    return "bigmodel.cn" in low or "zhipu" in low or "z.ai" in low


# --------------------------------------------------------------------------- #
# 进程级缓存
# --------------------------------------------------------------------------- #
# 只缓存「用户自建的 profile」。env 那份**不缓存**，每次按调用方传进来的 settings 现算。
# 为什么这么分：settings 是个带 lru_cache 的单例，测试里会 monkeypatch 它的
# ai_api_key；如果连 env 配置也缓存成一份 dataclass 副本，改过的 settings 就再也
# 影响不到 AIClient —— 表现为「给 settings 设了 key，AIClient 还是说未配置」。
# 现算的成本为零（纯内存拼装，没有 I/O），没必要冒这个险。
_active_profile: LLMConfig | None = None


def get_active(settings: Settings | None = None) -> LLMConfig:
    """当前生效的配置：有激活的 profile 就是它，否则现算 .env 那份。"""
    if _active_profile is not None:
        return _active_profile
    return config_from_settings(settings)


def set_active(cfg: LLMConfig | None) -> LLMConfig | None:
    global _active_profile
    _active_profile = cfg
    if cfg is None:
        logger.info("生效模型配置已回落 .env 默认")
    else:
        logger.info("生效模型配置已切换为：%s（来源 %s）", cfg.label, cfg.source)
    return cfg


def reset_cache() -> None:
    """测试用：清掉缓存，下一次 get_active 回落 .env。"""
    global _active_profile
    _active_profile = None


async def reload_active(settings: Settings | None = None) -> LLMConfig:
    """从库里读激活的 profile；没有就回落 .env。启动时和改完配置后调用。"""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import LLMProfile

    s = settings or get_settings()
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(LLMProfile).where(LLMProfile.is_active.is_(True)).limit(1)
                )
            ).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001
        # 库还没建表 / 库损坏都不该让整个后端起不来，回落 .env 就是可用的。
        # 只打异常第一行：SQLAlchemy 的 str(e) 会带整条 SQL 和参数，日志里刷一屏。
        logger.warning("读取自定义模型配置失败，回落 .env：%s",
                       (str(e).splitlines() or [""])[0][:120])
        set_active(None)
        return config_from_settings(s)

    if row is None:
        set_active(None)
        return config_from_settings(s)
    return set_active(config_from_profile(row))
