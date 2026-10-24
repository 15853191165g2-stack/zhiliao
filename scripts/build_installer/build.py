"""一键产出「知了」Windows 安装包。

做四件事，顺序不能换：
    1. 生成图标（多尺寸 ico）
    2. 组装载荷 staging（剔无用包、清密钥）—— 详细规则见 make_payload.py
    3. 生成 license.txt / 使用说明.txt
    4. 调 makensis 编译

为什么必须给 .nsi 加 BOM：NSIS 3 的 Unicode 模式靠 BOM 判断脚本编码，
   不带 BOM 的中文会被当成 ANSI 读，装出来的向导标题全是乱码 —— 而编译不报错。

用法：
    python scripts/build_installer/build.py --source D:/QuanZhi
    python scripts/build_installer/build.py --source D:/QuanZhi --skip-payload   # 复用上次 staging
环境变量：
    NSIS_HOME   makensis.exe 所在目录（默认自动找 PATH / 本机常见位置）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = Path(sys.executable)

LICENSE_CN = """
============================================================
  知了 · 电脑版 —— 安装前请务必读这 5 条
============================================================

1. 装在哪里、为什么不装 C:\\Program Files
   默认装到  %LOCALAPPDATA%\\Programs\\ZhiLiao （当前用户目录下）。
   知了启动后要自己建库、写通知、记已读状态，装在 Program Files 里
   普通用户没有写权限，装完会打不开。按用户安装还有个好处：不弹 UAC。

2. 安装包不含任何 AI 密钥
   安装包会发给别人，所以 .env 里的 API Key 与「模型配置」表的 Key 都已清空。
   开箱即可看通知（库里自带 265 条已抓好的真实通知、每条都有 3 行摘要），
   但「AI 问答」和「重新生成摘要」要你自己填 key：
   打开知了 → 问答页右上角「模型：未配置 ▾」→ 新建配置 → 填接入地址和 key。

3. 首次启动会慢一点
   要解压 Python 运行时、初始化数据库，第一次大约 5~15 秒。
   之后是 2 秒内开窗。

4. 会连哪些网
   · 抓取：大连理工大学各院系网站（公开页面，遵守 robots）
   · AI：你自己填的那个接口（不填就不联网）
   · 不会把通知内容、已读记录传到任何我们控制的服务器

5. 卸载会不会删数据
   不会默认删。卸载时问你「是否保留通知数据」，选保留会存到
   %LOCALAPPDATA%\\ZhiLiaoData，重装时自动问要不要恢复。
   命令行静默卸载（Uninstall.exe /S）一律保留数据。

------------------------------------------------------------
本程序以 MIT 协议开源：https://github.com/15853191165g2-stack/zhiliao
下面是完整的 MIT 许可证原文。
============================================================

"""

USAGE_TXT = """
知了 · 电脑版 使用说明
============================================================

怎么打开
    双击桌面「知了」图标，或开始菜单 → 知了 → 知了。
    （快捷方式调的是 python\\pythonw.exe，不会弹黑窗口）

第一次用
    1. 打开就是信息流，按重要度排好序，每条都有 3 行摘要。
    2. 顶栏 🔔 是气泡通知开关，开了之后有新通知会在右下角弹气泡。
    3. 打开详情会自动标已读（这是产品行为，不是 bug）。
    4. 筛选：全部 / 未读 / 重要；右上「全部标已读」可以一键清。
    5. 想加自己学院的栏目：设置页 → 添加来源 → 粘贴栏目列表页网址
       → 先试抓，看到真实标题再保存（抓不到的网址不让存）。

AI 功能怎么开
    问答页右上角「模型：未配置 ▾」→ 新建 → 填接口地址和 key → 测连通 → 启用。
    支持任何 OpenAI 兼容接口（/v1/chat/completions 或 /v1/messages）。
    没配也能用：摘要走本地规则兜底，只是不如模型写得好。

数据在哪
    <安装目录>\\backend\\quanzhi.db     所有通知
    <安装目录>\\state\\desktop.json    通知开关、已读游标
    <安装目录>\\logs\\                 后端日志，出问题先看这个

常见问题
    · 双击没反应？看 logs\\crash.log。
    · 提示 8765 端口被占用？说明已经开着一个知了，去任务栏找它。
    · 校内网才能访问学校 AI 网关，校外请自己换一个接口地址。
    · 学校网站改版导致抓不到？设置页把该来源关掉，等适配器更新。

自检
    双击 自检.bat 会逐项检查解释器、依赖、数据库、端口。
