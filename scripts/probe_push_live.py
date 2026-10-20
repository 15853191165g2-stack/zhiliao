"""用**真实抓到的通知**验证桌面版推送闭环。

和 verify_push.py 的区别：
- verify_push.py 往**库副本**里手工插一条测试通知，证明「推送链路是通的」；
- 本脚本不造任何数据，直接用库里**真实抓取进来的**待推送通知，
  证明「真数据也能正常弹出来」。

安全约定（和项目其它验收脚本一致）：
- 库是**真库**，但只读它，不写一行（不置已读、不标 flag）；
- 状态文件走 QUANZHI_STATE_DIR 隔离到 logs/_diag，不碰用户的 state/desktop.json；
- 后端复用已经在 8765 上跑着的那个，脚本自己不起也不杀后端。

用法：
    python\\python.exe scripts\\probe_push_live.py [--port 8765]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tkinter as tk
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / "logs" / "_diag"
STATE = DIAG / "pushlive_state"
OUT = ROOT / "logs"

sys.path.insert(0, str(ROOT / "scripts"))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    tail = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{tail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--wait", type=int, default=60, help="等气泡出现的最长秒数")
    args = ap.parse_args()

    # ---- 环境：绕开系统代理，隔离 state ---- #
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 先问后端要队列，脚本要知道「应该弹哪几条」才能断言
    try:
        raw = op.open(f"http://127.0.0.1:{args.port}/api/push/pending?limit=20",
                      timeout=10).read()
        pending = json.loads(raw)["data"]
    except Exception as e:  # noqa: BLE001
        print(f"!! 连不上后端 127.0.0.1:{args.port}：{type(e).__name__}: {e}")
        print("   请先启动「知了.bat」，或手动起 uvicorn。")
        return 2

    items = pending.get("items") or []
    print("=" * 70)
    print("  知了 电脑版 · 真实数据推送闭环")
    print("=" * 70)
    print(f"后端队列待推送：{pending.get('total')} 条  max_id={pending.get('max_id')}")
    for it in items:
        print(f"   #{it['id']} {it['title'][:40]}  ({it.get('source_name')})")
    check("队列里有真实待推送通知", bool(items), f"实际 {len(items)} 条")
    if not items:
        return 1

    expect = items[0]
    expect_id = int(expect["id"])
    expect_title = (expect.get("title") or "").strip()
    expect_lines = [str(x).strip() for x in (expect.get("lines") or []) if str(x).strip()]
    check("推送消息带满 3 行摘要", len(expect_lines) == 3, f"实际 {len(expect_lines)} 行")

    # 模拟「上次已经看到 expect_id - 1」——这样队列里那条就是新的
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "desktop.json").write_text(json.dumps({
        "notify_enabled": True,
        "notify_interval": 5,
        "toast_seconds": 300,          # 别自动消失，方便检查
        "push_cursor": expect_id - 1,
        "push_cursor_ready": True,
        "notified_count": 0,
        "last_check": "",
    }, ensure_ascii=False), encoding="utf-8")
    os.environ["QUANZHI_STATE_DIR"] = str(STATE)
    os.environ["QUANZHI_PORT"] = str(args.port)

    spec = importlib.util.spec_from_file_location("qz_pushlive", ROOT / "知了桌面版.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["qz_pushlive"] = m
    spec.loader.exec_module(m)

    print("-" * 70)
    print(f"端口 {m.PORT} · 状态文件 {m.DESKTOP_STATE.name} · "
          f"起始游标 {expect_id - 1}")
    check("状态文件已隔离到 _diag", "_diag" in str(m.DESKTOP_STATE),
          str(m.DESKTOP_STATE))
    check("端口按参数生效", m.PORT == args.port, f"实际 {m.PORT}")

    # ---- 驱动真实界面 ---- #
    root = tk.Tk()
    root.withdraw()
    backend = m.Backend()
    backend.ready = True              # 复用已在跑的后端，不再起第二个
    app = m.QuanZhiApp(root, backend)

    R: dict = {"texts": [], "win": None, "found": False}

    def collect(win) -> list[str]:
        out: list[str] = []

        def walk(w):
            for ch in w.winfo_children():
                try:
                    t = ch.cget("text")
                    if t:
                        out.append(str(t))
                except Exception:  # noqa: BLE001
                    pass
                walk(ch)

        walk(win)
        return out

    def shoot() -> None:
        """气泡映射完成之后再截图 —— 刚建好那一瞬间 PrintWindow 会返回空。"""
        try:
            import capture_shot as cs
            from png_pixel import Png
            hwnd = cs.top_level_hwnd(R["win_hwnd"])
            g = cs.grab_window(hwnd)
            if not g:
                print("  (截图失败：PrintWindow 返回空)")
                return
            rows, ww, hh, _ = g
            path = OUT / "shot_push_live.png"
            cs.write_png(path, rows, ww, hh)
            img = Png(path)
            uniq = {(img.rgb(x, y)) for y in range(0, img.h, 4)
                    for x in range(0, img.w, 4)}
            check("气泡截图有内容（非纯色）", len(uniq) > 8,
                  f"{ww}x{hh} 取色 {len(uniq)} 种 -> {path.name}")
        except Exception as e:  # noqa: BLE001
            print(f"  (截图失败：{type(e).__name__}: {e})")

    def tick(n: int = 0) -> None:
        alive = [a for a in app.toasts.alive if app.toasts._exists(a["win"])]
        if alive and not R["found"]:
            win = alive[0]["win"]
            win.update_idletasks()
            R["win_hwnd"] = win.winfo_id()
            R["texts"] = collect(win)
            R["win"] = {
                "x": win.winfo_x(), "y": win.winfo_y(),
                "w": win.winfo_width(), "h": win.winfo_height(),
                "screen_w": root.winfo_screenwidth(),
                "screen_h": root.winfo_screenheight(),
                "override": bool(win.overrideredirect()),
            }
            R["found"] = True
            root.after(1200, shoot)
            root.after(2200, root.destroy)
            return
        if n * 500 >= args.wait * 1000:
            root.destroy()
            return
        root.after(500, lambda: tick(n + 1))

    root.after(1500, lambda: tick(0))
    root.mainloop()

    print("-" * 70)
    check("桌面版自动弹出了通知气泡", R["found"],
          "等待 %.0fs 未出现" % args.wait if not R["found"] else "")

    if R["found"]:
        texts = R["texts"]
        blob = " | ".join(texts)
        print(f"气泡文字：\n  {blob}\n")
        check("气泡是刚才抓到的真实标题",
              any(expect_title[:16] in t for t in texts),
              expect_title[:40])
        for i, line in enumerate(expect_lines, 1):
            check(f"气泡里含第 {i} 行摘要", any(line[:14] in t for t in texts), line[:30])
        check("气泡标注了来源",
              any((expect.get("source_name") or "")[:6] in t for t in texts),
              str(expect.get("source_name")))
        check("气泡带重要程度标签",
              any((expect.get("importance_label") or "") in t for t in texts),
              str(expect.get("importance_label")))
        w = R["win"]
        print(f"气泡位置：x={w['x']} y={w['y']} 尺寸={w['w']}x{w['h']} "
              f"屏={w['screen_w']}x{w['screen_h']}")
        check("气泡贴在屏幕右下角",
              w["x"] + w["w"] >= w["screen_w"] - 80 and w["y"] > w["screen_h"] * 0.5)
        check("气泡无边框", w["override"])

    print("=" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  !! {f}")
    print("RESULT:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
