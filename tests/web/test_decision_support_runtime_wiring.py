from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.decision_support.monitor import MonitorConfig
from chanlun.decision_support.manual_check_workflow import FileManualCheckStore
from chanlun.decision_support.paper_adapter import PaperFeeSchedule
from chanlun.decision_support.paper_admission import SQLitePaperLedger
from chanlun.decision_support.paper_read_model import PaperResearchReadModel
from chanlun.decision_support.exit_evaluation_store import (
    SQLiteExitEvaluationStore,
)
from cl_app import create_app
from cl_app.services.decision_support import DecisionSupportFacade


def _app_config(**changes):
    values = {
        "TESTING": True,
        "VALIDATE_WEB_SECURITY": False,
        "WTF_CSRF_ENABLED": False,
        "SCHEDULER_ENABLED": False,
    }
    values.update(changes)
    return values


def test_decision_support_runtime_is_disabled_and_uninstalled_by_default() -> None:
    app = create_app(_app_config())

    status = app.extensions["decision_support_runtime_status"]()

    assert status == {
        "enabled": False,
        "installed": False,
        "jobs": (),
        "error": None,
    }
    assert app.extensions["install_decision_support_runtime"]({}) is None


def test_enabled_runtime_requires_explicit_same_bar_account_provider() -> None:
    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
        )
    )

    with pytest.raises(RuntimeError, match="account_provider"):
        app.extensions["install_decision_support_runtime"]({})

    status = app.extensions["decision_support_runtime_status"]()
    assert status["enabled"] is True
    assert status["installed"] is False
    assert status["error"] == "account_provider_unavailable"


def test_enabled_runtime_wires_production_review_facade_and_two_jobs(
    tmp_path,
) -> None:
    calls: dict[str, object] = {}
    dynamic_monitor = object()
    account_provider = lambda **_: object()
    llm_provider = object()
    runtime = SimpleNamespace(
        config=MonitorConfig(enabled=True, paper_enabled=False)
    )
    manual_check_workflow = object()
    composition = SimpleNamespace(
        runtime=runtime,
        manual_check_workflow=manual_check_workflow,
        review_provider=lambda event_id, user_id, force: {
            "event_id": event_id,
            "user_id": user_id,
            "force": force,
        },
        promotion_provider=lambda: {
            "state": "research",
            "paper_gate_pending": True,
            "reasons": ["paper_observation_gate_pending"],
        },
        rule_evidence_resolver=lambda event: event,
    )

    def composition_factory(**kwargs):
        calls["composition_kwargs"] = kwargs
        return composition

    def job_registrar(scheduler, supplied_runtime):
        calls["scheduler"] = scheduler
        calls["runtime"] = supplied_runtime
        return {"scan": object(), "review": object()}

    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=account_provider,
            DECISION_SUPPORT_DYNAMIC_MONITOR=dynamic_monitor,
            DECISION_SUPPORT_LLM_PROVIDER=llm_provider,
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=False,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=composition_factory,
            DECISION_SUPPORT_JOB_REGISTRAR=job_registrar,
            DECISION_SUPPORT_MANUAL_CHECK_DIR=tmp_path / "manual-checks",
        )
    )

    installed = app.extensions["install_decision_support_runtime"]({})

    assert installed is composition
    kwargs = calls["composition_kwargs"]
    assert kwargs["dynamic_monitor"] is dynamic_monitor
    assert kwargs["account_provider"] is account_provider
    assert kwargs["llm_provider"] is llm_provider
    assert kwargs["store"] is app.extensions["decision_support_store"]
    assert kwargs["monitor_config"] == MonitorConfig(
        enabled=True,
        paper_enabled=False,
    )
    assert isinstance(kwargs["manual_check_store"], FileManualCheckStore)
    assert kwargs["manual_check_store"].root == (
        tmp_path / "manual-checks"
    ).resolve()
    assert calls["runtime"] is runtime
    assert app.extensions["decision_support_composition"] is composition
    assert (
        app.extensions["decision_support_manual_check_workflow"]
        is manual_check_workflow
    )
    assert tuple(sorted(app.extensions["decision_support_jobs"])) == (
        "review",
        "scan",
    )
    facade = app.extensions["decision_support_facade"]
    assert isinstance(facade, DecisionSupportFacade)
    assert facade.request_review("event-1", "user-1", True) == {
        "event_id": "event-1",
        "user_id": "user-1",
        "force": True,
    }
    assert app.extensions["decision_support_runtime_status"]() == {
        "enabled": True,
        "installed": True,
        "jobs": ("review", "scan"),
        "error": None,
    }


