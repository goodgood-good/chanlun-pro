"""A small fixed-size executor whose workers do not block interpreter exit."""

import queue
import threading
from concurrent.futures import Executor, Future


class DaemonExecutor(Executor):
    """Executor-compatible daemon worker pool with explicit shutdown."""

    def __init__(self, max_workers: int, thread_name_prefix: str, max_pending=None):
        workers = max(1, int(max_workers))
        pending = workers * 4 if max_pending is None else max(1, int(max_pending))
        self._queue = queue.Queue()
        self._pending_slots = threading.BoundedSemaphore(pending)
        self._lock = threading.Lock()
        self._closed = False
        self._threads = []
        try:
            for index in range(workers):
                thread = threading.Thread(
                    target=self._worker,
                    daemon=True,
                    name=f"{thread_name_prefix}-{index}",
                )
                self._threads.append(thread)
                thread.start()
        except BaseException:
            self._closed = True
            live_threads = [thread for thread in self._threads if thread.is_alive()]
            for _ in live_threads:
                self._queue.put(None)
            for thread in live_threads:
                thread.join()
            raise

    def _worker(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args, kwargs = item
                self._pending_slots.release()
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def submit(self, fn, *args, **kwargs):
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if not self._pending_slots.acquire(blocking=False):
                raise RuntimeError("executor pending queue is full")
            self._queue.put((future, fn, args, kwargs))
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        with self._lock:
            first_shutdown = not self._closed
            self._closed = True
        if first_shutdown:
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if item is not None:
                            self._pending_slots.release()
                            item[0].cancel()
                    finally:
                        self._queue.task_done()
            for _ in self._threads:
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()
