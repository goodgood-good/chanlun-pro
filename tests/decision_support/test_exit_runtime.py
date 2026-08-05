from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import importlib

import pytest

from chanlun.decision_support.exit_runtime import (
    AuthoritativeEntryLink,
    ExitEvaluationRequest,
    ExitRuntimeFailure,
    TrackedPosition,
    evaluate_tracked_position,
    evaluate_tracked_positions,
)
from chanlun.decision_support.exit_evidence_policy import (
    ExitEvidenceBinding,
    ExitEvidencePolicy,
    ExitEvidenceReference,
)
from chanlun.decision_support.exit_evaluation_store import (
    ExitEvaluationConflictError,
    ExitEvaluationService,
    SQLiteExitEvaluationStore,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.exits import ExitStatus, ExitTrigger
from chanlun.decision_support.models import StrategyTrack
from chanlun.decision_support.paper_read_model import PaperResearchReadModel
from chanlun.decision_support.risk import HoldingSnapshot
from chanlun.decision_support.scanner import SymbolStructureSnapshot
from chanlun.recursive_bt.engine.engine import Signal


class _FrozenLevels:
    def __init__(self, levels: tuple[object, ...], frequency: str = "5m") -> None:
        self.frequency = frequency
        self._levels = levels

    def get_recursive_branch_levels(self) -> tuple[object, ...]:
        return self._levels


def _level(level: int, frequency: str, direction: str, *, completed=True):
    return SimpleNamespace(
        level=level,
        frequency=frequency,
        direction=direction,
        completed=completed,
        start=None,
        end=None,
        zss=(),
        mmds=(),
        divergences=(),
    )


def test_exit_cycle_identity_builder_is_deterministic_and_bar_bound() -> None:
    module = importlib.import_module("chanlun.decision_support.exit_runtime")
    builder = getattr(module, "build_exit_evaluation_cycle_id", None)
    assert callable(builder)
    now = datetime.fromisoformat("2026-07-14T10:35:00+08:00")
    kwargs = {
        "code": "SH.600519",
        "frequency": "5m",
        "bar_closed_at": now,
        "structure_source_fingerprint": "sha256:" + "2" * 64,
    }

    first = builder(**kwargs)
    second = builder(**kwargs)
    next_bar = builder(
        **{**kwargs, "bar_closed_at": now + timedelta(minutes=5)}
    )

    assert first == second
    assert first.startswith("sha256:")
    assert first != next_bar


def test_tracked_position_requires_complete_authoritative_entry_provenance(
    make_decision_event,
) -> None:
    event = make_decision_event()
    holding = HoldingSnapshot(
        code=event.code,
        shares=1000,
        sellable_shares=1000,
        opened_at=event.observed_at + timedelta(minutes=5),
        average_price=Decimal("10"),
    )

    tracked = TrackedPosition(
        entry_event_id=event.event_id,
        entry_data_fingerprint=event.data_fingerprint,
        entry_review_id="review-entry-1",
        entry_risk_snapshot_id="risk-entry-1",
        entry_paper_admission_id="sha256:" + "a" * 64,
        paper_fill_ids=("paper-fill-1", "paper-fill-2"),
        paper_ledger_revision=7,
        lot_provenance_fingerprint="sha256:" + "b" * 64,
        strategy_track=event.strategy_track,
        holding=holding,
    )

    assert tracked.entry_review_id == "review-entry-1"
    assert tracked.entry_risk_snapshot_id == "risk-entry-1"
    assert tracked.paper_fill_ids == ("paper-fill-1", "paper-fill-2")
    assert tracked.paper_ledger_revision == 7
    assert tracked.entry_provenance_fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="authoritative entry_review_id mismatch"):
        AuthoritativeEntryLink(
            position=tracked,
            entry_event=event,
            entry_review_id="review-forged",
            entry_risk_snapshot_id=tracked.entry_risk_snapshot_id,
            entry_paper_admission_id=tracked.entry_paper_admission_id,
            paper_fill_ids=tracked.paper_fill_ids,
            paper_ledger_revision=tracked.paper_ledger_revision,
            lot_provenance_fingerprint=tracked.lot_provenance_fingerprint,
        )


