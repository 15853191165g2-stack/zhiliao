# -*- coding: utf-8 -*-
"""让应用自己把界面「画」出来并自检，不依赖任何外部抓屏。

做法：在同一个进程里构造 QuanZhiApp，然后
  1) 打印 self.items / 卡片控件数 —— 逻辑层真相
  2) 递归统计所有 Label 的文字 —— 界面层真相
两者一致，就说明界面确实显示了数据；外部截图的差异与代码无关。
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

spec = importlib.util.spec_from_file_location("qz_selfcheck", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_selfcheck"] = m
# 状态文件隔离：本脚本会创建真的 QuanZhiApp，它一旦轮询到新通知，
# 就会把游标写进 state/desktop.json —— 那会让用户下次打开**看不到**那条。
# 所有会构建界面的验收脚本，都必须把自己的状态目录挪到 _diag 下。
os.environ["QUANZHI_STATE_DIR"] = str(ROOT / "logs" / "_diag" / "state_selfcheck")
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

# 不发 guard_port 的话：自己的后端绑定失败退出，但 /health 仍有响应，
# 于是整轮测试对着**别人的旧代码**跑，全绿也没意义。
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

    out = []
    out.append(f"FONT 实际取值        = {m.FONT}")
    out.append(f"逻辑层 total         = {app.total}")
    out.append(f"逻辑层 items 条数    = {len(app.items)}")
    out.append(f"状态栏文字           = {app.status_label.cget('text')}")
    out.append(f"计数标签文字         = {app.feed_count.cget('text')}")
    out.append(f"分页标签文字         = {app.page_label.cget('text')}")
    out.append(f"分类下拉当前值       = {app.cat_var.get()}")
    out.append(f"重要度下拉当前值     = {app.imp_var.get()}")
    out.append(f"卡片容器直接子控件数 = {len(app.card_holder.winfo_children())}")

    # 递归收集所有 Label 文字，看界面层到底写了什么
    texts = []

    def walk(w, depth=0):
        try:
            cls = w.winfo_class()
        except Exception:
            return
        if cls == "Label":
            t = w.cget("text")
            if t and str(t).strip():
                texts.append(str(t))
        elif cls == "Text":
            try:
                t = w.get("1.0", "end").strip()
                if t:
                    texts.append("[Text] " + t[:60])
            except Exception:
                pass
        for c in w.winfo_children():
            walk(c, depth + 1)

    walk(app.content)
    out.append("")
    out.append(f"--- 当前页面（{app.current_page}）里的 Label 文字，共 {len(texts)} 条 ---")
    for t in texts[:45]:
        out.append("   " + t)

    # 字体真实族名 & 字号（Tk 解析后的结果）
    try:
        fam = m.tkfont.Font(font=(m.FONT, m.FS_MD)).actual()
        out.append("")
        out.append(f"正文字体解析结果     = {fam}")
    except Exception as e:
        out.append(f"字体解析失败: {e}")

    report = "\n".join(out)
    (ROOT / "logs" / "selfcheck_ui.txt").write_text(report, encoding="utf-8")
    print(report)
    root.destroy()
finally:
    bk.terminate()
    time.sleep(1)
    if bk.poll() is None:
        bk.kill()
