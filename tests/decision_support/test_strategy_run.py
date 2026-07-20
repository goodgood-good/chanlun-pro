from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.strategy_run as strategy_run_module

from chanlun.decision_support.exit_evaluation_store import (
    SQLiteExitEvaluationStore,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.monitor import MonitorConfig
from chanlun.decision_support.paper_admission import SQLitePaperLedger
from chanlun.decision_support.paper_runtime import (
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
)
from chanlun.decision_support.risk import RiskPolicy
from chanlun.decision_support.strategy_run import (
    STRATEGY_RUN_STORE_ROLES,
    SQLiteStrategyRunRegistry,
    StrategyRunIdentity,
    StrategyRunIntegrityError,
    build_monitor_policy_fingerprint,
    build_review_runtime_policy_fingerprint,
    build_review_policy_fingerprint,
    build_rule_algorithm_fingerprint,
    build_universe_policy_fingerprint,
    establish_strategy_run,
    read_strategy_run_binding,
    trusted_bar_schema_fingerprint,
)
from chanlun.decision_support.universe import UniversePolicy


_CN = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 7, 15, 9, 0, tzinfo=_CN)


def _fp(character: str) -> str:
    return "sha256:" + character * 64


def _identity(**overrides: object) -> StrategyRunIdentity:
    values: dict[str, object] = {
        "rule_set_fingerprint": _fp("1"),
        "corpus_manifest_fingerprint": _fp("2"),
        "source_pdf_fingerprint": _fp("3"),
        "rule_algorithm_fingerprint": _fp("4"),
        "strategy_engine_build_fingerprint": _fp("5"),
        "scanner_algorithm_fingerprint": _fp("6"),
        "structure_algorithm_fingerprint": _fp("7"),
        "universe_policy_fingerprint": _fp("8"),
        "monitor_policy_fingerprint": _fp("9"),
        "review_provider": "openrouter",
        "review_model": "research-model-v1",
        "review_prompt_version": "chanlun-review-v3",
        "review_schema_fingerprint": _fp("a"),
        "review_runtime_policy_fingerprint": _fp("b"),
        "execution_policy_fingerprint": _fp("c"),
        "fee_schedule_fingerprint": _fp("d"),
        "initial_cash": Decimal("100000.00"),
        "account_algorithm_fingerprint": _fp("d"),
        "risk_policy_fingerprint": _fp("e"),
        "exit_policy_fingerprint": _fp("f"),
        "exit_algorithm_fingerprint": _fp("0"),
        "calendar_fingerprint": _fp("1"),
        "bar_provider_fingerprint": _fp("2"),
        "bar_schema_fingerprint": _fp("3"),
    }
    values.update(overrides)
    return StrategyRunIdentity(**values)


def _empty_store_paths(tmp_path: Path, name: str) -> dict[str, Path]:
    root = tmp_path / name
    root.mkdir()
    paths = {
        "ledger": root / "ledger.sqlite3",
        "bar": root / "bars.sqlite3",
        "risk": root / "risk.sqlite3",
        "exit": root / "exit.sqlite3",
    }
    SQLitePaperLedger(paths["ledger"], initial_cash=Decimal("100000.00"))
    SQLiteTrustedPaperBarStore(
        paths["bar"],
        calendar_fingerprint=_fp("d"),
    )
    SQLitePaperRiskState(paths["risk"], policy=RiskPolicy.conservative())
    SQLiteExitEvaluationStore(paths["exit"])
    return paths


def _uncreated_store_paths(tmp_path: Path, name: str) -> dict[str, Path]:
    root = tmp_path / name
    return {
        "ledger": root / "ledger.sqlite3",
        "bar": root / "bars.sqlite3",
        "risk": root / "risk.sqlite3",
        "exit": root / "exit.sqlite3",
    }


def _initialize_reserved_stores(
    bootstrap: object,
    store_paths: Mapping[str, Path],
) -> None:
    factories = {
        "ledger": lambda: SQLitePaperLedger(
            store_paths["ledger"],
            initial_cash=Decimal("100000.00"),
        ),
        "bar": lambda: SQLiteTrustedPaperBarStore(
            store_paths["bar"],
            calendar_fingerprint=_fp("d"),
        ),
        "risk": lambda: SQLitePaperRiskState(
            store_paths["risk"],
            policy=RiskPolicy.conservative(),
        ),
        "exit": lambda: SQLiteExitEvaluationStore(store_paths["exit"]),
    }
    for role in STRATEGY_RUN_STORE_ROLES:
        bootstrap.initialize_store(role, store_paths[role], factories[role])


def test_strategy_run_identity_is_canonical_and_every_component_is_bound() -> None:
    identity = _identity()
    reordered = StrategyRunIdentity(**dict(reversed(identity.to_payload().items())))

    assert reordered.fingerprint == identity.fingerprint
    assert identity.to_payload() == {
        "schema_version": 1,
        "rule_set_fingerprint": _fp("1"),
        "corpus_manifest_fingerprint": _fp("2"),
        "source_pdf_fingerprint": _fp("3"),
        "rule_algorithm_fingerprint": _fp("4"),
        "strategy_engine_build_fingerprint": _fp("5"),
        "scanner_algorithm_fingerprint": _fp("6"),
        "structure_algorithm_fingerprint": _fp("7"),
        "universe_policy_fingerprint": _fp("8"),
        "monitor_policy_fingerprint": _fp("9"),
        "review_provider": "openrouter",
        "review_model": "research-model-v1",
        "review_prompt_version": "chanlun-review-v3",
        "review_schema_fingerprint": _fp("a"),
        "review_runtime_policy_fingerprint": _fp("b"),
        "execution_policy_fingerprint": _fp("c"),
        "fee_schedule_fingerprint": _fp("d"),
        "initial_cash": "100000.00",
        "account_algorithm_fingerprint": _fp("d"),
        "risk_policy_fingerprint": _fp("e"),
        "exit_policy_fingerprint": _fp("f"),
        "exit_algorithm_fingerprint": _fp("0"),
        "calendar_fingerprint": _fp("1"),
        "bar_provider_fingerprint": _fp("2"),
        "bar_schema_fingerprint": _fp("3"),
    }
    for field_name in (
        "rule_set_fingerprint",
        "corpus_manifest_fingerprint",
        "source_pdf_fingerprint",
        "rule_algorithm_fingerprint",
        "strategy_engine_build_fingerprint",
        "scanner_algorithm_fingerprint",
        "structure_algorithm_fingerprint",
        "universe_policy_fingerprint",
        "monitor_policy_fingerprint",
        "review_provider",
        "review_model",
        "review_prompt_version",
        "review_schema_fingerprint",
        "review_runtime_policy_fingerprint",
        "execution_policy_fingerprint",
        "fee_schedule_fingerprint",
        "initial_cash",
        "account_algorithm_fingerprint",
        "risk_policy_fingerprint",
        "exit_policy_fingerprint",
        "exit_algorithm_fingerprint",
        "calendar_fingerprint",
        "bar_provider_fingerprint",
        "bar_schema_fingerprint",
    ):
        changed = replace(
            identity,
            **{
                field_name: (
                    Decimal("200000.00")
                    if field_name == "initial_cash"
                    else "changed"
                    if field_name
                    in {"review_provider", "review_model", "review_prompt_version"}
                    else (
                        _fp("1")
                        if getattr(identity, field_name) == _fp("0")
                        else _fp("0")
                    )
                )
            },
        )
        assert changed.fingerprint != identity.fingerprint, field_name


