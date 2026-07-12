from __future__ import annotations

from decimal import Decimal

import pytest
from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user

from cl_app.blueprints.decision_support import decision_support_bp
from chanlun.decision_support.exit_evaluation_store import (
    SQLiteExitEvaluationStore,
)
from chanlun.decision_support.paper_admission import SQLitePaperLedger
from chanlun.decision_support.paper_read_model import PaperResearchReadModel


_PATHS = (
    "/decision-support/paper/status",
    "/decision-support/paper/account",
    "/decision-support/paper/positions",
    "/decision-support/paper/intents",
    "/decision-support/paper/fills",
    "/decision-support/paper/exits",
)


class _User(UserMixin):
    id = "paper-read-user"


@pytest.fixture
def app(tmp_path) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="paper-read-test-secret",
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

    app.extensions["decision_support_paper_read_model"] = (
        PaperResearchReadModel(
            SQLitePaperLedger(
                tmp_path / "paper-ledger.sqlite3",
                initial_cash=Decimal("100000"),
            ),
            exit_store=SQLiteExitEvaluationStore(
                tmp_path / "exit-evaluations.sqlite3"
            ),
        )
    )
    app.register_blueprint(decision_support_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in_client(client):
    assert client.get("/_test/login").status_code == 200
    return client


def test_paper_routes_require_login(client) -> None:
    for path in _PATHS:
        response = client.get(path)
        assert response.status_code == 401
        assert response.get_json()["code"] == "authentication_required"


def test_paper_routes_return_only_research_snapshots(logged_in_client) -> None:
    for path in _PATHS:
        response = logged_in_client.get(path)
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["ok"] is True
        assert response.headers["Cache-Control"] == "private, no-store"
        assert payload["data"]["mode"] == "research_paper"
        assert payload["data"]["read_only"] is True
        assert payload["data"]["auto_order_enabled"] is False
        assert payload["data"]["live_order_capability"] is False


def test_paper_url_map_has_no_write_capability(app, logged_in_client) -> None:
    rules = {
        rule.rule: rule.methods
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/decision-support/paper/")
    }

    assert set(rules) == set(_PATHS)
    assert all(methods == {"GET", "HEAD", "OPTIONS"} for methods in rules.values())
    for path in _PATHS:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(logged_in_client, method)(path).status_code == 405


def test_paper_routes_fail_closed_when_unconfigured(
    app,
    logged_in_client,
) -> None:
    app.extensions.pop("decision_support_paper_read_model")

    response = logged_in_client.get("/decision-support/paper/status")

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "code": "paper_research_unavailable",
        "errmsg": "paper_research_unavailable",
    }


def test_paper_routes_redact_runtime_failures(app, logged_in_client) -> None:
    model = app.extensions["decision_support_paper_read_model"]

    def fail_status():
        raise RuntimeError("secret database path")

    model.status = fail_status
    response = logged_in_client.get("/decision-support/paper/status")

    assert response.status_code == 503
    assert response.get_json()["code"] == "paper_research_unavailable"
    assert "secret database path" not in response.get_data(as_text=True)
