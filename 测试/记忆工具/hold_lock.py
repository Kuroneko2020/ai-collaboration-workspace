# -*- coding: utf-8 -*-
"""测试辅助：持锁子进程（父进程强杀后验证内核锁释放）。

用法：python hold_lock.py <工具目录> <锁文件路径> [持锁秒数]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def main() -> int:
    tool_dir = Path(sys.argv[1]).resolve()
    lock_path = Path(sys.argv[2]).resolve()
    hold_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tool_dir))
    from memlib.lock import MemoryLock

    lock = MemoryLock(lock_path, timeout=5.0)
    lock.acquire()
    print("LOCKED", flush=True)
    try:
        time.sleep(hold_seconds)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