def test_establish_never_activates_uninitialized_missing_stores(
    tmp_path: Path,
) -> None:
    store_paths = _uncreated_store_paths(tmp_path, "missing")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_store_missing",
    ):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )

    assert all(not path.exists() for path in store_paths.values())


def test_strategy_run_identity_rejects_missing_or_unstable_components() -> None:
    with pytest.raises(ValueError, match="scanner_algorithm_fingerprint"):
        _identity(scanner_algorithm_fingerprint="")
    with pytest.raises(ValueError, match="review_model"):
        _identity(review_model="")
    with pytest.raises(ValueError, match="initial_cash"):
        _identity(initial_cash=Decimal("NaN"))


def test_component_fingerprints_bind_rule_review_and_bar_contracts() -> None:
    first_rules = SimpleNamespace(
        fingerprint=_fp("1"),
        cards=(
            SimpleNamespace(
                rule_id="rule-a",
                version=1,
                algorithm_version="algorithm-a/1",
                fingerprint=_fp("2"),
            ),
        ),
    )
    changed_rules = SimpleNamespace(
        fingerprint=_fp("1"),
        cards=(
            SimpleNamespace(
                rule_id="rule-a",
                version=1,
                algorithm_version="algorithm-a/2",
                fingerprint=_fp("3"),
            ),
        ),
    )
    review = build_review_policy_fingerprint(
        provider="openrouter",
        model="research-model-v1",
        prompt_version="chanlun-review-v3",
        response_schema={"type": "object", "required": ["verdict"]},
    )
    review_runtime = build_review_runtime_policy_fingerprint(
        max_evidence_units=8,
        timeout=(10, 180),
    )

    assert build_rule_algorithm_fingerprint(first_rules) != (
        build_rule_algorithm_fingerprint(changed_rules)
    )
    assert review != build_review_policy_fingerprint(
        provider="openrouter",
        model="research-model-v2",
        prompt_version="chanlun-review-v3",
        response_schema={"type": "object", "required": ["verdict"]},
    )
    assert review_runtime != build_review_runtime_policy_fingerprint(
        max_evidence_units=9,
        timeout=(10, 180),
    )
    assert review_runtime != build_review_runtime_policy_fingerprint(
        max_evidence_units=8,
        timeout=(11, 180),
    )
    assert trusted_bar_schema_fingerprint().startswith("sha256:")


def test_trusted_bar_schema_declares_committed_preflight_replay_watermark(
    monkeypatch,
) -> None:
    captured: list[dict[str, object]] = []

    def capture(payload):
        captured.append(payload)
        return _fp("f")

    monkeypatch.setattr(strategy_run_module, "sha256_json", capture)

    assert strategy_run_module.trusted_bar_schema_fingerprint() == _fp("f")
    assert len(captured) == 1
    schema = captured[0]
    assert schema["schema_version"] == 9
    assert schema["observation_schema"] == {
        "tables": (
            "trusted_signal_observation_log_state",
            "trusted_signal_observation_cycle",
            "trusted_signal_first_observation",
            "trusted_signal_segment_observation",
        ),
        "states": (
            "trusted_first_seen",
            "baseline_not_fresh",
            "quarantined_unknown",
        ),
        "scope": "required_codes_sell_signals_only",
        "authority": "current_required_segment_only",
        "quarantine": "sticky_until_new_segment_or_explicit_rebaseline",
        "attempt_generation": "monotonic_per_cycle_start",
        "retry_ambiguity": "generation_gt_1_quarantines_new_signals",
        "closed_at_lookup": "indexed_exact_closed_at",
        "atomic_commit": (
            "manifest+global_first_seen+segment_state+cycle+attempt"
        ),
    }
    assert schema["calendar_preflight_recovery_schema"] == {
        "tables": (
            "trusted_paper_bar_calendar_preflight",
            "trusted_paper_bar_calendar_preflight_resolution",
            "trusted_paper_bar_calendar_preflight_watermark",
        ),
        "watermark": "append_only_committed_cycle_observed_at",
        "atomic_commit": "watermark+cycle+attempt",
        "replay_cleanup": "committed_watermark_only",
    }
    assert schema["segment_ledger_schema"] == {
        "tables": (
            "trusted_paper_bar_segment",
            "trusted_paper_bar_segment_member",
        ),
        "cycle_payload_schema_version": 3,
        "bar_binding": "bar_id+payload_sha256+segment_id",
        "legacy_replay": "v2_exact_checksum_only",
        "validation": "full_ledger_at_every_trust_boundary",
        "tail_lookup": "indexed_segment_id+closed_at_desc",
    }
    assert schema["exit_manifest_schema"] == {
        "tables": (
            "trusted_paper_exit_manifest_log_state",
            "trusted_paper_exit_manifest",
        ),
        "commitment_fields": (
            "snapshot_id",
            "payload_fingerprint",
            "entry_event_id",
            "evaluation_cycle_id",
            "evaluated_at",
        ),
        "cardinality": "exactly_one_row_per_completed_cycle_including_empty",
        "atomic_commit": "exit_manifest+signal_observation+cycle+attempt",
        "publication": "exact_snapshot_commitment_membership",
        "replay": "exact_commitment_tuple_only",
        "history": "append_only_sha256_chain",
        "external_anchor": "content_addressed_manifest_history_head",
        "hot_append_validation": (
            "cached_full_prefix+exact_log_state+tail_row+tail_anchor"
        ),
        "full_validation_boundaries": (
            "startup+bind+health+read+bulk+cache_rebase"
        ),
    }


