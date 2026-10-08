"""Hourly background input retention, separate from screening and delivery."""

import threading
import time

from chanlun.screening.cache_retention import prune_inputs
from chanlun.tools.log_util import LogUtil


class InputRetention:
    def __init__(self):
        self.thread = None
        self.next_run = 0
        self.stop = threading.Event()

    def schedule(self, roots, *, runs_root=None, protected_runs=None):
        if (
            self.stop.is_set()
            or time.time() < self.next_run
            or self.thread
            and self.thread.is_alive()
        ):
            return
        self.next_run = time.time() + 3600

        def run():
            for root in roots:
                if self.stop.is_set():
                    return
                try:
                    result = prune_inputs(root, stop=self.stop)
                    if result["removed"] or result["error"]:
                        LogUtil.info("screening input retention: %s %s", root, result)
                except Exception:
                    LogUtil.exception("screening input retention failed")
            if runs_root is not None and not self.stop.is_set():
                try:
                    from .screening_retention import prune_runs
                    from chanlun.screening.runner import write_json
                    result=prune_runs(runs_root,protected_runs,stop=self.stop)
                    write_json(runs_root/'retention_status.json',result)
                    if result['removed'] or result['error']:
                        LogUtil.info('screening run retention: %s',result)
                except Exception:
                    LogUtil.exception('screening run retention failed')

        self.thread = threading.Thread(
            target=run, name="ScreeningInputRetention", daemon=True
        )
        self.thread.start()
