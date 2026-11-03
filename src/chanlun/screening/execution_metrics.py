"""Cheap process I/O counters alongside monotonic wall and process CPU time."""

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os


class Counters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


@lru_cache(maxsize=1)
def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
    kernel.GetProcessIoCounters.restype = wintypes.BOOL
    return kernel


def io_counters():
    if os.name != "nt":
        return None
    try:
        kernel = _kernel()
        value = Counters()
        if not kernel.GetProcessIoCounters(
            kernel.GetCurrentProcess(), ctypes.byref(value)
        ):
            return None
        return {name: getattr(value, name) for name, _ in Counters._fields_}
    except (OSError, ValueError, AttributeError):
        return None


def io_difference(before, after):
    return (
        {key: max(0, after[key] - value) for key, value in before.items()}
        if before is not None and after is not None
        else None
    )
