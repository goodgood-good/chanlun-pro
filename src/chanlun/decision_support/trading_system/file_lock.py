"""Small cross-process lock used by append-only research ledgers.

The web process, scheduled forward runner and manual diagnostics can all touch
the same evidence files.  ``threading.RLock`` only serializes threads inside
one interpreter, so it cannot protect a read-modify-write cycle across those
processes.  This module intentionally has no trading or strategy semantics.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Iterator


class InterprocessLockTimeout(TimeoutError):
    """Raised when another process keeps an evidence lock past the deadline."""


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
    """Hold one advisory lock for a complete file read-modify-write cycle."""

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
