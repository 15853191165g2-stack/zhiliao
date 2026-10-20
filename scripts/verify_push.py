# -*- coding: utf-8 -*-
"""阶段 3 端到端验证：定时刷新 + 推送（本地通知 / Webhook）。

## 这个脚本证明什么

不是「代码看起来对」，而是**真的跑一遍**：

  1. **不会把历史通知一次性轰出去**
     用真实库（153 条）的**副本**启动后端，断言启动基线把 153 条全挡在窗口外，
     `/push/pending` 返回 0 条。这是「用户一开推送就被刷屏」那个坑的守门测试。
  2. **新通知真的进队列**
     往副本库里插一条新通知，断言它出现在 `/push/pending` 里，且带 3 行摘要。
  3. **桌面版真的弹出了气泡**
     在进程内起真窗口，等它自己轮询到新通知 —— 然后**读控件的真实文字**
     来断言气泡内容（不是看截图）。顺带断言气泡停在屏幕右下角。
  4. **Webhook 真的发出去了**
     本地起一个 HTTP 服务收 webhook，断言收到正确的 JSON（不是 mock）。
  5. **游标不会卡死**
     断言 `/push/pending` 返回 next_since_id，且历史条目不会被反复返回。

## 数据安全

全程使用 `backend/quanzhi.db` 的**副本**（临时目录），
所以既不会改用户的 153 条真实数据，也不会跑脏 `state/desktop.json`。
脚本结束时只删自己创建的临时目录。

## 端口

默认 8765。**如果用户此刻正开着知了**（它的后端就占着 8765），
用 `--port 8791` 换一个端口跑，不必去关用户的窗口：

    python\\python.exe scripts\\verify_push.py --port 8791

用法：
    python\\python.exe scripts\\verify_push.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# 绕开系统代理：本机回环请求不能走代理（否则 502，看起来像后端没起来）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

import user_flags as uf  # noqa: E402

import capture_shot as cs  # noqa: E402  复用抓图（导入时即设 DPI 感知）
from png_pixel import Png  # noqa: E402

#: 默认端口。用户可能正开着知了（它的后端占着 8765），
#: 那时用 `--port` 换一个跑，不要去关用户的窗口。
DEFAULT_PORT = 8765
PORT = DEFAULT_PORT
API = f"http://127.0.0.1:{PORT}/api"
OUT = ROOT / "logs"

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_passed = 0
_failed = 0
_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


def check(name: str, ok: bool, extra: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        log(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        _failed += 1
        log(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))
    return bool(ok)


def get(path: str, timeout: float = 15.0) -> dict:
    with _op.open(f"{API}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict | None = None, timeout: float = 60.0) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with _op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 本地 webhook 接收器：证明「真的发出去了」，而不是「我们调了 post()」
# --------------------------------------------------------------------------- #
class Receiver:
    def __init__(self) -> None:
        self.hits: list[dict] = []
        recv = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    parsed = None
                recv.hits.append({"path": self.path, "json": parsed,
                                  "ctype": self.headers.get("Content-Type", "")})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):  # noqa: A003
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
def main() -> int:
    global PORT, API
    # 允许换端口：用户可能正开着知了（其后端占着 8765），
    # 我们不该为了跑测试去关他的窗口。
    if "--port" in sys.argv:
        try:
            PORT = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            log("--port 后面要跟端口号，例如 --port 8791")
            return 2
        API = f"http://127.0.0.1:{PORT}/api"

    log("=" * 66)
    log("  知了 · 阶段 3「定时刷新 + 推送」端到端验证")
    log(f"  端口 {PORT}")
    log("=" * 66)

    if uf.port_in_use(PORT):
        log(f"!! {PORT} 已被占用 —— 那测的可能是旧后端，"
            f"先关掉它、或换端口（--port 8791）再跑。")
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="qz_push_verify_"))
    tmp_db = tmpdir / "quanzhi.db"
    src_db = ROOT / "backend" / "quanzhi.db"
    if src_db.exists():
        shutil.copy2(src_db, tmp_db)
        log(f"\n[准备] 已复制真实库做副本：{tmp_db.name}（原库 {src_db.stat().st_size} 字节）")
    else:
        log("\n[准备] 真实库不存在，用空库跑")
    # user_flags 读的是真实库路径，这里改成副本 —— 全程不碰真实数据
    uf.DB = tmp_db

    # ⚠️ 把副本退回「还没启用过推送」的起点：删掉基线标记，
    #    好让后端启动时**真的重建一次**基线 —— [3] 段测的就是「首次基线」。
    #
    #    不删会怎样：真实库里可能留着没被消费的待推送通知
    #    （本地通道只入队不消费，所以抓取后的新通知会一直待在那儿），
    #    于是 [3] 的「历史通知一条都没进推送队列」会失败。
    #    那不是代码回归，是断言的前提（干净起点）不成立 ——
    #    实测踩过一次：真库刚抓完数据，队列里有 1 条，脚本报 45/46。
    if tmp_db.exists():
        con = sqlite3.connect(tmp_db)
        try:
            cur = con.execute("delete from app_meta where key = 'push_baseline_at'")
            # 同时把已标记的清零，让「重建基线」这一步真的有事可做
            cur2 = con.execute("update notifications set notified_at = null")
            con.commit()
            log(f"       已重置副本基线标记（{cur.rowcount} 条 meta，"
                f"{cur2.rowcount} 条通知恢复为未推送）")
        finally:
            con.close()

    receiver = Receiver()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_db.as_posix()}"
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["PYTHONUNBUFFERED"] = "1"
    env["ENABLE_SCHEDULER"] = "false"        # 验证里手动触发，不靠 cron
    env["AI_API_KEY"] = ""                   # 不联网，走本地兜底摘要
    # ★ 本地 webhook 接收器地址。走真实 HTTP，不是 mock。
    env["PUSH_CHANNELS"] = "local,webhook"
    env["PUSH_WEBHOOK_URL"] = receiver.url
    env["PUSH_WEBHOOK_KIND"] = "generic"
    env["PUSH_RETRIES"] = "0"

    bk = None
    root = None
    app = None
    try:
        # ---------------- 1. 起后端（真实库副本）----------------
        log("\n[1] 启动后端（使用真实库副本）")
        bk = subprocess.Popen(
            [str(ROOT / "python" / "python.exe"), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
            cwd=str(ROOT / "backend"), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = False
        for _ in range(60):
            try:
                get("/health", timeout=3)
                ready = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        check("后端启动成功", ready)
        if not ready:
            return 1

        health = get("/health")["data"]
        total = health["counts"]["notifications"]
        log(f"       库内通知 {total} 条，来源 {health['counts']['sources']} 个")

        # ---------------- 2. 通道与配置 ----------------
        log("\n[2] 推送通道与配置")
        chans = get("/push/channels")["data"]
        by = {c["kind"]: c for c in chans}
        check("三个通道都已注册", {"local", "webhook", "fcm"} <= set(by),
              f"实际 {sorted(by)}")
        check("本地通道就绪且不消费队列",
              by["local"]["ready"] and by["local"]["consumes_queue"] is False)
        check("Webhook 通道就绪且会消费队列",
              by["webhook"]["ready"] and by["webhook"]["consumes_queue"] is True)
        check("FCM 被标注为未验证", "未验证" in by["fcm"]["note"])

        cfg = get("/push/config")["data"]
        check("当前通道 = local,webhook", cfg["channels"] == ["local", "webhook"],
              str(cfg["channels"]))
        check("推送时间窗为 48 小时", cfg["lookback_hours"] == 48)
        # ⚠️ 断言要落在「密钥/完整地址有没有泄露」上。
        #    一开始我写的是 `"hook" not in 配置JSON`，结果永远不成立 ——
        #    因为 key 名字就叫 "webhook"，本身含 "hook"。断言写松了会假失败，
        #    写偏了会假通过，两种都得防。
        cfg_json = json.dumps(cfg, ensure_ascii=False)
        check("webhook 完整地址未回显",
              receiver.url not in cfg_json, str(cfg["webhook"]["target"]))
        check("webhook 端口未回显（密钥藏得住）",
              str(receiver.port) not in cfg_json, str(cfg["webhook"]["target"]))
        check("webhook 配置状态正确", cfg["webhook"]["configured"] is True)

        # ---------------- 3. 首次基线：不许轰历史 ----------------
        log("\n[3] 首次基线 —— 不许把历史通知一次性推出去")
        check("基线已建立", cfg["baseline_count"] >= 0,
              f"baseline_count={cfg['baseline_count']}")
        pend = get("/push/pending?limit=50")["data"]
        check("历史通知一条都没进推送队列", len(pend["items"]) == 0,
              f"实际返回 {len(pend['items'])} 条")
        check("max_id 可用于初始化客户端游标", pend["max_id"] == total,
              f"max_id={pend['max_id']} 通知总数={total}")
        check("启动时没有产生任何 webhook 请求", len(receiver.hits) == 0,
              f"实际 {len(receiver.hits)} 次")

        base_cursor = pend["max_id"]

        # ---------------- 4. 插一条新通知，看它进队列 ----------------
        log("\n[4] 新增一条通知，确认它进入推送队列")
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        con = sqlite3.connect(tmp_db)
        cur = con.execute(
            "INSERT INTO sources (name, school, adapter, url, category_hint, enabled,"
            " config, last_status, item_count, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("推送验证源", "验证大学", "html_list", "http://example.invalid/v",
             "通知公告", 1, "{}", "ok", 0, now_utc.strftime("%Y-%m-%d %H:%M:%S")))
        sid = cur.lastrowid
        cur = con.execute(
            "INSERT INTO notifications (source_id, dedup_key, title, url, published_at,"
            " content_hash, excerpt, summary_lines, ai_status, importance, category,"
            " is_read, is_starred, is_pinned, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
            (sid, "verify-push-1", "【验证】期末考试安排已发布",
             "http://example.invalid/v/1",
             now_utc.strftime("%Y-%m-%d %H:%M:%S"),
             "verifyhash1",                       # content_hash 是 NOT NULL
             "教务处发布了期末考试安排。",
             json.dumps(["期末考试安排已发布，共 12 门。",
                         "考试时间 2026-11-02 至 11-13。",
                         "请在教务系统核对并确认考场。"], ensure_ascii=False),
             "done", "important", "考试",
             now_utc.strftime("%Y-%m-%d %H:%M:%S"),
             now_utc.strftime("%Y-%m-%d %H:%M:%S")))
        nid = cur.lastrowid
        con.commit()
        con.close()
        log(f"       已插入 notification id={nid}（3 行摘要，important）")

        pend2 = get(f"/push/pending?since_id={base_cursor}&limit=10")["data"]
        got_ids = [it["id"] for it in pend2["items"]]
        check("新通知出现在待推送队列", nid in got_ids, f"队列={got_ids}")
        item = next((it for it in pend2["items"] if it["id"] == nid), {})
        check("队列里带 3 行摘要", len(item.get("lines") or []) == 3,
              str(item.get("lines")))
        check("队列里带中文重要度", item.get("importance_label") == "重要",
              str(item.get("importance_label")))
        check("游标被推进", (pend2.get("next_since_id") or 0) >= nid,
              f"next_since_id={pend2.get('next_since_id')}")

        # 拉取是只读的：再拉一次还在（证明「拉到 ≠ 已送达」）
        pend3 = get(f"/push/pending?since_id={base_cursor}&limit=10")["data"]
        check("拉取只读：再拉一次消息还在",
              nid in [it["id"] for it in pend3["items"]])

        # ---------------- 5. 手动推一次 → webhook 真的收到 ----------------
        log("\n[5] 手动触发推送，验证 webhook 真的发出去了")
        run = post("/push/run", {"limit": 20})["data"]
        check("推送执行完成", run.get("duration_ms") is not None, json.dumps(
            {k: run.get(k) for k in ("checked", "sent", "queued", "failed")},
            ensure_ascii=False))
        check("至少有一条进了本地队列", run.get("queued", 0) >= 1 or run.get("sent", 0) >= 1)
        time.sleep(0.5)
        check("webhook 收到了请求", len(receiver.hits) >= 1,
              f"实际 {len(receiver.hits)} 次")
        if receiver.hits:
            hit = receiver.hits[-1]
            payload = hit["json"] or {}
            check("webhook 路径正确", hit["path"] == "/hook", hit["path"])
            check("webhook content-type 是 JSON",
                  "application/json" in hit["ctype"], hit["ctype"])
            check("载荷是约定的 generic 格式",
                  payload.get("event") == "notification.new",
                  str(payload.get("event")))
            note = (payload.get("notification") or {})
            check("载荷里带通知正文与 3 行摘要",
                  len(note.get("lines") or []) == 3 and bool(note.get("title")),
                  f"title={note.get('title')} lines={len(note.get('lines') or [])}")

        logs = get("/push/logs?limit=20")["data"]
        check("推送留下了审计日志", len(logs) >= 1, f"{len(logs)} 条")
        check("审计日志记录了通道名",
              all(x.get("channel") for x in logs[:3]),
              str([x.get("channel") for x in logs[:3]]))

        # ---------------- 6. 桌面版真的弹出气泡 ----------------
        log("\n[6] 桌面版本地通知（真窗口 + 读控件文字，不看截图）")
        spec = importlib.util.spec_from_file_location(
            "qz_push_probe", ROOT / "知了桌面版.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["qz_push_probe"] = m
        spec.loader.exec_module(m)
        # 别把验证过程写进用户真实的 state/desktop.json
        m.DESKTOP_STATE = tmpdir / "desktop.json"
        m.PORT = PORT
        m.HOST = "127.0.0.1"
        m.BASE = f"http://127.0.0.1:{PORT}"
        m.API = f"{m.BASE}/api"
        state = m.load_desktop_state()
        state["notify_interval"] = 5         # 快一点，别让验证等 30 秒
        state["toast_seconds"] = 120         # 气泡久留，方便读控件 / 拍照
        m.DESKTOP_STATE.write_text(json.dumps(state, ensure_ascii=False),
                                   encoding="utf-8")

        import tkinter as tk
        root = tk.Tk()
        root.geometry("1300x820+0+0")        # 真实（未 withdraw）窗口
        backend = m.Backend()
        backend.ready = True                 # 后端已由本脚本拉起
        app = m.QuanZhiApp(root, backend)

        # ⚠️ 必须用真正的 mainloop，**不能**用 `while: root.update()` 轮询。
        #    桌面版的后台线程会调 `root.after(0, ...)` 把结果丢回主线程，
        #    而 tkinter 要求主线程处于 mainloop 中，否则抛
        #    `RuntimeError: main thread is not in main loop` ——
        #    表现为「游标永远初始化不了、气泡一个都不弹」，
        #    而产品本身没问题（生产环境走的就是 mainloop）。
        R: dict = {}
        phase = {"name": "cursor", "t0": time.time()}

        def insert_new_notification() -> int:
            """再插一条「刚发布」的通知，用来触发气泡。"""
            stamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime(
                "%Y-%m-%d %H:%M:%S")
            c = sqlite3.connect(tmp_db)
            nid = c.execute(
                "INSERT INTO notifications (source_id, dedup_key, title, url,"
                " published_at, content_hash, excerpt, summary_lines, ai_status,"
                " importance, category, is_read, is_starred, is_pinned,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
                (sid, f"verify-toast-{int(time.time())}",
                 "【验证】图书馆寒假开放时间调整",
                 "http://example.invalid/v/2", stamp, "verifyhash2",
                 "图书馆发布了寒假开放安排。",
                 json.dumps(["寒假期间图书馆开放至 21:00。",
                             "1 月 18 日起闭馆三天。",
                             "自习区需提前预约座位。"], ensure_ascii=False),
                 "done", "normal", "后勤", stamp, stamp)).lastrowid
            c.commit()
            c.close()
            return nid

        def tick() -> None:
            now = time.time()
            if phase["name"] == "cursor":
                if app.state.get("push_cursor_ready"):
                    R["cursor"] = int(app.state.get("push_cursor") or 0)
                    R["nid2"] = insert_new_notification()
                    log(f"       游标已初始化 = {R['cursor']}；"
                        f"新插一条 id={R['nid2']} 等它自己轮询到")
                    phase.update(name="toast", t0=now)
                elif now - phase["t0"] > 30:
                    R["cursor_timeout"] = True
                    phase.update(name="done", t0=now)
                    root.quit()
                    return
            elif phase["name"] == "toast":
                if app.toasts.alive:
                    root.after(400, inspect_toast)
                    phase["name"] = "done"
                    return
                if now - phase["t0"] > 60:
                    R["toast_timeout"] = True
                    phase["name"] = "done"
                    root.quit()
                    return
            root.after(200, tick)

        def inspect_toast() -> None:
            """气泡出来之后，在事件循环里读它的真实控件。"""
            try:
                alive = app.toasts.alive[0]
                win = alive["win"]
                texts: list[str] = []

                def walk(w):
                    for ch in w.winfo_children():
                        try:
                            t = ch.cget("text")
                            if t:
                                texts.append(str(t))
                        except Exception:  # noqa: BLE001
                            pass
                        walk(ch)

                walk(win)
                win.update_idletasks()
                R["texts"] = texts
                R["win"] = {
                    "x": win.winfo_x(), "y": win.winfo_y(),
                    "w": win.winfo_width(), "h": win.winfo_height(),
                    "screen_w": root.winfo_screenwidth(),
                    "screen_h": root.winfo_screenheight(),
                    "override": bool(win.overrideredirect()),
                }
                # 截图存证（PrintWindow，锁屏也能拍）
                hwnd = cs.top_level_hwnd(win.winfo_id())
                grabbed = cs.grab_window(hwnd)
                if grabbed:
                    rows, w, h, _rect = grabbed
                    out = OUT / "shot_toast.png"
                    cs.write_png(out, rows, w, h)
                    img = Png(out)
                    uniq = {(img.rgb(x, y)) for y in range(0, img.h, 5)
                            for x in range(0, img.w, 5)}
                    R["shot"] = (w, h, len(uniq), out.name)
                # 点一下气泡 → 应该打开详情并让气泡消失
                # ⚠️ 不能拿「Toplevel 数量变多」当断言：_open 会**同时**
                #    关掉气泡、打开详情，总数可能恰好不变（这里就是 1 -> 1）。
                #    改成按窗口标题找详情窗口。
                app.toasts._open({"id": R.get("nid2"),
                                  "title": "图书馆寒假开放时间调整",
                                  "source_name": "验证大学"})
                root.update_idletasks()
                R["after_click_alive"] = len(app.toasts.alive)
                toplevels = [w for w in root.winfo_children()
                             if isinstance(w, tk.Toplevel)]
                R["top_titles"] = [str(w.title()) for w in toplevels]
            except Exception as e:  # noqa: BLE001
                R["inspect_error"] = f"{type(e).__name__}: {e}"
            finally:
                root.quit()

        root.after(300, tick)
        root.mainloop()

        if R.get("inspect_error"):
            log(f"       (读控件时出错：{R['inspect_error']})")
        check("桌面版游标已初始化", R.get("cursor") is not None,
              f"cursor={R.get('cursor')} 超时={R.get('cursor_timeout', False)}")
        check("游标落在当前最大 id 上（不会重弹历史）",
              int(R.get("cursor") or 0) >= base_cursor,
              f"cursor={R.get('cursor')} base={base_cursor}")
        check("桌面版弹出了通知气泡", R.get("texts") is not None,
              f"超时={R.get('toast_timeout', False)}")

        texts = R.get("texts") or []
        joined = " | ".join(texts)
        check("气泡里有通知标题",
              any("图书馆寒假开放时间" in t for t in texts), joined[:120])
        check("气泡里有 3 行摘要",
              sum(1 for t in texts if t.startswith("·")) >= 3,
              f"带 · 的行={sum(1 for t in texts if t.startswith('·'))}")
        check("气泡里标了来源与重要性",
              any("知了" in t for t in texts) and any("一般" in t for t in texts))

        g = R.get("win") or {}
        if g:
            check("气泡贴在屏幕右侧", g["x"] + g["w"] >= g["screen_w"] - 40,
                  f"右边缘={g['x'] + g['w']} 屏宽={g['screen_w']}")
            check("气泡在屏幕下半部", g["y"] > g["screen_h"] * 0.5,
                  f"y={g['y']} 屏高={g['screen_h']}")
            check("气泡无边框（overrideredirect）", g["override"] is True)
        if R.get("shot"):
            w, h, uniq, name = R["shot"]
            check("气泡截图有内容（非纯色）", uniq > 8,
                  f"{w}x{h} 取色 {uniq} 种 -> {name}")
        else:
            check("气泡截图成功", False, "grab_window 返回 None")
        check("点气泡会关掉它本身", R.get("after_click_alive") == 0,
              f"剩余 {R.get('after_click_alive')}")
        check("点气泡会打开详情窗口",
              any("图书馆寒假开放时间调整" in t for t in (R.get("top_titles") or [])),
              str(R.get("top_titles")))

        # ---------------- 7. 重复推送去重 ----------------
        log("\n[7] 重复推送不会重发")
        post("/push/run", {"limit": 20})          # 先把队列排空
        time.sleep(0.5)
        hits_before = len(receiver.hits)
        post("/push/run", {"limit": 20})          # 再推一次
        time.sleep(0.5)
        check("队列空时再推不会重复外发", len(receiver.hits) == hits_before,
              f"{hits_before} -> {len(receiver.hits)}")

        # ---------------- 8. 推送自检接口 ----------------
        log("\n[8] /push/test 通道自检")
        hits_before = len(receiver.hits)
        t = post("/push/test", {"text": "知了推送自检"})["data"]
        by_ch = {r["channel"]: r for r in t["results"]}
        check("自检整体成功", t["ok"] is True, json.dumps(t, ensure_ascii=False)[:160])
        check("本地通道自检通过", by_ch.get("local", {}).get("ok") is True)
        check("webhook 自检通过", by_ch.get("webhook", {}).get("ok") is True)
        check("自检真的发出了 webhook", len(receiver.hits) == hits_before + 1,
              f"{hits_before} -> {len(receiver.hits)}")

        # ---------------- 9. 免打扰配置 ----------------
        log("\n[9] 免打扰时段解析（配置层）")
        rng_ok = True
        try:
            sys.path.insert(0, str(ROOT / "backend"))
            os.environ["PUSH_QUIET_HOURS"] = "23:00-07:00"
            from app.core.config import Settings
            s = Settings()
            rng_ok = s.push_quiet_range() == (23 * 60, 7 * 60)
            s2 = Settings(push_quiet_hours="aa-bb")
            rng_ok = rng_ok and s2.push_quiet_range() is None
        except Exception as e:  # noqa: BLE001
            rng_ok = False
            log(f"       (配置检查异常：{e})")
        check("跨零点时段解析正确、非法格式被忽略", rng_ok)

    finally:
        # ---------------- 收尾 ----------------
        log("\n[收尾]")
        try:
            if app is not None:
                app._stop_poll()
                app.toasts.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
        if bk is not None:
            bk.terminate()
            try:
                bk.wait(timeout=10)
            except Exception:  # noqa: BLE001
                bk.kill()
        receiver.stop()
        # 只删自己建的临时目录。真实库、真实 state 全程没被碰过。
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            log(f"       已清理临时目录 {tmpdir.name}")
        except Exception as e:  # noqa: BLE001
            log(f"       临时目录清理失败：{e}")

    log("\n" + "=" * 66)
    log(f"  结果：{_passed} 通过 / {_failed} 失败 / 共 {_passed + _failed} 项")
    log("=" * 66)

    (OUT / "push_verify.txt").write_text("\n".join(_lines), encoding="utf-8")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