============================================================
"""


def find_makensis() -> Path | None:
    env = os.environ.get("NSIS_HOME")
    cands: list[Path] = []
    if env:
        cands += [Path(env) / "makensis.exe", Path(env) / "Bin" / "makensis.exe"]
    which = shutil.which("makensis")
    if which:
        cands.append(Path(which))
    for p in (
        Path(r"C:\Program Files\NSIS\makensis.exe"),
        Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "ZhiLiaoBuildTools" / "nsis-3.10" / "makensis.exe",
        HERE / "nsis-3.10" / "makensis.exe",
        Path(os.environ.get("TEMP", "")) / "zl_build" / "nsis-3.10" / "makensis.exe",
    ):
        cands.append(p)
    for c in cands:
        if c and c.exists():
            return c
    return None


def ensure_bom(path: Path) -> None:
    """UTF-8 BOM：NSIS Unicode 模式靠它认编码，缺了中文变乱码且不报错。"""
    data = path.read_bytes()
    if not data.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(b"\xef\xbb\xbf" + data)
        print(f"  +BOM  {path.name}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="D:/QuanZhi", help="便携包目录（含 python/ 与 backend/）")
    ap.add_argument("--build", default=str(
        Path(os.environ.get("LOCALAPPDATA", str(HERE))) / "ZhiLiaoBuildTools" / "build"
    ), help="构建中间目录（默认放 %%LOCALAPPDATA%%，别把 95MB staging 塞进仓库）")
    ap.add_argument("--out", default=str(HERE.parents[1] / "dist"), help="安装包输出目录")
    ap.add_argument("--skip-payload", action="store_true", help="复用已有 staging，只重新编译")
    ap.add_argument("--skip-icon", action="store_true")
    args = ap.parse_args()

    src = Path(args.source).resolve()
    build = Path(args.build).resolve()
    out_dir = Path(args.out).resolve()
    build.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 1) 图标
    ico = build / "zhiliao.ico"
    if args.skip_icon and ico.exists():
        print(f"[1/4] 图标      复用 {ico}")
    else:
        print("[1/4] 图标")
        r = run([str(PY), str(HERE / "make_icon.py"), str(ico)])
        if r.returncode != 0:
            return 1

    # 2) 载荷
    staging = build / "staging"
    if args.skip_payload and (staging / "python" / "python.exe").exists():
        print(f"[2/4] 载荷      复用 {staging}")
    else:
        print("[2/4] 载荷")
        r = run([str(PY), str(HERE / "make_payload.py"), str(src), str(staging)])
        if r.returncode != 0:
            return 1

    # 3) 协议与说明
    print("[3/4] 协议 / 说明")
    lic = build / "license.txt"
    mit = ""
    mit_src = src / "LICENSE"
    if mit_src.exists():
        mit = mit_src.read_text(encoding="utf-8", errors="replace")
    lic.write_text(LICENSE_CN + mit, encoding="utf-8")
    ensure_bom(lic)
    usage = build / "使用说明.txt"
    usage.write_text(USAGE_TXT, encoding="utf-8")
    ensure_bom(usage)
    print(f"  license.txt {lic.stat().st_size} B / 使用说明.txt {usage.stat().st_size} B")

    # 4) 编译
    nsi = build / "zhiliao.nsi"
    shutil.copy2(HERE / "zhiliao.nsi", nsi)
    ensure_bom(nsi)

    maker = find_makensis()
    if not maker:
        print("\nFAIL 找不到 makensis.exe。装 NSIS 或设环境变量 NSIS_HOME 指向它所在目录。")
        return 1
    print(f"[4/4] 编译    makensis = {maker}")
    # /V2 只报警告与错误；中文路径用 UTF-8 输出
    r = run([str(maker), "/V2", "/INPUTCHARSET", "UTF8", str(nsi)], cwd=str(build))
    if r.returncode not in (0, 1):        # NSIS 用 1 表示「有警告但产物已生成」
        print(f"\nFAIL makensis 返回 {r.returncode}")
        return r.returncode

    out = build / f"知了-Setup-1.0.0.exe"
    if not out.exists():
        outs = sorted(build.glob("*.exe"), key=lambda p: p.stat().st_mtime)
        if not outs:
            print("\nFAIL 没找到产物 exe")
            return 1
        out = outs[-1]

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / out.name
    shutil.copy2(out, final)

    size_mb = final.stat().st_size / 1048576
    print("\n" + "=" * 60)
    print(f"OK  安装包：{final}")
    print(f"    体积 {size_mb:.1f} MB · 用时 {time.time() - t0:.0f}s")
    print("=" * 60)
    print("下一步：python scripts/build_installer/verify_installer.py <安装目录> --gui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
