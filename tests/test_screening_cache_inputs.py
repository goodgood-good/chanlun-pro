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


def test_append_proofs_require_identical_history_revision_and_exclude_previews(tmp_path):
    from chanlun.screening.cache import read_append_proofs,REPLAY_CHECKS
    source=frame().iloc[:3].copy();path=tmp_path/'cache/SH.600000_5m.json.gz'
    snap={'levels':[],'symbol':'SH.600000','source_frequency':'5m'}
    proofs={'formal':dict.fromkeys(REPLAY_CHECKS,True),'consolidation-preview:p':{'cold_rebuild':True,'closed_bar_replay':True}}
    write_calculation(path,'old',{'reason_counts':{}},snap,proofs,source_frame=source,calculation_revision='r')
    current=frame()
    assert read_append_proofs(path,current,'SH.600000','5m','r')=={'formal':proofs['formal']}
    assert read_append_proofs(path,current,'SH.600000','5m','new')=={}
    corrected=current.copy();corrected.loc[0,'volume']+=1
    assert read_append_proofs(path,corrected,'SH.600000','5m','r')=={}
    assert read_append_proofs(path,current.iloc[1:],'SH.600000','5m','r')=={}
    changed=current.copy();changed.attrs['price_basis_revision']='new'
    assert read_append_proofs(path,changed,'SH.600000','5m','r')=={}


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
