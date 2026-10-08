"""An optional result cache cannot hide successfully verified screening rows."""

import gzip
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from cl_app.services import screening
from tests.web.test_screening_results_cache import cached_run, _running


@pytest.mark.parametrize("mode", ["cold", "background", "persist", "progressive_finish"])
def test_locked_cache_temporary_does_not_discard_verified_results(cached_run, monkeypatch, mode):
    manager = cached_run
    directory = manager._directory()
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    if mode == "persist":
        manager.results()
    elif mode == "progressive_finish":
        _running(manager)
        manager.results()
        _running(manager, status="completed")

    replace, unlink = os.replace, Path.unlink
    failures = []

    def cache_temporary(path):
        path = Path(path)
        return path.parent == directory and path.name.startswith(screening._RESULT_CACHE_FILE + ".") and path.suffix == ".tmp"

    def blocked_replace(source, destination):
        if cache_temporary(source):
            failures.append("replace")
            raise PermissionError("cache file is temporarily held by another process")
        return replace(source, destination)

    def blocked_unlink(path, *args, **kwargs):
        if cache_temporary(path):
            assert path.is_file(), "the simulated locked temporary must really exist"
            failures.append("cleanup")
            raise PermissionError("the same temporary file is still held")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(screening.os, "replace", blocked_replace)
        patch.setattr(Path, "unlink", blocked_unlink)
        if mode == "background":
            manager.results(background=True)
            manager._results_future.result(timeout=5)
        result = manager.results(persist=mode == "persist")
        assert result["results_loading"] is False
        assert result["errors"][0]["code"] == "SH.600000"
        assert manager.results()["errors"] == result["errors"]
        assert read.call_count == 1, "optional cache failure must not restart evidence verification"
    assert failures == ["replace", "cleanup"]

    # Once the filesystem recovers, persistence works without a new scan/read.
    recovered = manager.results(persist=True)
    cached = json.loads(gzip.decompress((directory / screening._RESULT_CACHE_FILE).read_bytes()))
    assert cached["payload"]["errors"] == recovered["errors"] == result["errors"]
    assert read.call_count == 1


def test_actual_result_read_failure_is_not_treated_as_optional_cache_failure(cached_run, monkeypatch):
    manager = cached_run
    with monkeypatch.context() as patch:
        patch.setattr(manager, "_read_results", Mock(side_effect=PermissionError("actual results unavailable")))
        with pytest.raises(PermissionError, match="actual results unavailable"):
            manager.results()
    assert manager.results()["errors"][0]["code"] == "SH.600000"
