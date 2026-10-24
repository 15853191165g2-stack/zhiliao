# -*- coding: utf-8 -*-
"""验收「AI 问答 → 模型选择」这条界面链路。

验七件事，任何一件不对都算失败：
  1. AI 问答页上真的有「模型：xxx ▾」按钮，且文字带上了当前模型名
  2. 点开能弹出「AI 模型」窗口
  3. 窗口里列出了内置默认（.env 那一份）
  4. 表单四个输入框（配置名 / 模型名 / 接入地址 / API Key）+ 两个按钮都在
  5. 填一个**连不上**的地址点「测试连接」，要如实报失败，并把最终请求地址回显出来
  6. 保存并启用后，列表里出现这条，且接口的 active_id 指向它
  7. 删掉它之后回落 .env（不能留悬空 active）

用法（先自己起一个隔离后端，别对着用户的实例跑）：
    python scripts/probe_llm_model.py --port 8791

⚠️ 会往库里写一条「探针模型」，**只能用隔离库**。收尾会自己删掉。
⚠️ 界面里的「删除」按钮会弹 messagebox 阻塞脚本，所以删除动作走接口，
   按钮的存在性单独断言（action 已经有 pytest 覆盖）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True, help="已在运行的隔离后端端口")
ap.add_argument("--timeout", type=float, default=90.0)
args = ap.parse_args()

for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

state_dir = ROOT / "logs" / "_diag" / "state_llm_model"
state_dir.mkdir(parents=True, exist_ok=True)
(state_dir / "desktop.json").write_text('{"notify_enabled": false}', encoding="utf-8")
os.environ["QUANZHI_STATE_DIR"] = str(state_dir)

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = f"http://127.0.0.1:{args.port}"


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with op.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


try:
    api("/api/health")
except Exception as e:  # noqa: BLE001
    print(f"✗ 端口 {args.port} 上没有可用的后端：{e}")
    sys.exit(2)

# 先清掉上次残留的探针配置，保证 active 是干净的
for item in (api("/api/llm/profiles").get("data") or {}).get("items") or []:
    if item.get("name", "").startswith("探针"):
        api(f"/api/llm/profiles/{item['id']}", "DELETE")

spec = importlib.util.spec_from_file_location("qz_llm", ROOT / "知了桌面版.py")
m = importlib.util.module_from_spec(spec)
sys.modules["qz_llm"] = m
spec.loader.exec_module(m)

# 模块级常量要一起改，否则请求还发往默认端口（＝测到用户那个实例上）
m.PORT = args.port
m.BASE = BASE
m.API = BASE + "/api"

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


def find_all(widgets, cls):
    return [w for w in widgets if w.winfo_class() == cls]


def pump(root, seconds: float = 1.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.04)


root = tk.Tk()
root.withdraw()
app = m.QuanZhiApp(root, FakeBackend())
root.update()
app.switch_page("chat")
pump(root, 1.5)

fails: list[str] = []

# ---- 1. 按钮存在 ----
page = walk(app.pages["chat"], [])
btn = find(page, "Button", "模型")
label_text = str(btn.cget("text")) if btn else ""
print(f"1) 「模型」按钮：{label_text or '✗ 没找到'}")
if btn is None:
    fails.append("AI 问答页没有模型按钮")

if btn is None:
    print("\n".join(fails))
    sys.exit(1)

# ---- 2. 打开窗口 ----
btn.invoke()
pump(root, 2.0)
tops = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
print("2) 模型窗口：", f"打开 {len(tops)} 个" if tops else "✗ 没打开")
if not tops:
    fails.append("点按钮没有弹出模型窗口")
    root.destroy()
    print("\n".join(fails))
    sys.exit(1)

# 窗口里的 render() 会顺手刷新主界面按钮 —— 在它跑完之后再查文字，
# 否则读到的一定还是开工时的「读取中…」，变成一条假红。
pump(root, 1.0)
label_text = str(btn.cget("text"))
print(f"   主界面按钮文字：{label_text}")
if label_text.strip() in ("模型：读取中… ▾", "模型： ▾"):
    fails.append("模型按钮没拿到当前模型名（后端没响应？）")

win = tops[0]
dw = walk(win, [])

# ---- 3. 默认项 ----
labels = [str(w.cget("text")) for w in find_all(dw, "Label")]
has_env = any("默认" in t for t in labels)
print("3) 列表里有内置默认：", "有" if has_env else "✗ 无")
if not has_env:
    fails.append("模型窗口里看不到内置默认那一项")

# ---- 4. 表单控件 ----
entries = find_all(dw, "Entry")
btn_test = find(dw, "Button", "测试连接")
btn_save = find(dw, "Button", "保存并启用")
# Entry 的创建顺序是 配置名 → 模型名 → 接入地址 → API Key
by_grid = {}
for e in entries:
    try:
        info = e.grid_info()
        by_grid[(str(info.get("row")), str(info.get("column")))] = e
    except Exception:  # noqa: BLE001
        pass
e_name = by_grid.get(("0", "1"))
e_model = by_grid.get(("0", "3"))
e_url = by_grid.get(("1", "1"))
e_key = by_grid.get(("3", "1"))

print(f"4) 表单：配置名={'有' if e_name else '无'} 模型名={'有' if e_model else '无'} "
      f"地址={'有' if e_url else '无'} Key={'有' if e_key else '无'} "
      f"测试={'有' if btn_test else '无'} 保存={'有' if btn_save else '无'}")
for name, w in (("配置名", e_name), ("模型名", e_model), ("接入地址", e_url),
                ("API Key", e_key), ("测试连接", btn_test), ("保存并启用", btn_save)):
    if w is None:
        fails.append(f"模型窗口缺少{name}")
for w, text in ((e_name, "探针模型"), (e_model, "probe-model"),
                (e_url, "http://127.0.0.1:9"), (e_key, "sk-probe-0000")):
    if w is not None:
        w.delete(0, "end")
        w.insert(0, text)

# ---- 5. 测试连接（地址连不上，必须如实报失败并回显地址） ----
status_found = ""
if btn_test is not None and not fails:
    t_start = time.perf_counter()
    btn_test.invoke()
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        pump(root, 0.4)
        for w in find_all(walk(win, []), "Label"):
            try:
                t = str(w.cget("text"))
            except Exception:  # noqa: BLE001
                continue
            if "失败" in t or "连接成功" in t:
                status_found = t
                break
        if status_found:
            break
    elapsed = time.perf_counter() - t_start
    print("5) 测试连接结果：", (status_found[:110] or "✗ 没有回显"))
    if not status_found:
        fails.append("点测试连接后没有任何状态提示")
    elif "失败" not in status_found:
        fails.append(f"不通的地址却报成功：{status_found[:80]}")
    elif "实际请求" not in status_found and "127.0.0.1" not in status_found:
        fails.append("失败时没有回显最终请求地址（用户没法自查）")

    # ★ 关键断言：失败之后按钮必须回到可点状态。
    #   2026-10-24 实测踩过 —— except 里写 `later(lambda: ... e ...)`，
    #   e 出 except 块即失效 → 回调抛 NameError → 按钮永久 disabled、
    #   状态文字永远停在「正在测试…」，用户只能关窗口。
    #   只断言「有失败提示」是抓不到这个的：旧代码连提示都不会出现。
    pump(root, 0.5)
    try:
        st = str(btn_test.cget("state"))
    except Exception:  # noqa: BLE001
        st = "?"
    print(f"   「测试连接」按钮状态：{st}（{elapsed:.1f}s）")
    if st != "normal":
        fails.append(f"测试结束后按钮没恢复可点（state={st}）—— 界面锁死")
    if elapsed > 25:
        fails.append(f"测试耗时 {elapsed:.0f}s 太久，用户会以为卡死")

# ---- 5c. 黑洞地址：必须「超时如实报错」，不能转两分钟 ----
if btn_test is not None and e_url is not None and not fails:
    # 192.0.2.0/24 是 RFC 5737 的测试网段，保证不会真的通 —— 只能等超时。
    # 这是用户真实场景的复刻：校外打校内网关，TCP 无响应。
    e_url.delete(0, "end")
    e_url.insert(0, "http://192.0.2.1:9")
    t_start = time.perf_counter()
    btn_test.invoke()
    line = ""
    deadline = time.time() + 40
    while time.time() < deadline:
        pump(root, 0.4)
        for w in find_all(walk(win, []), "Label"):
            try:
                t = str(w.cget("text"))
            except Exception:  # noqa: BLE001
                continue
            if "失败" in t or "超时" in t:
                line = t
                break
        if line:
            break
    dt = time.perf_counter() - t_start
    print(f"5c) 黑洞地址：{line[:80] or '✗ 一直没结果'}（{dt:.1f}s）")
    if not line:
        fails.append("黑洞地址 40 秒内没给出任何结果（界面卡死）")
    if dt > 30:
        fails.append(f"黑洞地址等了 {dt:.0f}s，太久了 —— 后端没走短超时")
    try:
        if str(btn_test.cget("state")) != "normal":
            fails.append("黑洞地址测试后按钮没恢复")
    except Exception:  # noqa: BLE001
        pass
    e_url.delete(0, "end")
    e_url.insert(0, "http://127.0.0.1:9")

# ---- 5d. 慢服务：请求超时后界面必须自己收场 ----
# 这是用户真实踩到的场景，也是「卡在正在测试」的唯一复现路径：
#   地址填对但网关不响应 → 前端 urllib 超时 → 走 except 分支
#   → 如果 except 里的回调写错（引用 e），按钮永久 disabled、文字停在「正在测试…」。
# 注意「连接被拒」是复现不出来的：后端把失败包装成 HTTP 200 + ok:false，
# 前端走的是成功分支，根本进不到 except。
if btn_test is not None and e_url is not None and e_key is not None and not fails:
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    slow_port = int(srv.getsockname()[1])
    held: list = []

    def blackhole() -> None:
        """只 accept、不 recv 也不 send —— 让对端一直等到自己的超时。"""
        while True:
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                break

    threading.Thread(target=blackhole, daemon=True).start()
    for w, text in ((e_url, f"http://127.0.0.1:{slow_port}/v1"), (e_key, "sk-probe-0000")):
        w.delete(0, "end")
        w.insert(0, text)
    set_url_before = str(btn_test.cget("state"))
    t_start = time.perf_counter()
    btn_test.invoke()
    line = ""
    while time.perf_counter() - t_start < 32:
        pump(root, 0.4)
        for w in find_all(walk(win, []), "Label"):
            try:
                t = str(w.cget("text"))
            except Exception:  # noqa: BLE001
                continue
            if "失败" in t or "超时" in t:
                line = t
                break
        if line:
            break
    dt = time.perf_counter() - t_start
    try:
        st_after = str(btn_test.cget("state"))
    except Exception:  # noqa: BLE001
        st_after = "?"
    print(f"5d) 不响应的网关：{line[:80] or '✗ 一直没结果'}（{dt:.1f}s，"
          f"按钮 {set_url_before}→{st_after}）")
    if not line:
        fails.append("网关不响应时 32 秒内没有结果 —— 界面卡死在「正在测试…」")
    if st_after != "normal":
        fails.append(f"超时后按钮没恢复（state={st_after}）—— 用户只能关窗口")
    if dt > 30:
        fails.append(f"超时场景等了 {dt:.0f}s，太久")
    try:
        srv.close()
        for c in held:
            c.close()
    except Exception:  # noqa: BLE001
        pass
    e_url.delete(0, "end")
    e_url.insert(0, "http://127.0.0.1:9")

# ---- 5e. 后端不可达：走的是前端 except 分支，按钮同样必须恢复 ----
# 为什么单拎这一步：上面几条都是「后端返回 HTTP 200 + ok:false」，
# 前端走的是**成功分支**，压根碰不到 except。而用户当初卡死正是在 except 里
# ——旧写法 `later(lambda: ... e ...)` 里 e 出 except 块即失效，
# 回调抛 NameError 被 _ui_pump 吞掉，按钮永久 disabled、文字停在「正在测试…」。
# 这里把 API 指到一个死端口，精确命中那条路径。
if btn_test is not None and not fails:
    saved_api = m.API
    m.API = "http://127.0.0.1:9/api"      # 谁都连不上，必进 except
    try:
        t_start = time.perf_counter()
        btn_test.invoke()
        line = ""
        while time.perf_counter() - t_start < 15:
            pump(root, 0.3)
            for w in find_all(walk(win, []), "Label"):
                try:
                    t = str(w.cget("text"))
                except Exception:  # noqa: BLE001
                    continue
                if "失败" in t and "正在测试" not in t:
                    line = t
                    break
            if line:
                break
        dt = time.perf_counter() - t_start
        try:
            st_after = str(btn_test.cget("state"))
        except Exception:  # noqa: BLE001
            st_after = "?"
        print(f"5e) 后端不可达：{line[:70] or '✗ 没有反应'}（{dt:.1f}s，按钮 {st_after}）")
        if not line:
            fails.append("后端不可达时界面没有任何报错 —— 按钮锁死在「正在测试…」")
        if st_after != "normal":
            fails.append(f"except 分支没恢复按钮（state={st_after}）")
    finally:
        m.API = saved_api

# ---- 5b. 拉取模型列表 ----
btn_models = find(dw, "Button", "拉取模型列表")
print("5b) 「拉取模型列表」按钮：", "有" if btn_models else "✗ 无")
if btn_models is None:
    fails.append("模型窗口里没有「拉取模型列表」按钮")
elif not fails:
    # 先用连不上的地址：必须如实报「没拿到」，并且把实际请求地址回显出来
    btn_models.invoke()
    got = ""
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        pump(root, 0.4)
        for w in find_all(walk(win, []), "Label"):
            try:
                t = str(w.cget("text"))
            except Exception:  # noqa: BLE001
                continue
            if "没拿到模型列表" in t or "拉取失败" in t:
                got = t
                break
        if got:
            break
    print("    错误地址 ->", (got[:110] or "✗ 没有提示"))
    if not got:
        fails.append("拉取模型列表失败时没有任何提示（用户不知道点上没有）")
    elif "/models" not in got:
        fails.append("拉取失败时没回显实际请求地址")

    # 给了真 key 才跑真实网关那一段（可选，不给就只验失败分支）
    gw_key = os.environ.get("PROBE_GW_KEY", "")
    if gw_key and not fails:
        gw_base = os.environ.get("PROBE_GW_BASE", "http://aigw.dlut.edu.cn/v1/messages")
        for w, text in ((e_url, gw_base), (e_key, gw_key)):
            w.delete(0, "end")
            w.insert(0, text)
        btn_models.invoke()
        picker = None
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            pump(root, 0.4)
            # ⚠️ 选择窗口的 master 是「AI 模型」窗口，不是 root ——
            #    只扫 root.winfo_children() 永远找不到它（第一版就是这么假红的）。
            for base in (root, win):
                for t in walk(base, []):
                    if t.winfo_class() != "Toplevel":
                        continue
                    try:
                        if t is not win and "选择模型" in str(t.title()):
                            picker = t
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if picker:
                    break
            if picker:
                break
        print("    真实网关 ->", f"弹出选择窗口（{picker.title()}）" if picker else "✗ 没弹出")
        if not picker:
            fails.append("真实网关拉取成功后没有弹出模型选择窗口")
        else:
            boxes = find_all(walk(picker, []), "Listbox")
            n = boxes[0].size() if boxes else 0
            first = boxes[0].get(0) if n else ""
            print(f"    列表条数={n} 首项={first or '-'}")
            if n < 5:
                fails.append(f"模型列表只有 {n} 条，不像真的")
            elif e_model is not None:
                # 走「用这个」按钮 —— 和用户点一下是同一条路。
                # 只断言「非空」是假绿：填进去的可能还是上一次的残留值。
                use_btn = find(walk(picker, []), "Button", "用这个")
                if use_btn is None:
                    fails.append("选择窗口里没有「用这个」按钮")
                else:
                    if n:
                        boxes[0].selection_clear(0, "end")
                        boxes[0].selection_set(0)
                    use_btn.invoke()
                    pump(root, 1.2)
                    got_model = str(e_model.get()).strip()
                    print(f"    选完「模型名」= {got_model!r}")
                    if got_model != first:
                        fails.append(f"选中的是 {first!r}，模型名却填成了 {got_model!r}")
            try:
                picker.destroy()
            except Exception:  # noqa: BLE001
                pass
        # 还原成探针自己那份，后面 6/7 步还要用
        for w, text in ((e_url, "http://127.0.0.1:9"), (e_name, "探针模型"),
                        (e_model, "probe-model"), (e_key, "sk-probe-0000")):
            if w is not None:
                w.delete(0, "end")
                w.insert(0, text)

# ---- 6. 保存并启用 ----
if btn_save is not None and not fails:
    btn_save.invoke()
    deadline = time.time() + args.timeout
    row_ok = False
    while time.time() < deadline:
        pump(root, 0.4)
        if any("探针模型" in str(w.cget("text") if hasattr(w, "cget") else "")
               for w in find_all(walk(win, []), "Label")):
            row_ok = True
            break
    data = api("/api/llm/profiles").get("data") or {}
    active_id = data.get("active_id")
    probe = [i for i in data.get("items") or [] if i.get("name") == "探针模型"]
    print(f"6) 保存后列表出现={'是' if row_ok else '✗ 否'} "
          f"active_id={active_id} 命中 {len(probe)} 条")
    if not row_ok:
        fails.append("保存后列表里没有这条配置")
    if not probe:
        fails.append("接口里没有存进去")
    elif active_id != probe[0]["id"]:
        fails.append(f"保存并启用后 active_id={active_id} 没指向它")
    if probe and "sk-probe" in json.dumps(probe, ensure_ascii=False):
        fails.append("接口把明文 API Key 回显出来了")
    # 主界面按钮要跟着变
    pump(root, 0.8)
    btn_text = str(btn.cget("text"))
    print(f"   主界面按钮现在显示：{btn_text}")
    if "probe-model" not in btn_text:
        fails.append(f"主界面按钮没跟着更新（当前 {btn_text}）")

    # ---- 7. 删掉回落 ----
    if probe:
        api(f"/api/llm/profiles/{probe[0]['id']}", "DELETE")
        after = api("/api/llm/profiles").get("data") or {}
        print(f"7) 删除后 active_id={after.get('active_id')} "
              f"生效来源={after.get('effective_source')}")
        if after.get("active_id") is not None:
            fails.append("删除激活项后 active_id 没回落")
        if after.get("effective_source") != "env":
            fails.append("删除激活项后没回落 .env")

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
print("PASS：模型选择的界面链路可用（测试配置已清理）")
