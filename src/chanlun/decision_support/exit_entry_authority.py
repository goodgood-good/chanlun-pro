"""Read-only authoritative entry links derived from the paper ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal

from .exit_runtime import AuthoritativeEntryLink, TrackedPosition
from .fingerprints import sha256_json
from .models import DecisionEvent
from .paper_adapter import (
    PaperIntent,
    PaperLedgerPort,
    PaperLot,
    reconcile_paper_ledger,
)
from .risk import HoldingSnapshot


def paper_admission_identity(intent: PaperIntent) -> str:
    """Fingerprint the immutable identity authorized by trusted admission."""

    if not isinstance(intent, PaperIntent):
        raise TypeError("intent must be PaperIntent")
    if intent.side != "buy" or intent.entry_event_id != intent.event_id:
        raise ValueError("entry intent must be a buy bound to its own event")
    return sha256_json(
        {
            "event_id": intent.event_id,
            "event_data_fingerprint": intent.event_data_fingerprint,
            "review_id": intent.review_id,
            "risk_snapshot_id": intent.risk_snapshot_id,
            "admission_authorization_id": intent.admission_authorization_id,
            "admission_payload_fingerprint": (
                intent.admission_payload_fingerprint
            ),
            "admitted_at": intent.admitted_at,
            "fee_schedule_fingerprint": (
                intent.fee_schedule_fingerprint
            ),
            "execution_policy_fingerprint": (
                intent.execution_policy_fingerprint
            ),
            "entry_event_id": intent.entry_event_id,
            "code": intent.code,
            "side": intent.side,
            "signal_bar_id": intent.signal_bar_id,
            "signal_at": intent.signal_at,
        }
    )


def paper_lot_provenance_fingerprint(
    lots: Iterable[PaperLot],
    *,
    ledger_revision: int,
) -> str:
    if (
        isinstance(ledger_revision, bool)
        or not isinstance(ledger_revision, int)
        or ledger_revision <= 0
    ):
        raise ValueError("ledger_revision must be a positive integer")
    if isinstance(lots, (str, bytes)):
        raise TypeError("lots must be an iterable of PaperLot")
    frozen_lots = tuple(lots)
    if not frozen_lots or not all(
        isinstance(lot, PaperLot) for lot in frozen_lots
    ):
        raise ValueError("lots must contain PaperLot values")
    ordered = tuple(
        sorted(
            frozen_lots,
            key=lambda lot: (
                lot.entry_event_id,
                lot.code,
                lot.opened_at,
                lot.entry_review_id,
                lot.entry_risk_snapshot_id,
                lot.shares,
                lot.price,
            ),
        )
    )
    return sha256_json(
        {"ledger_revision": ledger_revision, "lots": ordered}
    )


class PaperLedgerEntryAuthorityResolver:
    """Rebuild the canonical tracked position from a read-only paper ledger."""

    def __init__(
        self,
        ledger: PaperLedgerPort,
        *,
        event_resolver: Callable[[str], DecisionEvent | None],
    ) -> None:
        if not isinstance(ledger, PaperLedgerPort):
            raise TypeError("ledger must implement PaperLedgerPort")
        if not callable(event_resolver):
            raise TypeError("event_resolver must be callable")
        self._ledger = ledger
        self._event_resolver = event_resolver

    def __call__(
        self,
        candidate: TrackedPosition,
    ) -> AuthoritativeEntryLink | None:
        if not isinstance(candidate, TrackedPosition):
            raise TypeError("candidate must be TrackedPosition")
        return self.resolve(candidate.entry_event_id, candidate.holding)

    def resolve(
        self,
        entry_event_id: str,
        holding: HoldingSnapshot,
    ) -> AuthoritativeEntryLink | None:
        if (
            not isinstance(entry_event_id, str)
            or not entry_event_id.strip()
            or entry_event_id != entry_event_id.strip()
        ):
            raise ValueError("entry_event_id must be a non-empty trimmed string")
        if not isinstance(holding, HoldingSnapshot):
            raise TypeError("holding must be HoldingSnapshot")
        state = self._ledger.load()
        reconciliation = reconcile_paper_ledger(self._ledger)
        if state.revision != reconciliation.revision or state.revision <= 0:
            raise ValueError("paper ledger revision changed during resolution")
        event = self._event_resolver(entry_event_id)
        if event is None:
            return None
        if not isinstance(event, DecisionEvent):
            raise TypeError("event_resolver must return DecisionEvent or None")
        intents = tuple(
            intent
            for intent in state.intents
            if intent.event_id == entry_event_id
            and intent.side == "buy"
        )
        if len(intents) != 1:
            return None
        intent = intents[0]
        if (
            intent.event_data_fingerprint != event.data_fingerprint
            or intent.entry_event_id != event.event_id
            or intent.code != event.code
        ):
            raise ValueError("paper entry intent does not match entry event")
        fills = tuple(
            sorted(
                (
                    fill
                    for fill in state.fills
                    if fill.event_id == intent.event_id and fill.side == "buy"
                ),
                key=lambda fill: fill.fill_id,
            )
        )
        lots = tuple(
            lot
            for lot in state.lots
            if lot.entry_event_id == intent.event_id
        )
        if not fills or not lots:
            return None
        shares = sum(lot.shares for lot in lots)
        if shares <= 0:
            raise ValueError("paper entry lots have no remaining shares")
        average_price = sum(
            (lot.price * lot.shares for lot in lots),
            Decimal("0"),
        ) / shares
        holding = HoldingSnapshot(
            code=event.code,
            shares=shares,
            sellable_shares=min(holding.sellable_shares, shares),
            opened_at=min(lot.opened_at for lot in lots),
            average_price=average_price,
        )
        canonical = TrackedPosition(
            entry_event_id=event.event_id,
            entry_data_fingerprint=event.data_fingerprint,
            entry_review_id=intent.review_id,
            entry_risk_snapshot_id=intent.risk_snapshot_id,
            entry_paper_admission_id=paper_admission_identity(intent),
            paper_fill_ids=tuple(fill.fill_id for fill in fills),
            paper_ledger_revision=state.revision,
            lot_provenance_fingerprint=paper_lot_provenance_fingerprint(
                lots,
                ledger_revision=state.revision,
            ),
            strategy_track=event.strategy_track,
            holding=holding,
        )
        if self._ledger.load() != state:
            raise ValueError("paper ledger changed during resolution")
        return AuthoritativeEntryLink(
            position=canonical,
            entry_event=event,
            entry_review_id=canonical.entry_review_id,
            entry_risk_snapshot_id=canonical.entry_risk_snapshot_id,
            entry_paper_admission_id=canonical.entry_paper_admission_id,
            paper_fill_ids=canonical.paper_fill_ids,
            paper_ledger_revision=canonical.paper_ledger_revision,
            lot_provenance_fingerprint=canonical.lot_provenance_fingerprint,
        )


__all__ = (
    "PaperLedgerEntryAuthorityResolver",
    "paper_admission_identity",
    "paper_lot_provenance_fingerprint",
)
