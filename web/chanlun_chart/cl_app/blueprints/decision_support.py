from __future__ import annotations

import re

from flask import Blueprint, Response, current_app, make_response, request
from flask_login import current_user, login_required

from chanlun.decision_support.manual_check_workflow import (
    manual_check_snapshot_from_dict,
)
from chanlun.decision_support.paper_read_model import PaperResearchReadModel

from ..services.decision_support import (
    DecisionSupportError,
    DecisionSupportFacade,
)


decision_support_bp = Blueprint("decision_support", __name__)
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_USER_DECISIONS = frozenset(
    {"accepted", "ignored", "executed_externally"}
)


def _facade() -> DecisionSupportFacade:
    value = current_app.extensions.get("decision_support_facade")
    if not isinstance(value, DecisionSupportFacade):
        return DecisionSupportFacade()
    return value


def _ok(data: object):
    return {"ok": True, "data": data}


def _json_object(error_code: str) -> dict[str, object]:
    if not request.is_json:
        raise DecisionSupportError(error_code, 400)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise DecisionSupportError(error_code, 400)
    return value


def _user_id() -> str:
    value = current_user.get_id()
    if not isinstance(value, str) or not value or len(value) > 191:
        raise DecisionSupportError("authentication_required", 401)
    return value


def _manual_check_workflow():
    workflow = current_app.extensions.get(
        "decision_support_manual_check_workflow"
    )
    if (
        workflow is None
        or not callable(getattr(workflow, "submit", None))
        or not callable(
            getattr(getattr(workflow, "store", None), "get_for_event", None)
        )
    ):
        raise DecisionSupportError("manual_checks_unavailable", 503)
    return workflow


def _paper_read_model() -> PaperResearchReadModel:
    value = current_app.extensions.get(
        "decision_support_paper_read_model"
    )
    if not isinstance(value, PaperResearchReadModel):
        raise DecisionSupportError("paper_research_unavailable", 503)
    return value


def _paper_snapshot(method_name: str) -> dict[str, object]:
    model = _paper_read_model()
    try:
        method = getattr(model, method_name)
        payload = method()
    except Exception as exc:
        raise DecisionSupportError(
            "paper_research_unavailable",
            503,
        ) from exc
    if not isinstance(payload, dict):
        raise DecisionSupportError("paper_research_unavailable", 503)
    return payload


def _paper_ok(method_name: str) -> Response:
    response = make_response(_ok(_paper_snapshot(method_name)))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@decision_support_bp.errorhandler(DecisionSupportError)
def _decision_support_error(error: DecisionSupportError):
    return {
        "ok": False,
        "code": error.code,
        "errmsg": error.message,
    }, error.status_code


@decision_support_bp.get("/decision-support/paper/status")
@login_required
def paper_status():
    return _paper_ok("status")


@decision_support_bp.get("/decision-support/paper/account")
@login_required
def paper_account():
    return _paper_ok("account")


@decision_support_bp.get("/decision-support/paper/positions")
@login_required
def paper_positions():
    return _paper_ok("positions")


@decision_support_bp.get("/decision-support/paper/intents")
@login_required
def paper_intents():
    return _paper_ok("intents")


@decision_support_bp.get("/decision-support/paper/fills")
@login_required
def paper_fills():
    return _paper_ok("fills")


@decision_support_bp.get("/decision-support/paper/exits")
@login_required
def paper_exits():
    return _paper_ok("exits")


@decision_support_bp.get("/decision-support/candidates")
@login_required
def candidates():
    raw_limit = request.args.get("limit", "25")
    if not raw_limit.isdecimal():
        raise DecisionSupportError("invalid_limit", 400)
    limit = int(raw_limit)
    if not 1 <= limit <= 100:
        raise DecisionSupportError("invalid_limit", 400)
    cursor = request.args.get("cursor")
    if cursor is not None and _CURSOR_RE.fullmatch(cursor) is None:
        raise DecisionSupportError("invalid_cursor", 400)
    return _ok(_facade().candidates(cursor, limit))


@decision_support_bp.get("/decision-support/events/<event_id>")
@login_required
def event_detail(event_id: str):
    return _ok(_facade().event(event_id))


@decision_support_bp.get("/decision-support/events/<event_id>/evidence")
@login_required
def event_evidence(event_id: str):
    return _ok(_facade().evidence(event_id))


