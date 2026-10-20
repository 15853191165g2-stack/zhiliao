# -*- coding: utf-8 -*-
"""滚动详情到底，截图证明「全文确实能看到结尾」。

做法：开详情 → 反复发送滚轮下滚 → 逐次截图，最后一张应是正文末尾。
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

# ★ 截图进程自己必须先 DPI 感知，否则 GetSystemMetrics / GetWindowRect 返回
#   缩放后的虚拟坐标，而 BitBlt 拷的是物理像素，结果只截到左上角一块
#   （高缩放下窗口右侧/底部被裁掉）。必须在任何 GDI 调用之前执行。
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


def find_win(keyword, want_pid):
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h) and pid_of(h) == want_pid:
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


env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

sys.path.insert(0, str(ROOT / "scripts"))
import user_flags  # noqa: E402

# ★ 端口自检必须在**启动自己的后端之前**（残留旧后端会让测试连到旧代码上）
if user_flags.guard_port(8765):
    sys.exit(2)

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


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:8765/api" + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with op.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ★ 拍图前先给**用户状态**拍快照，结束后逐行还原。
#   本脚本要注入真实鼠标点击打开一条通知，而桌面版打开详情会
#   **自动把该条标记为已读**（同邮件客户端，见 open_detail）——
#   不还原的话，每拍一次图用户就悄悄多一条「已读」。
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

# SetWindowPos 的 HWND_TOPMOST = -1 是**指针值**，不声明 argtypes 就会被当成
# 32 位 int 传，x64 上零扩展成 0x00000000FFFFFFFF —— 置顶静默失效，
# 于是桌面抓图抓到的还是盖在上面的窗口。
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wt.BOOL
HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE = -1, 0x0001, 0x0002


def pin_topmost(h) -> None:
    """抬到最顶层。SetForegroundWindow 会被 Windows 前台锁静默忽略，
    用它做「让窗口显示出来」的前提不可靠（曾截到过应用商店和 IDE）。"""
    user32.SetWindowPos(wt.HWND(h), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE)


def pid_of(h):
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return int(p.value)


user32.ShowWindow(hwnd, 3)
time.sleep(1.5)
pin_topmost(hwnd)
user32.SetForegroundWindow(hwnd)
user32.SetCursorPos(user32.GetSystemMetrics(0) - 3,
                    user32.GetSystemMetrics(1) - 3)
time.sleep(9)
pin_topmost(hwnd)
time.sleep(1.0)

rect = wt.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))

# 打开详情。
# ★ 点之前先确认这个坐标上最顶层的窗口确实是我们自己，否则这一下会点到
#   别的程序上去（会真的动用户的鼠标和键盘焦点）。
cx0, cy0 = rect.left + 400, rect.top + 260
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
top = int(user32.WindowFromPoint(wt.POINT(cx0, cy0)) or 0)
if pid_of(top) != gui.pid:
    print(f"!! ({cx0},{cy0}) 上最顶层窗口不属于本程序（PID={pid_of(top)}），"
          f"放弃模拟点击")
    gui.terminate(); bk.terminate()
    sys.exit(1)
user32.SetCursorPos(cx0, cy0)
time.sleep(0.4)
user32.mouse_event(0x0002, 0, 0, 0, 0)
time.sleep(0.08)
user32.mouse_event(0x0004, 0, 0, 0, 0)
time.sleep(3.5)

dw = 0
for pat in ("关于", "图书馆", "后勤", "通知"):
    hits = find_win(pat, gui.pid)
    hits = [h for h in hits if h != hwnd]
    if hits:
        dw = hits[0]
        break
if not dw:
    print("!! 详情没打开")
else:
    # ⚠️ 不能直接 ShowWindow(dw, 3) 最大化：本机 2560x1600 物理分辨率下，
    #   最大化后详情窗口有 1600px 高，正文**一屏就装下了**，
    #   滚轮怎么滚画面都不变，截出来的「顶部」和「底部」会是同一张图，
    #   完全证明不了「能滚到底」。所以显式给一个较小的尺寸。
    user32.SetWindowPos(wt.HWND(dw), wt.HWND(HWND_TOPMOST),
                        120, 90, 1100, 780, 0)
    time.sleep(1.5)
    user32.SetForegroundWindow(dw)
    time.sleep(0.8)
    pin_topmost(dw)
    # 鼠标放在详情内容区中间，确保滚轮事件落在详情窗口里
    r2 = wt.RECT()
    user32.GetWindowRect(dw, ctypes.byref(r2))
    print(f"详情窗口尺寸 = {r2.right - r2.left}x{r2.bottom - r2.top}")
    cx = (r2.left + r2.right) // 2
    cy = (r2.top + r2.bottom) // 2
    user32.SetCursorPos(cx, cy)
    time.sleep(0.5)

    grab(r2, ROOT / "logs" / "detail_top.png")
    print("已存 detail_top.png（顶部）")

    # 猛滚到底
    for i in range(40):
        user32.mouse_event(0x0800, 0, 0, -120, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(0.05)
    time.sleep(2)
    grab(r2, ROOT / "logs" / "detail_bottom.png")
    print("已存 detail_bottom.png（滚到底）")

    # ★ 自校验：两张图必须**不一样**，否则说明根本没滚动
    #   （曾经因为窗口给太高、正文一屏装得下，两张图字节完全相同，
    #    截图看着有，其实什么都没证明）。
    import hashlib
    ha = hashlib.md5((ROOT / "logs" / "detail_top.png").read_bytes()).hexdigest()
    hb = hashlib.md5((ROOT / "logs" / "detail_bottom.png").read_bytes()).hexdigest()
    if ha == hb:
        print("!! 顶部与底部两张图完全相同 —— 没滚动起来，截图不能作为证据")
    else:
        print(f"滚动有效性自校验：两图不同 ✓  (top={ha[:8]} bottom={hb[:8]})")

restore_once()      # 赶在后端被关闭之前还原（atexit 只是兜底）
gui.terminate(); time.sleep(1)
if gui.poll() is None:
    gui.kill()
bk.terminate(); time.sleep(1)
if bk.poll() is None:
    bk.kill()
print("完成")
