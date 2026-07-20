from __future__ import annotations

import importlib
from pathlib import Path

from chanlun.decision_support import scanner
from cl_app import create_app
from cl_app.services.trading_notifications import SignalNotificationDispatcher


ROOT = Path(__file__).resolve().parents[2]


def test_only_new_trading_system_is_importable() -> None:
    assert scanner.ACTIVE_STRATEGY_ID == "chanlun_original_low_drawdown_v1"
    assert hasattr(scanner, "TradingEngine")
    assert not hasattr(scanner, "classify_early_signal")
    assert not hasattr(scanner, "classify_" + "early_signals")
    assert not hasattr(scanner, "classify_" + "sector_level")


def test_web_surface_has_only_read_only_new_strategy_routes() -> None:
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
            "/decision-support/research-audit",
            "/decision-support/research-audit/data",
        }
        assert "decision_support_facade" not in app.extensions
        assert "install_decision_support_runtime" not in app.extensions
        assert "decision_support_trading_screening" in app.extensions
        assert "decision_support_early_screening" not in app.extensions
    finally:
        app.extensions["shutdown_runtime_services"]()
    assert "decision_support_facade" not in app.extensions
    assert "install_decision_support_runtime" not in app.extensions


def test_web_factory_wires_private_dingtalk_dispatcher_into_trading_screening(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "chanlun.config.RECURSIVE_MONITOR_CONFIG",
        {
            "common": {
                "dingtalk_webhook": "https://legacy.invalid/robot/send",
                "dingtalk_keyword": "旧通知关键词",
            }
        },
    )
    app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "EARLY_SCREENING_DINGTALK_WEBHOOK": (
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


def test_retired_ui_and_calibration_entry_files_are_deleted() -> None:
    retired_paths = (
        "web/chanlun_chart/cl_app/static/js/decision_support.js",
        "web/chanlun_chart/cl_app/static/js/__tests__/decision_support.test.js",
        "tools/build_causal_oos_candidates.py",
        "tools/decision_support_forward_oos.py",
    )
    assert all(not (ROOT / relative).exists() for relative in retired_paths)
    index = (ROOT / "web/chanlun_chart/cl_app/templates/index.html").read_text(
        encoding="utf-8"
    )
    assert "趋势延续" not in index
    assert "底部反转" not in index
    assert "decision_support.js" not in index
    assert "缠论提前选股" in index


def test_retired_web_composition_is_physically_removed() -> None:
    app_factory = (ROOT / "web/chanlun_chart/cl_app/__init__.py").read_text(
        encoding="utf-8"
    )
    for retired_name in (
        "DECISION_SUPPORT_ENABLED",
        "build_persistent_decision_support_facade",
        "install_decision_support_runtime",
        "DecisionEventStore",
        "OpportunityStore",
    ):
        assert retired_name not in app_factory

    retired_paths = (
        "web/chanlun_chart/cl_app/services/decision_support.py",
        "tools/build_current_readiness_input.py",
        "tools/build_historical_decision_dossier.py",
        "tools/build_decision_replay_evidence.py",
        "tools/build_decision_validation_evidence.py",
        "tools/decision_support_validation_evidence.py",
        "tools/validate_decision_support.py",
    )
    assert all(not (ROOT / relative).exists() for relative in retired_paths)


def test_reusable_risk_paper_and_backtest_infrastructure_still_imports() -> None:
    for module_name in (
        "chanlun.decision_support.risk",
        "chanlun.decision_support.paper_adapter",
        "chanlun.decision_support.paper_admission",
        "chanlun.decision_support.paper_read_model",
        "chanlun.decision_support.paper_runtime",
    ):
        assert importlib.import_module(module_name) is not None


def test_retired_early_screening_backtest_is_physically_removed() -> None:
    retired_paths = (
        "src/chanlun/decision_support/early_screening_backtest.py",
        "tools/backtest_early_screening.py",
        "tests/decision_support/test_early_screening_backtest.py",
        "web/chanlun_chart/cl_app/services/early_screening.py",
        "src/chanlun/decision_support/early_screening.py",
        "src/chanlun/decision_support/sector_chanlun_screening.py",
        "src/chanlun/decision_support/strategies.py",
    )

    assert all(not (ROOT / relative).exists() for relative in retired_paths)


def test_decision_support_source_contains_no_retired_strategy_identity() -> None:
    source_root = ROOT / "src/chanlun/decision_support"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(source_root.glob("*.py"))
    )
    for retired_name in (
        "trend_continuation",
        "bottom_reversal",
        "live_parity",
        "sector_first_" + "early_screening",
        "classify_" + "early_signals",
        "classify_" + "sector_level",
        "TREND_CONTINUATION",
        "BOTTOM_REVERSAL",
    ):
        assert retired_name not in source


def test_every_remaining_decision_support_module_imports() -> None:
    source_root = ROOT / "src/chanlun/decision_support"
    module_names = (
        f"chanlun.decision_support.{path.stem}"
        for path in sorted(source_root.glob("*.py"))
        if path.name != "__init__.py"
    )
    for module_name in module_names:
        assert importlib.import_module(module_name) is not None
