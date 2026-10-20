"""用真实 GLM 跑几条通知的摘要，把结果落盘，便于对比 local 与 GLM 的差别。

    python scripts/glm_sample.py --n 4 --sleep 5

免费档并发只有 1，所以串行 + 间隔；429 时自动退避重试。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.models import Notification, Source  # noqa: E402
from app.services.summarize import SummarizeService  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=5.0)
    args = ap.parse_args()

    s = get_settings()
    print(f"引擎: {'GLM ' + s.ai_model if s.ai_enabled else 'local'} | "
          f"thinking={s.ai_thinking} json_mode={s.ai_use_json_mode}", flush=True)

    await engine.dispose()
    out: list[dict] = []
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.content_text.isnot(None))
                    .order_by(Notification.published_at.desc().nulls_last(), Notification.id.desc())
                    .limit(args.n)
                )
            )
            .scalars()
            .all()
        )
        svc = SummarizeService(s)
        for r in rows:
            src = await db.get(Source, r.source_id) if r.source_id else None
            try:
                res = await svc.summarize(
                    title=r.title,
                    content=r.content_text or "",
                    source_name=src.name if src else "",
                    school="大连理工大学",
                    published_at=r.published_at.strftime("%Y-%m-%d") if r.published_at else "",
                    category_hint=r.category or "其他",
                )
                item = {
                    "title": r.title,
                    "source": src.name if src else None,
                    "model": res.model,
                    "summary_lines": res.summary.summary_lines,
                    "key_info": res.summary.key_info.model_dump(mode="json"),
                    "importance": res.summary.importance,
                    "category": res.summary.category,
                    "tags": res.summary.tags,
                    "confidence": res.summary.confidence,
                    "error": res.error,
                }
            except Exception as e:  # noqa: BLE001
                item = {"title": r.title, "model": "FAILED", "error": str(e)[:300]}
            out.append(item)
            print(json.dumps(item, ensure_ascii=False, indent=2), flush=True)
            print("-" * 72, flush=True)
            await asyncio.sleep(args.sleep)

    dest = Path("data/glm_sample.json")
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {dest}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
