"""AI 连通性自检。

    python scripts/check_ai.py

会真的发一次请求，把耗时和返回都打出来，方便确认 Key / 地址 / 模型名是否正确。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.ai_client import AIClient  # noqa: E402
from app.services.llm_config import reload_active  # noqa: E402


async def main() -> None:
    s = get_settings()
    # 先从库里读「当前激活的自定义模型」。不读的话这里打印的是 .env，
    # 而 AIClient 实际走的是 profile —— 会出现「脚本说 glm，实际跑 deepseek」这种假报告。
    cfg = await reload_active(s)
    print("=" * 66)
    print("知了 · AI 连通性自检")
    print("=" * 66)
    print(f"BASE_URL : {cfg.endpoint}")
    print(f"MODEL    : {cfg.model}")
    print(f"来源     : {'自定义「' + cfg.name + '」' if cfg.source == 'profile' else '.env 默认'}")
    print(f"API_KEY  : {'已配置（' + cfg.api_key[:6] + '…）' if cfg.enabled else '未配置'}")
    print("-" * 66)

    if not cfg.enabled:
        print("未配置 API Key。")
        print("后端仍可正常运行，摘要会自动降级为本地规则提取（LocalSummarizer）。")
        print("要接入大模型：在 backend/.env 里填 AI_API_KEY=你的Key")
        return

    client = AIClient(s, cfg)
    started = time.perf_counter()
    try:
        reply = await client.chat("你是一个测试助手，只回复 OK。", "回复 OK")
        cost = (time.perf_counter() - started) * 1000
        print(f"✅ 连通成功  耗时 {cost:.0f}ms")
        print(f"   返回：{reply.strip()[:100]}")
    except Exception as e:  # noqa: BLE001
        cost = (time.perf_counter() - started) * 1000
        print(f"❌ 调用失败  耗时 {cost:.0f}ms")
        print(f"   {e}")
        print("\n排查建议：")
        print("  1. 确认 AI_BASE_URL 是 https://open.bigmodel.cn/api/paas/v4/chat/completions")
        print("  2. 确认模型名与账号权限匹配（默认 glm-4.7-flash）")
        print("  3. 用 curl 直接试一次，排除网络/代理问题")
        return

    # ---- 关键一步：真实摘要必须能装下 ----
    # 免费档的输出上限比付费档小得多。如果 max_tokens 开太大，
    # 智谱会**静默截断**成半截 JSON，表现为「模型未返回合法 JSON」，
    # 很难查出真实原因。这里用真实 prompt 试一次，超长就告警。
    from app.services.prompts import get_prompt_library  # noqa: PLC0415

    rendered = get_prompt_library().render(
        "summarize",
        "v1",
        title="关于开展国家奖学金评审工作的通知",
        source_name="教务处",
        school="示例大学",
        published_at="2026-10-01 09:00",
        category_hint="奖助学金",
        content=(
            "请符合条件的同学于2026年10月15日17:00前将申请材料提交至所在学院学生工作办公室"
            "（逸夫楼305）。需提交的材料包括：国家奖学金申请表、上一学年成绩单原件、"
            "获奖证明复印件。联系人：李老师，联系电话：010-62781234。逾期不再受理。"
        ),
    )
    system, user = get_prompt_library().split_system_user(rendered)
    print("-" * 66)
    print("真实摘要预演（max_tokens=1500）…")
    try:
        payload = await client.chat_json(system, user, max_tokens=1500)
        lines = payload.get("summary_lines") or []
        print(f"✅ 输出合法 JSON，summary_lines = {len(lines)} 行")
        for i, line in enumerate(lines, 1):
            print(f"   [{i}] {str(line)[:60]}")
        if len(lines) < 3:
            print("⚠️  不足 3 行！很可能是 max_tokens 被免费档上限截断，")
            print("    请把 summarize 调用的 max_tokens 调小（如 800）再试。")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 摘要预演失败：{e}")
        print("   若报「模型未返回合法 JSON」，多半是被截断了 —— 调小 max_tokens 重试。")


if __name__ == "__main__":
    asyncio.run(main())
