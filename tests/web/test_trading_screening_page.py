from __future__ import annotations

import gzip
import json

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from cl_app.blueprints import decision_support as decision_support_module
from cl_app.blueprints.decision_support import _presentation_scope, decision_support_bp
from cl_app.services.human_review_screening import HumanReviewScreenUnavailable
from cl_app.services.realtime_quotes import (
    AShareDisplayQuoteBatch,
    AShareRealtimeQuote,
)


class _User(UserMixin):
    id = "trading-screening-user"


class _TradingScreeningService:
    def __init__(self) -> None:
        self.refresh_requests = 0

    def ensure_refresh(self) -> bool:
        self.refresh_requests += 1
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {
            "ready": True,
            "status": "ready",
            "worker_alive": True,
            "reasons": [],
            "priority_monitoring_enabled": True,
            "priority_monitor_ready": True,
            "priority_monitor_session_open": True,
            "screening_scope_mode": "VALIDATION_COHORT",
            "validation_cohort_size": 12,
            "effective_monitor_universe_limit": 12,
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": "chanlun-trading-screening",
            "algorithm_id": "chanlun_source_faithful",
            "structure_contract_id": "physical-timeframe-recursive",
            "available": True,
            "scan_state": "complete",
            "generated_at": "2026-07-20T15:00:00+08:00",
            "as_of": "2026-07-20T15:00:00+08:00",
            "sector_first": True,
            "read_only": True,
            "research_only": True,
            "no_order_execution": True,
            "screening_scope": {
                "schema": "chanlun-screening-scope-v1",
                "mode": "VALIDATION_COHORT",
                "validation_cohort_size": 12,
                "effective_monitor_universe_limit": 12,
            },
            "counts_by_stage": {"triggered": 1},
            "counts_by_point_type": {
                "1buy": 0,
                "2buy": 1,
                "3buy": 0,
                "1sell": 0,
                "2sell": 0,
                "3sell": 0,
            },
            "sectors": [],
            "signals": [],
            "risk_limits": {},
            "scan_audit": {
                "sector_discovered_count": 10,
                "sector_completed_count": 9,
                "sector_failed_count": 1,
                "sector_completion_ratio": "0.9",
            },
            "data_quality": {"complete": True, "stale": False},
            "backtest_verdict": {
                "live_ready": False,
                "status": "evidence_insufficient",
            },
            "errors": [],
        }


@pytest.fixture
def app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../../web/chanlun_chart/cl_app/templates",
        static_folder="../../web/chanlun_chart/cl_app/static",
    )
    app.config.update(TESTING=True, SECRET_KEY="trading-screening-test-secret")
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return _User() if user_id == _User.id else None

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify(ok=False, code="authentication_required"), 401

    @app.get("/_test/login")
    def login():
        login_user(_User())
        return {"ok": True}

    app.extensions["decision_support_trading_screening"] = (
        _TradingScreeningService()
    )
    app.register_blueprint(decision_support_bp)
    return app


@pytest.fixture
def logged_in_client(app: Flask):
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200
    return client


