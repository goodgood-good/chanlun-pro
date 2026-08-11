from __future__ import annotations

import importlib
from pathlib import Path

from chanlun.decision_support.trading_system.engine import TradingEngine
from chanlun.decision_support.trading_system.backtest.report import (
    STRATEGY_ID as BACKTEST_STRATEGY_ID,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)
from cl_app import create_app
from cl_app.services.trading_notifications import (
    STRATEGY_ID as NOTIFICATION_STRATEGY_ID,
    SignalNotificationDispatcher,
)
from cl_app.services.trading_screening import TradingScreeningConfig


ROOT = Path(__file__).resolve().parents[2]


def test_only_new_trading_system_is_importable() -> None:
    assert STRICT_STRATEGY_ID == "chanlun_source_faithful"
    assert TradingEngine is not None
    assert not (ROOT / "src/chanlun/decision_support/scanner.py").exists()


def test_backtest_scan_and_notification_share_one_strategy_id() -> None:
    assert {
        STRICT_STRATEGY_ID,
        BACKTEST_STRATEGY_ID,
        NOTIFICATION_STRATEGY_ID,
        TradingScreeningConfig().algorithm_id,
    } == {"chanlun_source_faithful"}


def test_web_surface_has_only_current_screening_and_human_review_routes() -> None:
    app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
        },
        start_scheduler=False,
    )
    try:
        routes = {
            rule.rule
            for rule in app.url_map.iter_rules()
            if rule.rule.startswith("/decision-support/")
        }
        assert routes == {
            "/decision-support/early-screening",
            "/decision-support/early-signals",
            "/decision-support/human-review/data",
            "/decision-support/human-review/candidate-detail",
            "/decision-support/human-review/feedback",
            "/decision-support/research-audit",
            "/decision-support/research-audit/data",
        }
        feedback_rule = next(
            rule
            for rule in app.url_map.iter_rules()
            if rule.rule == "/decision-support/human-review/feedback"
        )
        assert feedback_rule.methods == {"OPTIONS", "POST"}
        assert all("order" not in route.lower() for route in routes)
        assert "decision_support_facade" not in app.extensions
        assert "install_decision_support_runtime" not in app.extensions
        assert "decision_support_trading_screening" in app.extensions
        assert "decision_support_early_screening" not in app.extensions
    finally:
        app.extensions["shutdown_runtime_services"]()
    assert "decision_support_facade" not in app.extensions
    assert "install_decision_support_runtime" not in app.extensions


def test_web_factory_wires_private_dingtalk_dispatcher_into_trading_screening() -> None:
    app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_DINGTALK_WEBHOOK": (
                "https://example.invalid/robot/send?access_token=redacted"
            ),
        },
        start_scheduler=False,
    )
    try:
        service = app.extensions["decision_support_trading_screening"]
        assert isinstance(service._notifier, SignalNotificationDispatcher)
        notifier = service._notifier._notifier
        assert notifier.available is True
        assert notifier.keyword == "买卖通知"
    finally:
        app.extensions["shutdown_runtime_services"]()


def test_removed_ui_and_calibration_entry_files_are_absent() -> None:
    removed_paths = (
        "web/chanlun_chart/cl_app/static/js/decision_support.js",
        "web/chanlun_chart/cl_app/static/js/__tests__/decision_support.test.js",
        "tools/build_causal_oos_candidates.py",
        "tools/decision_support_forward_oos.py",
    )
    assert all(not (ROOT / relative).exists() for relative in removed_paths)
    index = (ROOT / "web/chanlun_chart/cl_app/templates/index.html").read_text(
        encoding="utf-8"
    )
    assert "趋势延续" not in index
    assert "底部反转" not in index
    assert "decision_support.js" not in index
    assert "缠论提前选股" in index


def test_removed_web_composition_is_absent() -> None:
    app_factory = (ROOT / "web/chanlun_chart/cl_app/__init__.py").read_text(
        encoding="utf-8"
    )
    for removed_name in (
        "DECISION_SUPPORT_ENABLED",
        "build_persistent_decision_support_facade",
        "install_decision_support_runtime",
        "DecisionEventStore",
        "OpportunityStore",
    ):
        assert removed_name not in app_factory

    removed_paths = (
        "web/chanlun_chart/cl_app/services/decision_support.py",
        "tools/build_current_readiness_input.py",
        "tools/build_historical_decision_dossier.py",
        "tools/build_decision_replay_evidence.py",
        "tools/build_decision_validation_evidence.py",
        "tools/decision_support_validation_evidence.py",
        "tools/validate_decision_support.py",
    )
    assert all(not (ROOT / relative).exists() for relative in removed_paths)


def test_removed_decision_and_paper_subsystem_is_absent() -> None:
    removed = (
        "risk.py",
        "paper_adapter.py",
        "paper_admission.py",
        "paper_read_model.py",
        "paper_runtime.py",
        "event_store.py",
        "review_service.py",
    )
    source_root = ROOT / "src/chanlun/decision_support"
    assert all(not (source_root / name).exists() for name in removed)


def test_removed_early_screening_backtest_is_absent() -> None:
    removed_paths = (
        "src/chanlun/decision_support/early_screening_backtest.py",
        "tools/backtest_early_screening.py",
        "tests/decision_support/test_early_screening_backtest.py",
        "web/chanlun_chart/cl_app/services/early_screening.py",
        "src/chanlun/decision_support/early_screening.py",
        "src/chanlun/decision_support/sector_chanlun_screening.py",
        "src/chanlun/decision_support/strategies.py",
    )

    assert all(not (ROOT / relative).exists() for relative in removed_paths)


def test_decision_support_source_contains_no_removed_strategy_identity() -> None:
    source_root = ROOT / "src/chanlun/decision_support"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(source_root.glob("*.py"))
    )
    for removed_name in (
        "trend_continuation",
        "bottom_reversal",
        "live_parity",
        "sector_first_" + "early_screening",
        "classify_" + "early_signals",
        "classify_" + "sector_level",
        "TREND_CONTINUATION",
        "BOTTOM_REVERSAL",
    ):
        assert removed_name not in source


def test_every_remaining_decision_support_module_imports() -> None:
    source_root = ROOT / "src/chanlun/decision_support"
    module_names = (
        f"chanlun.decision_support.{path.stem}"
        for path in sorted(source_root.glob("*.py"))
        if path.name != "__init__.py"
    )
    for module_name in module_names:
        assert importlib.import_module(module_name) is not None
