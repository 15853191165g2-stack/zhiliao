"""截屏工具：把知了窗口拍下来存到 logs\\shot.png，用于验收。

用 Windows 自带能力（ctypes + GDI），不额外装 Pillow。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "shot.png"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def find_window(keyword: str) -> int:
    """按标题关键词找窗口句柄（只找可见的顶层窗口）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if keyword in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def capture(hwnd: int, out: Path) -> None:
    rect = wt.RECT()
    # 用 DwmGetWindowAttribute 拿不到就去掉边框
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, SRCCOPY)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0

    bufsize = w * h * 4
    buf = ctypes.create_string_buffer(bufsize)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)

    # BGRA -> PNG（手写最小 PNG 编码，避免依赖 Pillow）
    rows = []
    for y in range(h):
        line = buf.raw[y * w * 4: (y + 1) * w * 4]
        rows.append(b"\x00" + bytes(line[i + 2] for i in range(0, len(line), 4))
                    if False else b"\x00" + line)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        import zlib
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    import zlib
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    out.write_bytes(png)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    print(f"已保存 {out}  ({w}x{h})")


def main() -> None:
    kw = sys.argv[1] if len(sys.argv) > 1 else "知了"
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    deadline = time.time() + wait
    hwnd = 0
    while time.time() < deadline:
        hwnd = find_window(kw)
        if hwnd:
            break
        time.sleep(1)
    if not hwnd:
        print(f"没找到标题含「{kw}」的可见窗口")
        sys.exit(1)
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    print("找到窗口:", buf.value)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)
    capture(hwnd, OUT)


if __name__ == "__main__":
    main()