def test_monitor_and_universe_fingerprints_bind_runtime_limits() -> None:
    universe_policy = UniversePolicy.a_share_short_term()
    universe_fingerprint = build_universe_policy_fingerprint(universe_policy)
    monitor_config = MonitorConfig(
        enabled=True,
        scan_interval_seconds=30,
        review_workers=1,
        review_queue_limit=20,
        max_llm_reviews_per_day=20,
        paper_enabled=True,
        auto_order_enabled=False,
    )
    base = build_monitor_policy_fingerprint(
        config=monitor_config,
        max_completed_bars=2_000,
        max_market_age_seconds=300,
        processed_bar_limit=2_048,
        universe_policy_fingerprint=universe_fingerprint,
    )

    assert universe_fingerprint != build_universe_policy_fingerprint(
        replace(universe_policy, min_listed_days=61)
    )
    assert base != build_monitor_policy_fingerprint(
        config=replace(monitor_config, max_llm_reviews_per_day=21),
        max_completed_bars=2_000,
        max_market_age_seconds=300,
        processed_bar_limit=2_048,
        universe_policy_fingerprint=universe_fingerprint,
    )
    assert base != build_monitor_policy_fingerprint(
        config=monitor_config,
        max_completed_bars=2_001,
        max_market_age_seconds=300,
        processed_bar_limit=2_048,
        universe_policy_fingerprint=universe_fingerprint,
    )
    assert base != build_monitor_policy_fingerprint(
        config=monitor_config,
        max_completed_bars=2_000,
        max_market_age_seconds=301,
        processed_bar_limit=2_048,
        universe_policy_fingerprint=universe_fingerprint,
    )
    assert base != build_monitor_policy_fingerprint(
        config=monitor_config,
        max_completed_bars=2_000,
        max_market_age_seconds=300,
        processed_bar_limit=2_049,
        universe_policy_fingerprint=universe_fingerprint,
    )
    with pytest.raises(ValueError, match="max_completed_bars"):
        build_monitor_policy_fingerprint(
            config=monitor_config,
            max_completed_bars=0,
            max_market_age_seconds=300,
            processed_bar_limit=2_048,
            universe_policy_fingerprint=universe_fingerprint,
        )


def test_review_mode_changes_strategy_config_fingerprint() -> None:
    external_config = MonitorConfig(review_mode="external_review")
    offline_config = replace(external_config, review_mode="offline_abstain")
    kwargs = {
        "max_completed_bars": 2_000,
        "max_market_age_seconds": 300,
        "processed_bar_limit": 2_048,
        "universe_policy_fingerprint": build_universe_policy_fingerprint(
            UniversePolicy.a_share_short_term()
        ),
    }

    external = build_monitor_policy_fingerprint(
        config=external_config,
        **kwargs,
    )
    offline = build_monitor_policy_fingerprint(
        config=offline_config,
        **kwargs,
    )

    assert replace(offline_config, review_mode="external_review") == external_config
    assert external != offline


def test_empty_stores_bind_once_and_same_fingerprint_restart_resumes_epoch(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "epoch-1")

    first = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )
    restarted = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )

    assert restarted == first
    assert first.epoch == 1
    assert first.strategy_run_fingerprint == _identity().fingerprint
    assert first.evidence_scope == "current_epoch_only"
    assert first.status_payload()["switch_capability"] == (
        "cold_stop_drain_required"
    )
    assert first.status_payload()["rolling_switch_supported"] is False
    assert set(first.store_bindings) == set(STRATEGY_RUN_STORE_ROLES)
    for role, path in store_paths.items():
        binding = read_strategy_run_binding(path)
        assert binding is not None
        assert binding.store_role == role
        assert binding.run_id == first.run_id
        assert binding.epoch == 1


def test_nonempty_unbound_store_is_legacy_and_is_never_adopted(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "legacy")
    with sqlite3.connect(store_paths["ledger"]) as connection:
        connection.execute(
            """
            INSERT INTO paper_buying_power_reservation (
                event_id, required_cash, created_at
            ) VALUES (?, ?, ?)
            """,
            ("legacy-event", "100.00", _NOW.isoformat()),
        )

    with pytest.raises(StrategyRunIntegrityError, match="legacy_unbound"):
        establish_strategy_run(
            registry_path,
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )

    assert SQLiteStrategyRunRegistry(registry_path).list_epochs() == ()
    assert read_strategy_run_binding(store_paths["ledger"]) is None


def test_orphan_signal_observation_log_state_is_never_adopted(
    tmp_path: Path,
) -> None:
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    orphan_state = {
        "schema_version": 1,
        "event_count": 1,
        "max_sequence": 1,
        "history_head_sha256": _fp("a"),
    }
    with sqlite3.connect(store_paths["bar"]) as connection:
        connection.execute(
            """
            UPDATE trusted_signal_observation_log_state
            SET event_count = ?, max_sequence = ?,
                history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                orphan_state["event_count"],
                orphan_state["max_sequence"],
                orphan_state["history_head_sha256"],
                sha256_json(orphan_state),
            ),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_store_schema_invalid:bar",
    ):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )


def test_orphan_exit_manifest_log_state_is_never_adopted(
    tmp_path: Path,
) -> None:
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    orphan_state = {
        "schema_version": 1,
        "event_count": 1,
        "max_sequence": 1,
        "history_head_sha256": _fp("b"),
    }
    with sqlite3.connect(store_paths["bar"]) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_exit_manifest_log_state
            SET event_count = ?, max_sequence = ?,
                history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                orphan_state["event_count"],
                orphan_state["max_sequence"],
                orphan_state["history_head_sha256"],
                sha256_json(orphan_state),
            ),
        )

    with pytest.raises(StrategyRunIntegrityError, match="legacy_unbound:bar"):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )


def test_orphan_calendar_preflight_watermark_is_never_adopted(
    tmp_path: Path,
) -> None:
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    with sqlite3.connect(store_paths["bar"]) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "trusted_paper_bar_calendar_preflight_watermark" in tables
        connection.execute(
            """
            INSERT INTO trusted_paper_bar_calendar_preflight_watermark (
                closed_at, observed_at, cycle_payload_sha256, payload_sha256
            ) VALUES (?, ?, ?, ?)
            """,
            (
                _NOW.isoformat(),
                (_NOW + timedelta(seconds=1)).isoformat(),
                _fp("a"),
                _fp("b"),
            ),
        )

    with pytest.raises(StrategyRunIntegrityError, match="legacy_unbound:bar"):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )


def test_orphan_signal_segment_authority_is_never_adopted(
    tmp_path: Path,
) -> None:
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    with sqlite3.connect(store_paths["bar"]) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "trusted_signal_segment_observation" in tables
        connection.execute(
            """
            INSERT INTO trusted_signal_segment_observation (
                run_id, epoch, strategy_run_fingerprint,
                identity_sha256, store_instance_id, segment_id,
                code, signal_fingerprint, first_observed_at,
                first_cycle_closed_at, observation_state,
                observation_sequence, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan-run",
                1,
                _fp("1"),
                _fp("2"),
                "orphan-store",
                "orphan-segment",
                "SH.600001",
                _fp("3"),
                None,
                _NOW.isoformat(),
                "quarantined_unknown",
                1,
                "{}",
                _fp("4"),
            ),
        )

    with pytest.raises(StrategyRunIntegrityError, match="legacy_unbound:bar"):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )


