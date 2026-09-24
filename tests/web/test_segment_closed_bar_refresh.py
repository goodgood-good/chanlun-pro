"""Completed-bar refreshes must confirm only at the actual close boundary."""

from pathlib import Path

import pandas as pd

from chanlun.exchange import kline_completion
from cl_app.services import kline_recompute


def test_same_price_refresh_confirms_only_after_last_minute_closes(monkeypatch):
    data = pd.read_parquet(Path(__file__).parents[1] / "fixtures/segment_clock/SZ.000766_1m.parquet")
    observed = [pd.Timestamp("2026-09-18 14:59:59", tz="Asia/Shanghai")]
    monkeypatch.setattr(kline_completion, "_timestamp_at", lambda value, reference: observed[0])
    endpoint = int(pd.Timestamp("2026-09-18 13:52", tz="Asia/Shanghai").timestamp())
    config = {"chart_show_xd": "1"}
    kline_recompute.reset_cl_pool()
    try:
        def refresh():
            return kline_recompute.recompute_chart_data_from_klines(
                "a", "SZ.000766", "1m", config, data, cache_key="closed-refresh-A08",
            )
        before = refresh()
        assert before["t"][-1] < int(data.date.iloc[-1].timestamp())
        target = lambda result: next(s for s in result["xds"] if s["points"][-1]["time"] == endpoint)
        assert not target(before)["locked"]
        observed[0] = pd.Timestamp("2026-09-18 15:00", tz="Asia/Shanghai")
        after = refresh()
        assert after["t"][-1] == int(observed[0].timestamp())
        assert target(after)["locked"]
        assert target(before)["points"] == target(after)["points"]
        assert after["strict_structure_mode"] == "replace"
    finally:
        kline_recompute.reset_cl_pool()
