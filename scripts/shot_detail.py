# -*- coding: utf-8 -*-
"""截图：先截主界面，再模拟点击第一张卡片，截详情窗口。

用途是留证「点开能看到全文」，以及详情里能滚动。
"""
from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
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
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020

# ★ 截图进程自己必须先 DPI 感知。
#   不感知的话 `GetSystemMetrics` / `GetWindowRect` 返回的是**缩放后的虚拟坐标**
#   （本机 150% 缩放：真实 2560x1600 被报成 1707x1067），
#   而 `GetDC(0)` + `BitBlt` 拷的是**物理像素** ——
#   于是只拷到屏幕左上角 1707 物理像素，**窗口右侧整块丢失**
#   （表现为搜索框、清空筛选、右上角状态都不见了）。
#   必须在任何窗口/GDI 调用之前执行。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:                       # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:                   # noqa: BLE001
        pass


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", BMIH), ("bmiColors", wt.DWORD * 3)]


def pid_of(h):
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return int(p.value)


# SetWindowPos 的参数里 HWND_TOPMOST = -1 是个**指针值**。
# 不声明 argtypes 的话 ctypes 会把它当 32 位 int 传，x64 上零扩展成
# 0x00000000FFFFFFFF 而不是 0xFFFFFFFFFFFFFFFF —— 结果就是「置顶没生效」，
# 于是桌面 BitBlt 抓到的还是盖在上面的窗口。必须显式声明类型。
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wt.BOOL
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002


def pin_topmost(hwnd) -> None:
    """把窗口抬到最顶层。

    为什么不用 SetForegroundWindow：它会被 Windows 的**前台锁**静默忽略，
    于是桌面抓图抓到的仍然是盖在上面的窗口（曾经截到过应用商店和 IDE）。
    SetWindowPos(TOPMOST) 走的是窗口管理器层级，和前台焦点无关，可靠得多。
    """
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE)


def top_window_at(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(wt.POINT(x, y)) or 0)


def find_win(keyword, want_pid=None):
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            if want_pid is None or pid_of(h) == want_pid:
                n = user32.GetWindowTextLengthW(h)
                if n > 0:
                    b = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(h, b, n + 1)
                    if keyword in b.value:
                        hits.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return hits


def grab(rect, out):
    vw, vh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
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
    x0, y0 = max(0, rect.left), max(0, rect.top)
    w = max(1, min(rect.right - rect.left, vw - x0))
    h = max(1, min(rect.bottom - rect.top, vh - y0))
    raw = buf.raw
    # ⚠️ GDI 的 32 位 DIB 内存序是 **BGRA**，PNG(color type 6) 要的是 **RGBA**。
    # 直接塞进去会让整张图红蓝互换（蓝 #2F6BFF 变橙 #FF6B2F），
    # 中性色看不出来所以极难发现。逐行把第 0 和第 2 字节对调。
    rows = []
    for y in range(y0, y0 + h):
        line = bytearray(raw[(y * vw + x0) * 4:(y * vw + x0) * 4 + w * 4])
        line[0::4], line[2::4] = line[2::4], line[0::4]
        rows.append(b"\x00" + bytes(line))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(
            ">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    out.write_bytes(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
                    + chunk(b"IEND", b""))
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, dc)
    return w, h


env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

sys.path.insert(0, str(ROOT / "scripts"))
import user_flags  # noqa: E402

# ★ 端口自检必须放在**启动自己的后端之前**：
#   残留旧后端占着 8765 时，自己启的后端会绑定失败，但 /health 依然有响应，
#   于是截出来的是旧后端渲染的界面，或者干脆什么都没截到。
if user_flags.guard_port(8765):
    sys.exit(2)


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:8765/api" + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with op.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

