# -*- coding: utf-8 -*-
"""验证卡片上「每一个」控件都真的绑定了点击事件。

针对上次的 bug：chip_row（关键信息标签行）漏绑，点那一块没反应。
本脚本会递归遍历卡片内所有控件，逐个检查 <Button-1> 绑定情况，
并实际调用一次事件处理函数验证它能打开详情。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_ap = argparse.ArgumentParser()
_ap.add_argument("--port", type=int, default=8765,
                 help="后端端口（默认 8765）。用户自己的实例占着 8765 时换一个空端口。")
PORT = _ap.parse_args().port

env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
os.environ.update(env)

spec = importlib.util.spec_from_file_location("qz_click_probe", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_click_probe"] = m
# 状态文件隔离：本脚本会创建真的 QuanZhiApp，它一旦轮询到新通知，
# 就会把游标写进 state/desktop.json —— 那会让用户下次打开**看不到**那条。
# 所有会构建界面的验收脚本，都必须把自己的状态目录挪到 _diag 下。
os.environ["QUANZHI_STATE_DIR"] = str(ROOT / "logs" / "_diag" / "state_click")
_sd = Path(os.environ["QUANZHI_STATE_DIR"])
_sd.mkdir(parents=True, exist_ok=True)
# 这个副本里关掉通知：本脚本测的不是通知，而气泡弹出时会顺带
# 刷新信息流（_on_push 里 load_feed(reset=True)），把 _views 清空重建 ——
# 检查时机撞上就会看到「0 张卡片」。
(_sd / "desktop.json").write_text('{"notify_enabled": false}', encoding="utf-8")
spec.loader.exec_module(m)

# ★ 桌面壳的 PORT / BASE / API 是模块级常量，请求在调用时才去读 API。
#   这里必须一起覆盖，否则 App 仍然往 8765 发请求 —— 就测到用户那个实例上了。
m.PORT = PORT
m.BASE = f"http://127.0.0.1:{PORT}"
m.API = m.BASE + "/api"

import user_flags as uf  # noqa: E402

if uf.guard_port(PORT):
    sys.exit(2)

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
bk = subprocess.Popen([str(ROOT / "python" / "python.exe"), "-m", "uvicorn",
                       "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
                       "--log-level", "error"],
                      cwd=str(ROOT / "backend"), env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
for _ in range(60):
    try:
        op.open(f"http://127.0.0.1:{PORT}/api/health", timeout=3)
        break
    except Exception:
        time.sleep(1)

out = []
try:
    import tkinter as tk

    class FakeBackend:
        ready = True
        last_error = ""
        def start(self): pass
        def stop(self): pass
        def restart(self): pass
        def wait_ready(self, timeout=30): return True
        def log_tail(self, n=50): return ""
        def is_alive(self): return True

    root = tk.Tk()
    root.withdraw()
    app = m.QuanZhiApp(root, FakeBackend())
    root.update()
    time.sleep(1.5)
    root.update()

    cards = app.card_holder.winfo_children()
    out.append(f"卡片数 = {len(cards)}")
    card = cards[0]

    # 递归收集卡片内所有控件
    widgets = []

    def collect(w, depth=0):
        widgets.append((w, depth, w.winfo_class()))
        for c in w.winfo_children():
            collect(c, depth + 1)

    collect(card)

    bound, unbound = [], []
    for w, depth, cls in widgets:
        b = w.bind("<Button-1>")
        if b:
            bound.append((depth, cls, (w.cget("text") if cls == "Label" else "")[:34], b))
        else:
            unbound.append((depth, cls, (w.cget("text") if cls == "Label" else "")[:40]))

    out.append(f"卡片内控件总数 = {len(widgets)}")
    out.append(f"已绑定点击 = {len(bound)}")
    out.append(f"未绑定点击 = {len(unbound)}")
    out.append("")
    out.append("--- 未绑定的控件（正常情况下只应有「收藏星 / 置顶开关 / 已读勾选框」）---")
    for depth, cls, txt in unbound:
        out.append(f"  {'  '*depth}{cls}: {txt!r}")

    # 关键点：chip_row 里的标签是否已绑定
    chip_texts = []
    for w, depth, cls in widgets:
        if cls == "Label":
            t = str(w.cget("text"))
            if t.startswith(("📍", "☎", "👥", "📎", "⏰")):
                chip_texts.append((t[:34], bool(w.bind("<Button-1>"))))
    out.append("")
    out.append("--- 关键信息标签（chip）的点击绑定 ---")
    for t, ok in chip_texts:
        out.append(f"  {'✅' if ok else '❌'} {t}")

    # 真正触发一次点击，确认能开详情
    opened = []
    orig_open = app.open_detail
    app.open_detail = lambda it: opened.append(it.get("id"))
    target = None
    for w, depth, cls in widgets:
        if cls == "Label" and str(w.cget("text")).startswith(("📍", "☎")):
            target = w
            break
    out.append("")
    if target is not None:
        out.append(f"模拟点击 chip Label: {str(target.cget('text'))[:30]!r}")
        target.event_generate("<Button-1>")
        root.update()
        out.append(f"是否触发了 open_detail = {bool(opened)}  (id={opened})")
    else:
        out.append("这条通知没有 chip 标签，换个样本再测")

    # 滚轮归属检查
    out.append("")
    out.append(f"_active_canvas 已设置 = {app._active_canvas is not None}")
    out.append(f"详情未开时 _wheel_target = {app._wheel_target}")

    app.open_detail = orig_open
    report = "\n".join(out)
    (ROOT / "logs" / "click_probe.txt").write_text(report, encoding="utf-8")
    print(report)
    root.destroy()
finally:
    bk.terminate()
    time.sleep(1)
    if bk.poll() is None:
        bk.kill()
