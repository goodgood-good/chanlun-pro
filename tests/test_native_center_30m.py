"""Recorded 5m bars provide complete, causal 30m buy/sell trigger cases."""

from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from chanlun.screening import rules, runner
from cl_app.services.screening import ScreeningManager


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("code,kind", [("SH.600004", "3sell"), ("SH.600183", "3buy")])
def test_recorded_30m_trigger_keeps_native_members_and_reaches_saved_formal_result(
    tmp_path, monkeypatch, code, kind,
):
    manifest = json.loads((FIXTURES / "native_30m" / "provenance.json").read_text(encoding="utf-8"))
    record = next(item for item in manifest["sources"] if item["code"] == code)
    source_path = FIXTURES / record["source"]
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == record["source_sha256"]
    source = pd.read_parquet(source_path)
    groups = source.set_index("date").resample("30min", closed="right", label="right")
    expected = groups.agg({"open": "first", "high": "max", "low": "min",
                           "close": "last", "volume": "sum"}).loc[groups.size().eq(6)].reset_index()
    expected["code"] = code
    frame = pd.read_parquet(FIXTURES / "native_30m" / record["file"])
    pd.testing.assert_frame_equal(frame, expected)
    assert len(frame) == record["output_rows"]
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw",
                       screening_market="a")

    full = runner.snapshot_for(frame, code, "30m")
    point, = full["levels"][0]["points"]
    assert point["point_type"] == kind
    assert point["member_basis"] == "native_locked_segment"
    assert full["native_member_policy"]["source_frequency"] == "30m"
    assert full["native_member_policy"]["recursive_type_proven"] is False
    assert full["levels"][0]["trend_types"] == []

    prefix = frame.loc[frame.date <= pd.Timestamp(point["available_at"], unit="s", tz="UTC")].copy()
    context = rules.trading_context(prefix.date.iloc[-1] + timedelta(minutes=1), "30m", 20, 60)
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args, **_kwargs: prefix)
    result = runner.scan_symbol(
        {"code": code, "name": "recorded-30m", "market": "a"},
        {"frequencies": ["30m"], "point_types": list(rules.POINT_TYPES),
         "strategy": runner.DIRECT_STRATEGY, "structural_levels": [0],
         "max_anchor_gain_pct": 100},
        {"30m": context}, tmp_path,
    )
    row, = result["rows"]
    assert not row["data_errors"], row
    assert not row["observations"]
    selected, = row["selected"]
    assert selected["point"] == point
    assert all(selected["audit"].values())
    assert selected["confirmation_age_sessions"] == 0
    assert row["evidence"]["chart_saved"] is True

    (tmp_path / "results.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    saved = ScreeningManager(tmp_path)._read_results(
        tmp_path, {"completed": 1, "source_current": True,
                   "settings": {"strategy": runner.DIRECT_STRATEGY}},
    )
    assert not saved["errors"]
    assert not saved["observations"]
    shown, = saved["selected"]
    assert shown["point"]["point_type"] == kind
    assert shown["point"]["status"] == "confirmed"
    assert shown["evidence_verified"] is True
