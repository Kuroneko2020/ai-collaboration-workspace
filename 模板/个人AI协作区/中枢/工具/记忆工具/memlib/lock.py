"""跨进程协调锁：固定锁文件 + 操作系统字节锁。

- Windows：``os.open(O_RDWR|O_CREAT)`` + ``msvcrt.locking(fd, LK_NBLCK, 1)``；
- POSIX：``fcntl.flock(fd, LOCK_EX|LOCK_NB)``；
- ``time.monotonic()`` 控制总等待时间，只对确认是锁占用的错误短间隔重试；
- 文件描述符不可继承；释放时 seek(0)/解锁/关闭；锁文件不删除。

该锁只协调遵守本协议的进程，不能阻止任意编辑器绕过 CLI 直接改文件。
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from types import TracebackType

from .errors import IOFailure, LockTimeoutError


def _is_lock_contention(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    contention_errnos = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EBUSY}
    if winerror is not None:
        # 33=ERROR_LOCK_VIOLATION, 36=ERROR_SHARING_BUFFER_EXCEEDED（Windows 资源占用类）
        if int(winerror) in (33, 36):
            return True
    return exc.errno in contention_errnos


class MemoryLock:
    """固定锁文件上的短时独占锁。"""

    def __init__(self, lock_path: Path, timeout: float = 5.0) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = float(timeout)
        self.fd: int | None = None
        self._locked = False

    # -- 上下文协议 -----------------------------------------------------
    def __enter__(self) -> "MemoryLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- 实现 -----------------------------------------------------------
    def acquire(self) -> None:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IOFailure(f"无法创建锁文件目录 {self.lock_path.parent}: {exc}") from exc

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(str(self.lock_path), flags, 0o666)
        except OSError as exc:
            raise IOFailure(f"无法打开锁文件 {self.lock_path}: {exc}") from exc
        try:
            os.set_inheritable(fd, False)
        except (AttributeError, OSError):
            pass

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_acquire(fd)
                self.fd = fd
                self._locked = True
                return
            except OSError as exc:
                if not _is_lock_contention(exc):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise IOFailure(f"获取锁失败（非占用类错误） {self.lock_path}: {exc}") from exc
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise LockTimeoutError(
                        f"等待记忆锁超时（{self.timeout:g} 秒）: {self.lock_path}"
                    ) from exc
                time.sleep(0.02)

    def release(self) -> None:
        fd = self.fd
        if fd is None:
            return
        try:
            if self._locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self.fd = None
            self._locked = False


def _try_acquire(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


__all__ = ["MemoryLock"]
