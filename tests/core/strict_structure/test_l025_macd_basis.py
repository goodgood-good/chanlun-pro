"""Recorded MACD weakening follows the declared native-center signal policy."""

from pathlib import Path

import pandas as pd

from chanlun.screening.runner import snapshot_for


def test_recorded_area_disagreement_uses_local_macd_reminder_only():
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures" / "segment_origin" / "SH.688377_5m.parquet"
    )
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = snapshot_for(frame, "SH.688377", "5m")
    level, = snapshot["levels"]
    assert snapshot["analysis_scope"] == "native_centers"
    assert level["trend_types"] == level["trend_shapes"] == level["divergences"] == []
    signals = [item for item in level["points"] if item["point_type"].startswith("1")]
    assert signals
    assert all(item["divergence"]["kind"] == "native_two_center_divergence"
               and item["divergence"]["histogram_area_decayed"] is True
               and item["rule_basis"] == "user_declared_native_center_macd_v4"
               and item["formal_point_issued"] is True
               and item["status"] == "confirmed" for item in signals)
