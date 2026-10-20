# -*- coding: utf-8 -*-
"""拍一张「多个通知气泡堆叠」的演示图（阶段 3 本地通知的视觉证据）。

三个气泡分别用 urgent / important / normal 三种重要度，
用来验证：
  * 气泡会**从下往上堆叠**，不会互相盖住；
  * 左侧强调条按重要度变色（红 / 橙 / 灰）；
  * 每个气泡都完整显示 **3 行摘要**。

⚠️ 这是**纯本地**演示：不连后端、不改任何数据、不注入鼠标点击。
   所以不需要 user_flags 那套快照还原 —— 它本来就只碰内存。

⚠️ 截图走 PrintWindow（窗口自己重绘），锁屏状态下也能拍到；
   并且用自写解码器 png_pixel 校验「拍出来的图不是纯色」。

用法：python\\python.exe scripts\\shot_toast.py
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import capture_shot as cs  # noqa: E402
from png_pixel import Png  # noqa: E402

OUT = ROOT / "logs"

DEMO = [
    {
        "id": None,
        "title": "【紧急】今晚 20:00 起图书馆临时闭馆",
        "source_name": "图书馆",
        "importance": "urgent",
        "importance_label": "紧急",
        "lines": [
            "因供电检修，今晚 20:00 起临时闭馆。",
            "预计次日 08:00 恢复开放。",
            "已借图书到期日不受影响。",
        ],
    },
    {
        "id": None,
        "title": "关于 2026 年秋季学期选课的通知",
        "source_name": "教务处",
        "importance": "important",
        "importance_label": "重要",
        "lines": [
            "选课将于 11 月 3 日 09:00 开放。",
            "共开放 128 门课程，含 12 门新增。",
            "请在 11 月 6 日前完成确认。",
        ],
    },
    {
        "id": None,
        "title": "研究生院学术讲座：人工智能与教育",
        "source_name": "研究生院",
        "importance": "normal",
        "importance_label": "一般",
        "lines": [
            "讲座时间 10 月 20 日 14:00。",
            "地点：主楼第一报告厅。",
            "无需报名，凭校园卡入场。",
        ],
    },
]


def main() -> int:
    import tkinter as tk

    spec = importlib.util.spec_from_file_location(
        "qz_toast_shot", ROOT / "知了桌面版.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["qz_toast_shot"] = m
    spec.loader.exec_module(m)
    m.DESKTOP_STATE = ROOT / "logs" / "_diag_toast_state.json"

    root = tk.Tk()
    root.geometry("1100x700+0+0")
    root.title("知了 · 通知气泡演示（本窗口不参与截图）")

    app = m.QuanZhiApp.__new__(m.QuanZhiApp)      # 不跑完整初始化
    app.root = root
    app.state = {"toast_seconds": 300}
    app.toasts = m.ToastManager(app)

    for msg in DEMO:
        app.toasts.show(msg)

    # 用 mainloop 驱动，保证气泡的 after / geometry 都真正生效
    shots: list[tuple[Path, tuple[int, int, int, int]]] = []

    def grab_all() -> None:
        for i, alive in enumerate(app.toasts.alive):
            win = alive["win"]
            win.update_idletasks()
            hwnd = cs.top_level_hwnd(win.winfo_id())
            got = cs.grab_window(hwnd)
            if got:
                rows, w, h, _rect = got
                p = OUT / f"_toast_part{i}.png"
                cs.write_png(p, rows, w, h)
                shots.append((p, (win.winfo_rootx(), win.winfo_rooty(), w, h)))
        root.quit()

    root.after(2500, grab_all)
    root.mainloop()

    print(f"拍到 {len(shots)} 个气泡")
    if not shots:
        print("!! 一个气泡都没拍到")
        return 1

    # ---- 堆叠顺序：最新的必须贴右下角，旧的往上让位 ----
    # shots 是按 self.alive 的插入顺序收集的，最后一个 = 最后弹的。
    order_ok = True
    ys = [s[1][1] for s in shots]
    bottoms = [s[1][1] + s[1][3] for s in shots]
    for i in range(len(shots) - 1):
        if bottoms[i] >= bottoms[i + 1]:      # 后弹的必须更低（y 更大）
            order_ok = False
    print(f"堆叠顺序（最新贴角）= {order_ok}   顶部 y={ys} / 底边 y={bottoms}")
    if len(shots) > 1 and not order_ok:
        print("!! 最新的气泡没有贴在右下角，堆叠方向反了")
        return 1

    # ---- 拼成一张图：按屏幕坐标的相对位置摆好，背景填浅色 ----
    x0 = min(s[1][0] for s in shots)
    y0 = min(s[1][1] for s in shots)
    x1 = max(s[1][0] + s[1][2] for s in shots)
    y1 = max(s[1][1] + s[1][3] for s in shots)
    pad = 24
    W = (x1 - x0) + pad * 2
    H = (y1 - y0) + pad * 2
    canvas = [[(246, 247, 251)] * W for _ in range(H)]

    for p, (rx, ry, w, h) in shots:
        img = Png(p)
        ox, oy = rx - x0 + pad, ry - y0 + pad
        for y in range(img.h):
            row = canvas[oy + y]
            for x in range(img.w):
                row[ox + x] = img.rgb(x, y)

    # write_png 固定写 RGBA（IHDR color type=6），所以每像素必须 4 字节。
    # 只写 3 字节会让行距错位，解码器会把背景色当成 filter 字节报
    # 「未知 filter 类型 246（=0xF6）」。这里补上 alpha=255。
    scan = bytearray()
    for row in canvas:
        scan.append(0)
        for r, g, b in row:
            scan += bytes((r, g, b, 255))
    out = OUT / "shot_toast_stack.png"
    cs.write_png(out, bytes(scan), W, H)

    check = Png(out)
    uniq = {check.rgb(x, y) for y in range(0, check.h, 7) for x in range(0, check.w, 7)}
    print(f"已生成 {out.name}  {W}x{H}  取色 {len(uniq)} 种")
    if len(uniq) < 20:
        print("!! 拼出来的图几乎是纯色，可能没拍到内容")
        return 1
    # 每个气泡的强调条颜色都要在
    for name, want in (("紧急", "#E5484D"), ("重要", "#F0A020"), ("一般", "#727A8C")):
        found = any(
            "#%02X%02X%02X" % px == want
            for px in uniq
        )
        print(f"  强调条 {name} {want} 出现 = {found}")

    for p, _ in shots:
        try:
            p.unlink()
        except Exception:  # noqa: BLE001
            pass

    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
