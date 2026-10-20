"""Dart 源码静态自检（没有 Flutter SDK 时的替代手段）。

检查三件事：
  1. 所有相对 import 的目标文件是否存在
  2. 括号/花括号/方括号是否配平（跳过字符串与注释）
  3. 有没有重复定义的类名（同文件内）

它不能替代 `flutter analyze`，但能挡住「路径写错」「少一个大括号」这类低级错误。

    python scripts/check_dart.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "lib"

IMPORT_RE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.MULTILINE)
CLASS_RE = re.compile(r"^\s*(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)


def strip_strings_and_comments(src: str) -> str:
    """把字符串字面量、注释、插值替换成占位符，避免误判括号。"""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        # 行注释
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        # 块注释
        if c == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # 三引号字符串
        if c == "'" and src[i : i + 3] == "'''":
            i += 3
            while i < n - 2 and src[i : i + 3] != "'''":
                i += 1
            i += 3
            continue
        if c == '"' and src[i : i + 3] == '"""':
            i += 3
            while i < n - 2 and src[i : i + 3] != '"""':
                i += 1
            i += 3
            continue
        # 普通字符串
        if c in "'\"":
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                if src[i] == "\n":  # 未闭合，说明写法有问题
                    break
                i += 1
            out.append("S")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_balance(path: Path, src: str) -> list[str]:
    clean = strip_strings_and_comments(src)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    line = 1
    problems: list[str] = []
    for ch in clean:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                problems.append(f"{path.name}:{line} 多出一个 '{ch}'")
            elif stack[-1][0] != pairs[ch]:
                problems.append(
                    f"{path.name}:{line} '{ch}' 与第 {stack[-1][1]} 行的 '{stack[-1][0]}' 不匹配"
                )
                stack.pop()
            else:
                stack.pop()
    for ch, ln in stack:
        problems.append(f"{path.name}:{ln} '{ch}' 没有闭合")
    return problems


