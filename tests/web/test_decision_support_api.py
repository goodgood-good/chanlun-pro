from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user

from cl_app.blueprints.decision_support import decision_support_bp
from cl_app.services.decision_support import (
    DecisionSupportError,
    DecisionSupportFacade,
    TrustedImageCatalog,
)
from chanlun.decision_support.certified_runtime import CertifiedCorpusRuntime


class _User(UserMixin):
    id = "test-user"


@pytest.fixture
def facade() -> DecisionSupportFacade:
    def candidates(cursor, limit):
        assert limit <= 100
        return {
            "trend": [
                {
                    "event_id": "trend-1",
                    "strategy_track": "trend_continuation",
                    "code": "SH.600001",
                    "name": "趋势候选",
                }
            ],
            "reversal": [
                {
                    "event_id": "reversal-1",
                    "strategy_track": "bottom_reversal",
                    "code": "SZ.000001",
                    "name": "反转候选",
                }
            ],
            "next_cursor": "next_1" if cursor is None else None,
            "stale": False,
        }

    def event(event_id):
        if event_id != "trend-1":
            raise DecisionSupportError("event_not_found", 404)
        return {
            "event_id": event_id,
            "state": "review_pending",
            "observed_at": "2026-07-13T10:35:00+08:00",
            "api_key": "must-not-leak",
            "internal_path": "D:/secret/event.json",
        }

    def evidence(event_id):
        if event_id != "trend-1":
            raise DecisionSupportError("event_not_found", 404)
        return {
            "event_id": event_id,
            "reviewable": False,
            "blockers": ["missing_original_evidence"],
            "supporting": [],
            "raw_response": "must-not-leak",
        }

    return DecisionSupportFacade(
        candidate_provider=candidates,
        event_provider=event,
        evidence_provider=evidence,
        risk_provider=lambda: {
            "available": True,
            "daily_loss_locked": False,
            "drawdown_locked": False,
        },
        corpus_provider=lambda: {
            "integrity": "incomplete",
            "original_evidence": "missing_original",
            "trusted_units": 66,
            "trusted_images": 31,
        },
    )


@pytest.fixture
def app(facade) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="decision-support-test-secret",
        WTF_CSRF_ENABLED=False,
    )
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _User() if user_id == _User.id else None

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify(
            ok=False,
            code="authentication_required",
            errmsg="Authentication required.",
        ), 401

    @app.get("/_test/login")
    def login():
        login_user(_User())
        return {"ok": True}

    app.extensions["decision_support_facade"] = facade
    app.register_blueprint(decision_support_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in_client(client):
    assert client.get("/_test/login").status_code == 200
    return client


def test_candidates_requires_login(client) -> None:
    response = client.get("/decision-support/candidates")

    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["code"] == "authentication_required"


def test_create_app_classifies_decision_support_as_json_api() -> None:
    from cl_app import create_app

    runtime_app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        }
    )

    response = runtime_app.test_client().get("/decision-support/candidates")

    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["code"] == "authentication_required"


def test_create_app_installs_persistent_decision_support_facade() -> None:
    from cl_app import create_app

    runtime_app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        }
    )

    assert isinstance(
        runtime_app.extensions.get("decision_support_facade"),
        DecisionSupportFacade,
    )
    assert isinstance(
        runtime_app.extensions.get("certified_corpus_runtime"),
        CertifiedCorpusRuntime,
    )


def test_candidates_returns_separate_tracks_and_bounded_page(logged_in_client) -> None:
    response = logged_in_client.get("/decision-support/candidates?limit=25")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert {item["strategy_track"] for item in data["trend"]} == {
        "trend_continuation"
    }
    assert {item["strategy_track"] for item in data["reversal"]} == {
        "bottom_reversal"
    }
    assert data["next_cursor"] == "next_1"


@pytest.mark.parametrize(
    ("query", "code"),
    (
        ("limit=0", "invalid_limit"),
        ("limit=101", "invalid_limit"),
        ("limit=true", "invalid_limit"),
        ("cursor=../../secret", "invalid_cursor"),
        ("cursor=%20", "invalid_cursor"),
    ),
)
def test_candidates_rejects_invalid_pagination(
    logged_in_client,
    query,
    code,
) -> None:
    response = logged_in_client.get(f"/decision-support/candidates?{query}")

    assert response.status_code == 400
    assert response.get_json()["code"] == code


