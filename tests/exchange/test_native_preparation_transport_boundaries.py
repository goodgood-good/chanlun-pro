import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cl_app.services import qmt_completed_batch
from cl_app.services.native_preparation_host import NativeRanges


def invoke(client, *, progress=None, timeout=1):
    return qmt_completed_batch.download(
        client, ["one", "two"], "5m", "start", "end",
        encode=json.dumps, decode=json.loads, progress=progress, timeout=timeout,
    )


def acknowledge(callback):
    callback({"finished": 1, "total": 2, "message": "one"})
    callback({"finished": 2, "total": 2, "message": "two"})


def test_native_rpc_elapsed_time_is_charged_before_accepting_completion(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(qmt_completed_batch.time, "monotonic", lambda: clock[0])

    def supply(*args):
        clock[0] = 12.0
        acknowledge(args[-1])

    client = SimpleNamespace(
        supply_history_data2=supply, stop_supply_history_data2=Mock()
    )
    with pytest.raises(TimeoutError):
        invoke(client)
    client.stop_supply_history_data2.assert_called_once_with()


def test_progress_does_not_hold_callback_lock_or_execute_on_sdk_callback(monkeypatch):
    caller_thread = threading.get_ident()
    callbacks = []
    progress_values = []

    def supply(*args):
        callback = args[-1]
        callbacks.append(callback)
        thread = threading.Thread(
            target=lambda: callback({"finished": 1, "total": 2, "message": "one"})
        )
        thread.start()
        thread.join(1)
        assert not thread.is_alive(), "the SDK callback must not wait for progress"

    def progress(done, total):
        assert threading.get_ident() == caller_thread
        progress_values.append((done, total))
        if done == 1:
            thread = threading.Thread(
                target=lambda: callbacks[0](
                    {"finished": 2, "total": 2, "message": "two"}
                )
            )
            thread.start()
            thread.join(1)
            assert not thread.is_alive(), "progress must not hold the callback lock"

    client = SimpleNamespace(
        supply_history_data2=supply, stop_supply_history_data2=Mock()
    )
    assert invoke(client, progress=progress, timeout=5)
    assert progress_values == [(1, 2), (2, 2)]
    client.stop_supply_history_data2.assert_not_called()


def test_slow_progress_cannot_turn_an_expired_result_into_a_receipt(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(qmt_completed_batch.time, "monotonic", lambda: clock[0])
    client = SimpleNamespace(
        supply_history_data2=lambda *args: acknowledge(args[-1]),
        stop_supply_history_data2=Mock(),
    )

    def progress(*args):
        clock[0] += 2

    with pytest.raises(TimeoutError):
        invoke(client, progress=progress)
    client.stop_supply_history_data2.assert_called_once_with()


@pytest.mark.parametrize("size", [1, 8, 64, 100])
def test_production_default_downloads_every_symbol_via_original_sync_ranges(size):
    codes = [f"SH.{index:06d}" for index in range(size)]
    exchange = SimpleNamespace(download_completed_window=Mock(return_value=True))
    progress = Mock()
    assert NativeRanges(exchange).download_completed_window(
        codes, "1m", "20261028000000", "20261028150000", progress=progress
    )
    exchange.download_completed_window.assert_called_once_with(
        codes, "1m", "20261028000000", "20261028150000", progress=progress
    )


def test_sync_failure_and_cancellation_propagate_without_retry():
    for error in (TimeoutError("native RPC failed"), KeyboardInterrupt()):
        exchange = SimpleNamespace(download_completed_window=Mock(side_effect=error))
        with pytest.raises(type(error)):
            NativeRanges(exchange).download_completed_window(
                ["SH.600000"] * 8, "1m", "start", "end"
            )
        assert exchange.download_completed_window.call_count == 1


def test_callbacks_after_failure_cannot_report_progress_or_prove_completion():
    callbacks = []
    progress = Mock()
    client = SimpleNamespace(
        supply_history_data2=lambda *args: callbacks.append(args[-1]),
        stop_supply_history_data2=Mock(),
    )
    with pytest.raises(TimeoutError):
        invoke(client, progress=progress, timeout=0.001)
    assert callbacks[0]({"finished": 2, "total": 2, "result": json.dumps({
        "one": {}, "two": {}
    })}) is True
    progress.assert_not_called()
