"""QMT's zero-volume extrema omission must have complete constituent evidence."""

import pandas as pd
import pytest

from chanlun.exchange.qmt_aggregate import reconstruct_thirty_minute


CODE = "BJ.920069"


def example():
    # Actual source window: 2026-09-14 10:35--11:00 Asia/Shanghai.
    source = pd.DataFrame(
        [
            (31.97, 31.99, 31.97, 31.99, 71),
            (32.07, 32.07, 32.07, 32.07, 20),
            (31.97, 31.97, 31.97, 31.97, 5),
            (31.96, 31.96, 31.90, 31.90, 23),
            (31.86, 31.86, 31.86, 31.86, 0),
            (31.86, 31.86, 31.86, 31.86, 0),
        ],
        columns=["open", "high", "low", "close", "volume"],
    )
    source["code"] = CODE
    source["date"] = pd.date_range(
        "2026-09-14 10:35", periods=6, freq="5min", tz="Asia/Shanghai"
    )
    source.attrs = {
        "structure_price_quantum": "0.01",
        "price_basis_revision": "same-adjustment",
        "price_basis_provider": "qmt",
        "price_basis_adjustment": "front_ratio",
    }
    native = pd.DataFrame(
        [
            (32.0, 32.1, 31.9, 32.0, 50.0),
            (31.97, 32.07, 31.90, 31.86, 119.0),
            (31.9, 32.0, 31.8, 31.9, 70.0),
        ],
        columns=["open", "high", "low", "close", "volume"],
        index=[10, 20, 30],
    )
    native["code"] = CODE
    native["date"] = pd.date_range(
        "2026-09-14 10:30", periods=3, freq="30min", tz="Asia/Shanghai"
    )
    native.attrs = dict(source.attrs)
    return native, source


def test_rebuilds_full_six_rows_preserving_neighbors_clocks_volume_and_original_evidence():
    native, source = example()
    original = native.copy(deep=True)
    result = reconstruct_thirty_minute(native, CODE, lambda *args: source)
    assert result.loc[20, "low"] == 31.86
    pd.testing.assert_frame_equal(result.loc[[10, 30]], native.loc[[10, 30]])
    pd.testing.assert_frame_equal(
        result.drop(columns="low"), native.drop(columns="low")
    )
    pd.testing.assert_frame_equal(native, original)
    assert result.attrs["price_basis_revision"] == native.attrs["price_basis_revision"]
    proof = result.attrs["qmt_30m_reconstruction"]["rows"][0]
    assert proof["original"]["low"] == 31.90
    assert len(proof["source_5m"]) == 6
    assert proof["source_5m"][-1]["low"] == 31.86
    assert len(proof["source_sha256"]) == 64


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "shifted_clock",
        "invalid_ohlc",
        "negative_volume",
        "nonfinite",
        "basis_changed",
        "adjustment_changed",
        "quantum_changed",
        "wrong_symbol",
        "listing_conflict",
        "native_open_differs",
        "native_volume_differs",
        "native_extrema_not_explained",
        "opening_labels",
        "wrong_period",
        "duplicate_index",
    ],
)
def test_unproved_source_keeps_the_original_invalid_frame(fault):
    native, source = example()
    if fault == "missing":
        source = source.drop(index=2)
    elif fault == "duplicate":
        source.loc[2, "date"] = source.loc[1, "date"]
    elif fault == "shifted_clock":
        source["date"] += pd.Timedelta(minutes=1)
    elif fault == "invalid_ohlc":
        source.loc[0, "high"] = 31.0
    elif fault == "negative_volume":
        source.loc[0, "volume"] = -1
    elif fault == "nonfinite":
        source.loc[0, "low"] = float("nan")
    elif fault == "basis_changed":
        source.attrs["price_basis_revision"] = "changed"
    elif fault == "adjustment_changed":
        source.attrs["price_basis_adjustment"] = "none"
    elif fault == "quantum_changed":
        source.attrs["structure_price_quantum"] = "0.001"
    elif fault == "wrong_symbol":
        source["code"] = "BJ.920160"
    elif fault == "listing_conflict":
        source.attrs["qmt_listing_history_conflict"] = True
    elif fault == "native_open_differs":
        native.loc[20, "open"] = 31.98
    elif fault == "native_volume_differs":
        native.loc[20, "volume"] = 120
    elif fault == "native_extrema_not_explained":
        native.loc[20, "low"] = 31.91
    elif fault == "opening_labels":
        source.attrs["bar_time_label"] = "start"
    elif fault == "wrong_period":
        source["frequency"] = "30m"
    elif fault == "duplicate_index":
        native.index = [10, 10, 30]
    assert reconstruct_thirty_minute(native, CODE, lambda *args: source) is native
    assert "qmt_30m_reconstruction" not in native.attrs