def test_event_and_evidence_are_redacted(logged_in_client) -> None:
    event = logged_in_client.get("/decision-support/events/trend-1")
    evidence = logged_in_client.get(
        "/decision-support/events/trend-1/evidence"
    )

    assert event.status_code == 200
    assert evidence.status_code == 200
    event_text = json.dumps(event.get_json(), ensure_ascii=False)
    evidence_text = json.dumps(evidence.get_json(), ensure_ascii=False)
    for forbidden in ("must-not-leak", "D:/secret", "api_key", "raw_response"):
        assert forbidden not in event_text
        assert forbidden not in evidence_text


@pytest.mark.parametrize(
    "path",
    (
        "/decision-support/events/unknown",
        "/decision-support/events/unknown/evidence",
    ),
)
def test_unknown_event_has_stable_json_error(logged_in_client, path) -> None:
    response = logged_in_client.get(path)

    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["code"] == "event_not_found"


def test_risk_and_corpus_status_are_explicit(logged_in_client) -> None:
    risk = logged_in_client.get("/decision-support/risk-status")
    corpus = logged_in_client.get("/decision-support/corpus-status")

    assert risk.status_code == 200
    assert risk.get_json()["data"]["available"] is True
    assert risk.get_json()["data"]["paper_gate_pending"] is True
    assert risk.get_json()["data"]["promotion_state"] == "research"
    assert corpus.status_code == 200
    corpus_data = corpus.get_json()["data"]
    assert corpus_data["integrity"] == "incomplete"
    assert corpus_data["original_evidence"] == "missing_original"
    assert corpus_data["review_eligible"] is False


def test_unconfigured_risk_and_corpus_fail_closed(app, logged_in_client) -> None:
    app.extensions["decision_support_facade"] = DecisionSupportFacade()

    risk = logged_in_client.get("/decision-support/risk-status")
    corpus = logged_in_client.get("/decision-support/corpus-status")

    assert risk.status_code == 503
    assert risk.get_json()["code"] == "risk_unavailable"
    assert corpus.status_code == 503
    assert corpus.get_json()["code"] == "corpus_untrusted"


