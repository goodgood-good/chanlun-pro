"""Persist a stopped worker's terminal state before applying run retention."""

import json
import math
import os
import threading
import time

from chanlun.screening.worker_lifetime import process_identity

_LOCK = threading.Lock()


def pid_alive(pid):
    """Return None when permissions/platform errors prevent a reliable answer."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def reconcile_interrupted(directory, state, *, now=None, alive=None):
    stamp = time.time() if now is None else now
    check = pid_alive if alive is None else alive
    if state.get("run_id") != directory.name or state.get("status") not in {
        "starting",
        "running",
    }:
        return state
    heartbeat = state.get("updated_at", state.get("started_at"))
    pid = state.get("worker_pid")
    birth = state.get('worker_process_birth')

    def owner_alive():
        if os.name == 'nt' and isinstance(birth, str) and birth:
            identity = process_identity(pid)
            if identity['alive'] is False:
                return False
            if identity['alive'] is True and isinstance(identity['birth'], str) and identity['birth']:
                return identity['birth'] == birth
            return None
        return check(pid)
    if (
        type(heartbeat) not in (int, float)
        or not math.isfinite(heartbeat)
        or stamp - heartbeat <= 30
        or type(pid) is not int
        or pid <= 0
        or owner_alive() is not False
    ):
        return state
    with _LOCK:
        path = directory / "status.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        # A completion/heartbeat published while checking the process wins.
        if current != state:
            return current
        if owner_alive() is not False:
            return current
        interrupted = {
            **state,
            "status": "interrupted",
            "interruption_detected_at": stamp,
            "error": "选股工作进程已退出，可重新运行",
        }
        from chanlun.screening.runner import write_json

        write_json(path, interrupted, durable=True)
        return interrupted
