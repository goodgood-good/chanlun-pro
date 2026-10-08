"""Storage maintenance cannot retire current signals; interpretation changes can."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from cl_app.services import screening, screening_result_storage as storage
from tests.web.test_screening_results_cache import cached_run as cached_run
from tests.web.test_signal_monitor import setup as setup, _seed, signal


def change_storage_source(monkeypatch):
    target = Path(storage.__file__).resolve()
    original_read = Path.read_bytes
    original = original_read(target)
    changed = original.replace(b"compresslevel=3", b"compresslevel=2")
    assert changed != original
    monkeypatch.setattr(Path, "read_bytes", lambda path: changed if path.resolve() == target else original_read(path))
    revision = storage.storage_revision()
    assert revision != storage.REVISION
    monkeypatch.setattr(storage, "REVISION", revision)


def test_storage_update_rebuilds_disk_cache_without_changing_interpretation(cached_run, monkeypatch):
    manager = cached_run
    before = manager.results(persist=True)
    reader_revision = screening._result_reader_revision()
    before_key = manager._results_cache_key(("same-input",))
    change_storage_source(monkeypatch)
    assert screening._result_reader_revision() == reader_revision
    assert manager._results_cache_key(("same-input",)) != before_key

    reopened = screening.ScreeningManager(manager.root)
    read = Mock(wraps=reopened._read_results)
    monkeypatch.setattr(reopened, "_read_results", read)
    after = reopened.results()
    assert read.call_count == 1, "a storage change must not silently reuse the old serialized cache"
    for key in ("selected", "errors", "processed_symbols", "result_reader_revision"):
        assert after[key] == before[key]
    reopened.results()
    assert read.call_count == 1


def live_pool(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    manual.state["result_reader_revision"] = screening._result_reader_revision()
    service.tick()
    live.rows = [{**signal(), "source_closed_at": cutoff}]
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 1 and service._load()["signals"]
    return service, manual, live


def test_storage_update_preserves_live_signals_and_does_not_schedule_full_pool(setup, monkeypatch):
    service, manual, live = live_pool(setup)
    original = deepcopy(service._load()["signals"])
    checks = deepcopy(service._load()["selection_checks"])
    change_storage_source(monkeypatch)
    manual.state["result_reader_revision"] = screening._result_reader_revision()
    service.tick()
    assert len(live.started) == 1
    assert service._load()["signals"] == original
    assert service._load()["selection_checks"] == checks
    assert service._load()["unverified_signals"] == {}
    assert not service._load()["force_pending_symbols"]


@pytest.mark.parametrize("dependency", [
    "web/chanlun_chart/cl_app/services/screening.py",
    "web/chanlun_chart/cl_app/services/screening_policy.py",
    "web/chanlun_chart/cl_app/services/screening_progress.py",
    "web/chanlun_chart/cl_app/services/historical_native_pool.py",
    "src/chanlun/screening/rules.py",
    "src/chanlun/screening/confirmation.py",
    "src/chanlun/screening/evidence.py",
    "src/chanlun/cl_utils/point_exits.py",
])
def test_interpretation_or_evidence_change_still_suspends_signals_and_rechecks_pool(
    setup, monkeypatch, dependency
):
    service, manual, live = live_pool(setup)
    original = deepcopy(service._load()["signals"])
    root = Path(screening.__file__).resolve().parents[4]
    target = root / dependency
    original_read = Path.read_bytes
    contents = original_read(target)
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents + b"\n# updated implementation\n"
                        if path.resolve() == target else original_read(path))
    revision = screening._result_reader_revision()
    assert revision != manual.state["result_reader_revision"]
    manual.state["result_reader_revision"] = revision
    service.tick()
    assert len(live.started) == 2
    assert service._load()["signals"] == {}
    assert service._load()["unverified_signals"] == original
