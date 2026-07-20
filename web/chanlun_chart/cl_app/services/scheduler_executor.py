"""Restartable APScheduler executor backed by daemon worker threads."""

from concurrent.futures import CancelledError

from apscheduler.executors.base import run_job
from apscheduler.executors.pool import BasePoolExecutor

from chanlun.tools.daemon_executor import DaemonExecutor


class RestartableDaemonPoolExecutor(BasePoolExecutor):
    """Keep scheduler jobs restartable without pinning interpreter shutdown."""

    def __init__(self, max_workers=10, max_pending=None):
        self._max_workers = max(1, int(max_workers))
        self._max_pending = (
            self._max_workers * 4
            if max_pending is None
            else max(1, int(max_pending))
        )
        super().__init__(None)

    def start(self, scheduler, alias):
        created = None
        if self._pool is None:
            created = DaemonExecutor(
                max_workers=self._max_workers,
                thread_name_prefix=f"Scheduler-{alias}",
                max_pending=self._max_pending,
            )
            self._pool = created
        try:
            super().start(scheduler, alias)
        except BaseException as start_error:
            if created is not None and self._pool is created:
                self._pool = None
                try:
                    created.shutdown(wait=True, cancel_futures=True)
                except BaseException as cleanup_error:
                    raise start_error from cleanup_error
            raise

    def _do_submit_job(self, job, run_times):
        def callback(future):
            if future.cancelled():
                self._run_job_error(job.id, CancelledError())
                return
            exception = future.exception()
            if exception is not None:
                self._run_job_error(
                    job.id,
                    exception,
                    getattr(exception, "__traceback__", None),
                )
            else:
                self._run_job_success(job.id, future.result())

        future = self._pool.submit(
            run_job,
            job,
            job._jobstore_alias,
            run_times,
            self._logger.name,
        )
        future.add_done_callback(callback)

    def shutdown(self, wait=True):
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=not wait)
