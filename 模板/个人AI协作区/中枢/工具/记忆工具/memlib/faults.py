"""测试专用故障注入。

安全边界：
- 只由环境变量 ``MEMORY_TEST_FAULT``（逗号分隔阶段名）触发；
- **只在当前根不是默认生产根时生效**；生产根上即使误设环境变量也只打印警告、不注入；
- 只允许抛错（模拟中断），不执行删除、不改变数据路径；
- 生产代码不使用它，测试用隔离 ``--root`` 配合真实子进程验证恢复。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import DerivedFailure, IOFailure, MemoryError
from .pathutil import path_parts

FAULT_ENV = "MEMORY_TEST_FAULT"
STAGES = (
    "prepared",           # 清单与暂存已落盘，尚未替换任何权威文件
    "applying",           # 已标记 applying，尚未替换第一个目标
    "after_first_replace",# 已替换第一个目标（多目标事务）
    "data_committed",     # 全部数据目标已替换并核对，尚未写凭据
    "before_credential",  # 同上，写凭据之前
    "credential_write",   # 模拟凭据写入失败
    "after_credential",   # 凭据已写，尚未标记 committed
    "committed",          # 已标记 committed，尚未重建派生
    "derived",            # 派生重建失败
    "cleanup",            # 清理暂存文件前
)

_warned_ignored = False


def _same_path(a: Path, b: Path) -> bool:
    return path_parts(a) == path_parts(b)


def active_stages(root: Path, default_root: Path) -> set[str]:
    """返回当前生效的故障阶段集合（生产根恒为空）。"""

    global _warned_ignored
    spec = os.environ.get(FAULT_ENV, "").strip()
    if not spec:
        return set()
    stages = {part.strip() for part in spec.split(",") if part.strip()}
    unknown = stages - set(STAGES)
    stages &= set(STAGES)
    if not stages:
        return set()
    if _same_path(root, default_root):
        if not _warned_ignored:
            _warned_ignored = True
            print(
                f"警告: 环境变量 {FAULT_ENV} 已设置，但当前是默认生产根，忽略测试故障注入。",
                file=sys.stderr,
            )
        return set()
    if unknown:
        print(f"提示: 忽略未知故障阶段: {sorted(unknown)}", file=sys.stderr)
    return stages


def inject(stage: str, root: Path, default_root: Path, *, exit_code: int = 6) -> None:
    """若当前隔离根启用了该阶段，则抛出模拟错误。"""

    if stage not in active_stages(root, default_root):
        return
    message = f"测试故障注入已触发（隔离根，阶段 {stage}）"
    if exit_code == 7:
        raise DerivedFailure(message, details={"stage": stage, "injected": True})
    error: MemoryError = IOFailure(message, details={"stage": stage, "injected": True})
    raise error


__all__ = ["FAULT_ENV", "STAGES", "active_stages", "inject"]
