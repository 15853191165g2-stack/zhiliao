"""组装安装包载荷（staging）：从便携包目录产出一份「可以装给别人」的干净副本。

三条原则：
1. **不装没用的**：运行时里混进来的 pymupdf(54MB)、pip(12MB) 和应用无关，一律剔除。
   装完要真启动一次后端验证（本脚本只负责搬文件，验证在 verify_installer.py）。
2. **不装密钥**：backend/.env 里的 AI_API_KEY、llm_profiles 表里的 api_key 全部清空。
   安装包会发给评委，明文 key 打包出去等于公开。
3. **不装我的数据**：logs/、state/、db 备份、__pycache__、.pytest_cache 不带。
   quanzhi.db 只保留通知本身（演示要有内容），用户态字段由首次运行自然生成。

用法：
    python scripts/build_installer/make_payload.py <源目录> <输出目录>
例：
    python scripts/build_installer/make_payload.py D:/QuanZhi build/staging
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# ---- python 运行时里可以安全删掉的部分 ----
PY_DROP_DIRS = {
    "lib/site-packages/pip",        # 安装完毕不再装包
    "lib/site-packages/pymupdf",    # 应用未引用（已 grep 确认）
    "lib/test",                     # CPython 自带测试，30MB 级
    "lib/idlelib",                  # IDLE 编辑器，桌面 app 不用
    "tcl/tk8.6/demos",              # Tk 演示程序
}
PY_DROP_GLOBS = ("**/__pycache__", "**/*.pyc", "**/*.pyo")

# ---- backend 里不带的东西 ----
BE_DROP_DIRS = {"__pycache__", ".pytest_cache", "logs"}
BE_DROP_PATTERNS = (r"\.db\.bak", r"\.db-wal$", r"\.db-shm$")


def _keep(rel: str) -> bool:
    """rel 是相对源根、小写、正斜杠的路径。"""
    low = rel.lower()
    for d in PY_DROP_DIRS:
        if low == d or low.startswith(d + "/"):
            return False
    return True


def copy_python(src: Path, dst: Path) -> int:
    n = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        if not _keep(rel):
            continue
        if "/__pycache__/" in rel or rel.endswith((".pyc", ".pyo")):
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n


def copy_backend(src: Path, dst: Path) -> int:
    n = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        if rel.split("/")[0] in BE_DROP_DIRS:
            continue
        if "/__pycache__/" in rel or rel.endswith((".pyc", ".pyo")):
            continue
        if any(re.search(pat, rel) for pat in BE_DROP_PATTERNS):
            continue
        if rel == ".env":            # 真实密钥，单独写一份脱敏的
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n


def write_sanitized_env(src: Path, dst: Path) -> list[str]:
    """照着真 .env 的键写一份，只把密钥清空 —— 键名保留，用户知道该填什么。"""
    src_env = src / ".env"
    dst_env = dst / ".env"
    if not src_env.exists():
        example = src / ".env.example"
        if example.exists():
            shutil.copy2(example, dst_env)
        return []
    out_lines: list[str] = []
    cleared: list[str] = []
    for line in src_env.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if m and m.group(2) in {"AI_API_KEY", "AI_BASE_URL"}:
            out_lines.append(f"{m.group(1)}{m.group(2)}=")
            cleared.append(m.group(2))
        else:
            out_lines.append(line)
    dst_env.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return cleared


def sanitize_db(db: Path) -> dict[str, int]:
    """清掉模型配置里的明文 key，并 VACUUM 收一收体积。"""
    stats: dict[str, int] = {}
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if "llm_profiles" in tables:
            n = con.execute("select count(*) from llm_profiles").fetchone()[0]
            con.execute("delete from llm_profiles")
            stats["清空的模型配置条数"] = n
        if "notifications" in tables:
            stats["通知条数"] = con.execute("select count(*) from notifications").fetchone()[0]
        con.commit()
        con.execute("vacuum")
    finally:
        con.close()
    return stats


def prepare_dst(dst: Path, fresh: bool) -> None:
    """准备输出目录。

    **不批量删**（项目铁律：用户拒绝批量删文件，safe-delete 钩子会直接拦下）。
    默认按文件覆盖更新；要一份干净的就 --fresh，把旧的改名挪到 _old/ 里，不删。
    """
    if not dst.exists():
        dst.mkdir(parents=True)
        return
    if fresh:
        old = dst.parent / "_old" / f"staging-{time.strftime('%Y%m%d-%H%M%S')}"
        old.parent.mkdir(parents=True, exist_ok=True)
        dst.rename(old)
        print(f"  旧 staging 已挪到 {old}（没删，需要时自己清）")
        dst.mkdir(parents=True)
    else:
        print("  复用已有 staging：按文件覆盖更新（不删任何文件）")


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(argv) < 2:
        print("用法: python make_payload.py <源目录 如 D:/QuanZhi> <输出目录> [--fresh]")
        return 2
    src = Path(argv[0]).resolve()
    dst = Path(argv[1]).resolve()
    if not (src / "python" / "python.exe").exists():
        print(f"FAIL 源目录不像便携包（缺 python/python.exe）：{src}")
        return 1
    prepare_dst(dst, "--fresh" in sys.argv)

    n_py = copy_python(src / "python", dst / "python")
    print(f"  python/     {n_py} 个文件")
    n_be = copy_backend(src / "backend", dst / "backend")
    print(f"  backend/    {n_be} 个文件")

    cleared = write_sanitized_env(src / "backend", dst / "backend")
    print(f"  .env        已清空密钥字段: {', '.join(cleared) if cleared else '（无）'}")

    stats = sanitize_db(dst / "backend" / "quanzhi.db")
    for k, v in stats.items():
        print(f"  db          {k}: {v}")

    # 顶层入口与说明：只带运行需要的那几个
    for name in ["quanzhi_desktop.py", "知了桌面版.py", "知了.bat", "自检.bat", "README.md", "LICENSE"]:
        s = src / name
        if s.exists():
            shutil.copy2(s, dst / name)
            print(f"  {name}")
        else:
            print(f"  跳过（源里没有）{name}")

    total = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file())
    files = sum(1 for p in dst.rglob("*") if p.is_file())
    print(f"\nOK  staging 完成：{files} 个文件 / {total / 1048576:.1f} MB → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
