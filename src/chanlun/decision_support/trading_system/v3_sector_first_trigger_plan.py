"""Causal sector-first scheduling plan for the V3 individual-stock path.

This module never calculates a Chanlun point and never creates an order.  It
joins already-computed sector assessments with the effective-dated SW1 member
ledger, producing the exact stock scope that may be expanded at each completed
30-minute decision time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    SectorResearchFacts,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataIndex,
    PITMetadataSnapshot,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.models import SectorAssessment
from chanlun.decision_support.trading_system.sector_policy import rank_sectors
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    SectorStrengthEvidence,
)
from chanlun.decision_support.trading_system.v3_recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
)
from chanlun.decision_support.trading_system.v3_sector_first_scope import (
    SectorFirstScope,
)


SECTOR_FIRST_TRIGGER_SCHEMA = "chanlun-v3-sector-first-trigger-ledger/v2"
CURRENT_BACKFILL_MEMBERSHIP_MODE = "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
FORWARD_SAME_SESSION_MEMBERSHIP_MODE = "FORWARD_SAME_SESSION_CAPTURE"
_CURRENT_BACKFILL_PREFIX = (
    "QMT_CURRENT_SECTOR_TRIGGER",
    "QMT_CURRENT_MEMBERS_BACKFILLED_USER_AUTHORIZED",
)
_FORWARD_CAPTURE_PREFIX = (
    "QMT_PIT_SECTOR_TRIGGER",
    "QMT_PIT_MEMBERS_CAPTURED_SAME_SESSION",
)


def _document_dates(value: object) -> object:
    """Convert date-only fields before the canonical fingerprint boundary."""

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _document_dates(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_document_dates(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SectorTriggerRankFact:
    sector_id: str
    sector_name: str
    ordinal: int
    rank_score: int
    regime: str
    reason_codes: tuple[str, ...]
    horizontal_strength: Decimal | None = None
    horizontal_rank: int | None = None
    strength_observed_at: datetime | None = None
    strength_anchor_session: date | None = None
    strength_member_count: int = 0
    strength_source_revision: str | None = None
    strength_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.sector_id or not self.sector_name or self.ordinal <= 0:
            raise ValueError("ranked sector identity is invalid")
        if self.reason_codes != tuple(dict.fromkeys(self.reason_codes)):
            raise ValueError("sector trigger reasons must be unique")
        if self.strength_observed_at is not None:
            object.__setattr__(
                self,
                "strength_observed_at",
                normalize_datetime(
                    self.strength_observed_at,
                    "strength_observed_at",
                ),
            )
        resolved = self.horizontal_strength is not None
        if resolved != (self.horizontal_rank is not None):
            raise ValueError("sector trigger strength and rank must resolve together")
        if resolved and (
            not self.horizontal_strength.is_finite()
            or self.horizontal_rank <= 0
            or self.strength_observed_at is None
            or self.strength_anchor_session is None
            or self.strength_member_count <= 0
            or not self.strength_source_revision
        ):
            raise ValueError("ranked sector strength provenance is incomplete")
        if self.strength_member_count < 0:
            raise ValueError("sector trigger strength member count is invalid")
        if self.strength_reason_codes != tuple(
            dict.fromkeys(self.strength_reason_codes)
        ):
            raise ValueError("sector trigger strength reasons must be unique")


@dataclass(frozen=True, slots=True)
class SectorFirstTriggerEvent:
    observed_at: datetime
    ranked_sectors: tuple[SectorTriggerRankFact, ...]
    hard_blocked_sector_ids: tuple[str, ...]
    missing_sector_ids: tuple[str, ...]
    candidate_symbol_count: int
    candidate_count_by_sector: tuple[tuple[str, int], ...]
    candidate_symbols_sha256: str
    sector_strength_evidence_revision: str | None = None
    unresolved_strength_sector_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if tuple(row.ordinal for row in self.ranked_sectors) != tuple(
            range(1, len(self.ranked_sectors) + 1)
        ):
            raise ValueError("sector trigger ranks must be contiguous")
        for field in ("hard_blocked_sector_ids", "missing_sector_ids"):
            values = tuple(getattr(self, field))
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field} must be unique and sorted")
        counts = tuple(self.candidate_count_by_sector)
        if counts != tuple(sorted(counts)) or any(value < 0 for _key, value in counts):
            raise ValueError("candidate sector counts must be sorted and non-negative")
        if self.candidate_symbol_count != sum(value for _key, value in counts):
            raise ValueError("candidate count does not match sector rows")
        if not self.candidate_symbols_sha256.startswith("sha256:"):
            raise ValueError("candidate symbols require a content identity")
        if self.sector_strength_evidence_revision is not None and not (
            self.sector_strength_evidence_revision.startswith("sha256:")
        ):
            raise ValueError("sector strength evidence requires a content identity")
        if self.unresolved_strength_sector_ids != tuple(
            sorted(set(self.unresolved_strength_sector_ids))
        ):
            raise ValueError("unresolved strength sector ids must be unique and sorted")


@dataclass(frozen=True, slots=True)
class SectorFirstTriggerLedger:
    algorithm_revision: str
    sector_scope_sha256: str
    pit_snapshot_sha256: str
    events: tuple[SectorFirstTriggerEvent, ...]
    sector_source_revisions: tuple[tuple[str, str], ...]
    selection_path: str = "INDIVIDUAL_THREE_PROGRAM"
    taxonomy: str = "SW1"
    source: str = "QMT_SW1_PIT"
    selection_order: tuple[str, ...] = (
        "POINT_IN_TIME_SECTOR_TRIGGER",
        "POINT_IN_TIME_SECTOR_MEMBERS",
        "INDIVIDUAL_THREE_PROGRAM",
        "MARKET_SECTOR_SYMBOL_HIGHER_TIMEFRAME_RISK",
        "DIRECT_RECURSIVE_30M_5M_1M_TECHNICAL_ENTRY",
    )
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        times = tuple(row.observed_at for row in self.events)
        if times != tuple(sorted(set(times))):
            raise ValueError("sector trigger events must be unique and chronological")
        if self.selection_path not in {
            "INDIVIDUAL_THREE_PROGRAM",
            RECENT_YEAR_SELECTION_PATH,
        }:
            raise ValueError("sector-first ledger selection path is unsupported")
        if self.selection_path == "INDIVIDUAL_THREE_PROGRAM":
            if self.selection_order[:2] != (
                "POINT_IN_TIME_SECTOR_TRIGGER",
                "POINT_IN_TIME_SECTOR_MEMBERS",
            ):
                raise ValueError("sector trigger must precede stock expansion")
        elif self.selection_order[:2] not in {
            _CURRENT_BACKFILL_PREFIX,
            _FORWARD_CAPTURE_PREFIX,
        }:
            raise ValueError("sector trigger must precede stock expansion")
        revisions = tuple(self.sector_source_revisions)
        if revisions != tuple(sorted(revisions)):
            raise ValueError("sector source revisions must be sorted")

    def document(self) -> dict[str, object]:
        stable: dict[str, object] = {
            "schema": SECTOR_FIRST_TRIGGER_SCHEMA,
            **_document_dates(asdict(self)),
            "counts": {
                "event_count": len(self.events),
                "ever_triggered_sector_count": len(
                    {
                        row.sector_id
                        for event in self.events
                        for row in event.ranked_sectors
                    }
                ),
                "maximum_candidate_symbol_count": max(
                    (event.candidate_symbol_count for event in self.events),
                    default=0,
                ),
                "minimum_candidate_symbol_count": min(
                    (event.candidate_symbol_count for event in self.events),
                    default=0,
                ),
            },
        }
        return {**stable, "content_sha256": sha256_json(stable)}


def _assessments_at(
    sector_facts: Mapping[str, SectorResearchFacts],
) -> dict[datetime, dict[str, SectorAssessment]]:
    output: dict[datetime, dict[str, SectorAssessment]] = {}
    for sector_id, facts in sector_facts.items():
        if facts.sector_id != sector_id:
            raise ValueError("sector fact map identity mismatch")
        for observed_at, assessment in facts.assessments:
            previous = output.setdefault(observed_at, {}).setdefault(
                sector_id,
                assessment,
            )
            if previous != assessment:
                raise ValueError("conflicting sector assessment at one decision time")
    return output


def _strength_batch_time(batch: SectorStrengthBatch) -> datetime:
    times = tuple(value.observed_at for value in batch.values())
    if not times or len(set(times)) != 1:
        raise ValueError("sector strength batch must share one observation time")
    return times[0]


def _strength_batches(
    values: Sequence[SectorStrengthBatch],
) -> tuple[tuple[datetime, SectorStrengthBatch], ...]:
    output = tuple(
        sorted(
            ((_strength_batch_time(value), value) for value in values),
            key=lambda row: row[0],
        )
    )
    times = tuple(value[0] for value in output)
    if times != tuple(sorted(set(times))):
        raise ValueError("sector strength batches must be unique and chronological")
    return output


def _visible_strength_batch(
    values: tuple[tuple[datetime, SectorStrengthBatch], ...],
    observed_at: datetime,
) -> SectorStrengthBatch | None:
    visible = tuple(batch for known_at, batch in values if known_at <= observed_at)
    return None if not visible else visible[-1]


def _assessment_with_strength(
    assessment: SectorAssessment,
    evidence: SectorStrengthEvidence,
) -> SectorAssessment:
    if assessment.sector_id != evidence.sector_id:
        raise ValueError("sector strength crossed an assessment identity")
    return replace(
        assessment,
        horizontal_strength=evidence.strength,
        horizontal_rank=evidence.rank,
        strength_anchor_session=evidence.anchor_session,
        strength_member_count=evidence.member_count,
        strength_source_revision=evidence.source_revision,
        strength_reason_codes=evidence.reason_codes,
    )


def _ranked_sector_facts(
    *,
    available: Mapping[str, SectorAssessment],
    strength_batch: SectorStrengthBatch | None,
    require_strength: bool,
) -> tuple[tuple[SectorTriggerRankFact, ...], tuple[str, ...]]:
    strength_by_sector = {} if strength_batch is None else dict(strength_batch)
    assessments = tuple(
        _assessment_with_strength(value, strength_by_sector[sector_id])
        if sector_id in strength_by_sector
        else value
        for sector_id, value in sorted(available.items())
    )
    unresolved = tuple(
        sorted(
            value.sector_id
            for value in assessments
            if value.eligible and value.horizontal_strength is None
        )
    )
    eligible = tuple(
        value
        for value in assessments
        if not require_strength or value.horizontal_strength is not None
    )
    ranked = rank_sectors(eligible)
    facts = tuple(
        SectorTriggerRankFact(
            sector_id=row.assessment.sector_id,
            sector_name=row.assessment.sector_name,
            ordinal=row.ordinal,
            rank_score=row.assessment.rank_score,
            regime=row.assessment.regime,
            reason_codes=row.assessment.reason_codes,
            horizontal_strength=row.assessment.horizontal_strength,
            horizontal_rank=row.assessment.horizontal_rank,
            strength_observed_at=(
                None
                if row.assessment.horizontal_strength is None
                else strength_by_sector[row.assessment.sector_id].observed_at
            ),
            strength_anchor_session=row.assessment.strength_anchor_session,
            strength_member_count=row.assessment.strength_member_count,
            strength_source_revision=row.assessment.strength_source_revision,
            strength_reason_codes=row.assessment.strength_reason_codes,
        )
        for row in ranked
    )
    return facts, unresolved


def _candidate_symbols_at(
    *,
    index: PITMetadataIndex,
    selected_securities: Sequence[SecurityMasterRecord],
    observed_at: datetime,
    eligible_sector_ids: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    output: list[tuple[str, str]] = []
    for security in selected_securities:
        if not security.listed_on(observed_at.date()):
            continue
        membership = index.membership_at(security.code, observed_at)
        if membership is not None and membership.sector_id in eligible_sector_ids:
            output.append((security.code, membership.sector_id))
    return tuple(sorted(output))


def build_sector_first_trigger_ledger(
    *,
    snapshot: PITMetadataSnapshot,
    scope: SectorFirstScope,
    sector_facts: Mapping[str, SectorResearchFacts],
    observed_times: Sequence[datetime],
    algorithm_revision: str,
    pit_snapshot_sha256: str,
) -> SectorFirstTriggerLedger:
    """Rank sectors first, then expand only their point-in-time members."""

    if scope.source_hashes != snapshot.source_hashes:
        raise ValueError("sector scope and PIT snapshot do not share a source")
    names = dict(snapshot.qmt_sw1_sector_names)
    if set(sector_facts) - set(names):
        raise ValueError("sector facts contain an unknown SW1 sector")
    by_time = _assessments_at(sector_facts)
    index = PITMetadataIndex(snapshot)
    selected = set(scope.selected_symbols)
    selected_securities = tuple(
        row for row in snapshot.securities if row.code in selected
    )
    events: list[SectorFirstTriggerEvent] = []
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "observed_at")
        available = by_time.get(observed_at, {})
        ranked = rank_sectors(tuple(available.values()))
        ranked_facts = tuple(
            SectorTriggerRankFact(
                sector_id=row.assessment.sector_id,
                sector_name=row.assessment.sector_name,
                ordinal=row.ordinal,
                rank_score=row.assessment.rank_score,
                regime=row.assessment.regime,
                reason_codes=row.assessment.reason_codes,
            )
            for row in ranked
        )
        eligible_ids = frozenset(row.sector_id for row in ranked_facts)
        candidates = _candidate_symbols_at(
            index=index,
            selected_securities=selected_securities,
            observed_at=observed_at,
            eligible_sector_ids=eligible_ids,
        )
        counts: dict[str, int] = {}
        for _code, sector_id in candidates:
            counts[sector_id] = counts.get(sector_id, 0) + 1
        events.append(
            SectorFirstTriggerEvent(
                observed_at=observed_at,
                ranked_sectors=ranked_facts,
                hard_blocked_sector_ids=tuple(
                    sorted(
                        sector_id
                        for sector_id, assessment in available.items()
                        if assessment.hard_block
                    )
                ),
                missing_sector_ids=tuple(sorted(set(names) - set(available))),
                candidate_symbol_count=len(candidates),
                candidate_count_by_sector=tuple(sorted(counts.items())),
                candidate_symbols_sha256=sha256_json(
                    {
                        "schema": "chanlun-v3-sector-first-candidates/v1",
                        "observed_at": observed_at,
                        "symbols": candidates,
                    }
                ),
            )
        )
    return SectorFirstTriggerLedger(
        algorithm_revision=algorithm_revision,
        sector_scope_sha256=scope.content_sha256,
        pit_snapshot_sha256=pit_snapshot_sha256,
        events=tuple(events),
        sector_source_revisions=tuple(
            sorted(
                (sector_id, facts.source_revision)
                for sector_id, facts in sector_facts.items()
            )
        ),
    )


def build_current_sector_trigger_ledger(
    *,
    sector_facts: Mapping[str, SectorResearchFacts],
    sector_members: Mapping[str, Sequence[str]],
    securities: Sequence[SecurityMasterRecord],
    observed_times: Sequence[datetime],
    algorithm_revision: str,
    catalog_entry_sha256: str,
    security_snapshot_sha256: str,
    membership_mode: str = CURRENT_BACKFILL_MEMBERSHIP_MODE,
    sector_strength_batches: Sequence[SectorStrengthBatch] = (),
) -> SectorFirstTriggerLedger:
    """Rank QMT GICS3 sectors, then expand their current captured members.

    ``CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED`` is the explicitly biased
    historical replay.  ``FORWARD_SAME_SESSION_CAPTURE`` is only valid for
    events on the catalog's actual capture session; the caller enforces that
    date/time binding.  Listing/expiry dates are evaluated at every event.
    """

    if membership_mode not in {
        CURRENT_BACKFILL_MEMBERSHIP_MODE,
        FORWARD_SAME_SESSION_MEMBERSHIP_MODE,
    }:
        raise ValueError("unsupported current-sector membership mode")

    if set(sector_facts) != set(sector_members):
        raise ValueError("current sector facts and membership catalog disagree")
    if not catalog_entry_sha256.startswith("sha256:"):
        raise ValueError("current sector capture identity is required")
    if not security_snapshot_sha256.startswith("sha256:"):
        raise ValueError("security snapshot identity is required")
    security_by_code = {row.code: row for row in securities}
    if len(security_by_code) != len(securities):
        raise ValueError("security master contains duplicate codes")
    normalized_members = {
        sector_id: tuple(sorted(set(members)))
        for sector_id, members in sector_members.items()
    }
    if any(
        len(values) != len(tuple(sector_members[sector_id]))
        for sector_id, values in normalized_members.items()
    ):
        raise ValueError("current sector member lists must be unique")
    by_time = _assessments_at(sector_facts)
    strength_values = _strength_batches(sector_strength_batches)
    require_strength = bool(strength_values)
    events: list[SectorFirstTriggerEvent] = []
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "observed_at")
        available = by_time.get(observed_at, {})
        strength_batch = _visible_strength_batch(strength_values, observed_at)
        ranked_facts, unresolved_strength = _ranked_sector_facts(
            available=available,
            strength_batch=strength_batch,
            require_strength=require_strength,
        )
        candidates: list[tuple[str, str]] = []
        for ranked_row in ranked_facts:
            for code in normalized_members[ranked_row.sector_id]:
                security = security_by_code.get(code)
                if security is not None and security.listed_on(observed_at.date()):
                    candidates.append((code, ranked_row.sector_id))
        candidates.sort()
        counts: dict[str, int] = {}
        for _code, sector_id in candidates:
            counts[sector_id] = counts.get(sector_id, 0) + 1
        events.append(
            SectorFirstTriggerEvent(
                observed_at=observed_at,
                ranked_sectors=ranked_facts,
                hard_blocked_sector_ids=tuple(
                    sorted(
                        sector_id
                        for sector_id, assessment in available.items()
                        if assessment.hard_block
                    )
                ),
                missing_sector_ids=tuple(
                    sorted(set(normalized_members) - set(available))
                ),
                candidate_symbol_count=len(candidates),
                candidate_count_by_sector=tuple(sorted(counts.items())),
                candidate_symbols_sha256=sha256_json(
                    {
                        "schema": (
                            "chanlun-v3-forward-sector-candidates/v1"
                            if membership_mode == FORWARD_SAME_SESSION_MEMBERSHIP_MODE
                            else "chanlun-v3-current-sector-candidates/v1"
                        ),
                        "observed_at": observed_at,
                        "catalog_entry_sha256": catalog_entry_sha256,
                        "symbols": tuple(candidates),
                    }
                ),
                sector_strength_evidence_revision=(
                    None
                    if strength_batch is None
                    else strength_batch.evidence_revision
                ),
                unresolved_strength_sector_ids=unresolved_strength,
            )
        )
    return SectorFirstTriggerLedger(
        algorithm_revision=algorithm_revision,
        sector_scope_sha256=catalog_entry_sha256,
        pit_snapshot_sha256=security_snapshot_sha256,
        events=tuple(events),
        sector_source_revisions=tuple(
            sorted(
                (sector_id, facts.source_revision)
                for sector_id, facts in sector_facts.items()
            )
        ),
        selection_path=RECENT_YEAR_SELECTION_PATH,
        taxonomy=(
            "QMT_GICS3_FORWARD_PIT"
            if membership_mode == FORWARD_SAME_SESSION_MEMBERSHIP_MODE
            else "QMT_GICS3_CURRENT_BACKFILL"
        ),
        source=(
            "QMT_GICS3_SAME_SESSION_CAPTURE_AND_LOCAL_BARS"
            if membership_mode == FORWARD_SAME_SESSION_MEMBERSHIP_MODE
            else "QMT_GICS3_CURRENT_CAPTURE_AND_LOCAL_BARS"
        ),
        selection_order=(
            *(
                _FORWARD_CAPTURE_PREFIX
                if membership_mode == FORWARD_SAME_SESSION_MEMBERSHIP_MODE
                else _CURRENT_BACKFILL_PREFIX
            ),
            "MARKET_SECTOR_SYMBOL_HIGHER_TIMEFRAME_RISK",
            "DIRECT_RECURSIVE_30M_5M_1M_TECHNICAL_ENTRY",
        ),
    )


def sector_trigger_windows_for_symbol(
    *,
    ledger: SectorFirstTriggerLedger,
    snapshot: PITMetadataSnapshot,
    code: str,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return periods in which the symbol may be expanded from its sector.

    A sector decision becomes usable at its completed 30-minute timestamp and
    remains current until the next completed sector decision.  Membership is
    resolved at the start of every such interval; a new membership therefore
    cannot inherit the previous sector's trigger.
    """

    if not ledger.events:
        return ()
    index = PITMetadataIndex(snapshot)
    security = index.security(code)
    return sector_trigger_windows_for_memberships(
        ledger=ledger,
        security=security,
        memberships=index.memberships_for(code),
    )


