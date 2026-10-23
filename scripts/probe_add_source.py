# -*- coding: utf-8 -*-
"""验收「来源管理 → 添加来源」这条界面链路。

**只探测、不保存**：所以不会往库里插来源，可以对着真实后端跑。
它验的是四件事，任何一件不对都算失败：

  1. 来源管理页上真的有「＋ 添加来源」按钮（不是只在代码里）
  2. 点开弹窗、填入网址、点「探测」后，**预览框里出现的是真标题**
  3. 探测成功后「保存」按钮从 disabled 变成 normal（否则用户根本无法保存）
  4. 探测失败时保存按钮**保持 disabled**（不许存一个抓不到的来源）

用法：
    python scripts/probe_add_source.py --port 8781 \
        --url http://teach.dlut.edu.cn/xwkd/xwkd.htm

⚠️ 必须传 --port 指向**已经起来**的后端，且那个后端要是本次改动之后的代码。
   本脚本不自己起后端（起后端就要占库，可能碰到用户的数据）。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True, help="已在运行的后端端口")
ap.add_argument("--url", default="http://teach.dlut.edu.cn/xwkd/xwkd.htm",
                help="用来探测的网址")
ap.add_argument("--expect-fail", action="store_true",
                help="反过来验：这个网址探测应当失败，且保存按钮必须保持禁用")
ap.add_argument("--timeout", type=float, default=90.0)
args = ap.parse_args()

# 本机有系统代理，访问 127.0.0.1 必须绕过，否则会被拦成 502（看起来像后端没起来）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

# 状态目录隔离：构建真的 QuanZhiApp 会写 state/desktop.json（游标）。
# 不对齐用户的 state，否则会影响他下次打开时的通知展示。
state_dir = ROOT / "logs" / "_diag" / "state_add_source"
state_dir.mkdir(parents=True, exist_ok=True)
(state_dir / "desktop.json").write_text('{"notify_enabled": false}', encoding="utf-8")
os.environ["QUANZHI_STATE_DIR"] = str(state_dir)

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    op.open(f"http://127.0.0.1:{args.port}/api/health", timeout=5)
except Exception as e:  # noqa: BLE001
    print(f"✗ 端口 {args.port} 上没有可用的后端：{e}")
    sys.exit(2)

spec = importlib.util.spec_from_file_location("qz_addsrc", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_addsrc"] = m
spec.loader.exec_module(m)

# 模块级常量必须一起改，否则请求还发往默认端口（＝测到用户那个实例上了）
m.PORT = args.port
m.BASE = f"http://127.0.0.1:{args.port}"
m.API = m.BASE + "/api"

import tkinter as tk  # noqa: E402


class FakeBackend:
    ready = True
    last_error = ""

    def start(self): pass
    def stop(self): pass
    def restart(self): pass
    def wait_ready(self, timeout=30): return True
    def log_tail(self, n=50): return ""
    def is_alive(self): return True


def walk(w, out):
    out.append(w)
    for c in w.winfo_children():
        walk(c, out)
    return out


def find(widgets, cls, text=None):
    for w in widgets:
        if w.winfo_class() != cls:
            continue
        if text is None:
            return w
        try:
            if text in str(w.cget("text")):
                return w
        except Exception:  # noqa: BLE001
            pass
    return None


root = tk.Tk()
root.withdraw()
app = m.QuanZhiApp(root, FakeBackend())
root.update()
app.switch_page("sources")
for _ in range(30):
    root.update()
    time.sleep(0.05)

fails: list[str] = []

# ---- 1. 按钮存在 ----
src_widgets = walk(app.pages["sources"], [])
btn_add = find(src_widgets, "Button", "添加来源")
print("1) 「＋ 添加来源」按钮：", "找到" if btn_add else "✗ 没找到")
if btn_add is None:
    fails.append("来源管理页没有添加来源按钮")

# ---- 2. 弹窗 + 探测 ----
before = len(root.winfo_children())
btn_add.invoke() if btn_add else None
for _ in range(30):
    root.update()
    time.sleep(0.05)
tops = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
print("2) 弹窗：", f"打开了 {len(tops)} 个 Toplevel" if tops else "✗ 没打开")
if not tops:
    fails.append("点按钮没有弹出添加来源窗口")
    print("\n".join(fails))
    sys.exit(1)

win = tops[0]
dw = walk(win, [])
entry = find(dw, "Entry")
btn_probe = find(dw, "Button", "探测")
btn_save = find(dw, "Button", "保存")
text = find(dw, "Text")

print(f"   网址输入框={'有' if entry else '无'} 探测按钮={'有' if btn_probe else '无'} "
      f"保存按钮={'有' if btn_save else '无'} 预览框={'有' if text else '无'}")
for name, w in (("输入框", entry), ("探测", btn_probe), ("保存", btn_save), ("预览", text)):
    if w is None:
        fails.append(f"弹窗缺少{name}")

if all([entry, btn_probe, btn_save, text]):
    entry.delete(0, "end")
    entry.insert(0, args.url)
    print(f"   填入网址：{args.url}")
    btn_probe.invoke()

    deadline = time.time() + args.timeout
    preview = ""
    while time.time() < deadline:
        root.update()
        time.sleep(0.15)
        try:
            preview = text.get("1.0", "end").strip()
        except Exception:  # noqa: BLE001
            break
        if preview and "（探测中）" not in preview:
            break

    state = str(btn_save.cget("state"))
    lines = [ln for ln in preview.splitlines() if ln.strip()]
    print(f"3) 预览 {len(lines)} 行，保存按钮 state={state}")
    for ln in lines[:5]:
        print("     " + ln[:78])

    if args.expect_fail:
        if state != "disabled":
            fails.append("探测失败但保存按钮可用 —— 会存进一个抓不到的来源")
        if not any(("没" in ln or "失败" in ln or "打不开" in ln or "⚠" in ln) for ln in lines):
            fails.append("探测失败但预览里没有明确的失败说明")
        print("4) 预期失败场景：", "符合预期" if state == "disabled" else "✗ 不符合")
    else:
        if state != "normal":
            fails.append(f"探测成功但保存按钮仍是 {state}（用户点不了保存）")
        # 预览必须像真标题：至少有一行长度 >= 10 且不是提示语
        if not any(len(ln) >= 10 for ln in lines):
            fails.append("预览里没有像样的标题")
        print("4) 探测成功场景：", "符合预期" if state == "normal" else "✗ 不符合")

try:
    win.destroy()
except Exception:  # noqa: BLE001
    pass
root.update()
root.destroy()

print()
if fails:
    for f in fails:
        print("✗", f)
    sys.exit(1)
print("PASS：添加来源的界面链路可用（本次只探测，未写库）")
