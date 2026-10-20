"""测试公共环境 —— 所有测试共用一个临时库。

## ⚠️ 为什么必须有这个文件

`app/db.py` 在**模块级**就执行了 `create_async_engine(settings.database_url)`。
也就是说：**谁先 import `app.db`，谁就决定了整个测试进程连哪个库**。

之前每个测试文件都在自己的模块顶部设 `os.environ["DATABASE_URL"]`，
单看没问题。但只要有两个文件都这么干，就会出这种事：

  1. `test_a.py` 被 import → 设环境变量 = 库 A → `app.db` 绑到库 A ✅
  2. `test_b.py` 被 import → 设环境变量 = 库 B → 但 `app.db` 早就绑在库 A 上了 ❌
  3. `test_b` 用自己的环境变量去「造数据」（写进库 B），
     却用 `TestClient(app)` 从库 A 读 —— 断言失败，而且原因极难看出来。

更糟的情况是它**碰巧通过**（比如两边都还没数据，断言恰好成立），
然后某天改个测试顺序就红了。

所以：环境变量统一在这里设一次，测试文件不要再自己设。
需要拿库路径时读 `os.environ["DATABASE_URL"]`。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="qz_test_"))
TMP_DB = _TMP_DIR / "test.db"

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP_DB.as_posix()}"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["AI_API_KEY"] = ""            # 强制走本地兜底，测试不碰网络
os.environ.setdefault("PUSH_CHANNELS", "local")
# 测试里我们要自己控制"该不该推"，所以关掉「首次静默基线」自动触发。
# 需要验证基线逻辑的用例会显式打开它。
os.environ.setdefault("PUSH_SILENT_FIRST_RUN", "false")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """整场测试开始前建好表。

    ⚠️ 为什么需要：建表发生在 `init_db()`，而它只被 FastAPI 的 lifespan 调用，
    也就是只有用到 `TestClient(app)` 的用例才会建表。
    但有的用例直接调服务层（不起 client），跑在第一个用到 client 的用例之前时
    就会撞 `no such table: sources` —— 而且它看起来像业务 bug，其实纯粹是执行顺序问题。

    这里用一次 context manager 把 lifespan 跑完，全进程的表就都建好了。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        yield