def test_enabled_paper_runtime_wires_explicit_state_and_three_jobs(
    tmp_path,
) -> None:
    calls: dict[str, object] = {}
    runtime = SimpleNamespace(
        config=MonitorConfig(enabled=True, paper_enabled=True)
    )
    paper_runtime = SimpleNamespace(
        attest_exit_snapshots=lambda snapshots: (False,) * len(snapshots),
        health=lambda: SimpleNamespace(
            mode="research_paper",
            auto_order_enabled=False,
            live_order_capability=False,
            bar_store=SimpleNamespace(
                bar_count=0,
                observed_trading_days=0,
                degraded=False,
                degraded_reason=None,
                last_bar_closed_at=None,
            ),
            bar_cycles=0,
            bar_cycle_failures=0,
            admission_cycles=0,
            admission_failures=0,
            admitted_event_count=0,
            last_error=None,
        )
    )
    paper_gateway = SimpleNamespace(
        fee_schedule_fingerprint="sha256:" + "c" * 64,
        execution_policy_fingerprint="sha256:" + "d" * 64,
    )
    strategy_run_payload = {
        "run_id": "paper-run-web-fixture",
        "epoch": 1,
        "fingerprint": "sha256:" + "e" * 64,
        "state": "active",
        "started_at": "2026-07-15T09:00:00+08:00",
        "evidence_scope": "current_epoch_only",
        "store_bindings_complete": True,
        "switch_capability": "cold_stop_drain_required",
        "rolling_switch_supported": False,
        "mutation_lease_protocol": "durable_registry_v1",
        "inflight_mutation_count": 0,
        "mutations_drained": True,
        "identity": {"schema_version": 1},
    }
    strategy_run = SimpleNamespace(
        run_id=strategy_run_payload["run_id"],
        epoch=strategy_run_payload["epoch"],
        strategy_run_fingerprint=strategy_run_payload["fingerprint"],
        status_payload=lambda: dict(strategy_run_payload),
        mutation_lease=lambda *_: nullcontext(),
    )
    paper_ledger = SQLitePaperLedger(
        tmp_path / "composition-ledger.sqlite3",
        initial_cash=Decimal("1000000"),
    )
    exit_store = SQLiteExitEvaluationStore(
        tmp_path / "composition-exits.sqlite3"
    )
    components = {
        "paper_ledger": paper_ledger,
        "trusted_bar_store": object(),
        "paper_gateway": paper_gateway,
        "paper_risk_state": object(),
        "exit_evaluation_store": exit_store,
        "exit_evaluation_service": object(),
        "paper_exit_cycle": object(),
    }
    composition = SimpleNamespace(
        runtime=runtime,
        paper_runtime=paper_runtime,
        manual_check_workflow=object(),
        review_provider=lambda *_: {},
        promotion_provider=lambda: {"state": "research"},
        rule_evidence_resolver=lambda event: event,
        strategy_run=strategy_run,
        **components,
    )

    def composition_factory(**kwargs):
        calls["composition_kwargs"] = kwargs
        return composition

    def job_registrar(
        scheduler,
        supplied_paper_runtime,
        analysis_runtime,
        *,
        strategy_run,
    ):
        assert composition.strategy_run.status_payload()["state"] == "active"
        assert strategy_run is composition.strategy_run
        calls["job_args"] = (
            scheduler,
            supplied_paper_runtime,
            analysis_runtime,
        )
        return {
            "bar": object(),
            "review": object(),
            "admission": object(),
        }

    fee_schedule = PaperFeeSchedule()
    calendar_provider = object()
    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=lambda **_: object(),
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=True,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=composition_factory,
            DECISION_SUPPORT_JOB_REGISTRAR=job_registrar,
            DECISION_SUPPORT_PAPER_INITIAL_CASH=Decimal("1000000"),
            DECISION_SUPPORT_PAPER_FEE_SCHEDULE=fee_schedule,
            DECISION_SUPPORT_PAPER_CALENDAR_PROVIDER=calendar_provider,
            DECISION_SUPPORT_PAPER_LEDGER_PATH=tmp_path / "ledger.sqlite3",
            DECISION_SUPPORT_TRUSTED_BAR_STORE_PATH=tmp_path / "bars.sqlite3",
            DECISION_SUPPORT_PAPER_RISK_STATE_PATH=tmp_path / "risk.sqlite3",
            DECISION_SUPPORT_EXIT_EVALUATION_STORE_PATH=(
                tmp_path / "exits.sqlite3"
            ),
            DECISION_SUPPORT_PAPER_STRATEGY_REGISTRY_PATH=(
                tmp_path / "strategy-runs.sqlite3"
            ),
            DECISION_SUPPORT_PAPER_STRATEGY_EPOCH=1,
            DECISION_SUPPORT_PAPER_STRATEGY_ENGINE_BUILD_FINGERPRINT=(
                "sha256:" + "1" * 64
            ),
            DECISION_SUPPORT_PAPER_SCANNER_ALGORITHM_FINGERPRINT=(
                "sha256:" + "2" * 64
            ),
            DECISION_SUPPORT_PAPER_STRUCTURE_ALGORITHM_FINGERPRINT=(
                "sha256:" + "3" * 64
            ),
            DECISION_SUPPORT_PAPER_ACCOUNT_ALGORITHM_FINGERPRINT=(
                "sha256:" + "4" * 64
            ),
            DECISION_SUPPORT_PAPER_BAR_PROVIDER_FINGERPRINT=(
                "sha256:" + "5" * 64
            ),
        )
    )

    installed = app.extensions["install_decision_support_runtime"]({})

    assert installed is composition
    kwargs = calls["composition_kwargs"]
    assert kwargs["paper_initial_cash"] == Decimal("1000000")
    assert kwargs["paper_fee_schedule"] is fee_schedule
    assert kwargs["paper_calendar_provider"] is calendar_provider
    assert kwargs["paper_ledger_path"] == tmp_path / "ledger.sqlite3"
    assert kwargs["trusted_bar_store_path"] == tmp_path / "bars.sqlite3"
    assert kwargs["paper_risk_state_path"] == tmp_path / "risk.sqlite3"
    assert kwargs["exit_evaluation_store_path"] == tmp_path / "exits.sqlite3"
    assert kwargs["paper_strategy_registry_path"] == (
        tmp_path / "strategy-runs.sqlite3"
    )
    assert kwargs["paper_strategy_epoch"] == 1
    assert kwargs["paper_strategy_engine_build_fingerprint"] == (
        "sha256:" + "1" * 64
    )
    assert calls["job_args"][1:] == (paper_runtime, runtime)
    assert tuple(sorted(app.extensions["decision_support_jobs"])) == (
        "admission",
        "bar",
        "review",
    )
    assert app.extensions["decision_support_paper_runtime"] is paper_runtime
    read_model = app.extensions["decision_support_paper_read_model"]
    assert isinstance(read_model, PaperResearchReadModel)
    assert read_model.status()["ledger_revision"] == 0
    assert read_model.status()["fee_schedule_fingerprint"] == (
        paper_gateway.fee_schedule_fingerprint
    )
    assert read_model.status()["strategy_run"] == strategy_run_payload
    for name, value in components.items():
        assert app.extensions[f"decision_support_{name}"] is value