def _tracked(event, now: datetime, *, shares=1000, sellable=1000):
    return TrackedPosition(
        entry_event_id=event.event_id,
        entry_data_fingerprint=event.data_fingerprint,
        entry_review_id="review-entry-1",
        entry_risk_snapshot_id="risk-entry-1",
        entry_paper_admission_id="sha256:" + "a" * 64,
        paper_fill_ids=("paper-fill-1",),
        paper_ledger_revision=7,
        lot_provenance_fingerprint="sha256:" + "b" * 64,
        strategy_track=event.strategy_track,
        holding=HoldingSnapshot(
            code=event.code,
            shares=shares,
            sellable_shares=sellable,
            opened_at=event.observed_at + timedelta(minutes=5),
            average_price=Decimal("10"),
        ),
    )


def _structure(
    now: datetime,
    *,
    direction: str = "up",
    sell_level: int | None = 1,
    sell_type: str = "3sell",
    signal_time: datetime | None = None,
    completed: bool = True,
    operation_bar_closed: bool = True,
    first_observed_at: datetime | None = None,
    bind_sell_signal: bool = True,
    observation_state: str = "trusted_first_seen",
    current_cycle_id: str = "sha256:" + "c" * 64,
) -> SymbolStructureSnapshot:
    signals = ()
    if sell_level is not None:
        signals = (
            Signal(
                date=signal_time or now,
                level=sell_level,
                bs_type=sell_type,
                price=10.0,
            ),
        )
    levels = (
        _level(1, "5m", "up", completed=completed),
        _level(2, "30m", direction, completed=completed),
    )
    signal_fingerprints = tuple(sha256_json(signal) for signal in signals)
    signal_observations = (
        {
            signal_fingerprint: first_observed_at or now
            for signal_fingerprint in signal_fingerprints
        }
        if bind_sell_signal and observation_state != "quarantined_unknown"
        else {}
    )
    signal_observation_states = (
        {
            signal_fingerprint: observation_state
            for signal_fingerprint in signal_fingerprints
        }
        if bind_sell_signal
        else {}
    )
    return SymbolStructureSnapshot(
        frequency="5m",
        cd=_FrozenLevels(levels),
        signals=signals,
        first_visible_bar=1,
        completed_bars=(
            {
                "closed_at": now,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1000.0,
            },
        ),
        config={"source": "test"},
        operation_bar_closed=operation_bar_closed,
        fund_ok=True,
        comparison_ok=True,
        current_cycle_id=current_cycle_id,
        signals_first_observed_at=signal_observations,
        signal_observation_states=signal_observation_states,
    )


def _request(event, context, structure, tracked, now):
    return ExitEvaluationRequest(
        position=tracked,
        entry_event=event,
        structure=structure,
        risk_context=context,
        bar_closed_at=now,
        evaluation_cycle_id="sha256:" + "c" * 64,
    )


def _evidence_policy() -> ExitEvidencePolicy:
    bindings = []
    for index, trigger in enumerate(ExitTrigger, start=1):
        bindings.append(
            ExitEvidenceBinding(
                trigger=trigger,
                references=(
                    ExitEvidenceReference(
                        evidence_id="evidence:" + f"{index:064x}",
                        lesson=index,
                        pdf_page=index,
                        source_role="lesson_body",
                        evidence_sha256=f"{index + 100:064x}",
                    ),
                ),
                boundary_tags=("project_risk_latch",)
                if trigger is ExitTrigger.HARD_RISK
                else (),
            )
        )
    return ExitEvidencePolicy(
        schema_version=1,
        policy_id="original-exit-policy",
        version=1,
        corpus_manifest_sha256="1" * 64,
        source_pdf_sha256="2" * 64,
        bindings=tuple(bindings),
    )


def _ledger(event, tracked):
    link = AuthoritativeEntryLink(
        position=tracked,
        entry_event=event,
        entry_review_id=tracked.entry_review_id,
        entry_risk_snapshot_id=tracked.entry_risk_snapshot_id,
        entry_paper_admission_id=tracked.entry_paper_admission_id,
        paper_fill_ids=tracked.paper_fill_ids,
        paper_ledger_revision=tracked.paper_ledger_revision,
        lot_provenance_fingerprint=tracked.lot_provenance_fingerprint,
    )

    def resolve(candidate):
        return link if candidate.holding == tracked.holding else None

    return resolve


