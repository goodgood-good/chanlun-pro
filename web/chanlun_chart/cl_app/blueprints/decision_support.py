"""Read-only routes for the sole active TradingEngine screening strategy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, make_response, render_template, request
from flask_login import current_user, login_required

from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)

from ..services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_snapshot,
)
from ..services.trading_screening import SCHEMA
from ..services.human_review_screening import (
    HumanReviewScreenUnavailable,
    HumanReviewScreeningService,
    WEB_SCHEMA as HUMAN_REVIEW_WEB_SCHEMA,
)


decision_support_bp = Blueprint("decision_support", __name__)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


class DecisionSupportError(RuntimeError):
    def __init__(self, code: str, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@decision_support_bp.errorhandler(DecisionSupportError)
def _decision_support_error(error: DecisionSupportError):
    return {
        "ok": False,
        "code": error.code,
        "errmsg": error.code,
    }, error.status_code


def _ok(data: object) -> dict[str, object]:
    return {"ok": True, "data": data}


def _no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _no_store_html(template: str, *, status: int = 200, **context):
    return _no_store(make_response(render_template(template, **context), status))


def _trading_screening_service():
    value = current_app.extensions.get("decision_support_trading_screening")
    if (
        not callable(getattr(value, "snapshot", None))
        or not callable(getattr(value, "ensure_refresh", None))
    ):
        raise DecisionSupportError("trading_screening_unavailable")
    return value


def _presentation_scope(
    output: dict[str, object],
    scope: str,
) -> dict[str, object]:
    """Bound the minute-polled response without changing the audit snapshot."""

    signals = []
    for value in output.get("signals", []):
        if not isinstance(value, Mapping):
            continue
        signal = dict(value)
        effective_stage = lifecycle_stage_from_signal(signal)
        if effective_stage is not None:
            signal["lifecycle_stage"] = effective_stage
        signals.append(signal)
    sector_triggered = [
        value
        for value in signals
        if isinstance(value.get("selection_sources"), (list, tuple))
        and "QMT_SECTOR_TRIGGER" in value["selection_sources"]
    ]
    manual_holdings = output.get("manual_holdings")
    manual_a_codes = {
        str(value.get("code"))
        for value in (
            manual_holdings.get("positions", [])
            if isinstance(manual_holdings, Mapping)
            else []
        )
        if isinstance(value, Mapping) and value.get("market") == "a"
    }
    manual_holding_signals = [
        value for value in signals if str(value.get("code")) in manual_a_codes
    ]
    selected = sector_triggered if scope == "sector-trigger" else signals
    output["signals"] = selected
    output["manual_holding_signals"] = (
        manual_holding_signals if scope == "sector-trigger" else []
    )
    counts_by_stage: dict[str, int] = {}
    counts_by_point_type: dict[str, int] = {}
    for value in selected:
        stage = str(value.get("lifecycle_stage") or "unknown")
        counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
        point_type = str(value.get("point_type") or "unknown")
        counts_by_point_type[point_type] = (
            counts_by_point_type.get(point_type, 0) + 1
        )
    output["counts_by_stage"] = counts_by_stage
    output["counts_by_point_type"] = counts_by_point_type
    output["presentation_scope"] = scope
    output["presentation_signal_count"] = len(selected)
    output["sector_trigger_signal_count"] = len(sector_triggered)
    output["total_qualified_signal_count"] = len(signals)
    output["presentation_scope_hash_coverage"] = (
        "EXCLUDED_OPERATIONAL_PROJECTION"
    )
    return output


def _trading_screening_snapshot(
    *,
    scope: str = "all-qualified",
) -> dict[str, object]:
    service = _trading_screening_service()
    try:
        service.ensure_refresh()
        presentation_provider = getattr(
            service,
            "presentation_snapshot",
            None,
        )
        payload = (
            presentation_provider()
            if callable(presentation_provider)
            else service.snapshot()
        )
    except Exception as exc:
        raise DecisionSupportError("trading_screening_unavailable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or not isinstance(payload.get("sectors"), list)
        or not isinstance(payload.get("signals"), list)
        or not isinstance(payload.get("data_quality"), Mapping)
    ):
        raise DecisionSupportError("trading_screening_unavailable")
    output = dict(payload)
    if current_app.config.get("TRADING_SCREENING_BACKGROUND_ENABLED", True):
        health_provider = getattr(service, "health_snapshot", None)
        try:
            if not callable(health_provider):
                raise RuntimeError("trading screening health unavailable")
            runtime_health = dict(health_provider())
            runtime_health["required"] = True
        except Exception as exc:
            current_app.logger.exception(
                "trading screening runtime health snapshot failed"
            )
            runtime_health = {
                "required": True,
                "ready": False,
                "status": "not_ready",
                "last_error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "reasons": ["screening_health_failed"],
            }
    else:
        runtime_health = {
            "required": False,
            "ready": True,
            "status": "disabled",
            "reasons": [],
        }
    runtime_health["snapshot_hash_coverage"] = (
        "EXCLUDED_OPERATIONAL_METADATA"
    )
    output["runtime_health"] = runtime_health
    output["manual_holdings"] = _manual_holdings_snapshot()
    return _presentation_scope(output, scope)


def _manual_holdings_snapshot() -> dict[str, object]:
    """Return local user-declared holdings without touching a broker account."""

    unavailable = {
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
    provider = current_app.config.get(
        "TRADING_SCREENING_MANUAL_HOLDINGS_SNAPSHOT_PROVIDER"
    )
    if not callable(provider):
        return unavailable
    try:
        value = provider()
    except Exception:
        current_app.logger.exception("local manual holdings snapshot failed")
        return unavailable
    positions = value.get("positions") if isinstance(value, Mapping) else None
    allowed_scopes = {
        "A_SHARE_STRICT_DECISION_CORE",
        "NON_A_AUXILIARY_STRUCTURE_RADAR",
    }
    valid_positions = (
        isinstance(positions, list)
        and all(
            isinstance(row, Mapping)
            and isinstance(row.get("market"), str)
            and bool(row.get("market"))
            and isinstance(row.get("code"), str)
            and bool(row.get("code"))
            and isinstance(row.get("name"), str)
            and bool(row.get("name"))
            and row.get("monitoring_scope") in allowed_scopes
            and (
                (row.get("market") == "a")
                == (
                    row.get("monitoring_scope")
                    == "A_SHARE_STRICT_DECISION_CORE"
                )
            )
            for row in positions
        )
    )
    priority_count = (
        sum(
            row.get("monitoring_scope") == "A_SHARE_STRICT_DECISION_CORE"
            for row in positions
        )
        if valid_positions
        else -1
    )
    cross_market_count = (
        sum(
            row.get("monitoring_scope")
            == "NON_A_AUXILIARY_STRUCTURE_RADAR"
            for row in positions
        )
        if valid_positions
        else -1
    )
    covered_count = (
        priority_count + cross_market_count if valid_positions else -1
    )
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != unavailable["schema"]
        or value.get("source") != unavailable["source"]
        or value.get("group_scope") != unavailable["group_scope"]
        or value.get("real_account_accessed") is not False
        or value.get("real_order_transport_enabled") is not False
        or value.get("automated_order_authorized") is not False
        or value.get("live_status") != "LIVE_DISABLED"
        or value.get("available") is not True
        or value.get("status") != "ready"
        or value.get("quantity_available") is not False
        or value.get("cost_basis_available") is not False
        or value.get("sellable_quantity_available") is not False
        or not valid_positions
        or value.get("declared_count") != len(positions)
        or value.get("priority_monitor_count") != priority_count
        or value.get("cross_market_monitor_count") != cross_market_count
        or value.get("covered_monitor_count") != covered_count
        or value.get("unsupported_market_count") != 0
    ):
        current_app.logger.error("local manual holdings snapshot contract invalid")
        return unavailable
    output = dict(value)
    monitor = current_app.extensions.get("holding_group_monitor")
    try:
        monitor_health = (
            dict(monitor.health_snapshot())
            if monitor is not None
            and callable(getattr(monitor, "health_snapshot", None))
            else {
                "ready": False,
                "status": "unavailable",
                "reason_code": "HOLDING_MONITOR_UNAVAILABLE",
                "positions": [],
            }
        )
    except Exception:
        current_app.logger.exception("holding group monitor health failed")
        monitor_health = {
            "ready": False,
            "status": "unavailable",
            "reason_code": "HOLDING_MONITOR_HEALTH_UNAVAILABLE",
            "positions": [],
        }
    statuses = {
        (str(row.get("market")), str(row.get("code"))): row
        for row in monitor_health.get("positions", [])
        if isinstance(row, Mapping)
    }
    screening = current_app.extensions.get("decision_support_trading_screening")
    try:
        screening_health = (
            dict(screening.health_snapshot())
            if screening is not None
            and callable(getattr(screening, "health_snapshot", None))
            else {}
        )
    except Exception:
        current_app.logger.exception("A-share strict holding monitor health failed")
        screening_health = {}
    enriched = []
    for position in output["positions"]:
        if position["monitoring_scope"] == "A_SHARE_STRICT_DECISION_CORE":
            if screening_health.get("priority_monitoring_enabled") is not True:
                realtime_status = "error"
                realtime_reason = "A_SHARE_STRICT_MONITOR_DISABLED"
            elif screening_health.get("priority_monitor_ready") is not True:
                realtime_status = "error"
                realtime_reason = "A_SHARE_STRICT_MONITOR_DEGRADED"
            elif screening_health.get("priority_monitor_session_open") is True:
                realtime_status = "monitoring"
                realtime_reason = "A_SHARE_STRICT_DECISION_CORE_ACTIVE"
            else:
                realtime_status = "market_closed"
                realtime_reason = "A_SHARE_MARKET_CLOSED"
        else:
            status = statuses.get((position["market"], position["code"]), {})
            realtime_status = status.get("status", "awaiting_first_run")
            realtime_reason = status.get(
                "reason_code", "HOLDING_MONITOR_AWAITING_FIRST_RUN"
            )
        enriched.append(
            {
                **position,
                "realtime_status": realtime_status,
                "realtime_reason_code": realtime_reason,
            }
        )
    output["positions"] = enriched
    output["realtime_monitor"] = monitor_health
    return output


def _human_review_service() -> HumanReviewScreeningService:
    value = current_app.extensions.get("decision_support_human_review")
    if not isinstance(value, HumanReviewScreeningService) and not (
        callable(getattr(value, "snapshot", None))
        and callable(getattr(value, "append_feedback", None))
        and callable(getattr(value, "validate_chart_lock", None))
    ):
        raise DecisionSupportError("human_review_screening_unavailable")
    return value


def _human_review_snapshot(source: str) -> dict[str, object]:
    try:
        service = _human_review_service()
        payload = (
            service.snapshot(source=source, include_evidence=False)
            if isinstance(service, HumanReviewScreeningService)
            else service.snapshot(source=source)
        )
    except HumanReviewScreenUnavailable as exc:
        raise DecisionSupportError(exc.code) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != HUMAN_REVIEW_WEB_SCHEMA
        or payload.get("highest_status") != "REVIEW_REQUIRED"
        or payload.get("live_status") != "LIVE_DISABLED"
        or payload.get("human_confirmation_required") is not True
        or payload.get("automated_order_authorized") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or not isinstance(payload.get("review_queue"), list)
    ):
        raise DecisionSupportError("human_review_screening_unavailable")
    output = dict(payload)
    output["forward_operations"] = _human_review_forward_operations()
    return output


def _human_review_forward_operations() -> dict[str, object]:
    """Expose the same forward verdicts as ``/readyz`` on the review page."""

    health_provider = current_app.extensions.get("health_snapshot")
    unavailable = {
        "schema": "chanlun-human-review-forward-operations",
        "session": None,
        "screening_market_data_as_of": None,
        "scheduler": {
            "ready": False,
            "status": "unresolved",
            "reason_code": "SCHEDULED_TASK_OBSERVATION_UNAVAILABLE",
        },
        "qmt_runtime": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "QMT_RUNTIME_OBSERVATION_UNAVAILABLE",
        },
        "archive_gate": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "FORWARD_ARCHIVE_READINESS_UNAVAILABLE",
        },
        "delivery": {
            "ready": False,
            "status": "not_ready",
            "reason_code": "FORWARD_DELIVERY_READINESS_UNAVAILABLE",
        },
        "complete": False,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    if not callable(health_provider):
        return unavailable
    try:
        health, _status_code = health_provider("readyz", "a", None)
        if not isinstance(health, Mapping):
            return unavailable
        components = health.get("components")
        if not isinstance(components, Mapping):
            return unavailable
        screening = components.get("trading_screening")
        market_data_as_of = (
            screening.get("market_data_as_of")
            if isinstance(screening, Mapping)
            else None
        )
        monitor_session = None
        if isinstance(market_data_as_of, str) and market_data_as_of:
            observed = datetime.fromisoformat(market_data_as_of)
            if observed.tzinfo is None:
                raise ValueError("screening market_data_as_of must be timezone-aware")
            monitor_session = observed.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date()
            health, _status_code = health_provider(
                "readyz",
                "a",
                monitor_session,
            )
            components = health.get("components")
            if not isinstance(components, Mapping):
                return unavailable
        scheduler = components.get("forward_scheduler")
        qmt_runtime = components.get("qmt_runtime")
        archive = components.get("forward_archive")
        delivery = components.get("forward_delivery")
        if not isinstance(archive, Mapping) or not isinstance(delivery, Mapping):
            return unavailable
        scheduler_value = (
            dict(scheduler)
            if isinstance(scheduler, Mapping)
            else dict(unavailable["scheduler"])
        )
        qmt_runtime_value = (
            dict(qmt_runtime)
            if isinstance(qmt_runtime, Mapping)
            else dict(unavailable["qmt_runtime"])
        )
        archive_value = dict(archive)
        delivery_value = dict(delivery)
        session_label = (
            monitor_session.isoformat()
            if monitor_session is not None
            else delivery_value.get("session") or archive_value.get("session")
        )
        return {
            "schema": "chanlun-human-review-forward-operations",
            "session": session_label,
            "screening_market_data_as_of": market_data_as_of,
            "scheduler": scheduler_value,
            "qmt_runtime": qmt_runtime_value,
            "archive_gate": archive_value,
            "delivery": delivery_value,
            "complete": bool(delivery_value.get("ready")),
            "highest_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }
    except Exception:
        current_app.logger.exception("human review forward operations failed")
        return unavailable


def _research_audit_root() -> str | Path:
    return current_app.config.get("RESEARCH_AUDIT_ROOT", _REPOSITORY_ROOT)


def _research_audit_snapshot() -> dict[str, object]:
    try:
        return build_research_audit_snapshot(_research_audit_root())
    except ResearchAuditUnavailable as exc:
        raise DecisionSupportError(exc.code) from exc


@decision_support_bp.get("/decision-support/early-screening")
@login_required
def early_screening():
    _trading_screening_service().ensure_refresh()
    return _no_store_html("early_screening.html")


@decision_support_bp.get("/decision-support/early-signals")
@login_required
def early_signals():
    # No-query callers keep the historical complete presentation contract.
    # The product page explicitly requests the bounded sector-trigger scope.
    scope = str(request.args.get("scope") or "all-qualified").strip().lower()
    if scope not in {"sector-trigger", "all-qualified"}:
        scope = "all-qualified"
    return _no_store(
        make_response(_ok(_trading_screening_snapshot(scope=scope)))
    )


@decision_support_bp.get("/decision-support/human-review/data")
@login_required
def human_review_data():
    source = str(request.args.get("source") or "latest").strip().lower()
    return _no_store(make_response(_ok(_human_review_snapshot(source))))


@decision_support_bp.get("/decision-support/human-review/candidate-detail")
@login_required
def human_review_candidate_detail():
    candidate_id = str(request.args.get("candidate_id") or "").strip()
    source_sha256 = str(
        request.args.get("source_content_sha256") or ""
    ).strip()
    provider = getattr(_human_review_service(), "candidate_detail", None)
    if not callable(provider):
        raise DecisionSupportError("human_review_candidate_detail_unavailable")
    try:
        payload = provider(
            candidate_id=candidate_id,
            source_sha256=source_sha256,
        )
    except HumanReviewScreenUnavailable as exc:
        status = 404 if exc.code == "human_review_candidate_not_found" else 400
        raise DecisionSupportError(exc.code, status) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema")
        != "chanlun-human-review-candidate-detail-web"
        or payload.get("candidate_id") != candidate_id
        or payload.get("source_content_sha256") != source_sha256
        or payload.get("highest_status") != "REVIEW_REQUIRED"
        or payload.get("human_confirmation_required") is not True
        or payload.get("automated_order_authorized") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise DecisionSupportError("human_review_candidate_detail_unavailable")
    return _no_store(make_response(_ok(payload)))


@decision_support_bp.post("/decision-support/human-review/feedback")
@login_required
def human_review_feedback():
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        raise DecisionSupportError("human_review_feedback_invalid", 400)
    candidate_id = str(payload.get("candidate_id") or "")
    source_sha256 = str(payload.get("source_content_sha256") or "")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise DecisionSupportError("human_review_request_id_required", 400)
    reviewer = str(current_user.get_id() or "authenticated-reviewer")
    try:
        result = _human_review_service().append_feedback(
            candidate_id=candidate_id,
            source_sha256=source_sha256,
            reviewer=reviewer,
            values=payload,
            reviewed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            request_id=request_id,
        )
    except HumanReviewScreenUnavailable as exc:
        status = 409 if exc.code == "human_review_source_not_found" else 400
        raise DecisionSupportError(exc.code, status) from exc
    return _no_store(make_response(_ok(result)))


@decision_support_bp.get("/decision-support/research-audit")
@login_required
def research_audit():
    try:
        audit = build_research_audit_snapshot(_research_audit_root())
    except ResearchAuditUnavailable as exc:
        current_app.logger.warning("research audit unavailable: %s", exc.code)
        template_name = (
            "research_audit_current.html"
            if exc.code.startswith("current_research_")
            else "research_audit.html"
        )
        return _no_store_html(
            template_name,
            status=503,
            audit=None,
            audit_error_code=exc.code,
            audit_error_details=exc.details,
        )
    template_name = (
        "research_audit_current.html"
        if audit.get("source_kind") == "current_research_variant"
        else "research_audit.html"
    )
    return _no_store_html(
        template_name,
        audit=audit,
        audit_error_code=None,
        audit_error_details=None,
    )


@decision_support_bp.get("/decision-support/research-audit/data")
@login_required
def research_audit_data():
    return _no_store(make_response(_ok(_research_audit_snapshot())))


__all__ = ("DecisionSupportError", "decision_support_bp")
