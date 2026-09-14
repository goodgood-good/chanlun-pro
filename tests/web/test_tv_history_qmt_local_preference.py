"""Ordinary QMT history requests verify local coverage before downloading."""

from types import SimpleNamespace

import pytest

from cl_app import create_app
from cl_app.blueprints import tv as subject


@pytest.fixture(scope="module")
def client():
    app = create_app(test_config={
        "TESTING": True, "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "WTF_CSRF_ENABLED": False,
    })
    yield app.test_client()
    app.extensions["shutdown_runtime_services"]()


@pytest.fixture
def fetch_history(client, monkeypatch):
    monkeypatch.setattr(subject, "query_cl_chart_config", lambda *_: {})
    monkeypatch.setattr(subject, "market_now_trading", lambda _: False)
    monkeypatch.setattr(subject, "_should_suppress_realtime_history_poll", lambda **_: False)
    monkeypatch.setattr(subject, "_mark_user_request", lambda *_: None)
    monkeypatch.setattr(subject, "_get_chart_cache_entry_ram_only", lambda _: None)
    monkeypatch.setattr(subject, "market_frequencys", SimpleNamespace(
        cached_snapshot=lambda _: {"a": ["1m", "5m", "30m"], "us": ["5m"]},
    ))

    def request(*, market="a", provider="qmt", first=True, reason="cache_empty",
                force=False, candidate=False, resolution="5"):
        calls = []
        candidate_calls = []
        monkeypatch.setattr(subject.config, "EXCHANGE_A", provider)
        monkeypatch.setattr(
            subject, "_get_chart_cache_entry",
            lambda _: None if reason == "cache_empty" else {"min_time": 1000},
        )
        monkeypatch.setattr(
            subject, "evaluate_cache_for_tv_history",
            lambda *_args, **_kwargs: (False, None, reason, False),
        )

        def local_ready(*args):
            candidate_calls.append(args)
            return candidate

        def fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return None  # Stop at the real route's fetch boundary, without I/O.

        monkeypatch.setattr(subject, "fetch_klines_and_compute_cl_data", fetch)
        symbol = "SH.600926" if market == "a" else "AAPL.US"
        response = client.get("/tv/history", query_string={
            "symbol": f"{market}:{symbol}", "resolution": resolution,
            "firstDataRequest": "true" if first else "false",
            "from": "2000", "to": "3000", "force_refresh": "1" if force else "0",
        })
        assert response.status_code == 200
        assert response.get_json() == {"s": "no_data"}
        assert len(calls) == 1
        return calls[0][1], candidate_calls

    return request


@pytest.mark.parametrize("resolution", ["1", "5", "30"])
def test_initial_qmt_history_prefers_complete_local_source(fetch_history, resolution):
    options, _ = fetch_history(resolution=resolution)
    assert options["kline_args"]["args"] == {"prefer_local": True}
    assert "end_date" in options["kline_args"]
    assert "start_date" not in options["kline_args"]
    assert options["is_range_request"] is False
    assert options["force_refresh"] is False


def test_qmt_range_gap_preserves_both_bounds_and_prefers_local(fetch_history):
    options, candidate_calls = fetch_history(first=False, reason="tail_gap", candidate=True)
    assert options["kline_args"] == {
        "start_date": "1970-01-01 08:33:20",
        "end_date": "1970-01-01 08:50:00",
        "args": {"prefer_local": True},
    }
    assert options["is_range_request"] is True
    assert candidate_calls == []


def test_cold_range_does_not_shrink_full_history_request(fetch_history):
    options, _ = fetch_history(first=False)
    assert options["kline_args"]["args"] == {"prefer_local": True}
    assert "start_date" not in options["kline_args"]
    assert options["cache_miss_reason"] == "cache_empty"




@pytest.mark.parametrize(
    "first,reason", [(True, "cache_empty"), (True, "cache_force_refresh"),
                     (False, "cache_force_refresh")],
)
def test_force_refresh_never_prefers_or_skips_download(fetch_history, first, reason):
    options, candidate_calls = fetch_history(
        first=first, reason=reason, force=True, candidate=True,
    )
    assert "args" not in options["kline_args"]
    assert candidate_calls == []
    assert options["force_refresh"] is True


@pytest.mark.parametrize("market,provider", [("us", "qmt"), ("a", "tdx")])
@pytest.mark.parametrize("first,reason", [(True, "cache_empty"), (False, "tail_gap")])
def test_other_market_or_provider_gets_no_qmt_options(fetch_history, market, provider, first, reason):
    options, candidate_calls = fetch_history(
        market=market, provider=provider, first=first, reason=reason, candidate=True,
    )
    assert "args" not in options["kline_args"]
    assert candidate_calls == []
