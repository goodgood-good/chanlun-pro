from datetime import datetime
import os
import pandas as pd
import pytest

from chanlun.screening.preparation_worker import PreparationWorker
from chanlun.screening import tail_preparation as prep, runner
from chanlun.screening.rules import CN


class FixtureExchange:
    def download_completed_window(self, codes, period, start, end, **kwargs):
        from pathlib import Path

        with Path(os.environ["CHANLUN_PREPARATION_TEST_LOG"]).open(
            "a", encoding="utf-8"
        ) as log:
            log.write(period + " " + end + "\n")
        return True

    def klines(self, code, frequency, *, end_date, **kwargs):
        return pd.DataFrame(
            {
                "code": [code],
                "frequency": [frequency],
                "date": [pd.Timestamp(end_date, tz=CN)],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
            }
        )


def fixture_server(connection):
    from chanlun.screening.preparation_worker import serve

    runner._EXCHANGE = FixtureExchange()
    prep.validate_frame = lambda frame, context: []
    serve(connection)


def test_real_preparation_protocol_reuses_a_child_and_revalidates_a_newer_range(
    tmp_path, monkeypatch
):
    logfile = tmp_path / "native_downloads.txt"
    monkeypatch.setenv("CHANLUN_PREPARATION_TEST_LOG", str(logfile))
    worker = PreparationWorker(target=fixture_server)
    pids = []
    proofs = []
    try:
        for minute in (6, 5):
            cutoff = int(datetime(2026, 10, 28, 13, minute, tzinfo=CN).timestamp())
            stock = {
                "market": "a",
                "code": "SH.600000",
                "name": "fixture",
                "_monitor_periods": ["1m"],
            }
            messages = list(
                prep.prepare_inputs(
                    [stock],
                    {"frequencies": ["1m"]},
                    {"1m": {"cutoff": cutoff}},
                    tmp_path / "cache",
                    tmp_path,
                    worker=worker,
                )
            )
            assert messages[-1]["state"] == "completed" and messages[-1]["failed"] == 0
            pids.append(worker.process.pid)
            proofs.append(stock["_completed_inputs"]["1m"])
        assert pids[0] == pids[1] and worker.started_processes == 1
        assert (
            proofs[0]["cutoff"] > proofs[1]["cutoff"]
            and proofs[0]["input_key"] != proofs[1]["input_key"]
        )
        assert len(logfile.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        worker.close()


def echo(connection):
    try:
        while (message := connection.recv()) is not None:
            identity, batches = message
            connection.send((identity, {"pid": os.getpid(), "batch": batches}))
            connection.send((identity, {"done": True}))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


def test_preparation_process_is_reused_and_cancelled_owner_is_never_reused():
    worker = PreparationWorker(target=echo)
    try:
        worker.start(["one"])
        first = worker.recv()
        assert worker.recv() == {"done": True}
        worker.finish(successful=True)
        worker.start(["two"])
        second = worker.recv()
        assert worker.recv() == {"done": True}
        assert first["pid"] == second["pid"] and second["batch"] == ["two"]
        worker.finish(successful=False)
        worker.start(["new"])
        third = worker.recv()
        assert worker.recv() == {"done": True}
        assert third["pid"] != first["pid"] and worker.started_processes == 2
        worker.finish(successful=True)
    finally:
        worker.close()
    assert worker.process is None


def test_late_reply_from_previous_preparation_is_rejected():
    worker = PreparationWorker(target=echo)
    try:
        worker.start(["one"])
        worker.recv()
        worker.finish(successful=True)  # Deliberately leave an old done message.
        worker.start(["two"])
        with pytest.raises(ValueError, match="another request"):
            worker.recv()
        assert worker.process is None
    finally:
        worker.close()


def test_newer_acknowledged_range_reuses_download_but_rechecks_older_actual_tail(
    monkeypatch,
):
    calls = []
    reads = []

    class Exchange:
        def download_completed_window(self, *args, **kwargs):
            calls.append(args)
            return True

        def klines(self, code, frequency, *, end_date, **kwargs):
            stamp = pd.Timestamp(end_date, tz=CN)
            reads.append(stamp)
            return pd.DataFrame(
                {
                    "code": [code],
                    "frequency": [frequency],
                    "date": [stamp],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1.0],
                }
            )

    class Connection:
        def __init__(self):
            self.receipts = {}
            self.values = []

        def send(self, value):
            self.values.append(value)

        def close(self):
            pass

    monkeypatch.setattr(runner, "_EXCHANGE", Exchange())
    checked = []

    def validate(frame, context):
        checked.append(context["cutoff"])
        return (
            []
            if int(frame.date.iloc[-1].timestamp()) == context["cutoff"]
            else ["BAD_CLOCK"]
        )

    monkeypatch.setattr(prep, "validate_frame", validate)
    connection = Connection()
    keys = []
    for minute in (6, 5, 7):
        cutoff = int(datetime(2026, 10, 28, 13, minute, tzinfo=CN).timestamp())
        batch = (["SH.600000"], "1m", cutoff, {"SH.600000": {"1m": {"cutoff": cutoff}}})
        prep._prepare_worker(connection, [batch])
        ready = next(row for row in reversed(connection.values) if "ready" in row)
        keys.append(ready["proofs"]["SH.600000"]["1m"])
    assert len(calls) == 2 and len(reads) == len(checked) == 3
    assert reads[1].minute == 5 and keys[0] != keys[1]