def test_early_signals_requires_new_schema(app: Flask, logged_in_client) -> None:
    response = logged_in_client.get("/decision-support/early-signals")
    payload = response.get_json()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert payload["ok"] is True
    assert payload["data"]["schema"] == "chanlun-trading-screening"
    assert payload["data"]["structure_contract_id"] == "physical-timeframe-recursive"
    assert payload["data"]["presentation_scope"] == "all-qualified"
    assert payload["data"]["presentation_signal_count"] == 0
    assert payload["data"]["total_qualified_signal_count"] == 0
    assert payload["data"]["runtime_health"] == {
        "required": True,
        "ready": True,
        "status": "ready",
            "worker_alive": True,
            "reasons": [],
            "priority_monitoring_enabled": True,
            "priority_monitor_ready": True,
            "priority_monitor_session_open": True,
            "screening_scope_mode": "VALIDATION_COHORT",
            "validation_cohort_size": 12,
            "effective_monitor_universe_limit": 12,
            "snapshot_hash_coverage": "EXCLUDED_OPERATIONAL_METADATA",
    }
    assert payload["data"]["screening_scope"] == {
        "schema": "chanlun-screening-scope-v1",
        "mode": "VALIDATION_COHORT",
        "validation_cohort_size": 12,
        "effective_monitor_universe_limit": 12,
    }
    assert payload["data"]["manual_attention"] == {
        "schema": "chanlun-local-manual-attention",
        "source": "LOCAL_GLOBAL_ATTENTION_GROUP",
        "group_name": "人工关注组",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": False,
        "status": "unavailable",
        "symbols": [],
        "declared_count": 0,
            "priority_monitor_count": 0,
            "cross_market_monitor_count": 0,
            "covered_monitor_count": 0,
            "unsupported_market_count": 0,
    }
    assert payload["data"]["us_monitor"]["schema"] == (
        "chanlun-us-realtime-monitor"
    )
    assert payload["data"]["us_monitor"]["available"] is False
    assert payload["data"]["us_monitor"]["reason_code"] == (
        "US_MONITOR_UNAVAILABLE"
    )
    assert payload["data"]["us_monitor"]["op_level"] == "5m"
    assert payload["data"]["us_monitor"]["mid_level"] == "1m"
    assert payload["data"]["us_monitor"]["selection_candidates"] is False
    assert payload["data"]["realtime_notifications"] == {
        "schema": "chanlun-realtime-review-inbox",
        "events": [],
        "event_count": 0,
        "pending_review_count": 0,
        "delivery_counts": {},
        "credentials_exposed": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    service = app.extensions["decision_support_trading_screening"]
    assert service.refresh_requests == 0


def test_early_signals_compresses_large_response_when_client_accepts_gzip(
    app: Flask,
    logged_in_client,
) -> None:
    service = app.extensions["decision_support_trading_screening"]
    original_snapshot = service.snapshot

    def large_snapshot() -> dict[str, object]:
        payload = original_snapshot()
        payload["errors"] = ["x" * (40 * 1024)]
        return payload

    service.snapshot = large_snapshot
    response = logged_in_client.get(
        "/decision-support/early-signals",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response.vary
    payload = json.loads(gzip.decompress(response.get_data()))
    assert payload["ok"] is True
    assert payload["data"]["errors"] == ["x" * (40 * 1024)]


def test_early_signals_reuses_gzip_bytes_for_the_same_content_revision(
    app: Flask,
    logged_in_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = app.extensions["decision_support_trading_screening"]
    original_snapshot = service.snapshot

    def large_snapshot() -> dict[str, object]:
        payload = original_snapshot()
        payload["presentation_revision"] = "sha256:http-cache-test"
        payload["errors"] = ["stable" * (8 * 1024)]
        return payload

    service.snapshot = large_snapshot
    decision_support_module._JSON_GZIP_CACHE.clear()
    original_compress = decision_support_module.gzip.compress
    compression_calls = 0

    def recording_compress(*args, **kwargs):
        nonlocal compression_calls
        compression_calls += 1
        return original_compress(*args, **kwargs)

    monkeypatch.setattr(
        decision_support_module.gzip,
        "compress",
        recording_compress,
    )
    first = logged_in_client.get(
        "/decision-support/early-signals",
        headers={"Accept-Encoding": "gzip"},
    )
    second = logged_in_client.get(
        "/decision-support/early-signals",
        headers={"Accept-Encoding": "gzip"},
    )

    assert first.status_code == second.status_code == 200
    assert first.get_data() == second.get_data()
    assert first.headers["X-Content-Revision"] == second.headers[
        "X-Content-Revision"
    ]
    assert compression_calls == 1


def test_early_signals_revalidates_semantically_unchanged_runtime_health(
    app: Flask,
    logged_in_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = app.extensions["decision_support_trading_screening"]
    original_snapshot = service.snapshot
    health_snapshots = iter(
        (
            {
                "ready": True,
                "status": "ready",
                "worker_alive": True,
                "reasons": [],
                "heartbeat_at": "2026-08-26T10:49:21.570134+08:00",
                "heartbeat_age_seconds": 0.07,
                "priority_monitor_age_seconds": 79.63,
                "refresh_elapsed_seconds": 19.63,
                "candidate_monitor_five_minute": {
                    "status": "verified",
                    "oldest_observation_age_seconds": 259.63,
                },
                "candidate_monitor_thirty_minute": {
                    "status": "verified",
                    "oldest_observation_age_seconds": 1819.63,
                },
                "native_gateway": {
                    "ready": True,
                    "status": "ready",
                    "completed_request_count": 469,
                    "total_completed_request_count": 469,
                    "last_progress_at": "2026-08-26T10:49:17+08:00",
                    "last_response_at": "2026-08-26T10:49:17+08:00",
                },
            },
            {
                "ready": True,
                "status": "ready",
                "worker_alive": True,
                "reasons": [],
                "heartbeat_at": "2026-08-26T10:49:21.776230+08:00",
                "heartbeat_age_seconds": 0.23,
                "priority_monitor_age_seconds": 79.99,
                "refresh_elapsed_seconds": 19.99,
                "candidate_monitor_five_minute": {
                    "status": "verified",
                    "oldest_observation_age_seconds": 259.99,
                },
                "candidate_monitor_thirty_minute": {
                    "status": "verified",
                    "oldest_observation_age_seconds": 1820.0,
                },
                "native_gateway": {
                    "ready": True,
                    "status": "ready",
                    "completed_request_count": 471,
                    "total_completed_request_count": 471,
                    "last_progress_at": "2026-08-26T10:49:21+08:00",
                    "last_response_at": "2026-08-26T10:49:21+08:00",
                },
            },
            {
                "ready": False,
                "status": "not_ready",
                "worker_alive": False,
                "reasons": ["screening_worker_not_alive"],
            },
        )
    )

    def versioned_snapshot() -> dict[str, object]:
        payload = original_snapshot()
        payload["presentation_revision"] = "sha256:stable-presentation"
        return payload

    service.snapshot = versioned_snapshot
    service.health_snapshot = lambda: next(health_snapshots)
    original_compact = decision_support_module._compact_early_signals_transport
    compact_calls = 0

    def recording_compact(data):
        nonlocal compact_calls
        compact_calls += 1
        return original_compact(data)

    monkeypatch.setattr(
        decision_support_module,
        "_compact_early_signals_transport",
        recording_compact,
    )
    endpoint = "/decision-support/early-signals?transport=signal-catalog-v1"

    first = logged_in_client.get(endpoint)
    etag = first.headers["ETag"]
    unchanged = logged_in_client.get(
        endpoint,
        headers={"If-None-Match": etag},
    )
    changed = logged_in_client.get(
        endpoint,
        headers={"If-None-Match": etag},
    )

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == (
        "private, no-cache, must-revalidate"
    )
    assert etag.startswith('W/"sha256:')
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""
    assert unchanged.headers["ETag"] == etag
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag
    assert changed.get_json()["data"]["runtime_health"]["ready"] is False
    assert compact_calls == 2


def test_early_signals_catalog_transport_deduplicates_browser_only_evidence(
    app: Flask,
    logged_in_client,
) -> None:
    service = app.extensions["decision_support_trading_screening"]
    original_snapshot = service.snapshot
    shared_fields = {
        "execution_profile": {"recommendation": "WAITING_STRUCTURE"},
        "higher_timeframe_risk": {
            "market_gate": "GREEN",
            "sector_gate": "GREEN",
            "symbol_gate": "GREEN",
        },
        "position_recommendation": {"status": "NOT_ACTIONABLE"},
        "sector": {"sector_id": "qmt:test", "sector_name": "测试板块"},
        "context_30m": {"direction": "up"},
        "context_d": {"direction": "up"},
        "decision_reasons": ["waiting_structure"],
        "warmup": {"converged": True},
    }

    def snapshot_with_repeated_evidence() -> dict[str, object]:
        payload = original_snapshot()
        payload["presentation_revision"] = "sha256:catalog-test"
        payload["sector_strength_evidence"] = {"blob": "x" * 4096}
        payload["admitted_universe_codes"] = ["SZ.000001", "SZ.000002"]
        payload["decision_source_snapshot"] = {"blob": "audit-only"}
        payload["sector_exclusions"] = [{"sector_id": "excluded"}]
        payload["sector_parent_relations"] = [{"sector_id": "child"}]
        payload["signals"] = [
            {
                "signal_id": f"signal-{index}",
                "code": f"SZ.{index:06d}",
                "point_type": "1buy",
                "lifecycle_stage": "observed",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
                **shared_fields,
            }
            for index in (1, 2)
        ]
        return payload

    service.snapshot = snapshot_with_repeated_evidence

    full = logged_in_client.get("/decision-support/early-signals")
    compact = logged_in_client.get(
        "/decision-support/early-signals?transport=signal-catalog-v1"
    )
    invalid = logged_in_client.get(
        "/decision-support/early-signals?transport=unknown"
    )
    full_data = full.get_json()["data"]
    compact_data = compact.get_json()["data"]

    assert full.status_code == compact.status_code == 200
    assert invalid.status_code == 400
    assert compact.headers["Cache-Control"] == (
        "private, no-cache, must-revalidate"
    )
    assert compact_data["signal_transport"] == "signal-catalog-v1"
    assert compact_data["signal_catalog"]["schema"] == (
        "chanlun-early-signals-signal-catalog-v1"
    )
    assert compact_data["signals"][0]["signal_catalog_refs"] == (
        compact_data["signals"][1]["signal_catalog_refs"]
    )
    for field in shared_fields:
        assert field in full_data["signals"][0]
        assert field not in compact_data["signals"][0]
        assert len(compact_data["signal_catalog"]["values"][field]) == 1
    for field in (
        "sector_strength_evidence",
        "admitted_universe_codes",
        "decision_source_snapshot",
        "sector_exclusions",
        "sector_parent_relations",
    ):
        assert field in full_data
        assert field not in compact_data
    assert len(compact.get_data()) < len(full.get_data())


def test_early_signals_projects_only_us_auxiliary_monitor_positions(
    app: Flask,
    logged_in_client,
) -> None:
    class _Monitor:
        @staticmethod
        def health_snapshot() -> dict[str, object]:
            return {
                "schema": "chanlun-holding-group-monitor",
                "ready": False,
                "status": "degraded",
                "reason_code": "HOLDING_MONITOR_DEGRADED",
                "job_registered": True,
                "notification_configured": True,
                "interval_seconds": 60,
                "op_level": "5m",
                "mid_level": "1m",
                "big_level": "30m",
                "last_run_at": "2026-08-14T22:30:00+08:00",
                "last_completed_at": "2026-08-14T22:30:01+08:00",
                "stale": False,
                "positions": [
                    {
                        "market": "us",
                        "code": "QCOM.US",
                        "name": "高通",
                        "groups": ["我的关注"],
                        "monitoring_scope": "WATCHLIST",
                        "status": "monitoring",
                        "reason_code": "MONITORING_ACTIVE",
                        "event_present": False,
                    },
                    {
                        "market": "us",
                        "code": "QQQ.US",
                        "name": "纳指100ETF",
                        "groups": ["ETF"],
                        "monitoring_scope": "WATCHLIST",
                        "status": "error",
                        "reason_code": "MARKET_DATA_OR_STRUCTURE_REFRESH_FAILED",
                        "event_present": False,
                    },
                    {
                        "market": "hk",
                        "code": "HK.00700",
                        "name": "腾讯控股",
                        "groups": ["我的关注"],
                        "monitoring_scope": "WATCHLIST",
                        "status": "monitoring",
                    },
                ],
                "notification_delivery": {"failure_count": 1},
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }

    app.extensions["holding_group_monitor"] = _Monitor()
    payload = logged_in_client.get("/decision-support/early-signals").get_json()
    monitor = payload["data"]["us_monitor"]

    assert monitor["available"] is True
    assert monitor["ready"] is False
    assert monitor["status"] == "degraded"
    assert [row["code"] for row in monitor["symbols"]] == [
        "QCOM.US",
        "QQQ.US",
    ]
    assert monitor["declared_count"] == 2
    assert monitor["active_count"] == 1
    assert monitor["failed_count"] == 1
    assert monitor["covered_count"] == 1
    assert monitor["selection_candidates"] is False
    assert monitor["op_level"] == "5m"
    assert monitor["mid_level"] == "1m"
    assert monitor["research_only"] is True
    assert monitor["no_order_execution"] is True
    assert monitor["manual_review_required"] is True
    serialized = json.dumps(monitor, ensure_ascii=False)
    assert not any(
        term in serialized
        for term in ("账户", "持仓", "仓位", "positions", "real_account", "HOLDING")
    )


def test_early_signals_supports_bounded_sector_scope_and_unfiltered_scope(
    app: Flask,
    logged_in_client,
) -> None:
    service = app.extensions["decision_support_trading_screening"]
    app.config["TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"] = lambda: {
        "schema": "chanlun-local-manual-holdings",
        "source": "LOCAL_GLOBAL_WATCHLIST_GROUP",
        "group_name": "我的持仓",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": True,
        "status": "ready",
        "positions": [
            {
                "market": "a",
                "code": "SZ.000001",
                "name": "平安银行",
                "monitoring_scope": "A_SHARE_STRICT_DECISION_CORE",
                "decision_mode": "UNIFIED_HUMAN_ASSISTED_DECISION_CORE",
            }
        ],
        "declared_count": 1,
        "priority_monitor_count": 1,
        "cross_market_monitor_count": 0,
        "covered_monitor_count": 1,
        "unsupported_market_count": 0,
        "quantity_available": False,
        "cost_basis_available": False,
        "sellable_quantity_available": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    snapshot = service.snapshot()
    snapshot["signals"] = [
        {
            "signal_id": "sha256:" + "1" * 64,
            "code": "SH.600000",
            "point_type": "3buy",
            "lifecycle_stage": "triggered",
            "selection_sources": ["QMT_SECTOR_TRIGGER"],
        },
        {
            "signal_id": "sha256:" + "2" * 64,
            "code": "SZ.000001",
            "point_type": "2buy",
            "lifecycle_stage": "observed",
            "selection_sources": ["QMT_SECTOR_ELIGIBLE_SCOPE"],
        },
    ]
    service.snapshot = lambda: snapshot

    sector_payload = logged_in_client.get(
        "/decision-support/early-signals?scope=sector-trigger"
    ).get_json()["data"]
    all_payload = logged_in_client.get(
        "/decision-support/early-signals"
    ).get_json()["data"]

    assert sector_payload["presentation_scope"] == "sector-trigger"
    assert [row["code"] for row in sector_payload["signals"]] == ["SH.600000"]
    assert sector_payload["presentation_signal_count"] == 1
    assert sector_payload["sector_trigger_signal_count"] == 1
    assert sector_payload["total_qualified_signal_count"] == 2
    assert sector_payload["counts_by_stage"] == {"triggered": 1}
    assert [
        row["code"] for row in sector_payload["manual_attention_signals"]
    ] == ["SZ.000001"]
    assert all_payload["presentation_scope"] == "all-qualified"
    assert len(all_payload["signals"]) == 2
    assert all_payload["manual_attention_signals"] == []
    assert all_payload["counts_by_point_type"] == {"3buy": 1, "2buy": 1}


def test_server_projection_preserves_declared_attention_signal_stage() -> None:
    output = {
        "signals": [
            {
                "signal_id": "sha256:" + "3" * 64,
                "code": "SZ.301004",
                "point_type": "3buy",
                "lifecycle_stage": "approaching",
                "selection_sources": ["QMT_SECTOR_TRIGGER", "HOLDING_MONITOR"],
                "setup_5m": {
                    "point_type": "3buy",
                    "status": "provisional",
                    "evidence_codes": [
                        "provisional_center_completion",
                        "core_boundary_held",
                    ],
                },
            }
        ],
        "manual_attention": {
            "symbols": [{"market": "a", "code": "SZ.301004"}],
        },
    }

    projected = _presentation_scope(output, "sector-trigger")

    assert projected["signals"][0]["lifecycle_stage"] == "approaching"
    assert projected["manual_attention_signals"][0]["lifecycle_stage"] == "approaching"
    assert projected["counts_by_stage"] == {"approaching": 1}


def test_server_projection_excludes_terminal_and_legacy_lifecycle_rows() -> None:
    output = {
        "signals": [
            {
                "signal_id": "current",
                "code": "SZ.000001",
                "point_type": "3buy",
                "lifecycle_stage": "triggered",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
            },
            {
                "signal_id": "legacy-formed",
                "code": "SZ.000003",
                "point_type": "3buy",
                "lifecycle_stage": "formed",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
            },
            {
                "signal_id": "legacy-armed",
                "code": "SZ.000004",
                "point_type": "2buy",
                "lifecycle_stage": "armed",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
            },
            {
                "signal_id": "invalidated",
                "code": "SZ.000002",
                "point_type": "3sell",
                "lifecycle_stage": "invalidated",
                "selection_sources": ["QMT_SECTOR_TRIGGER"],
            },
        ],
        "manual_attention": {"symbols": []},
    }

    projected = _presentation_scope(output, "all-qualified")

    assert [row["signal_id"] for row in projected["signals"]] == ["current"]
    assert projected["total_qualified_signal_count"] == 1
    assert projected["counts_by_stage"] == {"triggered": 1}


def test_early_signals_exposes_cross_market_manual_attention_without_account_fields(
    app: Flask,
    logged_in_client,
) -> None:
    app.config["TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"] = lambda: {
        "schema": "chanlun-local-manual-holdings",
        "source": "LOCAL_GLOBAL_WATCHLIST_GROUP",
        "group_name": "我的持仓",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": True,
        "status": "ready",
        "positions": [
            {
                "market": "a",
                "code": "SH.600000",
                "name": "浦发银行",
                "monitoring_scope": "A_SHARE_STRICT_DECISION_CORE",
                "decision_mode": "UNIFIED_HUMAN_ASSISTED_DECISION_CORE",
            },
            {
                "market": "hk",
                "code": "HK.00700",
                "name": "腾讯控股",
                "monitoring_scope": "NON_A_AUXILIARY_STRUCTURE_RADAR",
                "decision_mode": "STRICT_STRUCTURE_OBSERVATION_ONLY",
            },
        ],
        "declared_count": 2,
        "priority_monitor_count": 1,
        "cross_market_monitor_count": 1,
        "covered_monitor_count": 2,
        "unsupported_market_count": 0,
        "quantity_available": False,
        "cost_basis_available": False,
        "sellable_quantity_available": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    quote_requests: list[tuple[str, ...]] = []

    def quotes(codes: tuple[str, ...]) -> AShareDisplayQuoteBatch:
        quote_requests.append(codes)
        return AShareDisplayQuoteBatch(
            requested_codes=codes,
            market_open=False,
            quotes=(
                AShareRealtimeQuote(
                    code="SH.600000",
                    last=10.25,
                    buy1=10.24,
                    sell1=10.25,
                    high=10.5,
                    low=10.1,
                    open=10.2,
                    volume=1000.0,
                    rate=1.75,
                ),
            ),
            tick_data_used=True,
        )

    app.config["TRADING_SCREENING_NATIVE_PROCESS_ISOLATION"] = True
    app.extensions["a_share_realtime_quotes"] = quotes

    payload = logged_in_client.get("/decision-support/early-signals").get_json()
    attention = payload["data"]["manual_attention"]

    assert attention["available"] is True
    assert attention["group_scope"] == "GLOBAL_ACROSS_MARKETS"
    assert [row["market"] for row in attention["symbols"]] == ["a", "hk"]
    assert attention["priority_monitor_count"] == 1
    assert attention["cross_market_monitor_count"] == 1
    assert attention["covered_monitor_count"] == 2
    assert attention["unsupported_market_count"] == 0
    assert attention["symbols"][0]["realtime_status"] == "monitoring"
    assert attention["symbols"][0]["realtime_reason_code"] == (
        "A_SHARE_STRICT_DECISION_CORE_ACTIVE"
    )
    assert quote_requests == [("SH.600000",)]
    assert attention["quote_status"] == "ready"
    assert attention["quote_market_open"] is False
    assert attention["quote_available_count"] == 1
    assert attention["symbols"][0]["quote_available"] is True
    assert attention["symbols"][0]["current_price"] == 10.25
    assert attention["symbols"][0]["change_percent"] == 1.75
    assert attention["symbols"][1]["realtime_status"] == "awaiting_first_run"
    assert attention["symbols"][1]["quote_available"] is False
    assert "real_account_accessed" not in attention
    assert "automated_order_authorized" not in attention
    serialized = json.dumps(attention, ensure_ascii=False)
    assert not any(
        term in serialized
        for term in (
            "账户",
            "持仓",
            "仓位",
            "现金",
            "我的持仓",
            "manual_holdings",
            "positions",
            "real_account",
            "HOLDING",
        )
    )


def test_early_signals_fails_closed_on_inconsistent_manual_attention_source(
    app: Flask,
    logged_in_client,
) -> None:
    app.config["TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"] = lambda: {
        "schema": "chanlun-local-manual-holdings",
        "source": "LOCAL_GLOBAL_WATCHLIST_GROUP",
        "group_name": "我的持仓",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": True,
        "status": "ready",
        "positions": [
            {
                "market": "hk",
                "code": "HK.00700",
                "name": "腾讯控股",
                # A non-A-share row may not claim an A-share monitoring lane.
                "monitoring_scope": "A_SHARE_STRICT_DECISION_CORE",
            }
        ],
        "declared_count": 1,
        "priority_monitor_count": 1,
        "cross_market_monitor_count": 1,
        "covered_monitor_count": 1,
        "unsupported_market_count": 0,
        "quantity_available": True,
        "cost_basis_available": False,
        "sellable_quantity_available": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }

    payload = logged_in_client.get("/decision-support/early-signals").get_json()
    attention = payload["data"]["manual_attention"]

    assert attention["available"] is False
    assert attention["symbols"] == []
    assert "quantity_available" not in attention
    assert "real_account_accessed" not in attention


def test_alert_records_route_is_removed(logged_in_client) -> None:
    response = logged_in_client.get("/alert_records/a")

    assert response.status_code == 404


def test_screening_page_uses_new_three_workspace_contract(
    logged_in_client,
) -> None:
    response = logged_in_client.get("/decision-support/early-screening")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    service = logged_in_client.application.extensions[
        "decision_support_trading_screening"
    ]
    assert service.refresh_requests == 0
    assert 'data-schema="chanlun-trading-screening"' in html
    assert 'id="es-sector-completion"' in html
    assert 'id="es-member-history"' in html
    assert 'id="es-holdings-title"' in html
    assert 'id="es-holdings-list"' in html
    assert 'id="es-holdings-declared"' in html
    assert 'id="es-holdings-monitored"' in html
    assert 'id="es-holdings-unsupported"' in html
    assert 'id="es-sector-trigger-count"' in html
    assert 'id="es-total-qualified-count"' in html
    assert "当前候选范围" in html
    assert "板块真实触发" in html
    assert "板块质量" in html
    assert "QMT GICS3 / GICS4 行业层级" in html
    assert "按 QMT GICS3 父行业门控" in html
    assert "优先采用 GICS4 子行业结构与长期强弱" in html
    assert "子行业数据不足时回退父行业" in html
    assert "子行业结构明确不利时不回退" in html
    assert "成员日线" in html
    assert "板块按点时成分的中长期相对结构排序" in html
    assert "不按当日涨跌追强" in html
    assert "日线、30分钟、5分钟、1分钟均只使用决策时已经完成的K线" in html
    assert "日线和30分钟证据不足时标记“待判定”" in html
    assert (
        "5分钟操作确认决定主信号和首报，1分钟同向区间套决定是否进入精确执行候选"
        in html
    )
    assert "结构、复权或行情证据失真时关闭操作资格" in html
    assert "日线与30分钟负责环境分级" in html
    assert "周线和月线不参与当前执行判断" not in html
    assert "未来除权改写既有排序" not in html
    assert 'data-selection-scope="sector-trigger"' in html
    assert 'data-selection-scope="all-qualified"' in html
    assert "全部市场 · 全部来源 · 全部状态 · 全部买卖点" in html
    assert 'data-market="a"' in html
    assert 'data-market="us"' in html
    assert 'data-signal-source="notification"' in html
    assert 'data-review-stage="notified"' in html
    assert 'data-lifecycle="monitoring"' in html
    assert 'id="es-filter-reset"' in html
    assert 'id="es-us-monitor" class="es-us-monitor-compact"' in html
    assert 'id="es-us-monitor-list"' not in html
    assert "板块选择只筛 A 股，美股线索不参与板块门且会继续保留" in html
    assert html.count("data-workspace=") == 3
    assert 'data-workspace="sector"' in html
    assert 'data-workspace="signals"' in html
    assert 'data-workspace="charts"' in html
    assert 'data-evidence-toggle' in html
    assert 'data-evidence-count' in html
    assert 'data-theater-toggle' in html
    assert 'aria-controls="es-structure-evidence"' in html
    assert 'aria-controls="es-chart-workspace"' in html
    assert 'id="es-structure-evidence"' in html
    assert 'data-evidence-panel' in html
    assert 'data-evidence-close' in html
    assert "30m 环境分级" in html
    assert "5m 买卖点确认" in html
    assert "1m 区间套定位" in html
    assert "逆风只降级为谨慎复核" in html
    assert "1分钟买卖点不创造主信号，也不阻止5分钟结构首报" in html
    assert "必须形成同向区间套后，才升级为精确执行候选并生成结构比例参考" in html
    assert "5分钟确认结构信号；1分钟同向区间套解锁精确执行候选" in html
    assert "本系统仍不自动下单" in html
    assert "结构线索队列 · 人工复核" in html
    assert "买卖点线索队列" in html
    assert "线索只供人工识别，没有一条天然可执行" in html
    assert "ACTIONABLE QUEUE" not in html
    assert "实时模式图表会随市场继续更新" in html
    assert "不可作为历史因果复核" in html
    assert "请切换“人工复核选股”" in html
    assert "三买只取第一中枢" in html
    assert "历史研究/审计成果" in html
    assert "实时信号辅助 · 手工交易" in html
    assert "信号通知 · 无委托 · 人工确认" in html
    assert 'data-human-review-schema="chanlun-human-review-web"' in html
    assert 'data-default-mode="live"' in html
    assert (
        'data-screening-mode="human-review" aria-selected="false"'
    ) in html
    assert (
        'data-screening-mode="live" aria-selected="true" '
        'class="is-active"'
    ) in html
    assert "今日提前选股（实时）" in html
    assert 'id="hr-workspace"' in html
    assert 'id="hr-candidate-list"' in html
    assert 'id="hr-candidate-kind-filter"' in html
    assert 'id="hr-feedback-form"' in html
    for removed_id in (
        "hr-virtual-intent-count",
        "hr-virtual-pending-count",
        "hr-virtual-fill-count",
        "hr-virtual-position-count",
        "hr-portfolio-decision-audit-status",
        "hr-portfolio-fill-decision-audit-status",
        "hr-paper-cash-balance",
        "hr-paper-equity",
    ):
        assert f'id="{removed_id}"' not in html
    assert not any(
        term in html
        for term in (
            "账户",
            "现金",
            "持仓",
            "仓位",
            "持有",
            "虚拟",
            "组合热度",
            "硬阻断",
        )
    )
    assert 'id="hr-signal-lifecycle-status"' in html
    assert 'id="hr-tactical-execution-status"' in html
    assert 'id="hr-entry-confirmed-at"' in html
    assert 'id="hr-entry-price-cap"' in html
    assert 'id="hr-entry-valid-until"' in html
    assert 'id="hr-entry-attestation"' in html
    assert 'id="hr-market-risk"' in html
    assert 'id="hr-sector-risk"' in html
    assert 'id="hr-symbol-risk"' in html
    assert 'id="hr-sector-receipts"' in html
    assert 'id="hr-qmt-runtime-status"' in html
    assert 'id="hr-forward-scheduler-status"' in html
    assert 'id="hr-forward-screening-status"' in html
    assert 'id="hr-forward-archive-status"' in html
    assert 'id="hr-forward-delivery-status"' in html
    assert 'id="hr-decision-core-id"' in html
    assert 'id="hr-decision-source-id"' in html
    assert 'id="hr-markout-cohort-status"' in html
    assert 'id="hr-forward-lineage-status"' in html
    for horizon in (5, 10, 20):
        assert f'id="hr-markout-{horizon}"' in html
    assert "筛选观察" in html
    assert "观察样本版本" in html
    assert "暖机结构谱系" in html
    assert "不可评价" in html
    assert "human_review_screening.js" in html
    assert "human_review_markout_audit.js" in html
    assert html.index("human_review_markout_audit.js") < html.index(
        "human_review_screening.js"
    )
    assert "必须人工复核" in html
    assert "不自动下单" in html
    assert "REVIEW_REQUIRED" not in html
    assert "LIVE_DISABLED" not in html
    assert "因果图表锁定" in html
    for point_type in ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell"):
        assert f'data-point-type="{point_type}"' in html
    assert "AI 深度解读" not in html
    assert "原文课次与结构标签" not in html


def test_screening_page_exposes_resizable_chart_controls(logged_in_client) -> None:
    response = logged_in_client.get("/decision-support/early-screening")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "early_screening_chart_resize.js" in html
    assert html.index("early_screening_chart_resize.js") < html.index("early_screening.js")
    assert 'data-chart-grid' in html
    for resize_type in ("columns", "rows", "height"):
        assert f'data-chart-resizer="{resize_type}"' in html
    assert html.count('role="separator"') == 3
    assert 'data-chart-size-reset' in html
    assert 'data-chart-resize-status' in html


class _HumanReviewService:
    def __init__(self) -> None:
        self.last_feedback = None

    def snapshot(self, *, source="latest"):
        return {
            "schema": "chanlun-human-review-web",
            "source_kind": source,
            "review_queue": [],
            "highest_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
            "human_confirmation_required": True,
            "automated_order_authorized": False,
            "orders_created": 0,
            "fills_created": 0,
        }

    def append_feedback(self, **values):
        self.last_feedback = values
        return {
            "feedback": {"feedback_id": "sha256:" + "f" * 64},
            "ledger_content_sha256": "sha256:" + "e" * 64,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }

    def candidate_detail(self, *, candidate_id, source_sha256):
        return {
            "schema": "chanlun-human-review-candidate-detail-web",
            "candidate_id": candidate_id,
            "source_content_sha256": source_sha256,
            "sector_higher_timeframe_evidence": None,
            "market_symbol_higher_timeframe_evidence": None,
            "sector_ranking_evidence": None,
            "highest_status": "REVIEW_REQUIRED",
            "human_confirmation_required": True,
            "automated_order_authorized": False,
            "orders_created": 0,
            "fills_created": 0,
            "live_status": "LIVE_DISABLED",
        }

    def validate_chart_lock(self, **_values):
        return {}


class _UnavailableHumanReviewService(_HumanReviewService):
    def snapshot(self, *, source="latest"):
        del source
        raise HumanReviewScreenUnavailable("human_review_web_bundle_invalid")


def test_human_review_data_route_enforces_review_only_contract(
    app, logged_in_client
) -> None:
    app.extensions["decision_support_human_review"] = _HumanReviewService()
    monitored_sessions = []

    def health_snapshot(_kind, _market, forward_session=None):
        monitored_sessions.append(forward_session)
        session = (
            None if forward_session is None else forward_session.isoformat()
        )
        return {
            "status": "ready",
            "components": {
                "trading_screening": {
                    "market_data_as_of": "2026-07-30T15:00:00+08:00",
                },
                "qmt_runtime": {
                    "schema": "chanlun-qmt-runtime-readiness",
                    "contract_id": (
                        "chanlun-qmt-runtime/app-runtime-contract"
                    ),
                    "execution_owner": "APP_RUNTIME",
                    "ready": True,
                    "status": "ready",
                    "reason_code": "READY",
                },
                "forward_archive": {
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "COVERAGE_INCOMPLETE",
                    "session": session,
                    "screening_review_ready": False,
                    "sector_capture_ready": True,
                },
                "forward_delivery": {
                    "ready": False,
                    "status": "not_ready",
                    "reason_code": "EVALUATION_MISSING_AFTER_DEADLINE",
                    "session": session,
                    "capture_ready": False,
                    "evaluation_ready": False,
                },
            },
        }, 200

    app.extensions["health_snapshot"] = health_snapshot

    response = logged_in_client.get(
        "/decision-support/human-review/data?source=historical"
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    payload = response.get_json()["data"]
    assert payload["schema"] == "chanlun-human-review-web"
    assert payload["source_kind"] == "historical"
    assert payload["automated_order_authorized"] is False
    assert payload["realtime_notifications"]["schema"] == (
        "chanlun-realtime-review-inbox"
    )
    assert payload["realtime_notifications"]["events"] == []
    assert payload["realtime_notifications"]["automated_order_authorized"] is False
    operations = payload["forward_operations"]
    assert operations["session"] == "2026-07-30"
    assert operations["qmt_runtime"]["execution_owner"] == "APP_RUNTIME"
    assert operations["archive_gate"]["reason_code"] == "COVERAGE_INCOMPLETE"
    assert operations["delivery"]["reason_code"] == (
        "EVALUATION_MISSING_AFTER_DEADLINE"
    )
    assert operations["complete"] is False
    assert monitored_sessions[0] is None
    assert monitored_sessions[1].isoformat() == "2026-07-30"


def test_human_review_data_keeps_realtime_inbox_when_formal_bundle_is_invalid(
    app, logged_in_client
) -> None:
    app.extensions["decision_support_human_review"] = (
        _UnavailableHumanReviewService()
    )

    class _RealtimeInbox:
        @staticmethod
        def snapshot():
            return {
                "schema": "chanlun-realtime-review-inbox",
                "events": [
                    {
                        "schema": "chanlun-realtime-review-notification",
                        "notification_id": "sha256:" + "9" * 64,
                        "source": "CROSS_MARKET_ATTENTION_MONITOR",
                        "market": "us",
                        "code": "QCOM.US",
                        "side": "buy",
                        "signal_time": "2026-08-15T03:10:00+08:00",
                        "review_required": True,
                        "automated_action_authorized": False,
                        "real_order_transport_enabled": False,
                        "live_status": "LIVE_DISABLED",
                        "delivery_status": "delivered",
                        "chart_urls": {"1m": "/?market=us&code=QCOM.US"},
                    },
                    {
                        "schema": "chanlun-realtime-review-notification",
                        "notification_id": "sha256:" + "8" * 64,
                        "source": "CROSS_MARKET_ATTENTION_MONITOR",
                        "market": "us",
                        "code": "TSLA.US",
                        "side": "sell",
                        "signal_time": "2026-08-15T03:10:00+08:00",
                        "review_required": True,
                        "automated_action_authorized": False,
                        "real_order_transport_enabled": False,
                        "live_status": "LIVE_DISABLED",
                        "delivery_status": "delivered",
                        "chart_urls": {"1m": "/?market=us&code=TSLA.US"},
                    },
                ],
                "event_count": 2,
                "pending_review_count": 2,
                "delivery_counts": {"delivered": 2},
                "credentials_exposed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }

    app.extensions["realtime_review_inbox"] = _RealtimeInbox()

    class _Monitor:
        @staticmethod
        def admitted_identities():
            return (("us", "QCOM.US"),)

    app.extensions["holding_group_monitor"] = _Monitor()

    response = logged_in_client.get(
        "/decision-support/human-review/data?source=latest"
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["schema"] == "chanlun-human-review-web"
    assert payload["source_kind"] == "realtime"
    assert payload["formal_review_available"] is False
    assert payload["formal_review_unavailable_reason"] == (
        "human_review_web_bundle_invalid"
    )
    assert payload["review_queue"] == []
    assert payload["realtime_notifications"]["event_count"] == 1
    assert payload["realtime_notifications"]["events"][0]["code"] == "QCOM.US"
    assert payload["automated_order_authorized"] is False
    assert payload["orders_created"] == 0
    assert payload["fills_created"] == 0
    assert payload["live_status"] == "LIVE_DISABLED"


def test_feedback_route_binds_authenticated_reviewer_and_never_authorizes_orders(
    app, logged_in_client
) -> None:
    service = _HumanReviewService()
    app.extensions["decision_support_human_review"] = service

    response = logged_in_client.post(
        "/decision-support/human-review/feedback",
        json={
            "candidate_id": "sha256:" + "1" * 64,
            "source_content_sha256": "sha256:" + "2" * 64,
            "request_id": "test-human-review-request-1",
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "reviewed",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["automated_order_authorized"] is False
    assert service.last_feedback["reviewer"] == "trading-screening-user"


def test_candidate_detail_route_is_hash_bound_and_review_only(
    app, logged_in_client
) -> None:
    service = _HumanReviewService()
    app.extensions["decision_support_human_review"] = service
    candidate_id = "sha256:" + "1" * 64
    source_sha256 = "sha256:" + "2" * 64

    response = logged_in_client.get(
        "/decision-support/human-review/candidate-detail",
        query_string={
            "candidate_id": candidate_id,
            "source_content_sha256": source_sha256,
        },
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    detail = response.get_json()["data"]
    assert detail["candidate_id"] == candidate_id
    assert detail["source_content_sha256"] == source_sha256
    assert detail["automated_order_authorized"] is False
    assert detail["orders_created"] == 0
    assert detail["fills_created"] == 0
    assert detail["live_status"] == "LIVE_DISABLED"
