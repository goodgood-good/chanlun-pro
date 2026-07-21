from __future__ import annotations

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from cl_app.blueprints.alert import alert_bp
from cl_app.blueprints.decision_support import decision_support_bp


class _User(UserMixin):
    id = "trading-screening-user"


class _TradingScreeningService:
    def __init__(self) -> None:
        self.refresh_requests = 0

    def ensure_refresh(self) -> bool:
        self.refresh_requests += 1
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "chanlun-trading-screening/v2",
            "algorithm_version": "chanlun_source_faithful_v2",
            "structure_version": "v2",
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
    app.register_blueprint(alert_bp)
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
    assert payload["data"]["schema_version"] == "chanlun-trading-screening/v2"
    assert payload["data"]["structure_version"] == "v2"
    service = app.extensions["decision_support_trading_screening"]
    assert service.refresh_requests == 1


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
    assert 'data-schema="chanlun-trading-screening/v2"' in html
    assert 'id="es-sector-completion"' in html
    assert "板块质量" in html
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
    assert "三买只取第一中枢" in html
    assert "历史研究/审计成果" in html
    assert "只读研究 · 无订单能力" in html
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
