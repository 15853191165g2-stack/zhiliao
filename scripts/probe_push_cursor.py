"""复现 / 验证一个缺口：全新 state 的机器首次启动，会不会跳过队列里已有的通知。

## 缺口是什么

`QuanZhiApp._init_push_cursor()` 首次运行会把游标对齐到 `/push/pending` 的 `max_id`，
之后只拉 `id > max_id`。于是「队列里已经存在、且 id <= max_id」的通知
**永远不会被拉取，也不弹气泡，也不报错**。

触发场景都是很日常的操作：
- 把便携包拷到另一台电脑（`state/` 是新的）
- 手动删掉 `state/` 目录想「重置一下」

## 为什么这个脚本是必要的

「队列里有东西」和「用户看得到」是两件事。
只查 `/push/pending` 返回非空，证明不了客户端会把它弹出来 ——
正是这种「服务端一切都对」的错觉让这个缺口一直隐形。

所以本脚本驱动**真实的 QuanZhiApp**（进程内、真 mainloop），
在**全新 state** 下等待气泡，然后断言队列里那几条真的出现了。

用法（需要后端已在跑）：
    python\\python.exe scripts\\probe_push_cursor.py --port 8765

安全约定：只读真库、state 隔离到 logs/_diag、不碰用户数据。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import tkinter as tk
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / "logs" / "_diag"

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
    ap.add_argument("--wait", type=int, default=30, help="等气泡出现的最长秒数")
    args = ap.parse_args()

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---- 先问后端：队列里到底有什么 ---- #
    try:
        raw = op.open(f"http://127.0.0.1:{args.port}/api/push/pending"
                      "?since_id=0&limit=50", timeout=10).read()
        pend = json.loads(raw)["data"]
    except Exception as e:  # noqa: BLE001
        print(f"!! 连不上后端 127.0.0.1:{args.port}：{type(e).__name__}: {e}")
        return 2

    items = pend.get("items") or []
    print("=" * 72)
    print("  全新 state 首次启动 · 会不会跳过队列里已有的通知")
    print("=" * 72)
    print(f"队列（since_id=0，升序）: total={pend.get('total')} max_id={pend.get('max_id')}")
    for it in items:
        print(f"   #{it['id']}  {str(it.get('title'))[:44]}")
    check("队列里有待推送通知", bool(items), f"实际 {len(items)} 条")
    if not items:
        print("\n队列是空的 —— 这个场景测不出缺口。先跑一次抓取，或换个有积压的库。")
        return 2

    oldest_id = int(items[0]["id"])
    newest_id = int(items[-1]["id"])
    print(f"最老 id={oldest_id}  最新 id={newest_id}  "
          f"（原始逻辑会把游标设成 max_id={pend.get('max_id')}，"
          f"于是 id<={pend.get('max_id')} 的全部被跳过）")

    # ---- 全新 state：连 desktop.json 都不存在 ---- #
    stamp = time.strftime("%H%M%S")
    state_dir = DIAG / f"cursor_fresh_{stamp}"
    state_dir.mkdir(parents=True, exist_ok=True)
    assert not (state_dir / "desktop.json").exists(), "state 必须是全新的"

    os.environ["QUANZHI_STATE_DIR"] = str(state_dir)
    os.environ["QUANZHI_PORT"] = str(args.port)

    spec = importlib.util.spec_from_file_location("qz_cursor", ROOT / "知了桌面版.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["qz_cursor"] = m
    spec.loader.exec_module(m)

    print("-" * 72)
    print(f"全新 state：{state_dir.name}（desktop.json 不存在）")
    check("state 目录里没有 desktop.json",
          not (state_dir / "desktop.json").exists())

    # ---- 驱动真实界面 ---- #
    root = tk.Tk()
    root.withdraw()
    backend = m.Backend()
    backend.ready = True              # 复用已在跑的后端
    app = m.QuanZhiApp(root, backend)

    R: dict = {"toasts": [], "cursor": None, "ready": None}

    def texts_of(win) -> list[str]:
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

    def collect_toasts() -> None:
        seen: list[str] = []
        for a in app.toasts.alive:
            if app.toasts._exists(a["win"]):
                seen += texts_of(a["win"])
        R["toasts"] = seen

    def tick(n: int = 0) -> None:
        collect_toasts()
        R["cursor"] = app.state.get("push_cursor")
        R["ready"] = app.state.get("push_cursor_ready")
        titles = [str(it.get("title") or "")[:14] for it in items]
        got = sum(1 for t in titles if t and any(t in x for x in R["toasts"]))
        if got >= len(titles) or n * 500 >= args.wait * 1000:
            root.after(300, root.destroy)
            return
        root.after(500, lambda: tick(n + 1))

    root.after(1500, lambda: tick(0))
    root.mainloop()

    # ---- 判定 ---- #
    print("-" * 72)
    print(f"启动后 state 里的游标 = {R['cursor']}  ready={R['ready']}")
    print(f"屏幕上出现过的气泡文字：")
    for t in R["toasts"]:
        print(f"   · {t[:70]}")
    if not R["toasts"]:
        print("   （一个气泡都没有）")

    print("-" * 72)
    delivered = []
    for it in items:
        title = str(it.get("title") or "")
        ok = any(title[:14] in t for t in R["toasts"])
        delivered.append(ok)
        check(f"队列里的 #{it['id']} 被弹出来了", ok, title[:40])

    print("=" * 72)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("\n复现成功：队列里有通知，但客户端一条都没弹 —— "
              "这就是缺口本身。")
        for f in FAIL:
            print(f"  !! {f}")
    print("RESULT:", "PASS" if not FAIL else "FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
