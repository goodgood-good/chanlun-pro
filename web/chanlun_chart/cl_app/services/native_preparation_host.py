"""Monitor transport adapter; numerical readers and their revision are unchanged."""

import argparse
import datetime

from chanlun.screening.worker_pool import ReusableWorkerPool
from chanlun.screening.preparation_worker import (
    PreparationWorker,
    serve as serve_preparation,
)
from .us_source_rejection_diagnostics import diagnostic_reusable_worker


class NativeRanges:
    def __init__(self, exchange, *, bulk_enabled=False):
        self.exchange = exchange
        self.bulk_enabled = bulk_enabled

    def __getattr__(self, name):
        return getattr(self.exchange, name)

    def download_completed_window(self, codes, period, start, end, *, progress=None):
        # The native bulk callback has not proved reliable under the live
        # monitor workload. Keep production on the measured synchronous range
        # RPC, which releases the shared download lane after every symbol.
        # Bulk is an explicit experiment, never an automatic retry/fallback:
        # its stop RPC does not acknowledge that the shared session is idle.
        if not self.bulk_enabled or len(codes) < 8:
            return self.exchange.download_completed_window(
                codes, period, start, end, progress=progress
            )
        from chanlun.exchange.exchange_qmt import (
            _xtdata_download_interprocess_lock,
            _XTDATA_NATIVE_LOCK,
            qmt_exclusive_download_end,
        )
        from xtquant import xtdata
        from .qmt_completed_batch import download

        if (
            period not in {"1m", "5m"}
            or len(codes) > 100
            or len(set(codes)) != len(codes)
        ):
            raise ValueError("Invalid native preparation batch")
        beginning = datetime.datetime.strptime(start, "%Y%m%d%H%M%S")
        ending = datetime.datetime.strptime(end, "%Y%m%d%H%M%S")
        if beginning.date() != ending.date() or beginning > ending:
            raise ValueError("Invalid native preparation day")
        with _xtdata_download_interprocess_lock(), _XTDATA_NATIVE_LOCK:
            client = xtdata.get_client()
            if callable(getattr(client, "supply_history_data2", None)):
                return download(
                    client,
                    [self.exchange.code_to_qmt(code) for code in codes],
                    period,
                    start,
                    qmt_exclusive_download_end(ending),
                    encode=xtdata._BSON_.BSON.encode,
                    decode=xtdata._BSON_.BSON.decode,
                    progress=progress,
                    timeout=30,
                )
        return self.exchange.download_completed_window(
            codes, period, start, end, progress=progress
        )


def prepare(connection):
    from chanlun.screening import runner
    from chanlun.exchange.exchange_qmt import ExchangeQMT

    runner._EXCHANGE = NativeRanges(ExchangeQMT())
    serve_preparation(connection)


class NativePreparationPool(ReusableWorkerPool):
    def __init__(self, revision, **kwargs):
        kwargs.setdefault("worker_target", diagnostic_reusable_worker)
        super().__init__(revision, **kwargs)

    def get_preparation_worker(self):
        if self.preparation_worker is None:
            self.preparation_worker = PreparationWorker(target=prepare)
        return self.preparation_worker

    def snapshot(self):
        return {**super().snapshot(), "preparation_transport": "scoped-sync-v2"}


def main():
    from chanlun.screening.worker_host import serve

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    serve(args.root, args.generation, pool_factory=NativePreparationPool)


if __name__ == "__main__":
    main()