def test_orphan_exit_manifest_is_never_adopted(tmp_path: Path) -> None:
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    with sqlite3.connect(store_paths["bar"]) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "trusted_paper_exit_manifest" in tables
        connection.execute(
            """
            INSERT INTO trusted_paper_exit_manifest (
                closed_at, manifest_sequence, previous_manifest_sha256,
                payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_NOW.isoformat(), 1, None, "{}", _fp("4")),
        )

    with pytest.raises(StrategyRunIntegrityError, match="legacy_unbound:bar"):
        establish_strategy_run(
            tmp_path / "strategy-runs.sqlite3",
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW,
        )


def test_changed_identity_requires_next_epoch_and_fresh_stores(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    first = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    changed = _identity(rule_set_fingerprint=_fp("0"))

    with pytest.raises(StrategyRunIntegrityError, match="fingerprint_mismatch"):
        establish_strategy_run(
            registry_path,
            requested_epoch=1,
            identity=changed,
            store_paths=epoch_1_paths,
            now=_NOW,
        )

    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    second = establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=changed,
        store_paths=epoch_2_paths,
        now=_NOW,
    )
    epoch_3_paths = _empty_store_paths(tmp_path, "epoch-3")
    third = establish_strategy_run(
        registry_path,
        requested_epoch=3,
        identity=_identity(),
        store_paths=epoch_3_paths,
        now=_NOW,
    )

    assert second.epoch == 2
    assert third.epoch == 3
    assert third.run_id != first.run_id
    assert third.strategy_run_fingerprint == first.strategy_run_fingerprint
    assert [item.status for item in SQLiteStrategyRunRegistry(registry_path).list_epochs()] == [
        "closed",
        "closed",
        "active",
    ]


def test_store_swap_or_old_binding_cannot_join_a_new_epoch(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )

    swapped = dict(epoch_1_paths)
    swapped["ledger"], swapped["bar"] = swapped["bar"], swapped["ledger"]
    with pytest.raises(StrategyRunIntegrityError, match="store_(role|binding)_mismatch"):
        establish_strategy_run(
            registry_path,
            requested_epoch=1,
            identity=_identity(),
            store_paths=swapped,
            now=_NOW,
        )

    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    epoch_2_paths["exit"] = epoch_1_paths["exit"]
    with pytest.raises(StrategyRunIntegrityError, match="store_binding_mismatch"):
        establish_strategy_run(
            registry_path,
            requested_epoch=2,
            identity=_identity(exit_policy_fingerprint=_fp("0")),
            store_paths=epoch_2_paths,
            now=_NOW,
        )


def test_new_epoch_is_blocked_while_previous_ledger_has_reservation(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    with sqlite3.connect(epoch_1_paths["ledger"]) as connection:
        connection.execute(
            """
            INSERT INTO paper_buying_power_reservation (
                event_id, required_cash, created_at
            ) VALUES (?, ?, ?)
            """,
            ("pending-event", "100.00", _NOW.isoformat()),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="previous_epoch_not_flat",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=2,
            identity=_identity(rule_set_fingerprint=_fp("0")),
            store_paths=_empty_store_paths(tmp_path, "epoch-2"),
            now=_NOW,
        )


def test_exact_initializing_epoch_resumes_on_restart(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    registry = SQLiteStrategyRunRegistry(registry_path)
    registry.prepare_epoch(
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )

    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )

    assert active.epoch == 1
    assert all(
        read_strategy_run_binding(store_paths[role]) == active.store_bindings[role]
        for role in STRATEGY_RUN_STORE_ROLES
    )


def test_bootstrap_reservation_rejects_wrong_identity_before_store_factory(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    candidate_paths = _uncreated_store_paths(tmp_path, "wrong-candidate")
    factory_calls = 0

    def forbidden_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("store factory must not be entered")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_fingerprint_mismatch",
    ):
        with strategy_run_module.reserve_strategy_run_bootstrap(
            registry_path,
            requested_epoch=1,
            identity=_identity(rule_set_fingerprint=_fp("0")),
            store_paths=candidate_paths,
            now=_NOW + timedelta(seconds=1),
        ) as bootstrap:
            bootstrap.initialize_store(
                "ledger",
                candidate_paths["ledger"],
                forbidden_factory,
            )

    assert factory_calls == 0
    assert all(not path.exists() for path in candidate_paths.values())


@pytest.mark.parametrize(
    ("requested_epoch", "changed_role"),
    ((2, None), (1, "bar")),
    ids=("wrong-epoch", "wrong-store-path"),
)
def test_bootstrap_initializing_claim_rejects_wrong_epoch_or_path_before_factory(
    tmp_path: Path,
    requested_epoch: int,
    changed_role: str | None,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ):
        pass

    attempted_paths = dict(store_paths)
    changed_path = None
    if changed_role is not None:
        changed_path = tmp_path / "wrong-bar.sqlite3"
        attempted_paths[changed_role] = changed_path
    factory_calls = 0

    def forbidden_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("mismatched bootstrap factory must not run")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_partial_binding",
    ):
        with strategy_run_module.reserve_strategy_run_bootstrap(
            registry_path,
            requested_epoch=requested_epoch,
            identity=_identity(),
            store_paths=attempted_paths,
            now=_NOW + timedelta(seconds=1),
        ) as bootstrap:
            bootstrap.initialize_store(
                "ledger",
                attempted_paths["ledger"],
                forbidden_factory,
            )

    assert factory_calls == 0
    if changed_path is not None:
        assert not changed_path.exists()


def test_bootstrap_reservation_binds_store_before_factory(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    observed_binding = None

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ) as bootstrap:

        def ledger_factory() -> SQLitePaperLedger:
            nonlocal observed_binding
            observed_binding = read_strategy_run_binding(store_paths["ledger"])
            return SQLitePaperLedger(
                store_paths["ledger"],
                initial_cash=Decimal("100000.00"),
            )

        ledger = bootstrap.initialize_store(
            "ledger",
            store_paths["ledger"],
            ledger_factory,
        )

        assert ledger.path == store_paths["ledger"].absolute()
        assert observed_binding == bootstrap.store_bindings["ledger"]
        assert SQLiteStrategyRunRegistry(registry_path).list_epochs()[0].status == (
            "initializing"
        )


def test_bootstrap_factory_without_role_schema_is_not_initialized(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ) as bootstrap:
        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_bootstrap_store_schema_invalid:ledger",
        ):
            bootstrap.initialize_store(
                "ledger",
                store_paths["ledger"],
                lambda: SimpleNamespace(path=store_paths["ledger"]),
            )
        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_bootstrap_store_initialization_incomplete",
        ):
            bootstrap.activate(now=_NOW + timedelta(seconds=1))


def test_bootstrap_activation_requires_all_store_factories(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ) as bootstrap:
        bootstrap.initialize_store(
            "ledger",
            store_paths["ledger"],
            lambda: SQLitePaperLedger(
                store_paths["ledger"],
                initial_cash=Decimal("100000.00"),
            ),
        )

        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_bootstrap_store_initialization_incomplete",
        ):
            bootstrap.activate(now=_NOW + timedelta(seconds=1))

    assert SQLiteStrategyRunRegistry(registry_path).list_epochs()[0].status == (
        "initializing"
    )


def test_bootstrap_activation_requires_then_activates_exact_store_set(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ) as bootstrap:
        _initialize_reserved_stores(bootstrap, store_paths)
        active = bootstrap.activate(now=_NOW + timedelta(seconds=1))

    assert active.epoch == 1
    assert active.strategy_run_fingerprint == _identity().fingerprint
    assert SQLiteStrategyRunRegistry(registry_path).list_epochs()[0].status == (
        "active"
    )
    assert all(
        read_strategy_run_binding(store_paths[role]) == active.store_bindings[role]
        for role in STRATEGY_RUN_STORE_ROLES
    )


def test_bootstrap_exact_restart_completes_partial_store_bindings(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    registry = SQLiteStrategyRunRegistry(registry_path)
    record = registry.prepare_epoch(
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
        allow_uninitialized_stores=True,
    )
    registered = registry._store_rows(record.run_id)
    strategy_run_module._bind_store(
        store_paths["ledger"],
        role="ledger",
        record=record,
        store_instance_id=registered["ledger"][1],
    )
    assert read_strategy_run_binding(store_paths["ledger"]) is not None
    assert all(
        not store_paths[role].exists()
        for role in STRATEGY_RUN_STORE_ROLES
        if role != "ledger"
    )

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW + timedelta(seconds=1),
    ) as bootstrap:
        assert all(
            read_strategy_run_binding(store_paths[role])
            == bootstrap.store_bindings[role]
            for role in STRATEGY_RUN_STORE_ROLES
        )
        _initialize_reserved_stores(bootstrap, store_paths)
        active = bootstrap.activate(now=_NOW + timedelta(seconds=2))

    assert active.epoch == 1


def test_bootstrap_concurrent_claim_never_enters_store_factory(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    first_entered = Event()
    release_first = Event()
    first_errors: list[BaseException] = []

    def hold_first_claim() -> None:
        try:
            with strategy_run_module.reserve_strategy_run_bootstrap(
                registry_path,
                requested_epoch=1,
                identity=_identity(),
                store_paths=store_paths,
                now=_NOW,
                lock_timeout=2.0,
            ):
                first_entered.set()
                if not release_first.wait(5):
                    raise RuntimeError("bootstrap test synchronization timed out")
        except BaseException as exc:  # pragma: no cover - asserted below
            first_errors.append(exc)

    thread = Thread(target=hold_first_claim)
    thread.start()
    assert first_entered.wait(5)
    factory_calls = 0

    def forbidden_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("concurrent store factory must not run")

    try:
        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_bootstrap_claim_unavailable",
        ):
            with strategy_run_module.reserve_strategy_run_bootstrap(
                registry_path,
                requested_epoch=1,
                identity=_identity(),
                store_paths=store_paths,
                now=_NOW + timedelta(seconds=1),
                lock_timeout=0.1,
            ) as bootstrap:
                bootstrap.initialize_store(
                    "ledger",
                    store_paths["ledger"],
                    forbidden_factory,
                )
    finally:
        release_first.set()
        thread.join(5)

    assert not thread.is_alive()
    assert first_errors == []
    assert factory_calls == 0


def test_bootstrap_cross_process_lock_releases_after_holder_crash(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    lock_targets = (registry_path, *store_paths.values())
    ready_path = tmp_path / "holder-ready"
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    source_path = str(project_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else source_path + os.pathsep + existing_pythonpath
    )
    child_code = (
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "from chanlun.decision_support.strategy_run import "
        "_exclusive_bootstrap_lock_set\n"
        "paths = tuple(Path(value) for value in sys.argv[1:-1])\n"
        "ready = Path(sys.argv[-1])\n"
        "with _exclusive_bootstrap_lock_set(paths, timeout=2.0):\n"
        "    ready.write_text('ready', encoding='utf-8')\n"
        "    while True:\n"
        "        time.sleep(1)\n"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            *(str(path) for path in lock_targets),
            str(ready_path),
        ],
        cwd=project_root,
        env=environment,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists() and time.monotonic() < deadline:
            if holder.poll() is not None:
                pytest.fail(
                    "bootstrap lock holder exited early: "
                    + (holder.stderr.read() if holder.stderr else "")
                )
            time.sleep(0.01)
        assert ready_path.read_text(encoding="utf-8") == "ready"

        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_bootstrap_claim_unavailable",
        ):
            with strategy_run_module.reserve_strategy_run_bootstrap(
                registry_path,
                requested_epoch=1,
                identity=_identity(),
                store_paths=store_paths,
                now=_NOW,
                lock_timeout=0.1,
            ):
                raise AssertionError("live cross-process claim must not enter")
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)

    with strategy_run_module.reserve_strategy_run_bootstrap(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW + timedelta(seconds=1),
        lock_timeout=2.0,
    ) as bootstrap:
        _initialize_reserved_stores(bootstrap, store_paths)
        active = bootstrap.activate(now=_NOW + timedelta(seconds=2))

    assert active.epoch == 1


def test_bootstrap_role_path_mismatch_never_enters_store_factory(
    tmp_path: Path,
) -> None:
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    factory_calls = 0

    def forbidden_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("mismatched store factory must not run")

    with strategy_run_module.reserve_strategy_run_bootstrap(
        tmp_path / "strategy-runs.sqlite3",
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ) as bootstrap:
        with pytest.raises(
            StrategyRunIntegrityError,
            match="strategy_run_store_binding_mismatch",
        ):
            bootstrap.initialize_store(
                "ledger",
                store_paths["bar"],
                forbidden_factory,
            )

    assert factory_calls == 0


def test_bootstrap_foreign_registry_cannot_adopt_reserved_stores(
    tmp_path: Path,
) -> None:
    store_paths = _uncreated_store_paths(tmp_path, "epoch-1")
    first_registry = tmp_path / "strategy-runs-a.sqlite3"
    second_registry = tmp_path / "strategy-runs-b.sqlite3"
    with strategy_run_module.reserve_strategy_run_bootstrap(
        first_registry,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    ):
        pass
    factory_calls = 0

    def forbidden_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("foreign store factory must not run")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_store_binding_mismatch",
    ):
        with strategy_run_module.reserve_strategy_run_bootstrap(
            second_registry,
            requested_epoch=1,
            identity=_identity(),
            store_paths=store_paths,
            now=_NOW + timedelta(seconds=1),
        ) as bootstrap:
            bootstrap.initialize_store(
                "ledger",
                store_paths["ledger"],
                forbidden_factory,
            )

    assert factory_calls == 0
    assert SQLiteStrategyRunRegistry(second_registry).list_epochs() == ()


def test_activation_rechecks_previous_ledger_after_prepare(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    registry = SQLiteStrategyRunRegistry(registry_path)
    draft = registry.prepare_epoch(
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=epoch_2_paths,
        now=_NOW,
    )
    with sqlite3.connect(epoch_1_paths["ledger"]) as connection:
        connection.execute(
            """
            INSERT INTO paper_buying_power_reservation (
                event_id, required_cash, created_at
            ) VALUES (?, ?, ?)
            """,
            ("late-reservation", "100.00", _NOW.isoformat()),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="previous_epoch_not_flat",
    ):
        registry.bind_and_activate(
            draft,
            store_paths=epoch_2_paths,
            now=_NOW,
        )

    assert [item.status for item in registry.list_epochs()] == [
        "active",
        "initializing",
    ]


def test_activation_rechecks_previous_ledger_inside_registry_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    registry = SQLiteStrategyRunRegistry(registry_path)
    draft = registry.prepare_epoch(
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=epoch_2_paths,
        now=_NOW,
    )
    original = registry._validate_predecessor
    validation_count = 0

    def inject_after_last_prelock_validation(record: object) -> None:
        nonlocal validation_count
        original(record)  # type: ignore[arg-type]
        validation_count += 1
        if validation_count == 2:
            with sqlite3.connect(epoch_1_paths["ledger"]) as connection:
                connection.execute(
                    """
                    INSERT INTO paper_buying_power_reservation (
                        event_id, required_cash, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    ("raced-reservation", "100.00", _NOW.isoformat()),
                )

    monkeypatch.setattr(
        registry,
        "_validate_predecessor",
        inject_after_last_prelock_validation,
    )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="previous_epoch_not_flat",
    ):
        registry.bind_and_activate(
            draft,
            store_paths=epoch_2_paths,
            now=_NOW,
        )

    assert validation_count == 2
    assert [item.status for item in registry.list_epochs()] == [
        "active",
        "initializing",
    ]


