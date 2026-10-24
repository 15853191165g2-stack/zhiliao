"""一体化验收：自己拉起后端 → 跑完所有接口测试 → 关掉后端。

这样就不会被外部环境的进程回收干扰。
用法：python\\python.exe scripts\\verify_all.py [--port 8765]

    --port 默认 8765。用户自己的「知了」正开着时会占住 8765，
    这时换一个空端口跑，就不会和自己的实例抢，也**不会**误测到旧代码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "verify_all.log"
f = open(LOG, "w", encoding="utf-8", buffering=1)

sys.path.insert(0, str(ROOT / "scripts"))
import user_flags as uf  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--port", type=int, default=8765)
PORT = _ap.parse_args().port

_p = _f = 0


def log(m: str) -> None:
    print(m)
    f.write(m + "\n")
    f.flush()


def check(name: str, cond: bool, extra: str = "") -> None:
    global _p, _f
    if cond:
        _p += 1
        log(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        _f += 1
        log(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
API = f"http://127.0.0.1:{PORT}/api"

# 会调 GLM 的接口在免费档上可能很慢（并发 1 + 限流退避 5→10→20→40s），
# 所以给它们单独一个宽裕的超时，别用默认 20 秒把正常等待判成失败。
AI_TIMEOUT = 240.0


def get(path: str, timeout: float = 20.0, retries: int = 0):
    last = None
    for attempt in range(retries + 1):
        try:
            with OPENER.open(API + path, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:              # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(3)
    raise last


def post(path: str, payload=None, timeout: float = 150.0, retries: int = 0):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                API + path, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:              # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(3)
    raise last


def main() -> None:
    log("=" * 62)
    log("  知了 电脑版 · 一体化验收（自带后端）")
    log(f"  端口 = {PORT}")
    log("=" * 62)

    env = dict(subprocess.os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["NO_PROXY"] = "localhost,127.0.0.1"

    bklog = open(ROOT / "logs" / "verify_backend.log", "a", encoding="utf-8")

    # ★ 先确认端口是空的。残留的旧后端占着 8765 时，自己启的后端会绑定失败，
    #   但 /health 依然有响应 —— 测试会对着**旧代码**跑，全绿也没意义。
    if uf.guard_port(PORT, log=log):
        bklog.close()
        sys.exit(2)

    backend = subprocess.Popen(
        [str(ROOT / "python" / "python.exe"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info"],
        cwd=str(ROOT / "backend"), env=env,
        stdout=bklog, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # 后端起不来时会提前 return，所以先占位，避免 finally 里 NameError
    # （注意：变量名不能叫 uf —— 那会遮蔽上面模块级的 `import user_flags as uf`）
    _snap: dict = {}

    try:
        log(f"\n后端 PID = {backend.pid}，等待就绪…")
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
            return

        # ★ 用户状态全表快照（只读打开，不锁库）。
        #   各分节自己也会精确还原测过的那一条；这一层是**兜底**：
        #   万一某个测试意外动到了别的行，也能被发现并改回来。
        _snap = uf.snapshot()
        log(f"        用户状态快照已取（{len(_snap)} 行），跑完会还原")

        # ---------------- 健康 ----------------
        log("\n[1] 健康检查")
        h = get("/health")["data"]
        check("数据库正常", (h.get("database") or {}).get("ok") is True)
        check("AI 已启用", (h.get("ai") or {}).get("enabled") is True,
              (h.get("ai") or {}).get("model", ""))
        check("定时任务运行中", (h.get("scheduler") or {}).get("running") is True)
        log(f"        counts = {h.get('counts')}")

        # ---------------- 信息流 ----------------
        log("\n[2] 信息流与卡片契约")
        p = get("/notifications?page=1&page_size=20")["data"]
        check("总数 > 0", p["total"] > 0, f"total={p['total']}")
        items = p["items"]
        bad = [i["id"] for i in items if len(i.get("summary_lines") or []) != 3]
        check("★ 每条都正好 3 行摘要", not bad, f"异常 {len(bad)} 条")
        check("时间带 +08:00", "+08:00" in (items[0].get("published_at") or ""))
        check("重要度取值合法",
              all(i["importance"] in ("urgent", "important", "normal") for i in items))

        nid = items[0]["id"]
        d = get(f"/notifications/{nid}")["data"]
        check("详情含正文", len(d.get("content_text") or "") > 50,
              f"{len(d.get('content_text') or '')} 字")
        check("详情 3 行摘要", len(d.get("summary_lines") or []) == 3)

        # ---------------- 收藏 / 已读 / 置顶 ----------------
        log("\n[3] 用户状态：收藏 / 已读 / 置顶")
        # ★ 必须挑一条「既没置顶也没收藏」的做目标。
        #   /pin 和 /star 是**切换**语义：对着用户已经置顶的那条（列表第一条就是）
        #   调一次，等于把他的置顶取消掉，后面所有断言全红 —— 而代码是对的。
        #   实测踩过：用户置顶了 id=266，脚本把它取消后还断言"置顶成功"，6 项全 FAIL。
        pool = [i for i in items if not i.get("is_pinned") and not i.get("is_starred")]
        nid = (pool or items)[0]["id"]
        if not pool:
            log("        !! 前 20 条全都置顶或收藏了，只能退回第一条")
        before = get(f"/notifications/{nid}")["data"]
        orig = {"is_read": bool(before.get("is_read")),
                "is_starred": bool(before.get("is_starred")),
                "is_pinned": bool(before.get("is_pinned"))}
        log(f"        测试前状态 = {orig}")

        try:
            check("列表返回 is_pinned 字段", "is_pinned" in items[0])

            # ---- 已读勾选框：两个方向都要能用 ----
            r_on = post(f"/notifications/{nid}/read", {"is_read": True})["data"]
            check("勾选已读", r_on.get("is_read") is True)
            r_off = post(f"/notifications/{nid}/read", {"is_read": False})["data"]
            check("★ 取消已读（勾选框必须能反选）", r_off.get("is_read") is False)
            post(f"/notifications/{nid}/read", {"is_read": True})

            unread = get("/notifications?unread_only=true&page_size=100")["data"]
            check("只看未读筛选生效",
                  all(not i["is_read"] for i in unread["items"]),
                  f"total={unread['total']}")
            read_only = get("/notifications?read_only=true&page_size=100")["data"]
            check("只看已读筛选生效",
                  bool(read_only["items"]) and all(i["is_read"] for i in read_only["items"]),
                  f"total={read_only['total']}")

            # ---- 置顶：可观察效果是「出现在置顶列表 + 排在最前」 ----
            # 显式传 is_pinned，不用切换语义 —— 断言不该依赖"用户原来标没标"。
            p_on = post(f"/notifications/{nid}/pin", {"is_pinned": True})["data"]
            check("置顶成功", p_on.get("is_pinned") is True)

            pinned_only = get("/notifications?pinned_only=true&page_size=100")["data"]
            pinned_ids = [i["id"] for i in pinned_only["items"]]
            check("★ 置顶后出现在置顶列表里", nid in pinned_ids, f"{len(pinned_ids)} 条置顶")
            check("只看置顶筛选生效",
                  bool(pinned_only["items"]) and all(i["is_pinned"] for i in pinned_only["items"]),
                  f"total={pinned_only['total']}")

            top = get("/notifications?page=1&page_size=20")["data"]["items"]
            check("★ 置顶的排在最前（第一条必须已置顶）",
                  top[0]["is_pinned"] is True, f"第一位 id={top[0]['id']}")
            check("置顶没有改动发布时间",
                  get(f"/notifications/{nid}")["data"].get("published_at")
                  == before.get("published_at"))

            p_off = post(f"/notifications/{nid}/pin", {"is_pinned": False})["data"]
            check("★ 取消置顶", p_off.get("is_pinned") is False)
            check("取消置顶后不再出现在置顶列表",
                  nid not in [i["id"] for i in get(
                      "/notifications?pinned_only=true&page_size=100")["data"]["items"]])
            top2 = get("/notifications?page=1&page_size=20")["data"]["items"]
            check("取消置顶后不再排在最前", top2[0]["id"] != nid)

            # ---- 收藏 ----
            s1 = post(f"/notifications/{nid}/star", {"is_starred": True})["data"]
            check("收藏成功", s1.get("is_starred") is True)
            starred = get("/notifications?starred_only=true")["data"]
            check("收藏筛选能查到", starred["total"] >= 1, f"total={starred['total']}")

            # ---- 批量接口（只动这一个 id，不动别人的） ----
            bulk = post("/notifications/read-all",
                        {"ids": [nid], "is_read": False})["data"]
            check("批量接口可用", int(bulk.get("updated", -1)) >= 0,
                  f"updated={bulk.get('updated')}")
            check("批量后状态正确",
                  get(f"/notifications/{nid}")["data"]["is_read"] is False)

            # ---- 健康检查里要有未读 / 置顶计数 ----
            cnt = get("/health")["data"]["counts"]
            check("健康检查带未读/置顶计数",
                  isinstance(cnt.get("unread"), int) and isinstance(cnt.get("pinned"), int),
                  f"unread={cnt.get('unread')} pinned={cnt.get('pinned')}")
        finally:
            # 还原用户原来的标记
            for key, path in (("is_read", "read"), ("is_starred", "star"),
                              ("is_pinned", "pin")):
                try:
                    post(f"/notifications/{nid}/{path}", {key: orig[key]})
                except Exception as e:      # noqa: BLE001
                    log(f"        !! 还原 {key} 失败：{e}")
            after = get(f"/notifications/{nid}")["data"]
            still = {k: bool(after.get(k)) for k in orig}
            check("测试后状态已还原", still == orig, f"{still}")

        # ---------------- 简报 ----------------
        log("\n[4] 今日简报")
        # 简报会调 GLM；免费档限流时可能要退避几十秒，所以超时给宽一点。
        # 重试 2 次，避免把「模型正在排队」误判成「接口坏了」。
        b = get("/brief/today?hours=240", timeout=AI_TIMEOUT, retries=2)["data"]
        check("简报有 headline", bool(b.get("headline")))
        check("简报有朗读文本", len(b.get("speech_text") or "") > 20,
              f"{len(b.get('speech_text') or '')} 字")
        check("简报统计齐全", set(b.get("counts") or {}) >=
              {"total", "urgent", "due_soon", "important", "unread"})
        log(f"        generated_by = {b.get('generated_by')}")
        log(f"        headline     = {b.get('headline')}")

        # ---------------- 来源 ----------------
        log("\n[5] 来源管理")
        srcs = get("/sources")["data"]
        check("来源数量 ≥ 12", len(srcs) >= 12, f"{len(srcs)} 个")
        check("来源字段齐全",
              all(k in srcs[0] for k in ("id", "name", "school", "enabled", "last_status")))
        ok_cnt = sum(1 for s in srcs if s.get("last_status") == "ok")
        log(f"        抓取正常 {ok_cnt} / {len(srcs)} 个来源")

        # ---------------- AI 问答 ----------------
        log("\n[6] AI 问答（走当前生效的模型，可能较慢）")
        try:
            # 期望值不能写死 glm-4.7-flash：用户切到自定义模型（比如学校 AI 网关的
            # GLM-4.6-W8A8）之后，这条会变成假红 —— 功能明明是好的。
            # 正确做法是问 /health「现在生效的是哪个」再比。
            eff = (get("/health").get("data") or {}).get("ai") or {}
            expect = {"local"} | ({eff["model"]} if eff.get("model") else set())
            c = post("/chat", {"question": "最近有什么奖学金通知？",
                               "days": 180, "top_k": 8},
                     timeout=AI_TIMEOUT, retries=2)["data"]
            check("问答有回答", bool((c.get("answer") or "").strip()))
            check("问答标注生成方", c.get("generated_by") in expect,
                  f"{c.get('generated_by')}（当前生效 {eff.get('model') or '未配置'}）")
            log(f"        generated_by = {c.get('generated_by')}, "
                f"citations = {len(c.get('citations') or [])}")
            log(f"        回答前 200 字：{(c.get('answer') or '')[:200]}")
        except Exception as e:  # noqa: BLE001
            check("问答接口可用", False, f"{type(e).__name__}: {e}")

        # ---------------- 导入能力 ----------------
        log("\n[7] 导入能力说明（诚实边界）")
        cap = get("/import/capability")["data"]
        check("微信不支持自动抓取（如实标注）",
              (cap.get("wechat_official_account") or {}).get("supported") is False)
        check("小程序不支持（如实标注）",
              (cap.get("miniprogram") or {}).get("supported") is False)
        check("iOS 读通知不支持（如实标注）",
              (cap.get("ios_notification") or {}).get("supported") is False)
        check("Android 读通知支持",
              (cap.get("android_notification") or {}).get("supported") is True)

    except Exception as e:                  # noqa: BLE001
        # 任何一步炸了都要留下清晰结论，而不是甩一堆 traceback。
        # AI 相关步骤超时是最常见的情况（免费档排队），单独说清楚。
        import traceback
        log(f"\n!! 验收中断：{type(e).__name__}: {e}")
        if "timed out" in str(e).lower():
            log("   这通常是 GLM 免费档正在排队/限流（并发 1）。")
            log("   可以隔几分钟重跑；或临时把 backend/.env 的 AI_API_KEY 清空，")
            log("   走本地规则摘要就会秒回（链路仍可完整验收）。")
        log(traceback.format_exc())
    finally:
        # ★ 全表兜底还原，必须赶在后端关闭之前（还原是调接口做的）
        try:
            if _snap:
                n = uf.restore(_snap, post=post)
                same = uf.verify(_snap, log=log)
                log(f"        用户状态兜底还原：{n} 处；与快照比对 = "
                    f"{'一致' if same else '仍有差异！'}")
        except Exception as e:              # noqa: BLE001
            log(f"        !! 用户状态兜底还原失败：{e}")
        log("\n收尾：关闭后端…")
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            backend.kill()

    log("\n" + "=" * 62)
    log(f"  结果：{_p} 通过 / {_f} 失败 / 共 {_p + _f} 项")
    log("=" * 62)
    f.close()
    sys.exit(1 if _f else 0)


if __name__ == "__main__":
    main()
