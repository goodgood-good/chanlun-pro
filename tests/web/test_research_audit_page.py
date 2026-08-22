from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user
import pytest

from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.metrics import calculate_metrics
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    EquityPoint,
)
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    WalkForwardWindowResult,
    build_report,
)
from cl_app.blueprints.decision_support import decision_support_bp
from cl_app.services.research_audit import (
    ResearchAuditUnavailable,
    build_research_audit_status_snapshot,
    build_research_audit_snapshot,
)
from tests.trading_system.backtest.helpers import CN


_CAUSAL_CONTROLS = [
    "survivorship_free_effective_dated_security_master",
    "decision_time_sw1_membership",
    "ex_date_only_causal_price_basis",
    "cash_and_share_corporate_action_accounting",
    "closed_bar_strict_structure_witnesses",
    "next_complete_minute_execution",
    "observed_range_and_volume_fill_guard",
    "delisted_security_zero_recovery",
    "content_addressed_algorithm_data_and_checkpoints",
]
_DATA_SOURCE_HASHES = (
    ("pit_metadata_snapshot", "sha256:" + "1" * 64),
    ("qmt_extract_manifest", "sha256:" + "2" * 64),
    ("prefix_invariance_audit", "sha256:" + "3" * 64),
    ("symbol_fact_checkpoint_tree", "sha256:" + "4" * 64),
    ("sector_fact_checkpoint_tree", "sha256:" + "5" * 64),
    ("certified_portfolio_run", "sha256:" + "6" * 64),
)


class _User(UserMixin):
    id = "researcher"


def _report(include_walk_forward: bool = False) -> dict[str, object]:
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
    if include_walk_forward:
        evaluation = replace(
            evaluation,
            walk_forward_windows=(
                WalkForwardWindowResult(
                    window_id="wf-001",
                    train_start=date(2020, 1, 1),
                    train_end=date(2022, 12, 31),
                    validation_start=date(2023, 1, 6),
                    validation_end=date(2023, 7, 5),
                    test_start=date(2023, 7, 11),
                    test_end=date(2024, 1, 10),
                    selected_parameters=(
                        ("base_trade_risk", "0.0035"),
                        ("max_portfolio_heat", "0.015"),
                        ("first_buy_risk_multiplier", "0.25"),
                    ),
                    test_metrics=calculate_metrics(run),
                    closed_trade_count=0,
                ),
            ),
        )
    return build_report(
        evidence=DataEvidence(
            grade="research_only",
            failures=("historical_sector_membership_missing",),
            warnings=(
                "terminal_open_positions_marked_to_market_not_same_bar_liquidated",
            ),
            coverage=(("bar_status_coverage", Decimal("1")),),
        ),
        result=evaluation,
        ablations=(),
        benchmarks=(),
        generated_at=generated_at,
        requested_range=(date(2025, 7, 25), date(2026, 7, 24)),
        effective_range=(date(2025, 8, 1), date(2026, 7, 24)),
        evaluation_mode="fixed_policy_one_year",
        sector_price_source="qmt-sw1-pit-composite",
        algorithm_hashes=(("src/fixture.py", "sha256:" + "a" * 64),),
        data_source_hashes=_DATA_SOURCE_HASHES,
        universe_summary={
            "catalog_source": "qmt_sw1_with_cninfo_effective_dates",
            "eligible_sector_count": 31,
            "sector_composite_member_limit": None,
            "selected_symbol_count": 5201,
            "archived_intersecting_symbol_count": 5227,
            "unclassified_excluded_symbol_count": 26,
            "corporate_action_count": 7763,
            "causal_evaluation_count": 4000,
        },
    )