@decision_support_bp.get(
    "/decision-support/events/<event_id>/manual-checks"
)
@login_required
def event_manual_checks(event_id: str):
    workflow = _manual_check_workflow()
    try:
        record = workflow.store.get_for_event(event_id)
    except (
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise DecisionSupportError("manual_checks_unavailable", 503) from exc
    if record is None:
        raise DecisionSupportError("manual_checks_not_found", 404)
    try:
        payload = record.to_dict()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DecisionSupportError("manual_checks_unavailable", 503) from exc
    if not isinstance(payload, dict):
        raise DecisionSupportError("manual_checks_unavailable", 503)
    return _ok(payload)


@decision_support_bp.post(
    "/decision-support/events/<event_id>/manual-checks"
)
@login_required
def submit_event_manual_checks(event_id: str):
    workflow = _manual_check_workflow()
    payload = _json_object("invalid_manual_checks")
    if set(payload) != {"manual_checks"}:
        raise DecisionSupportError("invalid_manual_checks", 400)
    raw_checks = payload["manual_checks"]
    if (
        not isinstance(raw_checks, list)
        or not raw_checks
        or len(raw_checks) > 64
    ):
        raise DecisionSupportError("invalid_manual_checks", 400)
    try:
        snapshots = tuple(
            manual_check_snapshot_from_dict(item) for item in raw_checks
        )
    except (TypeError, ValueError) as exc:
        raise DecisionSupportError("invalid_manual_checks", 400) from exc
    user_id = _user_id()
    if any(
        snapshot.event_id != event_id or snapshot.operator_id != user_id
        for snapshot in snapshots
    ):
        raise DecisionSupportError("invalid_manual_checks", 400)
    try:
        result = workflow.submit(event_id, snapshots)
    except KeyError as exc:
        raise DecisionSupportError("manual_checks_not_found", 404) from exc
    except ValueError as exc:
        raise DecisionSupportError("manual_checks_conflict", 409) from exc
    except (OSError, RuntimeError, TypeError) as exc:
        raise DecisionSupportError("manual_checks_unavailable", 503) from exc
    try:
        result_payload = result.to_dict()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DecisionSupportError("manual_checks_unavailable", 503) from exc
    if not isinstance(result_payload, dict):
        raise DecisionSupportError("manual_checks_unavailable", 503)
    return _ok(result_payload)


@decision_support_bp.get("/decision-support/risk-status")
@login_required
def risk_status():
    return _ok(_facade().risk_status())


@decision_support_bp.get("/decision-support/corpus-status")
@login_required
def corpus_status():
    return _ok(_facade().corpus_status())


@decision_support_bp.get("/decision-support/evidence/images/<image_id>")
@login_required
def evidence_image(image_id: str):
    payload, media_type = _facade().image(image_id)
    response = Response(payload, mimetype=media_type)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@decision_support_bp.post("/decision-support/events/<event_id>/review")
@login_required
def request_event_review(event_id: str):
    payload = _json_object("invalid_review_request")
    if set(payload) - {"force"}:
        raise DecisionSupportError("invalid_review_request", 400)
    force = payload.get("force", False)
    if type(force) is not bool:
        raise DecisionSupportError("invalid_review_request", 400)
    return _ok(_facade().request_review(event_id, _user_id(), force))


@decision_support_bp.post(
    "/decision-support/events/<event_id>/user-decision"
)
@login_required
def record_user_decision(event_id: str):
    payload = _json_object("invalid_user_decision")
    allowed_fields = {
        "action",
        "event_data_fingerprint",
        "idempotency_key",
        "note",
    }
    if set(payload) - allowed_fields:
        raise DecisionSupportError("invalid_user_decision", 400)
    action = payload.get("action")
    fingerprint = payload.get("event_data_fingerprint")
    idempotency_key = payload.get("idempotency_key")
    note = payload.get("note")
    if (
        action not in _USER_DECISIONS
        or not isinstance(fingerprint, str)
        or _FINGERPRINT_RE.fullmatch(fingerprint) is None
        or not isinstance(idempotency_key, str)
        or _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None
        or (note is not None and not isinstance(note, str))
        or (isinstance(note, str) and len(note) > 1000)
    ):
        raise DecisionSupportError("invalid_user_decision", 400)
    normalized = {
        "action": action,
        "event_data_fingerprint": fingerprint,
        "idempotency_key": idempotency_key,
    }
    if note is not None:
        normalized["note"] = note
    return _ok(
        _facade().record_user_decision(
            event_id,
            _user_id(),
            normalized,
        )
    )
