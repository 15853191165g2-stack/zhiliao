"""发布门禁：安装 → 卸载 → 重装（数据恢复）→ 再卸载，全流程真跑。

为什么不能只看退出码（踩过）：
    NSIS 卸载器 /S 会先把**自己复制到 %TEMP% 再重新执行**，原进程立刻返回 0。
    所以 `Uninstall.exe /S` 退出码 0 只代表「已排上队」，不代表删完了。
    第一次实测就撞上：退出码 0，目录里 4078 个文件一个没少。
    因此这里所有「等它做完」的地方都是**轮询文件数/存在性**，不信退出码。

用法：
    python scripts/build_installer/verify_install_cycle.py <安装包.exe> [--target C:/Users/Public/zl_test]
退出码：0 全过 / 1 有断言没过
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

DESKTOP = Path(os.environ["USERPROFILE"]) / "Desktop"
APPDATA = Path(os.environ["APPDATA"])
LOCALAPPDATA = Path(os.environ["LOCALAPPDATA"])
STARTMENU = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "知了"
DATA_DIR = LOCALAPPDATA / "ZhiLiaoData"
REG = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ZhiLiao"


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else ""


def count_files(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(1 for p in d.rglob("*") if p.is_file())


def wait_until(fn, expect, timeout: float, label: str) -> bool:
    """轮询直到条件成立，别用 sleep 猜。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if last == expect:
            print(f"    {label}：{last}（{time.time() - t0:.0f}s）")
            return True
        time.sleep(0.5)
    print(f"    {label}：超时，最后是 {last}，期望 {expect}")
    return False


