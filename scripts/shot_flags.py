# -*- coding: utf-8 -*-
"""给「置顶 / 已读」截一张能看懂的演示图。

单靠截图判断 UI 对错是不可靠的（见 probe_flags.py 的说明），
但**给人看的功能演示**还是要有图。

── 为什么不用「另起 pythonw 进程 + 抓桌面再裁剪」那一套 ──
那条路踩了三个坑，全是环境性的、不值得再修：
  1. 窗口认不出来：启动早期存在多个标题匹配、尺寸很小（158x26 / 237x39）的窗口，
     取「第一个」会拿到假目标；
  2. 窗口不在最上面：SetForegroundWindow 会被 Windows「前台窗口锁」静默忽略，
     SetWindowPos(TOPMOST) 也时灵时不灵 —— 先后截到过 Microsoft Store 和 IDE 自己；
  3. 截图进程若不 DPI 感知，GetSystemMetrics 会虚报尺寸、BitBlt 只拷到屏幕左上角，
     右边一大截（正好是置顶按钮和已读勾选框）被裁掉。
现在改成**同进程构造窗口**：`root.attributes("-topmost", True)` 是 Tk 自己的 API，
一定生效；也不用找窗口（窗口就是自己的）。

── 数据安全 ──
演示需要真的置顶一条。为避免「脚本崩在中间留下脏状态」，
动手**之前**先把全部 is_read / is_pinned / is_starred 快照下来，
收尾时逐条比对、只还原**发生变化**的那些，最后断言已完全一致。
（之前用「finally 里取消一次置顶」的写法就漏过：真的残留了 2 条置顶。）

产出：logs/shot_flags.png
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import ctypes.wintypes as wt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import capture_shot as cs  # noqa: E402  复用抓图/存 PNG（导入时即设 DPI 感知）
import user_flags as uf    # noqa: E402  用户状态的快照 / 还原（唯一实现）

OUT = ROOT / "logs" / "shot_flags.png"
DB = ROOT / "backend" / "quanzhi.db"

env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
env["PYTHONPATH"] = str(ROOT / "backend")
os.environ.update(env)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
API = "http://127.0.0.1:8765/api"


def call(path: str, payload=None, method="GET"):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot() -> dict[int, tuple[int, int, int]]:
    """当前所有通知的 (is_read, is_pinned, is_starred)。只读打开，不锁库。

    ⚠️ 实现已收敛到 `scripts/user_flags.py`，本脚本不再自带一份 ——
    之前三个截图脚本各写各的快照/还原，最后变成了「有的还原了、有的没还原」，
    残留了真实数据且极难发现。
    """
    return uf.snapshot()


def restore(before: dict[int, tuple[int, int, int]]) -> int:
    """只把变了的还原回去，返回还原处数。"""
    return uf.restore(before,
                      post=lambda path, payload: call(path, payload, "POST"),
                      log=cs.log)


def looks_blank(rows: bytes, w: int, h: int) -> bool:
    step = max(1, (w * h) // 400)
    px = set()
    for i in range(0, w * h, step):
        px.add(bytes(rows[i * 4 + 1:i * 4 + 4]))
        if len(px) > 6:
            return False
    return True


def main() -> int:
    cs.log("=== 新功能演示截图（同进程）===")
    cs.kill_stale_gui()
    cs.log(f"截图进程 DPI 感知后屏幕 = {cs.user32.GetSystemMetrics(0)}"
           f"x{cs.user32.GetSystemMetrics(1)}")

    spec = importlib.util.spec_from_file_location("qz_shot", ROOT / "知了桌面版.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["qz_shot"] = m
# 状态文件隔离：本脚本会创建真的 QuanZhiApp，它一旦轮询到新通知，
    # 就会把游标写进 state/desktop.json —— 那会让用户下次打开**看不到**那条。
    # 所有会构建界面的验收脚本，都必须把自己的状态目录挪到 _diag 下。
    os.environ["QUANZHI_STATE_DIR"] = str(ROOT / "logs" / "_diag" / "state_shot_flags")
    _sd = Path(os.environ["QUANZHI_STATE_DIR"])
    _sd.mkdir(parents=True, exist_ok=True)
    # 这个副本里关掉通知：本脚本测的不是通知，而气泡弹出时会顺带
    # 刷新信息流（_on_push 里 load_feed(reset=True)），把 _views 清空重建 ——
    # 检查时机撞上就会看到「0 张卡片」。
    (_sd / "desktop.json").write_text('{"notify_enabled": false}', encoding="utf-8")
    spec.loader.exec_module(m)

    bk = None
    root = None
    before = snapshot()

    # ★ 端口自检必须在**启动自己的后端之前**（残留旧后端会让脚本连到旧代码上，
    #   表现是"后端瞬间就绪"、图也照出，但其实是别人在应答）
    if uf.guard_port(8765, log=cs.log):
        cs._logf.close()
        return 2

    try:
        bk = subprocess.Popen(
            [str(ROOT / "python" / "python.exe"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", "8765", "--log-level", "error"],
            cwd=str(ROOT / "backend"), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(60):
            try:
                call("/health")
                break
            except Exception:               # noqa: BLE001
                time.sleep(1)
        cs.log(f"后端就绪 PID={bk.pid}")

        # 挑一条「不在一屏最前面、未读、未置顶」的来置顶 ——
        # 这样截图里能同时看到「置顶卡」和普通未读卡，且位移看得出来
        items = call("/notifications?page=1&page_size=20")["data"]["items"]
        cand = next((it for it in items[6:]
                     if not it.get("is_pinned") and not it.get("is_read")), items[-1])
        call(f"/notifications/{cand['id']}/pin", {"is_pinned": True}, "POST")
        cs.log(f"已临时置顶 id={cand['id']} 《{cand['title'][:28]}》")

        import tkinter as tk

        class FakeBackend:
            ready = True
            last_error = ""

            def start(self): pass
            def stop(self): pass
            def wait_ready(self, timeout=30): return True

        root = tk.Tk()
        app = m.QuanZhiApp(root, FakeBackend())
        for _ in range(40):
            root.update()
            time.sleep(0.1)

        # 窗口摆在左上角、尺寸固定 —— 截图区域就是这块，可预期。
        # 高度取 1180 是为了让「置顶 / 已读 / 未读」三张卡**都完整入镜**，
        # 否则第三张只露半截，颜色自校验没法覆盖到它。
        W, H = 1500, 1180
        root.geometry(f"{W}x{H}+0+0")
        root.deiconify()
        # ★ Tk 自己的置顶，一定生效（不像 SetForegroundWindow 会被前台锁忽略）
        root.attributes("-topmost", True)
        root.lift()
        for _ in range(30):
            root.update()
            time.sleep(0.1)

        # 先断言「界面里真的画出了三种状态」，再截图 —— 画不出来就不该产图
        views = app._views
        pinned_v = next((v for v in views if v["item"].get("is_pinned")), None)
        read_v = next((v for v in views if v["item"].get("is_read")), None)
        unread_v = next((v for v in views if not v["item"].get("is_read")
                         and not v["item"].get("is_pinned")), None)
        cs.log(f"视图数={len(views)} 置顶卡={bool(pinned_v)} "
               f"已读卡={bool(read_v)} 未读卡={bool(unread_v)}")
        if pinned_v:
            cs.log(f"  置顶卡：底色={pinned_v['card'].cget('bg')} "
                   f"胶囊={pinned_v['read_pill'].cget('text')!r} "
                   f"开关={pinned_v['pin_lbl'].cget('text')!r}")
        if read_v:
            cs.log(f"  已读卡：底色={read_v['card'].cget('bg')} "
                   f"状态条={read_v['accent'].cget('bg')} "
                   f"胶囊={read_v['read_pill'].cget('text')!r}")
        if unread_v:
            cs.log(f"  未读卡：底色={unread_v['card'].cget('bg')} "
                   f"状态条={unread_v['accent'].cget('bg')} "
                   f"胶囊={unread_v['read_pill'].cget('text')!r}")

        # 优先用 PrintWindow：让窗口**自己重绘**，完全不读屏幕。
        # 桌面抓图（GetDC(0)+BitBlt）在三种情况下会拍出废图：
        # 会话锁定 / 显示器休眠（整块黑）、窗口被别的程序盖住、窗口没到前台。
        # 实测：屏幕休眠时桌面抓图抽样 1200 个点**全是 (0,0,0)**，
        # 而 PrintWindow 照样能拿到正确的 #2F6BFF / #FFFAF0 / #F7F8FA。
        cap = cs.grab_window(cs.top_level_hwnd(root.winfo_id()))
        rect = wt.RECT(0, 0, W, H)
        if cap is not None:
            rows, w, h, rect = cap
            cs.log(f"PrintWindow 抓到窗口 {w}x{h}，原点=({rect.left},{rect.top})")
        else:
            cs.log("PrintWindow 不可用，退回桌面抓图")
            rows, w, h = cs.grab(rect)

        if cs.looks_solid(rows, w, h):
            # ★ 关键：**不能用坏图覆盖好图**。
            #   之前就是这么把一张正常的演示图覆盖成了 9.8KB 的纯黑图，
            #   而且照样"跑完输出完成"，非常隐蔽。
            bad_path = OUT.with_name(OUT.stem + ".solid.png")
            cs.write_png(bad_path, rows, w, h)
            cs.log("!! 截到的是纯色，窗口可能没渲染出来（或屏幕休眠/被遮挡）")
            cs.log(f"   为免用坏图覆盖好图，本次**不写** {OUT.name}，"
                   f"改存 {bad_path.name} 供排查；原文件保持不变。")
            return 3
        cs.write_png(OUT, rows, w, h)
        cs.log(f"已截图 {w}x{h}  {OUT.stat().st_size} 字节 -> {OUT.name}")

        # ★ 截图自校验：把**写出的文件**重新解码，逐张卡比对状态条颜色。
        #   防的就是「GDI 给的是 BGRA、PNG 要的是 RGBA」这类色序 bug：
        #   它会把蓝色 #2F6BFF 存成橙色 #FF6B2F，中性色却看不出来，极难发现。
        try:
            from png_pixel import Png
            img = Png(OUT)
            bad = 0
            for tag, v in (("置顶", pinned_v), ("已读", read_v), ("未读", unread_v)):
                if not v:
                    continue
                # 控件坐标是**屏幕绝对坐标**，要先减去截图原点才是图内坐标；
                # 状态条矩形再与图片范围求交取中点 —— 底部的卡只露一半时，
                # 直接取控件几何中心会落到图外，采到的是别的颜色。
                ax0 = v["accent"].winfo_rootx() - rect.left
                ay0 = v["accent"].winfo_rooty() - rect.top
                ix0 = max(ax0, 0)
                iy0 = max(ay0, 0)
                ix1 = min(ax0 + v["accent"].winfo_width(), img.w)
                iy1 = min(ay0 + v["accent"].winfo_height(), img.h)
                if ix1 - ix0 < 1 or iy1 - iy0 < 8:
                    cs.log(f"  取色 {tag}卡状态条: 可视高度不足，跳过")
                    continue
                ax = min(img.w - 1, max(0, ix0 + 1))   # +1 避开卡片 1px 描边
                ay = min(img.h - 1, max(0, (iy0 + iy1) // 2))
                got = img.hex(ax, ay).upper()
                want = str(v["accent"].cget("bg")).upper()
                hit = got == want
                bad += 0 if hit else 1
                cs.log(f"  取色 {tag}卡状态条: 文件={got} 控件={want} "
                       f"{'✓' if hit else '✗'}  (y={ay} 可见 {iy1 - iy0}px)")
            cs.log("  截图颜色自校验：" + ("通过" if bad == 0 else f"!! {bad} 处不符（色序 bug？）"))
        except Exception as e:                  # noqa: BLE001
            cs.log(f"  截图取色自校验跳过：{e}")

        cs.log("=== 完成 ===")
        return 0
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:               # noqa: BLE001
                pass
        # ★ 还原必须赶在后端关闭**之前** —— 还原是靠调接口做的，
        #   放到 bk.terminate() 之后就只能靠「端口还没释放」侥幸成功。
        try:
            n = restore(before)
            same = uf.verify(before, log=cs.log)
            cs.log(f"还原 {n} 处改动；与快照比对 = {'一致' if same else '仍有差异！'}")
        except Exception as e:              # noqa: BLE001
            cs.log(f"!! 还原失败：{e}")
        if bk is not None:
            bk.terminate()
            time.sleep(1)
            if bk.poll() is None:
                bk.kill()
            cs.log("后端已关闭")
        cs._logf.close()


if __name__ == "__main__":
    sys.exit(main())