def test_activation_writer_lock_fences_competing_old_epoch_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    registry = SQLiteStrategyRunRegistry(registry_path)
    draft = registry.prepare_epoch(
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=epoch_2_paths,
        now=_NOW,
    )
    activation_inside_lock = Event()
    allow_activation = Event()
    acquire_started = Event()
    acquire_finished = Event()
    activation_results: list[object] = []
    acquire_results: list[BaseException | object] = []
    original = registry._validate_locked_predecessor_stores_and_flat

    def pause_inside_writer_lock(
        active: object,
        store_rows: list[tuple[object, ...]],
    ) -> None:
        original(active, store_rows)  # type: ignore[arg-type]
        activation_inside_lock.set()
        if not allow_activation.wait(5):
            raise RuntimeError("activation test synchronization timed out")

    monkeypatch.setattr(
        registry,
        "_validate_locked_predecessor_stores_and_flat",
        pause_inside_writer_lock,
    )

    def activate() -> None:
        try:
            activation_results.append(
                registry.bind_and_activate(
                    draft,
                    store_paths=epoch_2_paths,
                    now=_NOW + timedelta(seconds=1),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            activation_results.append(exc)

    def acquire_old_epoch() -> None:
        acquire_started.set()
        try:
            acquire_results.append(
                active_one.acquire_mutation_lease(
                    "raced-old-epoch.commit",
                    now=_NOW + timedelta(seconds=2),
                )
            )
        except BaseException as exc:
            acquire_results.append(exc)
        finally:
            acquire_finished.set()

    activation_thread = Thread(target=activate)
    activation_thread.start()
    assert activation_inside_lock.wait(5)
    acquire_thread = Thread(target=acquire_old_epoch)
    acquire_thread.start()
    assert acquire_started.wait(5)
    assert not acquire_finished.wait(0.1)
    allow_activation.set()
    activation_thread.join(5)
    acquire_thread.join(5)

    assert not activation_thread.is_alive()
    assert not acquire_thread.is_alive()
    assert len(activation_results) == 1
    assert not isinstance(activation_results[0], BaseException)
    assert len(acquire_results) == 1
    assert isinstance(acquire_results[0], StrategyRunIntegrityError)
    assert str(acquire_results[0]) == "strategy_run_not_active"


def test_activation_rechecks_previous_store_bindings_after_prepare(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    registry = SQLiteStrategyRunRegistry(registry_path)
    draft = registry.prepare_epoch(
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=epoch_2_paths,
        now=_NOW,
    )
    with sqlite3.connect(epoch_1_paths["bar"]) as connection:
        connection.execute(
            """
            UPDATE paper_strategy_run_binding
            SET binding_sha256 = ? WHERE singleton_id = 1
            """,
            (_fp("0"),),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="binding_(checksum_mismatch|mismatch)",
    ):
        registry.bind_and_activate(
            draft,
            store_paths=epoch_2_paths,
            now=_NOW,
        )

    assert [item.status for item in registry.list_epochs()] == [
        "active",
        "initializing",
    ]


def test_nonempty_registry_without_active_epoch_is_corrupt(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute(
            """
            UPDATE paper_strategy_run_epoch
            SET status = 'closed', ended_at = ?
            WHERE epoch = 1
            """,
            (_NOW.isoformat(),),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="no_active_epoch",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=2,
            identity=_identity(rule_set_fingerprint=_fp("0")),
            store_paths=_empty_store_paths(tmp_path, "epoch-2"),
            now=_NOW,
        )


def test_status_revalidates_registry_and_detects_exact_store_file_replacement(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )
    replacement = tmp_path / "replacement-ledger.sqlite3"
    shutil.copy2(store_paths["ledger"], replacement)
    replacement.replace(store_paths["ledger"])

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_store_file_replaced",
    ):
        active.status_payload()


def test_status_revalidates_active_registry_epoch(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute(
            "UPDATE paper_strategy_run_epoch SET status = 'closed', "
            "ended_at = ? "
            "WHERE run_id = ?",
            ((_NOW + timedelta(seconds=1)).isoformat(), active.run_id),
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_not_active",
    ):
        active.status_payload()


def test_deleted_closed_epoch_invalidates_active_history_chain(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    epoch_1_paths = _empty_store_paths(tmp_path, "epoch-1")
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=epoch_1_paths,
        now=_NOW,
    )
    epoch_2_paths = _empty_store_paths(tmp_path, "epoch-2")
    establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=epoch_2_paths,
        now=_NOW + timedelta(seconds=1),
    )
    epoch_3_paths = _empty_store_paths(tmp_path, "epoch-3")
    active = establish_strategy_run(
        registry_path,
        requested_epoch=3,
        identity=_identity(),
        store_paths=epoch_3_paths,
        now=_NOW + timedelta(seconds=2),
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute(
            "DELETE FROM paper_strategy_run_store WHERE run_id = "
            "(SELECT run_id FROM paper_strategy_run_epoch WHERE epoch = 1)"
        )
        connection.execute(
            "DELETE FROM paper_strategy_run_epoch WHERE epoch = 1"
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_history_invalid",
    ):
        active.status_payload()
    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_history_invalid",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=4,
            identity=_identity(rule_set_fingerprint=_fp("f")),
            store_paths=_empty_store_paths(tmp_path, "epoch-4"),
            now=_NOW + timedelta(seconds=3),
        )


def test_deleted_closed_epoch_store_row_invalidates_history(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    active = establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=_identity(rule_set_fingerprint=_fp("0")),
        store_paths=_empty_store_paths(tmp_path, "epoch-2"),
        now=_NOW + timedelta(seconds=1),
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute(
            "DELETE FROM paper_strategy_run_store "
            "WHERE run_id = ("
            "SELECT run_id FROM paper_strategy_run_epoch WHERE epoch = 1"
            ") AND store_role = 'exit'"
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_history_invalid",
    ):
        active.status_payload()


def test_registry_anchor_prevents_full_history_rollback(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute("DELETE FROM paper_strategy_run_store")
        connection.execute("DELETE FROM paper_strategy_run_epoch")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_history_invalid",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=1,
            identity=_identity(rule_set_fingerprint=_fp("0")),
            store_paths=_empty_store_paths(tmp_path, "replacement-epoch-1"),
            now=_NOW + timedelta(seconds=1),
        )


def test_mutation_lease_blocks_epoch_switch_until_release_then_retry_succeeds(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    paths_one = _empty_store_paths(tmp_path, "epoch-1")
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=paths_one,
        now=_NOW,
    )
    lease = active_one.acquire_mutation_lease(
        "test.commit",
        now=_NOW + timedelta(seconds=1),
    )
    paths_two = _empty_store_paths(tmp_path, "epoch-2")
    identity_two = _identity(strategy_engine_build_fingerprint=_fp("a"))

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_inflight_not_drained",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=2,
            identity=identity_two,
            store_paths=paths_two,
            now=_NOW + timedelta(seconds=2),
        )

    active_one.release_mutation_lease(
        lease,
        now=_NOW + timedelta(seconds=3),
    )
    active_two = establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=identity_two,
        store_paths=paths_two,
        now=_NOW + timedelta(seconds=4),
    )

    assert active_two.epoch == 2
    assert active_two.run_id != active_one.run_id
    assert active_two.status_payload()["mutations_drained"] is True
    assert active_two.status_payload()["inflight_mutation_count"] == 0


def test_epoch_switch_winner_prevents_old_run_from_acquiring_mutation_lease(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    active_two = establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=_identity(strategy_engine_build_fingerprint=_fp("a")),
        store_paths=_empty_store_paths(tmp_path, "epoch-2"),
        now=_NOW + timedelta(seconds=1),
    )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_not_active",
    ):
        active_one.acquire_mutation_lease(
            "stale.commit",
            now=_NOW + timedelta(seconds=2),
        )

    assert active_two.status_payload()["mutations_drained"] is True


def test_crash_lease_survives_restart_and_permanently_blocks_switch(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    paths_one = _empty_store_paths(tmp_path, "epoch-1")
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=paths_one,
        now=_NOW,
    )
    crashed = active_one.acquire_mutation_lease(
        "crashed.commit",
        now=_NOW + timedelta(seconds=1),
    )

    restarted = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=paths_one,
        now=_NOW + timedelta(seconds=2),
    )
    diagnostics = restarted.mutation_lease_diagnostics()

    assert diagnostics["protocol"] == "durable_registry_v1"
    assert diagnostics["active_count"] == 1
    assert diagnostics["leases"] == [crashed.to_payload()]
    assert restarted.status_payload()["mutations_drained"] is False
    assert restarted.status_payload()["inflight_mutation_count"] == 1
    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_inflight_not_drained",
    ):
        establish_strategy_run(
            registry_path,
            requested_epoch=2,
            identity=_identity(strategy_engine_build_fingerprint=_fp("a")),
            store_paths=_empty_store_paths(tmp_path, "epoch-2"),
            now=_NOW + timedelta(seconds=3),
        )


