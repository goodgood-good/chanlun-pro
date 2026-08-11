from __future__ import annotations

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from cl_app.blueprints.decision_support import _presentation_scope, decision_support_bp


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
            "snapshot_hash_coverage": "EXCLUDED_OPERATIONAL_METADATA",
    }
    assert payload["data"]["manual_holdings"] == {
        "schema": "chanlun-local-manual-holdings",
        "source": "LOCAL_GLOBAL_WATCHLIST_GROUP",
        "group_name": "我的持仓",
        "group_scope": "GLOBAL_ACROSS_MARKETS",
        "available": False,
        "status": "unavailable",
        "positions": [],
        "declared_count": 0,
            "priority_monitor_count": 0,
            "cross_market_monitor_count": 0,
            "covered_monitor_count": 0,
            "unsupported_market_count": 0,
        "quantity_available": False,
        "cost_basis_available": False,
        "sellable_quantity_available": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    service = app.extensions["decision_support_trading_screening"]
    assert service.refresh_requests == 1


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
        row["code"] for row in sector_payload["manual_holding_signals"]
    ] == ["SZ.000001"]
    assert all_payload["presentation_scope"] == "all-qualified"
    assert len(all_payload["signals"]) == 2
    assert all_payload["manual_holding_signals"] == []
    assert all_payload["counts_by_point_type"] == {"3buy": 1, "2buy": 1}


def test_server_projection_preserves_declared_holding_signal_stage() -> None:
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
        "manual_holdings": {
            "positions": [{"market": "a", "code": "SZ.301004"}],
        },
    }

    projected = _presentation_scope(output, "sector-trigger")

    assert projected["signals"][0]["lifecycle_stage"] == "approaching"
    assert projected["manual_holding_signals"][0]["lifecycle_stage"] == "approaching"
    assert projected["counts_by_stage"] == {"approaching": 1}


def test_early_signals_exposes_cross_market_manual_holdings_without_account_access(
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
                "decision_mode": "APPROXIMATE_STRUCTURE_OBSERVATION",
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

    payload = logged_in_client.get("/decision-support/early-signals").get_json()
    holdings = payload["data"]["manual_holdings"]

    assert holdings["available"] is True
    assert holdings["group_scope"] == "GLOBAL_ACROSS_MARKETS"
    assert [row["market"] for row in holdings["positions"]] == ["a", "hk"]
    assert holdings["priority_monitor_count"] == 1
    assert holdings["cross_market_monitor_count"] == 1
    assert holdings["covered_monitor_count"] == 2
    assert holdings["unsupported_market_count"] == 0
    assert holdings["positions"][0]["realtime_status"] == "monitoring"
    assert holdings["positions"][0]["realtime_reason_code"] == (
        "A_SHARE_STRICT_DECISION_CORE_ACTIVE"
    )
    assert holdings["positions"][1]["realtime_status"] == "awaiting_first_run"
    assert holdings["real_account_accessed"] is False
    assert holdings["automated_order_authorized"] is False


def test_early_signals_fails_closed_on_inconsistent_manual_holdings_contract(
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
    holdings = payload["data"]["manual_holdings"]

    assert holdings["available"] is False
    assert holdings["positions"] == []
    assert holdings["quantity_available"] is False
    assert holdings["real_account_accessed"] is False


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
    assert "成员日线" in html
    assert "板块按点时成分的中长期相对结构排序" in html
    assert "不按当日涨跌追强" in html
    assert "日线、30m、5m、1m 均只使用决策时已经完成的K线" in html
    assert "成分、复权或高周期证据不足时关闭候选" in html
    assert "未来除权改写既有排序" not in html
    assert 'data-selection-scope="sector-trigger"' in html
    assert 'data-selection-scope="all-qualified"' in html
    assert "板块已触发 · 全部 · 选股买点" in html
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
    assert "30m 大级别筛选" in html
    assert "5m 可操作级别筛选" in html
    assert "1m 精确操作确认" in html
    assert "STRUCTURE CLUE QUEUE · HUMAN REVIEW" in html
    assert "买卖点线索队列" in html
    assert "线索只供人工识别，没有一条天然可执行" in html
    assert "ACTIONABLE QUEUE" not in html
    assert "实时模式图表会随市场继续更新" in html
    assert "不可作为历史因果复核" in html
    assert "请切换“人工复核选股”" in html
    assert "三买只取第一中枢" in html
    assert "历史研究/审计成果" in html
    assert "只读研究 · 无订单能力" in html
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
    assert 'id="hr-feedback-form"' in html
    assert 'id="hr-virtual-intent-count"' in html
    assert 'id="hr-virtual-pending-count"' in html
    assert 'id="hr-virtual-fill-count"' in html
    assert 'id="hr-virtual-position-count"' in html
    assert 'id="hr-portfolio-decision-audit-status"' in html
    assert 'id="hr-portfolio-fill-decision-audit-status"' in html
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
    assert "REVIEW_REQUIRED" in html
    assert "LIVE_DISABLED" in html
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