def reg_value(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


class Checker:
    def __init__(self) -> None:
        self.ok = 0
        self.bad = 0

    def __call__(self, cond: bool, label: str, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  [PASS] {label}" + (f"  ({extra})" if extra else ""))
        else:
            self.bad += 1
            print(f"  [FAIL] {label}" + (f"  ({extra})" if extra else ""))
        return bool(cond)


def run_installer(exe: Path, target: Path | None, timeout: float = 300) -> int:
    """直接 CreateProcess 起安装包。

    别用 `cmd /c "..."`：一是带引号的绝对路径会被 cmd 的引号规则搅乱，
    二是中文文件名/中文路径经过 cmd 的代码页会转坏。
    Python 的 subprocess 走 CreateProcessW，路径是 UTF-16，原样传进去最稳。
    """
    args = [str(exe), "/S"]
    if target:
        args.append(f"/D={target}")
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          cwd=str(exe.parent)).returncode


def run_uninstaller(target: Path, attempts: int = 3) -> int:
    """跑卸载器，允许重试。

    两个坑叠在一起，实测各踩过一次：
    1. NSIS 卸载器先把自己复制到 %TEMP%\\~nsu.tmp\\Un.exe 再执行，
       **原进程立刻返回 0** —— 退出码 0 不代表删完了。
    2. 同时存在第二个实例时复制会撞锁，直接返回 1、什么都不删。
    另外 **cwd 必须是安装目录**（实测在别处调绝对路径会返回 1），所以显式给 cwd。
    """
    un = target / "Uninstall.exe"
    rc = -1
    for i in range(attempts):
        if not un.exists():
            return -1
        rc = subprocess.run([str(un), "/S"], cwd=str(target),
                            capture_output=True, timeout=120).returncode
        if rc == 0:
            return 0
        print(f"    卸载器返回 {rc}，{2 * (i + 1)}s 后重试")
        time.sleep(2 * (i + 1))
    return rc


def uninstall_fully(target: Path, timeout: float = 180) -> bool:
    """卸载到「只剩自删中的 Uninstall.exe」为止，反复轮询 + 补刀。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if count_files(target) <= 1:
            return True
        run_uninstaller(target, attempts=1)
        time.sleep(2)
    return count_files(target) <= 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("setup", help="安装包 exe")
    ap.add_argument("--target", default=r"C:\Users\Public\zl_test",
                    help="测试安装目录（用 ASCII 路径：中文路径经 cmd 传给 /D= 会被转码搞坏）")
    ap.add_argument("--keep", action="store_true",
                    help="最后不卸载，留着给 verify_installer.py --gui 做界面验证")
    args = ap.parse_args()

    exe = Path(args.setup).resolve()
    target = Path(args.target)
    ck = Checker()
    t_all = time.time()

    print(f"\n安装包：{exe}  ({exe.stat().st_size / 1048576:.1f} MB)")
    print(f"测试目录：{target}")
    print("-" * 60)

    # ---------- 0. 清场 ----------
    print("\n[0/5] 清场")
    if target.exists():
        print("  目标目录已存在，先卸载")
        ok = uninstall_fully(target)
        ck(ok, "旧安装已清空", f"剩 {count_files(target)} 个文件")
        shutil.rmtree(target, ignore_errors=True)
    # 把上一轮保留的数据挪走，否则第 2 步「数据已保留」是假绿（文件本来就在那儿）
    if DATA_DIR.exists():
        bak = Path("logs/_diag/backup") / f"ZhiLiaoData-{time.strftime('%H%M%S')}"
        bak.parent.mkdir(parents=True, exist_ok=True)
        DATA_DIR.rename(bak.resolve())
        print(f"  上轮保留的数据已挪到 {bak}（测试要从零开始）")

    # ---------- 1. 静默安装 ----------
    print("\n[1/5] 静默安装")
    rc = run_installer(exe, target)
    ck(rc == 0, "安装器返回 0", f"rc={rc}")
    ok = wait_until(lambda: (target / "python" / "python.exe").exists(), True, 300, "解释器就位")
    ck(ok, "安装完成（轮询 python.exe 出现，不信退出码）")

    n = count_files(target)
    size_mb = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) / 1048576
    ck(n > 4000, "文件数 > 4000", f"{n} 个")
    ck(80 < size_mb < 130, "体积在 80~130MB", f"{size_mb:.1f} MB")
    ck((target / "backend" / "quanzhi.db").exists(), "数据库已装")
    ck((target / "zhiliao.ico").exists(), "程序图标已装")
    ck(reg_value("DisplayName") is not None, "注册表卸载项已写", reg_value("DisplayName") or "")
    ck(reg_value("EstimatedSize") is not None, "注册表登记了体积")
    ck((DESKTOP / "知了.lnk").exists(), "桌面快捷方式已建")
    ck(STARTMENU.exists() and (STARTMENU / "知了.lnk").exists(), "开始菜单已建")

    db_hash_installed = md5(target / "backend" / "quanzhi.db")

    # ---------- 2. 卸载，验证数据保留 ----------
    print("\n[2/5] 卸载（应保留数据）")
    ck(not DATA_DIR.exists(), "卸载前 ZhiLiaoData 不存在（保证第 3 步是它新建的）")
    rc = run_uninstaller(target)
    ck(rc == 0, "卸载器返回 0", f"rc={rc}")
    ok = uninstall_fully(target)
    ck(ok, "程序文件已清空（轮询文件数，不信退出码）", f"剩 {count_files(target)} 个")
    ck(not (target / "python").exists(), "python 运行时已删")
    ck(not (DESKTOP / "知了.lnk").exists(), "桌面快捷方式已删")
    ck(not STARTMENU.exists(), "开始菜单目录已删")
    ck(reg_value("DisplayName") is None, "注册表卸载项已删")
    kept = DATA_DIR / "quanzhi.db"
    ck(kept.exists(), "数据已保留到 ZhiLiaoData")
    ck(md5(kept) == db_hash_installed, "保留的数据与卸载前一致")

    # ---------- 3. 重装，验证自动恢复 ----------
    print("\n[3/5] 重装（应自动恢复上次数据）")
    # 只剩自删中的 Uninstall.exe 才敢收尾，否则会触发 safe-delete 的批量删除拦截
    if count_files(target) <= 5:
        shutil.rmtree(target, ignore_errors=True)
    rc = run_installer(exe, target)
    ck(rc == 0, "重装返回 0", f"rc={rc}")
    ok = wait_until(lambda: (target / "python" / "python.exe").exists(), True, 300, "解释器就位")
    ck(ok, "重装完成")
    ck(md5(target / "backend" / "quanzhi.db") == db_hash_installed,
       "数据已自动恢复（md5 与卸载前一致）")

    # ---------- 4. 最后卸载，恢复干净环境 ----------
    if args.keep:
        print("\n[4/5] 收尾卸载 —— 跳过（--keep），留给下一步做界面验证")
    else:
        print("\n[4/5] 收尾卸载")
        run_uninstaller(target)
        ok = uninstall_fully(target)
        ck(ok, "收尾卸载完成", f"剩 {count_files(target)} 个")
        if target.exists() and count_files(target) == 0:
            target.rmdir()

    # ---------- 5. 汇总 ----------
    print("\n[5/5] 汇总")
    print("-" * 60)
    print(f"结果：{ck.ok} 通过 / {ck.bad} 失败 · 总耗时 {time.time() - t_all:.0f}s")
    print(f"（卸载保留的数据仍在：{DATA_DIR} —— 这是设计行为，下次安装会问是否恢复）")
    return 0 if ck.bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
