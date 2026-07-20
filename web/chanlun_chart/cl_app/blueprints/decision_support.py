"""Read-only routes for the sole active TradingEngine screening strategy."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from flask import Blueprint, current_app, make_response, render_template
from flask_login import login_required

from ..services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_snapshot,
)
from ..services.trading_screening import SCHEMA_VERSION


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


def _trading_screening_snapshot() -> dict[str, object]:
    service = _trading_screening_service()
    try:
        service.ensure_refresh()
        payload = service.snapshot()
    except Exception as exc:
        raise DecisionSupportError("trading_screening_unavailable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or not isinstance(payload.get("sectors"), list)
        or not isinstance(payload.get("signals"), list)
        or not isinstance(payload.get("data_quality"), Mapping)
    ):
        raise DecisionSupportError("trading_screening_unavailable")
    return payload


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
    return _no_store(make_response(_ok(_trading_screening_snapshot())))


@decision_support_bp.get("/decision-support/research-audit")
@login_required
def research_audit():
    try:
        audit = build_research_audit_snapshot(_research_audit_root())
    except ResearchAuditUnavailable as exc:
        current_app.logger.warning("research audit unavailable: %s", exc.code)
        return _no_store_html(
            "research_audit.html",
            status=503,
            audit=None,
            audit_error_code=exc.code,
        )
    return _no_store_html(
        "research_audit.html",
        audit=audit,
        audit_error_code=None,
    )


@decision_support_bp.get("/decision-support/research-audit/data")
@login_required
def research_audit_data():
    return _no_store(make_response(_ok(_research_audit_snapshot())))


__all__ = ("DecisionSupportError", "decision_support_bp")
