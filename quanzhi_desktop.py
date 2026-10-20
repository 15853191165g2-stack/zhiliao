"""知了 · 电脑版启动入口（ASCII 文件名）。

⚠️ 为什么多这一个文件？
Windows 下 `python.exe` / `pythonw.exe` 直接执行**文件名为中文**的 .py 脚本时，
会**静默退出**（返回码 0、无任何输出、无 traceback），排查成本极高。
所以启动脚本固定调用这个 ASCII 名入口，内部再把中文名的实现导入进来。
中文文件名不影响 `import`，只影响「直接作为脚本执行」。

用法：
    python\\pythonw.exe quanzhi_desktop.py
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path


def _load():
    """把同目录下的「知了桌面版.py」当成模块加载（中文文件名 import 是安全的）。"""
    here = Path(__file__).resolve().parent
    target = here / "知了桌面版.py"
    if not target.exists():
        raise FileNotFoundError(f"找不到主程序：{target}")

    spec = importlib.util.spec_from_file_location("quanzhi_desktop_impl", target)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{target}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["quanzhi_desktop_impl"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    try:
        module = _load()
        module.main()
    except Exception:
        here = Path(__file__).resolve().parent
        log_dir = here / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        crash = log_dir / "crash.log"
        detail = traceback.format_exc()
        crash.write_text(detail, encoding="utf-8")
        try:
            import tkinter as tk
            from tkinter import messagebox

            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "知了启动失败",
                f"启动时出错，完整日志见：\n{crash}\n\n{detail[-800:]}",
            )
            r.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    main()
