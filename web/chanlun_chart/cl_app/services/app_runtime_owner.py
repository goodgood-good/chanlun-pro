"""Cross-platform process ownership helpers for app-owned runtimes."""

from __future__ import annotations

import os


def pid_alive(pid: object) -> bool:
    """Return whether *pid* identifies a live process without signalling it."""

    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
# Windows 下 ``os.kill(pid, 0)`` 可能走控制台控制路径，并对已分离进程或图形界面
# 进程报告 WinError 87/SystemError。
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
        # 即使访问被拒绝，也足以证明受保护进程仍然存在。
            return ctypes.get_last_error() == access_denied
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
        # 活跃句柄却无法读取退出码时状态不明确；此时安全关闭，避免两个应用进程
        # 共用同一个所有者身份。
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError, ValueError):
        return False
    return True


__all__ = ("pid_alive",)
