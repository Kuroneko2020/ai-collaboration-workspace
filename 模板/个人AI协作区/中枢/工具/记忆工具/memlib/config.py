"""配置加载与工作区根解析。

配置.json 是版本化小配置，不保存运行凭据。根目录必须显式存在并带有合法配置；
错误根目录不会被自动初始化。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import SchemaError, UsageError
from .jsonutil import read_json_strict
from .pathutil import resolve_path

#: 相对工作区根的配置路径（组件形式，避免硬编码分隔符）
CONFIG_REL_PARTS = ("中枢", "记忆", "配置.json")

WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CONFIG_KEYS = (
    "schema_version",
    "workspace_id",
    "lock_timeout_seconds",
    "query_limit",
    "context_max_chars",
    "candidate_review_days",
)


@dataclass(frozen=True)
class MemoryConfig:
    schema_version: int
    workspace_id: str
    lock_timeout_seconds: float
    query_limit: int
    context_max_chars: int
    candidate_review_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "query_limit": self.query_limit,
            "context_max_chars": self.context_max_chars,
            "candidate_review_days": self.candidate_review_days,
        }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"配置字段 {field} 必须是整数")
    if not (minimum <= value <= maximum):
        raise SchemaError(f"配置字段 {field} 超出允许范围 {minimum}..{maximum}: {value}")
    return value


def validate_config(data: Any, origin: str) -> MemoryConfig:
    if not isinstance(data, dict):
        raise SchemaError(f"{origin}: 配置必须是 JSON 对象")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise SchemaError(f"{origin}: 出现 v1 未定义的配置字段: {unknown}")
    missing = [key for key in CONFIG_KEYS if key not in data]
    if missing:
        raise SchemaError(f"{origin}: 缺少配置字段: {missing}")
    schema_version = _require_int(data["schema_version"], "schema_version", 0, 10**6)
    if schema_version != SCHEMA_VERSION:
        raise SchemaError(f"{origin}: 不支持的配置 schema_version={schema_version}（本工具支持 {SCHEMA_VERSION}）")
    workspace_id = data["workspace_id"]
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise SchemaError(f"{origin}: workspace_id 必须是 1..100 位的稳定标识（字母数字及 ._-），当前为 {workspace_id!r}")
    timeout = data["lock_timeout_seconds"]
    if not _is_number(timeout) or not (0 < float(timeout) <= 60):
        raise SchemaError(f"{origin}: lock_timeout_seconds 必须是 0..60 之间的数字")
    query_limit = _require_int(data["query_limit"], "query_limit", 1, 1000)
    max_chars = _require_int(data["context_max_chars"], "context_max_chars", 100, 1_000_000)
    review_days = _require_int(data["candidate_review_days"], "candidate_review_days", 1, 3650)
    return MemoryConfig(
        schema_version=schema_version,
        workspace_id=workspace_id,
        lock_timeout_seconds=float(timeout),
        query_limit=query_limit,
        context_max_chars=max_chars,
        candidate_review_days=review_days,
    )


def config_path(root: Path) -> Path:
    return root.joinpath(*CONFIG_REL_PARTS)


def load_config(root: Path) -> MemoryConfig:
    path = config_path(root)
    if not path.is_file():
        raise UsageError(
            f"工作区根 {root} 缺少 中枢/记忆/配置.json；错误根目录不会自动初始化。"
            " 请确认 --root 指向已有记忆中枢的工作区根，或先把配置放到正确位置。"
        )
    data = read_json_strict(path, str(path))
    return validate_config(data, str(path))


def resolve_root(explicit: str | None, default_root: Path) -> Path:
    """解析工作区根；默认根由入口脚本位置推算，--root 可指定隔离/迁移根。"""

    if explicit:
        candidate = Path(os.path.expanduser(str(explicit)))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
    else:
        candidate = default_root
    real = resolve_path(candidate)
    if not real.is_dir():
        raise UsageError(f"工作区根不存在或不是目录: {real}；错误根目录不会自动初始化")
    return real


__all__ = [
    "CONFIG_REL_PARTS",
    "MemoryConfig",
    "validate_config",
    "config_path",
    "load_config",
    "resolve_root",
]
