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


def test_risk_point_audit_lock_falls_back_from_human_review_and_keeps_focus(
    monkeypatch: pytest.MonkeyPatch,
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
    point_id = "sha256:" + "8" * 64
    audit_id = "sha256:" + "9" * 64
    as_of = 1_784_784_600
    focus_at = as_of - 86400
    observed = {}

    def validate(_root, **values):
        observed.update(values)
        return {
            "candidate_id": values["point_id"],
            "source_sha256": values["source_sha256"],
            "review_as_of": values["review_as_of"],
            "symbol": "SH.000001",
            "review_available_at": "2026-07-23T13:30:00+08:00",
            "focus_at": focus_at,
            "point_available_at": "2026-07-22T13:30:00+08:00",
            "point_type": "1sell",
            "source_frequency": "30m",
            "chart_interval": "30",
            "lock_kind": "RISK_POINT_AUDIT",
        }

    monkeypatch.setattr(
        "cl_app.services.research_audit.validate_risk_point_chart_lock",
        validate,
    )
    try:
        url = (
            "/?market=a&code=SH.000001&layout=single&intervals=30"
            f"&review_candidate_id={point_id}"
            f"&review_source_sha256={audit_id}&review_as_of={as_of}"
        )
        response = app.test_client().get(url)
        wrong_interval = app.test_client().get(url.replace("intervals=30", "intervals=5"))
        with app.test_request_context(
            "/tv/history"
            f"?review_candidate_id={point_id}"
            f"&review_source_sha256={audit_id}&review_as_of={as_of}"
        ):
            lock = _validated_review_chart_lock()
        history_wrong_interval = app.test_client().get(
            "/tv/history?symbol=a:SH.000001&resolution=5&from=0&to=0"
            f"&review_candidate_id={point_id}"
            f"&review_source_sha256={audit_id}&review_as_of={as_of}"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert wrong_interval.status_code == 404
    assert history_wrong_interval.get_json() == {"s": "no_data"}
    assert '"lock_kind": "RISK_POINT_AUDIT"' in html
    assert f'"focus_at": {focus_at}' in html
    assert lock["focus_at"] == focus_at
    assert observed == {
        "point_id": point_id,
        "source_sha256": audit_id,
        "review_as_of": as_of,
    }


def test_verified_sector_archive_routes_never_fall_through_to_live_exchange(
    monkeypatch: pytest.MonkeyPatch,
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
    sector = "qmt-gics3:" + "a" * 64
    cutoff = 1_784_784_600
    lock = {
        "candidate_id": "sha256:" + "b" * 64,
        "source_sha256": "sha256:" + "c" * 64,
        "review_as_of": cutoff,
        "symbol": sector,
        "chart_interval": "30",
        "lock_kind": "RISK_POINT_AUDIT",
        "chart_source_kind": "VERIFIED_QMT_SECTOR_ARCHIVE",
        "sector_chart_archive_entry_id": "sha256:" + "d" * 64,
    }
    archive = object()
    monkeypatch.setattr(tv_module, "_validated_review_chart_lock", lambda: lock)
    monkeypatch.setattr(
        tv_module,
        "_sector_chart_archive_for_lock",
        lambda value: (archive, lock["sector_chart_archive_entry_id"]),
    )
    monkeypatch.setattr(
        tv_module,
        "get_exchange",
        lambda *_args: pytest.fail("sector archive must not query live exchange"),
    )
    monkeypatch.setattr(
        "cl_app.services.sector_chart_archive.sector_chart_symbol_info",
        lambda loaded, **kwargs: {
            "name": sector,
            "ticker": f"a:{sector}",
            "supported_resolutions": [kwargs["interval"]],
        },
    )
    observed = {}

    def history(loaded, **kwargs):
        observed.update(kwargs)
        assert loaded is archive
        return {
            "s": "ok",
            "t": [cutoff - 1800],
            "o": [1.0],
            "h": [1.0],
            "l": [1.0],
            "c": [1.0],
            "v": [1.0],
        }

    monkeypatch.setattr(
        "cl_app.services.sector_chart_archive.sector_chart_history_payload",
        history,
    )
    try:
        client = app.test_client()
        symbols = client.get(f"/tv/symbols?symbol=a:{sector}")
        bars = client.get(
            f"/tv/history?symbol=a:{sector}&resolution=30"
            f"&from={cutoff - 86400}&to={cutoff + 86400}"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert symbols.status_code == 200
    assert symbols.get_json()["ticker"] == f"a:{sector}"
    assert bars.get_json()["s"] == "ok"
    assert observed == {
        "entry_id": lock["sector_chart_archive_entry_id"],
        "interval": "30",
        "from_ts": cutoff - 86400,
        "to_ts": cutoff,
    }