bk = subprocess.Popen([str(ROOT / "python" / "python.exe"), "-m", "uvicorn",
                       "app.main:app", "--host", "127.0.0.1", "--port", "8765",
                       "--log-level", "error"],
                      cwd=str(ROOT / "backend"), env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
for _ in range(60):
    try:
        op.open("http://127.0.0.1:8765/api/health", timeout=3)
        break
    except Exception:
        time.sleep(1)

# ★ 拍图前先给**用户状态**拍快照，结束后逐行还原。
#   为什么必须做：这个脚本会注入**真实鼠标点击**去打开一条通知，而桌面版
#   打开详情时会**自动把该条标记为已读**（同邮件客户端，见 open_detail）。
#   不还原的话，每拍一次图用户的通知就悄悄多出一条「已读」。
before = user_flags.snapshot()
_restored = {"done": False}


def restore_once() -> None:
    if _restored["done"]:
        return
    _restored["done"] = True
    n = user_flags.restore(before, post=api_post, log=print)
    same = user_flags.verify(before, log=print)
    print(f"  用户状态还原：{n} 处；与快照比对 = {'一致' if same else '仍有差异！'}")


atexit.register(restore_once)       # 中途异常也不会留下脏数据

gui = subprocess.Popen([str(ROOT / "python" / "pythonw.exe"),
                        str(ROOT / "quanzhi_desktop.py")],
                       cwd=str(ROOT), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
hwnd = 0
for _ in range(60):
    w = find_win("知了", gui.pid)
    if w:
        hwnd = w[0]
        break
    time.sleep(1)
print("主窗口 hwnd =", hwnd)

user32.ShowWindow(hwnd, 3)
time.sleep(1.5)
pin_topmost(hwnd)                     # ★ 靠窗口层级置顶，不依赖前台锁
user32.SetForegroundWindow(hwnd)
user32.SetCursorPos(user32.GetSystemMetrics(0) - 3,
                    user32.GetSystemMetrics(1) - 3)
time.sleep(9)
pin_topmost(hwnd)
time.sleep(1.0)

rect = wt.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
print("主界面截图:", grab(rect, ROOT / "logs" / "shot_feed.png"))

# 模拟点击：用 Tk 没法从外部点，改用坐标点击第一张卡片中部。
# 卡片区大致从窗口左边 200px 起；第一张卡片在导航栏高度之下约 210px 处。
x = rect.left + 400
y = rect.top + 260
# ★ 点之前先确认「这个坐标上最顶层的窗口确实是我们自己」。
#   否则一旦置顶失败，这一下就点到别的程序上去了（会真的动你的鼠标）。
top = top_window_at(x, y)
if pid_of(top) != gui.pid and top != hwnd:
    print(f"!! 坐标 ({x},{y}) 上最顶层窗口不属于本程序 PID={gui.pid}（实际 PID={pid_of(top)}），"
          f"放弃模拟点击，避免误点别的窗口")
    gui.terminate(); bk.terminate()
    sys.exit(1)
print(f"点击坐标 ({x},{y}) —— 已确认该处是本程序窗口")
user32.SetCursorPos(x, y)
time.sleep(0.4)
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
time.sleep(0.08)
user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
time.sleep(3.5)

# 找详情窗口。
# ⚠️ 不能用 find_win("通知") —— 主窗口标题是
#   「知了 · 只开一个窗口，看全学校所有通知」，里面就含「通知」两个字，
#   结果会把主窗口当详情窗口截下来。改成「枚举本进程所有有标题的窗口，
#   再排除主窗口」，不依赖标题里恰好有哪些字。
wins = [w for w in find_win("", gui.pid) if w != hwnd]
print("找到的详情窗口:", wins)
if wins:
    dw = wins[0]
    user32.ShowWindow(dw, 3)
    time.sleep(1.5)
    pin_topmost(dw)
    user32.SetForegroundWindow(dw)
    time.sleep(1.5)
    pin_topmost(dw)
    time.sleep(0.8)
    r2 = wt.RECT()
    user32.GetWindowRect(dw, ctypes.byref(r2))
    print("详情截图:", grab(r2, ROOT / "logs" / "shot_detail.png"))
else:
    print("!! 没点开详情窗口")

# ★ 收尾自校验：把写出的 PNG 重新解码，确认应用主题确实是**蓝色**的。
#   防的是「GDI 给 BGRA、PNG 要 RGBA」这类色序 bug —— 它会把蓝色
#   #2F6BFF 存成橙色 #FF6B2F，界面本身却是对的，极难靠肉眼发现。
try:
    from png_pixel import Png
    for name in ("shot_feed.png", "shot_detail.png"):
        p = ROOT / "logs" / name
        if not p.exists():
            continue
        im = Png(p)
        bluer = redder = 0
        for yy in range(0, im.h, 3):
            for xx in range(0, im.w, 3):
                rr, _g, bb = im.rgb(xx, yy)
                if bb - rr > 60:
                    bluer += 1
                elif rr - bb > 60:
                    redder += 1
        verdict = "正常" if bluer > redder else "!! 红蓝疑似互换"
        print(f"  色序自校验 {name}: 偏蓝={bluer} 偏红={redder} -> {verdict}")
except Exception as e:                      # noqa: BLE001
    print(f"  色序自校验跳过：{e}")

restore_once()      # 赶在后端被关闭之前还原（atexit 只是兜底）
gui.terminate(); time.sleep(1)
if gui.poll() is None:
    gui.kill()
bk.terminate(); time.sleep(1)
if bk.poll() is None:
    bk.kill()
print("完成")