def test_arbitrary_tuple_evidence_resolver_is_rejected(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )

    with pytest.raises(TypeError, match="ExitEvidencePolicy"):
        evaluate_tracked_position(
            _request(event, context, _structure(now), tracked, now),
            evidence_resolver=lambda _trigger: ("forged-original",),
            entry_ledger_resolver=_ledger(event, tracked),
        )


def test_authoritative_ledger_is_required_and_rejects_forged_entry_link(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    authoritative = _tracked(event, now)
    forged = replace(authoritative, entry_event_id="forged-entry")
    context = make_risk_context(
        holdings=(forged.holding,), asof=now, quote_time=now
    )
    request = _request(event, context, _structure(now), forged, now)

    with pytest.raises(ValueError, match="authoritative_entry_link_mismatch"):
        evaluate_tracked_position(
            request,
            evidence_resolver=_evidence_policy(),
            entry_ledger_resolver=_ledger(event, authoritative),
        )

    with pytest.raises(ValueError, match="authoritative_entry_link_missing"):
        evaluate_tracked_position(
            _request(event, context, _structure(now), authoritative, now),
            evidence_resolver=_evidence_policy(),
            entry_ledger_resolver=lambda _holding: None,
        )


def test_authoritative_resolver_receives_complete_tracked_identity(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    link = AuthoritativeEntryLink(
        position=tracked,
        entry_event=event,
        entry_review_id=tracked.entry_review_id,
        entry_risk_snapshot_id=tracked.entry_risk_snapshot_id,
        entry_paper_admission_id=tracked.entry_paper_admission_id,
        paper_fill_ids=tracked.paper_fill_ids,
        paper_ledger_revision=tracked.paper_ledger_revision,
        lot_provenance_fingerprint=tracked.lot_provenance_fingerprint,
    )
    resolved: list[object] = []

    def resolver(candidate):
        resolved.append(candidate)
        return link

    evaluate_tracked_position(
        _request(event, context, _structure(now), tracked, now),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=resolver,
    )

    assert resolved == [tracked]


@pytest.mark.parametrize(
    "changes",
    (
        {"entry_review_id": "review-forged"},
        {"entry_risk_snapshot_id": "risk-forged"},
        {"entry_paper_admission_id": "sha256:" + "d" * 64},
        {"paper_fill_ids": ("paper-fill-forged",)},
        {"paper_ledger_revision": 8},
        {"lot_provenance_fingerprint": "sha256:" + "e" * 64},
    ),
)
def test_authoritative_resolver_rejects_each_entry_provenance_forgery(
    make_decision_event,
    make_risk_context,
    changes,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    canonical = _tracked(event, now)
    forged = replace(canonical, **changes)
    context = make_risk_context(
        holdings=(forged.holding,), asof=now, quote_time=now
    )

    with pytest.raises(ValueError, match="authoritative_entry_link_mismatch"):
        evaluate_tracked_position(
            _request(event, context, _structure(now), forged, now),
            evidence_resolver=_evidence_policy(),
            entry_ledger_resolver=_ledger(event, canonical),
        )


def test_tracked_position_is_frozen_and_strictly_bound(
    make_decision_event,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)

    assert tracked.entry_event_id == event.event_id
    assert tracked.strategy_track is StrategyTrack.CHANLUN_SOURCE_FAITHFUL
    with pytest.raises(FrozenInstanceError):
        tracked.entry_event_id = "changed"
    with pytest.raises(ValueError, match="entry_data_fingerprint"):
        replace(tracked, entry_data_fingerprint="not-a-fingerprint")


def test_operation_sell_is_layered_and_deterministic(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
    )
    request = _request(
        event,
        context,
        _structure(now),
        tracked,
        now,
    )

    first = evaluate_tracked_position(
        request,
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )
    second = evaluate_tracked_position(
        request,
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert first == second
    assert first.entry_event_id == event.event_id
    assert first.selection.intent is not None
    assert first.selection.intent.trigger is ExitTrigger.OPERATION_LEVEL_SELL
    assert first.selection.intent.requested_shares == 200
    assert first.outcome is not None
    assert first.outcome.status is ExitStatus.EXECUTABLE


def test_recommendation_freezes_entry_evidence_market_and_algorithm_identity(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    structure = _structure(now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    policy = _evidence_policy()

    result = evaluate_tracked_position(
        _request(event, context, structure, tracked, now),
        evidence_resolver=policy,
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.entry_provenance_fingerprint == (
        tracked.entry_provenance_fingerprint
    )
    assert result.exit_evidence_policy_fingerprint == policy.fingerprint
    assert result.certified_corpus_manifest_fingerprint == "sha256:" + "1" * 64
    assert result.source_pdf_fingerprint == "sha256:" + "2" * 64
    assert result.bar_structure_payload_fingerprint.startswith("sha256:")
    assert result.risk_context_payload_fingerprint.startswith("sha256:")
    assert result.quote_payload_fingerprint.startswith("sha256:")
    assert result.algorithm_version == "chanlun-exit-runtime-v3"
    assert result.evaluation_version == 3
    assert result.max_signal_confirmation_lag_seconds == 600


@pytest.mark.parametrize(
    "observation_state",
    ("baseline_not_fresh", "quarantined_unknown"),
)
def test_untrusted_sell_observation_is_suppressed_but_hard_risk_remains_active(
    make_decision_event,
    make_risk_context,
    observation_state,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
        daily_loss_locked=True,
    )
    structure = _structure(
        now,
        signal_time=now - timedelta(minutes=5),
        observation_state=observation_state,
    )
    signal_fingerprint = sha256_json(structure.signals[0])

    result = evaluate_tracked_position(
        _request(event, context, structure, tracked, now),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.selection.intent is not None
    assert result.selection.intent.trigger is ExitTrigger.HARD_RISK
    assert result.signal_snapshot.operation_level_sell is False
    assert result.signal_observation_states == {
        signal_fingerprint: observation_state
    }
    assert result.signal_observation_suppressions == {
        signal_fingerprint: observation_state
    }
    assert result.to_dict()["signal_observation_states"] == {
        signal_fingerprint: observation_state
    }


def test_stale_trusted_sell_is_suppressed_but_hard_risk_remains_active(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
        daily_loss_locked=True,
    )
    structure = _structure(
        now,
        signal_time=now - timedelta(minutes=5),
        first_observed_at=now - timedelta(minutes=5),
    )
    signal_fingerprint = sha256_json(structure.signals[0])

    result = evaluate_tracked_position(
        _request(event, context, structure, tracked, now),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.selection.intent is not None
    assert result.selection.intent.trigger is ExitTrigger.HARD_RISK
    assert result.signal_snapshot.operation_level_sell is False
    assert result.signal_observation_states == {
        signal_fingerprint: "trusted_first_seen"
    }
    assert result.signal_observation_suppressions == {
        signal_fingerprint: "stale_signal_observation"
    }
    assert result.to_dict()["signal_observation_suppressions"] == {
        signal_fingerprint: "stale_signal_observation"
    }


@pytest.mark.parametrize(
    "observation_state",
    ("baseline_not_fresh", "quarantined_unknown"),
)
def test_untrusted_sell_observation_alone_never_triggers_signal_exit(
    make_decision_event,
    make_risk_context,
    observation_state,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
    )

    result = evaluate_tracked_position(
        _request(
            event,
            context,
            _structure(now, observation_state=observation_state),
            tracked,
            now,
        ),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.selection.intent is None
    assert result.outcome is None
    assert result.signal_snapshot.operation_level_sell is False


def test_right_side_confirmation_accepts_only_bound_current_cycle_signal(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    request = _request(
        event,
        context,
        _structure(
            now,
            signal_time=now - timedelta(minutes=5),
            first_observed_at=now - timedelta(minutes=1),
        ),
        tracked,
        now,
    )

    result = evaluate_tracked_position(
        request,
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )
    assert result.selection.intent is not None
    assert result.selection.intent.trigger is ExitTrigger.OPERATION_LEVEL_SELL

    for structure, reason in (
        (
            _structure(
                now,
                signal_time=now - timedelta(minutes=5),
                bind_sell_signal=False,
            ),
            "unbound_sell_signal",
        ),
        (
            _structure(now, signal_time=now - timedelta(minutes=15)),
            "sell_signal_confirmation_window_exceeded",
        ),
        (
            _structure(now, current_cycle_id="sha256:" + "d" * 64),
            "evaluation_cycle_mismatch",
        ),
    ):
        with pytest.raises(ValueError, match=reason):
            evaluate_tracked_position(
                _request(event, context, structure, tracked, now),
                evidence_resolver=_evidence_policy(),
                entry_ledger_resolver=_ledger(event, tracked),
            )


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"daily_loss_locked": True}, ExitTrigger.HARD_RISK),
        ({"entry_reference": "8.9"}, ExitTrigger.STRUCTURAL_INVALIDATION),
        ({"direction": "down"}, ExitTrigger.CONTROL_LEVEL_DOWN),
        ({"sell_level": 2}, ExitTrigger.CONTROL_LEVEL_SELL),
    ),
)
def test_exit_priority_is_built_from_frozen_runtime_facts(
    make_decision_event,
    make_risk_context,
    changes,
    expected,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    direction = changes.get("direction", "up")
    sell_level = changes.get("sell_level", 1)
    context_kwargs = {
        key: value
        for key, value in changes.items()
        if key not in {"direction", "sell_level"}
    }
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
        **context_kwargs,
    )

    result = evaluate_tracked_position(
        _request(
            event,
            context,
            _structure(now, direction=direction, sell_level=sell_level),
            tracked,
            now,
        ),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.selection.intent is not None
    assert result.selection.intent.trigger is expected
    assert result.selection.intent.evidence[0].evidence_ids[0].startswith(
        "evidence:"
    )


@pytest.mark.parametrize(
    "context_changes",
    (
        {"sellable": 0},
        {"limit_down_locked": True},
        {"exit_tradable": False},
    ),
)
def test_t1_limit_and_suspension_remain_pending(
    make_decision_event,
    make_risk_context,
    context_changes,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    sellable = context_changes.pop("sellable", 1000)
    tracked = _tracked(event, now, sellable=sellable)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=now,
        **context_changes,
    )

    result = evaluate_tracked_position(
        _request(event, context, _structure(now, sell_level=2), tracked, now),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert result.outcome is not None
    assert result.outcome.status is ExitStatus.PENDING
    assert result.outcome.executable_shares == 0
    assert result.outcome.pending_shares == tracked.holding.shares


def test_non_policy_original_evidence_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )

    with pytest.raises(TypeError, match="ExitEvidencePolicy"):
        evaluate_tracked_position(
            _request(event, context, _structure(now), tracked, now),
            evidence_resolver=lambda _trigger: (),
            entry_ledger_resolver=_ledger(event, tracked),
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_entry_id",
        "wrong_fingerprint",
        "unbound_stale_signal",
        "future_signal",
        "incomplete_level",
        "open_bar",
        "stale_quote",
        "future_bar_history",
    ),
)
def test_identity_time_and_completeness_conflicts_fail_closed(
    make_decision_event,
    make_risk_context,
    mutation,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    canonical = _tracked(event, now)
    tracked = canonical
    structure = _structure(now)
    quote_time = now
    if mutation == "wrong_entry_id":
        tracked = replace(tracked, entry_event_id="missing-entry")
    elif mutation == "wrong_fingerprint":
        tracked = replace(
            tracked,
            entry_data_fingerprint="sha256:" + "f" * 64,
        )
    elif mutation == "unbound_stale_signal":
        structure = _structure(
            now,
            signal_time=now - timedelta(minutes=5),
            bind_sell_signal=False,
        )
    elif mutation == "future_signal":
        structure = _structure(now, signal_time=now + timedelta(minutes=5))
    elif mutation == "incomplete_level":
        structure = _structure(now, completed=False)
    elif mutation == "open_bar":
        structure = _structure(now, operation_bar_closed=False)
    elif mutation == "stale_quote":
        quote_time = now - timedelta(minutes=5)
    elif mutation == "future_bar_history":
        future_bar = {
            **structure.completed_bars[-1],
            "closed_at": now + timedelta(minutes=5),
        }
        structure = replace(
            structure,
            completed_bars=(future_bar, structure.completed_bars[-1]),
        )
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=now,
        quote_time=quote_time,
    )

    with pytest.raises((TypeError, ValueError)):
        evaluate_tracked_position(
            _request(event, context, structure, tracked, now),
            evidence_resolver=_evidence_policy(),
            entry_ledger_resolver=_ledger(event, canonical),
        )


def test_many_positions_isolate_failures_and_never_drop_valid_results(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    valid = _request(event, context, _structure(now), tracked, now)
    invalid = replace(
        valid,
        position=replace(tracked, entry_event_id="missing-entry"),
    )

    result = evaluate_tracked_positions(
        (invalid, valid),
        evidence_resolver=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    assert len(result.recommendations) == 1
    assert result.recommendations[0].entry_event_id == event.event_id
    assert len(result.failures) == 1
    assert isinstance(result.failures[0], ExitRuntimeFailure)
    assert result.failures[0].entry_event_id == "missing-entry"
    assert result.failures[0].reason == "authoritative_entry_link_mismatch"


def test_evaluate_and_persist_is_idempotent_and_rejects_cycle_payload_change(
    tmp_path,
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    request = _request(
        event,
        context,
        _structure(now, observation_state="baseline_not_fresh"),
        tracked,
        now,
    )
    store = SQLiteExitEvaluationStore(tmp_path / "exit-runtime.sqlite3")
    service = ExitEvaluationService(
        store,
        evidence_policy=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    first = service.evaluate_and_persist(request)
    replay = service.evaluate_and_persist(request)

    assert first == replay
    assert first.entry_provenance_fingerprint == (
        tracked.entry_provenance_fingerprint
    )
    assert first.recommendation_payload["entry_event_id"] == event.event_id
    signal_fingerprint = sha256_json(request.structure.signals[0])
    assert first.recommendation_payload["signal_observation_states"] == {
        signal_fingerprint: "baseline_not_fresh"
    }
    assert first.recommendation_payload["signal_observation_suppressions"] == {
        signal_fingerprint: "baseline_not_fresh"
    }
    assert store.revision == 1

    changed_quote = replace(context.quote, price=Decimal("10.1"))
    changed_request = replace(
        request,
        risk_context=replace(context, quote=changed_quote),
    )
    with pytest.raises(ExitEvaluationConflictError, match="payload conflict"):
        service.evaluate_and_persist(changed_request)


def test_raw_service_snapshot_at_completed_time_stays_provisional_without_manifest(
    tmp_path,
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    completed_at = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, completed_at)
    context = make_risk_context(
        holdings=(tracked.holding,),
        asof=completed_at,
        quote_time=completed_at,
    )
    request = _request(
        event,
        context,
        _structure(completed_at),
        tracked,
        completed_at,
    )
    store = SQLiteExitEvaluationStore(tmp_path / "raw-exit.sqlite3")
    service = ExitEvaluationService(
        store,
        evidence_policy=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )
    snapshot = service.evaluate_and_persist(request)
    assert snapshot.recommendation_payload["selection"]["intent"] is not None

    def exact_manifest_memberships(candidates) -> tuple[bool, ...]:
        assert all(candidate.evaluated_at == completed_at for candidate in candidates)
        return (False,) * len(candidates)

    model = PaperResearchReadModel(
        SimpleNamespace(load=lambda: None, account_snapshot=lambda: None),
        exit_store=store,
        runtime=SimpleNamespace(
            health=lambda: object(),
            attest_exit_snapshots=exact_manifest_memberships,
        ),
    )

    payload = model.exits()

    assert payload["count"] == 0
    assert payload["provisional_count"] == 1
    assert payload["items"] == []


def test_evaluate_and_persist_batch_isolates_each_position_failure(
    tmp_path,
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    now = event.observed_at + timedelta(days=1)
    tracked = _tracked(event, now)
    context = make_risk_context(
        holdings=(tracked.holding,), asof=now, quote_time=now
    )
    valid = _request(event, context, _structure(now), tracked, now)
    invalid = replace(
        valid,
        structure=replace(
            valid.structure,
            current_cycle_id="sha256:" + "d" * 64,
        ),
    )
    service = ExitEvaluationService(
        SQLiteExitEvaluationStore(tmp_path / "exit-batch.sqlite3"),
        evidence_policy=_evidence_policy(),
        entry_ledger_resolver=_ledger(event, tracked),
    )

    result = service.evaluate_and_persist_many((invalid, valid))

    assert len(result.snapshots) == 1
    assert result.snapshots[0].entry_event_id == event.event_id
    assert len(result.failures) == 1
    assert result.failures[0].reason == "evaluation_cycle_mismatch"
