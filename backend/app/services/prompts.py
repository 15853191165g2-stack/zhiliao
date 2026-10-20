"""Prompt 模板加载器。

模板文件独立放在 `app/prompts/`，按版本命名（summarize.v1.md）。
换 Prompt = 换文件，不改一行代码；改坏了可以立刻回滚到上一版。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptNotFound(FileNotFoundError):
    pass


class PromptLibrary:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().prompt_dir)

    def path_of(self, name: str, version: str = "v1") -> Path:
        return self.root / f"{name}.{version}.md"

    @lru_cache(maxsize=64)
    def _read(self, path_str: str) -> str:
        p = Path(path_str)
        if not p.exists():
            raise PromptNotFound(f"Prompt 模板不存在：{p}")
        return p.read_text(encoding="utf-8")

    def render(self, name: str, version: str = "v1", **variables: Any) -> str:
        """读取模板并替换 {{变量}}。

        未提供的变量会原样保留，方便一眼看出漏传了哪个参数。
        """
        raw = self._read(str(self.path_of(name, version)))

        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in variables:
                return m.group(0)
            value = variables[key]
            if value is None:
                return "（无）"
            if isinstance(value, (list, tuple)):
                return "\n".join(f"- {v}" for v in value)
            if isinstance(value, dict):
                return "\n".join(f"{k}: {v}" for k, v in value.items())
            return str(value)

        return _VAR_RE.sub(repl, raw)

    def split_system_user(self, text: str) -> tuple[str, str]:
        """模板用 `---USER---` 分隔 system 与 user 两段。"""
        if "---USER---" in text:
            system, user = text.split("---USER---", 1)
            return system.strip(), user.strip()
        return text.strip(), ""

    def available(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.glob("*.md"))


@lru_cache
def get_prompt_library() -> PromptLibrary:
    return PromptLibrary()
