"""路径规范化与组件级包含判断。

禁止使用字符串 ``startswith`` 判断目录包含：``root-old`` 不是
``root`` 的子目录。这里统一用 ``os.path.realpath`` + 组件元组比较，
并按当前平台大小写语义（``os.path.normcase``）归一。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath
from typing import Iterable

from .errors import IOFailure, UsageError


def resolve_path(value: str | os.PathLike[str]) -> Path:
    """返回绝对、规范化（解析链接/junction）的路径；允许目标暂不存在。"""

    try:
        return Path(os.path.realpath(os.path.abspath(os.fspath(value))))
    except (OSError, ValueError) as exc:
        raise UsageError(f"无法规范化路径 {value!r}: {exc}") from exc


def path_parts(value: str | os.PathLike[str]) -> tuple[str, ...]:
    """规范化路径的组件元组（平台大小写语义）。"""

    try:
        real = os.path.realpath(os.path.abspath(os.fspath(value)))
    except (OSError, ValueError) as exc:
        raise UsageError(f"无法规范化路径 {value!r}: {exc}") from exc
    return tuple(PurePath(os.path.normcase(real)).parts)


def parts_within(child: Iterable[str], parent: Iterable[str]) -> bool:
    child_t = tuple(child)
    parent_t = tuple(parent)
    return len(child_t) >= len(parent_t) and child_t[: len(parent_t)] == parent_t


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    return parts_within(path_parts(child), path_parts(parent))


def is_strictly_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    return path_parts(child) != path_parts(parent) and is_within(child, parent)


def ensure_within(child: str | os.PathLike[str], parent: str | os.PathLike[str], what: str) -> None:
    if not is_within(child, parent):
        raise UsageError(f"{what}越界：{child} 不在 {parent} 之内（按路径组件判断）")


def relative_posix(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> str:
    """返回相对工作区根、以 ``/`` 分隔的路径；越界时报错。"""

    if not is_within(path, root):
        raise UsageError(f"路径越界，无法表示为工作区相对路径: {path}")
    rel = os.path.relpath(os.fspath(path), os.fspath(root))
    rel = rel.replace("\\", "/")
    return rel if rel != "." else "."


def display_path(path: Path) -> str:
    return str(path)


def safe_read_bytes(path: Path, label: str | None = None) -> bytes:
    label = label or str(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise IOFailure(f"{label}: 文件不存在") from None
    except OSError as exc:
        raise IOFailure(f"{label}: 读取失败: {exc}") from exc


__all__ = [
    "resolve_path",
    "path_parts",
    "parts_within",
    "is_within",
    "is_strictly_within",
    "ensure_within",
    "relative_posix",
    "safe_read_bytes",
    "display_path",
]
