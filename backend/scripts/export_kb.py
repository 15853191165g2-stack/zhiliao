"""把库里的通知导出成「连小理」知识库文件（**只读**，不改任何数据）。

为什么要这个脚本
----------------
知了每轮定时任务（默认 8:00 / 18:00）会抓取 → 摘要 → 推送，库里的条数是会长的。
但连小理的知识库是「上传的快照」，它不会自己去读知了的库。
所以「每天更新」这条链的最后一步，必须有东西负责生成新快照 —— 就是这个脚本。

用法
----
    python scripts/export_kb.py                          # 默认两份：全量 + 2026 近期
    python scripts/export_kb.py --min-date 2026-01-01    # 只看某天之后（近期快照）
    python scripts/export_kb.py --out-dir D:/连小理知识库  # 换输出目录
    python scripts/export_kb.py --all-only               # 只出全量一份

设计约束
--------
1. 只读：`mode=ro` 打开数据库，连事务都不开写。导出绝不能弄脏用户的已读/置顶状态。
2. 格式与首次交付的 `知了知识库-全量264条.txt` 完全一致 —— 换文件不换格式，
   智能体的角色设定一个字都不用改。
3. 抓不到正文的条目（`ai_model == 'local-no-content'`）必须显式标注，
   否则智能体会把标题当正文编内容（项目铁律 9）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "quanzhi.db"
DEFAULT_OUT = ROOT.parent / "output" / "连小理大赛"

CST = timezone(timedelta(hours=8))

IMPORTANCE_CN = {"urgent": "紧急", "high": "重要", "normal": "普通", "low": "低"}

# 正文抓不到的判定：摘要模型落成这两个值，说明正文是空的/图片/需登录
NO_CONTENT_MODELS = {"local-no-content", "local-empty"}


def beijing_day(dt_value: str | None) -> str:
    """库里存 naive UTC，导出按北京时间取到天。"""
    if not dt_value:
        return ""
    try:
        s = dt_value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d")
    except Exception:
        return str(dt_value)[:10]


def load_rows(db_path: Path) -> list[dict]:
    """只读打开库，连一条 SQL 拿全部需要的字段。"""
    if not db_path.exists():
        sys.exit(f"[错误] 找不到数据库：{db_path}\n       先跑一次抓取，或确认 --db 路径。")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT n.id, n.title, n.url, n.published_at,
                   n.category, n.importance, n.summary_lines, n.ai_model, n.ai_status,
                   COALESCE(s.name, '未知来源') AS source_name
            FROM notifications n
            LEFT JOIN sources s ON s.id = n.source_id
            ORDER BY n.published_at DESC, n.id DESC
            """
        ).fetchall()
    finally:
        con.close()

    out: list[dict] = []
    for r in rows:
        lines = r["summary_lines"]
        if isinstance(lines, str):
            try:
                lines = json.loads(lines)
            except Exception:
                lines = []
        lines = lines or []
        lines += [""] * (3 - len(lines))
        out.append(
            {
                "id": r["id"],
                "title": (r["title"] or "").strip(),
                "url": (r["url"] or "").strip(),
                "date": beijing_day(r["published_at"]),
                "source": r["source_name"],
                "category": r["category"] or "其他",
                "importance": r["importance"] or "normal",
                "model": r["ai_model"] or "",
                "status": r["ai_status"] or "",
                "s1": (lines[0] or "").strip(),
                "s2": (lines[1] or "").strip(),
                "s3": (lines[2] or "").strip(),
            }
        )
    return out


def render_kb(rows: list[dict], title: str, note: str) -> str:
    L = [f"# {title}", "", note, "", "---", ""]
    for i, r in enumerate(rows, 1):
        no_content = r["model"] in NO_CONTENT_MODELS
        tag = "（正文未能获取：原文为图片/PDF/需登录，只有标题，不可据此推测内容）" if no_content else ""
        L.append(
            f"## [{i}] {r['date']}｜{r['source']}｜{r['category']}｜"
            f"重要度 {IMPORTANCE_CN.get(r['importance'], r['importance'])}{tag}"
        )
        L.append(f"标题：{r['title']}")
        L.append(f"摘要：{r['s1']} / {r['s2']} / {r['s3']}")
        L.append(f"链接：{r['url']}")
        L.append("")
    return "\n".join(L)


