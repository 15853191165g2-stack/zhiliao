"""最小 PNG 像素读取器（只用标准库 zlib）。

为什么不用 PIL / Tk？
- 托管 venv 和便携运行时都**没装 Pillow**；
- Tk 的 `PhotoImage.get()` 在本机返回的是 **BGR** 顺序
  （实测：蓝色 #2F6BFF 被读成 (255,107,47)），直接拿它下结论会看错颜色；
- PowerShell 的 `Add-Type`（System.Drawing）被本机安全策略拦截。

所以这里自己解 PNG：读 IHDR + 逐行反滤波（PNG 的 5 种 filter 全实现）。
只支持非隔行的 8bit RGB / RGBA / 灰度，够用。

用法：
    python scripts/png_pixel.py logs/shot_flags.png 223 425 226 425
    python scripts/png_pixel.py logs/shot_flags.png --scan 425
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path


class Png:
    def __init__(self, path: str | Path) -> None:
        data = Path(path).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG 文件"
        pos = 8
        idat = bytearray()
        self.w = self.h = self.depth = self.ctype = self.interlace = 0
        while pos < len(data):
            (length,) = struct.unpack(">I", data[pos : pos + 4])
            ctype = data[pos + 4 : pos + 8]
            body = data[pos + 8 : pos + 8 + length]
            pos += 12 + length
            if ctype == b"IHDR":
                (self.w, self.h, self.depth, self.ctype, _comp, _filt, self.interlace) = struct.unpack(
                    ">IIBBBBB", body
                )
            elif ctype == b"IDAT":
                idat += body
            elif ctype == b"IEND":
                break
        assert self.depth == 8, f"只支持 8bit，实际 {self.depth}"
        assert self.interlace == 0, "不支持隔行 PNG"
        self.channels = {0: 1, 2: 3, 4: 2, 6: 4}[self.ctype]
        self.rows = self._unfilter(zlib.decompress(bytes(idat)))

    def _unfilter(self, raw: bytes) -> list[bytes]:
        stride = self.w * self.channels
        out: list[bytes] = []
        prev = bytearray(stride)
        p = 0
        for _ in range(self.h):
            ft = raw[p]
            line = bytearray(raw[p + 1 : p + 1 + stride])
            p += 1 + stride
            if ft == 0:
                pass
            elif ft == 1:  # Sub
                for i in range(self.channels, stride):
                    line[i] = (line[i] + line[i - self.channels]) & 0xFF
            elif ft == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ft == 3:  # Average
                for i in range(stride):
                    left = line[i - self.channels] if i >= self.channels else 0
                    line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
            elif ft == 4:  # Paeth
                for i in range(stride):
                    a = line[i - self.channels] if i >= self.channels else 0
                    b = prev[i]
                    c = prev[i - self.channels] if i >= self.channels else 0
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pred) & 0xFF
            else:
                raise ValueError(f"未知 filter 类型 {ft}")
            out.append(bytes(line))
            prev = line
        return out

    def rgb(self, x: int, y: int) -> tuple[int, int, int]:
        row = self.rows[y]
        i = x * self.channels
        if self.channels >= 3:
            return row[i], row[i + 1], row[i + 2]
        g = row[i]
        return g, g, g

    def hex(self, x: int, y: int) -> str:
        return "#%02X%02X%02X" % self.rgb(x, y)


def main() -> int:
    args = ["--scan" in sys.argv and "--scan" or ""]
    path = sys.argv[1]
    img = Png(path)
    print(f"图片尺寸: {img.w} x {img.h}  通道={img.channels}")
    if "--scan" in sys.argv:
        y = int(sys.argv[sys.argv.index("--scan") + 1])
        prev = None
        for x in range(img.w):
            c = img.rgb(x, y)
            if c != prev:
                print(f"  x={x}  {img.hex(x, y)}  {c}")
                prev = c
    else:
        nums = [int(v) for v in sys.argv[2:]]
        for i in range(0, len(nums) - 1, 2):
            x, y = nums[i], nums[i + 1]
            print(f"  ({x},{y}) = {img.hex(x, y)}  {img.rgb(x, y)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
