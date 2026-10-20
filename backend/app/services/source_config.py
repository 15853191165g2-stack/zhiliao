"""sources/*.yaml 的加载与数据库同步。

「新增一所学校 = 加一个 yaml」这件事在这里落地：
把 yaml 里的来源 upsert 进数据库，App 端立刻能看到并抓取。
数据库里用户自己加的来源（比如导入的公众号 RSS）不会被这个流程删掉。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Source

logger = logging.getLogger(__name__)


def load_yaml_sources(directory: Path | None = None) -> list[dict[str, Any]]:
    """读取目录下所有 yaml，返回来源配置列表。"""
    root = Path(directory or get_settings().sources_dir)
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.y*ml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.error("解析 %s 失败：%s", path.name, e)
            continue

        # 支持一个文件里写多个来源
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                logger.warning("跳过 %s 中的无效条目（缺少 name）", path.name)
                continue
            entry.setdefault("config", {})
            entry["_file"] = path.name
            out.append(entry)
    return out


async def sync_yaml_to_db(session: AsyncSession, directory: Path | None = None) -> dict[str, int]:
    """把 yaml 来源写进数据库。同名同 URL 的更新配置，其余保持不变。"""
    entries = load_yaml_sources(directory)
    created = updated = 0

    for entry in entries:
        name = str(entry["name"]).strip()
        url = str(entry.get("url") or "").strip()
        row = (
            await session.execute(
                select(Source).where(Source.name == name, Source.url == url)
            )
        ).scalar_one_or_none()

        payload = {
            "name": name,
            "school": str(entry.get("school") or ""),
            "adapter": str(entry.get("adapter") or "html_list"),
            "url": url,
            "category_hint": str(entry.get("category_hint") or "其他"),
            "enabled": bool(entry.get("enabled", True)),
            "config": entry.get("config") or {},
        }

        if row is None:
            session.add(Source(**payload))
            created += 1
        else:
            for k, v in payload.items():
                setattr(row, k, v)
            updated += 1

    await session.commit()
    logger.info("来源同步完成：新增 %d，更新 %d", created, updated)
    return {"created": created, "updated": updated, "total": len(entries)}


def dump_source_to_yaml(source: Source, directory: Path | None = None) -> Path:
    """把数据库里的来源导出成 yaml，方便用户分享给同学。"""
    root = Path(directory or get_settings().sources_dir)
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in source.name if c.isalnum() or c in "-_") or f"source_{source.id}"
    path = root / f"{safe}.yaml"
    data = {
        "name": source.name,
        "school": source.school,
        "adapter": source.adapter,
        "url": source.url,
        "category_hint": source.category_hint,
        "enabled": source.enabled,
        "config": source.config or {},
    }
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path
