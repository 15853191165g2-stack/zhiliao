# -*- coding: utf-8 -*-
"""可靠的界面截图（已在真机验证）。

关键改进（前两版都栽在这）：
1. **先清残留**：上次若崩在中途，旧 Tk 窗口会一直活着并占着「知了」标题，
   于是枚举窗口拿到的是**旧窗口**，截出的是陈旧画面 —— 表现为
   「截图大小/哈希每次都一样，看着像没更新」。所以开场先杀 pythonw。
2. **只认自己启的 PID**：用 GetWindowThreadProcessId 反查窗口属于哪个进程，
   只接受本次启的那个 PID，彻底杜绝抓到别人的窗口。
3. **等卡片真的建出来**：轮询后端 total>0 之后再留时间给 Tk 建控件。
4. **收尾放 finally**：异常也要把 GUI 关掉，不给下一次留雷。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "shot.png"
LOG = ROOT / "logs" / "capture.log"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020
_logf = open(LOG, "w", encoding="utf-8", buffering=1)


def make_dpi_aware() -> int:
    """让**截图进程自己**也 DPI 感知，然后再问屏幕尺寸。

    ⚠️ 这是个很隐蔽的坑：本进程若不是 DPI 感知的，
    `GetSystemMetrics(0/1)` 返回的是**缩放后的虚拟尺寸**
    （本机 150% 缩放：真实 2560x1600 → 虚报 1707x1067）。
    而 `GetDC(0)` + `BitBlt` 抓的是**物理像素**，
    于是只拷到屏幕左上角 1707 物理像素 —— 也就是**右边一大截没了**。
    表现是截图里窗口内容被裁掉右侧（置顶按钮、已读勾选框正好在右边缘）。
    返回真实屏幕宽（拿不到就返回 0）。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # Win8.1+
    except Exception:                                       # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()       # 老系统兜底
        except Exception:                                   # noqa: BLE001
            return 0
    try:
        return int(user32.GetSystemMetrics(0))
    except Exception:                                       # noqa: BLE001
        return 0


# 导入即生效：必须在任何 GetDC / SetWindowPos 之前调用
make_dpi_aware()


def log(m: str) -> None:
    _logf.write(m + "\n")
    _logf.flush()


class BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", BMIH), ("bmiColors", wt.DWORD * 3)]


def kill_stale_gui() -> int:
    """杀掉所有 pythonw.exe（本项目里只有界面用它）。返回杀掉的数量。"""
    n = 0
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
        ).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower().startswith("pythonw"):
                pid = parts[1]
                if pid.isdigit():
                    subprocess.run(["taskkill", "/PID", pid, "/F"],
                                   capture_output=True)
                    log(f"  清掉残留界面 PID={pid}")
                    n += 1
    except Exception as e:  # noqa: BLE001
        log(f"  （清理时出错，忽略）{e}")
    return n


def pid_of_window(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def find_window_of_pid(keyword: str, want_pid: int) -> int:
    """只找属于 want_pid 的、标题含 keyword 的可见窗口。"""
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and pid_of_window(hwnd) == want_pid:
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if keyword in buf.value:
                    hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else 0


def grab(rect) -> tuple[bytes, int, int]:
    vw = user32.GetSystemMetrics(0)
    vh = user32.GetSystemMetrics(1)
    dc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(dc)
    bmp = gdi32.CreateCompatibleBitmap(dc, vw, vh)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, vw, vh, dc, 0, 0, SRCCOPY)

    bi = BMI()
    bi.bmiHeader.biSize = ctypes.sizeof(BMIH)
    bi.bmiHeader.biWidth = vw
    bi.bmiHeader.biHeight = -vh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(vw * vh * 4)
    gdi32.GetDIBits(mem, bmp, 0, vh, buf, ctypes.byref(bi), 0)

    x0 = max(0, min(rect.left, vw - 1))
    y0 = max(0, min(rect.top, vh - 1))
    w = max(1, min(rect.right - rect.left, vw - x0))
    h = max(1, min(rect.bottom - rect.top, vh - y0))
    raw = buf.raw
    # ⚠️ 极隐蔽的坑：GDI 的 32 位 DIB 在内存里是 **BGRA** 字节序
    #   （little-endian 的 0x00RRGGBB），而 PNG 规范里 color type 6 是 **RGBA**。
    #   直接把 raw 塞进去 → 整张图**红蓝互换**：蓝色 #2F6BFF 存成橙色 #FF6B2F。
    #   中性色看不出问题，所以特别难发现（实测：状态条 #D5DAE5 被存成 #E5DAD5）。
    #   这里逐行把第 0 和第 2 字节对调。
    rows = []
    for y in range(y0, y0 + h):
        line = bytearray(raw[(y * vw + x0) * 4:(y * vw + x0) * 4 + w * 4])
        line[0::4], line[2::4] = line[2::4], line[0::4]   # B<->R
        rows.append(b"\x00" + bytes(line))

    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, dc)
    return b"".join(rows), w, h


def write_png(path: Path, rows: bytes, w: int, h: int) -> None:
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


# ---------------------------------------------------------------------------
# PrintWindow 抓图：不读屏幕，让窗口自己重绘
# ---------------------------------------------------------------------------

user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
user32.PrintWindow.restype = wt.BOOL
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND

GA_ROOT = 2
PW_RENDERFULLCONTENT = 0x00000002


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", BMIH), ("bmiColors", wt.DWORD * 3)]


def top_level_hwnd(child_hwnd: int) -> int:
    """Tk 的 `winfo_id()` 给的是**客户区子窗口**，往上找才是带标题栏的顶层窗口。"""
    return int(user32.GetAncestor(wt.HWND(child_hwnd), GA_ROOT) or child_hwnd)


def grab_window(hwnd: int):
    """把窗口**自己**渲染进内存 DC（PrintWindow），完全不碰屏幕。

    为什么需要它：`grab()` 走的是 `GetDC(0)` + `BitBlt`，抓的是
    「**屏幕上此刻显示的内容**」。于是三种情况会拍出废图：
      - 会话被锁定 / 显示器休眠 → 整块黑（实测：抽样 1200 个点全是 (0,0,0)）
      - 窗口被别的程序盖住 → 拍到的是别人
      - 窗口没到前台 → 同上
    `PrintWindow(PW_RENDERFULLCONTENT)` 让窗口自己重绘一遍，与 z 序、
    前台焦点、屏幕电源状态**都无关**，锁屏时照样能拍。

    返回 (rows, w, h, rect)；返回 None 表示窗口不吃这一套（会给全白/全黑）。
    """
    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    dc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(dc)
    bmp = gdi32.CreateCompatibleBitmap(dc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(wt.HWND(hwnd), mem, PW_RENDERFULLCONTENT):
            return None
        bi = BMI()
        bi.bmiHeader.biSize = ctypes.sizeof(BMIH)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h            # 负高度 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, dc)

    raw = buf.raw
    # 同上：GDI 是 BGRA，PNG 是 RGBA，必须逐行对调第 0/2 字节
    rows = []
    for y in range(h):
        line = bytearray(raw[y * w * 4:(y + 1) * w * 4])
        line[0::4], line[2::4] = line[2::4], line[0::4]
        rows.append(b"\x00" + bytes(line))
    return b"".join(rows), w, h, rect


def looks_solid(rows: bytes, w: int, h: int, limit: int = 6) -> bool:
    """抽样子节，颜色种类 <= limit 就认为没渲染出内容。"""
    n = w * h * 4
    if n == 0:
        return True
    step = max(4, (n // 2000) // 4 * 4)
    seen = set()
    for i in range(0, n, step):
        seen.add(rows[i:i + 3])
        if len(seen) > limit:
            return False
    return True


def main() -> int:
    log("=== 截图流程开始 ===")
    log("清理可能的残留界面进程…")
    n = kill_stale_gui()
    log(f"清掉 {n} 个")

    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    gui = None
    bk = None

    # ★ 端口自检：残留旧后端占着 8765 时，自己启的后端会绑定失败，
    #   但 /health 依然有响应 → 截到的其实是旧后端渲染的界面。
    sys.path.insert(0, str(ROOT / "scripts"))
    import user_flags as uf  # noqa: E402

    if uf.guard_port(8765, log=log):
        return 2

    try:
        bk = subprocess.Popen(
            [str(ROOT / "python" / "python.exe"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", "8765", "--log-level", "error"],
            cwd=str(ROOT / "backend"), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(60):
            try:
                opener.open("http://127.0.0.1:8765/api/health", timeout=3)
                log(f"后端就绪 PID={bk.pid}")
                break
            except Exception:
                time.sleep(1)

        gui = subprocess.Popen(
            [str(ROOT / "python" / "pythonw.exe"), str(ROOT / "quanzhi_desktop.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log(f"界面 PID={gui.pid}")

        hwnd = 0
        for i in range(60):
            hwnd = find_window_of_pid("知了", gui.pid)
            if hwnd:
                log(f"第 {i+1} 秒找到窗口 hwnd={hwnd}（属于 PID {gui.pid}）")
                break
            time.sleep(1)
        if not hwnd:
            log("!! 没找到属于本进程的窗口")
            return 1

        for _ in range(30):
            try:
                with opener.open("http://127.0.0.1:8765/api/notifications"
                                 "?page=1&page_size=20", timeout=5) as r:
                    d = json.load(r)["data"]
                if int(d.get("total") or 0) > 0:
                    log(f"后端有数据 total={d['total']}，等界面建卡片…")
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(10)

        user32.ShowWindow(hwnd, 3)              # SW_MAXIMIZE
        time.sleep(1.5)
        user32.SetForegroundWindow(hwnd)
        # 鼠标挪走，免得 hover 高亮误导
        user32.SetCursorPos(user32.GetSystemMetrics(0) - 3,
                            user32.GetSystemMetrics(1) - 3)
        time.sleep(2.5)

        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows, w, h = grab(rect)
        write_png(OUT, rows, w, h)
        md5 = hashlib.md5(OUT.read_bytes()).hexdigest()
        log(f"已截图 {w}x{h}  {OUT.stat().st_size} 字节  md5={md5[:12]}")
        log("=== 截图流程结束 ===")
        return 0
    finally:
        if gui is not None:
            gui.terminate()
            time.sleep(1)
            if gui.poll() is None:
                gui.kill()
            log("界面已关闭")
        if bk is not None:
            bk.terminate()
            time.sleep(1)
            if bk.poll() is None:
                bk.kill()
            log("后端已关闭")
        _logf.close()


if __name__ == "__main__":
    sys.exit(main())