def test_mutation_lease_cache_rejects_consistent_sequence_rollback(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    before_lease = tmp_path / "strategy-runs-before-lease.sqlite3"
    shutil.copyfile(registry_path, before_lease)
    active.acquire_mutation_lease(
        "crashed.commit",
        now=_NOW + timedelta(seconds=1),
    )

    shutil.copyfile(before_lease, registry_path)

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_history_invalid",
    ):
        active.status_payload()


def test_mutation_lease_cache_full_scans_same_sequence_file_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    with active.mutation_lease("seed.commit"):
        pass
    active.status_payload()
    full_validation_count = 0
    original = SQLiteStrategyRunRegistry._validate_mutation_lease_history

    def counted_validation(*args: object, **kwargs: object) -> object:
        nonlocal full_validation_count
        full_validation_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        SQLiteStrategyRunRegistry,
        "_validate_mutation_lease_history",
        staticmethod(counted_validation),
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute("PRAGMA user_version = 7")
    stat = registry_path.stat()
    os.utime(
        registry_path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
    )

    assert active.status_payload()["mutations_drained"] is True
    assert full_validation_count == 1


def test_mutation_lease_audit_row_deletion_is_detected(tmp_path: Path) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    active.acquire_mutation_lease(
        "crashed.commit",
        now=_NOW + timedelta(seconds=1),
    )
    with sqlite3.connect(registry_path) as connection:
        connection.execute("DELETE FROM paper_strategy_run_mutation_lease")

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_history_invalid",
    ):
        active.status_payload()


