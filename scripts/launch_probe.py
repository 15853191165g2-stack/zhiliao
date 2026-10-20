# -*- coding: utf-8 -*-
"""实测「双击 知了.bat」的启动链路：
启动 pythonw.exe + ASCII 入口，确认 GUI 存活且能自行拉起后端。

用法：
    python\\python.exe scripts\\launch_probe.py [--port 8765]

── 为什么要有 --port，以及为什么窗口要按 PID 找 ──

以前的版本有两处会给出**假 PASS**（本机开着另一个「知了」时必现）：
  1. 探活固定打 8765。8765 上如果已经跑着别人的后端，`/health` 有响应，
     于是即使新实例的后端因为端口占用而**绑定失败**，也会被判成「后端就绪」。
  2. 窗口是「枚举所有可见窗口，标题含『知了』就算」。
     用户自己开着的那个窗口同样满足条件 —— 测的到底是不是刚启动的进程都不知道。
   另外 pythonw 被 terminate() 之后，它拉起来的 uvicorn 是**子进程**，
   Windows 不会连带杀掉，就留下一个「残留的旧后端」占着端口。

现在：端口可指定且**启动前必须为空**，窗口只认新进程自己的 PID，
收尾时把监听该端口的后端一并清掉。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import user_flags as uf  # noqa: E402

entry = ROOT / "quanzhi_desktop.py"
exe = ROOT / "python" / "pythonw.exe"

_ap = argparse.ArgumentParser()
_ap.add_argument("--port", type=int, default=8765)
PORT = _ap.parse_args().port

print("root      :", ROOT)
print("entry     :", entry, "exists =", entry.exists())
print("pythonw   :", exe, "exists =", exe.exists())
print("port      :", PORT)
if not (entry.exists() and exe.exists()):
    print("RESULT: FAIL (缺少入口或解释器)")
    sys.exit(1)

if uf.guard_port(PORT):
    print("RESULT: FAIL (端口被占用 —— 先腾空再跑，否则测到的不是本次启动的进程)")
    sys.exit(2)

env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    env.pop(k, None)
env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
env["QUANZHI_PORT"] = str(PORT)

# ★ 状态目录也隔离掉：否则这次探测会往用户真实使用中的
#   state\desktop.json 里写游标和「上次检查时间」。
#   放在 logs\_diag\ 下留档，不删（用户不接受批量删文件）。
STATE_ISOLATED = ROOT / "logs" / "_diag" / "launch_probe_state"
STATE_ISOLATED.mkdir(parents=True, exist_ok=True)
env["QUANZHI_STATE_DIR"] = str(STATE_ISOLATED)
print("state dir :", STATE_ISOLATED)


def listening_pids(port: int) -> list[int]:
    """谁在监听这个端口（用 netstat，便携运行时里没有 psutil）。

    ⚠️ 不要用 `text=True`：中文 Windows 的 netstat 输出是 **GBK**，
    Python 按 UTF-8 去解会抛 UnicodeDecodeError（而且是在读取线程里抛，
    外层拿到的是 None，报出来的是莫名其妙的 AttributeError）。
    这里先拿字节，再按 GBK 容错解码 —— 我们只取端口/PID 这两列，纯 ASCII。
    """
    pids: set[int] = set()
    try:
        proc = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                              capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return []
    out = (proc.stdout or b"").decode("gbk", errors="replace")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(pids)


p = subprocess.Popen(
    [str(exe), str(entry)],
    cwd=str(ROOT), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print("launched pid:", p.pid)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ready = False
for i in range(40):
    if p.poll() is not None:
        print(f"!! GUI 进程在第 {i} 秒就退出了，exit={p.returncode}")
        break
    time.sleep(1)
    try:
        with opener.open(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as r:
            d = json.load(r)
        print(f"  t={i + 1}s  后端已就绪: {d['data']['counts']}")
        ready = True
        break
    except Exception:  # noqa: BLE001
        pass

alive = p.poll() is None
print("GUI 进程存活:", alive)

# 早点退会误判，所以先画一个 Tk 窗口预热（纯旁观，不影响被测进程）。
try:
    import tkinter
    _probe = tkinter.Tk()
    _probe.withdraw()   # 不显示，只为把 Tk 运行时拉起来
    _probe.update()
except Exception:  # noqa: BLE001
    _probe = None
time.sleep(4)

# ★ 按 PID 找窗口：标题含「知了」还不够，必须是**本次启动的那个进程**的窗口。
title_ok = False
try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: list[str] = []
    target = p.pid

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != target:
            return True
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if "知了" in buf.value:
                    found.append(buf.value)
        return True

    user32.EnumWindows(_cb, 0)
    if found:
        print("窗口标题    :", found[0], f"（属于 pid={target}）")
        title_ok = True
    else:
        print(f"!! pid={target} 没有标题含「知了」的可见窗口")
except Exception as e:  # noqa: BLE001
    print("窗口枚举跳过:", e)

# ---- 收尾：GUI + 它拉起来的后端（子进程，Windows 不会连带杀）----
p.terminate()
time.sleep(1)
if p.poll() is None:
    p.kill()
leftover = listening_pids(PORT)
if leftover:
    print("清理孤儿后端 pid:", leftover)
    for pid in leftover:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=20)
time.sleep(0.5)
still = listening_pids(PORT)
print("端口是否已释放:", not still)
if _probe is not None:
    try:
        _probe.destroy()
    except Exception:  # noqa: BLE001
        pass

ok = ready and alive and title_ok and not still
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
