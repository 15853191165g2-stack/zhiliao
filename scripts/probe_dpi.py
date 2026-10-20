"""量一下「窗口创建之后」再调 SetProcessDpiAwareness 到底还有没有用。

背景：`知了桌面版.py::_tune_fonts` 是在 `tk.Tk()` 之后才调用
`shcore.SetProcessDpiAwareness(1)` 的。MS 文档明确要求 DPI 感知必须
**在进程创建任何窗口之前**设置，否则对已存在的窗口不生效。
这里用 shcore.GetProcessDpiAwareness 直接读回真实值来验证：
    0 = UNAWARE  1 = SYSTEM_DPI_AWARE  2 = PER_MONITOR_DPI_AWARE
"""

from __future__ import annotations

import ctypes
import sys

shcore = ctypes.windll.shcore
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def awareness() -> int:
    v = ctypes.c_int(-1)
    hr = shcore.GetProcessDpiAwareness(None, ctypes.byref(v))
    return v.value if hr == 0 else -hr


NAMES = {0: "UNAWARE（会被系统拉伸 → 发虚）",
         1: "SYSTEM_DPI_AWARE（清晰）",
         2: "PER_MONITOR_DPI_AWARE（清晰）"}


def report(tag: str) -> None:
    a = awareness()
    hdc = user32.GetDC(0)
    dpi = gdi32.GetDeviceCaps(hdc, 88)
    user32.ReleaseDC(0, hdc)
    print(f"  {tag}: awareness={a} ({NAMES.get(a, '未知')})  "
          f"屏幕={user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}  LOGPIXELSX={dpi}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "late"
    print(f"模式 = {mode}")
    report("初始")

    if mode == "early":
        r = shcore.SetProcessDpiAwareness(1)
        print(f"  SetProcessDpiAwareness(1) 返回 hr={r}  （窗口创建前）")
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        report("建 Tk 之后")
    else:
        import tkinter as tk
        root = tk.Tk()          # ← 先建窗口（和 App 的实际顺序一致）
        root.withdraw()
        report("建 Tk 之后、设置之前")
        r = shcore.SetProcessDpiAwareness(1)
        print(f"  SetProcessDpiAwareness(1) 返回 hr={r}  （窗口创建之后）")
        report("设置之后")
        try:
            user32.SetProcessDPIAware()
            print("  SetProcessDPIAware() 也已调用（同样在窗口之后）")
        except Exception as e:  # noqa: BLE001
            print(f"  SetProcessDPIAware 失败：{e}")
        report("再试一次之后")

    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
