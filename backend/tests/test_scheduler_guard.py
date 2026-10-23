"""调度器的两条「静默失效」兜底。

背景（2026-10-23 真实事故）：
    18:00 的定时任务**确实跑了**，但 24 个来源全部
    `[Errno 11001] getaddrinfo failed`（本机 DNS 瞬时失效），
    一条都没抓进来。界面上完全看不出来，用户只会觉得「今天没更新」。
    同一天还确认了第二件事：APScheduler 是进程内内存态，
    程序关着时错过的时间点**不会**回补。

这两个测试守的就是上面两条。它们都是纯函数级别的，
不需要起抓取、不碰网络 —— 事故本身也发生在「判断该不该动手」这一步。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.workers.scheduler import (
    _failure_counts,
    should_catchup,
    should_retry_total_failure,
)

NOW = datetime(2026, 10, 23, 13, 48, 0)  # naive UTC


# --------------------------------------------------------------------------- #
# 1. 启动补抓的判定
# --------------------------------------------------------------------------- #


def test_catchup_when_never_fetched():
    """从没抓过的库必须补 —— 空库启动后要立刻有内容。"""
    assert should_catchup(None, NOW, 6.0) is True


def test_no_catchup_when_fresh():
    """刚抓完 1 小时就重启，不该再抓。

    ⚠️ 这条挡的是「每次开程序都全量抓一轮」：
    24 个源、同域限速 2 秒，一轮 4 分钟。频繁重抓既不礼貌也不合规。
    """
    assert should_catchup(NOW - timedelta(hours=1), NOW, 6.0) is False


def test_catchup_when_stale():
    """超过阈值就该补。今天 18:00 那轮失败、21:10 才开机，正是这个场景。"""
    assert should_catchup(NOW - timedelta(hours=14), NOW, 6.0) is True


def test_catchup_boundary_is_inclusive():
    """刚好卡在阈值上算陈旧。

    边界必须钉死：写成 `>` 的话，恰好 6.0 小时会漏掉，
    而「关机 6 小时」恰恰是最常见的场景。
    """
    assert should_catchup(NOW - timedelta(hours=6), NOW, 6.0) is True
    assert should_catchup(NOW - timedelta(hours=6) + timedelta(seconds=1), NOW, 6.0) is False


def test_catchup_disabled_by_zero_threshold():
    """阈值 <=0 表示关掉按时间判断，不该误触发。"""
    assert should_catchup(NOW - timedelta(days=30), NOW, 0) is False
    assert should_catchup(None, NOW, -1) is False


def test_catchup_handles_future_timestamp():
    """库里的时间比现在晚（改过系统时钟 / 时区错位）时不能反手抓一轮。

    负数小时数不应该被判成「陈旧」，否则时钟一跳就全量重抓。
    """
    assert should_catchup(NOW + timedelta(hours=3), NOW, 6.0) is False


# --------------------------------------------------------------------------- #
# 2. 整轮失败的识别
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, statuses):
        self.sources = [{"name": f"s{i}", "status": s} for i, s in enumerate(statuses)]


def test_detects_total_failure():
    """24 个全 error —— 这就是 18:00 那轮的样子，必须能识别。"""
    assert _failure_counts(_Result(["error"] * 24)) == (24, 24)


def test_partial_failure_is_not_total():
    """部分失败不触发重试：抓到的那些已经入库了，重跑是浪费。"""
    assert _failure_counts(_Result(["ok"] * 20 + ["error"] * 4)) == (4, 24)


def test_all_ok():
    assert _failure_counts(_Result(["ok"] * 24)) == (0, 24)


def test_empty_sources_is_not_total_failure():
    """没有启用任何来源时 failed == total == 0。

    ⚠️ 这是最容易写错的地方：断言 `failed == total` 而不判 `total > 0`，
    会在「没有任何来源」时判成整轮失败，然后无限重试。
    调用侧确实加了 `total and` 保护，这里把前提钉在纯函数上。
    """
    assert _failure_counts(_Result([])) == (0, 0)


def test_missing_status_counts_as_not_error():
    """状态字段缺失时按「没失败」处理，宁可漏报也不误报。"""
    r = _Result([])
    r.sources = [{"name": "a"}, {"name": "b", "status": None}]
    assert _failure_counts(r) == (0, 2)


def test_result_without_sources_attr():
    """老结果对象没有 sources 字段时不能抛 —— 调度器不能因为统计出错而挂掉。"""
    assert _failure_counts(object()) == (0, 0)


# --------------------------------------------------------------------------- #
# 3. 该不该重试（最容易写错的一步）
# --------------------------------------------------------------------------- #


def test_retry_when_every_source_failed():
    assert should_retry_total_failure(24, 24) is True


def test_no_retry_on_partial_failure():
    """抓到的 20 个已经入库了，重跑是白跑。"""
    assert should_retry_total_failure(4, 24) is False


def test_no_retry_when_zero_sources():
    """★ 这条守的是「无限重试」。

    一个启用来源都没有时 failed == total == 0。写成 `failed == total` 就会
    判成整轮失败 → 重试 → 又是 0/0 → 无限循环。只在没配来源时出现，平时全绿。
    """
    assert should_retry_total_failure(0, 0) is False


def test_no_retry_when_disabled_by_config():
    """用户关掉自动重试时，就算全挂也不许自己再跑。"""
    assert should_retry_total_failure(24, 24, enabled=False) is False