def test_enabled_paper_runtime_requires_explicit_cash_and_fee_policy(
    tmp_path,
) -> None:
    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=lambda **_: object(),
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=True,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=lambda **_: pytest.fail(
                "composition must not be built without explicit paper policy"
            ),
            DECISION_SUPPORT_MANUAL_CHECK_DIR=tmp_path / "manual-checks",
        )
    )

    with pytest.raises(RuntimeError, match="PAPER_INITIAL_CASH"):
        app.extensions["install_decision_support_runtime"]({})


def test_enabled_paper_runtime_requires_explicit_fee_policy(tmp_path) -> None:
    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=lambda **_: object(),
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=True,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=lambda **_: pytest.fail(
                "composition must not be built without explicit fee policy"
            ),
            DECISION_SUPPORT_PAPER_INITIAL_CASH=Decimal("1000000"),
            DECISION_SUPPORT_MANUAL_CHECK_DIR=tmp_path / "manual-checks",
        )
    )

    with pytest.raises(RuntimeError, match="PAPER_FEE_SCHEDULE"):
        app.extensions["install_decision_support_runtime"]({})


def test_paper_strategy_identity_is_validated_before_job_registration(
    tmp_path,
) -> None:
    calls = {"composition": 0, "jobs": 0}

    def composition_factory(**_kwargs):
        calls["composition"] += 1
        return object()

    def job_registrar(*_args, **_kwargs):
        calls["jobs"] += 1
        return {}

    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=lambda **_: object(),
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=True,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=composition_factory,
            DECISION_SUPPORT_JOB_REGISTRAR=job_registrar,
            DECISION_SUPPORT_PAPER_INITIAL_CASH=Decimal("1000000"),
            DECISION_SUPPORT_PAPER_FEE_SCHEDULE=PaperFeeSchedule(),
            DECISION_SUPPORT_MANUAL_CHECK_DIR=tmp_path / "manual-checks",
        )
    )

    with pytest.raises(RuntimeError, match="PAPER_STRATEGY_EPOCH"):
        app.extensions["install_decision_support_runtime"]({})
    assert calls == {"composition": 0, "jobs": 0}


