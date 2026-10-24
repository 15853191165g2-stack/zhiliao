"""生成「知了」品牌图标（多尺寸 .ico）。

图标语义：品牌蓝圆角方块 + 白色「知」字。
为什么不用 AI 生图：安装包图标要 16/24/32/48/64/128/256 七档，
每档都得清晰，生成式图片缩到 16px 会糊成一团；字形缩放反而更干净。

用法：
    python scripts/build_installer/make_icon.py <输出.ico>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 与前端 theme.dart / 桌面版 SEED 同色，不做第二套蓝
BRAND = (47, 107, 255)          # #2F6BFF
BRAND_DARK = (32, 84, 224)      # 渐变尾部，让大尺寸不那么平
WHITE = (255, 255, 255)

SIZES = [16, 24, 32, 48, 64, 128, 256]
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 粗体
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError("找不到可用的中文字体（msyhbd/msyh/simhei）")


def _vertical_gradient(px: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """竖直渐变底图。"""
    img = Image.new("RGB", (px, px))
    draw = ImageDraw.Draw(img)
    for y in range(px):
        t = y / max(px - 1, 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (px, y)], fill=color)
    return img


def render(size: int) -> Image.Image:
    """渲染单个尺寸的图标（RGBA，圆角外透明）。"""
    # 超采样再缩：直接按目标尺寸画，小尺寸字形边缘会是硬的锯齿
    scale = 8 if size <= 64 else 4
    px = size * scale

    radius = round(px * 0.22)
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, px - 1, px - 1], radius=radius, fill=255)

    base = _vertical_gradient(px, (74, 130, 255), BRAND_DARK).convert("RGBA")
    base.putalpha(mask)

    # 字形：占画布 62%，小尺寸略放大以保住辨识度
    ratio = 0.68 if size <= 24 else 0.62
    font = _font(round(px * ratio))
    glyph = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glyph)
    box = gd.textbbox((0, 0), "知", font=font)
    x = (px - (box[2] - box[0])) / 2 - box[0]
    y = (px - (box[3] - box[1])) / 2 - box[1]
    gd.text((x, y), "知", font=font, fill=WHITE + (255,))

    out = Image.alpha_composite(base, glyph)
    return out.resize((size, size), Image.LANCZOS)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python make_icon.py <输出.ico>")
        return 2
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)

    images = [render(s) for s in SIZES]
    # sizes 参数必须显式给出，否则 Pillow 只写单帧
    images[-1].save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"OK  已生成 {target}  ({target.stat().st_size} 字节, {len(SIZES)} 档尺寸)")

    # 另存 256px PNG，方便文档/PPT 里用
    png = target.with_suffix(".png")
    images[-1].save(png)
    print(f"OK  附带 {png.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
