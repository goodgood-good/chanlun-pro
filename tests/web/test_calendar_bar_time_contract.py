from __future__ import annotations

from datetime import datetime, timezone
import time

import pytest

from cl_app import create_app
from cl_app.blueprints import tv as tv_mod
from cl_app.services import chart_cache
from cl_app.services.chart_compute import (
    _decide_full_snapshot,
    chart_bar_time_coordinate,
    slice_chart_data_to_window,
    strict_structure_history_fields,
    trim_future_bars,
)
from chanlun.cl_utils import query_cl_chart_config


def _ts(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def _chart_data(times: list[int]) -> dict[str, object]:
    values = [float(index) for index in range(len(times))]
    return {
        "t": times,
        "c": values,
        "o": values,
        "h": values,
        "l": values,
        "v": values,
    }


def test_calendar_coordinates_use_utc_period_anchors() -> None:
    assert chart_bar_time_coordinate(_ts(2026, 7, 22, 7), "d") == _ts(2026, 7, 22)
    assert chart_bar_time_coordinate(_ts(2026, 7, 19, 7), "w") == _ts(2026, 7, 13)
    assert chart_bar_time_coordinate(_ts(2026, 7, 31, 7), "m") == _ts(2026, 7, 1)
    assert chart_bar_time_coordinate(_ts(2026, 9, 30, 7), "q") == _ts(2026, 7, 1)
    assert chart_bar_time_coordinate(_ts(2026, 12, 31, 7), "y") == _ts(2026, 1, 1)
    assert chart_bar_time_coordinate(_ts(2026, 7, 22, 7), "30m") == _ts(2026, 7, 22, 7)


def test_current_calendar_bar_is_not_trimmed_as_future() -> None:
    june_close = _ts(2026, 6, 30, 7)
    july_close = _ts(2026, 7, 31, 7)
    august_close = _ts(2026, 8, 31, 7)
    request_to = _ts(2026, 7, 23, 8)

    retained = trim_future_bars(
        _chart_data([june_close, july_close]),
        request_to,
        frequency="m",
    )
    trimmed = trim_future_bars(
        _chart_data([june_close, july_close, august_close]),
        request_to,
        frequency="m",
    )

    assert retained["t"] == [june_close, july_close]
    assert trimmed["t"] == [june_close, july_close]


def test_calendar_poll_window_includes_the_active_period() -> None:
    june_close = _ts(2026, 6, 30, 7)
    july_close = _ts(2026, 7, 31, 7)
    august_close = _ts(2026, 8, 31, 7)

    sliced = slice_chart_data_to_window(
        _chart_data([june_close, july_close, august_close]),
        _ts(2026, 7, 1),
        _ts(2026, 8, 1),
        frequency="m",
    )

    assert sliced["t"] == [july_close]
    assert _decide_full_snapshot(
        "false",
        _ts(2026, 7, 23, 8),
        [july_close],
        True,
        frequency="m",
    )


def test_final_response_close_must_match_atomic_strict_snapshot() -> None:
    strict = {
        "schema": "chanlun-chart-structure",
        "source_closed_at": _ts(2026, 7, 31, 7),
    }
    chart_data = {
        "strict_structure_mode": "replace",
        "strict_structure": strict,
    }

    valid = strict_structure_history_fields(
        chart_data,
        authoritative=True,
        expected_source_closed_at=strict["source_closed_at"],
    )
    invalid = strict_structure_history_fields(
        chart_data,
        authoritative=True,
        expected_source_closed_at=_ts(2026, 6, 30, 7),
    )

    assert valid["strict_structure"] is strict
    assert invalid == {
        "strict_structure_mode": "unavailable",
        "strict_structure_error": {"code": "strict_context_mismatch"},
    }


@pytest.fixture
def client():
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
    return app.test_client()


def test_monthly_history_response_keeps_active_bar_and_matching_strict_snapshot(
    client,
    monkeypatch,
) -> None:
    market = "a"
    code = "SH.513100"
    june_close = _ts(2026, 6, 30, 7)
    july_close = _ts(2026, 7, 31, 7)
    chart_data = _chart_data([june_close, july_close])
    chart_data.update({
        "strict_structure_mode": "replace",
        "strict_structure": {
            "schema": "chanlun-chart-structure",
            "source_closed_at": july_close,
        },
    })
    config = query_cl_chart_config(market, code)
    cache_key = chart_cache._build_cache_key(market, code, "m", config)
    entry = chart_cache._build_chart_cache_entry(
        chart_data,
        is_full_snapshot=True,
        validated_at=time.time(),
    )
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[cache_key] = entry

    monkeypatch.setattr(tv_mod, "market_now_trading", lambda _market: False)
    monkeypatch.setattr(tv_mod, "submit_revalidation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tv_mod.market_frequencys,
        "cached_snapshot",
        lambda _markets: {market: ["m"]},
    )
    response = client.get(
        "/tv/history"
        f"?symbol={market}:{code}"
        "&resolution=1M"
        "&firstDataRequest=false"
        f"&from={_ts(2026, 7, 1)}"
        f"&to={_ts(2026, 7, 23, 8)}"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["s"] == "ok"
    assert payload["t"] == [july_close]
    assert payload["strict_structure_mode"] == "replace"
    assert payload["strict_structure"]["source_closed_at"] == payload["t"][-1]
