"""Offline diagnostics tests, including two fresh Windows spawn interpreters."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.exchange.quote_capacity import QuoteConnectionCancelled
from chanlun.exchange.us_dual_history import DualUSHistory
from chanlun.exchange.errors import SourcePreparationPending
from cl_app.services import us_source_rejection_diagnostics as diagnostics


END = "2026-10-27T20:00:00+00:00"


def _frame(provider="longbridge", frequency="d"):
    result = pd.DataFrame({
        "date": pd.to_datetime(["2026-10-27 19:55Z", "2026-10-27 20:00Z"]),
        "code": "QQQ.US", "frequency": frequency,
        "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 100.0,
    })
    result.attrs["price_basis_provider"] = provider
    return result


def _route(frame):
    source = SimpleNamespace(klines=lambda *_args, **_kwargs: deepcopy(frame), kline_time_label="end")
    return DualUSHistory(source, lambda: source)


@pytest.fixture
def installed(monkeypatch):
    # Register the original before installation so the fixture restores it.
    original = DualUSHistory._read
    monkeypatch.setattr(DualUSHistory, "_read", original)
    assert diagnostics.install_source_rejection_diagnostics()
    return original


@pytest.mark.parametrize("damage,reason", [
    ("basis", "incomplete_price_basis"),
    ("clock", "invalid_history_clock"),
    ("window", "empty_window"),
    ("identity", "invalid_values_or_identity"),
    ("cutoff", "completed_clock_not_reached"),
])
def test_actual_reader_static_rejections_have_exact_safe_reason(installed, caplog, damage, reason):
    frequency = "5m" if damage == "cutoff" else "d"
    frame = _frame(frequency=frequency)
    if damage == "basis":
        frame.attrs["fetch_incomplete"] = True
    elif damage == "clock":
        frame["date"] = frame.date.iloc[0]
    elif damage == "window":
        frame["date"] += pd.Timedelta(days=1)
    elif damage == "identity":
        frame["code"] = "OTHER.US"
    elif damage == "cutoff":
        frame["date"] -= pd.Timedelta(minutes=5)
    with pytest.raises(ValueError):
        _route(frame)._read("longbridge", "QQQ.US", frequency, None, END, {})
    record = caplog.records[-1]
    assert record.getMessage() == (
        f"us_source_rejection provider=longbridge code=QQQ.US frequency={frequency} reason={reason}"
    )
    assert record.exc_info is None and record.stack_info is None


def test_success_passes_through_the_identical_result_and_inputs_without_logging(caplog):
    sentinel, owner, args = object(), object(), {"private": object()}
    inputs = (owner, "usmart", "QQQ.US", "5m", object(), object(), args)
    seen = []

    def original(*received):
        seen.append(received)
        return sentinel

    assert diagnostics._wrap_read(original)(*inputs) is sentinel
    assert len(seen) == 1 and all(a is b for a, b in zip(seen[0], inputs))
    assert not caplog.records


def test_live_missing_tail_keeps_static_reason_without_logging_preparation(installed, caplog):
    value = _frame(frequency="5m")
    value["date"] -= pd.Timedelta(minutes=5)
    route = _route(value)
    route.now = lambda: pd.Timestamp(END).timestamp()+10
    with pytest.raises(SourcePreparationPending) as caught:
        route._read("longbridge", "QQQ.US", "5m", None, END, {})
    assert caught.value.code == "us_completed_clock_pending"
    assert caplog.records[-1].getMessage() == (
        "us_source_rejection provider=longbridge code=QQQ.US frequency=5m reason=completed_clock_not_reached"
    )
    assert caught.value.preparation['progress_token'] not in caplog.text


def test_provider_cannot_impersonate_typed_reader_clock_wait(installed, caplog):
    error = SourcePreparationPending({'private':'secret'}, code='us_completed_clock_pending')
    def provider(*args, **kwargs):
        raise error
    source = SimpleNamespace(klines=provider, kline_time_label='end')
    route = DualUSHistory(source, lambda: source)
    with pytest.raises(SourcePreparationPending) as caught:
        route._read('usmart', 'QQQ.US', '5m', None, END, {})
    assert caught.value is error
    assert caplog.records[-1].args[-1] == 'unclassified_source_failure'
    assert 'secret' not in caplog.text


@pytest.mark.parametrize("error", [
    ValueError("secret-value"), RuntimeError("secret-sdk"),
    QuoteConnectionCancelled("secret-cancel"), asyncio.CancelledError("secret-async"),
    KeyboardInterrupt("secret-interrupt"), SystemExit("secret-exit"),
])
def test_failure_and_cancellation_identity_are_unchanged(error, caplog):
    def original(*_args):
        raise error

    with pytest.raises(type(error)) as caught:
        diagnostics._wrap_read(original)(None, "usmart", "QQQ.US", "5m", None, None, {})
    assert caught.value is error
    assert "secret" not in caplog.text
    if not isinstance(error, Exception):
        assert not caplog.records


@pytest.mark.parametrize("log_error", [OSError("log unavailable"), KeyboardInterrupt("log interrupted")])
def test_logging_failure_cannot_replace_original_failure(monkeypatch, log_error):
    original_error = QuoteConnectionCancelled("cancelled")

    def original(*_args):
        raise original_error

    def broken_log(*_args, **_kwargs):
        raise log_error

    monkeypatch.setattr(diagnostics._LOGGER, "warning", broken_log)
    with pytest.raises(QuoteConnectionCancelled) as caught:
        diagnostics._wrap_read(original)(None, "usmart", "QQQ.US", "5m", None, None, {})
    assert caught.value is original_error


def test_provider_same_text_does_not_impersonate_reader_rejection(installed, caplog):
    error = ValueError("Source history clock is invalid")

    def provider(*_args, **_kwargs):
        raise error

    source = SimpleNamespace(klines=provider, kline_time_label="end")
    route = DualUSHistory(source, lambda: source)
    with pytest.raises(ValueError) as caught:
        route._read("longbridge", "QQQ.US", "5m", None, END, {})
    assert caught.value is error
    assert caplog.records[-1].args[-1] == "unclassified_source_failure"


def test_sdk_text_codes_urls_args_and_invalid_labels_are_never_logged(caplog):
    secret = "credential-secret https://vendor.invalid/?token=private\nforged"
    error = RuntimeError(secret)
    error.code = secret

    def original(*_args):
        raise error

    with pytest.raises(RuntimeError):
        diagnostics._wrap_read(original)(None, secret, secret, secret, secret, secret, {secret: secret})
    record = caplog.records[-1]
    assert record.getMessage() == (
        "us_source_rejection provider=unknown code=unknown frequency=unknown reason=unclassified_source_failure"
    )
    assert record.args == ("unknown", "unknown", "unknown", "unclassified_source_failure")
    assert record.exc_info is None and record.stack_info is None


def test_installation_is_idempotent(installed):
    first = DualUSHistory._read
    assert diagnostics.install_source_rejection_diagnostics()
    assert DualUSHistory._read is first and first.__wrapped__ is installed


def test_install_failure_is_best_effort(monkeypatch):
    def broken_wrap(_original):
        raise RuntimeError("diagnostics unavailable")

    original = DualUSHistory._read
    monkeypatch.setattr(diagnostics, "_wrap_read", broken_wrap)
    assert diagnostics.install_source_rejection_diagnostics() is False
    assert DualUSHistory._read is original


def test_fallback_selection_and_route_state_are_unchanged(tmp_path, monkeypatch):
    results, books, calls = [], [], []
    original = DualUSHistory._read
    monkeypatch.setattr(DualUSHistory, "_read", original)
    for installed in (False, True):
        if installed:
            assert diagnostics.install_source_rejection_diagnostics()
        seen = []

        def lb(*_args, **_kwargs):
            seen.append("longbridge")
            return None

        def us(*_args, **_kwargs):
            seen.append("usmart")
            return _frame("usmart")

        route = DualUSHistory(
            SimpleNamespace(klines=us, kline_time_label="end"),
            lambda: SimpleNamespace(klines=lb, kline_time_label="end"),
            root=tmp_path / str(installed), now=lambda: 12345,
        )
        results.append(route.klines("QQQ.US", "d", end_date=END))
        books.append(route._book())
        calls.append(seen)
    pd.testing.assert_frame_equal(*results)
    assert results[0].attrs == results[1].attrs
    assert calls == [["longbridge", "usmart"], ["longbridge", "usmart"]]
    for book in books:
        for provider in book["providers"].values():
            provider.pop("seconds")
    assert books[0] == books[1]


def test_pool_default_target_and_explicit_override():
    from cl_app.services.native_preparation_host import NativePreparationPool
    from chanlun.screening.worker_pool import reusable_worker

    default = NativePreparationPool("offline-test-revision")
    explicit = NativePreparationPool("offline-test-revision", worker_target=reusable_worker)
    try:
        assert default.worker_target is diagnostics.diagnostic_reusable_worker
        assert explicit.worker_target is reusable_worker
    finally:
        default.close()
        explicit.close()


class _SpawnProbeConnection:
    """Runs only inside the real target's unchanged recv/close loop."""

    def __init__(self, reply):
        self.reply = reply

    def recv(self):
        source = SimpleNamespace(klines=lambda *_args, **_kwargs: None, kline_time_label="end")
        route = DualUSHistory(source, lambda: source)
        failures = []
        for provider in ("longbridge", "usmart"):
            try:
                route._read(provider, "QQQ.US", "5m", None, END, {"secret": "never-log-this"})
            except ValueError:
                failures.append(provider)
        self.reply.send({"pid": os.getpid(), "installed": getattr(DualUSHistory._read, diagnostics._MARKER, False),
                         "providers": failures})
        return None

    def close(self):
        self.reply.close()


