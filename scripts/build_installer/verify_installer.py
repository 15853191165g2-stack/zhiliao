"""验证「一个装好的知了目录」能不能真的跑起来 —— 装完必须跑这个，别只看安装器报成功。

为什么不能只看安装器 / 只看进程起来了：
    · 装完了文件少一个（漏装 site-packages）→ 后端秒退，安装器依然报 success
    · 后端起来了但连的是**别人那份**旧数据/旧代码 → 全绿也没意义（docs/02 #49）
所以这里必须：① 端口独占（起不来就算失败）② 断言拿到的是本目录这份库
③ 状态目录隔离，不动用户真实数据 ④ 跑完比对 db 的 mtime 证明没写脏。

用法：
    python verify_installer.py "C:/Users/x/AppData/Local/Programs/ZhiLiao" [--port 8791] [--gui]
退出码：0 全过 / 1 有断言没过
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
_proxy_off = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, timeout: float = 5.0) -> dict:
    """直连，不吃系统代理（本机有代理时 127.0.0.1 会被拦成 502）。"""
    with _proxy_off.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def wait_port_released(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(0.3)
    return False


def windows_of_pid(pid: int) -> list[str]:
    """按 PID 收集可见顶层窗口标题 —— 只认自己的进程，避免匹配到别人开的知了。

    先按 PID 过滤再用标题判断，是两件事：PID 过滤保证「测的是本次启动的进程」，
    标题只用来确认「主窗口出来了」而不是停在 Tk 那个隐藏的默认窗口（标题叫 tk）。
    """
    found: list[str] = []
    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        wpid = ctypes.c_ulong()
        GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and IsWindowVisible(hwnd):
            n = GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                found.append(buf.value)
        return True

    EnumWindows(CB(cb), 0)
    return found


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.bad = 0

    def __call__(self, cond: bool, label: str, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  [PASS] {label}" + (f"  ({extra})" if extra else ""))
        else:
            self.bad += 1
            print(f"  [FAIL] {label}" + (f"  ({extra})" if extra else ""))
        return bool(cond)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print("用法: python verify_installer.py <安装目录> [--port 8791] [--gui]")
        return 2
    root = Path(args[0]).resolve()

    port = 8791
    for f in flags:
        if f.startswith("--port="):
            port = int(f.split("=", 1)[1])
    port = int(os.environ.get("VERIFY_PORT") or port)
    gui = "--gui" in flags or os.environ.get("VERIFY_GUI") == "1"

    ck = Checker()
    print(f"\n验证目录：{root}\n端口：{port}\n" + "-" * 60)

    # ---- 1. 文件结构 ----
    print("\n[1/6] 文件结构")
    py = root / "python" / "python.exe"
    pyw = root / "python" / "pythonw.exe"
    be = root / "backend"
    ck(py.exists(), "自带解释器 python/python.exe")
    ck(pyw.exists(), "无窗口解释器 pythonw.exe")
    ck((be / "app" / "main.py").exists(), "后端入口 backend/app/main.py")
    ck((root / "quanzhi_desktop.py").exists(), "启动入口 quanzhi_desktop.py")
    ck((be / "quanzhi.db").exists(), "数据库 backend/quanzhi.db")
    for pkg in ("fastapi", "uvicorn", "sqlalchemy", "httpx", "bs4", "lxml", "apscheduler"):
        ck((root / "python" / "Lib" / "site-packages" / pkg).is_dir(), f"依赖包 {pkg}")

    # ---- 2. 不吃系统中的旧实例 ----
    print("\n[2/6] 端口独占")
    if not ck(port_free(port), f"{port} 端口空闲（保证测的是本次启动的后端）"):
        print("      端口被占用，换一个：--port=87xx")
        return 1

    db = be / "quanzhi.db"
    db_mtime_before = db.stat().st_mtime

    state_dir = Path(os.environ.get("TEMP", ".")) / f"zhiliao_verify_state_{port}"
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True)
    # 预置「关掉提醒」：否则 App 当首次运行会拉通知 + load_feed(reset=True)，等到 0 张卡片
    (state_dir / "desktop.json").write_text(
        json.dumps({"notify_enabled": False}, ensure_ascii=False), encoding="utf-8"
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(be)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    env["no_proxy"] = "localhost,127.0.0.1,::1"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)

    log = Path(os.environ.get("TEMP", ".")) / f"zhiliao_verify_{port}.log"
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=str(be), env=env, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # ---- 3. 后端就绪 ----
    print("\n[3/6] 后端就绪")
    base = f"http://{HOST}:{port}/api"
    health: dict = {}
    deadline = time.time() + 70
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            d = http_json(f"{base}/health", timeout=3)
            if d.get("code") == 0:
                health = d.get("data") or {}
                break
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            pass
        time.sleep(0.5)

    if not ck(bool(health), "/api/health 返回 code=0"):
        print(f"\n  后端日志尾部（{log}）：")
        try:
            fh.flush()
            print("\n".join(log.read_text(encoding="utf-8").splitlines()[-25:]))
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)
        return 1

    ai = health.get("ai") or {}
    print(f"      AI 启用 {ai.get('enabled')} / 模型 {ai.get('model') or '(未配置)'} / 来源 {ai.get('source')}")
    print(f"      库内统计 {health.get('counts')}")
    ck((health.get("database") or {}).get("ok") is True, "数据库连接正常")

    # ---- 4. 数据是否本目录这份 ----
    print("\n[4/6] 数据与接口")
    try:
        feed = http_json(f"{base}/notifications?page=1&page_size=5")["data"]
        total = feed.get("total", 0)
        items = feed.get("items") or []
        ck(total > 0, "通知总数 > 0", f"total={total}")
        ck(len(items) > 0, "首页能取到条目", f"{len(items)} 条")
        if items:
            lines = items[0].get("summary_lines") or []
            # 铁律：每条通知必须 3 行摘要。这里验的是「装出来的包」守不守规矩
            ck(len(lines) == 3, "首页条目都是 3 行摘要",
               lines[0][:26] if lines else "(空)")
            bad = [n for n in items if len(n.get("summary_lines") or []) != 3]
            ck(not bad, "5 条里没有 3 行违约", f"违约 {len(bad)} 条")
    except Exception as e:  # noqa: BLE001
        ck(False, "信息流接口", repr(e))

    try:
        srcs = http_json(f"{base}/sources")["data"]   # 这个接口直接返回列表，不是分页结构
        ck(len(srcs) > 0, "来源配置已装载", f"{len(srcs)} 个来源")
        ck(sum(1 for s in srcs if s.get("enabled")) > 0, "有启用的来源",
           f"{sum(1 for s in srcs if s.get('enabled'))} 个启用")
    except Exception as e:  # noqa: BLE001
        ck(False, "来源接口", repr(e))

    try:
        prof = http_json(f"{base}/llm/profiles")["data"]
        items = prof.get("items", prof) if isinstance(prof, dict) else prof
        ck(len(items) == 0, "安装包内不含模型配置（密钥未打包）", f"profiles={len(items)}")
    except Exception as e:  # noqa: BLE001
        ck(False, "模型配置接口", repr(e))

    # ---- 5. 桌面壳能开窗（可选） ----
    gui_proc = None
    if gui:
        print("\n[5/6] 桌面壳启动")
        genv = env.copy()
        genv["QUANZHI_PORT"] = str(port)
        genv["QUANZHI_STATE_DIR"] = str(state_dir)
        gui_proc = subprocess.Popen(
            [str(pyw), str(root / "quanzhi_desktop.py")],
            cwd=str(root), env=genv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        titles: list[str] = []
        deadline = time.time() + 45
        while time.time() < deadline:
            if gui_proc.poll() is not None:
                break
            titles = windows_of_pid(gui_proc.pid)
            if any("知了" in t for t in titles):
                break
            time.sleep(0.5)
        main_title = next((t for t in titles if "知了" in t), None)
        ck(main_title is not None, "主窗口出现（按 PID 认，标题含「知了」）",
           main_title or (f"只看到 {titles}" if titles else "超时未出现"))
        if gui_proc.poll() is None:
            gui_proc.terminate()
            try:
                gui_proc.wait(10)
            except subprocess.TimeoutExpired:
                gui_proc.kill()
    else:
        print("\n[5/6] 桌面壳启动 —— 跳过（加 --gui 才测；需要图形会话）")

    # ---- 6. 收尾：不留孤儿、不写脏数据 ----
    print("\n[6/6] 收尾")
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    fh.close()
    ck(wait_port_released(port), f"{port} 端口已释放（没有孤儿后端）")

    db_mtime_after = db.stat().st_mtime
    ck(db_mtime_before == db_mtime_after, "验证过程没写库（db mtime 未变）",
       "未改动" if db_mtime_before == db_mtime_after else "被改动了")
    shutil.rmtree(state_dir, ignore_errors=True)

    print("-" * 60)
    print(f"结果：{ck.ok} 通过 / {ck.bad} 失败")
    return 0 if ck.bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
