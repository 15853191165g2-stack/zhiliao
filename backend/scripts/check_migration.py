"""在真实库的副本上验证迁移（脚本一次跑完，避免长命令行被环境拦）。

用法：
    python _mig_check.py <副本 db 路径>
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))


def dump(db: str, tag: str) -> None:
    con = sqlite3.connect(db)
    cols = [c[1] for c in con.execute("PRAGMA table_info(notifications)")]
    tabs = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    total = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    print(f"[{tag}] 通知总数={total}")
    print(f"[{tag}] notified_at={('notified_at' in cols)} notify_error={('notify_error' in cols)}")
    print(f"[{tag}] 表={tabs}")
    if "notified_at" in cols:
        nulls = con.execute("SELECT COUNT(*) FROM notifications WHERE notified_at IS NULL").fetchone()[0]
        print(f"[{tag}] notified_at 为 NULL 的行={nulls}")
    idx = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%notified%'")]
    print(f"[{tag}] 相关索引={idx}")
    con.close()


def main() -> int:
    db = Path(sys.argv[1]).as_posix()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    os.environ["ENABLE_SCHEDULER"] = "false"

    dump(db, "迁移前")

    from app.db import init_db

    async def run():
        await init_db()
        print("init_db 第 1 遍 OK")
        await init_db()
        print("init_db 第 2 遍 OK（幂等验证通过）")

    asyncio.run(run())
    dump(db, "迁移后")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
