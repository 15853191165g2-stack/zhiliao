# -*- coding: utf-8 -*-
"""验证「置顶 / 已读勾选 / 已读未读差异化标注」在界面上真的可用。

为什么要有这个脚本：前两轮吃过亏 ——
  * 界面到底渲染成什么样，**不能靠截图判断**（截图受缓存、前台窗口、
    DPI、僵尸进程影响）。要在进程内直接读控件的真实属性。
  * 光「代码看起来对」不算数，必须真的调用一次、真的看到状态变化。

本脚本会：
  1. 构造真的 QuanZhiApp（自带后端）
  2. 断言每张卡片上都有「置顶」开关和「已读」勾选框
  3. 断言卡片的整块点击**没有**劫持这两个开关
  4. 真的点一次已读勾选框，逐项对比「未读 / 已读」两种标注的差异
  5. 真的置顶一条**原本不在最前**的通知，断言它排到了第一位
  6. 全部还原，不改用户的标记

⚠️ 测勾选框要用 `chk.invoke()`，不要直接调 `toggle_read()`：
   `toggle_read` 是拿勾选框的当前值当「用户想要的状态」——
   直接调用时勾选框没被翻过，等于「把未读标成未读」，什么都不会发生。
   invoke() 才是真实点击（翻变量 + 触发 command）。

用法：python\\python.exe scripts\\probe_flags.py [--port 8765]

    --port 默认 8765。用户自己的「知了」正开着时会占住 8765，
    这时换一个空端口跑，就不会和自己的实例抢，也**不会**误测到旧代码。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_ap = argparse.ArgumentParser()
_ap.add_argument("--port", type=int, default=8765)
PORT = _ap.parse_args().port
API_BASE = f"http://127.0.0.1:{PORT}"

env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
os.environ.update(env)

spec = importlib.util.spec_from_file_location("qz_flags_probe", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_flags_probe"] = m
# 状态文件隔离：本脚本会创建真的 QuanZhiApp，它一旦轮询到新通知，
# 就会把游标写进 state/desktop.json —— 那会让用户下次打开**看不到**那条。
# 所有会构建界面的验收脚本，都必须把自己的状态目录挪到 _diag 下。
os.environ["QUANZHI_STATE_DIR"] = str(ROOT / "logs" / "_diag" / "state_probe_flags")
_sd = Path(os.environ["QUANZHI_STATE_DIR"])
_sd.mkdir(parents=True, exist_ok=True)
# 这个副本里关掉通知：本脚本测的不是通知，而气泡弹出时会顺带
# 刷新信息流（_on_push 里 load_feed(reset=True)），把 _views 清空重建 ——
# 检查时机撞上就会看到「0 张卡片」。
(_sd / "desktop.json").write_text('{"notify_enabled": false}', encoding="utf-8")
spec.loader.exec_module(m)

# ⚠️ 光起了自己的后端还不够：QuanZhiApp 内部走的是模块级的 API 常量，
#    不覆盖它就会去连**默认的 8765**。那个端口上如果恰好有别的实例在跑，
#    脚本照样「通过」，但验证的其实是别人的后端 —— 又是一次假绿。
#    （实测踩过：8765 空着时本脚本直接崩在 views[0]，因为它连不上。）
m.API = f"http://127.0.0.1:{PORT}/api"

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ★ 用户状态快照 + 端口自检。本探针会真的勾选「已读」、真的置顶，
#   虽然脚本内自己会切回来，但一旦中途断言失败/抛异常就会留下残留，
#   所以统一走 user_flags 兜底。
sys.path.insert(0, str(ROOT / "scripts"))
import user_flags as uf  # noqa: E402

# 端口被残留旧后端占着时，自己启的后端会绑定失败，但 /health 依然有响应 ——
# 那样测的就是**旧代码**，全绿也无意义。
if uf.guard_port(PORT):
    sys.exit(2)

bk = subprocess.Popen(
    [str(ROOT / "python" / "python.exe"), "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "error"],
    cwd=str(ROOT / "backend"), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
for _ in range(60):
    try:
        op.open(f"{API_BASE}/api/health", timeout=3)
        break
    except Exception:                       # noqa: BLE001
        time.sleep(1)

# ★ 用户状态快照。本探针会真的勾选「已读」、真的置顶，虽然脚本内自己会切回来，
#   但一旦中途断言失败/抛异常就会留下残留。统一走 user_flags 兜底。


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/api" + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with op.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


_before = uf.snapshot()

out: list[str] = []
ok = bad = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        out.append(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        bad += 1
        out.append(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


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
    # ⚠️ 必须用**真实显示**的窗口：withdraw() 之后 Tk 不做真实布局，
    #    winfo_height() 会返回 1，量不出「状态条有没有撑满卡片」。
    root.geometry("1300x820+0+0")
    app = m.QuanZhiApp(root, FakeBackend())
    root.update()
    time.sleep(1.5)
    root.update()

    out.append("=" * 62)
    out.append("  知了 · 置顶 / 已读 界面功能实测")
    out.append("=" * 62)

    views = app._views
    out.append(f"\n[1] 卡片与控件")
    check("卡片区渲染出了卡片", len(views) > 0, f"{len(views)} 张")

    def kids(w):
        acc = []
        for c in w.winfo_children():
            acc.append(c)
            acc.extend(kids(c))
        return acc

    v0 = views[0]
    tree = kids(v0["card"])
    chks = [w for w in tree if w.winfo_class() == "Checkbutton"]
    check("每张卡片恰好 1 个已读勾选框", len(chks) == 1, f"{len(chks)} 个")
    check("勾选框文字是「已读」",
          chks and str(chks[0].cget("text")) == "已读",
          repr(chks[0].cget("text")) if chks else "-")
    check("置顶开关存在且默认文案是「置顶」",
          str(v0["pin_lbl"].cget("text")) == "置顶",
          repr(v0["pin_lbl"].cget("text")))
    check("左侧状态条是 4px 宽", int(v0["accent"].cget("width")) == 4,
          f"width={v0['accent'].cget('width')}")

    # 光有颜色不够 —— 状态条还得真的**画出来**（高度撑满、贴在卡片左缘）。
    # 每次重绘只改 bg 不改尺寸，这里量的是真实布局后的几何。
    root.update_idletasks()
    ah, aw = v0["accent"].winfo_height(), v0["accent"].winfo_width()
    ch, cx = v0["card"].winfo_height(), v0["accent"].winfo_rootx() - v0["card"].winfo_rootx()
    check("★ 状态条真的撑满卡片高度", ah > 100 and abs(ah - ch) <= 4,
          f"状态条高={ah} 卡片高={ch}")
    check("★ 状态条贴在卡片左缘（含 1px 描边）", 0 <= cx <= 2, f"偏移 x={cx}")

    # ---- 整卡点击不能劫持这两个开关 ----
    out.append("\n[2] 点击归属（开关不能被「打开详情」抢走）")
    check("勾选框没有被绑上「打开详情」",
          not chks[0].bind("<Button-1>"))
    check("置顶开关有自己的点击绑定",
          bool(v0["pin_lbl"].bind("<Button-1>")))

    # 「有没有绑定」说明不了性质 —— 收藏星也绑了东西，绑的是它自己的 toggle_star。
    # 所以这里换成**功能性**判断：真的点一下，看触发的是哪个处理函数。
    fired: list[str] = []
    real_open, real_pin, real_read, real_star = (
        app.open_detail, app.toggle_pin, app.toggle_read, app.toggle_star)
    app.open_detail = lambda it: fired.append("open_detail")
    app.toggle_pin = lambda v: fired.append("toggle_pin")
    app.toggle_read = lambda v: fired.append("toggle_read")
    app.toggle_star = lambda v: fired.append("toggle_star")
    try:
        v0["pin_lbl"].event_generate("<Button-1>")
        root.update()
        check("点置顶开关 → 触发置顶，且不打开详情",
              fired == ["toggle_pin"], f"实际触发 {fired}")

        fired.clear()
        chks[0].invoke()
        root.update()
        check("点勾选框 → 触发已读切换，且不打开详情",
              fired == ["toggle_read"], f"实际触发 {fired}")

        fired.clear()
        v0["star_lbl"].event_generate("<Button-1>")
        root.update()
        check("点收藏星 → 触发收藏，且不打开详情",
              fired == ["toggle_star"], f"实际触发 {fired}")

        fired.clear()
        # 卡片正文里的一个普通控件：应该照旧打开详情
        body_lbl = [w for w in tree
                    if w.winfo_class() == "Label"
                    and str(w.cget("text")).startswith(("•", "  "))][0]
        body_lbl.event_generate("<Button-1>")
        root.update()
        check("点正文 → 打开详情",
              fired == ["open_detail"], f"实际触发 {fired}")
    finally:
        (app.open_detail, app.toggle_pin, app.toggle_read, app.toggle_star) = (
            real_open, real_pin, real_read, real_star)

    # 上面 invoke() 把 v0 的勾选框翻过、但处理函数被换成了记录器，
    # 于是 v0 的「变量值」和「数据里的是否已读」会有偏差。重新拉一次列表消掉它。
    app.load_feed(reset=True)
    root.update()
    views = app._views

    # ---- 已读 / 未读 两种标注的差异 ----
    out.append("\n[3] 未读 / 已读 的标注差异（真的切一次）")
    # 挑一条「既未读、又没置顶」的：置顶卡片底色是暖色，会干扰底色对比
    target = next((v for v in views
                   if not v["item"].get("is_read") and not v["item"].get("is_pinned")), None)
    if target is None:
        out.append("  当前没有未读通知，无法对比")
    else:
        unread_style = {
            "accent": str(target["accent"].cget("bg")),
            "card": str(target["card"].cget("bg")),
            "pill_text": str(target["read_pill"].cget("text")),
            "pill_bg": str(target["read_pill"].cget("bg")),
            "sum0_bold": "bold" in str(target["sum_labels"][0].cget("font")),
            "title_bold": "bold" in str(target["title_lbl"].cget("font")),
            "checked": bool(target["chk_var"].get()),
        }
        out.append(f"  未读样式 = {unread_style}")

        check("未读：胶囊写「未读」", unread_style["pill_text"] == "未读")
        check("未读：状态条是主色蓝", unread_style["accent"] == m.UNREAD_BAR,
              unread_style["accent"])
        check("未读：卡片是纯白底", unread_style["card"] == m.CARD)
        check("未读：勾选框未勾选", unread_style["checked"] is False)
        check("未读：首行摘要加粗", unread_style["sum0_bold"] is True)

        # 真切换：用 invoke() 模拟真实点击（翻变量 + 触发 command）
        target["chk"].invoke()
        root.update()
        read_style = {
            "accent": str(target["accent"].cget("bg")),
            "card": str(target["card"].cget("bg")),
            "pill_text": str(target["read_pill"].cget("text")),
            "pill_bg": str(target["read_pill"].cget("bg")),
            "sum0_bold": "bold" in str(target["sum_labels"][0].cget("font")),
            "title_bold": "bold" in str(target["title_lbl"].cget("font")),
            "checked": bool(target["chk_var"].get()),
        }
        out.append(f"  已读样式 = {read_style}")

        check("★ 勾选真的写进了数据", bool(target["item"].get("is_read")) is True)
        check("已读：胶囊改名「已读」", read_style["pill_text"] == "已读")
        check("★ 已读 / 未读 的胶囊不一样",
              read_style["pill_text"] != unread_style["pill_text"])
        check("★ 已读 / 未读 的状态条颜色不一样",
              read_style["accent"] != unread_style["accent"],
              f"{read_style['accent']} vs {unread_style['accent']}")
        check("★ 已读 / 未读 的卡片底色不一样",
              read_style["card"] != unread_style["card"],
              f"{read_style['card']} vs {unread_style['card']}")
        check("★ 已读后首行不再加粗",
              read_style["sum0_bold"] is False and unread_style["sum0_bold"] is True)
        check("已读后勾选框是勾上的", read_style["checked"] is True)
        check("已读后状态条变中性灰", read_style["accent"] == m.READ_BAR,
              read_style["accent"])

        # 还原：再点一次勾选框取消已读
        target["chk"].invoke()
        root.update()
        check("★ 取消勾选能回到未读",
              target["item"].get("is_read") is False
              and str(target["read_pill"].cget("text")) == "未读"
              and str(target["accent"].cget("bg")) == m.UNREAD_BAR)

    # ---- 置顶 ----
    out.append("\n[4] 置顶（唯一的可观察效果 = 排到最前）")
    # ★ 故意挑一张**不在最前**的卡片：如果拿本来就第一的那条去测，
    #   「排到最前」这个断言永远成立，测了等于没测。
    top_id = app._views[0]["item"]["id"]
    target = next((v for v in reversed(app._views)
                   if not v["item"].get("is_pinned")
                   and v["item"]["id"] != top_id), None)
    if target is None:
        out.append("  没有可置顶的非首条通知，跳过")
    else:
        tid = target["item"]["id"]
        pos_before = [v["item"]["id"] for v in app._views].index(tid)
        out.append(f"  置顶前：第一位 id={top_id}，本次置顶 id={tid}（原本在第 {pos_before + 1} 位）")

        app.toggle_pin(target)
        root.update()
        time.sleep(0.6)
        root.update()

        # toggle_pin 会整页重取，视图要重新拿
        view_now = {v["item"]["id"]: v for v in app._views}
        check("★ 置顶写进了数据", bool(view_now[tid]["item"].get("is_pinned")) is True)
        check("★ 置顶后排到列表第一位",
              app._views[0]["item"]["id"] == tid,
              f"第一位 id={app._views[0]['item']['id']}")
        check("★ 确实发生了位移（原本不在第一位）", pos_before > 0,
              f"原本位次 {pos_before + 1}")
        check("置顶开关文案变成「已置顶」",
              str(view_now[tid]["pin_lbl"].cget("text")) == "已置顶",
              repr(view_now[tid]["pin_lbl"].cget("text")))
        check("置顶卡片底色和普通卡片不同",
              str(view_now[tid]["card"].cget("bg")) == m.PIN_BG,
              str(view_now[tid]["card"].cget("bg")))

        # 还原
        app.toggle_pin(view_now[tid])
        root.update()
        time.sleep(0.6)
        root.update()
        check("★ 取消置顶后回到原位",
              app._views[0]["item"]["id"] == top_id,
              f"第一位 id={app._views[0]['item']['id']}")

    # ---- 汇总 ----
    out.append("\n" + "=" * 62)
    out.append(f"  结果：{ok} 通过 / {bad} 失败 / 共 {ok + bad} 项")
    out.append("=" * 62)

    report = "\n".join(out)
    (ROOT / "logs" / "flags_probe.txt").write_text(report, encoding="utf-8")
    print(report)
    root.destroy()
    sys.exit(1 if bad else 0)

finally:
    # ★ 还原要赶在后端关闭之前（还原是调接口做的）。
    #   路径：正常跑完 → sys.exit 触发 finally；中途抛异常 → 同样进这里。
    try:
        n = uf.restore(_before, post=api_post)
        same = uf.verify(_before)
        print(f"用户状态还原：{n} 处；与快照比对 = {'一致' if same else '仍有差异！'}")
    except Exception as e:                  # noqa: BLE001
        print(f"!! 用户状态还原失败：{e}")
    try:
        bk.terminate()
        bk.wait(timeout=8)
    except Exception:                       # noqa: BLE001
        try:
            bk.kill()
        except Exception:                   # noqa: BLE001
            pass
