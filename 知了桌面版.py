"""知了 · 电脑版桌面外壳。

做三件事：
  1. 用自带的便携 Python 把后端（FastAPI）拉起来
  2. 等 /api/health 通了，开一个原生窗口
  3. 窗口里就是「今日简报 + 信息流 + 详情 + 问答」，全部走本机后端

设计原则跟手机端一致：
  - 信息层级是「3 行摘要 > 标题 > 元信息」
  - 每条通知必须正好 3 行摘要，后端没给就本地兜底补齐
  - 后端连不上时进「离线演示模式」，明确告诉用户现在看的是演示数据，不装样子
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
# 打包后布局：<包根>  { python/  backend/  知了桌面版.py  logs/ }
#
# ⚠️ 两个真实的坑（都踩过了）：
#  1) 不能只用 __file__ 推包根：脚本被复制到别处（比如 scripts/）运行时会推错，
#     结果去 scripts\python\python.exe 找解释器 → FileNotFoundError。
#     所以这里先从 __file__ 猜，再逐个候选目录验证，最后按「有 python\python.exe
#     ／有 backend」这两个特征往上找。
#  2) 程序不能放在**带中文的脚本文件名**上被直接执行：
#     Windows 下 python.exe / pythonw.exe 执行中文文件名脚本会**静默退出**
#     （返回码 0、无任何输出），极难排查。
#     因此启动器调用的是同目录的 ASCII 名入口 `quanzhi_desktop.py`，
#     本文件仍可作为模块被导入（中文名不影响 import）。


def _resolve_root() -> Path:
    """找出包根（也就是含 python\\ 和 backend\\ 的那层目录）。"""
    candidates: list[Path] = []

    # 候选 1：__file__ 所在目录（正常情况就是包根）
    try:
        candidates.append(Path(__file__).resolve().parent)
    except NameError:  # pragma: no cover
        pass

    # 候选 2：可执行文件所在目录
    try:
        candidates.append(Path(sys.executable).resolve().parent.parent)
    except Exception:  # noqa: BLE001
        pass

    # 候选 3：当前工作目录
    try:
        candidates.append(Path.cwd())
    except Exception:  # noqa: BLE001
        pass

    # 先精确匹配
    for c in candidates:
        if (c / "python" / "python.exe").exists() and (c / "backend").is_dir():
            return c

    # 再放宽：只要含 backend 且含 python 目录
    for c in candidates:
        if (c / "backend").is_dir() and (c / "python").is_dir():
            return c

    # 最后：从候选目录逐级向上找
    for c in candidates:
        for parent in [c, *c.parents]:
            if (parent / "backend").is_dir() and (parent / "python" / "python.exe").exists():
                return parent

    # 兜底：用 __file__ 的两级父目录
    try:
        return Path(__file__).resolve().parent
    except NameError:  # pragma: no cover
        return Path.cwd()


ROOT = _resolve_root()
BACKEND_DIR = ROOT / "backend"
PYTHON_EXE = ROOT / "python" / "python.exe"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
# 端口允许用环境变量覆盖（QUANZHI_PORT），默认 8765。
# 为什么要有这个后门：本机经常已经开着一个「知了」，再跑验收脚本时
# 新后端绑不上 8765，而 /health 依然有响应 —— 于是测试对着**别人那份旧代码**
# 跑，全绿也没意义（踩过好几次）。有了它，验收可以起在空端口上，互不干扰。
PORT = int(os.environ.get("QUANZHI_PORT") or 8765)
BASE = f"http://{HOST}:{PORT}"
API = f"{BASE}/api"

# ---- 配色（对齐手机端 theme.dart）----
SEED = "#2F6BFF"
BG = "#F6F7FB"
CARD = "#FFFFFF"
TEXT = "#14171F"          # 主文字：加深（原 #1B1F2A），小字号下更实
TEXT_SUB = "#4A5163"      # 次要文字：加深（原 #5B6275），保证 11pt 下对比度足够
TEXT_FAINT = "#727A8C"    # 最淡一级：原 #8B93A7 在白底上对比度只有 3.0 左右，
                          # 小字根本看不清；换成 #727A8C 约 4.6，达到 AA 级
BORDER = "#E3E6EE"
CHIP_BG = "#F1F3F9"
STAR_COLOR = "#E8940C"    # 收藏星：原 #F5A524 在白底上偏亮，压深一档更清楚
NAV_ACTIVE = "#E8EEFF"    # 导航选中底 / 主按钮底
SUBTLE_BG = "#F2F4FA"     # hover 底 / 统计小卡底
IMPORTANCE = {"urgent": "#E5484D", "important": "#F0A020", "normal": "#727A8C"}
IMPORTANCE_LABEL = {"urgent": "紧急", "important": "重要", "normal": "一般"}

# 摘要生成方式的用户可读说法（详情页右上角那行小字）。
# 后端会把降级原因写进 ai_model，直接印出来用户看不懂，这里翻译成一句话。
_MODEL_LABEL = {
    "local": "摘要：本地规则（未配置 AI）",
    "local-fallback": "摘要：本地规则（AI 暂时不可用）",
    "local-no-content": "摘要：本地规则（未抓到正文，建议点原文）",
}

# ---- 已读 / 未读 / 置顶 的状态色 ----
# 要求是「已读以后的通知和未读要有不同的标注」，所以这里用**四重信号**叠加，
# 而不是只靠一个标签：
#   1) 卡片底色：未读纯白、已读压一档灰 → 扫一眼就有明暗节奏
#   2) 左侧 4px 状态条：未读主色蓝、已读中性灰 → 不用看字就知道
#   3) 胶囊：未读=蓝底白字，已读=灰底灰字
#   4) 字重：未读首行摘要加粗 + 标题加粗，已读取消加粗
#
# ⚠️ 刻意**没有**把已读的正文调浅：TEXT_FAINT(#727A8C) 在白底上对比度约 4.3，
#    已经在 AA(4.5) 边缘；再浅一档正文就不合格了。
#    「弱化」靠背景和字重表达，不靠降低正文对比度。
CARD_READ = "#F7F8FA"        # 已读卡片底
BORDER_READ = "#E9EBF1"      # 已读卡片描边
CHIP_BG_READ = "#ECEFF5"     # 已读卡片里的 chip 底
READ_BAR = "#D5DAE5"         # 已读左侧状态条
UNREAD_BAR = SEED            # 未读左侧状态条
PIN_BG = "#FFFAF0"           # 置顶卡片底（极淡暖色，和已读灰区分开）
PIN_COLOR = "#C9761A"        # 置顶文字 / 图标
PIN_CHIP_BG = "#FDECD2"      # 「已置顶」胶囊底

CATEGORY_ICON = {
    "教务": "🎓", "考试": "📝", "奖助学金": "🎁", "活动": "🎉",
    "讲座": "🎤", "就业": "💼", "后勤": "🔧", "其他": "📄",
}

# --------------------------------------------------------------------------- #
# 字体方案
# --------------------------------------------------------------------------- #
# 之前直接用 "Microsoft YaHei UI" 是雅黑的「UI 显示优化版」——
# 它为了小尺寸可读性用了较细的字重 + hinting，在 9/10pt 这种小字号下
# 笔画会发虚、发灰，中文尤其明显（截图里能直接看出来）。
# 现在改成两条路：
#   1) 正文字体优先用 "Microsoft YaHei"（标准雅黑，字形更实），
#      只有它不存在时才回退到 UI 版；
#   2) 字号整体上调一档，并且**小字号不再用 8pt**（8pt 中文基本是糊的）。
def _pick_font(root) -> str:
    """挑一个在 Windows 上最清晰的中文无衬线字体。"""
    try:
        families = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        return "Microsoft YaHei"
    # 按「清晰度」优先级排；HarmonyOS Sans SC / 思源黑体也常被装上，同样很锐
    for name in ("Microsoft YaHei", "微软雅黑", "HarmonyOS Sans SC",
                 "Source Han Sans SC", "Noto Sans SC", "Microsoft YaHei UI"):
        if name in families:
            return name
    return "Microsoft YaHei"


FONT = "Microsoft YaHei"          # 启动时会被 _pick_font() 覆盖成实际可用的那个
# 字号阶梯：小字号从 8 提到 9/10，正文从 10 提到 11，标题从 14 提到 15。
# 数字越大越糊的问题在 125% 缩放下尤其明显，所以整体 +1。
FS_XS = 9      # 次要元信息（原来是 8）
FS_SM = 10     # 标签、来源、时间（原来是 9）
FS_MD = 11     # 正文 —— 卡片摘要主行（原来是 10）
FS_LG = 12     # 小标题
FS_XL = 15     # 页面大标题（原来是 14）
FS_LOGO = 18   # 左上角「知了」（原来是 17）

# 行高倍数（Tk 没有行高概念，用间距模拟）
LINE_GAP = 5


# --------------------------------------------------------------------------- #
# HTTP 小工具（只用标准库，不额外引依赖）
# --------------------------------------------------------------------------- #
# ⚠️ 本地回环必须绕开系统代理：
# 如果机器上设了 HTTP_PROXY / HTTPS_PROXY（很常见，Clash 之类都会设），
# urllib 会把 http://127.0.0.1:8765 也丢给代理，代理当然不认识这个地址，
# 于是返回 502 Bad Gateway —— 表现是「后端明明在跑，App 却说连不上」。
# 这里显式指定「不要对本地地址用代理」。
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})          # 空 dict = 所有请求都直连
)


def http_json(path: str, method: str = "GET", payload: dict | None = None, timeout: float = 20.0):
    url = path if path.startswith("http") else API + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------- #
# 后端进程管理
# --------------------------------------------------------------------------- #
class Backend:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.log_path = LOG_DIR / "backend.log"
        self.ready = False
        self.last_error = ""

    def start(self) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(BACKEND_DIR)

        # 后端自己要访问校园网站，不能继承系统代理（否则抓取会走代理甚至失败）；
        # 同时把 NO_PROXY 设上，保证端口探测这类本机回环请求直连。
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
        env["no_proxy"] = "localhost,127.0.0.1,::1"

        # 优先用包内自带的解释器；万一找不到（比如被单独拷出去了），
        # 退回当前正在跑的这个解释器，保证还能启动。
        python = PYTHON_EXE if PYTHON_EXE.exists() else Path(sys.executable)

        self._logfile = open(self.log_path, "a", encoding="utf-8")
        self._logfile.write(f"\n{'='*70}\n[启动] {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._logfile.write(f"[信息] root={ROOT}\n[信息] python={python}\n[信息] backend={BACKEND_DIR}\n")
        self._logfile.flush()

        self.proc = subprocess.Popen(
            [
                str(python), "-m", "uvicorn", "app.main:app",
                "--host", HOST, "--port", str(PORT),
                "--log-level", "info",
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=self._logfile,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """轮询 /api/health，通了才算起来。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                self.last_error = f"后端进程已退出（返回码 {self.proc.returncode}）"
                return False
            try:
                data = http_json("/health", timeout=3.0)
                if data.get("code") == 0:
                    self.ready = True
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        self.last_error = f"等待后端就绪超时（{timeout:.0f}s）"
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------- #
# 本地兜底：补齐 3 行摘要
# --------------------------------------------------------------------------- #
def ensure_three_lines(item: dict) -> list[str]:
    """后端正常情况下已给 3 行。这里只兜底，绝不编造内容。"""
    lines = [str(x).strip() for x in (item.get("summary_lines") or []) if str(x).strip()]
    if len(lines) >= 3:
        return lines[:3]
    if not lines:
        text = (item.get("excerpt") or "").strip()
        if text:
            lines = _split_sentences(text)[:3]
    lines = [x for x in lines if x]
    while len(lines) < 3:
        if len(lines) == 0:
            lines.append("这条通知暂时没有可用摘要。")
        else:
            lines.append("（原文较短，未提取到更多要点，建议点开看原文）")
    return lines[:3]


def _split_sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？；\n":
            s = buf.strip()
            if s:
                out.append(s)
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


