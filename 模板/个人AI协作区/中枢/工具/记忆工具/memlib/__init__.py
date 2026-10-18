"""记忆工具内部模块（标准库实现）。

入口是同目录上一级的 ``memory.py``；本包只依赖 Python 3.11+ 标准库。
"""

from __future__ import annotations

TOOL_VERSION = "1.0.0"

#: 条目文件与配置 schema 版本
SCHEMA_VERSION = 1
#: 派生索引 schema 版本
INDEX_SCHEMA_VERSION = 1
#: 事务清单 schema 版本
MANIFEST_SCHEMA_VERSION = 1
#: 变更凭据 schema 版本
CREDENTIAL_SCHEMA_VERSION = 1

__all__ = [
    "TOOL_VERSION",
    "SCHEMA_VERSION",
    "INDEX_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "CREDENTIAL_SCHEMA_VERSION",
]
