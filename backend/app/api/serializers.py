"""ORM → Pydantic 的转换。集中一处，避免各路由各写一遍。"""

from __future__ import annotations

from app.models import Notification, Source
from app.schemas import IMPORTANCE_LABELS, NotificationDetail, NotificationOut, SourceOut


def to_out(n: Notification) -> NotificationOut:
    out = NotificationOut.model_validate(n)
    out.importance_label = IMPORTANCE_LABELS.get(n.importance, "一般")
    if n.source is not None:
        out.source_name = n.source.name
        out.school = n.source.school
    return out


def to_detail(n: Notification) -> NotificationDetail:
    detail = NotificationDetail.model_validate(n)
    detail.importance_label = IMPORTANCE_LABELS.get(n.importance, "一般")
    if n.source is not None:
        detail.source_name = n.source.name
        detail.school = n.source.school
    return detail


def to_source_out(s: Source) -> SourceOut:
    return SourceOut.model_validate(s)
