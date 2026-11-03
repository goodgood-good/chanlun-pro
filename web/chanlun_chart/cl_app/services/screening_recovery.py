"""Observe coordinator exits and resume verified work without replacing its run."""
import ctypes
from ctypes import wintypes
import hashlib
import logging
import os
import threading
import time

from chanlun.screening.runner import write_json
from chanlun.screening.state_io import read_state_json
from chanlun.screening.worker_lifetime import process_identity
from chanlun.persistence.file_lock import interprocess_file_lock
from .screening_lifecycle import reconcile_interrupted
from .screening_resume import start_resume

LOG = logging.getLogger(__name__)


class ExitObserver:
    """Keep a query handle so an exited Windows process still has an exit code."""
    def __init__(self, pid, birth):
        self.handle = None
        if os.name != 'nt' or not birth or process_identity(pid) != {'alive': True, 'birth': birth}:
            return
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000, False, pid)
        if self.handle and process_identity(pid) != {'alive': True, 'birth': birth}:
            self.close()

    def code(self):
        code = wintypes.DWORD()
        if self.handle and self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)) and code.value != 259:
            return code.value
        return None

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ScreeningRecovery:
    def __init__(self, manager, *, now=time.time, observer=ExitObserver, resume=start_resume):
        self.manager, self.now, self.observer_factory, self.resume = manager, now, observer, resume
        self.stop_event = threading.Event()
        self.thread = None
        self.observed = None
        self.observer = None
        self.retry_after = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.manager._automatic_recovery = self
        self.thread = threading.Thread(target=self._loop, name='screening-recovery', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if getattr(self.manager,'_automatic_recovery',None) is self:
            self.manager._automatic_recovery = None

    def pending(self, state):
        return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()
                    and state.get('status') == 'interrupted' and state.get('source_current') is True
                    and not state.get('cancel_requested') and not state.get('recovery_error'))

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    LOG.exception('Screening recovery check failed')
                    self.retry_after = self.now()+30
                self.stop_event.wait(2)
        finally:
            if self.observer:
                self.observer.close()

    def tick(self):
        if self.stop_event.is_set() or self.now() < self.retry_after:
            return
        directory = self.manager._directory()
        if directory is None:
            return
        path = directory/'status.json'
        state = read_state_json(path)
        pid, birth = state.get('worker_pid'), state.get('worker_process_birth')
        identity = directory.name, pid, birth
        if identity != self.observed:
            if self.observer:
                self.observer.close()
            self.observed = identity
            self.observer = self.observer_factory(pid, birth) if type(pid) is int else None
        exit_code = self.observer.code() if self.observer else None
        receipt = directory/('worker_exit.'+hashlib.sha256(f'{pid}:{birth}'.encode()).hexdigest()[:16]+'.json')
        if exit_code is not None and not receipt.exists():
            detail = {
                'run_id': directory.name, 'pid': pid, 'birth': birth, 'observed_at': self.now(),
                'exit_code': exit_code, 'exit_code_hex': f'0x{exit_code:08X}',
                'completed': state.get('completed'), 'total': state.get('total'), 'phase': state.get('phase'),
            }
            write_json(receipt, detail, durable=True)
            write_json(directory/'worker_exit.json', detail, durable=True)
        state = reconcile_interrupted(directory, state, now=self.now())
        if state.get('status') != 'interrupted' or (directory/'cancel').exists():
            return
        recent = [stamp for stamp in state.get('automatic_resume_times', [])
                  if type(stamp) in (int, float) and self.now()-stamp < 600]
        if len(recent) >= 3:
            self._record_error(path, state, '十分钟内已自动恢复三次，保留结果并等待排查，未从头重算')
            return
        try:
            self.resume(self.manager, automatic=True)
            LOG.info('Resumed interrupted screening %s from %s/%s', directory.name,
                     state.get('completed'), state.get('total'))
        except (OSError, ValueError, RuntimeError) as exc:
            self._record_error(path, state, str(exc))
        self.retry_after = self.now()+10

    def _record_error(self, path, expected, message):
        self.retry_after = self.now()+30
        # A manual restart or a recovery that already advanced state wins.
        with interprocess_file_lock(self.manager.root/'worker_dispatch.lock', timeout_seconds=1):
            if read_state_json(path) == expected and expected.get('recovery_error') != message:
                write_json(path, {**expected, 'recovery_error': message}, durable=True)

