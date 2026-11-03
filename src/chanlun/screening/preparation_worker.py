"""One monitor-owned native preparation process, with request-scoped messages."""

import multiprocessing
import os
from datetime import datetime
from uuid import uuid4


class _Reply:
    def __init__(self, connection, identity, receipts):
        self.connection, self.identity, self.receipts = connection, identity, receipts

    def send(self, value):
        self.connection.send((self.identity, value))

    def close(self):
        pass


def serve(connection):
    from .tail_preparation import _prepare_worker

    receipts = {}
    try:
        while (message := connection.recv()) is not None:
            identity, batches = message
            days = {key[2] for key in receipts}
            from .rules import CN

            wanted = {
                datetime.fromtimestamp(batch[2], CN).date().isoformat()
                for batch in batches
            }
            if not days <= wanted:
                receipts.clear()
            _prepare_worker(_Reply(connection, identity, receipts), batches)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class PreparationWorker:
    def __init__(self, *, target=serve):
        self.target = target
        self.process = None
        self.connection = None
        self.job = None
        self.identity = None
        self.closed = False
        self.started_processes = 0
        self._retiring_pid = self._retiring_birth = None

    def start(self, batches):
        if self.closed or self.identity is not None:
            raise RuntimeError("Preparation worker is unavailable")
        if self.process is not None and not self.process.is_alive():
            self._retire()
        if self.process is None:
            ctx = multiprocessing.get_context("spawn")
            parent, child = ctx.Pipe()
            self.process = ctx.Process(target=self.target, args=(child,))
            self.connection = parent
            try:
                self.process.start()
                child.close()
                if os.name == "nt":
                    from .worker_lifetime import WindowsWorkerJob

                    self.job = WindowsWorkerJob()
                    self.job.assign(self.process)
            except BaseException:
                child.close()
                self._retire()
                raise
            self.started_processes += 1
        self.identity = uuid4().hex
        try:
            self.connection.send((self.identity, batches))
        except BaseException:
            self._retire()
            raise
        return self

    def poll(self, timeout=0):
        return self.connection.poll(timeout)

    def recv(self):
        identity, value = self.connection.recv()
        if identity != self.identity:
            self._retire()
            raise ValueError("Preparation reply belongs to another request")
        return value

    def finish(self, *, successful):
        if successful:
            self.identity = None
        else:
            self._retire()

    def _retire(self):
        from .runner import _stop_worker
        from .worker_lifetime import process_identity

        # Capture identity before Process.close() invalidates its pid property.
        # A failed teardown keeps the exact process/job handles for close retry.
        if self.process is not None and self._retiring_pid is None:
            self._retiring_pid = self.process.pid
            if self._retiring_pid is not None:
                self._retiring_birth = process_identity(self._retiring_pid).get('birth')
        try:
            if self.job is not None:
                self.job.close()
            if self.process is not None:
                try:
                    pid = self.process.pid
                except ValueError:
                    current = process_identity(self._retiring_pid)
                    if not (current.get('alive') is False or self._retiring_birth
                            and current.get('birth') and current['birth'] != self._retiring_birth):
                        raise RuntimeError('Preparation owner exit remains unconfirmed')
                else:
                    if pid is not None:
                        _stop_worker({'process': self.process, 'connection': self.connection,
                                      'stock': self.identity})
                    else:
                        self.process.close()
            if self.connection is not None:
                self.connection.close()
        except BaseException:
            self.closed = True
            raise
        self.process = self.connection = self.job = self.identity = None
        self._retiring_pid = self._retiring_birth = None

    def close(self):
        self.closed = True
        self._retire()
