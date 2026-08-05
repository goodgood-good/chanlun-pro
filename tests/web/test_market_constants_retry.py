"""Retry-state tests for lazily loaded market metadata."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cl_app.services.constants import _LazyMarketDict


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_failed_market_retries_only_after_ttl_and_success_is_cached():
    clock = _FakeClock()
    calls = 0

    def builder(_key, _market):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return ["1m", "5m"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=list,
        retry_seconds=30,
        clock=clock,
    )

    assert metadata["a"] == []
    assert calls == 1

    clock.now = 29.999
    assert metadata.get("a") == []
    assert calls == 1

    clock.now = 30.0
    assert metadata["a"] == ["1m", "5m"]
    assert calls == 2

    clock.now = 3600.0
    assert metadata["a"] == ["1m", "5m"]
    assert calls == 2


def test_failure_for_one_market_does_not_pollute_another_market():
    calls = {"a": 0, "hk": 0}

    def builder(key, _market):
        calls[key] += 1
        if key == "a":
            raise RuntimeError("a is unavailable")
        return ["hk-value"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object()), ("hk", object())],
        fallback_factory=list,
        clock=lambda: 0.0,
    )

    assert metadata["a"] == []
    assert metadata["hk"] == ["hk-value"]
    assert metadata["hk"] == ["hk-value"]
    assert calls == {"a": 1, "hk": 1}


def test_concurrent_reads_build_the_same_market_once():
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(3)
    calls = 0

    def builder(_key, _market):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return ["ready"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=list,
    )

    def read_market():
        start.wait(timeout=2)
        return metadata["a"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(read_market)
        second = executor.submit(read_market)
        start.wait(timeout=2)
        assert entered.wait(timeout=2)
        release.set()

        assert first.result(timeout=2) == ["ready"]
        assert second.result(timeout=2) == ["ready"]

    assert calls == 1


def test_same_market_read_has_bounded_wait_while_builder_is_loading():
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def builder(_key, _market):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return ["ready"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=list,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(lambda: metadata["a"])
        assert entered.wait(timeout=2)
        waiter = executor.submit(lambda: metadata["a"])
        try:
            assert waiter.result(timeout=0.25) == []
        finally:
            release.set()

        assert owner.result(timeout=2) == ["ready"]

    assert metadata["a"] == ["ready"]
    assert calls == 1


def test_json_serialization_loads_all_keys_and_retries_only_failed_market():
    clock = _FakeClock()
    calls = {"a": 0, "hk": 0}

    def builder(key, _market):
        calls[key] += 1
        if key == "a" and calls[key] == 1:
            raise RuntimeError("a failed once")
        return [f"{key}-value"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object()), ("hk", object())],
        fallback_factory=list,
        retry_seconds=30,
        clock=clock,
    )

    assert json.loads(json.dumps(metadata)) == {
        "a": [],
        "hk": ["hk-value"],
    }
    assert calls == {"a": 1, "hk": 1}

    clock.now = 30.0
    assert dict(metadata.items()) == {
        "a": ["a-value"],
        "hk": ["hk-value"],
    }
    assert calls == {"a": 2, "hk": 1}


@pytest.mark.parametrize(
    ("empty_value", "fallback_factory", "recovered"),
    [
        pytest.param([], list, ["1m"], id="empty-list"),
        pytest.param("", lambda: "", "AAPL", id="empty-string"),
    ],
)
def test_empty_builder_value_is_failed_and_retried_after_ttl(
    empty_value, fallback_factory, recovered
):
    clock = _FakeClock()
    calls = 0

    def builder(_key, _market):
        nonlocal calls
        calls += 1
        return empty_value if calls == 1 else recovered

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=fallback_factory,
        retry_seconds=30,
        clock=clock,
    )

    assert metadata["a"] == empty_value
    assert metadata.status("a") == {"state": "failed", "ready": False}
    assert calls == 1

    clock.now = 29.999
    assert metadata["a"] == empty_value
    assert calls == 1

    clock.now = 30.0
    assert metadata["a"] == recovered
    assert metadata.status("a") == {"state": "loaded", "ready": True}
    assert calls == 2


def test_snapshot_returns_plain_dict_and_retries_only_expired_failures():
    clock = _FakeClock()
    calls = {"a": 0, "hk": 0}

    def builder(key, _market):
        calls[key] += 1
        if key == "a" and calls[key] == 1:
            raise RuntimeError("a failed once")
        return [f"{key}-value"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object()), ("hk", object())],
        fallback_factory=list,
        retry_seconds=30,
        clock=clock,
    )

    assert metadata.cached_snapshot() == {"a": [], "hk": []}
    assert calls == {"a": 0, "hk": 0}

    selected = metadata.snapshot(["hk"])
    assert type(selected) is dict
    assert selected == {"hk": ["hk-value"]}
    assert calls == {"a": 0, "hk": 1}

    first = metadata.snapshot()
    assert type(first) is dict
    assert first == {"a": [], "hk": ["hk-value"]}
    assert calls == {"a": 1, "hk": 1}

    clock.now = 29.999
    assert metadata.snapshot() == first
    assert calls == {"a": 1, "hk": 1}

    clock.now = 30.0
    assert metadata.snapshot() == {
        "a": ["a-value"],
        "hk": ["hk-value"],
    }
    assert calls == {"a": 2, "hk": 1}


def test_status_is_side_effect_free_and_distinguishes_cache_states():
    clock = _FakeClock()
    calls = {"a": 0, "hk": 0}

    def builder(key, _market):
        calls[key] += 1
        if key == "a" and calls[key] == 1:
            raise RuntimeError("a failed once")
        if key == "a" and calls[key] == 2:
            return []
        if key == "a":
            return ["recovered"]
        return ["ready"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object()), ("hk", object())],
        fallback_factory=list,
        retry_seconds=30,
        clock=clock,
    )

    assert metadata.status("a") == {"state": "unloaded", "ready": False}
    assert calls == {"a": 0, "hk": 0}

    assert metadata["a"] == []
    assert metadata.status("a") == {"state": "failed", "ready": False}

    assert metadata["hk"] == ["ready"]
    assert metadata.status("hk") == {"state": "loaded", "ready": True}

    clock.now = 30.0
    assert metadata["a"] == []
    assert metadata.status("a") == {"state": "failed", "ready": False}
    assert calls == {"a": 2, "hk": 1}

    clock.now = 59.999
    assert metadata["a"] == []
    assert calls == {"a": 2, "hk": 1}

    clock.now = 60.0
    assert metadata["a"] == ["recovered"]
    assert metadata.status("a") == {"state": "loaded", "ready": True}
    assert calls == {"a": 3, "hk": 1}


def test_mapping_interface_preserves_known_keys_and_custom_fallback():
    def builder(_key, _market):
        raise RuntimeError("unavailable")

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=lambda: "",
        clock=lambda: 0.0,
    )

    assert list(metadata.keys()) == ["a"]
    assert list(metadata) == ["a"]
    assert len(metadata) == 1
    assert "a" in metadata
    assert "missing" not in metadata
    assert metadata.get("missing", "default") == "default"
    assert metadata["a"] == ""
    with pytest.raises(KeyError):
        _ = metadata["missing"]


def test_new_lifecycle_retries_a_pre_shutdown_failure_immediately():
    calls = 0

    def builder(_key, _market):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary startup failure")
        return ["1m", "5m"]

    metadata = _LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=list,
        retry_seconds=30,
        clock=lambda: 0.0,
    )

    assert metadata["a"] == []
    assert metadata.status("a") == {"state": "failed", "ready": False}
    metadata.shutdown()
    metadata.start()

    assert metadata["a"] == ["1m", "5m"]
    assert calls == 2