def test_mutation_lease_noop_guard_replacement_is_detected(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    with active.mutation_lease("seed.commit"):
        pass
    with sqlite3.connect(registry_path) as connection:
        connection.executescript(
            """
            DROP TRIGGER paper_strategy_run_mutation_lease_delete_guard;
            DROP TRIGGER paper_strategy_run_mutation_lease_update_guard;
            CREATE TRIGGER paper_strategy_run_mutation_lease_delete_guard
            AFTER DELETE ON paper_strategy_run_mutation_lease
            BEGIN SELECT 1; END;
            CREATE TRIGGER paper_strategy_run_mutation_lease_update_guard
            AFTER UPDATE ON paper_strategy_run_mutation_lease
            BEGIN SELECT 1; END;
            DELETE FROM paper_strategy_run_mutation_lease
            WHERE event_sequence = 1;
            """
        )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_history_invalid",
    ):
        active.status_payload()


def test_mutation_lease_insert_or_replace_cannot_bypass_append_guard(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    with active.mutation_lease("seed.commit"):
        pass

    with sqlite3.connect(registry_path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="mutation-lease-append-order-invalid",
        ):
            connection.execute(
                """
                INSERT OR REPLACE INTO paper_strategy_run_mutation_lease (
                    event_sequence, lease_id, run_id, epoch,
                    strategy_run_fingerprint, operation,
                    owner_token_sha256, acquired_at, event_type,
                    occurred_at, previous_event_sha256, event_sha256
                )
                SELECT event_sequence, lease_id, run_id, epoch,
                       strategy_run_fingerprint, 'tampered.operation',
                       owner_token_sha256, acquired_at, event_type,
                       occurred_at, previous_event_sha256, event_sha256
                FROM paper_strategy_run_mutation_lease
                WHERE event_sequence = 1
                """
            )
    assert active.status_payload()["mutations_drained"] is True


def test_mutation_lease_release_requires_unexposed_owner_token(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    lease = active.acquire_mutation_lease(
        "token-bound.commit",
        now=_NOW + timedelta(seconds=1),
    )
    diagnostics = active.mutation_lease_diagnostics()

    assert "owner_token" not in diagnostics["leases"][0]
    assert lease.owner_token not in repr(lease)
    assert lease.owner_token not in repr(diagnostics)
    forged = replace(lease, owner_token="forged-owner-token")
    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_invalid",
    ):
        active.release_mutation_lease(
            forged,
            now=_NOW + timedelta(seconds=2),
        )
    assert active.status_payload()["inflight_mutation_count"] == 1

    active.release_mutation_lease(
        lease,
        now=_NOW + timedelta(seconds=3),
    )
    assert active.status_payload()["mutations_drained"] is True
    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_invalid",
    ):
        active.release_mutation_lease(
            lease,
            now=_NOW + timedelta(seconds=4),
        )


