"""验证「定时抓取」是真的会跑，还是只注册了个壳。

为什么要这个脚本
----------------
日志里能看到的 `定时任务已注册：每天 8,18 点` 只证明 APScheduler 收下了这个 job，
不证明到点真的会执行。要让「知了每天自动更新」这句话站得住，必须看到一次真实触发。

做法
----
1. 真实读取 backend/.env 里的 REFRESH_CRON_HOURS，打印等价 cron 表达式；
2. 把库复制一份到 logs/_diag 下（绝不碰你的 quanzhi.db）；
3. 把触发时间临时改成「下一分钟」，走真实的 start_scheduler() + 真实 CronTrigger；
4. 只把抓取与推送替换成桩（抓取本身已由 264 条数据与 e2e 脚本证明，
   这里要验的是「到点会不会调 job」）；
5. 等到点，断言 job 状态为 ok 且调用链是 抓取 → 推送。

用法
----
    python scripts/verify_schedule.py
    python scripts/verify_schedule.py --db quanzhi.db --timeout 80

进度同时写 logs/_diag/sched_trace.log，即使 stdout 被截断也能回看。
退出码：0 = PASS，1 = FAIL。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 进度同时写文件：进程若被外部中断，stdout 缓冲会丢，文件不会
_TRACE_PATH = ROOT / "logs" / "_diag" / "sched_trace.log"


def _trace(msg: str) -> None:
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_env_cron(env_path: Path) -> tuple[str, int]:
    """直接从 .env 文本读，避免被 import 顺序干扰。"""
    hours, minute = "8,18", 0  # 与 app/core/config.py 的默认值保持一致
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("REFRESH_CRON_HOURS="):
                hours = line.split("=", 1)[1].strip()
            elif line.startswith("REFRESH_CRON_MINUTE="):
                minute = int(line.split("=", 1)[1].strip() or 0)
    return hours, minute


async def main() -> int:
    ap = argparse.ArgumentParser(description="验证定时抓取到点真的会执行")
    ap.add_argument("--db", default=str(ROOT / "quanzhi.db"), help="要复制的源库")
    ap.add_argument("--timeout", type=int, default=80, help="等待触发的上限秒数")
    args = ap.parse_args()

    _trace("=" * 60)
    _trace("知了 · 定时抓取验证")

    # ---- 1. 配置 ----
    hours, minute = read_env_cron(ROOT / ".env")
    _trace(f"[1/5] .env：REFRESH_CRON_HOURS={hours}  REFRESH_CRON_MINUTE={minute}")
    if not ("," in hours or hours.isdigit()):
        _trace(f"FAIL：HOURS 解析异常 {hours!r}")
        return 1
    _trace(f"      等价 cron：{minute} {hours} * * *")

    # ---- 2. 隔离库 ----
    src = Path(args.db)
    if not src.exists():
        _trace(f"[错误] 找不到库：{src}")
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = ROOT / "logs" / "_diag" / f"sched_{stamp}"
    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / "quanzhi.db"
    shutil.copy2(src, dst)
    _trace(f"[2/5] 库已复制到隔离目录（原库不动）：{dst}")

    # 环境变量必须在 import app 之前设置
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{dst.as_posix()}"
    os.environ["ENABLE_SCHEDULER"] = "true"
    os.environ["PUSH_ENABLED"] = "false"

    # 触发时间设为下一整分；太接近边界就挪到再下一分钟
    now = dt.datetime.now()
    target = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    if (target - now).total_seconds() < 12:
        target += dt.timedelta(minutes=1)
    os.environ["REFRESH_CRON_HOURS"] = str(target.hour)
    os.environ["REFRESH_CRON_MINUTE"] = str(target.minute)
    _trace(f"[3/5] 临时把触发时间改为 {target:%H:%M}（现在 {now:%H:%M:%S}，"
           f"需等 {(target - now).total_seconds():.0f}s）")

    sys.path.insert(0, str(ROOT))

    # ---- 桩：只替掉抓取与推送，调度链保持真实 ----
    calls: list[str] = []

    class _FakeResult:
        total_items = 1
        new_items = 1
        updated_items = 0
        summarized = 1
        duration_ms = 1

    class _FakeIngest:
        def __init__(self, session):
            self.session = session

        async def run(self):
            calls.append("ingest")
            _trace("      → job 调用了 IngestService.run()")
            return _FakeResult()

    class _FakePush:
        def __init__(self, session):
            self.session = session

        async def push_pending(self):
            calls.append("push")
            _trace("      → job 调用了 PushService.push_pending()")
            return {"checked": 0, "sent": 0, "failed": 0, "channels": []}

    import app.services.ingest as ingest_mod
    import app.services.push as push_mod

    ingest_mod.IngestService = _FakeIngest
    push_mod.PushService = _FakePush

    from app.core.config import get_settings
    from app.workers.scheduler import _last_run, shutdown_scheduler, start_scheduler

    settings = get_settings()
    _trace(f"      时区 {settings.timezone}")

    scheduler = start_scheduler()
    jobs = scheduler.get_jobs()
    if not jobs:
        _trace("FAIL：调度器里没有任何 job")
        return 1
    nxt = jobs[0].next_run_time
    _trace(f"[4/5] 已注册 job「{jobs[0].name}」· 下次执行 {nxt:%Y-%m-%d %H:%M:%S}")

    # 下次执行时间必须落在目标分钟，否则说明 cron 拼接错了
    if not (nxt.hour == target.hour and nxt.minute == target.minute and nxt.second == 0):
        _trace(f"FAIL：下次执行时间 {nxt:%H:%M:%S} 与目标 {target:%H:%M}:00 不一致")
        shutdown_scheduler()
        return 1

    # ---- 5. 等到点 ----
    waited = 0.0
    fired = False
    while waited < args.timeout:
        await asyncio.sleep(1)
        waited += 1
        if calls and _last_run.get("status") not in (None, "idle"):
            fired = True
            break

    shutdown_scheduler()
    _trace(f"[5/5] 等待 {waited:.0f}s · job 状态 {_last_run.get('status')} · 调用链 {calls}")

    ok = fired and _last_run.get("status") == "ok" and calls[:2] == ["ingest", "push"]
    _trace("-" * 60)
    if ok:
        _trace("PASS：调度器到点真的执行了 job（抓取 → 推送），不是只注册不跑")
        _trace(f"      隔离库留在：{workdir}")
        return 0
    _trace("FAIL：到点没有执行 job —— 「每天自动更新」现在还不能写成既成事实")
    if not calls:
        _trace("      排查：cron 时分是否落在等待窗口内、APScheduler 是否已 start")
    elif _last_run.get("status") != "ok":
        _trace(f"      排查：job 内部报错 —— {_last_run.get('status')}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