def sector_trigger_windows_for_memberships(
    *,
    ledger: SectorFirstTriggerLedger,
    security: SecurityMasterRecord,
    memberships: Sequence[SectorMembershipChange],
) -> tuple[tuple[datetime, datetime], ...]:
    """Resolve trigger windows from one symbol's immutable PIT rows."""

    if any(row.code != security.code for row in memberships):
        raise ValueError("trigger-window membership crossed a symbol identity")
    ordered_memberships = tuple(
        sorted(memberships, key=lambda row: (row.known_at, row.sector_id))
    )

    def membership_at(observed_at: datetime) -> SectorMembershipChange | None:
        available = tuple(
            row for row in ordered_memberships if row.known_at <= observed_at
        )
        return None if not available else available[-1]

    windows: list[tuple[datetime, datetime]] = []
    microsecond = timedelta(microseconds=1)
    for position, event in enumerate(ledger.events):
        start = event.observed_at
        end = (
            ledger.events[position + 1].observed_at - microsecond
            if position + 1 < len(ledger.events)
            else event.observed_at
        )
        membership = membership_at(start)
        eligible = {
            row.sector_id for row in event.ranked_sectors
        }
        if (
            not security.listed_on(start.date())
            or membership is None
            or membership.sector_id not in eligible
        ):
            continue
        if windows and start <= windows[-1][1] + microsecond:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return tuple(windows)


