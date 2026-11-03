"""OS-enforced lifetime for the Windows coordinator's own child processes."""
import ctypes
from ctypes import wintypes
import os


def prefer_analysis_performance(process_handle=None):
    """Use all suitable hybrid cores without raising process scheduling priority.

    Windows may infer a low QoS for hidden, BelowNormal workers and confine
    expensive calculations to efficient cores. Explicit HighQoS only opts out
    of execution-speed throttling; CPU affinity and priority remain unchanged.
    https://learn.microsoft.com/windows/win32/procthread/quality-of-service
    """
    if os.name != 'nt':
        return {'state':'not_applicable'}
    class Power(ctypes.Structure):
        _fields_ = [('Version',wintypes.ULONG),('ControlMask',wintypes.ULONG),('StateMask',wintypes.ULONG)]
    try:
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetProcessInformation.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.SetProcessInformation.restype = wintypes.BOOL
        handle = kernel.GetCurrentProcess() if process_handle is None else process_handle
        state = Power(1,1,0)  # CURRENT_VERSION, EXECUTION_SPEED control, throttling off.
        if kernel.SetProcessInformation(handle,4,ctypes.byref(state),ctypes.sizeof(state)):
            return {'state':'high_qos','priority_changed':False,'affinity_changed':False}
        return {'state':'unavailable','error':ctypes.get_last_error()}
    except (AttributeError,OSError,TypeError,ValueError) as exc:
        return {'state':'unavailable','error_type':type(exc).__name__}


def process_identity(pid):
    """Read liveness and creation identity through the same Windows handle.

    Unknown access/inspection failures remain unknown, rather than proving exit.
    Creation FILETIME is stored as hex to preserve all 64 bits in JSON readers.
    """
    unknown = {'alive': None, 'birth': None}
    if os.name != 'nt' or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return unknown
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return {'alive': False, 'birth': None} if ctypes.get_last_error() == 87 else unknown
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return unknown
        if code.value != 259:
            return {'alive': False, 'birth': None}
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return {'alive': True, 'birth': None}
        birth = times[0]
        return {'alive': True, 'birth': f'{birth.dwHighDateTime:08x}{birth.dwLowDateTime:08x}'}
    finally:
        kernel.CloseHandle(handle)


class WindowsWorkerJob:
    """A non-inheritable job handle; its closure kills only assigned children.

    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Windows worker lifetime protection is unavailable')
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                        ('max_working', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                'read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Io), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        self.owner_power_policy = prefer_analysis_performance()

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, process.sentinel):
            raise ctypes.WinError(ctypes.get_last_error())
        return prefer_analysis_performance(process.sentinel)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
