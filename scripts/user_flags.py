"""验收 / 截图脚本的两类「自保」工具。

## 一、不许弄脏用户数据

背景：这是一个已经在跑、库里有真实数据的产品。截图脚本为了拍到
「详情窗口」会**注入真实鼠标点击**去打开一条通知，而桌面版打开详情时会
**自动把该条标记为已读**（`知了桌面版.py::open_detail`，行为同邮件客户端）。
于是每次拍完图，用户的通知就悄悄多出一条「已读」。

同理，演示「置顶」「已读勾选」也会改状态。

所以：**任何会改动用户状态的脚本，都必须先快照、结束后逐行还原并断言一致。**
这个模块把这件事收敛到一处 —— 之前三份拷贝各写各的，最后是
「有的脚本还原了、有的没还原」，很难发现。

## 二、不许连到「别人的后端」

脚本自己用 uvicorn 起后端时，如果 8765 **已经被一个残留的旧后端占着**，
新后端会绑定失败退出，但 `/health` 依然有响应 ——
于是测试全程在跟**旧版本代码**对话，结果全绿却毫无意义。
（实测踩过：`verify_backend.log` 里有 6 条
`[Errno 10048] error while attempting to bind`，而那一轮仍是 38/38。）

所以启动前必须用 `port_in_use()` 确认端口是空的。

用法：
    import user_flags
    if user_flags.port_in_use(8765):
        sys.exit("!! 8765 被占用，先关掉残留后端")
    before = user_flags.snapshot()
    try:
        ...  # 随便折腾（截图 / 点击 / 调接口）
    finally:
        n = user_flags.restore(before, post=my_post, log=my_log)
        assert user_flags.verify(before)
"""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "quanzhi.db"

# 三个用户状态字段 -> 对应的接口路径后缀
FLAGS: tuple[tuple[str, str], ...] = (
    ("is_read", "read"),
    ("is_pinned", "pin"),
    ("is_starred", "star"),
)


def snapshot() -> dict[int, tuple[int, int, int]]:
    """全表 (is_read, is_pinned, is_starred)。

    ⚠️ 用 `mode=ro` **只读**打开，不要用可写连接 ——
    后端（uvicorn）正持有这个库，只读连接不会参与写锁，避免互相阻塞。
    """
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, is_read, is_pinned, is_starred FROM notifications")
        return {int(i): (int(r), int(p), int(s)) for i, r, p, s in rows}
    finally:
        con.close()


def diff(before: dict, after: dict | None = None) -> list[tuple[int, str, bool, bool]]:
    """列出相对快照发生变化的 (id, 字段, 原值, 现值)。"""
    after = snapshot() if after is None else after
    out = []
    for nid, vals in before.items():
        now = after.get(nid)
        if now is None:
            continue
        for (key, _path), old, cur in zip(FLAGS, vals, now):
            if bool(cur) != bool(old):
                out.append((nid, key, bool(old), bool(cur)))
    return out


def restore(before: dict, post=None, log=print) -> int:
    """把**发生变化**的行改回快照值，返回还原的字段数。

    post(path, payload) 由调用方注入：不同脚本用的 HTTP 客户端不一样
    （有的走 urllib + 自定义 opener，有的复用了自己的封装），
    所以这里只负责「算差分 + 决定改什么」，不绑定具体客户端。
    """
    changed = diff(before)
    if post is None:
        if changed:
            log(f"  !! 有 {len(changed)} 处改动但没提供 post()，无法还原")
        return 0
    fixed = 0
    for nid, key, want, _got in changed:
        path = next(p for k, p in FLAGS if k == key)
        try:
            post(f"/notifications/{nid}/{path}", {key: want})
            fixed += 1
            log(f"  还原 id={nid} {key}={want}")
        except Exception as e:              # noqa: BLE001
            log(f"  !! 还原 id={nid} {key} 失败：{e}")
    return fixed


def verify(before: dict, log=print) -> bool:
    """还原之后再比一次，确认真的干净了。"""
    left = diff(before)
    if left:
        for nid, key, old, cur in left:
            log(f"  !! 残留 id={nid} {key}: {old} -> {cur}")
        return False
    return True


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    """端口上是否已经有人在监听。

    ⚠️ 这是**启动自己的后端之前**必须做的一步。
    不检查的话：残留旧后端占着端口 → 自己启的后端绑定失败退出 →
    但 `/health` 依然有响应 → 测试对着旧代码跑，全绿也无意义。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def guard_port(port: int, host: str = "127.0.0.1", log=print) -> bool:
    """端口被占用就打一段人话提示并返回 True（调用方应中止）。"""
    if not port_in_use(port, host):
        return False
    log(f"!! {host}:{port} 已被占用 —— 很可能有一个**残留的旧后端**还在跑。")
    log("   继续下去的话，测试会连到那个旧后端上，结果即使全绿也不可信")
    log("   （实测踩过：verify_backend.log 里有 6 条 "
        "[Errno 10048] bind，而那一轮仍然显示 38/38）。")
    log("   处理办法：先关掉残留后端。可以看 logs/ 下的日志确认，")
    log("   或直接在任务管理器结束监听该端口的 python.exe。")
    return True
