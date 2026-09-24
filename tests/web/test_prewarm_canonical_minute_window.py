"""Prewarming must use the same complete 1m input contract as an initial chart.

A wall-clock 30-day cutoff can truncate the first session in mid-minute,
removing the predecessor required for a segment proof under the same cache key.
"""

from datetime import datetime, timedelta
import pandas as pd
import pytest
from cl_app.services import chart_compute


@pytest.mark.parametrize("market", ["us", "hk"])
def test_minute_prewarm_uses_canonical_history_when_adapter_exposes_it(
    monkeypatch, market
):
    calls = []
    saved = []
    full = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-08-20 09:31", periods=8, freq="min", tz="America/New_York"
            ),
            "open": range(8),
            "high": range(1, 9),
            "low": range(8),
            "close": range(1, 9),
            "volume": [1] * 8,
        }
    )
    full.attrs["bar_time_label"] = "end"

    class Exchange:
        def canonical_closed_minute_history(self, code, **kwargs):
            calls.append(("canonical", code, kwargs))
            return full

        def klines(self, code, frequency, **kwargs):
            calls.append(("rolling", code, kwargs))
            return full.iloc[-3:]

    monkeypatch.setattr(chart_compute, "get_exchange", lambda _: Exchange())
    monkeypatch.setattr(chart_compute, "_is_negatively_cached", lambda _: False)
    monkeypatch.setattr(
        chart_compute, "attach_chart_bar_time_label", lambda frame, **_: frame
    )
    monkeypatch.setattr(
        chart_compute,
        "serialize_chart_data_with_strict_runtime",
        lambda **kw: {"t": kw["display_klines"].date.tolist()},
    )
    monkeypatch.setattr(
        chart_compute,
        "_set_chart_cache_entry",
        lambda key, payload, **kw: saved.append(payload),
    )
    assert chart_compute.compute_and_cache_chart_data(
        market, "QQQ.US" if market == "us" else "HK.00700", "1m", {}
    )
    assert calls[0][0] == "canonical"
    assert len(saved[0]["t"]) == len(full)
    if market == "us":
        end = datetime.fromisoformat(calls[0][2]["end_date"])
        assert end.utcoffset() == timedelta(hours=8)


def test_other_frequencies_keep_their_own_history_request(monkeypatch):
    calls = []

    class Exchange:
        def canonical_closed_minute_history(self, *args, **kwargs):
            raise AssertionError("unexpected canonical minute request")

        def klines(self, code, frequency, **kwargs):
            calls.append((frequency, kwargs))
            return pd.DataFrame()

    monkeypatch.setattr(chart_compute, "get_exchange", lambda _: Exchange())
    monkeypatch.setattr(chart_compute, "_is_negatively_cached", lambda _: False)
    monkeypatch.setattr(
        chart_compute, "_mark_negative_cache", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        chart_compute, "attach_chart_bar_time_label", lambda frame, **_: frame
    )
    assert not chart_compute.compute_and_cache_chart_data("us", "QQQ.US", "5m", {})
    assert calls[0][0] == "5m"
