"""供只追加研究账本使用的轻量跨进程锁。

Web 进程、定时前向任务和人工诊断都可能访问同一证据文件。
``threading.RLock`` 只能串行化单个解释器内的线程，无法保护跨进程的
读取—修改—写入周期。本模块刻意不包含任何交易或策略语义。
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Iterator


class InterprocessLockTimeout(TimeoutError):
    """其他进程持有证据锁超过期限时抛出。"""


def _try_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def interprocess_file_lock(
    lock_path: str | os.PathLike[str] | Path,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    """在完整的文件读取—修改—写入周期内持有建议锁。"""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("file lock timeouts must be positive")
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        acquired = False
        while not acquired:
            acquired = _try_lock(handle)
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise InterprocessLockTimeout(
                    f"timed out waiting for evidence lock: {path}"
                )
            time.sleep(poll_seconds)
        try:
            yield
        finally:
            _unlock(handle)


__all__ = ("InterprocessLockTimeout", "interprocess_file_lock")
