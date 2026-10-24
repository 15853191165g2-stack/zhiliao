"""知了 电脑版 · 验收测试。

分两部分：
  A. 后端接口冒烟（信息流 / 详情 / 简报 / 来源 / 问答 / 收藏 / 已读）
  B. 桌面壳的纯逻辑单测（3 行摘要兜底、时间格式化、关键信息标签）

用法：
    python scripts/smoke_desktop.py            # A+B，需要后端在 8765 跑着
    python scripts/smoke_desktop.py --no-api   # 只跑 B
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8765"
API = BASE + "/api"

# 本机回环请求必须绕开系统代理，否则会被代理返回 502。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_pass = 0
_fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        _fail += 1
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def get(path: str, timeout: float = 15.0, retries: int = 0):
    url = path if path.startswith("http") else API + path
    last = None
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(url, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:              # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(3)
    raise last


def post(path: str, payload=None, timeout: float = 30.0, retries: int = 0):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                API + path, data=data,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"},
                method="POST")
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:              # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(3)
    raise last


def _ai_expect(get) -> set:
    """「生成方」这一栏允许的取值 = 当前生效模型 + local。

    为什么现算：内置默认是 glm-4.7-flash，但用户可以换成任何 OpenAI 兼容的服务
    （学校 AI 网关的 GLM-4.6-W8A8 就是实测案例）。写死期望值的话，功能明明是好的，
    脚本却报红 —— 那是把「我记错了」当成「产品坏了」。
    """
    try:
        eff = (get("/health").get("data") or {}).get("ai") or {}
    except Exception:  # noqa: BLE001
        return {"local"}
    out = {"local"}
    if eff.get("model"):
        out.add(str(eff["model"]))
    return out

# --------------------------------------------------------------------------- #
def test_api() -> None:
    print("\n[A] 后端接口冒烟  " + API)
    print("-" * 62)

    # ---- 健康 ----
    h = get("/health")["data"]
    check("GET /health 可用", h.get("app") is not None)
    check("数据库正常", (h.get("database") or {}).get("ok") is True)
    check("AI 已启用", (h.get("ai") or {}).get("enabled") is True,
          f"model={(h.get('ai') or {}).get('model')}")
    check("适配器已注册", len(h.get("adapters") or []) >= 3,
          str(h.get("adapters")))
    check("定时任务在跑", (h.get("scheduler") or {}).get("running") is True,
          f"cron={h.get('cron')}")

    # ---- 信息流 ----
    p = get("/notifications?page=1&page_size=20")["data"]
    check("GET /notifications 分页结构", {"items", "total", "page", "page_size"} <= set(p))
    check("信息流有数据", p["total"] > 0, f"total={p['total']}")
    items = p["items"]
    check("首页返回非空", len(items) > 0, f"items={len(items)}")

    if items:
        first = items[0]
        check("卡片必备字段齐全",
              all(k in first for k in
                  ("id", "title", "summary_lines", "importance", "category",
                   "source_name", "published_at")))
        # ★ 项目铁律：每条通知必须正好 3 行摘要
        bad = [i["id"] for i in items if len(i.get("summary_lines") or []) != 3]
        check("每条都是 3 行摘要", not bad, f"异常 {len(bad)} 条 {bad[:5]}")
        # 时间必须带 +08:00
        pub = first.get("published_at") or ""
        check("时间带 +08:00 时区", "+08:00" in pub, pub)
        check("重要度取值合法",
              all(i["importance"] in ("urgent", "important", "normal") for i in items))

        # ---- 详情 ----
        # ★ 目标要挑「没被收藏、没被置顶」的那条：/star 是切换语义，
        #   用户已经收藏了列表第一条（实测 id=266），再切一次就是取消收藏，
        #   "POST /star 生效" 会假红。用户的标记不该成为测试的隐含前提。
        pool = [i for i in items if not i.get("is_starred") and not i.get("is_pinned")]
        nid = (pool or items)[0]["id"]
        d = get(f"/notifications/{nid}")["data"]
        check("GET /notifications/{id} 可用", d.get("id") == nid)
        check("详情含正文", bool((d.get("content_text") or "").strip()),
              f"{len(d.get('content_text') or '')} 字")
        check("详情含摘要 3 行", len(d.get("summary_lines") or []) == 3)
        check("AI 状态已落库", d.get("ai_status") in ("done", "pending", "failed"),
              str(d.get("ai_status")))

        # ---- 已读 / 收藏 ----
        r1 = post(f"/notifications/{nid}/read", {"is_read": True})["data"]
        check("POST /read 生效", r1.get("is_read") is True)
        s1 = post(f"/notifications/{nid}/star", {"is_starred": True})["data"]
        check("POST /star 生效", s1.get("is_starred") is True)
        s2 = post(f"/notifications/{nid}/star", {"is_starred": False})["data"]
        check("POST /star 可取消", s2.get("is_starred") is False)

        # ---- 筛选 ----
        f1 = get("/notifications?starred_only=true")["data"]
        check("收藏筛选可用", isinstance(f1["total"], int), f"total={f1['total']}")
        f2 = get("/notifications?importance=urgent")["data"]
        check("重要度筛选可用", isinstance(f2["total"], int), f"urgent={f2['total']}")
        f3 = get("/notifications?q=" + urllib.parse.quote("通知"))["data"]
        check("关键词搜索可用", isinstance(f3["total"], int), f"匹配={f3['total']}")

    # ---- 简报 ----
    # 会调 GLM；免费档排队时可能需要退避几十秒，超时给宽裕些并重试。
    b = get("/brief/today?hours=240", timeout=240.0, retries=2)["data"]
    check("GET /brief/today 可用", "headline" in b)
    check("简报含统计", isinstance(b.get("counts"), dict), str(b.get("counts")))
    check("简报含朗读文本", bool((b.get("speech_text") or "").strip()),
          f"{len(b.get('speech_text') or '')} 字")
    # 期望值取「当前生效的模型」，不能写死 glm-4.7-flash ——
    # 用户切到自定义模型后这条会变成假红（实测 GLM-4.6-W8A8 报红）。
    check("简报标注生成方", b.get("generated_by") in _ai_expect(get),
          str(b.get("generated_by")))

    # ---- 来源 ----
    srcs = get("/sources")["data"]
    check("GET /sources 可用", len(srcs) >= 12, f"{len(srcs)} 个来源")
    check("来源字段齐全",
          all(k in srcs[0] for k in ("id", "name", "school", "enabled", "last_status")))

    # ---- 问答（走 GLM，慢）----
    try:
        c = post("/chat", {"question": "最近有什么通知？", "days": 90, "top_k": 8},
                 timeout=240.0, retries=2)["data"]
        check("POST /chat 可用", bool((c.get("answer") or "").strip()))
        check("问答标注生成方", c.get("generated_by") in _ai_expect(get),
              str(c.get("generated_by")))
        check("问答有引用条数输出", isinstance(c.get("citations"), list),
              f"{len(c.get('citations') or [])} 条引用")
    except Exception as e:  # noqa: BLE001
        check("POST /chat 可用", False, f"{type(e).__name__}: {e}")

    # ---- 导入能力说明 ----
    cap = get("/import/capability")["data"]
    check("GET /import/capability 可用", "article_url" in cap)
    check("如实标注微信不支持", (cap.get("wechat_official_account") or {}).get("supported") is False)
    check("如实标注 iOS 不支持", (cap.get("ios_notification") or {}).get("supported") is False)


# --------------------------------------------------------------------------- #
def test_desktop_logic() -> None:
    print("\n[B] 桌面壳逻辑单测（不依赖后端）")
    print("-" * 62)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "quanzhi_desktop", ROOT / "知了桌面版.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # ---- 3 行摘要兜底 ----
    three = mod.ensure_three_lines({"summary_lines": ["a", "b", "c", "d"]})
    check("超过 3 行会被截断", three == ["a", "b", "c"], str(three))

    two = mod.ensure_three_lines({"summary_lines": ["a", "b"]})
    check("不足 3 行会补齐", len(two) == 3 and two[0] == "a", str(two))

    empty = mod.ensure_three_lines({})
    check("完全没摘要也不崩", len(empty) == 3 and all(isinstance(x, str) for x in empty),
          str(empty))

    from_excerpt = mod.ensure_three_lines(
        {"excerpt": "第一句话。第二句话。第三句话。第四句话。"})
    check("能从摘要文本切出 3 行",
          len(from_excerpt) == 3 and from_excerpt[0].endswith("。"), str(from_excerpt))

    # ---- 时间格式化 ----
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    iso_recent = (now - dt.timedelta(minutes=30)).isoformat()
    check("相对时间·分钟", "分钟前" in mod.QuanZhiApp._rel_time(iso_recent),
          mod.QuanZhiApp._rel_time(iso_recent))
    iso_hours = (now - dt.timedelta(hours=5)).isoformat()
    check("相对时间·小时", "小时前" in mod.QuanZhiApp._rel_time(iso_hours),
          mod.QuanZhiApp._rel_time(iso_hours))
    iso_days = (now - dt.timedelta(days=3)).isoformat()
    check("相对时间·天", "天前" in mod.QuanZhiApp._rel_time(iso_days),
          mod.QuanZhiApp._rel_time(iso_days))
    iso_old = (now - dt.timedelta(days=400)).isoformat()
    check("相对时间·久远则显示日期", mod.QuanZhiApp._rel_time(iso_old)[:2] == "20",
          mod.QuanZhiApp._rel_time(iso_old))
    check("空时间不崩", mod.QuanZhiApp._rel_time(None) == "--")

    # ---- 关键信息标签 ----
    chips = mod.QuanZhiApp._key_info_chips({
        "deadline": "2026-10-15 17:00", "location": "图书馆",
        "audience": "2024级本科生", "materials": ["申请表", "成绩单"],
        "contact": "8470xxxx",
    })
    check("关键信息能转成标签", len(chips) == 4, f"{len(chips)} 个（最多 4 个）")
    check("截止时间排在最前", "截止" in chips[0][0], chips[0][0])
    joined = " ".join(t for t, _ in chips)
    check("材料标签正确", "申请表" in joined, joined)

    check("空关键信息返回空", mod.QuanZhiApp._key_info_chips({}) == [])

    # ---- 句子切分 ----
    sents = mod._split_sentences("第一句。第二句！第三句？")
    check("中英文标点都能切句", len(sents) == 3, str(sents))

    # ---- 配置常量 ----
    check("端口不是常见占用端口", mod.PORT == 8765, str(mod.PORT))
    check("三种重要度配色齐全", set(mod.IMPORTANCE) == {"urgent", "important", "normal"})
    check("紧急=红色（国内习惯）", mod.IMPORTANCE["urgent"].upper() == "#E5484D")


# --------------------------------------------------------------------------- #
def test_portable_runtime() -> None:
    print("\n[C] 便携运行时（离线可验证）")
    print("-" * 62)

    py = ROOT / "python" / "python.exe"
    check("自带解释器存在", py.exists(), str(py))
    check("后端源码存在", (ROOT / "backend" / "app" / "main.py").exists())
    check("来源配置存在", (ROOT / "backend" / "sources").is_dir())
    check("启动脚本存在", (ROOT / "知了.bat").exists())
    check("配置文件存在", (ROOT / "backend" / ".env").exists())

    # 确认没把密钥写进源码
    src = (ROOT / "知了桌面版.py").read_text(encoding="utf-8")
    check("桌面壳源码不含密钥", "995f0e32" not in src)

    env = (ROOT / "backend" / ".env").read_text(encoding="utf-8")
    check("密钥只放在 .env 里", "AI_API_KEY=" in env)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-api", action="store_true", help="跳过接口冒烟")
    ap.add_argument("--port", type=int, default=8765,
                    help="后端端口（默认 8765）。用户自己的实例占着 8765 时换一个空端口。")
    args = ap.parse_args()

    # ★ A 部分的接口地址要跟着 --port 走。
    #   本产品自己的 PORT 恒为 8765（下面 test_portable_runtime 会断言），
    #   但**验收目标**可以是另一个端口 —— 否则用户开着自己的「知了」时
    #   就只能对着那个实例测，而那可能跑的是旧代码。
    global BASE, API
    BASE = f"http://127.0.0.1:{args.port}"
    API = BASE + "/api"

    print("=" * 62)
    print("  知了 电脑版 · 验收测试")
    print(f"  接口地址 = {API}")
    print("=" * 62)

    test_portable_runtime()
    test_desktop_logic()
    if not args.no_api:
        # ★ A 部分会真的改用户标记（POST /read 置已读、POST /star 翻转收藏），
        #   以前**从来没有还原过** —— 跑一次就悄悄把一条通知标成已读。
        #   这里补上快照/还原，和 verify_all / probe_flags 用同一套 user_flags，
        #   不要再各写一份。
        sys.path.insert(0, str(ROOT / "scripts"))
        import user_flags as uf  # noqa: E402

        before = uf.snapshot()
        try:
            try:
                test_api()
            except urllib.error.URLError as e:
                print(f"\n[A] 跳过：后端未运行（{e}）")
                print("    先双击「知了.bat」启动，或执行：")
                print("    python\\python.exe 知了桌面版.py")
        finally:
            try:
                n = uf.restore(before, post=post)
                same = uf.verify(before)
                print(f"\n用户状态还原：{n} 处；与快照比对 = "
                      f"{'一致' if same else '仍有差异！'}")
            except Exception as e:          # noqa: BLE001
                print(f"\n!! 用户状态还原失败：{e}")

    print("\n" + "=" * 62)
    total = _pass + _fail
    print(f"  结果：{_pass} 通过 / {_fail} 失败 / 共 {total} 项")
    print("=" * 62)
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
