"""时间处理统一入口。

约定：**数据库里存的全部是 naive UTC**。
对外输出时统一转成 Asia/Shanghai 的带时区 ISO 字符串，避免前端拿到裸时间瞎猜。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

# 常见日期时间格式。**顺序即优先级**，越具体的排越前。
#
# 这些形态不是拍脑袋列的，是把大连理工 7 个子站的通知列表逐页翻完后
# 归纳出来的实际存在形式（见 docs/03-dlut-sources.md 的「日期形态」一节）：
#   2026-08-10 19:01   博达详情页/迎新网
#   2026年07月20日      学生处列表 span.date
#   [2026-04-29]       研究生院列表 span.date（带方括号）
#   2026/09/21         后勤处详情页「时间：」
#   2026 09-22         图书馆列表（年份与月-日被拆成两个 span）
#   21 2026-09         后勤处列表（日 与 年-月 被拆成 p.day / p.year-month）
#   2026-08            迎新网首页时间被截断，只到月
_DATE_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?P<y>20\d{2})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})\s*[日]?\s*"
        r"(?P<H>\d{1,2}):(?P<M>\d{2})",
        "ymdhm",
    ),
    (r"(?P<y>20\d{2})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})\s*[日]?", "ymd"),
    # 年份与「月-日」被拆开：2026 09-22
    (r"(?P<y>20\d{2})\s+(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})(?!\d)", "ymd"),
    # 「日」与「年-月」被拆开：21 2026-09（必须排在 ym 之前，否则会被当成 2026-09-01）
    (r"(?P<d>\d{1,2})\s+(?P<y>20\d{2})[-/.](?P<m>\d{1,2})(?![\d\-/.])", "dmy"),
    # 只精确到月：2026-08 → 取当月 1 日
    (r"(?P<y>20\d{2})[-/.年](?P<m>\d{1,2})月?(?![\d\-/.])", "ym"),
    (r"(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})\s*[日]?\s*(?P<H>\d{1,2}):(?P<M>\d{2})", "mdhm"),
    (r"(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})\s*[日]?", "md"),
]


def now_utc() -> datetime:
    """当前时间（naive UTC），用于写库。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """把任意 datetime 归一成 naive UTC。

    无时区信息的按北京时间处理（学校通知的语境默认就是东八区）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_local_iso(dt: datetime | None) -> str | None:
    """naive UTC -> 带 +08:00 的 ISO 字符串，给客户端用。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(SHANGHAI).isoformat()


def to_local_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(SHANGHAI)


def parse_dt(text: str | None, fmt: str | None = None) -> datetime | None:
    """从一段文本里解析日期时间，返回 naive UTC。

    依次尝试：显式格式 -> 内置正则表 -> dateutil 兜底。
    解析不出来返回 None（不猜，宁可空着也不写错）。
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    # 1) 配置里指定的格式
    if fmt:
        try:
            return to_utc_naive(datetime.strptime(raw, fmt))
        except ValueError:
            pass

    # 2) 归一化全角字符/包裹符号后走正则
    norm = (
        raw.replace("：", ":")
        .replace("（", "(")
        .replace("）", ")")
        .replace("T", " ")
        .replace("【", "[").replace("】", "]")
        .replace("〔", "[").replace("〕", "]")
        .strip()
    )
    norm = re.sub(r"^[\[\(【〔\s]+|[\]\)】〕\s]+$", "", norm) or norm
    for pattern, kind in _DATE_PATTERNS:
        m = re.search(pattern, norm)
        if not m:
            continue
        g = m.groupdict()
        # ⚠️ 必须取 **naive** 的北京时间：下面构造的 dt 是 naive 的，
        # 用带 tzinfo 的 today 做减法会抛
        # `TypeError: can't subtract offset-naive and offset-aware datetimes`
        # （踩过：迎新网《新生家长指南》里有个「9月1日」，走 md 分支直接崩）。
        today = datetime.now(SHANGHAI).replace(tzinfo=None)
        try:
            if kind == "ymdhm":
                dt = datetime(int(g["y"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]))
            elif kind == "ymd":
                dt = datetime(int(g["y"]), int(g["m"]), int(g["d"]), 0, 0)
            elif kind == "dmy":
                dt = datetime(int(g["y"]), int(g["m"]), int(g["d"]), 0, 0)
            elif kind == "ym":
                dt = datetime(int(g["y"]), int(g["m"]), 1, 0, 0)
            elif kind == "mdhm":
                dt = datetime(today.year, int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]))
            else:  # md
                dt = datetime(today.year, int(g["m"]), int(g["d"]), 0, 0)
                # 跨年修正：解析出的日期比今天晚 3 个月以上，说明是去年的
                if (dt - today).days > 90:
                    dt = dt.replace(year=today.year - 1)
            return to_utc_naive(dt)
        except ValueError:
            continue

    # 3) dateutil 兜底
    try:
        from dateutil import parser as dateparser

        return to_utc_naive(dateparser.parse(norm, fuzzy=True))
    except Exception:  # noqa: BLE001
        return None


def normalize_text(text: str | None) -> str:
    """清理正文空白，供去重与摘要使用。"""
    if not text:
        return ""
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
