"""文件系统写入辅助：唯一临时文件 + flush/fsync/close + os.replace。

只做单文件原子替换；跨文件事务由 tx.py 负责记录与恢复。
Windows 不宣称提供与 POSIX 目录 fsync 等价的物理耐久性保证。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import IOFailure


def _fsync_dir(parent: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """在目标同目录创建唯一临时文件，完整写入后原子替换目标。"""

    path = Path(path)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IOFailure(f"无法创建目录 {parent}: {exc}") from exc

    fd: int | None = None
    tmp: Path | None = None
    replaced = False
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.tmp-")
        tmp = Path(tmp_name)
        handle = os.fdopen(fd, "wb")
        fd = None  # 所有权移交给 handle
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(str(tmp), str(path))
        replaced = True
        tmp = None
    except OSError as exc:
        raise IOFailure(f"原子写入 {path} 失败: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None and not replaced:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    _fsync_dir(parent)


def write_staged_file(path: Path, data: bytes) -> None:
    """事务暂存文件：完整写入并 fsync（不要求原子替换）。"""

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise IOFailure(f"写入事务暂存文件 {path} 失败: {exc}") from exc


def remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IOFailure(f"删除 {path} 失败: {exc}") from exc


__all__ = ["atomic_write_bytes", "write_staged_file", "remove_file_if_exists"]