def main() -> int:
    if not ROOT.exists():
        print(f"找不到目录：{ROOT}")
        return 1

    files = sorted(ROOT.rglob("*.dart"))
    problems: list[str] = []
    class_count = 0

    print("=" * 70)
    print(f"知了 · Dart 源码自检   （{len(files)} 个文件）")
    print("=" * 70)

    for f in files:
        src = f.read_text(encoding="utf-8")

        # 1) 相对导入
        for target in IMPORT_RE.findall(src):
            if target.startswith("dart:") or target.startswith("package:"):
                continue
            resolved = (f.parent / target).resolve()
            if not resolved.exists():
                problems.append(f"{f.name} 导入了不存在的文件：{target}")

        # 2) 括号配平
        problems.extend(check_balance(f, src))

        # 3) 类名重复
        names = CLASS_RE.findall(src)
        for name, cnt in Counter(names).items():
            if cnt > 1:
                problems.append(f"{f.name} 中重复定义了 class {name}")
        class_count += len(names)

    # 分层清点
    for d in ("core", "data", "widgets", "features"):
        n = len(list((ROOT / d).rglob("*.dart"))) if (ROOT / d).exists() else 0
        print(f"  lib/{d:<9} {n} 个文件")

    print(f"  合计 {len(files)} 个文件，{class_count} 个类")
    print("-" * 70)

    # 关键符号是否都被引用到（粗查有没有写了却忘了接线）
    all_src = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for symbol in ("AppState", "NotificationCard", "KeyInfoCard", "ApiClient", "Repository",
                   "demoNotifications", "PushService", "PushPendingItem"):
        print(f"  {symbol:<20} 出现 {all_src.count(symbol)} 次")

    # ---- 阶段 3：推送链路的接线检查 ----
    # 「写了一个类但忘了接进去」是靠符号出现次数看不出来的：
    # PushService 只要定义了就至少有 N 次出现。
    # 所以这里逐条断言「定义文件 + 必须引用它的文件」都存在该符号。
    #
    # 注意消费方要用「**它自己**该出现的符号」来判断，不要一律用类名：
    # main.dart 是通过 AppState.pollPushNow() 驱动推送的，它不该也不需要
    # 直接认识 PushService —— 一开始这里写死用 PushService 去查 main.dart，
    # 结果误报了一次「写了但没接线」。
    wiring = {
        "PushService": (
            "core/push_service.dart",
            {"core/app_state.dart": "PushService"},
        ),
        "PushPendingItem": (
            "data/models.dart",
            {"core/push_service.dart": "PushPendingItem",
             "core/app_state.dart": "PushPendingItem"},
        ),
        "fetchPushPending": (
            "data/repository.dart",
            {"core/push_service.dart": "fetchPushPending"},
        ),
        # 前台恢复时补拉：由 HomeShell 的生命周期回调触发
        "pollPushNow": (
            "core/app_state.dart",
            {"main.dart": "pollPushNow"},
        ),
    }
    print("-" * 70)
    print("  推送接线自检：")
    for symbol, (definer, users) in wiring.items():
        dpath = ROOT / definer
        if not dpath.exists():
            problems.append(f"推送符号 {symbol} 的定义文件不存在：{definer}")
            continue
        if symbol not in dpath.read_text(encoding="utf-8"):
            problems.append(f"{definer} 里没有定义 {symbol}")
        missing = []
        for user, token in users.items():
            upath = ROOT / user
            if not upath.exists() or token not in upath.read_text(encoding="utf-8"):
                missing.append(f"{user}（找 {token}）")
        if missing:
            problems.append(f"{symbol} 没有被使用：{missing}（写了但没接线？）")
        else:
            print(f"    ✓ {symbol:<18} 定义于 {definer}，被 {', '.join(users)} 使用")

    # ---- 游标安全检查 ----
    # 这两个问题都属于「静默丢通知」：不报错、日志干净、用户就是收不到。
    # 肉眼 review 很难发现，但用模式匹配能守住。背景见 push_service.dart 的注释。
    print("-" * 70)
    print("  游标安全自检：")
    push_src = (ROOT / "core/push_service.dart").read_text(encoding="utf-8")

    # ① 初始化游标必须走 sinceId: 0 —— 不传 sinceId 时后端按发布时间**倒序**返回，
    #    items.first 是最新的那条，拿它当起点会跳过所有更老的队列条目。
    #    注意匹配的是**调用形式**而不是裸的 "sinceId: 0"：注释里也会写这个词，
    #    用裸词匹配会让这条断言永远为真（变异测试实测：改坏了也不报）。
    if "fetchPushPending(sinceId: 0" not in push_src:
        problems.append(
            "push_service.dart 初始化游标时没有带 `sinceId: 0` —— "
            "不带 sinceId 后端按发布时间倒序返回，拿到的最新那条会让更老的条目被跳过")
    else:
        print("    ✓ 初始化游标走 sinceId: 0（拿到的是队列里最老的一条）")

    # ② 初始游标的**起点**必须是「队列最老那条的 id - 1」，不能直接对齐 maxId。
    #    对齐 maxId 会把队列里已存在的通知全部跳过 —— 这正是
    #    D:\QuanZhi\scripts\probe_push_cursor.py 复现出来的那个缺口。
    if "items.first.id - 1" not in push_src:
        problems.append(
            "_ensureCursor 的初始游标没有用「队列最老一条的 id - 1」当起点 —— "
            "直接对齐 maxId 会把队列里已有的通知全部跳过，且不报任何错")
    else:
        print("    ✓ 初始游标取「队列最老一条 - 1」，不是对齐 maxId")

    # ③ 推进游标不能拿 maxId 兜底 —— maxId 是全局最大 id，和「队列里有什么」不是一回事。
    if "r.nextSinceId : r.maxId" in push_src:
        problems.append(
            "push_service.dart 推进游标时用了 maxId 兜底 —— "
            "maxId 会把「还没摘要完所以暂时不在队列里」的通知一起跳过"
            "（抓取刚跑完、AI 还在串行摘要时正是这种情况），"
            "等它们摘要好了游标已经走过去了，永远不推")
    else:
        print("    ✓ 推进游标只认 next_since_id，没有 maxId 兜底")

    print("-" * 70)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("✅ 未发现问题（导入路径全部存在，括号全部配平，无重复类名）")
    print("\n注意：这只是静态自检，无法替代 flutter analyze。")
    print("真正编译验证需要本机安装 Flutter SDK 后执行：")
    print("  cd app && flutter pub get && flutter analyze && flutter run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
