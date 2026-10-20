"""量真实运行中的「知了桌面版」进程：DPI 感知级别 + 窗口真实矩形。

为什么要单独量：`ShellExecute(shcore.SetProcessDpiAwareness)` 即使返回 hr=0，
对**已经创建过的窗口**也可能不生效（MS 文档要求必须在创建任何窗口之前设置）。
这和「性能/清晰度」直接相关 —— 不感知 DPI 的进程会被 Windows 整体拉伸，字就虚。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shcore = ctypes.windll.shcore

# ★ 本探针自己先感知 DPI，否则 GetSystemMetrics 拿到的是缩放后的虚拟尺寸
try:
    shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    pass

AWARE = {0: "UNAWARE（被系统拉伸，字发虚）",
         1: "SYSTEM_DPI_AWARE（清晰）",
         2: "PER_MONITOR_DPI_AWARE（最清晰）"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def find_widest_window(keyword: str, pid: int) -> int:
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if int(p.value) == pid:
                n = user32.GetWindowTextLengthW(h)
                if n > 0:
                    b = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(h, b, n + 1)
                    if keyword in b.value:
                        r = wt.RECT()
                        user32.GetWindowRect(h, ctypes.byref(r))
                        hits.append((r.right - r.left, h))
        return True

    user32.EnumWindows(cb, 0)
    hits.sort(reverse=True)
    return hits[0][1] if hits else 0


def main() -> int:
    print(f"探针自身已 DPI 感知：屏幕 = "
          f"{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")

    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"

    gui = subprocess.Popen(
        [str(ROOT / "python" / "pythonw.exe"), str(ROOT / "quanzhi_desktop.py")],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"启动桌面版 PID={gui.pid}")
    hwnd = 0
    try:
        for _ in range(40):
            hwnd = find_widest_window("知了", gui.pid)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            print("!! 没找到窗口")
            return 1
        time.sleep(3)

        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, gui.pid)
        v = ctypes.c_int(-1)
        hr = shcore.GetProcessDpiAwareness(h, ctypes.byref(v)) if h else -1
        kernel32.CloseHandle(h)
        aw = v.value if hr == 0 else None

        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, ht = r.right - r.left, r.bottom - r.top

        print(f"  进程 DPI 感知 = {aw} ({AWARE.get(aw, '读不到')})")
        print(f"  窗口矩形 = {w} x {ht}   (GetWindowRect)")
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        print(f"  物理屏幕 = {sw} x {sh}   虚拟屏幕 = {sw * 96 // 144} x {sh * 96 // 144}"
              f"  （按 150% 缩放反推）")
        verdict = ("窗口 ≈ 物理屏幕 → 真正按物理像素布局，清晰"
                   if w > sw * 0.9 else
                   "窗口 ≈ 虚拟尺寸 → 进程被 DPI 虚拟化，界面会被整体拉伸变虚")
        print(f"  判定：{verdict}")
    finally:
        gui.terminate()
        time.sleep(1)
        if gui.poll() is None:
            gui.kill()
        print("已关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
