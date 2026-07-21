from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    EquityPoint,
)
from chanlun.decision_support.trading_system.backtest.metrics import calculate_metrics
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    WalkForwardWindowResult,
    build_report,
)
from cl_app.blueprints.decision_support import decision_support_bp
from cl_app.services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_snapshot,
)
from tests.trading_system.backtest.helpers import CN


class _User(UserMixin):
    id = "researcher"


def _report(
    first_center_selection: bool | None = None,
) -> dict[str, object]:
    generated_at = datetime(2026, 7, 20, 18, 0, tzinfo=CN)
    run = BacktestRun(
        fills=(),
        trades=(),
        equity_curve=(
            EquityPoint(
                generated_at - timedelta(days=30),
                Decimal("100"),
                Decimal("0"),
                Decimal("100"),
                Decimal("0"),
            ),
            EquityPoint(
                generated_at,
                Decimal("101"),
                Decimal("0"),
                Decimal("101"),
                Decimal("0"),
            ),
        ),
        open_positions=(),
        pending_exits=(),
    )
    evaluation = BacktestEvaluationResult(
        aggregate_run=run,
        bootstrap_repetitions=20,
    )
    if first_center_selection is not None:
        window = WalkForwardWindowResult(
            window_id="wf-001",
            train_start=date(2020, 1, 1),
            train_end=date(2022, 12, 31),
            validation_start=date(2023, 1, 6),
            validation_end=date(2023, 7, 5),
            test_start=date(2023, 7, 11),
            test_end=date(2024, 1, 10),
            selected_parameters=(
                ("base_trade_risk", "0.0035"),
                ("first_center_three_buy_only", first_center_selection),
                ("max_portfolio_heat", "0.015"),
                ("first_buy_risk_multiplier", "0.25"),
            ),
            test_metrics=calculate_metrics(run),
            closed_trade_count=0,
        )
        evaluation = replace(evaluation, walk_forward_windows=(window,))
    return build_report(
        evidence=DataEvidence(
            grade="research_only",
            failures=("historical_sector_membership_missing",),
            warnings=(),
            coverage=(("bar_status_coverage", Decimal("1")),),
        ),
        result=evaluation,
        ablations=(),
        benchmarks=(),
        generated_at=generated_at,
    )


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    path = (
        tmp_path
        / "audit/chanlun_trading_system_backtest/research_report.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_report(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def app(audit_root: Path) -> Flask:
    repository_root = Path(__file__).resolve().parents[2]
    app = Flask(
        __name__,
        template_folder=str(repository_root / "web/chanlun_chart/cl_app/templates"),
        static_folder=str(repository_root / "web/chanlun_chart/cl_app/static"),
    )
    app.config.update(
        TESTING=True,
        SECRET_KEY="research-audit-test-secret",
        RESEARCH_AUDIT_ROOT=audit_root,
    )
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

    app.register_blueprint(decision_support_bp)
    return app


def test_snapshot_exposes_only_new_read_only_strategy(audit_root: Path) -> None:
    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["schema_version"] == "research-audit-page-v11"
    assert snapshot["strategy_id"] == "chanlun_source_faithful_v2"
    assert snapshot["active_strategy_count"] == 1
    assert snapshot["read_only"] is True
    assert snapshot["historical"] is True
    assert snapshot["no_order_execution"] is True
    assert snapshot["data_evidence"]["grade"] == "research_only"
    assert snapshot["aggregate_out_of_sample"]["annualized_return"] is None
    assert snapshot["verdict"]["live_ready"] is False
    assert snapshot["artifact"]["integrity_verified"] is True


def test_snapshot_rejects_content_tampering(audit_root: Path) -> None:
    path = next(
        (audit_root / "audit/chanlun_trading_system_backtest").glob("*.json")
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["aggregate_out_of_sample"]["net_return"] = "99"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ResearchAuditUnavailable, match="artifact_hash_mismatch"):
        build_research_audit_snapshot(audit_root)


def test_snapshot_accepts_locked_non_first_center_selection(tmp_path: Path) -> None:
    path = tmp_path / "audit/chanlun_trading_system_backtest/research_report.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_report(False), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    snapshot = build_research_audit_snapshot(tmp_path)

    contract = snapshot["execution_contract"]
    assert contract["first_center_three_buy_only"] is False
    assert contract["first_center_three_buy_mode"] == "walk_forward_selected"
    assert contract["first_center_three_buy_selected_values"] == [False]


def test_page_and_data_endpoint_are_login_protected_and_read_only(app: Flask) -> None:
    client = app.test_client()
    assert client.get("/decision-support/research-audit").status_code == 401
    assert client.get("/decision-support/research-audit/data").status_code == 401
    assert client.get("/_test/login").status_code == 200

    page = client.get("/decision-support/research-audit")
    data = client.get("/decision-support/research-audit/data")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "private, no-store"
    assert data.status_code == 200
    assert data.get_json()["data"]["active_strategy_count"] == 1
    assert "历史研究 / 审计成果" in html
    assert "未达到实盘标准" in html
    assert "30m 大级别结构" in html
    assert "5m 可操作级别" in html
    assert "1m 精细触发" in html
    assert "一、二、三类买卖点独立" in html
    assert "第一中枢三买" in html
    assert "通达信 880" in html
    assert "样本门槛" in html
    assert "年化收益" not in html
    assert "旧双轨" not in html
    assert "sector_first_" + "early_screening" not in html
    assert "<form" not in html.lower()
    assert 'method="post"' not in html.lower()


def test_audit_page_leads_with_evidence_and_out_of_sample_verdict(
    app: Flask,
) -> None:
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    headings = (
        "是否达到实盘标准",
        "数据证据等级",
        "样本充分性",
        "样本外净收益",
        "样本外最大回撤",
        "样本外 Calmar",
    )
    for heading in headings:
        assert heading in html
    assert [html.index(heading) for heading in headings] == sorted(
        html.index(heading) for heading in headings
    )
    for section in (
        "一、二、三类买点独立归因",
        "滚动样本外窗口",
        "过滤器消融与样本代价",
        "参数稳健性",
        "基线与市场参照",
        "集中度与限制",
        "算法源文件哈希",
    ):
        assert section in html
    assert "证据不足，结果仅可用于研究" in html
    assert 'data-live-ready="false"' in html


def test_page_fails_closed_when_new_artifact_is_missing(
    app: Flask,
    audit_root: Path,
) -> None:
    path = next(
        (audit_root / "audit/chanlun_trading_system_backtest").glob("*.json")
    )
    path.unlink()
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")

    assert response.status_code == 503
    assert "artifact_unavailable" in response.get_data(as_text=True)


def test_research_audit_styles_keep_dense_content_readable(app: Flask) -> None:
    client = app.test_client()

    response = client.get("/static/css/research_audit.css")
    css = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "font-size: 16px;" in css
    assert ".ra-metric-card small {\n  font-size: 13px;" in css
    assert "th {\n  color:" in css
    assert "font-size: 13px;" in css
