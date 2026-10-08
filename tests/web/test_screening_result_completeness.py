"""Committed full-screening results cannot silently lose rows on publication."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from cl_app.services import screening
from tests.web.test_screening_results_cache import seed_run
from tests.web.test_signal_monitor import setup as setup, _seed, signal


@pytest.fixture
def recorded(tmp_path):
    manager = screening.ScreeningManager(tmp_path / "screening", warm_results=False)
    directory = seed_run(manager)
    path = directory / "results.jsonl"
    first = path.read_bytes()
    original = first + first.replace(b"SH.600000", b"SH.600001")
    path.write_bytes(original)
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**status, "completed": 2, "total": 2})
    return manager, directory, path, first, original


@pytest.mark.parametrize("damage", ["missing", "empty", "missing_tail", "partial_tail"])
def test_cold_completed_read_rejects_missing_committed_rows_and_recovers(recorded, damage):
    manager, directory, path, first, original = recorded
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes({"empty": b"", "missing_tail": first,
                          "partial_tail": original.rstrip(b"\n")}[damage])
    with pytest.raises(ValueError, match="结果|record"):
        manager.results()
    assert not (directory / screening._RESULT_CACHE_FILE).exists()
    path.write_bytes(original)
    result = manager.results()
    assert result["results_through"] == result["completed"] == 2
    assert len(result["processed_symbols"]) == 2


def test_background_read_cannot_publish_a_truncated_completed_result(recorded):
    manager, directory, path, first, _original = recorded
    path.write_bytes(first)
    try:
        result = manager.results(background=True)
    except ValueError:
        pass
    else:
        assert result["results_loading"], "an incomplete result was published immediately"
        with pytest.raises(ValueError):
            manager._results_future.result(timeout=2)
        with pytest.raises(ValueError):
            manager.results()
    assert not (directory / screening._RESULT_CACHE_FILE).exists()


def test_truncation_cannot_overwrite_a_previous_verified_cache(recorded):
    manager, directory, path, first, original = recorded
    assert manager.results()["results_through"] == 2
    cache = directory / screening._RESULT_CACHE_FILE
    verified = cache.read_bytes()
    path.write_bytes(first)
    with pytest.raises(ValueError):
        manager.results()
    assert cache.read_bytes() == verified
    path.write_bytes(original)
    assert manager.results()["results_through"] == 2


def test_incomplete_new_run_cannot_replace_the_existing_monitor_pool(setup, tmp_path, monkeypatch):
    service, _manual, live, _clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.finish(cutoff)
    service.tick()
    previous = deepcopy(service._load()["seed"])
    manager = screening.ScreeningManager(tmp_path / "new-screening", warm_results=False)
    directory = seed_run(manager, run_id="b" * 32)
    first = {"code": "SH.600099", "rows": [{"code": "SH.600099", "frequency": "5m",
             "source_closed_at": cutoff, "selected": [], "recent_rejections": [],
             "reason_counts": {}, "data_errors": []}]}
    (directory / "results.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**status, "completed": 2, "total": 2,
        "error_count": 0, "successful_period_count": 2,
        "settings": previous["settings"], "cutoffs": {"5m": cutoff}})
    service.screening = manager
    monkeypatch.setattr(service, "_schedule_daily", lambda *args: None)
    scheduled = Mock()
    monkeypatch.setattr(service, "_schedule_monitor", scheduled)
    service.tick()
    if manager._results_future is not None:
        try:
            manager._results_future.result(timeout=2)
        except ValueError:
            pass
    service.tick()
    assert service._load()["seed"]["run_id"] == previous["run_id"]
    assert service._load()["seed"]["codes"] == previous["codes"]
    assert service._load()["screening_result_error"]["run_id"] == directory.name
    assert scheduled.call_count == 2


def test_uncommitted_partial_tail_does_not_invalidate_the_completed_prefix(recorded):
    manager, _directory, path, _first, original = recorded
    path.write_bytes(original + b'{"uncommitted":')
    result = manager.results()
    assert result["results_through"] == 2 and len(result["processed_symbols"]) == 2


def test_zero_committed_rows_allow_an_absent_result_file(recorded):
    manager, directory, path, _first, _original = recorded
    path.unlink()
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    screening.write_json(directory / "status.json", {**status, "completed": 0, "total": 0})
    result = manager.results()
    assert result["results_through"] == 0 and not result["processed_symbols"]