def test_valid_native_frame_does_not_read_more_data():
    native, _source = example()
    native.loc[20, "low"] = 31.86

    def must_not_read(*args):
        pytest.fail("valid 30m input does not need base-period reads")

    assert reconstruct_thirty_minute(native, CODE, must_not_read) is native


def test_source_failure_and_non_session_clock_leave_original_rejection():
    native, _source = example()

    def unavailable(*args):
        raise OSError("local source unavailable")

    assert reconstruct_thirty_minute(native, CODE, unavailable) is native
    native.loc[20, "date"] += pd.Timedelta(minutes=1)
    assert (
        reconstruct_thirty_minute(
            native, CODE, lambda *args: pytest.fail("invalid session read")
        )
        is native
    )


def test_adapter_only_reads_matching_six_local_five_minute_rows(monkeypatch):
    from chanlun.exchange import exchange_qmt

    native, source = example()
    calls = []

    class Provider:
        @staticmethod
        def get_market_data_ori(**kwargs):
            calls.append(kwargs)
            chosen = native if kwargs["period"] == "30m" else source
            values = {
                name: [chosen[name].to_numpy()]
                for name in ("open", "high", "low", "close", "volume")
            }
            values["time"] = [chosen.date.astype("int64").to_numpy() // 1_000_000]
            return (kwargs['stock_list'], list(range(len(chosen)))), values

    monkeypatch.setattr(exchange_qmt, "xtdata", Provider)
    result = exchange_qmt.ExchangeQMT()._klines_once(
        CODE,
        "30m",
        "20260914",
        "2026-09-14 11:30:00",
        {
            "exact_end": True,
            "local_only": True,
            "skip_download": True,
            "dividend_type": "none",
        },
    )
    assert result.low.iloc[1] == 31.86
    assert [call["period"] for call in calls] == ["30m", "5m"]
    base = calls[-1]
    assert (
        base["start_time"] == "20260914103500" and base["end_time"] == "20260914110000"
    )
    assert base["enable_read_from_server"] is False and base["fill_data"] is False
    assert base["dividend_type"] == "none"
    assert "qmt_30m_reconstruction" in result.attrs


def test_provenance_survives_frozen_input_roundtrip_and_is_part_of_cache_identity(
    tmp_path,
):
    from chanlun.screening.cache import input_fingerprint

    native, source = example()
    result = reconstruct_thirty_minute(native, CODE, lambda *args: source)
    before = input_fingerprint(result, CODE, "30m")
    destination = tmp_path / "frozen.parquet"
    result.to_parquet(destination, index=False)
    restored = pd.read_parquet(destination)
    assert restored.attrs == result.attrs
    assert input_fingerprint(restored, CODE, "30m") == before
    restored.attrs.pop("qmt_30m_reconstruction")
    assert input_fingerprint(restored, CODE, "30m") != before


def test_reconstruction_is_atomic_if_a_second_bad_bucket_cannot_be_proven():
    native, source = example()
    native.loc[30, ["open", "high", "low", "close", "volume"]] = [
        31.97,
        32.07,
        31.90,
        31.86,
        119.0,
    ]
    original = native.copy(deep=True)

    def read(opening, closing):
        return source if closing.minute == 0 else source.iloc[:-1]

    assert reconstruct_thirty_minute(native, CODE, read) is native
    pd.testing.assert_frame_equal(native, original)
