"""错误类型与退出码合同。

退出码（与 README、规格第 10.3 节一致）：

- 0：命令完成；查询无结果、无变更也是成功。
- 2：参数、请求或 schema 不合法，或根目录/存储布局不可用。
- 3：版本/SHA 冲突，或不能安全裁决的结构冲突。
- 4：锁等待超时。
- 5：有未完成事务或需要恢复。
- 6：I/O 错误或不完整写入；说明已完成到哪一步。
- 7：权威数据与凭据已提交，但派生视图更新失败。
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_LOCK_TIMEOUT = 4
EXIT_NEEDS_RECOVERY = 5
EXIT_IO = 6
EXIT_DERIVED = 7


class MemoryError(Exception):
    """工具内所有可预期错误的基类。"""

    def __init__(
        self,
        message: str,
        *,
        code: int = EXIT_USAGE,
        details: dict[str, Any] | None = None,
        data_status: str = "error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details: dict[str, Any] = dict(details or {})
        self.data_status = data_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


class UsageError(MemoryError):
    """参数、请求或根目录问题。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_USAGE)
        kwargs.setdefault("data_status", "error")
        super().__init__(message, **kwargs)


class SchemaError(UsageError):
    """字段、JSON、正文等格式不合法。"""


class ConflictError(MemoryError):
    """版本/SHA 或结构冲突。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_CONFLICT)
        kwargs.setdefault("data_status", "conflict")
        super().__init__(message, **kwargs)


class StructureConflictError(ConflictError):
    """不能安全裁决的结构冲突（例如替代链环）。"""


class LockTimeoutError(MemoryError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_LOCK_TIMEOUT)
        kwargs.setdefault("data_status", "unavailable")
        super().__init__(message, **kwargs)


class NeedsRecoveryError(MemoryError):
    def __init__(self, transaction_ids: list[str], message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_NEEDS_RECOVERY)
        kwargs.setdefault("data_status", "needs_recovery")
        details = dict(kwargs.pop("details", {}) or {})
        details.setdefault("transaction_ids", list(transaction_ids))
        details.setdefault("next_action", "先运行 recover --dry-run，再运行 recover --apply，然后重试原操作")
        super().__init__(
            message or ("存在未完成事务，需要先恢复: " + ", ".join(transaction_ids)),
            details=details,
            **kwargs,
        )


class IOFailure(MemoryError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_IO)
        kwargs.setdefault("data_status", "incomplete")
        super().__init__(message, **kwargs)


class DerivedFailure(MemoryError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", EXIT_DERIVED)
        kwargs.setdefault("data_status", "committed_derived_failed")
        super().__init__(message, **kwargs)


__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_CONFLICT",
    "EXIT_LOCK_TIMEOUT",
    "EXIT_NEEDS_RECOVERY",
    "EXIT_IO",
    "EXIT_DERIVED",
    "MemoryError",
    "UsageError",
    "SchemaError",
    "ConflictError",
    "StructureConflictError",
    "LockTimeoutError",
    "NeedsRecoveryError",
    "IOFailure",
    "DerivedFailure",
]