@pytest.mark.skipif(os.name != "nt", reason="verifies the deployed Windows spawn and stderr path")
def test_two_fresh_spawned_compute_targets_install_and_log_to_inherited_stderr(tmp_path):
    project = Path(__file__).resolve().parents[2]
    script = """
import json
import multiprocessing
from cl_app.services.native_preparation_host import NativePreparationPool
from tests.web.test_us_source_rejection_diagnostics import _SpawnProbeConnection

if __name__ == '__main__':
    context = multiprocessing.get_context('spawn')
    pool = NativePreparationPool('offline-test-revision')
    children = []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process = context.Process(target=pool.worker_target, args=(_SpawnProbeConnection(child),))
            process.start()
            child.close()
            children.append((process, parent))
        records = []
        for process, parent in children:
            assert parent.poll(30), 'child did not reach original receive loop'
            records.append(parent.recv())
            process.join(30)
            assert process.exitcode == 0
        print(json.dumps(records))
    finally:
        for process, parent in children:
            if process.is_alive():
                process.terminate()
                process.join(10)
            parent.close()
        pool.close()
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, [project, project / "src", project / "web/chanlun_chart"])),
           "CHANLUN_TESTING": "1", "CHANLUN_TEST_DATA_PATH": str(tmp_path / "data"),
           "CHANLUN_LOG_DIR": str(tmp_path / "logs")}
    with (tmp_path / "worker_host.log").open("w+", encoding="utf8") as log:
        completed = subprocess.run([sys.executable, "-c", script], cwd=project, env=env,
                                   stdout=subprocess.PIPE, stderr=log, text=True, timeout=90)
        log.seek(0)
        logged = log.read()
    assert completed.returncode == 0, logged
    records = json.loads(completed.stdout.strip().splitlines()[-1])
    assert len({row["pid"] for row in records}) == 2
    assert all(row["installed"] and row["providers"] == ["longbridge", "usmart"] for row in records)
    lines = [line for line in logged.splitlines() if "us_source_rejection" in line]
    assert len(lines) == 4
    for provider in ("longbridge", "usmart"):
        expected = f"us_source_rejection provider={provider} code=QQQ.US frequency=5m reason=incomplete_price_basis"
        assert lines.count(expected) == 2
    assert "never-log-this" not in logged
