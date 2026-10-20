"""在 pythonw 下运行桌面壳，并把退出原因写进文件。

用法：python\\pythonw.exe scripts\\run_with_exitcode.py
"""

from __future__ import annotations

import faulthandler
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "exit_trace.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

f = open(LOG, "w", encoding="utf-8", buffering=1)


def log(msg: str) -> None:
    f.write(msg + "\n")
    f.flush()


log("=== 退出原因跟踪 ===")
log(f"python={sys.executable}")
log(f"root={ROOT}")

faulthandler.enable(file=f, all_threads=True)

try:
    spec = importlib.util.spec_from_file_location(
        "qd", ROOT / "知了桌面版.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["qd"] = m
    spec.loader.exec_module(m)
    log("模块加载 OK")

    # 给 root 挂一个「窗口被销毁」的钩子
    import tkinter as tk
    orig_main = m.main


    def traced_main():
        log("进入 main()")
        try:
            orig_main()
            log("main() 正常返回（窗口被关闭）")
        except SystemExit as e:
            log(f"main() 触发 SystemExit: {e}")
            raise
        except BaseException:
            log("main() 抛异常：")
            log(traceback.format_exc())
            raise

    traced_main()

except SystemExit as e:
    log(f"顶层 SystemExit code={e.code}")
except BaseException:
    log("顶层异常：")
    log(traceback.format_exc())
finally:
    try:
        log("进程即将结束")
    finally:
        f.close()
