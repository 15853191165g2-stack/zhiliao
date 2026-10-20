"""适配器注册表。

新增一个适配器只要两步：
    1. 写一个类，实现 `async def fetch(spec, ctx) -> list[RawItem]`
    2. 在类上加 `@register("your_kind")`
之后配置里写 `adapter: your_kind` 就能用。
"""

from __future__ import annotations

from typing import Any

from app.adapters.base import AdapterError, SourceAdapter, SourceSpec

_REGISTRY: dict[str, type] = {}
_INSTANCES: dict[str, Any] = {}
_LOADED = False


def register(kind: str):
    """类装饰器：把适配器注册到指定 kind。"""

    def deco(cls: type) -> type:
        cls.kind = kind  # type: ignore[attr-defined]
        _REGISTRY[kind] = cls
        return cls

    return deco


def load_builtin() -> None:
    """导入内置适配器模块，触发注册。幂等。"""
    global _LOADED
    if _LOADED:
        return
    from app.adapters import article, html_list, rss  # noqa: F401

    _LOADED = True


def available_kinds() -> list[str]:
    load_builtin()
    return sorted(_REGISTRY.keys())


def get_adapter(kind: str) -> Any:
    """取适配器实例（无状态，缓存复用）。"""
    load_builtin()
    if kind not in _REGISTRY:
        raise AdapterError(
            f"未知适配器 '{kind}'，可用：{', '.join(available_kinds())}"
        )
    if kind not in _INSTANCES:
        _INSTANCES[kind] = _REGISTRY[kind]()
    return _INSTANCES[kind]


def adapter_for(spec: SourceSpec) -> Any:
    return get_adapter(spec.adapter)


__all__ = [
    "AdapterError",
    "SourceAdapter",
    "SourceSpec",
    "adapter_for",
    "available_kinds",
    "get_adapter",
    "load_builtin",
    "register",
]