# --------------------------------------------------------------------------- #
# 桌面通知（阶段 3）
# --------------------------------------------------------------------------- #
# 设计说明 —— 先讲清楚「为什么这么做」，因为它直接关系到「能不能真的收到」：
#
#   * 不用 Windows 系统 toast。原因有三：
#       ① 需要额外依赖（win10toast / winrt），便携包里没带，装了也未必能跑；
#       ② 要注册 AppUserModelID，卸载后会留注册表残留 —— 这和「删文件夹即卸载」
#          的产品承诺冲突；
#       ③ 样式没法控制，显示不下 3 行摘要。
#     所以自己画一个右下角气泡：零依赖、可控、颜色和主界面一致。
#
#   * 它的**局限**必须说清楚：气泡是本程序自己开的窗口，
#     **App 关掉之后就收不到了**。
#     「App 没开也能收到」需要常驻服务或厂商推送通道，
#     而厂商推送要开发者账号 + 应用上架审核（个人拿不到），
#     FCM 在国内无 GMS 手机上收不到。这条限制写在 README 的「诚实的边界说明」里。
#
#   * 拉取用**游标**而不是 ack。因为 notified_at 是全局的：
#     手机 ack 了，电脑就再也拿不到这条了。游标存在本机，两端互不抢。
# 状态目录允许用环境变量覆盖（QUANZHI_STATE_DIR）。
# 用途和上面的 QUANZHI_PORT 一样：验收/截图脚本跑自己的那一份状态，
# 不去动用户真实使用中的 state\desktop.json（游标、开关、上次检查时间）。
STATE_DIR = Path(os.environ.get("QUANZHI_STATE_DIR") or (ROOT / "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DESKTOP_STATE = STATE_DIR / "desktop.json"

_STATE_DEFAULTS = {
    "notify_enabled": True,       # 是否开启新通知提醒
    "notify_interval": 30,        # 轮询间隔（秒）
    "toast_seconds": 9,           # 气泡自动消失时间（秒）
    "push_cursor": 0,             # 已见过的最大通知 id
    # 游标是否已初始化。未初始化时不许轮询 —— 起点必须由 _init_push_cursor
    # 按「队列里最老那条」算出来（见那里的说明），不能拿 0 直接上。
    "push_cursor_ready": False,
    "notified_count": 0,          # 累计弹过多少条气泡
    "last_check": "",
}


def load_desktop_state() -> dict:
    """读本地状态。文件损坏/不存在都退回默认值 —— 绝不能因为这个起不来。"""
    state = dict(_STATE_DEFAULTS)
    try:
        if DESKTOP_STATE.exists():
            raw = json.loads(DESKTOP_STATE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update({k: v for k, v in raw.items() if k in _STATE_DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    return state


def save_desktop_state(state: dict) -> None:
    try:
        DESKTOP_STATE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


class ToastManager:
    """右下角的通知气泡。多个气泡自动往上堆叠。

    ⚠️ 这里刻意**不**抢焦点：
      `overrideredirect(True)` 的无边框窗口本身不会拿到焦点，
      再配合 `attributes("-topmost", True)`，就能做到
      「飘在最上面但不打断你正在做的事」。
      用 Win32 的 SetForegroundWindow 去抢前台是行不通的
      （会被前台锁静默忽略），也没必要。
    """

    WIDTH = 400          # 气泡宽度
    MARGIN = 18          # 距屏幕右下角的边距
    GAP = 10             # 气泡之间的间距

    def __init__(self, app: "QuanZhiApp") -> None:
        self.app = app
        self.root = app.root
        self.alive: list[dict] = []

    # ---- 对外 ----
    def show(self, msg: dict) -> None:
        """弹一个气泡。`msg` 形如 /api/push/pending 返回的条目。"""
        try:
            self._show(msg)
        except Exception:  # noqa: BLE001
            # 弹通知失败绝不能让主界面崩 —— 它只是个附加提醒
            traceback.print_exc()

    def clear(self) -> None:
        for item in list(self.alive):
            self._dismiss(item)

    # ---- 内部 ----
    def _show(self, msg: dict) -> None:
        title = (msg.get("title") or "(无标题)").strip()
        source = (msg.get("source_name") or "").strip()
        imp = msg.get("importance") or "normal"
        lines = [str(x).strip() for x in (msg.get("lines") or []) if str(x).strip()]

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)            # 无标题栏
        win.configure(bg=BORDER)
        try:
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass

        outer = tk.Frame(win, bg=BORDER)
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # 左侧强调条：用重要度的颜色，扫一眼就知道是不是急事
        accent = tk.Frame(body, bg=IMPORTANCE.get(imp, TEXT_FAINT), width=4)
        accent.pack(side="left", fill="y")

        inner = tk.Frame(body, bg=CARD)
        inner.pack(side="left", fill="both", expand=True, padx=(12, 14), pady=(10, 10))

        # 第一行：应用名 + 来源 + 关闭
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="知了", bg=CARD, fg=SEED,
                 font=(FONT, FS_XS, "bold")).pack(side="left")
        tk.Label(head, text=f"· {source}" if source else "", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_XS)).pack(side="left", padx=(4, 0))
        close = tk.Label(head, text="✕", bg=CARD, fg=TEXT_FAINT,
                         font=(FONT, FS_XS), cursor="hand2", padx=4)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e, w=win: self._dismiss_by_win(w))

        # 标题
        # ⚠️ 变量名别用 title —— 上面 `title` 是**字符串**。
        #    之前这里把 Label 也赋给 title，下面 `title.bind(...)` 就变成
        #    「给字符串调 bind」，AttributeError 被外层兜底 try 吞掉，
        #    表现是「气泡一个都不弹」但不报错，很难查。
        title_lbl = tk.Label(inner, text=title, bg=CARD, fg=TEXT,
                             font=(FONT, FS_MD, "bold"),
                             wraplength=self.WIDTH - 60, justify="left", anchor="w")
        title_lbl.pack(fill="x", pady=(6, 0))

        # 3 行摘要 —— 和列表卡片保持同一套内容，用户不用点开就知道要点
        for i, text in enumerate(lines[:3]):
            tk.Label(inner, text=f"· {text}" if text else "", bg=CARD,
                     fg=TEXT if i == 0 else TEXT_SUB, font=(FONT, FS_SM),
                     wraplength=self.WIDTH - 60, justify="left", anchor="w").pack(
                fill="x", pady=(3, 0))

        # 重要性胶囊
        foot = tk.Frame(inner, bg=CARD)
        foot.pack(fill="x", pady=(8, 0))
        tk.Label(foot, text=IMPORTANCE_LABEL.get(imp, "一般"), bg=CHIP_BG,
                 fg=IMPORTANCE.get(imp, TEXT_FAINT), font=(FONT, FS_XS),
                 padx=6, pady=1).pack(side="left")
        tk.Label(foot, text="点这里看全文", bg=CARD, fg=SEED,
                 font=(FONT, FS_XS)).pack(side="left", padx=(8, 0))

        # ---- 交互 ----
        # 点气泡任意处 → 打开详情。同样用递归绑定，别漏掉嵌套最深的小标签
        # （之前卡片就是手写绑定清单漏了二层嵌套，点了没反应）。
        item = dict(msg)
        # 推送消息里的字段叫 `lines`，详情窗口用的是 `summary_lines`。
        # 这里对一下，免得点开之后摘要变成兜底文案。
        if not item.get("summary_lines") and item.get("lines"):
            item["summary_lines"] = item["lines"]
        for w in (inner, title_lbl, accent, body, outer, win):
            try:
                w.bind("<Button-1>", lambda e, it=item: self._open(it))
            except Exception:  # noqa: BLE001
                pass
        for child in list(inner.winfo_children()) + list(foot.winfo_children()):
            if child is close:
                continue
            try:
                child.bind("<Button-1>", lambda e, it=item: self._open(it))
            except Exception:  # noqa: BLE001
                pass

        # 鼠标停上去就暂停倒计时 —— 免得正在看标题就没了
        alive = {"win": win, "after": None, "msgs": msg}

        def pause(_e=None):
            job = alive.get("after")
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
                alive["after"] = None

        def resume(_e=None):
            if alive["after"] is None:
                alive["after"] = self.root.after(
                    int(self.app.state.get("toast_seconds", 9) * 1000),
                    lambda: self._dismiss(alive))

        for w in (win, body, inner, title_lbl):
            w.bind("<Enter>", pause)
            w.bind("<Leave>", resume)
        # 关闭按钮上方不要触发「暂停后重新计时」以外的逻辑，保持简单
        close.bind("<Enter>", lambda e: pause())
        close.bind("<Leave>", lambda e: resume())

        self.alive.append(alive)
        self._reflow()
        resume()
        try:
            win.deiconify()
        except Exception:  # noqa: BLE001
            pass

    def _reflow(self) -> None:
        """把所有气泡从下往上排好。

        ⚠️ 必须**倒序**遍历：最新的一条要贴在右下角，旧的往上让位
           （和 Windows 通知中心、微信的观感一致）。
           一开始写成按插入顺序摆，结果最早弹的那条霸占了贴角位置、
           最新的一条飘在最上面 —— 越新的通知离眼睛越远，反直觉。
        """
        self.alive = [a for a in self.alive if self._exists(a["win"])]
        y = self.root.winfo_screenheight() - self.MARGIN
        for alive in reversed(self.alive):
            win = alive["win"]
            try:
                win.update_idletasks()
                h = max(1, win.winfo_reqheight())
                w = self.WIDTH
                x = self.root.winfo_screenwidth() - self.MARGIN - w
                y -= h
                win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
                y -= self.GAP
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def _exists(win) -> bool:
        try:
            return bool(win.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _open(self, item: dict) -> None:
        self.app.open_detail(item)
        self._dismiss_by_id(item.get("id"))

    def _dismiss_by_id(self, nid) -> None:
        for alive in list(self.alive):
            if alive["msgs"].get("id") == nid:
                self._dismiss(alive)

    def _dismiss_by_win(self, win) -> None:
        for alive in list(self.alive):
            if alive["win"] is win:
                self._dismiss(alive)

    def _dismiss(self, alive: dict) -> None:
        job = alive.get("after")
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        try:
            alive["win"].destroy()
        except Exception:  # noqa: BLE001
            pass
        if alive in self.alive:
            self.alive.remove(alive)
        self._reflow()


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class QuanZhiApp:
    def __init__(self, root: tk.Tk, backend: Backend) -> None:
        self.root = root
        self.backend = backend
        self.online = backend.ready
        self.demo_mode = not backend.ready

        self.items: list[dict] = []
        self.page = 1
        self.page_size = 20
        self.total = 0
        self.filter_category = None
        self.filter_importance = None
        self.only_unread = False
        self.only_read = False
        self.only_pinned = False
        self.only_starred = False
        self.keyword = ""

        # 信息流页的筛选状态。切到别的页再切回来时恢复，
        # 免得「收藏页」的状态（only_starred）漏到「信息流页」上。
        self._feed_state: dict = {}

        # 当前这一屏卡片对应的视图（供已读/置顶时就地改样式，不用整页重绘）
        self._views: list[dict] = []

        # 滚轮归属：_active_canvas 是主界面当前页的 canvas；
        # _wheel_target 非空时（详情窗口打开着）滚轮只作用于它。
        self._active_canvas: tk.Widget | None = None
        self._wheel_target: tk.Widget | None = None

        self._card_widgets: list[tk.Widget] = []
        global FONT
        FONT = _pick_font(self.root)      # 先定字体，再建界面
        self._tune_fonts()

        # ---- 线程 → 界面 的安全通道（必须在任何线程启动之前就位）----
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_pump()

        # ---- 桌面通知（阶段 3）----
        # 状态存本机 state/desktop.json：通知开关、轮询间隔、拉取游标。
        # 游标放在本地是有意的 —— 见文件顶部 STATE_DIR 那段说明。
        self.state = load_desktop_state()
        self._poll_job = None
        self._poll_busy = False
        self._push_errors = 0

        self._setup_window()
        self._setup_style()
        self._build_layout()
        self.toasts = ToastManager(self)
        self.refresh_all()
        # 先把游标初始化好，再开始轮询。
        # ⚠️ 顺序不能反：游标没初始化就轮询，第一次会把库里所有历史通知一次性弹出来。
        self._init_push_cursor()

    # ------------------------------------------------------------------ #
    # 线程 → 界面 的安全通道
    # ------------------------------------------------------------------ #
    def _ui_pump(self) -> None:
        """在主线程里执行工作线程丢过来的回调。

        ⚠️ 为什么不能直接在工作线程里写 `self.root.after(...)`：
        Tk 的 `after` 内部走 `createcommand`，**不是线程安全的**。
        只要主线程还没进入 `mainloop()`（启动那一瞬间，或者用 `root.update()`
        驱动的测试脚本里），就会抛：
            RuntimeError: main thread is not in main loop

        更糟的是这个坑会「装死」：如果那句 `after` 写在 `except` 分支里
        （重试逻辑常这么写），异常会把工作线程**直接打死**，
        表现是「后端明明起来了，通知永远不来」，而且不报任何错。

        所以统一走「线程只往队列里 put，主线程用 after 泵出来执行」。
        """
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        try:
            self.root.after(40, self._ui_pump)
        except Exception:  # noqa: BLE001
            # 窗口已销毁 —— 停止泵，不要再排下一次（否则会一直报错）
            pass

    def _post(self, fn) -> None:
        """线程安全地把一个回调交给主线程执行。工作线程里请一律用它。"""
        self._ui_queue.put(fn)

    # ------------------------------------------------------------------ #
    # 桌面通知
    # ------------------------------------------------------------------ #
    def _init_push_cursor(self) -> None:
        """首次运行时给游标定一个起点：**队列里最老那条之前**。

        这样队列里已经攒下的通知会被正常弹出来。队列为空时才退化成
        「对齐到 max_id（= 什么都不弹）」，跟从前的行为一致。

        ⚠️ 为什么不能直接对齐 max_id（这是本来的写法，已修）：
        对齐 max_id 意味着 `id <= max_id` 的一律不拉，而队列里那些待推送的通知
        **正好**满足 `id <= max_id` —— 于是它们永远不弹、也不报错。
        触发场景很日常：把便携包拷到另一台电脑，或者删掉 `state/` 目录想重置一下。
        实测（scripts/probe_push_cursor.py）：队列里有 1 条待推送，
        全新 state 启动后游标被设成 159，屏幕上 0 个气泡。

        为什么不担心「一次性刷屏」（当初对齐 max_id 的动机）：
        能进队列的通知要同时满足三个条件，条数天然有限 ——
          ① 不在启动基线里（库里的历史通知在 lifespan 就被标掉了）；
          ② 落在 48 小时时间窗内（push_lookback_hours）；
          ③ 摘要已完成（ai_status == done，而 AI 是串行 + 限流的）。
        真正会糊满屏的是第①条没做的情况，那归基线管，不该让游标来兜。
        """
        if self.state.get("push_cursor_ready"):
            self._start_poll(delay=2000)
            return

        def work():
            try:
                # ⚠️ 必须带 since_id=0：不带 since_id 时后端按**发布时间倒序**返回，
                # items[0] 是最新的那条 —— 拿它当起点会跳过所有更老的队列条目，
                # 等于把同一个缺口换个地方再犯一次。
                d = http_json("/push/pending?since_id=0&limit=1", timeout=10)
                data = d.get("data") or {}
                queued = data.get("items") or []
                if queued:
                    start = max(0, int(queued[0]["id"]) - 1)
                    total = int(data.get("total") or len(queued))
                    self._post(lambda: self._cursor_ready(start, total))
                else:
                    max_id = int(data.get("max_id") or 0)
                    self._post(lambda: self._cursor_ready(max_id, 0))
            except Exception:  # noqa: BLE001
                # 后端还没起来：等一会儿再试，**不要**带着未初始化的游标去轮询。
                # 这里也走 _post —— 之前直接调 root.after，一旦主线程还没进
                # mainloop 就会在这里再抛一次，把线程打死、轮询再也起不来。
                self._post(lambda: self._start_poll(delay=5000, init_first=True))

        threading.Thread(target=work, daemon=True).start()

    def _cursor_ready(self, start_id: int, queued: int = 0) -> None:
        self.state["push_cursor"] = start_id
        self.state["push_cursor_ready"] = True
        save_desktop_state(self.state)
        # 先把轮询拉起来 —— 它才是主线；状态栏只是顺带的提示，
        # 万一它出错不能把轮询一起带走。
        self._start_poll(delay=1500)
        if queued:
            try:
                self._set_status(True, f"有 {queued} 条新通知待查看")
            except Exception:  # noqa: BLE001
                pass

    def _start_poll(self, delay: int | None = None, init_first: bool = False) -> None:
        if not self.notify_on():
            return
        if init_first and not self.state.get("push_cursor_ready"):
            self._poll_job = self.root.after(delay or 5000, self._init_push_cursor)
            return
        interval = max(5, int(self.state.get("notify_interval") or 30))
        self._poll_job = self.root.after(int(delay or interval * 1000), self._poll_push)

    def _stop_poll(self) -> None:
        if self._poll_job:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:  # noqa: BLE001
                pass
        self._poll_job = None

    def notify_on(self) -> bool:
        return bool(self.state.get("notify_enabled"))

    def _poll_push(self) -> None:
        """一轮拉取。网络动作丢到后台线程，结果回主线程处理。"""
        self._poll_job = None
        if not self.notify_on():
            return
        if not self.online:
            self._start_poll(delay=8000)      # 后端离线，稍后再试
            return
        if self._poll_busy:
            self._start_poll()
            return
        self._poll_busy = True
        cursor = int(self.state.get("push_cursor") or 0)

        def work():
            try:
                d = http_json(f"/push/pending?since_id={cursor}&limit=10", timeout=12)
                data = d.get("data") or {}
                self._post(lambda: self._on_push(data))
            except Exception:  # noqa: BLE001
                self._post(self._on_push_error)

        threading.Thread(target=work, daemon=True).start()

    def _on_push_error(self) -> None:
        self._poll_busy = False
        self._push_errors += 1
        # 连续失败就放慢，别把日志刷爆
        self._start_poll(delay=min(120000, 15000 * min(4, self._push_errors)))

    def _on_push(self, data: dict) -> None:
        self._poll_busy = False
        self._push_errors = 0
        items = data.get("items") or []
        nxt = data.get("next_since_id")
        if nxt is not None:
            try:
                if int(nxt) > int(self.state.get("push_cursor") or 0):
                    self.state["push_cursor"] = int(nxt)
            except (TypeError, ValueError):
                pass
        if items:
            self.state["notified_count"] = int(self.state.get("notified_count") or 0) + len(items)
            self.state["push_cursor_ready"] = True
            for it in items:
                self.toasts.show(it)
            newest = items[-1].get("title") or ""
            self._set_status(True, f"收到 {len(items)} 条新通知 · {newest[:22]}")
            # 首页正在看信息流时，顺手把它刷新一下，让新通知立刻出现在列表里
            if getattr(self, "current_page", None) == "feed":
                self.load_feed(reset=True)
        self.state["last_check"] = time.strftime("%H:%M:%S")
        save_desktop_state(self.state)
        if getattr(self, "current_page", None) == "settings":
            self.load_settings()
        self._start_poll()

    def toggle_notify(self, _event=None) -> None:
        self.state["notify_enabled"] = not self.notify_on()
        save_desktop_state(self.state)
        if self.notify_on():
            if self.state.get("push_cursor_ready"):
                self._start_poll(delay=500)
            else:
                self._init_push_cursor()
            self._set_status(True, "新通知提醒已开启")
        else:
            self._stop_poll()
            self.toasts.clear()
            self._set_status(True, "新通知提醒已关闭")
        self._refresh_bell()
        if getattr(self, "current_page", None) == "settings":
            self.load_settings()

    def _refresh_bell(self) -> None:
        if not hasattr(self, "bell"):
            return
        on = self.notify_on()
        self.bell.configure(text="🔔" if on else "🔕",
                            fg=SEED if on else TEXT_FAINT)

    def test_toast(self) -> None:
        """本地演示一条气泡（不碰后端、不改任何数据）。验收脚本用它截图。"""
        self.toasts.show({
            "id": None,
            "title": "这是一条演示通知",
            "lines": [
                "第 1 行：通知的关键事实。",
                "第 2 行：时间、地点这类要点。",
                "第 3 行：需要你做什么。",
            ],
            "importance": "important",
            "importance_label": "重要",
            "category": "系统",
            "source_name": "知了 · 本地演示",
            "url": None,
            "published_at": None,
        })

    # ------------------------------------------------------------------ #
    def _setup_window(self) -> None:
        r = self.root
        r.title("知了 · 只开一个窗口，看全学校所有通知")
        r.configure(bg=BG)
        w, h = 1240, 820
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(w, max(940, sw - 80))
        h = min(h, max(640, sh - 80))
        r.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")
        r.minsize(1020, 660)
        try:
            r.iconbitmap(default="")
        except Exception:  # noqa: BLE001
            pass

    def _tune_fonts(self) -> None:
        """让 Tk 用上系统实际 DPI，避免高缩放下字被拉伸糊掉。

        本机如果设了 125% / 150% 缩放（笔记本很常见），
        Tk 默认按 96 DPI 布局，再被 Windows 整体拉伸上去 —— 字就虚了。
        显式告诉 Tk 真实 DPI，它就按真实像素排版，字会明显锐利。
        """
        try:
            import ctypes
            # 让本进程感知 DPI（必须在窗口创建前/后均可，越早越好）
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:  # noqa: BLE001
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:  # noqa: BLE001
                    pass
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if dpi and dpi > 0:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:  # noqa: BLE001
            pass

        # 全局默认字体：兜住那些没显式指定 font 的控件
        try:
            for cls in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                        "TkHeadingFont", "TkTooltipFont"):
                tkfont.nametofont(cls).configure(family=FONT, size=FS_MD)
            tkfont.nametofont("TkFixedFont").configure(size=FS_SM)
        except Exception:  # noqa: BLE001
            pass

    def _setup_style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Feed.Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=34, borderwidth=0)
        st.configure("QuanZhi.TCombobox", padding=3)
        st.configure("QuanZhi.Horizontal.TProgressbar",
                     background=SEED, troughcolor=BORDER, borderwidth=0)

    # ------------------------------------------------------------------ #
    def _build_layout(self) -> None:
        r = self.root

        # ---------- 顶栏 ----------
        top = tk.Frame(r, bg=CARD, height=64)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        tk.Label(top, text="知了", bg=CARD, fg=SEED,
                 font=(FONT, FS_LOGO, "bold")).pack(side="left", padx=(20, 8))
        tk.Label(top, text="只开一个窗口，看全学校所有通知", bg=CARD, fg=TEXT_SUB,
                 font=(FONT, FS_SM)).pack(side="left", pady=(7, 0))

        self.status_dot = tk.Label(top, text="●", bg=CARD, fg="#2FA84F",
                                   font=(FONT, FS_LG))
        self.status_dot.pack(side="right", padx=(0, 4))
        self.status_label = tk.Label(top, text="准备中…", bg=CARD, fg=TEXT_SUB,
                                     font=(FONT, FS_SM))
        self.status_label.pack(side="right", padx=(0, 16))

        # 通知开关（🔔 / 🔕）。放顶栏是为了随时能一键静音，
        # 不用每次都翻到设置页。
        self.bell = tk.Label(top, text="🔔", bg=CARD, fg=SEED,
                             font=(FONT, FS_LG), cursor="hand2", padx=8)
        self.bell.pack(side="right")
        self.bell.bind("<Button-1>", self.toggle_notify)
        self._refresh_bell()

        tk.Frame(r, bg=BORDER, height=1).pack(fill="x", side="top")

        # ---------- 主体：左导航 + 右内容 ----------
        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True)

        side = tk.Frame(body, bg=CARD, width=196)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

        # 左侧导航按钮
        self.nav_buttons: dict[str, tk.Label] = {}
        nav_items = [
            ("feed", "📋  信息流"),
            ("brief", "☀️  今日简报"),
            ("starred", "⭐  我的收藏"),
            ("sources", "🔗  来源管理"),
            ("chat", "💬  AI 问答"),
            ("settings", "⚙️  设置"),
        ]
        tk.Label(side, text="导航", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_XS)).pack(anchor="w", padx=18, pady=(16, 8))
        for key, label in nav_items:
            lbl = tk.Label(side, text=label, bg=CARD, fg=TEXT_SUB, font=(FONT, FS_MD),
                           anchor="w", padx=18, pady=10, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, k=key: self.switch_page(k))
            lbl.bind("<Enter>", lambda e, w=lbl: self._nav_hover(w, True))
            lbl.bind("<Leave>", lambda e, w=lbl: self._nav_hover(w, False))
            self.nav_buttons[key] = lbl

        tk.Label(side, text="", bg=CARD).pack(expand=True, fill="both")

        # 底部：刷新 + 打包信息
        self.btn_refresh = tk.Button(
            side, text="🔄  立即刷新", bg=NAV_ACTIVE, fg=SEED, bd=0,
            font=(FONT, FS_MD, "bold"), activebackground="#DDE5FF", cursor="hand2",
            pady=7,
            command=self.do_refresh,
        )
        self.btn_refresh.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(side, text="电脑版 v0.4 · 本机运行", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_XS)).pack(side="bottom", pady=12)

        # 页面容器
        self.pages: dict[str, tk.Frame] = {}
        for key in ("feed", "brief", "starred", "sources", "chat", "settings"):
            f = tk.Frame(self.content, bg=BG)
            self.pages[key] = f

        self._build_feed_page()
        self._build_brief_page()
        self._build_starred_page()
        self._build_sources_page()
        self._build_chat_page()
        self._build_settings_page()

        self.switch_page("feed")

    def _nav_hover(self, w: tk.Label, on: bool) -> None:
        if w.cget("bg") == NAV_ACTIVE:       # 当前选中，不覆盖
            return
        w.configure(bg=SUBTLE_BG if on else CARD)

    def switch_page(self, key: str) -> None:
        # 离开信息流页时先把它自己的筛选条件存起来
        if getattr(self, "current_page", None) == "feed" and key != "feed":
            self._save_feed_filters()

        for k, f in self.pages.items():
            f.pack_forget()
            lbl = self.nav_buttons.get(k)
            if lbl:
                lbl.configure(bg=CARD, fg=TEXT_SUB)
        self.pages[key].pack(fill="both", expand=True)
        lbl = self.nav_buttons.get(key)
        if lbl:
            lbl.configure(bg=NAV_ACTIVE, fg=SEED)
        self.current_page = key

        # 渲染目标：信息流用 feed 的 canvas，收藏页用 star 的 canvas。
        # 这两个页面的卡片长得一样，但画在不同的 canvas 上。
        if key == "starred":
            self.render_canvas, self.render_holder = self.star_canvas, self.star_holder
        else:
            self.render_canvas, self.render_holder = self.canvas, self.card_holder
        self._active_canvas = self.render_canvas

        if key == "feed":
            # ★ 收藏页的筛选状态不能漏到信息流页：
            #   之前从收藏页切回来，only_starred 还是 True，信息流就只剩收藏那几条了。
            self.only_starred = False
            self._restore_feed_filters()
            self.load_feed(reset=True)
        elif key == "brief":
            self.load_brief()
        elif key == "starred":
            # 收藏页有自己的固定口径：只看收藏，日期/分类等筛选一律不继承
            self.only_starred = True
            self.only_unread = False
            self.only_read = False
            self.only_pinned = False
            self.keyword = ""
            self.filter_category = None
            self.filter_importance = None
            self.load_feed(reset=True)
        elif key == "sources":
            self.load_sources()
        elif key == "settings":
            self.load_settings()

    # ------------------------------------------------------------------ #
    # 信息流页的筛选状态存取（切页时保存 / 恢复）
    # ------------------------------------------------------------------ #
    def _save_feed_filters(self) -> None:
        self._feed_state = {
            "cat": self.cat_var.get(),
            "imp": self.imp_var.get(),
            "unread": bool(self.unread_var.get()),
            "read": bool(self.read_var.get()),
            "pinned": bool(self.pinned_var.get()),
            "keyword": self.keyword,
        }

    def _restore_feed_filters(self) -> None:
        s = self._feed_state
        self.cat_var.set(s.get("cat", "全部"))
        self.imp_var.set(s.get("imp", "全部"))
        # 不触发 command，只同步变量，避免递归调用 load_feed
        self.unread_var.set(bool(s.get("unread")))
        self.read_var.set(bool(s.get("read")))
        self.pinned_var.set(bool(s.get("pinned")))
        self.search_var.set(s.get("keyword", ""))
        self._apply_filter_vars()

    def _apply_filter_vars(self) -> None:
        """把界面上的勾选状态读进 self.only_* 这几个查询标记。"""
        cat, imp = self.cat_var.get(), self.imp_var.get()
        self.filter_category = None if cat == "全部" else cat
        rev = {"紧急": "urgent", "重要": "important", "一般": "normal"}
        self.filter_importance = None if imp == "全部" else rev.get(imp)
        # 未读 / 已读 互斥：谁被勾上就以谁为准（都勾上时按未读走）
        if self.unread_var.get() and self.read_var.get():
            self.read_var.set(False)
        self.only_unread = bool(self.unread_var.get())
        self.only_read = bool(self.read_var.get())
        self.only_pinned = bool(self.pinned_var.get())

    # ================================================================== #
    # 信息流页
    # ================================================================== #
    def _build_feed_page(self) -> None:
        p = self.pages["feed"]

        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=18, pady=(16, 10))

        tk.Label(bar, text="最新通知", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(side="left")

        self.feed_count = tk.Label(bar, text="", bg=BG, fg=TEXT_SUB, font=(FONT, FS_SM))
        self.feed_count.pack(side="left", padx=(10, 0), pady=(5, 0))

        # 搜索框
        search_box = tk.Frame(bar, bg=CARD, highlightthickness=1,
                              highlightbackground=BORDER)
        search_box.pack(side="right")
        tk.Label(search_box, text="🔍", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_MD)).pack(side="left", padx=(9, 3), pady=5)
        self.search_var = tk.StringVar()
        ent = tk.Entry(search_box, textvariable=self.search_var, bd=0, bg=CARD,
                       fg=TEXT, font=(FONT, FS_MD), width=22,
                       insertbackground=TEXT)
        ent.pack(side="left", padx=(0, 9), pady=5)
        ent.bind("<Return>", lambda e: self.on_search())
        self.search_entry = ent

        # 筛选行
        filt = tk.Frame(p, bg=BG)
        filt.pack(fill="x", padx=18, pady=(0, 10))

        tk.Label(filt, text="分类", bg=BG, fg=TEXT_SUB,
                 font=(FONT, FS_SM)).pack(side="left", padx=(0, 6))
        self.cat_var = tk.StringVar(value="全部")
        cats = ["全部", "教务", "考试", "奖助学金", "活动", "讲座", "就业", "后勤", "其他"]
        cb = ttk.Combobox(filt, textvariable=self.cat_var, values=cats,
                          state="readonly", width=9, font=(FONT, FS_SM),
                          style="QuanZhi.TCombobox")
        cb.pack(side="left", padx=(0, 16))
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())

        tk.Label(filt, text="重要度", bg=BG, fg=TEXT_SUB,
                 font=(FONT, FS_SM)).pack(side="left", padx=(0, 6))
        self.imp_var = tk.StringVar(value="全部")
        cb2 = ttk.Combobox(filt, textvariable=self.imp_var,
                           values=["全部", "紧急", "重要", "一般"],
                           state="readonly", width=7, font=(FONT, FS_SM),
                           style="QuanZhi.TCombobox")
        cb2.pack(side="left", padx=(0, 16))
        cb2.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())

        # 未读 / 已读 / 置顶 三个勾选。未读和已读互斥 ——
        # 同时勾上只会得到空列表，不如点一个自动取消另一个。
        self.unread_var = tk.BooleanVar(value=False)
        self.read_var = tk.BooleanVar(value=False)
        self.pinned_var = tk.BooleanVar(value=False)
        for text, var in (("只看未读", self.unread_var), ("只看已读", self.read_var),
                          ("只看置顶", self.pinned_var)):
            tk.Checkbutton(filt, text=text, variable=var, bg=BG,
                           fg=TEXT_SUB, font=(FONT, FS_SM), activebackground=BG,
                           selectcolor=CARD, bd=0, highlightthickness=0,
                           cursor="hand2",
                           command=self.on_filter_change).pack(side="left", padx=(0, 6))

        tk.Button(filt, text="清空筛选", bg=BG, fg=SEED, bd=0, font=(FONT, FS_SM),
                  activebackground=BG, cursor="hand2",
                  command=self.clear_filters).pack(side="right")

        # 卡片区（可滚动）
        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True, padx=0, pady=0)

        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.card_holder = tk.Frame(self.canvas, bg=BG)
        self.card_window = self.canvas.create_window((0, 0), window=self.card_holder,
                                                     anchor="nw")
        self.card_holder.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self.card_window, width=e.width),
        )

        # ★ 渲染目标。信息流和收藏页各自有自己的 canvas/holder，
        #   渲染前必须知道「往哪画」。切页时由 switch_page 更新这一对。
        #   （之前 _render_cards 写死了 self.card_holder，
        #    结果收藏页的 star_holder 永远空着 —— 收藏页是白的。）
        self.render_canvas = self.canvas
        self.render_holder = self.card_holder

        # 鼠标滚轮
        self._bind_wheel(self.canvas)

        # 分页条
        pager = tk.Frame(p, bg=BG)
        pager.pack(fill="x", padx=18, pady=(8, 14))
        self.btn_prev = tk.Button(pager, text="← 上一页", bg=CARD, fg=TEXT_SUB, bd=0,
                                  font=(FONT, FS_SM), padx=14, pady=6, cursor="hand2",
                                  command=lambda: self.goto_page(self.page - 1))
        self.btn_prev.pack(side="left")
        self.page_label = tk.Label(pager, text="", bg=BG, fg=TEXT_SUB, font=(FONT, FS_SM))
        self.page_label.pack(side="left", padx=14)
        self.btn_next = tk.Button(pager, text="下一页 →", bg=CARD, fg=TEXT_SUB, bd=0,
                                  font=(FONT, FS_SM), padx=14, pady=6, cursor="hand2",
                                  command=lambda: self.goto_page(self.page + 1))
        self.btn_next.pack(side="left")

        tk.Button(pager, text="✓  全部标为已读", bg=CARD, fg=TEXT_SUB, bd=0,
                  font=(FONT, FS_SM), padx=14, pady=6, cursor="hand2",
                  activebackground=SUBTLE_BG,
                  command=self.mark_all_read).pack(side="right")

    # 当前「拥有滚轮」的 canvas：只有它响应滚轮。
    # ⚠️ 之前这里用 bind_all("<MouseWheel>")，那是**全局**绑定，而且：
    #   1) 后绑的会盖掉先绑的 —— 打开详情后滚轮全被详情抢走；
    #   2) 详情关掉也不解绑 —— 滚轮继续往已销毁的 canvas 上滚，主界面就废了。
    #   结果就是「详情里滚不动，正文看不全」。改成绑定到 root，
    #   由 _active_canvas 决定这次滚谁。
    def _bind_wheel(self, widget: tk.Widget) -> None:
        """把滚轮事件绑定到 root，只作用于当前活跃的 canvas。"""
        self._active_canvas = widget

        def on_wheel(event):
            target = getattr(self, "_wheel_target", None) or self._active_canvas
            if target is None:
                return
            try:
                if not target.winfo_exists():
                    # 目标已销毁（详情窗口被关掉了）→ 交还给主界面
                    self._wheel_target = None
                    target = self._active_canvas
                    if target is None or not target.winfo_exists():
                        return
                delta = -1 * (event.delta // 120) if event.delta else 0
                target.yview_scroll(delta, "units")
            except Exception:               # noqa: BLE001
                pass

        # 绑定一次即可（重复调用不会叠加，因为用的是同一个 tag）
        self.root.bind_all("<MouseWheel>", on_wheel, add="+")

    # ------------------------------------------------------------------ #
    def on_search(self) -> None:
        self.keyword = self.search_var.get().strip()
        self.load_feed(reset=True)

    def on_filter_change(self) -> None:
        self._apply_filter_vars()
        self.load_feed(reset=True)

    def clear_filters(self) -> None:
        self.cat_var.set("全部")
        self.imp_var.set("全部")
        self.unread_var.set(False)
        self.read_var.set(False)
        self.pinned_var.set(False)
        self.search_var.set("")
        self.keyword = ""
        self.only_starred = (getattr(self, "current_page", "feed") == "starred")
        self.load_feed(reset=True)

    def goto_page(self, page: int) -> None:
        if page < 1:
            return
        maxpage = max(1, (self.total + self.page_size - 1) // self.page_size)
        if page > maxpage:
            return
        self.page = page
        self.load_feed()

    def load_feed(self, reset: bool = False, preserve_scroll: bool = False) -> None:
        if reset:
            self.page = 1
        params = {"page": self.page, "page_size": self.page_size}
        if self.filter_category:
            params["category"] = self.filter_category
        if self.filter_importance:
            params["importance"] = self.filter_importance
        if self.only_unread:
            params["unread_only"] = "true"
        if self.only_read:
            params["read_only"] = "true"
        if self.only_pinned:
            params["pinned_only"] = "true"
        if self.only_starred:
            params["starred_only"] = "true"
        if self.keyword:
            params["q"] = self.keyword

        # 重绘前记下滚动位置，重绘后还回去 —— 勾了「已读」页面不该跳回顶部
        offset = 0.0
        if preserve_scroll:
            try:
                offset = self.render_canvas.yview()[0]
            except Exception:               # noqa: BLE001
                offset = 0.0

        try:
            data = http_json("/notifications?" + urllib.parse.urlencode(params))
            page = data.get("data") or {}
            self.items = page.get("items") or []
            self.total = int(page.get("total") or 0)
            self.online = True
            self._set_status(True, f"后端在线 · 共 {self.total} 条通知")
        except Exception as e:  # noqa: BLE001
            self.items = []
            self.total = 0
            self.online = False
            self._set_status(False, f"后端未响应（{type(e).__name__}）")
            self._render_message(
                "后端连接失败",
                f"没能连上本机后端。\n\n错误：{e}\n\n"
                "可以点左侧「设置」看后端日志，或用「立即刷新」重试。",
            )
            return

        self._render_cards(keep_scroll=offset)

    def _render_cards(self, keep_scroll: float = 0.0) -> None:
        for w in self.render_holder.winfo_children():
            w.destroy()
        self._views.clear()

        self.feed_count.configure(text=f"共 {self.total} 条")
        maxpage = max(1, (self.total + self.page_size - 1) // self.page_size)
        self.page_label.configure(text=f"第 {self.page} / {maxpage} 页")
        self.btn_prev.configure(state="normal" if self.page > 1 else "disabled")
        self.btn_next.configure(state="normal" if self.page < maxpage else "disabled")

        if not self.items:
            self._render_message("没有符合条件的通知",
                                 "换个筛选条件，或者点「清空筛选」看看全部。")
            return

        for item in self.items:
            self._build_card(item)

        if keep_scroll:
            self.render_canvas.yview_moveto(keep_scroll)
        else:
            self.render_canvas.yview_moveto(0)

    def _render_message(self, title: str, body: str) -> None:
        for w in self.render_holder.winfo_children():
            w.destroy()
        self._views.clear()
        box = tk.Frame(self.render_holder, bg=CARD)
        box.pack(fill="x", padx=12, pady=18)
        tk.Label(box, text=title, bg=CARD, fg=TEXT,
                 font=(FONT, FS_LG, "bold")).pack(anchor="w", padx=18, pady=(18, 6))
        tk.Label(box, text=body, bg=CARD, fg=TEXT_SUB, font=(FONT, FS_MD),
                 justify="left", wraplength=820).pack(anchor="w", padx=18, pady=(0, 18))

    def _build_card(self, item: dict) -> None:
        """画一张通知卡片。

        卡片上有两个用户可点的「状态开关」：
          - 右上角「置顶 / 已置顶」：把这条顶到列表最前
          - 右下角「已读」勾选框：勾上=已读，取消=未读
        其余任何位置点一下都是「打开详情」。

        ⚠️ 这两个开关必须从「整卡点击」里排除（见下面 skip 集合），
           否则点勾选框会同时触发「打开详情」+「切换已读」，行为就乱了。
        """
        imp = item.get("importance") or "normal"
        imp_color = IMPORTANCE.get(imp, IMPORTANCE["normal"])

        card = tk.Frame(self.render_holder, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, cursor="hand2")
        card.pack(fill="x", padx=14, pady=6)

        # 左侧 4px 状态条：未读蓝 / 已读灰。颜色在 _skin() 里按状态设。
        accent = tk.Frame(card, width=4, bg=UNREAD_BAR, bd=0, highlightthickness=0)
        accent.pack(side="left", fill="y")

        inner = tk.Frame(card, bg=CARD)
        inner.pack(side="left", fill="both", expand=True, padx=(14, 18), pady=15)

        # ---- 第一行：重要度 · 分类 · 已读未读 · 时间 · 来源 · 置顶 · 收藏 ----
        head = tk.Frame(inner, bg=CARD)
        head.pack(fill="x")

        dot = tk.Label(head, text="●", bg=CARD, fg=imp_color, font=(FONT, FS_SM))
        dot.pack(side="left", padx=(0, 5))
        imp_lbl = tk.Label(head, text=IMPORTANCE_LABEL.get(imp, "一般"), bg=CARD,
                           fg=imp_color, font=(FONT, FS_SM, "bold"))
        imp_lbl.pack(side="left", padx=(0, 10))

        cat = item.get("category") or "其他"
        cat_lbl = tk.Label(head, text=f"{CATEGORY_ICON.get(cat,'📄')} {cat}", bg=CHIP_BG,
                           fg=TEXT_SUB, font=(FONT, FS_XS), padx=7, pady=2)
        cat_lbl.pack(side="left")

        # 已读 / 未读 胶囊：两种状态都显示，只是换样式（蓝底白字 → 灰底灰字）
        read_pill = tk.Label(head, text="", bg=CHIP_BG, fg=TEXT_SUB,
                             font=(FONT, FS_XS), padx=7, pady=2)
        read_pill.pack(side="left", padx=(7, 0))

        # 右侧从右往左：收藏星 → 置顶 → 时间 → 来源
        star_lbl = tk.Label(head, text="☆", bg=CARD, fg=TEXT_FAINT,
                            font=(FONT, FS_LG), cursor="hand2")
        star_lbl.pack(side="right", padx=(10, 0))

        pin_lbl = tk.Label(head, text="置顶", bg=CHIP_BG, fg=TEXT_FAINT,
                           font=(FONT, FS_XS), padx=7, pady=2, cursor="hand2")
        pin_lbl.pack(side="right", padx=(8, 0))

        time_lbl = tk.Label(head, text=self._rel_time(item.get("published_at")), bg=CARD,
                            fg=TEXT_SUB, font=(FONT, FS_XS))
        time_lbl.pack(side="right")

        src_lbl = tk.Label(head, text=item.get("source_name") or "未知来源", bg=CARD,
                           fg=TEXT_FAINT, font=(FONT, FS_SM))
        src_lbl.pack(side="right", padx=(0, 12))

        # ---- 第二行：3 行摘要（核心）----
        lines = ensure_three_lines(item)
        body = tk.Frame(inner, bg=CARD)
        body.pack(fill="x", pady=(11, 6))
        sum_labels: list[tk.Label] = []
        for i, line in enumerate(lines):
            lbl = tk.Label(
                body, text=("• " if i == 0 else "  ") + line,
                bg=CARD, fg=TEXT if i == 0 else TEXT_SUB,
                font=(FONT, FS_MD),
                justify="left", anchor="w", wraplength=860,
            )
            lbl.pack(fill="x", anchor="w",
                     pady=(0, LINE_GAP if i < len(lines) - 1 else 0))
            sum_labels.append(lbl)

        # ---- 第三行：标题 ----
        foot = tk.Frame(inner, bg=CARD)
        foot.pack(fill="x")
        title_lbl = tk.Label(foot, text="📄 " + (item.get("title") or "(无标题)"),
                             bg=CARD, fg=TEXT, font=(FONT, FS_SM),
                             anchor="w", wraplength=860, justify="left")
        title_lbl.pack(fill="x", anchor="w")

        # ---- 第四行：关键信息 chips（左） + 已读勾选框（右）----
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(fill="x", pady=(9, 0))

        chip_lbls: list[tk.Label] = []
        chips = self._key_info_chips(item.get("key_info") or {})
        if chips:
            chip_row = tk.Frame(bottom, bg=CARD)
            chip_row.pack(side="left")
            for text, color in chips:
                lbl = tk.Label(chip_row, text=text, bg=CHIP_BG, fg=color,
                               font=(FONT, FS_XS), padx=8, pady=3)
                lbl.pack(side="left", padx=(0, 7))
                chip_lbls.append(lbl)

        chk_var = tk.BooleanVar(value=bool(item.get("is_read")))
        chk = tk.Checkbutton(bottom, text="已读", variable=chk_var, bg=CARD,
                             fg=TEXT_SUB, font=(FONT, FS_XS), bd=0,
                             highlightthickness=0, activebackground=CARD,
                             selectcolor=CARD, cursor="hand2")
        chk.pack(side="right", padx=(10, 0))

        # ---- 视图记录：状态一变就靠它就地改样式，不用整页重绘 ----
        view = {
            "item": item,
            "card": card, "accent": accent, "inner": inner,
            "head": head, "body": body, "foot": foot, "bottom": bottom,
            "dot": dot, "imp_lbl": imp_lbl, "cat_lbl": cat_lbl,
            "read_pill": read_pill, "star_lbl": star_lbl, "pin_lbl": pin_lbl,
            "time_lbl": time_lbl, "src_lbl": src_lbl,
            "sum_labels": sum_labels, "title_lbl": title_lbl,
            "chip_lbls": chip_lbls, "chk": chk, "chk_var": chk_var,
            "imp_color": imp_color,
        }
        self._views.append(view)

        # ---- 交互 ----
        # ⚠️ 这里原来是一个「手写清单」式的绑定循环，只覆盖了
        #    body / foot / head 的直接子控件 —— 于是 chip_row（关键信息标签行）
        #    及其内部的 Label 全都漏掉了。用户点「📍 地点的标签」那一块毫无反应。
        #    改成**递归**绑定：卡片之下所有后代控件统统绑上，
        #    只有三个开关（收藏 / 置顶 / 已读）排除在外。
        def open_detail(_e=None):
            self.open_detail(item)

        def bind_all_click(widget) -> None:
            if widget in (star_lbl, pin_lbl, chk):
                return                      # 这三个自己处理点击
            try:
                widget.bind("<Button-1>", open_detail)
            except Exception:               # noqa: BLE001
                pass
            for child in widget.winfo_children():
                bind_all_click(child)

        bind_all_click(card)
        star_lbl.bind("<Button-1>", lambda e, v=view: self.toggle_star(v))
        pin_lbl.bind("<Button-1>", lambda e, v=view: self.toggle_pin(v))
        chk.configure(command=lambda v=view: self.toggle_read(v))

        # 悬停高亮描边。移开时要按**当前**已读状态还回去，不能一律还成 BORDER，
        # 否则划过一次的已读卡片描边会变得比别的已读卡片深。
        card.bind("<Enter>", lambda e, v=view: v["card"].configure(
            highlightbackground="#C9D4F5"))
        card.bind("<Leave>", lambda e, v=view: v["card"].configure(
            highlightbackground=BORDER_READ if v["item"].get("is_read") else BORDER))

        self._skin(view)

    def _skin(self, view: dict) -> None:
        """按「已读 / 未读 / 置顶」把一张卡片的样式刷一遍。

        单独抽出来是为了**就地改样式**：勾了「已读」只刷这一张卡，
        不重新请求、不重绘整页、滚动位置也不动。
        """
        item = view["item"]
        is_read = bool(item.get("is_read"))
        is_pinned = bool(item.get("is_pinned"))

        # 底色：置顶（淡暖）> 已读（浅灰）> 未读（纯白）
        bg = PIN_BG if is_pinned else (CARD_READ if is_read else CARD)
        chip_bg = CHIP_BG_READ if is_read else CHIP_BG

        view["card"].configure(bg=bg,
                               highlightbackground=BORDER_READ if is_read else BORDER)
        for key in ("inner", "head", "body", "foot", "bottom"):
            view[key].configure(bg=bg)
        for key in ("dot", "imp_lbl", "time_lbl", "src_lbl", "star_lbl"):
            view[key].configure(bg=bg)

        # 左侧状态条：这是最不用读字就能分辨的一条信号
        view["accent"].configure(bg=READ_BAR if is_read else UNREAD_BAR)

        # 已读 / 未读 胶囊
        if is_read:
            view["read_pill"].configure(text="已读", bg=CHIP_BG_READ, fg=TEXT_FAINT,
                                        font=(FONT, FS_XS))
        else:
            view["read_pill"].configure(text="未读", bg=SEED, fg="#FFFFFF",
                                        font=(FONT, FS_XS, "bold"))

        # chips
        view["cat_lbl"].configure(bg=chip_bg)
        for lbl in view["chip_lbls"]:
            lbl.configure(bg=chip_bg)

        # 置顶开关
        view["pin_lbl"].configure(
            text="已置顶" if is_pinned else "置顶",
            bg=PIN_CHIP_BG if is_pinned else chip_bg,
            fg=PIN_COLOR if is_pinned else TEXT_FAINT,
            font=(FONT, FS_XS, "bold") if is_pinned else (FONT, FS_XS),
        )

        # 收藏星
        view["star_lbl"].configure(
            text="⭐" if item.get("is_starred") else "☆",
            fg=STAR_COLOR if item.get("is_starred") else TEXT_FAINT,
        )

        # 摘要：未读时首行加粗；读过的不用再抢注意力
        for i, lbl in enumerate(view["sum_labels"]):
            lbl.configure(bg=bg, fg=TEXT if i == 0 else TEXT_SUB,
                          font=(FONT, FS_MD, "bold") if (i == 0 and not is_read)
                          else (FONT, FS_MD))

        # 标题：未读加粗
        view["title_lbl"].configure(
            bg=bg, fg=TEXT_SUB if is_read else TEXT,
            font=(FONT, FS_SM) if is_read else (FONT, FS_SM, "bold"),
        )

        # 已读勾选框
        if view["chk_var"].get() != is_read:
            view["chk_var"].set(is_read)
        view["chk"].configure(bg=bg, activebackground=bg, selectcolor=bg,
                              fg=TEXT_FAINT if is_read else TEXT_SUB)

    # ------------------------------------------------------------------ #
    def _item_visible_under_filters(self, item: dict) -> bool:
        """当前筛选条件下这条还该不该留在列表里。"""
        if self.only_unread and item.get("is_read"):
            return False
        if self.only_read and not item.get("is_read"):
            return False
        if self.only_pinned and not item.get("is_pinned"):
            return False
        if self.only_starred and not item.get("is_starred"):
            return False
        return True

    @staticmethod
    def _key_info_chips(ki: dict) -> list[tuple[str, str]]:
        chips: list[tuple[str, str]] = []
        if ki.get("deadline"):
            chips.append((f"⏰ 截止 {ki['deadline']}", "#E5484D"))
        if ki.get("location"):
            chips.append((f"📍 {ki['location']}", TEXT_SUB))
        if ki.get("audience"):
            chips.append((f"👥 {str(ki['audience'])[:22]}", TEXT_SUB))
        mats = ki.get("materials") or []
        if mats:
            chips.append(("📎 需交 " + "、".join(str(m) for m in mats[:3]), "#7A5AF8"))
        if ki.get("contact"):
            chips.append((f"☎ {str(ki['contact'])[:26]}", TEXT_SUB))
        return chips[:4]

    @staticmethod
    def _rel_time(iso: str | None) -> str:
        if not iso:
            return "--"
        try:
            from datetime import datetime, timezone
            s = iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(dt.tzinfo)
            delta = now - dt
            sec = delta.total_seconds()
            if sec < 0:
                return dt.strftime("%m-%d %H:%M")
            if sec < 3600:
                return f"{int(sec//60)} 分钟前"
            if sec < 86400:
                return f"{int(sec//3600)} 小时前"
            if sec < 86400 * 7:
                return f"{int(sec//86400)} 天前"
            return dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return str(iso)[:16]

    def toggle_star(self, view: dict) -> None:
        item = view["item"]
        nid = item.get("id")
        if nid is None:
            return
        try:
            data = http_json(f"/notifications/{nid}/star", method="POST", payload={})
            item["is_starred"] = bool((data.get("data") or {}).get("is_starred"))
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("收藏失败", str(e), parent=self.root)
            return
        # 在「我的收藏」页取消收藏后这条就不该还在 → 整页重取
        if self.only_starred and not item["is_starred"]:
            self.load_feed(preserve_scroll=True)
            self._set_status(True, "已取消收藏")
            return
        self._skin(view)
        self._set_status(True, "已加入收藏" if item["is_starred"] else "已取消收藏")

    def toggle_pin(self, view: dict) -> None:
        """置顶 / 取消置顶。

        置顶会改变排序（置顶的浮到最前），所以不能只刷这一张卡片，
        必须按后端的新顺序整页重取 —— 排序规则只写在后端一处，
        前端不复制一份，免得两边慢慢跑偏。
        """
        item = view["item"]
        nid = item.get("id")
        if nid is None:
            return
        try:
            data = http_json(f"/notifications/{nid}/pin", method="POST", payload={})
            item["is_pinned"] = bool((data.get("data") or {}).get("is_pinned"))
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("置顶失败", str(e), parent=self.root)
            return

        pinned = bool(item["is_pinned"])
        # 置顶 → 跳回顶部，让你直接看到它已经在最前面；
        # 取消置顶 → 停在原地，别把人甩到列表头上。
        self.load_feed(preserve_scroll=not pinned)
        self._set_status(True, "已置顶，排到列表最前" if pinned else "已取消置顶")

    def toggle_read(self, view: dict) -> None:
        """已读勾选框。勾上=已读，取消=未读（两个方向都要能用）。"""
        item = view["item"]
        nid = item.get("id")
        if nid is None:
            return
        want = bool(view["chk_var"].get())        # 用户点完之后期望的状态
        try:
            data = http_json(f"/notifications/{nid}/read", method="POST",
                             payload={"is_read": want})
            item["is_read"] = bool((data.get("data") or {}).get("is_read", want))
        except Exception as e:  # noqa: BLE001
            view["chk_var"].set(not want)         # 失败回滚勾选，别骗用户
            messagebox.showwarning("标记失败", str(e), parent=self.root)
            return

        # 例如在「只看未读」里把一条标成已读 —— 它就不该再留在这一屏了
        if not self._item_visible_under_filters(item):
            self.load_feed(preserve_scroll=True)
        else:
            self._skin(view)
        self._set_status(True, "已标记为已读" if item["is_read"] else "已标记为未读")

    def mark_all_read(self) -> None:
        """把全部未读标为已读。

        ⚠️ 这是**全局**操作，不受当前筛选/分页影响 —— 确认框里要写清楚，
        否则用户以为只标了当前这一页。
        """
        try:
            info = (http_json("/health") or {}).get("data") or {}
            unread = int((info.get("counts") or {}).get("unread") or 0)
        except Exception:  # noqa: BLE001
            unread = -1

        if unread == 0:
            messagebox.showinfo("全部标为已读", "现在没有未读通知。", parent=self.root)
            return
        tip = f"共 {unread} 条未读。" if unread > 0 else "未读数暂时取不到。"
        if not messagebox.askyesno(
            "全部标为已读",
            f"{tip}\n\n会把**所有**未读通知标为已读（不受当前筛选和分页影响），确定？",
            parent=self.root,
        ):
            return
        try:
            data = http_json("/notifications/read-all", method="POST",
                             payload={"is_read": True})
            n = int((data.get("data") or {}).get("updated") or 0)
            self.load_feed(preserve_scroll=True)
            self._set_status(True, f"已把 {n} 条标为已读")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("批量标记失败", str(e), parent=self.root)

    # ------------------------------------------------------------------ #
    def open_detail(self, item: dict) -> None:
        nid = item.get("id")
        detail = item
        if nid is not None and self.online:
            try:
                data = http_json(f"/notifications/{nid}")
                detail = data.get("data") or item
                if nid and not detail.get("is_read"):
                    try:
                        http_json(f"/notifications/{nid}/read", method="POST", payload={})
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

        win = tk.Toplevel(self.root)
        win.title((detail.get("title") or "通知详情")[:60])
        win.configure(bg=BG)
        w, h = 920, 760
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = min(w, max(760, sw - 100))
        h = min(h, max(560, sh - 100))
        win.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")
        win.transient(self.root)

        # 标题区
        head = tk.Frame(win, bg=CARD)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=CARD)
        inner.pack(fill="x", padx=22, pady=16)

        tk.Label(inner, text=detail.get("title") or "(无标题)", bg=CARD, fg=TEXT,
                 font=(FONT, FS_XL, "bold"), wraplength=850, justify="left",
                 anchor="w").pack(fill="x")

        meta = tk.Frame(inner, bg=CARD)
        meta.pack(fill="x", pady=(9, 0))
        imp = detail.get("importance") or "normal"
        tk.Label(meta, text=IMPORTANCE_LABEL.get(imp, "一般"), bg=CARD,
                 fg=IMPORTANCE.get(imp, TEXT_FAINT),
                 font=(FONT, FS_SM, "bold")).pack(side="left", padx=(0, 10))
        tk.Label(meta, text=detail.get("source_name") or "", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_SM)).pack(side="left", padx=(0, 10))
        tk.Label(meta, text=(detail.get("published_at") or "")[:16].replace("T", " "),
                 bg=CARD, fg=TEXT_FAINT, font=(FONT, FS_SM)).pack(side="left")
        if detail.get("ai_model"):
            # 直接把 model 名印出来对用户是黑话，尤其是这几种降级：
            #   local                 —— 没配 AI Key，纯规则
            #   local-fallback        —— AI 调不通（限流/断网），降级了
            #   local-no-content      —— 正文没抓到（公众号链接被 robots 拦），
            #                            所以摘要只是「原文信息较少，建议点开看全文」
            # 最后一种最需要解释：用户会觉得「为什么摘要这么敷衍」。
            tk.Label(meta, text=_MODEL_LABEL.get(
                detail["ai_model"], str(detail["ai_model"])),
                bg=CARD, fg="#B9BFCC",
                font=(FONT, FS_XS)).pack(side="right")

        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        # 滚动区
        wrap = tk.Frame(win, bg=BG)
        wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)

        holder = tk.Frame(cv, bg=BG)
        cw = cv.create_window((0, 0), window=holder, anchor="nw")
        holder.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(cw, width=e.width))

        # 滚轮归属：详情打开期间，滚轮只滚详情的内容区，不滚背后的信息流。
        self._wheel_target = cv

        def on_detail_close():
            # 交还滚轮给主界面
            self._wheel_target = None
            try:
                win.destroy()
            except Exception:               # noqa: BLE001
                pass

        win.protocol("WM_DELETE_WINDOW", on_detail_close)
        # 兜底：不管窗口是被按钮、协议还是别的什么方式销毁的，
        # 销毁时一律交还滚轮 —— 否则 _wheel_target 会指向已销毁的 canvas，
        # 主界面的滚轮就彻底失灵了。
        win.bind("<Destroy>", lambda e: setattr(self, "_wheel_target", None)
                 if e.widget is win else None, add="+")

        # 3 行摘要
        sec = tk.Frame(holder, bg=CARD)
        sec.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(sec, text="三行摘要", bg=CARD, fg=SEED,
                 font=(FONT, FS_MD, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        for line in ensure_three_lines(detail):
            tk.Label(sec, text="• " + line, bg=CARD, fg=TEXT, font=(FONT, FS_MD),
                     wraplength=820, justify="left", anchor="w").pack(
                fill="x", padx=16, anchor="w", pady=2)
        tk.Label(sec, text="", bg=CARD, height=1).pack()

        # 关键信息
        ki = detail.get("key_info") or {}
        if any(ki.values()):
            kis = tk.Frame(holder, bg=CARD)
            kis.pack(fill="x", padx=14, pady=6)
            tk.Label(kis, text="关键信息", bg=CARD, fg=SEED,
                     font=(FONT, FS_MD, "bold")).pack(anchor="w", padx=16, pady=(12, 8))
            rows = [
                ("截止时间", ki.get("deadline")),
                ("适用对象", ki.get("audience")),
                ("地点", ki.get("location")),
                ("需交材料", "、".join(str(m) for m in (ki.get("materials") or [])) or None),
                ("联系人", ki.get("contact")),
                ("费用", ki.get("fee")),
            ]
            for label, val in rows:
                if not val:
                    continue
                row = tk.Frame(kis, bg=CARD)
                row.pack(fill="x", padx=16, pady=3)
                tk.Label(row, text=label, bg=CARD, fg=TEXT_FAINT, font=(FONT, FS_SM),
                         width=8, anchor="w").pack(side="left")
                tk.Label(row, text=str(val), bg=CARD, fg=TEXT, font=(FONT, FS_MD),
                         wraplength=680, justify="left", anchor="w").pack(
                    side="left", fill="x", expand=True)
            tk.Label(kis, text="", bg=CARD, height=1).pack()

        # 原文
        text = (detail.get("content_text") or detail.get("excerpt") or "").strip()
        if text:
            box = tk.Frame(holder, bg=CARD)
            box.pack(fill="both", expand=True, padx=14, pady=(6, 8))
            tk.Label(box, text="原文", bg=CARD, fg=SEED,
                     font=(FONT, FS_MD, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
            tb = tk.Text(box, bg=CARD, fg=TEXT_SUB, bd=0, font=(FONT, FS_MD),
                         wrap="word", height=14, padx=16, pady=0,
                         highlightthickness=0)
            tb.insert("1.0", text)
            tb.configure(state="disabled")
            tb.pack(fill="both", expand=True, padx=0, pady=(0, 4))

        # 底部按钮
        foot = tk.Frame(win, bg=CARD)
        foot.pack(fill="x", side="bottom")
        tk.Frame(foot, bg=BORDER, height=1).pack(fill="x")

        fb = tk.Frame(foot, bg=CARD)
        fb.pack(fill="x", padx=16, pady=11)

        url = detail.get("url")
        btn_open = tk.Button(fb, text="🌐  打开原文", bg=NAV_ACTIVE, fg=SEED, bd=0,
                             font=(FONT, FS_MD), padx=16, pady=6, cursor="hand2",
                             activebackground="#DDE5FF",
                             command=lambda: webbrowser.open(url) if url else None)
        btn_open.pack(side="left")
        if not url:
            btn_open.configure(state="disabled", fg=TEXT_FAINT)

        tk.Button(fb, text="📋  复制摘要", bg=CARD, fg=TEXT_SUB, bd=0,
                  font=(FONT, FS_MD), padx=14, pady=6, cursor="hand2",
                  command=lambda: self._copy(win, "\n".join(ensure_three_lines(detail)))
                  ).pack(side="left", padx=8)

        tk.Button(fb, text="🔄  重跑 AI 摘要", bg=CARD, fg=TEXT_SUB, bd=0,
                  font=(FONT, FS_MD), padx=14, pady=6, cursor="hand2",
                  command=lambda: self._resummarize(win, detail)
                  ).pack(side="left")

        tk.Button(fb, text="关闭", bg=CARD, fg=TEXT_SUB, bd=0, font=(FONT, FS_MD),
                  padx=14, pady=6, cursor="hand2",
                  command=on_detail_close).pack(side="right")

    def _copy(self, win: tk.Toplevel, text: str) -> None:
        win.clipboard_clear()
        win.clipboard_append(text)
        self._set_status(self.online, "已复制到剪贴板")

    def _resummarize(self, win: tk.Toplevel, detail: dict) -> None:
        nid = detail.get("id")
        if nid is None:
            return
        if not messagebox.askyesno(
            "重跑 AI 摘要",
            "会用 GLM 重新读一遍原文并覆盖当前摘要，确定？\n（免费额度有限，别频繁跑）",
            parent=win,
        ):
            return
        self._set_status(self.online, "正在重跑 AI 摘要…")
        win.update_idletasks()
        try:
            http_json(f"/notifications/{nid}/summarize", method="POST",
                      payload=None, timeout=120)
            win.destroy()
            self.load_feed()
            self._set_status(True, "AI 摘要已更新")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("重跑失败", str(e), parent=win)
            self._set_status(self.online, f"重跑失败：{str(e)[:60]}")

    # ================================================================== #
    # 今日简报
    # ================================================================== #
    def _build_brief_page(self) -> None:
        p = self.pages["brief"]
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text="今日简报", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(side="left")
        tk.Button(head, text="🔊  朗读", bg=CARD, fg=SEED, bd=0, font=(FONT, FS_SM),
                  padx=12, pady=5, cursor="hand2",
                  command=self.speak_brief).pack(side="right")

        self.brief_holder = tk.Frame(p, bg=BG)
        self.brief_holder.pack(fill="both", expand=True, padx=16, pady=(0, 14))

    def load_brief(self) -> None:
        for w in self.brief_holder.winfo_children():
            w.destroy()
        try:
            data = http_json("/brief/today?hours=72")
            brief = data.get("data") or {}
        except Exception as e:  # noqa: BLE001
            tk.Label(self.brief_holder, text=f"简报加载失败：{e}", bg=BG, fg="#E5484D",
                     font=(FONT, FS_MD), wraplength=860, justify="left").pack(anchor="w")
            return

        self._brief_data = brief

        card = tk.Frame(self.brief_holder, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=20, pady=18)

        tk.Label(inner, text=brief.get("greeting") or "", bg=CARD, fg=TEXT_SUB,
                 font=(FONT, FS_MD)).pack(anchor="w")
        tk.Label(inner, text=brief.get("headline") or "", bg=CARD, fg=TEXT,
                 font=(FONT, FS_XL, "bold"), wraplength=870, justify="left").pack(
            anchor="w", pady=(6, 0))

        counts = brief.get("counts") or {}
        chips = tk.Frame(inner, bg=CARD)
        chips.pack(fill="x", pady=(12, 0))
        for label, key, color in [("总条数", "total", TEXT_SUB), ("紧急", "urgent", "#E5484D"),
                                  ("即将截止", "due_soon", STAR_COLOR),
                                  ("重要", "important", "#2F6BFF"),
                                  ("未读", "unread", TEXT_FAINT)]:
            c = tk.Frame(chips, bg=SUBTLE_BG)
            c.pack(side="left", padx=(0, 8))
            tk.Label(c, text=str(counts.get(key, 0)), bg=SUBTLE_BG, fg=color,
                     font=(FONT, FS_XL, "bold")).pack(padx=14, pady=(8, 0))
            tk.Label(c, text=label, bg=SUBTLE_BG, fg=TEXT_FAINT,
                     font=(FONT, FS_XS)).pack(padx=14, pady=(0, 8))

        gen = brief.get("generated_by") or "local"
        tag = f"由 {gen} 生成" if gen != "local" else "本地规则生成（未使用大模型）"
        tk.Label(inner, text=tag, bg=CARD, fg="#B9BFCC",
                 font=(FONT, FS_XS)).pack(anchor="w", pady=(10, 0))

        # 分节
        for sec in brief.get("sections") or []:
            box = tk.Frame(self.brief_holder, bg=CARD, highlightthickness=1,
                           highlightbackground=BORDER)
            box.pack(fill="x", pady=(10, 0))
            bi = tk.Frame(box, bg=CARD)
            bi.pack(fill="x", padx=20, pady=14)
            tk.Label(bi, text=sec.get("title") or "", bg=CARD, fg=TEXT,
                     font=(FONT, FS_MD, "bold")).pack(anchor="w")
            for line in sec.get("items") or []:
                tk.Label(bi, text="• " + line, bg=CARD, fg=TEXT_SUB, font=(FONT, FS_MD),
                         wraplength=860, justify="left", anchor="w").pack(
                    fill="x", anchor="w", pady=2)

    def speak_brief(self) -> None:
        """朗读：用 Windows 自带 SAPI，不额外装东西。没装就明说。"""
        brief = getattr(self, "_brief_data", None)
        if not brief:
            messagebox.showinfo("朗读", "先等简报加载出来。", parent=self.root)
            return
        text = brief.get("speech_text") or brief.get("headline") or ""
        if not text:
            messagebox.showinfo("朗读", "这条简报没有可朗读的文本。", parent=self.root)
            return
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Speech; "
                 "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                 f"$s.Speak([Console]::In.ReadToEnd())"],
                stdin=subprocess.PIPE, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).communicate(text)
            self._set_status(self.online, "已朗读简报")
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning(
                "朗读不可用",
                f"调用系统语音失败：{e}\n\n"
                "说明：电脑版目前用 Windows 自带 SAPI 朗读。\n"
                "阶段 5 会接入更自然的语音（按手机端设计）。",
                parent=self.root,
            )

    # ================================================================== #
    # 收藏页
    # ================================================================== #
    def _build_starred_page(self) -> None:
        p = self.pages["starred"]
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text="我的收藏", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(side="left")
        tk.Label(head, text="点卡片右上角 ⭐ 收进来；📌 置顶；右下角勾选已读",
                 bg=BG, fg=TEXT_FAINT,
                 font=(FONT, FS_SM)).pack(side="left", padx=(12, 0), pady=(4, 0))
        tk.Button(head, text="🔄  刷新", bg=CARD, fg=SEED, bd=0, font=(FONT, FS_SM),
                  padx=12, pady=5, cursor="hand2",
                  command=lambda: self.load_feed(reset=True)).pack(side="right")

        # 复用信息流卡片区（同一套渲染，换一个 canvas）
        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.star_canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.star_canvas.yview)
        self.star_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.star_canvas.pack(side="left", fill="both", expand=True)
        self.star_holder = tk.Frame(self.star_canvas, bg=BG)
        cvw = self.star_canvas.create_window((0, 0), window=self.star_holder, anchor="nw")
        self.star_holder.bind(
            "<Configure>",
            lambda e: self.star_canvas.configure(scrollregion=self.star_canvas.bbox("all")))
        self.star_canvas.bind(
            "<Configure>",
            lambda e: self.star_canvas.itemconfigure(cvw, width=e.width))
        # 切到收藏页时，把滚轮交给收藏的 canvas（走统一的 _active_canvas 机制，
        # 不要用 bind_all —— 那会全局抢占，详情窗口就滚不动了）
        self.star_canvas.bind(
            "<Enter>", lambda e: setattr(self, "_active_canvas", self.star_canvas))

    # ================================================================== #
    # 来源管理
    # ================================================================== #
    def _build_sources_page(self) -> None:
        p = self.pages["sources"]
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text="来源管理", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(side="left")
        self.src_count = tk.Label(head, text="", bg=BG, fg=TEXT_FAINT, font=(FONT, FS_SM))
        self.src_count.pack(side="left", padx=(10, 0), pady=(4, 0))
        tk.Button(head, text="🔄  重新加载 yaml", bg=CARD, fg=SEED, bd=0,
                  font=(FONT, FS_SM), padx=12, pady=5, cursor="hand2",
                  command=self.reload_sources).pack(side="right")

        cols = ("name", "school", "category", "status", "count", "last")
        style = ttk.Style()
        style.configure("Src.Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=34, borderwidth=0)
        style.configure("Src.Treeview.Heading", font=(FONT, FS_SM, "bold"),
                        background=SUBTLE_BG, foreground=TEXT_SUB, borderwidth=0)

        holder = tk.Frame(p, bg=BG)
        holder.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.src_tree = ttk.Treeview(holder, columns=cols, show="headings",
                                     style="Src.Treeview")
        heads = {"name": ("来源名称", 260), "school": ("学校", 100),
                 "category": ("分类", 80), "status": ("状态", 80),
                 "count": ("条目", 70), "last": ("上次抓取", 150)}
        for c, (label, width) in heads.items():
            self.src_tree.heading(c, text=label)
            self.src_tree.column(c, width=width, anchor="w")
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.src_tree.yview)
        self.src_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.src_tree.pack(side="left", fill="both", expand=True)
        self.src_tree.bind("<Double-1>", self.fetch_selected_source)

        tip = tk.Frame(p, bg=BG)
        tip.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(tip, text="双击某一行 = 单独抓取这个来源。想新增学校，"
                           "在 backend/sources/ 里加一个 yaml，再点「重新加载 yaml」即可。",
                 bg=BG, fg=TEXT_FAINT, font=(FONT, FS_SM)).pack(anchor="w")

    def load_sources(self) -> None:
        try:
            data = http_json("/sources")
            rows = data.get("data") or []
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("加载失败", str(e), parent=self.root)
            return
        for i in self.src_tree.get_children():
            self.src_tree.delete(i)
        for s in rows:
            status = s.get("last_status") or "pending"
            status_icon = {"ok": "✅ 正常", "pending": "· 待抓", "error": "❌ 失败"}.get(
                status, status)
            self.src_tree.insert("", "end", values=(
                s.get("name") or "",
                s.get("school") or "",
                s.get("category_hint") or "",
                status_icon,
                s.get("item_count") or 0,
                (s.get("last_fetched_at") or "--")[:16].replace("T", " "),
            ))
        self.src_count.configure(text=f"共 {len(rows)} 个来源")

    def reload_sources(self) -> None:
        try:
            data = http_json("/sources/reload", method="POST", payload=None, timeout=60)
            st = data.get("data") or {}
            self.load_sources()
            messagebox.showinfo("已重新加载",
                                f"新增 {st.get('created', 0)}，更新 {st.get('updated', 0)}，"
                                f"共 {st.get('total', 0)} 个来源。", parent=self.root)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("加载失败", str(e), parent=self.root)

    def fetch_selected_source(self, _event=None) -> None:
        sel = self.src_tree.selection()
        if not sel:
            return
        name = self.src_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno(
            "单独抓取", f"现在只抓「{name}」这一个来源？\n（会遵守 robots.txt，同域限速 2 秒）",
            parent=self.root,
        ):
            return
        self._set_status(self.online, f"正在抓取 {name}…")
        self.root.update_idletasks()
        try:
            data = http_json("/ingest/run", method="POST", payload=None, timeout=300)
            r = data.get("data") or {}
            self.load_sources()
            self.load_feed(reset=True)
            messagebox.showinfo(
                "抓取完成",
                f"共 {r.get('total_items', 0)} 条，新增 {r.get('new_items', 0)} 条，"
                f"摘要 {r.get('summarized', 0)} 条。",
                parent=self.root)
            self._set_status(True, "抓取完成")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("抓取失败", str(e), parent=self.root)
            self._set_status(self.online, f"抓取失败：{str(e)[:60]}")

    # ================================================================== #
    # AI 问答
    # ================================================================== #
    def _build_chat_page(self) -> None:
        p = self.pages["chat"]
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text="AI 问答", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(side="left")
        self.chat_by = tk.Label(head, text="", bg=BG, fg=TEXT_FAINT, font=(FONT, FS_SM))
        self.chat_by.pack(side="left", padx=(10, 0), pady=(4, 0))

        ex = tk.Frame(p, bg=BG)
        ex.pack(fill="x", padx=16, pady=(0, 6))
        tk.Label(ex, text="试试：", bg=BG, fg=TEXT_FAINT,
                 font=(FONT, FS_SM)).pack(side="left")
        for q in ["最近有什么奖学金通知？", "有没有即将截止的事？", "国庆期间图书馆怎么开？"]:
            tk.Button(ex, text=q, bg=CARD, fg=SEED, bd=0, font=(FONT, FS_XS),
                      padx=8, pady=3, cursor="hand2",
                      command=lambda t=q: self.ask(t)).pack(side="left", padx=(0, 6))

        area = tk.Frame(p, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        area.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.chat_text = tk.Text(area, bg=CARD, fg=TEXT, bd=0, font=(FONT, FS_MD),
                                 wrap="word", padx=16, pady=14,
                                 highlightthickness=0, spacing1=2, spacing3=6)
        sb = ttk.Scrollbar(area, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat_text.pack(side="left", fill="both", expand=True)
        self.chat_text.configure(state="disabled")
        self.chat_text.tag_configure("q", foreground=SEED, font=(FONT, FS_MD, "bold"))
        self.chat_text.tag_configure("a", foreground=TEXT)
        self.chat_text.tag_configure("cite", foreground=TEXT_FAINT, font=(FONT, FS_SM))

        inrow = tk.Frame(p, bg=BG)
        inrow.pack(fill="x", padx=16, pady=(0, 12))
        self.q_var = tk.StringVar()
        e = tk.Entry(inrow, textvariable=self.q_var, bd=0, bg=CARD, fg=TEXT,
                     font=(FONT, FS_MD), insertbackground=TEXT,
                     highlightthickness=1, highlightbackground=BORDER)
        e.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        e.bind("<Return>", lambda ev: self.ask())
        self.q_entry = e
        self.btn_ask = tk.Button(inrow, text="提问", bg=SEED, fg="#FFFFFF", bd=0,
                                 font=(FONT, FS_MD), padx=24, pady=7, cursor="hand2",
                                 activebackground="#2559D8", command=self.ask)
        self.btn_ask.pack(side="left")

        self._chat_append("知了 AI 问答", "a")
        self._chat_append(
            "我会只依据「已经抓到的通知原文」回答，找不到就说找不到，不编。"
            "回答下面会列出引用到的通知。\n", "cite")

    def _chat_append(self, text: str, tag: str) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", text + "\n", tag)
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def ask(self, question: str | None = None) -> None:
        q = (question if question is not None else self.q_var.get()).strip()
        if not q:
            return
        self.q_var.set("")
        self._chat_append(f"你：{q}", "q")
        self.btn_ask.configure(state="disabled", text="思考中…")
        self.root.update_idletasks()

        def work():
            try:
                data = http_json("/chat", method="POST",
                                 payload={"question": q, "days": 90, "top_k": 12},
                                 timeout=120)
                res = data.get("data") or {}
                self._post(lambda: self._chat_render(res))
            except Exception as e:  # noqa: BLE001
                self._post(lambda: self._chat_error(e))

        threading.Thread(target=work, daemon=True).start()

    def _chat_render(self, res: dict) -> None:
        self.btn_ask.configure(state="normal", text="提问")
        self._chat_append(res.get("answer") or "（模型没有返回内容）", "a")
        gen = res.get("generated_by") or "local"
        label = "智谱 " + gen if gen != "local" else "本地兜底（未用大模型）"
        self.chat_by.configure(text=f"本次回答：{label}")
        cites = res.get("citations") or []
        if cites:
            self._chat_append(f"引用 {len(cites)} 条通知：", "cite")
            for i, c in enumerate(cites[:8], 1):
                date = (c.get("published_at") or "")[:10]
                self._chat_append(f"  {i}. [{date}] {c.get('title')}", "cite")
        self._chat_append("", "cite")

    def _chat_error(self, e: Exception) -> None:
        self.btn_ask.configure(state="normal", text="提问")
        self._chat_append(f"出错了：{e}", "cite")

    # ================================================================== #
    # 设置
    # ================================================================== #
    def _build_settings_page(self) -> None:
        p = self.pages["settings"]
        self.settings_holder = tk.Frame(p, bg=BG)
        self.settings_holder.pack(fill="both", expand=True, padx=16, pady=14)

    def load_settings(self) -> None:
        for w in self.settings_holder.winfo_children():
            w.destroy()
        tk.Label(self.settings_holder, text="设置与状态", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(anchor="w", pady=(0, 10))

        info: dict = {}
        try:
            info = (http_json("/health?deep=false") or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            info = {"_error": str(e)}

        ai = info.get("ai") or {}
        counts = info.get("counts") or {}
        db = info.get("database") or {}
        sched = info.get("scheduler") or {}
        push = info.get("push") or {}

        push_channels = ", ".join(push.get("channels") or []) or "-"
        rows = [
            ("后端状态", "✅ 在线" if not info.get("_error") else f"❌ {info['_error'][:60]}"),
            ("运行模式", info.get("env") or "-"),
            ("数据库", f"{db.get('dialect','-')} · {'正常' if db.get('ok') else '异常'}"),
            ("通知总数", str(counts.get("notifications", 0))),
            ("未读", str(counts.get("unread", 0))),
            ("已置顶", str(counts.get("pinned", 0))),
            ("来源总数", str(counts.get("sources", 0))),
            ("待摘要", str(counts.get("ai_pending", 0))),
            ("AI 模型", f"{ai.get('model','-')} · {'已启用' if ai.get('enabled') else '未配置'}"),
            ("刷新计划", info.get("cron") or "-"),
            ("定时任务", "运行中" if sched.get("running") else "未运行"),
            ("推送通道", f"{push_channels}（待拉取 {push.get('unsent', 0)} 条）"),
            ("后端端口", str(PORT)),
            ("程序目录", str(ROOT)),
        ]
        card = tk.Frame(self.settings_holder, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=14)
        for k, v in rows:
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=k, bg=CARD, fg=TEXT_FAINT, font=(FONT, FS_SM),
                     width=12, anchor="w").pack(side="left")
            tk.Label(row, text=str(v), bg=CARD, fg=TEXT, font=(FONT, FS_MD),
                     anchor="w", justify="left", wraplength=760).pack(
                side="left", fill="x", expand=True)

        btns = tk.Frame(self.settings_holder, bg=BG)
        btns.pack(fill="x", pady=(12, 0))
        for label, fn in [
            ("🌐  打开接口文档", lambda: webbrowser.open(f"{BASE}/docs")),
            ("📂  打开程序目录", lambda: os.startfile(str(ROOT))),
            ("📜  查看后端日志", self.open_log),
            ("🔍  AI 连通性自检", self.check_ai_now),
            ("♻️  重启后端", self.restart_backend),
        ]:
            tk.Button(btns, text=label, bg=CARD, fg=SEED, bd=0, font=(FONT, FS_SM),
                      padx=14, pady=7, cursor="hand2",
                      command=fn).pack(side="left", padx=(0, 8))

        self._build_notify_card()

        note = ("说明：电脑版与手机端共用同一套后端和数据库结构，"
                "界面是把手机端的信息层级在桌面上重排了一遍。\n"
                "AI 走智谱 GLM-4.7-Flash，Key 存在 backend/.env 里，"
                "没有 Key 或额度用尽时会自动降级为本地规则摘要，链路不会断。")
        tk.Label(self.settings_holder, text=note, bg=BG, fg=TEXT_FAINT,
                 font=(FONT, FS_SM), justify="left").pack(anchor="w", pady=(14, 0))

    # ------------------------------------------------------------------ #
    def _build_notify_card(self) -> None:
        """设置页的「桌面通知」卡片。"""
        tk.Label(self.settings_holder, text="桌面通知", bg=BG, fg=TEXT,
                 font=(FONT, FS_XL, "bold")).pack(anchor="w", pady=(20, 8))

        card = tk.Frame(self.settings_holder, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=14)

        # 开关 + 间隔
        line = tk.Frame(inner, bg=CARD)
        line.pack(fill="x")

        chk_var = tk.BooleanVar(value=self.notify_on())
        chk = tk.Checkbutton(
            line, text="开启新通知提醒", variable=chk_var, bg=CARD, fg=TEXT,
            activebackground=CARD, selectcolor=CARD, font=(FONT, FS_MD),
            cursor="hand2", bd=0, highlightthickness=0,
            command=self.toggle_notify,
        )
        chk.pack(side="left")

        tk.Label(line, text="检查间隔", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_SM)).pack(side="left", padx=(24, 6))
        self.interval_var = tk.StringVar(value=str(self.state.get("notify_interval") or 30))
        spin = tk.Spinbox(line, from_=5, to=3600, increment=5, width=6,
                          textvariable=self.interval_var, font=(FONT, FS_SM),
                          bg=BG, fg=TEXT, bd=1, relief="solid",
                          command=self._save_interval)
        spin.pack(side="left")
        # Spinbox 直接手输时不会触发 command，用 FocusOut / 回车兜住
        spin.bind("<Return>", lambda e: self._save_interval())
        spin.bind("<FocusOut>", lambda e: self._save_interval())
        tk.Label(line, text="秒", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_SM)).pack(side="left", padx=(4, 0))

        tk.Button(line, text="🔔  试一下通知", bg=NAV_ACTIVE, fg=SEED, bd=0,
                  font=(FONT, FS_SM, "bold"), padx=12, pady=5, cursor="hand2",
                  command=self.test_toast).pack(side="right")

        # 状态行
        info_line = tk.Frame(inner, bg=CARD)
        info_line.pack(fill="x", pady=(12, 0))
        cursor = self.state.get("push_cursor") or 0
        stamp = self.state.get("last_check") or "尚未检查"
        tk.Label(info_line,
                 text=(f"已弹过 {self.state.get('notified_count', 0)} 条 · "
                       f"游标 #{cursor} · 上次检查 {stamp}"),
                 bg=CARD, fg=TEXT_SUB, font=(FONT, FS_SM)).pack(side="left")

        # 必须写清楚的限制，不能让人以为「关掉 App 也能收到」
        tk.Label(inner, justify="left", anchor="w", bg=CARD, fg=TEXT_FAINT,
                 font=(FONT, FS_SM), wraplength=900,
                 text=("拉取方式：本机每 N 秒问一次后端「有没有新的」拿到就弹气泡。\n"
                       "⚠️ 气泡是本程序自己开的窗口，**关掉知了之后就收不到**了 —— "
                       "「App 没开也能收到」需要厂商推送通道，需要开发者账号 + 应用上架审核，"
                       "个人开发者拿不到；FCM 在国内没有 Google 服务框架的手机上也收不到。\n"
                       "想「关掉电脑也能收到」，可以配置 Webhook 转发到企业微信 / 钉钉 / "
                       "Server酱 / Bark（见 README 的「推送到手机」一节）。")
                 ).pack(fill="x", pady=(10, 0))

    def _save_interval(self) -> None:
        try:
            val = int(float(self.interval_var.get()))
        except Exception:  # noqa: BLE001
            val = 30
        val = max(5, min(3600, val))
        self.interval_var.set(str(val))
        if val != self.state.get("notify_interval"):
            self.state["notify_interval"] = val
            save_desktop_state(self.state)
            if self.notify_on():
                self._stop_poll()
                self._start_poll(delay=500)
            self._set_status(self.online, f"通知检查间隔已改为 {val} 秒")

    def open_log(self) -> None:
        path = LOG_DIR / "backend.log"
        if path.exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo("日志", "日志文件还没生成。", parent=self.root)

    def check_ai_now(self) -> None:
        self._set_status(self.online, "正在自检 AI…")
        self.root.update_idletasks()

        def work():
            try:
                d = http_json("/health?deep=true", timeout=90)
                chk = ((d.get("data") or {}).get("ai") or {}).get("check") or {}
                self._post(lambda: self._ai_result(chk))
            except Exception as e:  # noqa: BLE001
                self._post(lambda: messagebox.showerror(
                    "自检失败", str(e), parent=self.root))

        threading.Thread(target=work, daemon=True).start()

    def _ai_result(self, chk: dict) -> None:
        self._set_status(self.online, "AI 自检完成")
        if chk.get("ok"):
            messagebox.showinfo("AI 自检", f"✅ 通路正常\n\n模型：{chk.get('model')}\n"
                                           f"模型回复：{chk.get('reply')}", parent=self.root)
        else:
            messagebox.showwarning("AI 自检",
                                   f"⚠️ 不可用\n\n{chk.get('detail') or '未配置 Key'}\n\n"
                                   "不要紧：没有 AI 时全链路会自动降级到本地规则摘要。",
                                   parent=self.root)

    def restart_backend(self) -> None:
        if not messagebox.askyesno("重启后端", "会短暂中断服务，确定重启？", parent=self.root):
            return
        self._set_status(False, "正在重启后端…")
        self.root.update_idletasks()
        self.backend.stop()
        self.backend.start()
        ok = self.backend.wait_ready(timeout=60)
        self.online = ok
        self._set_status(ok, "后端已重启" if ok else f"重启失败：{self.backend.last_error}")
        self.refresh_all()

    # ================================================================== #
    def do_refresh(self) -> None:
        if not self.online:
            # 后端没起来，先试着拉起来
            self._set_status(False, "正在尝试重连后端…")
            self.root.update_idletasks()
            if not self.backend.proc or self.backend.proc.poll() is not None:
                self.backend.start()
            ok = self.backend.wait_ready(timeout=45)
            self.online = ok
            self._set_status(ok, "后端已就绪" if ok else f"仍不可用：{self.backend.last_error}")
            self.refresh_all()
            return
        self.load_feed(reset=True)

    def refresh_all(self) -> None:
        self.load_feed(reset=True)
        if self.current_page == "sources":
            self.load_sources()

    def _set_status(self, online: bool, text: str) -> None:
        self.online = online
        color = "#2FA84F" if online else "#E5484D"
        self.status_dot.configure(fg=color)
        self.status_label.configure(text=text)


# --------------------------------------------------------------------------- #
def main() -> None:
    backend = Backend()
    backend.start()

    root = tk.Tk()
    root.withdraw()   # 先藏着，等后端就绪再显示

    # 启动画面
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.configure(bg="#FFFFFF")
    w, h = 420, 170
    sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    splash.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")
    tk.Label(splash, text="知了", bg="#FFFFFF", fg=SEED,
             font=(FONT, 26, "bold")).pack(pady=(34, 4))
    tk.Label(splash, text="正在启动本机后端…", bg="#FFFFFF", fg=TEXT_SUB,
             font=(FONT, FS_MD)).pack()
    pb = ttk.Progressbar(splash, mode="indeterminate", length=300,
                         style="QuanZhi.Horizontal.TProgressbar")
    pb.pack(pady=(18, 0))
    pb.start(12)
    splash.update()

    ok = backend.wait_ready(timeout=90)
    splash.destroy()

    if not ok:
        messagebox.showwarning(
            "后端启动异常",
            f"后端没能正常启动：\n{backend.last_error}\n\n"
            "窗口仍会打开，但会显示「后端未连接」。\n"
            "可以点左侧「设置 → 查看后端日志」看具体原因。",
            parent=root,
        )

    app = QuanZhiApp(root, backend)
    if not ok:
        app._set_status(False, f"后端离线 · {backend.last_error[:40]}")

    def on_close():
        try:
            try:
                app._stop_poll()
                save_desktop_state(app.state)
            except Exception:  # noqa: BLE001
                pass
            backend.stop()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.deiconify()
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        crash = LOG_DIR / "crash.log"
        crash.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror("知了启动失败",
                                 f"启动时出错，详情见：\n{crash}\n\n"
                                 + traceback.format_exc()[-600:])
        except Exception:  # noqa: BLE001
            pass
        raise
