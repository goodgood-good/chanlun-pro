from __future__ import annotations

from flask import Flask
import pytest

from cl_app import create_app
from cl_app.blueprints import tv as tv_module
from cl_app.blueprints.tv import _validated_review_chart_lock


class _LockService:
    def __init__(self) -> None:
        self.values = None

    def validate_chart_lock(self, **values):
        self.values = values
        return {
            **values,
            "symbol": "SH.600000",
            "review_available_at": "2026-07-20T10:30:00+08:00",
        }


def test_review_chart_lock_is_bound_to_server_validated_candidate() -> None:
    app = Flask(__name__)
    service = _LockService()
    app.extensions["decision_support_human_review"] = service
    candidate = "sha256:" + "1" * 64
    source = "sha256:" + "2" * 64

    with app.test_request_context(
        "/tv/history"
        f"?review_candidate_id={candidate}"
        f"&review_source_sha256={source}"
        "&review_as_of=1784784600"
    ):
        lock = _validated_review_chart_lock()

    assert lock["symbol"] == "SH.600000"
    assert service.values == {
        "candidate_id": candidate,
        "source_sha256": source,
        "review_as_of": 1784784600,
    }


def test_partial_review_chart_lock_is_rejected() -> None:
    app = Flask(__name__)
    app.extensions["decision_support_human_review"] = _LockService()

    with app.test_request_context(
        "/tv/history?review_candidate_id=sha256:missing-fields"
    ), pytest.raises(ValueError, match="partial"):
        _validated_review_chart_lock()


def test_normal_chart_has_no_review_lock() -> None:
    app = Flask(__name__)
    with app.test_request_context("/tv/history"):
        assert _validated_review_chart_lock() is None


def test_history_fetch_and_structure_snapshot_are_cut_off_before_future_bars(
    monkeypatch,
) -> None:
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    cap = 1_784_784_600
    candidate = "sha256:" + "4" * 64
    lock = {
        "candidate_id": candidate,
        "source_sha256": "sha256:" + "5" * 64,
        "review_as_of": cap,
        "symbol": "SH.600000",
    }
    observed = {}

    monkeypatch.setattr(tv_module, "_validated_review_chart_lock", lambda: lock)
    monkeypatch.setattr(tv_module, "query_cl_chart_config", lambda *_args: {})
    monkeypatch.setattr(
        tv_module.market_frequencys,
        "cached_snapshot",
        lambda *_args, **_kwargs: {"a": ["5m"]},
    )
    monkeypatch.setattr(tv_module, "_get_chart_cache_entry", lambda key: observed.setdefault("cache_key", key) and None)
    monkeypatch.setattr(
        tv_module,
        "submit_revalidation",
        lambda *_args: pytest.fail("historical review must not live-revalidate"),
    )

    def fetch(*_args, **kwargs):
        observed["kline_args"] = kwargs["kline_args"]
        observed["to_ts"] = kwargs["to_ts"]
        times = [cap - 900, cap - 600, cap - 300]
        values = [10.0, 10.1, 10.2]
        return {
            "cl_chart_data": {
                "t": times,
                "o": values,
                "h": values,
                "l": values,
                "c": values,
                "v": [100.0, 100.0, 100.0],
            },
            "cd": None,
            "is_full_snapshot": True,
            "cache_already_written": False,
        }

    monkeypatch.setattr(tv_module, "fetch_klines_and_compute_cl_data", fetch)
    try:
        response = app.test_client().get(
            "/tv/history?symbol=a:SH.600000&resolution=5"
            f"&firstDataRequest=true&from={cap - 86400}&to={cap + 86400}"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 200
    assert response.get_json()["s"] == "ok"
    assert max(response.get_json()["t"]) <= cap
    assert observed["to_ts"] == cap
    assert observed["kline_args"]["end_date"] == "2026-07-23 13:30:00"
    assert f"_review_{candidate[7:]}_{cap}" in observed["cache_key"]


def test_chart_page_injects_verified_lock_and_disables_sse() -> None:
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    candidate = "sha256:" + "6" * 64
    source = "sha256:" + "7" * 64
    as_of = 1_784_784_600

    class Service:
        def validate_chart_lock(self, **values):
            return {
                **values,
                "symbol": "SH.600000",
                "review_available_at": "2026-07-27T13:30:00+08:00",
            }

    app.extensions["decision_support_human_review"] = Service()
    try:
        response = app.test_client().get(
            "/?market=a&code=SH.600000&layout=single&intervals=30"
            f"&review_candidate_id={candidate}"
            f"&review_source_sha256={source}&review_as_of={as_of}"
        )
        mismatch = app.test_client().get(
            "/?market=a&code=SH.600001&layout=single&intervals=30"
            f"&review_candidate_id={candidate}"
            f"&review_source_sha256={source}&review_as_of={as_of}"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "window.__CHANLUN_SSE_ENABLED = false" in html
    assert "window.__chanlunReviewChartLock" in html
    assert candidate in html
    assert source in html
    assert mismatch.status_code == 404
