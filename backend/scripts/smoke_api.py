"""接口冒烟测试：把后端主要接口打一遍，人眼可读地输出结果。

    python scripts/smoke_api.py                  # 默认 http://127.0.0.1:8000
    python scripts/smoke_api.py http://127.0.0.1:8011

阶段 2-6 每加一个功能，都应该往这里加一段，保证接口没被改坏。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
LINE = "-" * 72


def _encode(path: str) -> str:
    """URL 里带中文查询参数时必须转义，否则 http.client 会按 ascii 编码报错。"""
    return urllib.parse.quote(path, safe="/?=&:%")


def get(path: str):
    with urllib.request.urlopen(BASE + _encode(path), timeout=30) as r:
        return json.load(r)


def post(path: str, body: dict | None = None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + _encode(path),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main() -> int:
    print("=" * 72)
    print(f"知了 · 接口冒烟测试   {BASE}")
    print("=" * 72)

    # ---- health ----
    h = get("/api/health")["data"]
    print(
        f"/api/health            db_ok={h['database']['ok']}  "
        f"来源={h['counts']['sources']}  通知={h['counts']['notifications']}  "
        f"待摘要={h['counts']['ai_pending']}"
    )
    print(f"                       适配器={h['adapters']}  cron={h['cron']}")
    print(f"                       AI={h['ai']}  调度器={h['scheduler']['running']}")

    # ---- 信息流 ----
    feed = get("/api/notifications?page_size=5")["data"]
    print(f"{LINE}\n/api/notifications     共 {feed['total']} 条，本页 {len(feed['items'])} 条")
    for it in feed["items"]:
        print(f"   #{it['id']} [{it['importance_label']}/{it['category']}] {it['title'][:36]}")
        print(f"         来源={it['source_name']}  发布={it['published_at']}")

    if not feed["items"]:
        print("（库里还没有数据，先跑 python scripts/seed_demo.py）")
        return 0

    # ---- 筛选 ----
    urgent = get("/api/notifications?importance=urgent")["data"]
    print(f"{LINE}\n筛选 importance=urgent  共 {urgent['total']} 条")
    for it in urgent["items"]:
        print(f"   ! {it['title'][:44]}")

    # ---- 搜索 ----
    kw = get("/api/notifications?q=奖学金")["data"]
    print(f"{LINE}\n搜索 q=奖学金           共 {kw['total']} 条")

    # ---- 详情（含 AI 摘要与关键信息）----
    target = next((i for i in feed["items"] if "奖学金" in i["title"]), feed["items"][0])
    d = get(f"/api/notifications/{target['id']}")["data"]
    print(f"{LINE}\n详情 #{d['id']}  {d['title']}")
    print(f"  正文长度: {len(d.get('content_text') or '')} 字")
    print("  摘要（3 行）:")
    for i, line in enumerate(d.get("summary_lines") or [], 1):
        print(f"    {i}. {line}")
    ki = d.get("key_info") or {}
    print("  关键信息:")
    print(f"    截止时间 : {ki.get('deadline')}  ->  解析值 {ki.get('deadline_ts')}")
    print(f"    对象     : {ki.get('audience')}")
    print(f"    地点     : {ki.get('location')}")
    print(f"    材料     : {ki.get('materials')}")
    print(f"    联系人   : {ki.get('contact')}")
    print(f"  重要程度={d['importance_label']}  分类={d['category']}  模型={d.get('ai_model')}")

    # ---- 已读 / 收藏 ----
    r = post(f"/api/notifications/{d['id']}/read")["data"]
    s = post(f"/api/notifications/{d['id']}/star")["data"]
    print(f"{LINE}\n已读={r['is_read']}  收藏={s['is_starred']}")

    # ---- 今日简报 ----
    brief = get("/api/brief/today")["data"]
    print(f"{LINE}\n/api/brief/today       {brief['greeting']}｜{brief['headline']}")
    for sec in brief["sections"]:
        print(f"   {sec['title']}  ({len(sec['items'])} 条)")
        for line in sec["items"][:3]:
            print(f"     - {line}")
    print(f"   TTS 文本：{brief['speech_text'][:120]}…")

    # ---- AI 问答 ----
    chat = post("/api/chat", {"question": "奖学金申请要交什么材料？", "days": 90})["data"]
    print(f"{LINE}\n/api/chat              引用 {len(chat['citations'])} 条  生成方={chat['generated_by']}")
    print("   " + chat["answer"].replace("\n", "\n   ")[:500])

    # ---- 来源 ----
    src = get("/api/sources")["data"]
    print(f"{LINE}\n/api/sources           {len(src)} 个来源")
    for x in src:
        print(
            f"   #{x['id']} {x['name']} [{x['adapter']}] "
            f"{x['last_status']} / {x['item_count']} 条"
        )

    # ---- 手动抓取 ----
    run = post("/api/ingest/run")["data"]
    print(
        f"{LINE}\n/api/ingest/run        抓取 {run['total_items']}，"
        f"新增 {run['new_items']}，更新 {run['updated_items']}，"
        f"摘要 {run['summarized']}，耗时 {run['duration_ms']:.0f}ms"
    )

    logs = get("/api/ingest/logs?limit=3")["data"]
    print(f"{LINE}\n最近抓取日志")
    for lg in logs:
        print(f"   {lg['source_name']}  {lg['status']}  新增 {lg['new_count']}  错误={lg['error']}")

    print("=" * 72)
    print("全部接口正常。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as e:
        print(f"连不上后端 {BASE}：{e}")
        print("先在 backend/ 目录执行：uvicorn app.main:app --port 8000")
        raise SystemExit(1) from e
