from __future__ import annotations

import importlib.util
import importlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from chanlun.decision_support.exit_entry_authority import (
    PaperLedgerEntryAuthorityResolver,
    paper_admission_identity,
    paper_lot_provenance_fingerprint,
)
from chanlun.decision_support.exit_runtime import TrackedPosition
from chanlun.decision_support.paper_adapter import (
    InMemoryPaperLedger,
    PaperFill,
    PaperIntent,
    PaperLedgerState,
    PaperLot,
)
from chanlun.decision_support.risk import HoldingSnapshot


def test_exit_entry_authority_module_is_available() -> None:
    assert (
        importlib.util.find_spec(
            "chanlun.decision_support.exit_entry_authority"
        )
        is not None
    )


def test_exit_entry_authority_api_is_available() -> None:
    module = importlib.import_module(
        "chanlun.decision_support.exit_entry_authority"
    )
    assert hasattr(module, "PaperLedgerEntryAuthorityResolver")
    assert hasattr(module, "paper_admission_identity")
    assert hasattr(module, "paper_lot_provenance_fingerprint")


def test_paper_ledger_resolver_derives_every_entry_provenance_field(
    make_decision_event,
) -> None:
    event = make_decision_event()
    opened_at = event.observed_at + timedelta(minutes=5)
    intent = PaperIntent(
        event_id=event.event_id,
        event_data_fingerprint=event.data_fingerprint,
        review_id="review-entry-1",
        risk_snapshot_id="risk-entry-1",
        admission_authorization_id="paper-admission-auth-1",
        admission_payload_fingerprint="sha256:" + "e" * 64,
        admitted_at=event.observed_at,
        risk_expires_at=opened_at + timedelta(minutes=5),
        entry_event_id=event.event_id,
        code=event.code,
        side="buy",
        risk_shares=1000,
        requested_shares=1000,
        remaining_shares=0,
        signal_bar_id="bar-1",
        signal_at=event.observed_at,
        limit_pct=Decimal("0.1"),
        status="filled",
        reason="paper_buy",
        fee_schedule_fingerprint="sha256:" + "a" * 64,
        execution_policy_fingerprint="sha256:" + "b" * 64,
    )
    assert paper_admission_identity(intent) != paper_admission_identity(
        replace(
            intent,
            execution_policy_fingerprint="sha256:" + "c" * 64,
        )
    )
    fill = PaperFill(
        fill_id="paper-fill-1",
        event_id=event.event_id,
        entry_event_id=event.event_id,
        review_id=intent.review_id,
        risk_snapshot_id=intent.risk_snapshot_id,
        code=event.code,
        side="buy",
        shares=1000,
        reference_price=Decimal("10"),
        price=Decimal("10"),
        gross_value=Decimal("10000"),
        commission=Decimal("5"),
        stamp_duty=Decimal("0"),
        transfer_fee=Decimal("0"),
        regulatory_fee=Decimal("0"),
        slippage_cost=Decimal("0"),
        trade_cost=Decimal("5"),
        filled_at=opened_at,
        bar_id="bar-1",
    )
    lot = PaperLot(
        code=event.code,
        shares=1000,
        price=Decimal("10"),
        opened_at=opened_at,
        entry_event_id=event.event_id,
        entry_review_id=intent.review_id,
        entry_risk_snapshot_id=intent.risk_snapshot_id,
    )
    state = PaperLedgerState(
        revision=3,
        intents=(intent,),
        fills=(fill,),
        lots=(lot,),
        processed_bar_ids=("bar-1",),
    )
    holding = HoldingSnapshot(
        code=event.code,
        shares=1000,
        sellable_shares=1000,
        opened_at=opened_at,
        average_price=Decimal("10"),
    )
    tracked = TrackedPosition(
        entry_event_id=event.event_id,
        entry_data_fingerprint=event.data_fingerprint,
        entry_review_id=intent.review_id,
        entry_risk_snapshot_id=intent.risk_snapshot_id,
        entry_paper_admission_id=paper_admission_identity(intent),
        paper_fill_ids=(fill.fill_id,),
        paper_ledger_revision=state.revision,
        lot_provenance_fingerprint=paper_lot_provenance_fingerprint(
            (lot,),
            ledger_revision=state.revision,
        ),
        strategy_track=event.strategy_track,
        holding=holding,
    )
    resolver = PaperLedgerEntryAuthorityResolver(
        InMemoryPaperLedger(state),
        event_resolver=lambda event_id: event
        if event_id == event.event_id
        else None,
    )

    link = resolver(tracked)
    discovered_link = resolver.resolve(event.event_id, holding)

    assert link is not None
    assert discovered_link == link
    assert link.position == tracked
    assert link.entry_review_id == intent.review_id
    assert link.entry_risk_snapshot_id == intent.risk_snapshot_id
    assert link.entry_paper_admission_id == paper_admission_identity(intent)
    assert link.paper_fill_ids == (fill.fill_id,)
    assert link.paper_ledger_revision == state.revision
    assert link.lot_provenance_fingerprint == (
        tracked.lot_provenance_fingerprint
    )
    forged = replace(tracked, entry_review_id="review-forged")
    forged_link = resolver(forged)
    assert forged_link is not None
    assert forged_link.position.entry_review_id == intent.review_id
    assert forged_link.position != forged

    class ChangingLedger:
        def __init__(self):
            self.loads = 0

        def load(self):
            self.loads += 1
            return state if self.loads < 3 else replace(state, revision=4)

        def commit(self, *, expected_revision, state):
            raise AssertionError("read-only resolver must not commit")

    changing_resolver = PaperLedgerEntryAuthorityResolver(
        ChangingLedger(),
        event_resolver=lambda _event_id: event,
    )
    with pytest.raises(ValueError, match="changed during resolution"):
        changing_resolver(tracked)
