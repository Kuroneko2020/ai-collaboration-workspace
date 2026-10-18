"""严格 JSON 读写工具。

- 用 ``object_pairs_hook`` 拒绝重复键；
- 拒绝 NaN/Infinity 这类非标准常量；
- 读取兼容 UTF-8 BOM；写出 UTF-8 无 BOM、LF（调用方控制字节）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import IOFailure, SchemaError


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"JSON 中出现不受支持的常量: {name}（只接受标准 JSON）")


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"JSON 对象出现重复键: {key!r}")
        result[key] = value
    return result


def loads_strict(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{label}: JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}") from exc


def read_text_utf8(path: Path, label: str | None = None) -> str:
    """读取 UTF-8 文本；自动剥离 BOM，统一换行为 LF（不写回原件）。"""

    label = label or str(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise IOFailure(f"{label}: 文件不存在") from None
    except OSError as exc:
        raise IOFailure(f"{label}: 读取失败: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{label}: 不是合法的 UTF-8 文本（{exc}）") from exc
    return text


def read_json_strict(path: Path, label: str) -> Any:
    return loads_strict(read_text_utf8(path, label), label)


def dumps_pretty(obj: Any) -> str:
    """稳定、可读的 JSON 文本（末尾带换行）。"""

    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


__all__ = ["loads_strict", "read_text_utf8", "read_json_strict", "dumps_pretty"]