def sector_trigger_windows_for_current_member(
    *,
    ledger: SectorFirstTriggerLedger,
    security: SecurityMasterRecord,
    sector_id: str,
) -> tuple[tuple[datetime, datetime], ...]:
    """Resolve windows with one explicitly backfilled current sector."""

    if ledger.selection_path != RECENT_YEAR_SELECTION_PATH:
        raise ValueError("current-member windows require the recent-year ledger")
    if not sector_id:
        raise ValueError("current sector identity is required")
    windows: list[tuple[datetime, datetime]] = []
    microsecond = timedelta(microseconds=1)
    for position, event in enumerate(ledger.events):
        start = event.observed_at
        end = (
            ledger.events[position + 1].observed_at - microsecond
            if position + 1 < len(ledger.events)
            else event.observed_at
        )
        eligible = {row.sector_id for row in event.ranked_sectors}
        if not security.listed_on(start.date()) or sector_id not in eligible:
            continue
        if windows and start <= windows[-1][1] + microsecond:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return tuple(windows)


__all__ = (
    "CURRENT_BACKFILL_MEMBERSHIP_MODE",
    "FORWARD_SAME_SESSION_MEMBERSHIP_MODE",
    "SECTOR_FIRST_TRIGGER_SCHEMA",
    "SectorFirstTriggerEvent",
    "SectorFirstTriggerLedger",
    "SectorTriggerRankFact",
    "build_sector_first_trigger_ledger",
    "build_current_sector_trigger_ledger",
    "sector_trigger_windows_for_current_member",
    "sector_trigger_windows_for_memberships",
    "sector_trigger_windows_for_symbol",
)