def _algorithm_revision(report: dict[str, object]) -> str:
    hashes = tuple(
        (str(row["source"]), str(row["sha256"]))
        for row in report["algorithm_hashes"]
    )
    encoded = json.dumps(
        hashes,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_passed_gate(root: Path, report: dict[str, object]) -> None:
    directory = root / "audit/chanlun_trading_system_backtest"
    report_path = directory / "certified_report.json"
    (directory / "causality_gate.json").write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "passed",
                "pnl_generated": True,
                "algorithm_revision": _algorithm_revision(report),
                "pit_snapshot_sha256": dict(_DATA_SOURCE_HASHES)[
                    "pit_metadata_snapshot"
                ],
                "validated_symbol_fact_count": 5201,
                "validated_decision_count": 4000,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": [],
                "report": str(report_path.resolve()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    path = tmp_path / "audit/chanlun_trading_system_backtest/certified_report.json"
    path.parent.mkdir(parents=True)
    report = _report()
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    _write_passed_gate(tmp_path, report)
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


def test_snapshot_exposes_only_formal_read_only_strategy(audit_root: Path) -> None:
    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["schema"] == "research-audit-page"
    assert snapshot["source_kind"] == "certified_report"
    assert snapshot["strategy_id"] == "chanlun_source_faithful"
    assert snapshot["active_strategy_count"] == 1
    assert snapshot["read_only"] is True
    assert snapshot["historical"] is True
    assert snapshot["no_order_execution"] is True
    assert snapshot["data_evidence"]["grade"] == "research_only"
    assert snapshot["verdict"]["live_ready"] is False
    assert snapshot["artifact"]["integrity_verified"] is True
    assert snapshot["closed_trade_net_pnl"] == "0"
    assert snapshot["terminal_positions_marked_to_market"] is True


def test_snapshot_rejects_content_tampering(audit_root: Path) -> None:
    path = audit_root / "audit/chanlun_trading_system_backtest/certified_report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["aggregate_out_of_sample"]["net_return"] = "99"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ResearchAuditUnavailable, match="artifact_hash_mismatch"):
        build_research_audit_snapshot(audit_root)


def test_causality_gate_blocks_report(audit_root: Path) -> None:
    gate = audit_root / "audit/chanlun_trading_system_backtest/causality_gate.json"
    gate.write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "blocked",
                "pnl_generated": False,
                "algorithm_revision": "sha256:" + "a" * 64,
                "pit_snapshot_sha256": "sha256:" + "1" * 64,
                "validated_symbol_fact_count": 1,
                "validated_decision_count": 0,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": ["survivorship_free_universe_unverified"],
                "report": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResearchAuditUnavailable) as raised:
        build_research_audit_snapshot(audit_root)

    assert raised.value.code == "causality_gate_blocked"
    assert raised.value.details is not None
    assert raised.value.details["pnl_generated"] is False


def test_page_explains_blocked_gate_generated_no_pnl(
    app: Flask,
    audit_root: Path,
) -> None:
    gate = audit_root / "audit/chanlun_trading_system_backtest/causality_gate.json"
    gate.write_text(
        json.dumps(
            {
                "schema": "chanlun-backtest-causality-gate",
                "checked_at": "2026-07-25T12:00:00+08:00",
                "status": "blocked",
                "pnl_generated": False,
                "algorithm_revision": "sha256:" + "b" * 64,
                "pit_snapshot_sha256": "sha256:" + "1" * 64,
                "validated_symbol_fact_count": 1,
                "validated_decision_count": 0,
                "proven_controls": _CAUSAL_CONTROLS,
                "failures": ["historical_sector_membership_unverified"],
                "report": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "未生成正式回测收益" in html
    assert "系统已在计算收益前停止" in html
    assert "historical_sector_membership_unverified" in html


def test_snapshot_reads_only_canonical_report(audit_root: Path) -> None:
    directory = audit_root / "audit/chanlun_trading_system_backtest"
    (directory / "newer_unrelated_strategy.json").write_text(
        json.dumps(
            {
                "strategy_id": "unrelated_strategy",
                "generated_at": "2099-01-01T00:00:00+08:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = build_research_audit_snapshot(audit_root)

    assert snapshot["strategy_id"] == "chanlun_source_faithful"
    assert snapshot["artifact"]["relative_path"].endswith(
        "/certified_report.json"
    )


def test_snapshot_accepts_unified_buy_point_contract(tmp_path: Path) -> None:
    path = tmp_path / "audit/chanlun_trading_system_backtest/certified_report.json"
    path.parent.mkdir(parents=True)
    report = _report(True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    _write_passed_gate(tmp_path, report)

    contract = build_research_audit_snapshot(tmp_path)["execution_contract"]

    assert contract["point_classes_analyzed_independently"] is True
    assert contract["buy_point_classes_share_execution_logic"] is True
    assert contract["trade_frequency"] == "5m"
    assert contract["segment_difference_frequency"] == "1m"
    assert contract["segment_difference_required_for_trade_signal"] is False


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
    assert "chanlun_source_faithful" in html
    assert "未达到实盘标准" in html
    assert "30m 大级别结构" in html
    assert "5m 操作确认买卖级别" in html
    assert "1m 区间套级别" in html
    assert "一、二、三类买卖点独立" in html
    assert "一、二、三类统一" in html
    assert "5201 / 5227 个历史标的纳入" in html
    assert "总收益包含期末未平仓持仓的盯市损益" in html
    assert "旧双轨" not in html
    assert "<form" not in html.lower()


def test_audit_page_leads_with_evidence_and_verdict(app: Flask) -> None:
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    headings = (
        "是否达到实盘标准",
        "数据证据等级",
        "样本充分性",
        "固定策略收益、回撤与风险",
    )
    for heading in headings:
        assert heading in html
    assert [html.index(heading) for heading in headings] == sorted(
        html.index(heading) for heading in headings
    )
    assert "证据不足，结果仅可用于研究" in html
    assert 'data-live-ready="false"' in html


def test_page_fails_closed_when_artifact_is_missing(
    app: Flask,
    audit_root: Path,
) -> None:
    directory = audit_root / "audit/chanlun_trading_system_backtest"
    (directory / "certified_report.json").unlink()
    (directory / "unsupported_report.json").write_text(
        json.dumps(_report(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    client = app.test_client()
    assert client.get("/_test/login").status_code == 200

    response = client.get("/decision-support/research-audit")
    data = client.get("/decision-support/research-audit/data")

    assert response.status_code == 200
    assert "artifact_unavailable" in response.get_data(as_text=True)
    assert "历史资料可读取，正式回测结论尚不可用" in response.get_data(
        as_text=True
    )
    assert data.status_code == 200
    assert data.get_json()["data"]["schema"] == "research-audit-status-page"
    assert data.get_json()["data"]["formal_report"]["available"] is False


def test_status_snapshot_inventory_exposes_real_material_without_pnl(
    tmp_path: Path,
) -> None:
    dataset = (
        tmp_path
        / "audit/chanlun_trading_system_backtest/fixed_year_2025_2026"
    )
    symbols = dataset / "symbols"
    symbols.mkdir(parents=True)
    metadata = {
        "schema": "chanlun-qmt-pit-metadata/v1",
        "source_start": "2025-05-01",
        "source_end": "2026-07-24",
        "captured_at": "2026-07-27T12:00:00+08:00",
        "securities": [{"symbol": "688132.SH"}, {"symbol": "513100.SH"}],
        "memberships": [{"symbol": "688132.SH", "sector": "电子"}],
        "factors": [{"symbol": "688132.SH", "date": "2026-01-01"}],
        "qmt_sw1_sector_names": ["电子"],
        "content_sha256": "sha256:" + "7" * 64,
    }
    (dataset / "pit_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = dataset / "extract_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "chanlun-fixed-year-qmt-run",
                "generated_at": "2026-08-16T12:00:00+08:00",
                "started_at": "2026-08-16T11:00:00+08:00",
                "complete": False,
                "algorithm": {"revision": "sha256:" + "8" * 64},
                "summary": {
                    "selected_symbol_count": 2,
                    "completed_symbol_count": 1,
                    "failed_symbol_count": 0,
                    "evaluation_count": 4,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old_manifest_time = datetime.now().timestamp() - 20 * 60
    os.utime(manifest_path, (old_manifest_time, old_manifest_time))
    (symbols / "688132.SH.pkl").write_bytes(b"checkpoint")
    live_audit = tmp_path / "audit/chanlun_live_integration"
    live_audit.mkdir(parents=True)
    (live_audit / "notification_delivery.json").write_text(
        json.dumps(
            {
                "schema": "chanlun-notification-delivery-audit",
                "status": "passed",
            }
        ),
        encoding="utf-8",
    )
    (live_audit / "utf8_bom.json").write_bytes(
        json.dumps({"status": "complete"}).encode("utf-8-sig")
    )
    (live_audit / "utf16.json").write_bytes(
        json.dumps({"status": "complete"}).encode("utf-16")
    )

    snapshot = build_research_audit_status_snapshot(
        tmp_path,
        formal_error_code="causality_gate_unavailable",
    )

    assert snapshot["schema"] == "research-audit-status-page"
    assert snapshot["formal_report"]["available"] is False
    assert "aggregate_out_of_sample" not in snapshot
    assert snapshot["historical_dataset"]["security_count"] == 2
    assert snapshot["historical_dataset"]["symbol_checkpoint_count"] == 1
    assert snapshot["historical_dataset"]["checkpoint_coverage"] == 0.5
    assert snapshot["historical_replay"]["status"] == "running"
    assert snapshot["historical_replay"]["completed_symbol_count"] == 1
    assert snapshot["historical_replay"]["remaining_symbol_count"] == 1
    assert snapshot["historical_replay"]["completion_ratio"] == 0.5
    assert snapshot["historical_replay"]["heartbeat_source"] == (
        "symbol_checkpoint"
    )
    assert snapshot["historical_replay"]["latest_checkpoint_modified_at"]
    assert snapshot["historical_replay"]["manifest_modified_at"]
    assert snapshot["audit_artifact_count"] == 3
    assert {row["status"] for row in snapshot["audit_artifacts"]} == {
        "passed",
        "complete",
    }
    assert {row["encoding"] for row in snapshot["audit_artifacts"]} == {
        "utf-8",
        "utf-8-sig",
        "utf-16",
    }


def test_research_audit_styles_keep_dense_content_readable(app: Flask) -> None:
    response = app.test_client().get("/static/css/research_audit.css")
    css = response.get_data(as_text=True).replace("\r\n", "\n")

    assert response.status_code == 200
    assert "font-size: 16px;" in css
    assert ".ra-metric-card small {\n  font-size: 13px;" in css
    assert "th {\n  color:" in css
