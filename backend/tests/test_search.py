"""检索：关键词抽取、零命中兜底、时间窗字段。

## 这三条断言为什么必须存在（2026-10-23 实测）

「我这周有什么要办的事？」——产品最典型的一句问话，实测返回 **0 条引用**，
模型答「最近的通知里没有相关内容」。而库里明明有 4 条 deadline 落在 10-24 之后的待办。
HTTP 200、模型在线、回答通顺、引用为空：**四项指标全绿，答案是错的。**
这是本项目最该防的那类失败，所以每一条都单独钉一个用例。

拆开是三层，一层一个用例：

1. **关键词切出跨词垃圾。** 旧实现是
   `extract_keywords("我这周有什么要办的事？")` →
   `['我这周有','这周有什','周有什么','有什么要','什么要办','么要办的','要办的事']`
   停用词检查是**整串**比对（`sub in STOPWORDS`），而集合里存的是「这周」，
   切出来的是「这周有什」——整串不在集合里，就躲过了过滤。
   这些词被 OR 进 LIKE，注定零命中。

2. **零命中之后不兜底。** 直接把空列表交给模型，模型只能答"没有相关内容"。

3. **时间窗用错字段。** 用的是 `created_at`（入库时间）而不是 `published_at`（发布时间）。
   实测「最近 7 天」按入库算是 115 条，最早一条发布于 **2019-06-17**；按发布算只有 9 条。
   补抓一次历史页，七年前的通知就会以"最近"的身份混进来。

改回旧逻辑，下面这些用例都必须变红。
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.timeutil import now_utc
from app.models import Base, Notification, Source
from app.services.search import extract_keywords, search_notifications


# --------------------------------------------------------------------------- #
# ① 关键词抽取：不许有跨词垃圾
# --------------------------------------------------------------------------- #
def test_intent_question_has_no_cross_word_garbage():
    """「我这周有什么要办的事」不该切出「这周有什」这种跨词串。"""
    kws = extract_keywords("我这周有什么要办的事？")
    for bad in ("这周", "什么"):
        assert all(bad not in k for k in kws), f"{bad!r} 仍残留在 {kws!r}"
    # 旧实现的确切产物，逐条挡掉
    for garbage in ("我这周有", "这周有什", "周有什么", "有什么要", "什么要办", "么要办的"):
        assert garbage not in kws, f"跨词垃圾 {garbage!r} 又出现了：{kws!r}"


def test_generic_word_is_not_a_keyword():
    """「通知」这种每条标题都有的词，当关键词等于不筛。"""
    kws = extract_keywords("最近有什么通知")
    assert "通知" not in kws


def test_specific_term_is_extracted():
    """千万别把 bug 修成"什么词都抽不出来"。"""
    assert "充电桩" in extract_keywords("充电桩")
    assert "奖学金" in extract_keywords("奖学金什么时候截止")
    assert any("截止" in k for k in extract_keywords("奖学金什么时候截止"))


# --------------------------------------------------------------------------- #
# ② / ③ 库相关：零命中兜底 + 时间窗
# --------------------------------------------------------------------------- #
async def _run_async(fn):
    tmp = Path(tempfile.mkdtemp(prefix="qz_search_")) / "t.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp.as_posix()}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with Sess() as s:
            return await fn(s)
    finally:
        await eng.dispose()


async def _seed(session):
    """造四条数据，其中 C 是关键陷阱：入库时间很新，发布时间是 2019 年。"""
    src = Source(name="后勤处·通知公告", adapter="html_list", enabled=True)
    session.add(src)
    await session.flush()

    now = now_utc()

    def mk(title: str, published: datetime, *, deadline: str | None = None) -> Notification:
        return Notification(
            source_id=src.id,
            dedup_key=title,
            title=title,
            url=f"https://x.example/{abs(hash(title))}.htm",
            published_at=published,
            content_text="这是一段足够长的正文，用来避免被判成空壳。" * 4,
            created_at=now,  # 全部"今天入库"
            ai_status="done",
            key_info={"deadline": deadline} if deadline else {},
        )

    open_deadline = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    items = [
        mk("关于D02东侧充电桩暂停使用的通知", now - timedelta(days=1), deadline=open_deadline),
        mk("食堂营业时间调整", now - timedelta(days=2)),
        # ↓ 陷阱：created_at 是今天，published_at 是七年前
        mk("2019年海峡两岸文化研习营名单公示", datetime(2019, 6, 17)),
        mk("关于奖学金评选结果的公示", now - timedelta(days=3)),
    ]
    session.add_all(items)
    await session.commit()
    return items


def test_intent_question_falls_back_and_skips_stale():
    """「我这周要办什么」→ 必须返回一周内的通知，且不能把 2019 年的端上来。"""

    async def body(session):
        items = await _seed(session)
        hits = await search_notifications(session, "我这周有什么要办的事？")
        return items, hits

    items, hits = asyncio.run(_run_async(body))

    assert hits, "零命中兜底失效：这个问题必须能返回最近的通知"
    titles = [n.title for n in hits]

    # ③ 时间窗必须按 published_at：2019 那条 created_at 是今天，不该进来
    assert "2019年海峡两岸文化研习营名单公示" not in titles, (
        "时间窗用成 created_at 了：七年前发布、今天入库的通知混进了「这周」"
    )
    # 一周内的两条必须在
    assert "关于D02东侧充电桩暂停使用的通知" in titles
    assert "食堂营业时间调整" in titles
    # ② 待办类问题，还没到期的截止日要排最前
    assert titles[0] == "关于D02东侧充电桩暂停使用的通知", (
        f"带未到期 deadline 的通知没排第一，实际顺序：{titles}"
    )


def test_specific_term_without_match_returns_empty():
    """问具体事物、库里没有 → 老实返回空，不许拿不相关通知充数。"""

    async def body(session):
        await _seed(session)
        return await search_notifications(session, "量子计算机")

    assert asyncio.run(_run_async(body)) == []


def test_specific_term_finds_the_notice():
    async def body(session):
        await _seed(session)
        return await search_notifications(session, "充电桩")

    hits = asyncio.run(_run_async(body))
    assert len(hits) == 1
    assert "充电桩" in hits[0].title


def test_open_intent_without_keywords_returns_latest():
    """「最近有什么通知」抽不出关键词（全是泛词），也要能给出最近的一批。

    注意：这条走的是「没有关键词 → 不加过滤」那条路，**不是**零命中兜底。
    兜底单独由下面那条用例覆盖，别让两条混在一起变成假绿。
    """

    async def body(session):
        await _seed(session)
        return await search_notifications(session, "最近有什么通知")

    hits = asyncio.run(_run_async(body))
    assert hits, "开放意图且无关键词时应当返回最新一批"


def test_keyword_zero_hit_falls_back_for_open_intent():
    """有**关键词但零命中**时，开放意图的问题才走兜底。

    「注意」能抽出来当关键词，但种子数据里没有任何一条含「注意」——
    旧逻辑会直接把空列表交给模型，模型答「没有相关内容」。
    """

    async def body(session):
        await _seed(session)
        return await search_notifications(session, "最近有什么要注意的事情")

    hits = asyncio.run(_run_async(body))
    assert hits, "关键词零命中且是开放意图时必须退回最新一批，而不是空手而归"


# --------------------------------------------------------------------------- #
# ④ 上下文：过期的截止日要标出来
# --------------------------------------------------------------------------- #
def test_build_context_marks_expired_deadline():
    """过期的 deadline 必须标注，否则模型会把「10月22日领票」当待办推荐。

    实测：修好检索之后，模型确实推荐了「10月22日12:00 起领票」——
    日期是真的，但那件事已经错过了。这不属于编造，但同样是"看起来对、其实错"。
    """
    from app.services.search import build_context

    future = (now_utc() + timedelta(days=5)).strftime("%Y-%m-%d")

    def mk(deadline: str) -> Notification:
        return Notification(
            source_id=1,
            dedup_key=deadline,
            title="某通知",
            published_at=now_utc(),
            content_text="正文" * 20,
            key_info={"deadline": deadline},
        )

    expired_ctx = build_context([mk("2020-01-01")], {1: "测试来源"})
    open_ctx = build_context([mk(future)], {1: "测试来源"})

    assert "已过期" in expired_ctx
    assert "已过期" not in open_ctx
