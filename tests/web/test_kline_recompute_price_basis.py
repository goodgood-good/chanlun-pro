from __future__ import annotations

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
from cl_app.services import chart_cache, chart_compute, kline_recompute


def _chart_data(revision: str | None = "sha256:basis") -> dict:
    payload = {
        "t": [1000, 1060],
        "o": [10.0, 11.0],
        "h": [10.0, 11.0],
        "l": [10.0, 11.0],
        "c": [10.0, 11.0],
        "v": [100.0, 110.0],
    }
    if revision is not None:
        payload.update(
            strict_structure_mode="replace",
            strict_structure={
                "structure_price_quantum": "0.01",
                "price_basis_revision": revision,
            },
        )
    return payload


def _klines(
    revision: str | None = "sha256:basis",
    *,
    prices: tuple[float, ...] = (10.0, 11.0),
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [1000 + index * 60 for index in range(len(prices))],
                unit="s",
                utc=True,
            ),
            "open": list(prices),
            "high": list(prices),
            "low": list(prices),
            "close": list(prices),
            "volume": [100.0 + index * 10 for index in range(len(prices))],
        }
    )
    if revision is not None:
        frame.attrs.update(
            structure_price_quantum="0.01",
            price_basis_revision=revision,
            price_basis_provider="qmt",
            price_basis_adjustment="front",
        )
    return frame


def test_extract_chart_payload_recovers_strict_price_basis_metadata() -> None:
    frame = kline_recompute.extract_klines_df_from_chart_data(_chart_data())

    assert frame.attrs["structure_price_quantum"] == "0.01"
    assert frame.attrs["price_basis_revision"] == "sha256:basis"


def test_merge_same_price_basis_preserves_new_metadata() -> None:
    cached = _klines()
    new = _klines(prices=(10.0, 12.0, 13.0))

    merged = kline_recompute.merge_klines_df(cached, new)

    assert merged.attrs == new.attrs
    assert merged["close"].tolist() == [10.0, 12.0, 13.0]


@pytest.mark.parametrize(
    "cached_revision",
    ["sha256:basis", None],
    ids=["changed-known-basis", "unknown-cached-basis"],
)
def test_prepend_rejects_unsafe_basis_mix_and_invalidates_cache(
    monkeypatch,
    cached_revision,
) -> None:
    cache_key = "price-basis-test-a-SH.600926-1m"
    deleted = []
    recomputed = []
    monkeypatch.setattr(
        chart_cache,
        "_get_chart_cache_entry",
        lambda key: {"data": _chart_data(cached_revision)},
    )
    monkeypatch.setattr(
        chart_cache,
        "_delete_chart_cache_entry",
        lambda key: deleted.append(key),
        raising=False,
    )
    monkeypatch.setattr(
        kline_recompute,
        "recompute_chart_data_from_klines",
        lambda *args, **kwargs: recomputed.append(1),
    )
    with kline_recompute._cl_pool_lock:
        kline_recompute._cl_pool[cache_key] = {"stale": True}

    try:
        result = kline_recompute.prepend_klines_and_replace_cache(
            "a",
            "SH.600926",
            "1m",
            {},
            _klines("sha256:new-basis"),
            cache_key,
        )

        assert result is None
        assert deleted == [cache_key]
        assert recomputed == []
        with kline_recompute._cl_pool_lock:
            assert cache_key not in kline_recompute._cl_pool
    finally:
        with kline_recompute._cl_pool_lock:
            kline_recompute._cl_pool.pop(cache_key, None)


def test_delete_chart_cache_entry_clears_ram_and_disk(monkeypatch) -> None:
    cache_key = "price-basis-delete-test"
    deleted = []
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[cache_key] = {"data": _chart_data()}
    monkeypatch.setattr(
        chart_cache.fdb,
        "delete_chart_cache",
        lambda key: deleted.append(key),
    )

    chart_cache._delete_chart_cache_entry(cache_key)

    with chart_cache.cache_lock:
        assert cache_key not in chart_cache.chart_data_cache
    assert deleted == [cache_key]


def test_recompute_serializes_exact_frame_through_strict_bridge(monkeypatch) -> None:
    frame = _klines()
    config = {"bi_type": "old"}
    captured = {}

    class FakeCL:
        def process_klines(self, klines):
            captured["processed"] = klines

    def build(**kwargs):
        captured["build"] = kwargs
        cd = FakeCL()
        cd.process_klines(kwargs["frame"])
        return StrictChartRuntimeResult.success(cd)

    def serialize(**kwargs):
        captured["serialize"] = kwargs
        return {"t": [1000, 1060]}

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build)
    monkeypatch.setattr(
        chart_compute,
        "serialize_chart_data_with_strict_runtime",
        serialize,
    )
    result = kline_recompute.recompute_chart_data_from_klines(
        "a",
        "SH.600926",
        "1m",
        config,
        frame,
    )

    assert result == {"t": [1000, 1060]}
    assert captured["processed"] is frame
    serialized = captured["serialize"]
    assert serialized["market"] == "a"
    assert serialized["code"] == "SH.600926"
    assert serialized["display_frequency"] == "1m"
    assert serialized["display_klines"] is frame
    assert serialized["chart_config"] is config
    assert isinstance(serialized["strict_runtime"].cd, FakeCL)