def write_csv(rows: list[dict], path: Path) -> None:
    cols = [
        ("id", "id"), ("date", "发布日期"), ("source", "来源"), ("category", "分类"),
        ("importance", "重要度"), ("title", "标题"), ("s1", "摘要1_是什么"),
        ("s2", "摘要2_关键信息"), ("s3", "摘要3_该怎么办"),
        ("model", "摘要模型"), ("status", "状态"), ("url", "原文链接"),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([c[1] for c in cols])
        for r in rows:
            w.writerow([r[k] for k, _ in cols])


def main() -> None:
    ap = argparse.ArgumentParser(description="导出连小理知识库文件（只读）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 库路径")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--min-date", default=None,
                    help="只导出该日期（北京时间的 YYYY-MM-DD）之后的通知")
    ap.add_argument("--all-only", action="store_true", help="只生成全量文件")
    args = ap.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(db_path)
    if not rows:
        sys.exit("[错误] 库里没有通知，先跑一轮抓取。")

    stamped = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    note_all = (
        f"数据说明：以下通知由「知了」从大连理工大学各官方站点自动抓取整理，共 {len(rows)} 条，"
        f"按发布日期倒序（快照时间 {stamped}，北京时间）。摘要为三行：是什么 / 关键信息 / 该怎么办。\n\n"
        "使用要求：回答用户问题时，日期、电话号码、邮箱、人名必须从本文件逐字复制，不得改写或换算。"
        "标有「正文未能获取」的条目，只能说明标题，不可推测其内容。"
    )
    p_all = out_dir / "知了知识库-全量.txt"
    p_all.write_text(render_kb(rows, f"知了 · 大连理工大学校园通知库（全量 {len(rows)} 条）", note_all),
                     encoding="utf-8")
    write_csv(rows, out_dir / "知了通知-全量.csv")
    print(f"[全量] {p_all.name}  {len(rows)} 条  {p_all.stat().st_size / 1024:.0f} KB")

    if not args.all_only:
        min_date = args.min_date or f"{datetime.now(CST).year}-01-01"
        recent = [r for r in rows if r["date"] >= min_date]
        if recent:
            note_recent = (
                f"数据说明：以下为 {min_date} 之后的大连理工大学校园通知（知了 自动抓取整理），"
                f"共 {len(recent)} 条，按发布日期倒序（快照时间 {stamped}，北京时间）。"
                "摘要为三行：是什么 / 关键信息 / 该怎么办。\n\n"
                "使用要求：日期、电话号码、邮箱、人名必须逐字复制，不得改写。"
                "标有「正文未能获取」的条目，只能说明标题，不可推测内容。"
            )
            p_recent = out_dir / "知了知识库-近期.txt"
            p_recent.write_text(
                render_kb(recent, f"知了 · {min_date} 之后的校园通知（{len(recent)} 条）", note_recent),
                encoding="utf-8")
            print(f"[近期] {p_recent.name}  {len(recent)} 条  {p_recent.stat().st_size / 1024:.0f} KB")
        else:
            print(f"[近期] {min_date} 之后没有通知，跳过")

    no_content = sum(1 for r in rows if r["model"] in NO_CONTENT_MODELS)
    pending = sum(1 for r in rows if r["status"] != "done")
    print(f"[统计] 共 {len(rows)} 条 · 正文未获取 {no_content} 条 · 未完成摘要 {pending} 条")
    print(f"[提示] 把生成的 txt 重新上传到连小理知识库（覆盖旧文件），智能体即可用上最新通知。")


if __name__ == "__main__":
    main()
