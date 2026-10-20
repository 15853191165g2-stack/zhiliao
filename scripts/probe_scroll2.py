# -*- coding: utf-8 -*-
"""真窗口版滚动验证：root 不 withdraw，让布局真实发生，
这样 canvas 的「可视高度」才是真的，滚动行为也才是真的。
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

spec = importlib.util.spec_from_file_location("qz_scroll2", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_scroll2"] = m
# 状态文件隔离：本脚本会创建真的 QuanZhiApp，它一旦轮询到新通知，
# 就会把游标写进 state/desktop.json —— 那会让用户下次打开**看不到**那条。
# 所有会构建界面的验收脚本，都必须把自己的状态目录挪到 _diag 下。
os.environ["QUANZHI_STATE_DIR"] = str(ROOT / "logs" / "_diag" / "state_scroll")
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
    # 真显示，真布局
    root.geometry("1240x820+60+40")
    app = m.QuanZhiApp(root, FakeBackend())
    root.update()
    time.sleep(1.5)
    root.update()

    # 主界面滚轮
    main_cv = app.canvas
    out.append("=== 主界面 ===")
    out.append(f"主 canvas 可视高度 = {main_cv.winfo_height()}")
    bb = main_cv.bbox("all")
    out.append(f"主内容高度 = {bb[3]-bb[1] if bb else 0}")
    root.event_generate("<MouseWheel>", delta=-120)
    root.update()
    out.append(f"主界面滚动后 yview = {tuple(round(v,4) for v in main_cv.yview())}")

    # 打详情
    before = set(root.winfo_children())
    item = app.items[0]
    app.open_detail(item)
    root.update()
    win = [w for w in set(root.winfo_children()) - before
           if isinstance(w, tk.Toplevel)][0]
    win.update()
    time.sleep(1.5)
    win.update()

    cvs = []
    def walk(w):
        if w.winfo_class() == "Canvas":
            cvs.append(w)
        for c in w.winfo_children():
            walk(c)
    walk(win)
    cv = cvs[0]

    out.append("")
    out.append("=== 详情窗口 ===")
    out.append(f"窗口尺寸 = {win.winfo_width()}x{win.winfo_height()}")
    out.append(f"详情 canvas 可视高度 = {cv.winfo_height()}")
    bb2 = cv.bbox("all")
    ch = (bb2[3] - bb2[1]) if bb2 else 0
    out.append(f"详情内容高度 = {ch}")
    out.append(f"★ 内容超出可视区（必须能滚）= {ch > cv.winfo_height()}")
    out.append(f"_wheel_target 指向详情 canvas = {app._wheel_target is cv}")

    y0 = tuple(round(v, 4) for v in cv.yview())
    out.append(f"滚动前 yview = {y0}")

    for _ in range(10):
        root.event_generate("<MouseWheel>", delta=-120)
        root.update()
    time.sleep(0.4)
    root.update()
    y1 = tuple(round(v, 4) for v in cv.yview())
    out.append(f"下滚 10 次后 yview = {y1}")
    out.append(f"★ 滚轮在详情里生效 = {y0 != y1}")

    # 确认主界面没被一起滚（应该保持不动）
    out.append(f"（同时）主界面 yview = {tuple(round(v,4) for v in main_cv.yview())}")

    cv.yview_moveto(1.0)
    root.update()
    y2 = tuple(round(v, 4) for v in cv.yview())
    out.append(f"滚到底 yview = {y2}   ★ 能看到结尾 = {y2[1] > 0.99}")

    # 用按钮路径关闭，验证滚轮交还 + 主界面恢复可用
    win.destroy()
    root.update()
    time.sleep(0.3)
    out.append("")
    out.append("=== 关闭详情后 ===")
    out.append(f"_wheel_target = {app._wheel_target}（应为 None）")
    main_cv.yview_moveto(0.0)
    root.update()
    m0 = tuple(round(v, 4) for v in main_cv.yview())
    root.event_generate("<MouseWheel>", delta=-120)
    root.update()
    time.sleep(0.2)
    m1 = tuple(round(v, 4) for v in main_cv.yview())
    out.append(f"主界面滚动前 = {m0}  滚动后 = {m1}")
    out.append(f"★ 主界面滚轮已恢复 = {m0 != m1}")

    report = "\n".join(out)
    (ROOT / "logs" / "scroll_probe2.txt").write_text(report, encoding="utf-8")
    print(report)
    root.destroy()
finally:
    bk.terminate()
    time.sleep(1)
    if bk.poll() is None:
        bk.kill()