@pytest.mark.parametrize(
    "unsafe_status",
    [
        {
            "switch_capability": "rolling_switch",
            "rolling_switch_supported": True,
            "mutation_lease_protocol": "durable_registry_v1",
            "inflight_mutation_count": 0,
            "mutations_drained": True,
        },
        {
            "switch_capability": "cold_stop_drain_required",
            "rolling_switch_supported": False,
            "mutation_lease_protocol": "durable_registry_v1",
            "inflight_mutation_count": 1,
            "mutations_drained": True,
        },
    ],
    ids=("unsafe-rolling", "false-drained-claim"),
)
def test_strategy_run_mismatch_never_registers_paper_jobs(
    tmp_path,
    unsafe_status,
) -> None:
    calls = {"jobs": 0}
    composition = SimpleNamespace(
        runtime=SimpleNamespace(
            config=MonitorConfig(enabled=True, paper_enabled=True)
        ),
        paper_runtime=object(),
        strategy_run=SimpleNamespace(
            status_payload=lambda: {
                "run_id": "unsafe-rolling-run",
                "epoch": 1,
                "fingerprint": "sha256:" + "e" * 64,
                "state": "active",
                "started_at": "2026-07-15T09:00:00+08:00",
                "evidence_scope": "current_epoch_only",
                "store_bindings_complete": True,
                "identity": {"schema_version": 1},
                **unsafe_status,
            },
            mutation_lease=lambda *_: nullcontext(),
        ),
    )

    def job_registrar(*_args, **_kwargs):
        calls["jobs"] += 1
        return {}

    app = create_app(
        _app_config(
            DECISION_SUPPORT_ENABLED=True,
            DECISION_SUPPORT_ACCOUNT_PROVIDER=lambda **_: object(),
            DECISION_SUPPORT_DYNAMIC_MONITOR=object(),
            DECISION_SUPPORT_LLM_PROVIDER=object(),
            DECISION_SUPPORT_MONITOR_CONFIG=MonitorConfig(
                enabled=True,
                paper_enabled=True,
            ),
            DECISION_SUPPORT_COMPOSITION_FACTORY=lambda **_: composition,
            DECISION_SUPPORT_JOB_REGISTRAR=job_registrar,
            DECISION_SUPPORT_PAPER_INITIAL_CASH=Decimal("1000000"),
            DECISION_SUPPORT_PAPER_FEE_SCHEDULE=PaperFeeSchedule(),
            DECISION_SUPPORT_PAPER_STRATEGY_REGISTRY_PATH=(
                tmp_path / "strategy-runs.sqlite3"
            ),
            DECISION_SUPPORT_PAPER_STRATEGY_EPOCH=1,
            DECISION_SUPPORT_PAPER_STRATEGY_ENGINE_BUILD_FINGERPRINT=(
                "sha256:" + "1" * 64
            ),
            DECISION_SUPPORT_PAPER_SCANNER_ALGORITHM_FINGERPRINT=(
                "sha256:" + "2" * 64
            ),
            DECISION_SUPPORT_PAPER_STRUCTURE_ALGORITHM_FINGERPRINT=(
                "sha256:" + "3" * 64
            ),
            DECISION_SUPPORT_PAPER_ACCOUNT_ALGORITHM_FINGERPRINT=(
                "sha256:" + "4" * 64
            ),
            DECISION_SUPPORT_PAPER_BAR_PROVIDER_FINGERPRINT=(
                "sha256:" + "5" * 64
            ),
            DECISION_SUPPORT_MANUAL_CHECK_DIR=tmp_path / "manual-checks",
        )
    )

    with pytest.raises(RuntimeError, match="store bindings are unavailable"):
        app.extensions["install_decision_support_runtime"]({})
    assert calls["jobs"] == 0
