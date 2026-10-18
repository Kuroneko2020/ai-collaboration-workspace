#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆工具稳定入口：``py -3 -X utf8 memory.py <子命令>``。

可从任意工作目录运行；默认工作区根由本文件位置推算（parents[3]），
并读取/校验 ``根/中枢/记忆/配置.json``。隔离测试或迁移根用全局 ``--root``。
错误的根目录不会被自动初始化。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证从任意 cwd 运行时能导入同目录的 memlib 包（不依赖安装或 PYTHONPATH）
_TOOL_DIR = Path(__file__).resolve().parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

from memlib.cli import main as _main  # noqa: E402

#: memory.py -> 记忆工具 -> 工具 -> 中枢 -> 工作区根
DEFAULT_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    return _main(argv, default_root=DEFAULT_ROOT)


if __name__ == "__main__":
    sys.exit(main())
