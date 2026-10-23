"""全局配置。所有可调参数集中在这里，通过环境变量或 .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "知了 API"
    env: str = "dev"
    api_prefix: str = "/api"

    # ---- 数据库 ----
    # 默认 SQLite，开箱即跑；生产换成 postgresql+asyncpg://...
    # 注意用 as_posix()：Windows 下反斜杠会破坏 SQLite 的 URL 解析
    database_url: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'quanzhi.db').as_posix()}"

    # ---- 路径 ----
    sources_dir: Path = BASE_DIR / "sources"
    prompt_dir: Path = BASE_DIR / "app" / "prompts"

    # ---- AI ----
    ai_base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    ai_api_key: str = ""
    ai_model: str = "glm-4.7-flash"
    ai_timeout: float = 60.0
    ai_max_input_chars: int = 6000      # 送进模型的正文字数上限，控成本
    ai_temperature: float = 0.2
    # ⚠️ GLM-4.7-Flash 是**推理模型**，默认会先输出一大段思维链
    #    （响应里的 `reasoning_content`），而这段思维链**计入 max_tokens**。
    #    实测：max_tokens=1500 时思维链把额度吃光，`content` 返回空字符串、
    #    `finish_reason="length"`，表面报错是「模型未返回合法 JSON」。
    #    摘要属于「抽取」而非「推理」，关掉思考既省额度又快得多。
    ai_thinking: bool = False
    # 用 JSON 模式约束输出格式（智谱 OpenAI 兼容协议支持）
    ai_use_json_mode: bool = True

    # ---- 抓取合规 ----
    respect_robots: bool = True
    fetch_concurrency: int = 4
    fetch_min_interval: float = 2.0     # 同一域名两次请求的最小间隔（秒）；需求要求 ≥2s
    fetch_timeout: float = 20.0
    fetch_retries: int = 2              # 失败重试次数（指数退避）
    user_agent: str = "QuanZhiBot/0.1 (+contact: you@example.com)"

    # ---- 调度 ----
    timezone: str = "Asia/Shanghai"
    refresh_cron_hours: str = "8,18"
    refresh_cron_minute: int = 0
    enable_scheduler: bool = True

    # ---- 两条「静默失效」的兜底（2026-10-23 加）----
    # 进程内调度器 + 单次抓取，有两条路会安静地让数据停更，且界面上看不出来：
    #   ① 到点时本机 DNS / 网络抖一下 → 24 个来源全失败，一条都没进来
    #      （2026-10-23 18:00 真实发生：全部 getaddrinfo failed）
    #   ② 到点时程序没开 → APScheduler 是内存态，错过的时间点不会补跑
    # 下面两组分别堵这两条。
    refresh_retry_on_total_failure: bool = True
    refresh_retry_delay_seconds: int = 90
    refresh_catchup_on_start: bool = True
    # 距上次成功抓取超过这么多小时，启动时就补一轮；<=0 表示不按时间判断
    refresh_catchup_after_hours: float = 6.0

    # 抓完是否自动触发 AI 摘要
    auto_summarize: bool = True
    # 每次抓取最多送进 AI 的新条目数（防止一次性烧掉额度）
    summarize_batch_limit: int = 20

    # ---- 推送（阶段 3）----
    # ⚠️ 通道选型说明见 services/push.py 顶部与 README「诚实的边界说明」。
    #    默认只开 local（本地队列，客户端拉取后弹系统通知），零第三方依赖。
    #    webhook 需要配 URL 才自动生效。
    push_enabled: bool = True
    # 逗号分隔：local / webhook / fcm
    push_channels: str = "local"
    # Webhook 目标地址。企业微信、钉钉机器人、Server酱、Bark 的地址都往这里填。
    push_webhook_url: str = ""
    # 载荷格式：generic / wecom / dingtalk / serverchan / bark
    push_webhook_kind: str = "generic"
    push_webhook_timeout: float = 10.0
    # 只推这个时间窗**之内**发布的条目（小时）。这条规则同时承担两个职责：
    #   ① 防止第一次开推送时把库里几百条历史积压一次性轰出去；
    #   ② 防止某次抓取迟到后，把一周前的老通知当新闻推给用户。
    # 发布时间为空的条目用 created_at 兜底判断。
    push_lookback_hours: int = 48
    # 一次最多推几条，避免通道被刷爆
    push_batch_limit: int = 20
    # 只推不低于此重要度的条目：normal（全推）/ important / urgent
    push_min_importance: str = "normal"
    # 免打扰时段，如 "23:00-07:00"。留空 = 不启用。
    # ⚠️ 只拦**外发通道**（webhook）—— 本地通知是用户自己拉取的，
    #    拦它没有意义，反而会让用户第二天打开 App 时看不到当时的消息。
    push_quiet_hours: str = ""
    # 外发失败重试次数（指数退避）。耗尽后记 push_logs 但不阻断抓取。
    push_retries: int = 2
    # 首次启用推送时，把**已经躺在库里的历史条目**对齐成「无需推送」，只记一条基线，
    # 不真的发。理由：库里有 150+ 条历史通知，一开推送就全轰出去体验很糟。
    # 想手动把积压推一遍时用 POST /api/push/run?force=true（会绕过这个开关）。
    push_silent_first_run: bool = True

    # ---- FCM（预留，⚠️ 未验证）----
    # 国内无 Google 服务框架的机器大概率收不到；本机也没有 GCP 凭据，
    # 所以这段代码"写好了但从未真实发送成功过"。仅在确有 GMS 用户时启用。
    push_fcm_project_id: str = ""
    push_fcm_access_token: str = ""     # 需自行用服务账号换 OAuth2 token
    push_fcm_topic: str = "quanzhi"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key.strip())

    def refresh_cron(self) -> str:
        """生成 cron 表达式，如 '0 8,18 * * *'"""
        return f"{self.refresh_cron_minute} {self.refresh_cron_hours} * * *"

    # ---- 推送辅助 ----
    def push_channel_list(self) -> list[str]:
        """解析 push_channels，返回去重后的通道名（保序）。"""
        out: list[str] = []
        for raw in self.push_channels.split(","):
            name = raw.strip().lower()
            if name and name not in out:
                out.append(name)
        return out

    def push_quiet_range(self) -> tuple[int, int] | None:
        """解析免打扰时段，返回 (起始分钟, 结束分钟)。格式非法则视为未启用。

        用「分钟数」而不是 (时,分) 元组，是为了让跨零点的区间能用一个
        统一的取模比较处理（如 23:00-07:00）。
        """
        text = (self.push_quiet_hours or "").strip()
        if not text or "-" not in text:
            return None
        left, _, right = text.partition("-")
        try:
            sh, sm = (int(x) for x in left.strip().split(":"))
            eh, em = (int(x) for x in right.strip().split(":"))
        except (ValueError, TypeError):
            return None
        if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
            return None
        return sh * 60 + sm, eh * 60 + em


@lru_cache
def get_settings() -> Settings:
    return Settings()