def _image_manifest(root: Path, image: Path, digest: str) -> Path:
    manifest = root / "trusted_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus_status": {
                    "integrity": "complete",
                    "original_evidence": "available",
                },
                "units": [],
                "images": [
                    {
                        "image_id": "img_test_1",
                        "source_tier": "secondary_annotation",
                        "source_path": image.name,
                        "sha256": digest,
                        "media_type": "image/jpeg",
                        "width": 1,
                        "height": 1,
                        "alt_text": "test",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def test_trusted_image_route_rechecks_bytes_and_mime(
    app,
    logged_in_client,
    tmp_path,
) -> None:
    image = tmp_path / "trusted.jpg"
    payload = b"trusted-image-bytes"
    image.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = _image_manifest(tmp_path, image, digest)
    catalog = TrustedImageCatalog(
        manifest,
        {"secondary_annotation": tmp_path},
    )
    app.extensions["decision_support_facade"].image_catalog = catalog

    response = logged_in_client.get(
        "/decision-support/evidence/images/img_test_1"
    )

    assert response.status_code == 200
    assert response.data == payload
    assert response.mimetype == "image/jpeg"
    assert "trusted.jpg" not in response.headers.get("Content-Disposition", "")


def test_trusted_image_route_rejects_tampering_after_catalog_load(
    app,
    logged_in_client,
    tmp_path,
) -> None:
    image = tmp_path / "trusted.jpg"
    original = b"trusted-image-bytes"
    image.write_bytes(original)
    manifest = _image_manifest(
        tmp_path,
        image,
        hashlib.sha256(original).hexdigest(),
    )
    app.extensions["decision_support_facade"].image_catalog = TrustedImageCatalog(
        manifest,
        {"secondary_annotation": tmp_path},
    )
    image.write_bytes(b"tampered")

    response = logged_in_client.get(
        "/decision-support/evidence/images/img_test_1"
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "image_untrusted"


def test_trusted_image_catalog_rejects_manifest_path_escape(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    manifest = _image_manifest(
        corpus,
        outside,
        hashlib.sha256(outside.read_bytes()).hexdigest(),
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["images"][0]["source_path"] = "../outside.jpg"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DecisionSupportError, match="image_untrusted"):
        TrustedImageCatalog(
            manifest,
            {"secondary_annotation": corpus},
        )


def test_unknown_or_path_like_image_id_never_resolves(
    app,
    logged_in_client,
) -> None:
    unknown = logged_in_client.get(
        "/decision-support/evidence/images/unknown"
    )
    traversal = logged_in_client.get(
        "/decision-support/evidence/images/%2e%2e%2fsecret"
    )

    assert unknown.status_code == 404
    assert unknown.get_json()["code"] == "image_not_found"
    assert traversal.status_code in {400, 404}


def test_user_decision_rejects_order_like_action(logged_in_client) -> None:
    response = logged_in_client.post(
        "/decision-support/events/trend-1/user-decision",
        json={
            "action": "place_order",
            "event_data_fingerprint": "sha256:" + "1" * 64,
            "idempotency_key": "request-key-001",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_user_decision"


def test_user_decision_rejects_unknown_fields_and_oversized_note(
    logged_in_client,
) -> None:
    base = {
        "action": "accepted",
        "event_data_fingerprint": "sha256:" + "1" * 64,
        "idempotency_key": "request-key-001",
    }

    unknown = logged_in_client.post(
        "/decision-support/events/trend-1/user-decision",
        json=base | {"shares": 100},
    )
    oversized = logged_in_client.post(
        "/decision-support/events/trend-1/user-decision",
        json=base | {"note": "x" * 1001},
    )

    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "invalid_user_decision"
    assert oversized.status_code == 400
    assert oversized.get_json()["code"] == "invalid_user_decision"


def test_user_decision_calls_only_audit_provider(
    app,
    logged_in_client,
) -> None:
    calls = []

    def record(event_id, user_id, payload):
        calls.append((event_id, user_id, payload))
        return {
            "decision_id": "decision-1",
            "event_id": event_id,
            "user_id": user_id,
            **payload,
        }

    app.extensions["decision_support_facade"] = DecisionSupportFacade(
        user_decision_provider=record,
    )
    body = {
        "action": "executed_externally",
        "note": "已在券商客户端人工操作",
        "event_data_fingerprint": "sha256:" + "1" * 64,
        "idempotency_key": "request-key-001",
    }

    response = logged_in_client.post(
        "/decision-support/events/trend-1/user-decision",
        json=body,
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["action"] == "executed_externally"
    assert calls == [("trend-1", "test-user", body)]


def test_review_write_validates_bounded_json(app, logged_in_client) -> None:
    calls = []

    def review(event_id, user_id, force):
        calls.append((event_id, user_id, force))
        return {"event_id": event_id, "status": "review_pending"}

    app.extensions["decision_support_facade"] = DecisionSupportFacade(
        review_provider=review,
    )

    invalid = logged_in_client.post(
        "/decision-support/events/trend-1/review",
        json={"force": "true"},
    )
    valid = logged_in_client.post(
        "/decision-support/events/trend-1/review",
        json={"force": True},
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_review_request"
    assert valid.status_code == 200
    assert calls == [("trend-1", "test-user", True)]


def test_runtime_write_route_requires_csrf() -> None:
    from cl_app import create_app

    runtime_app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": True,
            "SCHEDULER_ENABLED": False,
        }
    )

    response = runtime_app.test_client().post(
        "/decision-support/events/e1/review",
        json={},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "csrf_failed"


class _ManualCheckRecordStub:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class _ManualCheckStoreStub:
    def __init__(self, record=None):
        self.record = record
        self.calls = []

    def get_for_event(self, event_id):
        self.calls.append(event_id)
        return self.record


class _ManualCheckResultStub:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class _ManualCheckWorkflowStub:
    def __init__(self, *, record=None, result=None):
        self.store = _ManualCheckStoreStub(record)
        self.result = result
        self.submit_calls = []

    def submit(self, event_id, snapshots):
        self.submit_calls.append((event_id, tuple(snapshots)))
        return self.result


def _manual_check_record_payload(event_id="trend-1"):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "context_fingerprint": "sha256:" + "2" * 64,
        "required_checks": [
            {
                "manual_check_id": "check-third-buy",
                "prompt": "确认次级别回抽不回中枢",
                "evidence_ids": ["lesson-20:p1", "chart-20-1"],
            }
        ],
        "status": "pending",
    }


def _manual_check_snapshot_body(
    *,
    event_id="trend-1",
    operator_id="test-user",
):
    return {
        "manual_check_id": "check-third-buy",
        "value": True,
        "operator_id": operator_id,
        "recorded_at": "2026-07-14T10:31:00+08:00",
        "event_id": event_id,
        "context_fingerprint": "sha256:" + "2" * 64,
        "evidence_ids": ["lesson-20:p1", "chart-20-1"],
    }


def test_manual_checks_get_requires_configured_workflow(
    logged_in_client,
) -> None:
    response = logged_in_client.get(
        "/decision-support/events/trend-1/manual-checks"
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "code": "manual_checks_unavailable",
        "errmsg": "manual_checks_unavailable",
    }


def test_manual_checks_get_returns_record_or_stable_not_found(
    app,
    logged_in_client,
) -> None:
    workflow = _ManualCheckWorkflowStub()
    app.extensions["decision_support_manual_check_workflow"] = workflow

    missing = logged_in_client.get(
        "/decision-support/events/trend-1/manual-checks"
    )
    record_payload = _manual_check_record_payload()
    workflow.store.record = _ManualCheckRecordStub(record_payload)
    found = logged_in_client.get(
        "/decision-support/events/trend-1/manual-checks"
    )

    assert missing.status_code == 404
    assert missing.get_json()["code"] == "manual_checks_not_found"
    assert found.status_code == 200
    assert found.get_json() == {"ok": True, "data": record_payload}
    assert workflow.store.calls == ["trend-1", "trend-1"]


@pytest.mark.parametrize(
    "body",
    (
        [],
        {},
        {"manual_checks": {}},
        {"manual_checks": [], "force": True},
        {"manual_checks": [{"value": True}]},
        {"manual_checks": [_manual_check_snapshot_body() | {"note": "x"}]},
    ),
)
def test_manual_checks_post_rejects_non_exact_payloads(
    app,
    logged_in_client,
    body,
) -> None:
    workflow = _ManualCheckWorkflowStub()
    app.extensions["decision_support_manual_check_workflow"] = workflow

    response = logged_in_client.post(
        "/decision-support/events/trend-1/manual-checks",
        json=body,
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_manual_checks"
    assert workflow.submit_calls == []


@pytest.mark.parametrize(
    "snapshot",
    (
        _manual_check_snapshot_body(event_id="forged-event"),
        _manual_check_snapshot_body(operator_id="forged-user"),
    ),
)
def test_manual_checks_post_rejects_event_or_operator_forgery(
    app,
    logged_in_client,
    snapshot,
) -> None:
    workflow = _ManualCheckWorkflowStub()
    app.extensions["decision_support_manual_check_workflow"] = workflow

    response = logged_in_client.post(
        "/decision-support/events/trend-1/manual-checks",
        json={"manual_checks": [snapshot]},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_manual_checks"
    assert workflow.submit_calls == []


def test_manual_checks_post_parses_and_submits_only_human_snapshots(
    app,
    logged_in_client,
) -> None:
    result_payload = {
        "accepted": True,
        "reasons": [],
        "record": _manual_check_record_payload() | {"status": "approved"},
        "evaluation": {"verdict": "confirm"},
    }
    workflow = _ManualCheckWorkflowStub(
        result=_ManualCheckResultStub(result_payload)
    )
    app.extensions["decision_support_manual_check_workflow"] = workflow
    snapshot = _manual_check_snapshot_body()

    response = logged_in_client.post(
        "/decision-support/events/trend-1/manual-checks",
        json={"manual_checks": [snapshot]},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "data": result_payload}
    assert len(workflow.submit_calls) == 1
    event_id, submitted = workflow.submit_calls[0]
    assert event_id == "trend-1"
    assert len(submitted) == 1
    assert submitted[0].event_id == "trend-1"
    assert submitted[0].operator_id == "test-user"
    assert submitted[0].value is True
    assert submitted[0].evidence_ids == (
        "chart-20-1",
        "lesson-20:p1",
    )


def test_manual_checks_runtime_failure_has_stable_fail_closed_json(
    app,
    logged_in_client,
) -> None:
    workflow = _ManualCheckWorkflowStub()

    def fail_submit(event_id, snapshots):
        raise RuntimeError("internal path and state must not leak")

    workflow.submit = fail_submit
    app.extensions["decision_support_manual_check_workflow"] = workflow

    response = logged_in_client.post(
        "/decision-support/events/trend-1/manual-checks",
        json={"manual_checks": [_manual_check_snapshot_body()]},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "code": "manual_checks_unavailable",
        "errmsg": "manual_checks_unavailable",
    }
    assert "internal path" not in response.get_data(as_text=True)


def test_web_decision_support_has_no_order_execution_dependency() -> None:
    roots = (
        Path("web/chanlun_chart/cl_app/blueprints/decision_support.py"),
        Path("web/chanlun_chart/cl_app/services/decision_support.py"),
    )
    source = "\n".join(
        path.read_text(encoding="utf-8").casefold() for path in roots
    )

    for forbidden in (
        "chanlun.trader",
        "chanlun.exchange",
        "paperbroker",
        "place_order",
        "open_buy",
        "open_sell",
        "cancel_order",
    ):
        assert forbidden not in source