def test_mutation_lease_cannot_be_released_through_another_registry(
    tmp_path: Path,
) -> None:
    active_one = establish_strategy_run(
        tmp_path / "registry-one.sqlite3",
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "registry-one-epoch-1"),
        now=_NOW,
    )
    active_two = establish_strategy_run(
        tmp_path / "registry-two.sqlite3",
        requested_epoch=1,
        identity=_identity(rule_set_fingerprint=_fp("a")),
        store_paths=_empty_store_paths(tmp_path, "registry-two-epoch-1"),
        now=_NOW,
    )
    lease = active_one.acquire_mutation_lease(
        "registry-bound.commit",
        now=_NOW + timedelta(seconds=1),
    )

    with pytest.raises(
        StrategyRunIntegrityError,
        match="strategy_run_mutation_lease_invalid",
    ):
        active_two.release_mutation_lease(
            lease,
            now=_NOW + timedelta(seconds=2),
        )
    assert active_one.status_payload()["inflight_mutation_count"] == 1
    active_one.release_mutation_lease(
        lease,
        now=_NOW + timedelta(seconds=3),
    )


def test_mutation_context_releases_lease_when_body_raises(tmp_path: Path) -> None:
    active = establish_strategy_run(
        tmp_path / "strategy-runs.sqlite3",
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )

    with pytest.raises(RuntimeError, match="body failed"):
        with active.mutation_lease("failing.commit"):
            raise RuntimeError("body failed")

    assert active.status_payload()["mutations_drained"] is True


def test_nested_mutation_context_reuses_one_physical_lease_500_times(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )

    with active.mutation_lease("outer.bar_cycle") as outer:
        for index in range(500):
            with active.mutation_lease(
                f"nested.process_bar.{index}"
            ) as nested:
                assert nested is outer

    with sqlite3.connect(registry_path) as connection:
        events = connection.execute(
            """
            SELECT event_type, COUNT(*)
            FROM paper_strategy_run_mutation_lease
            GROUP BY event_type ORDER BY event_type
            """
        ).fetchall()
    assert events == [("acquire", 1), ("release", 1)]
    assert active.status_payload()["mutations_drained"] is True


def test_copied_async_context_cannot_reuse_a_released_physical_lease(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )

    async def scenario() -> None:
        proceed = asyncio.Event()

        async def child() -> None:
            await proceed.wait()
            with active.mutation_lease("async.child") as child_lease:
                assert child_lease.operation == "async.child"

        with active.mutation_lease("async.parent"):
            task = asyncio.create_task(child())
        proceed.set()
        await task

    asyncio.run(scenario())

    with sqlite3.connect(registry_path) as connection:
        events = connection.execute(
            """
            SELECT operation, event_type
            FROM paper_strategy_run_mutation_lease
            ORDER BY event_sequence
            """
        ).fetchall()
    assert events == [
        ("async.parent", "acquire"),
        ("async.parent", "release"),
        ("async.child", "acquire"),
        ("async.child", "release"),
    ]


def test_sequential_mutation_leases_do_not_rescan_full_audit_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_validation_count = 0
    original = SQLiteStrategyRunRegistry._validate_mutation_lease_history

    def counted_validation(*args: object, **kwargs: object) -> object:
        nonlocal full_validation_count
        full_validation_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        SQLiteStrategyRunRegistry,
        "_validate_mutation_lease_history",
        staticmethod(counted_validation),
    )
    registry_path = tmp_path / "strategy-runs.sqlite3"
    active = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=_empty_store_paths(tmp_path, "epoch-1"),
        now=_NOW,
    )
    baseline = full_validation_count

    for index in range(20):
        with active.mutation_lease(f"sequential.commit.{index}"):
            pass

    assert full_validation_count - baseline <= 1
    with sqlite3.connect(registry_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_strategy_run_mutation_lease"
        ).fetchone() == (40,)


def test_alternating_registry_instances_validate_only_new_lease_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    store_paths = _empty_store_paths(tmp_path, "epoch-1")
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )
    active_two = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=_identity(),
        store_paths=store_paths,
        now=_NOW,
    )
    full_validation_count = 0
    original = SQLiteStrategyRunRegistry._validate_mutation_lease_history

    def counted_validation(*args: object, **kwargs: object) -> object:
        nonlocal full_validation_count
        full_validation_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        SQLiteStrategyRunRegistry,
        "_validate_mutation_lease_history",
        staticmethod(counted_validation),
    )

    for index in range(10):
        with active_one.mutation_lease(f"process-one.{index}"):
            pass
        with active_two.mutation_lease(f"process-two.{index}"):
            pass

    assert full_validation_count <= 1
    with sqlite3.connect(registry_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_strategy_run_mutation_lease"
        ).fetchone() == (40,)
