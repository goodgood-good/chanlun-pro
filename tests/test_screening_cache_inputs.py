"""No selected signal must not mean no original input for future audits."""

import hashlib
import pandas as pd
import pytest
from chanlun.screening.cache import (
    input_fingerprint,
    read_calculation,
    write_calculation,
)


def frame():
    f = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-09-01", periods=4, freq="5min", tz="Asia/Shanghai"
            ),
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [2.0, 3.0, 4.0, 5.0],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.5, 2.5, 3.5, 4.5],
            "volume": [1, 2, 3, 4],
        }
    )
    f.attrs = {
        "screening_market": "a",
        "price_basis_revision": "fixture",
        "_screening_trace": "temporary",
    }
    return f


def test_no_signal_cache_keeps_an_authenticated_round_trip_input(tmp_path):
    f = frame()
    path = tmp_path / "analysis/SH.600000_5m.json.gz"
    snap = {"levels": [], "symbol": "SH.600000", "source_frequency": "5m"}
    write_calculation(path, "key", {"reason_counts": {}}, snap, {}, source_frame=f)
    value = read_calculation(path, "key")
    source = value["source_input"]
    original = path.parent / source["file"]
    assert source["fingerprint"] == input_fingerprint(f, "SH.600000", "5m")
    assert hashlib.sha256(original.read_bytes()).hexdigest() == source["sha256"]
    assert (
        input_fingerprint(pd.read_parquet(original), "SH.600000", "5m")
        == source["fingerprint"]
    )
    assert f.attrs["_screening_trace"] == "temporary"
    f.attrs["_screening_trace"] = "new trace"
    write_calculation(
        path, "next-code-version", {"reason_counts": {}}, snap, {}, source_frame=f
    )
    assert read_calculation(path, "next-code-version")["source_input"] == source
    assert len(list((path.parent / "inputs").glob("*.parquet"))) == 1


@pytest.mark.parametrize("corruption", ["missing", "changed"])
def test_cache_with_missing_or_changed_input_is_not_reused(tmp_path, corruption):
    path = tmp_path / "analysis/SH.600000_5m.json.gz"
    snap = {"levels": [], "symbol": "SH.600000", "source_frequency": "5m"}
    write_calculation(
        path, "key", {"reason_counts": {}}, snap, {}, source_frame=frame()
    )
    original = path.parent / read_calculation(path, "key")["source_input"]["file"]
    if corruption == "missing":
        original.unlink()
    else:
        original.write_bytes(b"changed")
    assert read_calculation(path, "key") is None


def test_legacy_cache_can_receive_input_without_changing_the_saved_partition(tmp_path):
    path = tmp_path / "analysis/SH.600000_5m.json.gz"
    snap = {
        "levels": [],
        "symbol": "SH.600000",
        "source_frequency": "5m",
        "test_partition": [1, 3, 5],
    }
    write_calculation(path, "key", {"reason_counts": {}}, snap, {})
    legacy = read_calculation(path, "key")
    assert "source_input" not in legacy
    write_calculation(
        path,
        "key",
        {"reason_counts": {}},
        legacy["snapshot"],
        legacy["reviewed"],
        source_frame=frame(),
    )
    result = read_calculation(path, "key")
    assert result["snapshot"] == snap and result["source_input"]
