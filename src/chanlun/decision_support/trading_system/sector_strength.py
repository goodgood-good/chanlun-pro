"""Auditable horizontal sector strength built from one common daily anchor."""

from __future__ import annotations

from collections.abc import Iterator, Mapping as MappingABC
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    DailyMarketBar,
    latest_completed_bottom_fractal_anchor,
)
from chanlun.decision_support.trading_system.v3_selection import (
    SectorMemberHistory,
    SectorStrengthSnapshot,
    build_sector_strength_snapshot,
)


MIN_MEMBER_HISTORY_COVERAGE = Decimal("1")
SECTOR_STRENGTH_EVIDENCE_SCHEMA = (
    "chanlun-horizontal-sector-strength-evidence/v3"
)
SECTOR_MEMBER_HISTORY_DIAGNOSTICS_SCHEMA = (
    "chanlun-sector-member-history-diagnostics/v1"
)
_MEMBER_HISTORY_STATUS_ORDER = (
    "COMPLETE",
    "NEW_LISTING",
    "SUSPENDED",
    "UNEXPLAINED_GAP",
)
_MEMBER_HISTORY_STATUSES = frozenset(_MEMBER_HISTORY_STATUS_ORDER)


@dataclass(frozen=True, slots=True)
class SectorStrengthEvidence:
    sector_id: str
    observed_at: datetime
    anchor_session: date | None
    member_count: int
    strength: Decimal | None
    rank: int | None
    source_revision: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.sector_id or not self.source_revision:
            raise ValueError("sector strength provenance is required")
        if self.member_count < 0:
            raise ValueError("sector strength member_count cannot be negative")
        if (self.strength is None) != (self.rank is None):
            raise ValueError("sector strength and rank must resolve together")
        if self.strength is not None and (
            self.anchor_session is None or self.member_count <= 0 or self.rank <= 0
        ):
            raise ValueError("resolved sector strength is incomplete")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("sector strength reason codes must be unique")

    @property
    def resolved(self) -> bool:
        return self.strength is not None


@dataclass(frozen=True, slots=True)
class SectorMemberCategoryFact:
    """One replay-efficient member category on the shared §6.3 contract.

    The page path owns full daily histories and derives this fact on demand.
    Historical replay can derive the same strict ``close > SMA`` result once
    per completed daily cutoff, then reuse it at every intraday decision until
    a newer daily close becomes visible.  ``UNEXPLAINED_GAP`` deliberately has
    no category; all other statuses remain in the equal-weight denominator.
    """

    symbol: str
    history_status: str
    category: int | None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("sector member category symbol is required")
        if self.history_status not in _MEMBER_HISTORY_STATUSES:
            raise ValueError("sector member category history status is invalid")
        if self.history_status == "UNEXPLAINED_GAP":
            if self.category is not None:
                raise ValueError("unexplained member history cannot have a category")
        elif self.category is None or not 1 <= self.category <= 9:
            raise ValueError("resolved member category must be in [1, 9]")


@dataclass(frozen=True, slots=True)
class SectorStrengthBatch(MappingABC[str, SectorStrengthEvidence]):
    """One cross-sector ranking plus its canonical recomputation evidence.

    The evidence intentionally contains member categories rather than the full
    daily bar history.  It is therefore a compact derived-fact boundary: a
    consumer can independently recompute every sector mean, cross-sector rank
    and per-sector ``source_revision`` without duplicating hundreds of daily
    bars into every page candidate.  The benchmark daily-bar revision remains
    an explicit upstream fact identity.
    """

    strengths: tuple[SectorStrengthEvidence, ...]
    evidence_json: str

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.strengths, key=lambda value: value.sector_id))
        if ordered != self.strengths or len({row.sector_id for row in ordered}) != len(
            ordered
        ):
            raise ValueError("sector strength batch must be unique and sorted")
        try:
            document = json.loads(self.evidence_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("sector strength evidence JSON is invalid") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema") != SECTOR_STRENGTH_EVIDENCE_SCHEMA
            or self.evidence_json
            != json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        ):
            raise ValueError("sector strength evidence JSON is not canonical")

    def __getitem__(self, key: str) -> SectorStrengthEvidence:
        for value in self.strengths:
            if value.sector_id == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (value.sector_id for value in self.strengths)

    def __len__(self) -> int:
        return len(self.strengths)

    @property
    def evidence_revision(self) -> str:
        return sha256_json(self.evidence_document())

    def evidence_document(self) -> dict[str, object]:
        value = json.loads(self.evidence_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded in init
            raise ValueError("sector strength evidence document is invalid")
        return value


def _canonical_evidence_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _build_horizontal_sector_strength(
    *,
    decision_time: datetime,
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, tuple[SectorMemberHistory, ...]],
    membership_revision: str,
) -> tuple[dict[str, SectorStrengthEvidence], dict[str, object]]:
    """Rank sectors by equal-weight member MA categories from one anchor.

    The category definition is the existing V3 rule (MA5/13/21/34/55/89/
    144/233 conquered since the latest completed broad-market daily bottom
    fractal).  Missing histories are never synthesized or removed from the
    denominator.  A sector is resolved only when every current QMT member has
    verifiable history.  One unexplained member gap makes the whole sector
    unresolved; short but explained histories remain category 1 members.
    """

    observed = normalize_datetime(decision_time, "decision_time")
    anchor = latest_completed_bottom_fractal_anchor(
        tuple(benchmark_daily),
        decision_time=observed,
        symbol=benchmark_symbol,
    )
    source_base = {
        "schema": "chanlun-horizontal-sector-strength/v4",
        "decision_time": observed,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_source_revision": anchor.source_revision,
        "membership_revision": membership_revision,
        "minimum_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
    }
    evidence_base: dict[str, object] = {
        "schema": SECTOR_STRENGTH_EVIDENCE_SCHEMA,
        "decision_time": observed.isoformat(),
        "benchmark_symbol": benchmark_symbol,
        "benchmark_source_revision": anchor.source_revision,
        "membership_revision": membership_revision,
        "minimum_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
        "anchor_session": (
            None if anchor.anchor_session is None else anchor.anchor_session.isoformat()
        ),
    }
    if not anchor.resolved or anchor.anchor_session is None:
        reasons = tuple(
            dict.fromkeys(
                [
                    "BROAD_MARKET_DAILY_ANCHOR_UNRESOLVED",
                    *(value.code for value in anchor.blockers),
                ]
            )
        )
        output = {
            sector_id: SectorStrengthEvidence(
                sector_id=sector_id,
                observed_at=observed,
                anchor_session=None,
                member_count=len(members),
                strength=None,
                rank=None,
                source_revision=sha256_json(
                    {
                        **source_base,
                        "sector_id": sector_id,
                        "member_history_statuses": tuple(
                            (member.symbol, member.history_status)
                            for member in members
                        ),
                    }
                ),
                reason_codes=reasons,
            )
            for sector_id, members in sorted(members_by_sector.items())
        }
        evidence_base["anchor_reason_codes"] = list(reasons)
        evidence_base["sectors"] = [
            {
                "sector_id": sector_id,
                "member_symbols": [member.symbol for member in members],
                "member_history_statuses": [
                    [member.symbol, member.history_status] for member in members
                ],
                "total_member_count": len(members),
                "usable_member_count": sum(
                    member.history_status != "UNEXPLAINED_GAP"
                    for member in members
                ),
                "missing_members": [
                    member.symbol
                    for member in members
                    if member.history_status == "UNEXPLAINED_GAP"
                ],
                "categories": [],
                "strength": None,
                "rank": None,
                "unresolved_reasons": list(reasons),
                "member_count": len(members),
                "reason_codes": list(reasons),
                "source_revision": output[sector_id].source_revision,
            }
            for sector_id, members in sorted(members_by_sector.items())
        ]
        return output, evidence_base

    provisional: dict[str, SectorStrengthSnapshot] = {}
    coverage: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    for sector_id, members in sorted(members_by_sector.items()):
        usable = tuple(
            member
            for member in members
            if member.history_status != "UNEXPLAINED_GAP"
        )
        missing = tuple(
            member.symbol
            for member in members
            if member.history_status == "UNEXPLAINED_GAP"
        )
        coverage[sector_id] = (len(usable), len(members), missing)
        provisional[sector_id] = build_sector_strength_snapshot(
            snapshot_id=sha256_json(
                {
                    **source_base,
                    "sector_id": sector_id,
                    "anchor_session": anchor.anchor_session.isoformat(),
                    "members": tuple(member.symbol for member in members),
                    "member_history_statuses": tuple(
                        (member.symbol, member.history_status)
                        for member in members
                    ),
                    "missing_members": missing,
                }
            ),
            sector_id=sector_id,
            anchor_session=anchor.anchor_session,
            decision_time=observed,
            # §6.3: all point-in-time members stay in the denominator.  The
            # snapshot therefore fails closed when even one member has an
            # unexplained history gap instead of ranking a selected subset.
            members=members,
            # A temporary positive value satisfies the immutable snapshot;
            # the cross-sector rank is assigned below from all resolved values.
            rank=1,
        )
    ordered = sorted(
        (value for value in provisional.values() if value.resolved),
        key=lambda value: (-value.strength, value.sector_id),
    )
    ranked = {
        value.sector_id: replace(value, rank=ordinal)
        for ordinal, value in enumerate(ordered, start=1)
    }
    output: dict[str, SectorStrengthEvidence] = {}
    audit_rows: list[dict[str, object]] = []
    for sector_id, snapshot in provisional.items():
        final = ranked.get(sector_id, snapshot)
        usable_count, total_count, missing = coverage[sector_id]
        reasons = (
            tuple(final.unresolved_reasons)
            if final.unresolved_reasons
            else (
                "CURRENT_QMT_MEMBERSHIP_AUTHORIZED",
                "COMMON_BROAD_MARKET_DAILY_BOTTOM_FRACTAL_ANCHOR",
                "EQUAL_WEIGHT_MEMBER_MA_CATEGORY_MEAN",
            )
        )
        source_revision = sha256_json(
            {
                **source_base,
                "sector_id": sector_id,
                "anchor_session": anchor.anchor_session.isoformat(),
                "member_history_statuses": tuple(
                    (member.symbol, member.history_status)
                    for member in members_by_sector[sector_id]
                ),
                "categories": final.categories,
                "total_member_count": total_count,
                "usable_member_count": usable_count,
                "missing_members": missing,
                "strength": final.strength,
                "rank": final.rank,
                "unresolved_reasons": final.unresolved_reasons,
            }
        )
        output[sector_id] = SectorStrengthEvidence(
            sector_id=sector_id,
            observed_at=observed,
            anchor_session=anchor.anchor_session,
            member_count=final.member_count,
            strength=final.strength,
            rank=final.rank,
            source_revision=source_revision,
            reason_codes=reasons,
        )
        audit_rows.append(
            {
                "sector_id": sector_id,
                "member_symbols": [
                    member.symbol for member in members_by_sector[sector_id]
                ],
                "member_history_statuses": [
                    [member.symbol, member.history_status]
                    for member in members_by_sector[sector_id]
                ],
                "total_member_count": total_count,
                "usable_member_count": usable_count,
                "missing_members": list(missing),
                "categories": [list(value) for value in final.categories],
                "strength": (
                    None if final.strength is None else str(final.strength)
                ),
                "rank": final.rank,
                "unresolved_reasons": list(final.unresolved_reasons),
                "member_count": final.member_count,
                "reason_codes": list(reasons),
                "source_revision": source_revision,
            }
        )
    evidence_base["anchor_reason_codes"] = []
    evidence_base["sectors"] = audit_rows
    return output, evidence_base


def _build_horizontal_sector_strength_from_categories(
    *,
    decision_time: datetime,
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, tuple[SectorMemberCategoryFact, ...]],
    membership_revision: str,
) -> tuple[dict[str, SectorStrengthEvidence], dict[str, object]]:
    """Replay-efficient twin of the full-history strength calculation.

    Category facts are still combined, ranked and hashed here, so historical
    replay and the page cannot drift into two sector-ranking policies.  The
    caller may optimize only the mechanical ``close > SMA`` prefix scan.
    """

    observed = normalize_datetime(decision_time, "decision_time")
    anchor = latest_completed_bottom_fractal_anchor(
        tuple(benchmark_daily),
        decision_time=observed,
        symbol=benchmark_symbol,
    )
    source_base = {
        "schema": "chanlun-horizontal-sector-strength/v4",
        "decision_time": observed,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_source_revision": anchor.source_revision,
        "membership_revision": membership_revision,
        "minimum_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
    }
    evidence_base: dict[str, object] = {
        "schema": SECTOR_STRENGTH_EVIDENCE_SCHEMA,
        "decision_time": observed.isoformat(),
        "benchmark_symbol": benchmark_symbol,
        "benchmark_source_revision": anchor.source_revision,
        "membership_revision": membership_revision,
        "minimum_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
        "anchor_session": (
            None if anchor.anchor_session is None else anchor.anchor_session.isoformat()
        ),
    }
    for sector_id, members in members_by_sector.items():
        symbols = tuple(member.symbol for member in members)
        if not sector_id or symbols != tuple(sorted(set(symbols))):
            raise ValueError("sector category facts must be unique and sorted")
    if not anchor.resolved or anchor.anchor_session is None:
        reasons = tuple(
            dict.fromkeys(
                [
                    "BROAD_MARKET_DAILY_ANCHOR_UNRESOLVED",
                    *(value.code for value in anchor.blockers),
                ]
            )
        )
        output = {
            sector_id: SectorStrengthEvidence(
                sector_id=sector_id,
                observed_at=observed,
                anchor_session=None,
                member_count=len(members),
                strength=None,
                rank=None,
                source_revision=sha256_json(
                    {
                        **source_base,
                        "sector_id": sector_id,
                        "member_history_statuses": tuple(
                            (member.symbol, member.history_status)
                            for member in members
                        ),
                    }
                ),
                reason_codes=reasons,
            )
            for sector_id, members in sorted(members_by_sector.items())
        }
        evidence_base["anchor_reason_codes"] = list(reasons)
        evidence_base["sectors"] = [
            {
                "sector_id": sector_id,
                "member_symbols": [member.symbol for member in members],
                "member_history_statuses": [
                    [member.symbol, member.history_status] for member in members
                ],
                "total_member_count": len(members),
                "usable_member_count": sum(
                    member.history_status != "UNEXPLAINED_GAP"
                    for member in members
                ),
                "missing_members": [
                    member.symbol
                    for member in members
                    if member.history_status == "UNEXPLAINED_GAP"
                ],
                "categories": [],
                "strength": None,
                "rank": None,
                "unresolved_reasons": list(reasons),
                "member_count": len(members),
                "reason_codes": list(reasons),
                "source_revision": output[sector_id].source_revision,
            }
            for sector_id, members in sorted(members_by_sector.items())
        ]
        return output, evidence_base

    provisional: dict[str, SectorStrengthSnapshot] = {}
    coverage: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    for sector_id, members in sorted(members_by_sector.items()):
        missing = tuple(
            member.symbol
            for member in members
            if member.history_status == "UNEXPLAINED_GAP"
        )
        unresolved = tuple(
            f"UNEXPLAINED_MEMBER_HISTORY:{symbol}" for symbol in missing
        )
        if not members:
            unresolved = (*unresolved, "EMPTY_POINT_IN_TIME_BASKET")
        categories = (
            tuple((member.symbol, 1) for member in members)
            if unresolved
            else tuple((member.symbol, int(member.category)) for member in members)
        )
        strength = (
            None
            if unresolved
            else sum(
                (Decimal(category) for _symbol, category in categories),
                Decimal("0"),
            )
            / Decimal(len(categories))
        )
        coverage[sector_id] = (len(members) - len(missing), len(members), missing)
        provisional[sector_id] = SectorStrengthSnapshot(
            snapshot_id=sha256_json(
                {
                    **source_base,
                    "sector_id": sector_id,
                    "anchor_session": anchor.anchor_session.isoformat(),
                    "members": tuple(member.symbol for member in members),
                    "member_history_statuses": tuple(
                        (member.symbol, member.history_status)
                        for member in members
                    ),
                    "missing_members": missing,
                }
            ),
            sector_id=sector_id,
            observed_at=observed,
            anchor_session=anchor.anchor_session,
            member_count=len(members),
            categories=categories,
            strength=strength,
            rank=None if unresolved else 1,
            unresolved_reasons=unresolved,
        )
    ordered = sorted(
        (value for value in provisional.values() if value.resolved),
        key=lambda value: (-value.strength, value.sector_id),
    )
    ranked = {
        value.sector_id: replace(value, rank=ordinal)
        for ordinal, value in enumerate(ordered, start=1)
    }
    output: dict[str, SectorStrengthEvidence] = {}
    audit_rows: list[dict[str, object]] = []
    for sector_id, snapshot in provisional.items():
        final = ranked.get(sector_id, snapshot)
        usable_count, total_count, missing = coverage[sector_id]
        reasons = (
            tuple(final.unresolved_reasons)
            if final.unresolved_reasons
            else (
                "CURRENT_QMT_MEMBERSHIP_AUTHORIZED",
                "COMMON_BROAD_MARKET_DAILY_BOTTOM_FRACTAL_ANCHOR",
                "EQUAL_WEIGHT_MEMBER_MA_CATEGORY_MEAN",
            )
        )
        source_revision = sha256_json(
            {
                **source_base,
                "sector_id": sector_id,
                "anchor_session": anchor.anchor_session.isoformat(),
                "member_history_statuses": tuple(
                    (member.symbol, member.history_status)
                    for member in members_by_sector[sector_id]
                ),
                "categories": final.categories,
                "total_member_count": total_count,
                "usable_member_count": usable_count,
                "missing_members": missing,
                "strength": final.strength,
                "rank": final.rank,
                "unresolved_reasons": final.unresolved_reasons,
            }
        )
        output[sector_id] = SectorStrengthEvidence(
            sector_id=sector_id,
            observed_at=observed,
            anchor_session=anchor.anchor_session,
            member_count=final.member_count,
            strength=final.strength,
            rank=final.rank,
            source_revision=source_revision,
            reason_codes=reasons,
        )
        audit_rows.append(
            {
                "sector_id": sector_id,
                "member_symbols": [
                    member.symbol for member in members_by_sector[sector_id]
                ],
                "member_history_statuses": [
                    [member.symbol, member.history_status]
                    for member in members_by_sector[sector_id]
                ],
                "total_member_count": total_count,
                "usable_member_count": usable_count,
                "missing_members": list(missing),
                "categories": [list(value) for value in final.categories],
                "strength": (
                    None if final.strength is None else str(final.strength)
                ),
                "rank": final.rank,
                "unresolved_reasons": list(final.unresolved_reasons),
                "member_count": final.member_count,
                "reason_codes": list(reasons),
                "source_revision": source_revision,
            }
        )
    evidence_base["anchor_reason_codes"] = []
    evidence_base["sectors"] = audit_rows
    return output, evidence_base


def build_horizontal_sector_strength_batch_from_categories(
    *,
    decision_time: datetime,
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, tuple[SectorMemberCategoryFact, ...]],
    membership_revision: str,
) -> SectorStrengthBatch:
    output, evidence = _build_horizontal_sector_strength_from_categories(
        decision_time=decision_time,
        benchmark_symbol=benchmark_symbol,
        benchmark_daily=benchmark_daily,
        members_by_sector=members_by_sector,
        membership_revision=membership_revision,
    )
    return SectorStrengthBatch(
        strengths=tuple(output[key] for key in sorted(output)),
        evidence_json=_canonical_evidence_json(evidence),
    )


def build_horizontal_sector_strength(
    *,
    decision_time: datetime,
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, tuple[SectorMemberHistory, ...]],
    membership_revision: str,
) -> dict[str, SectorStrengthEvidence]:
    output, _evidence = _build_horizontal_sector_strength(
        decision_time=decision_time,
        benchmark_symbol=benchmark_symbol,
        benchmark_daily=benchmark_daily,
        members_by_sector=members_by_sector,
        membership_revision=membership_revision,
    )
    return output


def build_horizontal_sector_strength_batch(
    *,
    decision_time: datetime,
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, tuple[SectorMemberHistory, ...]],
    membership_revision: str,
) -> SectorStrengthBatch:
    output, evidence = _build_horizontal_sector_strength(
        decision_time=decision_time,
        benchmark_symbol=benchmark_symbol,
        benchmark_daily=benchmark_daily,
        members_by_sector=members_by_sector,
        membership_revision=membership_revision,
    )
    return SectorStrengthBatch(
        strengths=tuple(output[key] for key in sorted(output)),
        evidence_json=_canonical_evidence_json(evidence),
    )


def _is_sha256_identity(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith(
        "sha256:"
    ):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def sector_strength_batch_from_evidence_document(
    value: object,
) -> SectorStrengthBatch:
    """Recompute every published strength fact from one compact batch document."""

    if not isinstance(value, Mapping):
        raise ValueError("sector strength evidence must be a mapping")
    required = {
        "schema",
        "decision_time",
        "benchmark_symbol",
        "benchmark_source_revision",
        "membership_revision",
        "minimum_member_history_coverage",
        "anchor_session",
        "anchor_reason_codes",
        "sectors",
    }
    if set(value) != required or value.get("schema") != SECTOR_STRENGTH_EVIDENCE_SCHEMA:
        raise ValueError("sector strength evidence contract is invalid")
    try:
        observed = normalize_datetime(
            datetime.fromisoformat(str(value["decision_time"])),
            "sector strength decision_time",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sector strength decision time is invalid") from exc
    benchmark_symbol = value.get("benchmark_symbol")
    benchmark_revision = value.get("benchmark_source_revision")
    membership_revision = value.get("membership_revision")
    if (
        not isinstance(benchmark_symbol, str)
        or not benchmark_symbol
        or not _is_sha256_identity(benchmark_revision)
        or not _is_sha256_identity(membership_revision)
        or value.get("minimum_member_history_coverage")
        != str(MIN_MEMBER_HISTORY_COVERAGE)
    ):
        raise ValueError("sector strength source identity is invalid")
    anchor_raw = value.get("anchor_session")
    try:
        anchor_session = (
            None if anchor_raw is None else date.fromisoformat(str(anchor_raw))
        )
    except ValueError as exc:
        raise ValueError("sector strength anchor session is invalid") from exc
    raw_anchor_reasons = value.get("anchor_reason_codes")
    if (
        not isinstance(raw_anchor_reasons, list)
        or any(not isinstance(item, str) or not item for item in raw_anchor_reasons)
        or len(raw_anchor_reasons) != len(set(raw_anchor_reasons))
        or (anchor_session is None) != bool(raw_anchor_reasons)
    ):
        raise ValueError("sector strength anchor evidence is invalid")
    anchor_reasons = tuple(raw_anchor_reasons)
    raw_sectors = value.get("sectors")
    if not isinstance(raw_sectors, list):
        raise ValueError("sector strength sector evidence is invalid")

    parsed: list[dict[str, object]] = []
    for raw in raw_sectors:
        if not isinstance(raw, Mapping):
            raise ValueError("sector strength sector evidence is invalid")
        row_required = {
            "sector_id",
            "member_symbols",
            "member_history_statuses",
            "total_member_count",
            "usable_member_count",
            "missing_members",
            "categories",
            "strength",
            "rank",
            "unresolved_reasons",
            "member_count",
            "reason_codes",
            "source_revision",
        }
        if set(raw) != row_required:
            raise ValueError("sector strength sector evidence is invalid")
        sector_id = raw.get("sector_id")
        member_symbols = raw.get("member_symbols")
        raw_member_statuses = raw.get("member_history_statuses")
        missing_members = raw.get("missing_members")
        raw_categories = raw.get("categories")
        unresolved = raw.get("unresolved_reasons")
        reasons = raw.get("reason_codes")
        if (
            not isinstance(sector_id, str)
            or not sector_id
            or not isinstance(member_symbols, list)
            or not isinstance(raw_member_statuses, list)
            or not isinstance(missing_members, list)
            or not isinstance(raw_categories, list)
            or not isinstance(unresolved, list)
            or not isinstance(reasons, list)
            or any(not isinstance(item, str) or not item for item in member_symbols)
            or member_symbols != sorted(set(member_symbols))
            or any(not isinstance(item, str) or not item for item in missing_members)
            or missing_members != sorted(set(missing_members))
            or not set(missing_members).issubset(member_symbols)
            or any(not isinstance(item, str) or not item for item in unresolved)
            or len(unresolved) != len(set(unresolved))
            or any(not isinstance(item, str) or not item for item in reasons)
            or len(reasons) != len(set(reasons))
            or not _is_sha256_identity(raw.get("source_revision"))
        ):
            raise ValueError("sector strength sector evidence is invalid")
        member_statuses: list[tuple[str, str]] = []
        for member_status in raw_member_statuses:
            if (
                not isinstance(member_status, list)
                or len(member_status) != 2
                or not isinstance(member_status[0], str)
                or member_status[0] not in member_symbols
                or member_status[1] not in _MEMBER_HISTORY_STATUSES
            ):
                raise ValueError("sector member history status evidence is invalid")
            member_statuses.append((member_status[0], member_status[1]))
        if (
            tuple(symbol for symbol, _status in member_statuses)
            != tuple(member_symbols)
            or tuple(
                symbol
                for symbol, status in member_statuses
                if status == "UNEXPLAINED_GAP"
            )
            != tuple(missing_members)
        ):
            raise ValueError("sector member history status evidence is inconsistent")
        categories: list[tuple[str, int]] = []
        for category in raw_categories:
            if (
                not isinstance(category, list)
                or len(category) != 2
                or not isinstance(category[0], str)
                or not category[0]
                or type(category[1]) is not int
                or not 1 <= category[1] <= 9
            ):
                raise ValueError("sector strength category evidence is invalid")
            categories.append((category[0], category[1]))
        if categories != sorted(set(categories)):
            raise ValueError("sector strength categories must be unique and sorted")
        try:
            total_count = int(raw.get("total_member_count"))
            usable_count = int(raw.get("usable_member_count"))
            member_count = int(raw.get("member_count"))
            strength = (
                None
                if raw.get("strength") is None
                else Decimal(str(raw.get("strength")))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("sector strength numeric evidence is invalid") from exc
        rank = raw.get("rank")
        if (
            min(total_count, usable_count, member_count) < 0
            or total_count != len(member_symbols)
            or usable_count > total_count
            or usable_count
            != sum(status != "UNEXPLAINED_GAP" for _symbol, status in member_statuses)
            or len(missing_members) != total_count - usable_count
            or strength is not None
            and not strength.is_finite()
            or (strength is None) != (rank is None)
            or rank is not None
            and (type(rank) is not int or rank <= 0)
        ):
            raise ValueError("sector strength numeric evidence is invalid")
        parsed.append(
            {
                "sector_id": sector_id,
                "member_symbols": tuple(member_symbols),
                "member_statuses": tuple(member_statuses),
                "total_count": total_count,
                "usable_count": usable_count,
                "missing": tuple(missing_members),
                "categories": tuple(categories),
                "strength": strength,
                "rank": rank,
                "unresolved": tuple(unresolved),
                "member_count": member_count,
                "reasons": tuple(reasons),
                "source_revision": raw["source_revision"],
            }
        )
    sector_ids = [str(row["sector_id"]) for row in parsed]
    if sector_ids != sorted(set(sector_ids)):
        raise ValueError("sector strength sectors must be unique and sorted")

    resolved_order = sorted(
        (row for row in parsed if row["strength"] is not None),
        key=lambda row: (-row["strength"], row["sector_id"]),  # type: ignore[operator]
    )
    expected_ranks = {
        str(row["sector_id"]): ordinal
        for ordinal, row in enumerate(resolved_order, start=1)
    }
    source_base = {
        "schema": "chanlun-horizontal-sector-strength/v4",
        "decision_time": observed,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_source_revision": benchmark_revision,
        "membership_revision": membership_revision,
        "minimum_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
    }
    outputs: list[SectorStrengthEvidence] = []
    for row in parsed:
        sector_id = str(row["sector_id"])
        total_count = int(row["total_count"])
        usable_count = int(row["usable_count"])
        member_symbols = row["member_symbols"]
        member_statuses = row["member_statuses"]
        missing = row["missing"]
        categories = row["categories"]
        strength = row["strength"]
        rank = row["rank"]
        unresolved = row["unresolved"]
        member_count = int(row["member_count"])
        reasons = row["reasons"]
        if anchor_session is None:
            expected_source_revision = sha256_json(
                {
                    **source_base,
                    "sector_id": sector_id,
                    "member_history_statuses": member_statuses,
                }
            )
            valid = (
                strength is None
                and rank is None
                and not categories
                and unresolved == anchor_reasons
                and reasons == anchor_reasons
                and member_count == total_count
            )
        else:
            expected_source_revision = sha256_json(
                {
                    **source_base,
                    "sector_id": sector_id,
                    "anchor_session": anchor_session.isoformat(),
                    "member_history_statuses": member_statuses,
                    "categories": categories,
                    "total_member_count": total_count,
                    "usable_member_count": usable_count,
                    "missing_members": missing,
                    "strength": strength,
                    "rank": rank,
                    "unresolved_reasons": unresolved,
                }
            )
            if strength is not None:
                expected_reasons = (
                    "CURRENT_QMT_MEMBERSHIP_AUTHORIZED",
                    "COMMON_BROAD_MARKET_DAILY_BOTTOM_FRACTAL_ANCHOR",
                    "EQUAL_WEIGHT_MEMBER_MA_CATEGORY_MEAN",
                )
                valid = (
                    bool(categories)
                    and not missing
                    and usable_count == total_count
                    and tuple(symbol for symbol, _category in categories)
                    == member_symbols
                    and member_count
                    == total_count
                    == usable_count
                    == len(categories)
                    and strength
                    == sum(
                        (Decimal(category) for _symbol, category in categories),
                        Decimal("0"),
                    )
                    / Decimal(len(categories))
                    and rank == expected_ranks[sector_id]
                    and not unresolved
                    and reasons == expected_reasons
                )
            else:
                expected_unresolved = (
                    ("EMPTY_POINT_IN_TIME_BASKET",)
                    if total_count == 0
                    else tuple(
                        f"UNEXPLAINED_MEMBER_HISTORY:{symbol}"
                        for symbol in missing
                    )
                )
                valid = (
                    member_count == total_count
                    and categories
                    == tuple((symbol, 1) for symbol in member_symbols)
                    and unresolved == expected_unresolved
                    and reasons == expected_unresolved
                    and (
                        total_count == 0
                        or bool(missing) and usable_count < total_count
                    )
                )
        if not valid or row["source_revision"] != expected_source_revision:
            raise ValueError("sector strength derived evidence is inconsistent")
        outputs.append(
            SectorStrengthEvidence(
                sector_id=sector_id,
                observed_at=observed,
                anchor_session=anchor_session,
                member_count=member_count,
                strength=strength,  # type: ignore[arg-type]
                rank=rank,  # type: ignore[arg-type]
                source_revision=expected_source_revision,
                reason_codes=reasons,  # type: ignore[arg-type]
            )
        )
    return SectorStrengthBatch(
        strengths=tuple(outputs),
        evidence_json=_canonical_evidence_json(value),
    )


def build_sector_member_history_diagnostics(
    batch: SectorStrengthBatch,
) -> dict[str, object]:
    """Summarize authenticated member-history states for operators and UI.

    The summary is deliberately derived from the canonical strength evidence,
    never from a second provider query.  Re-parsing first means a caller cannot
    construct a merely canonical-looking ``SectorStrengthBatch`` and use this
    helper to launder inconsistent member evidence.  A symbol may occur in
    more than one point-in-time sector relation, but its history state must be
    identical everywhere.
    """

    document = batch.evidence_document()
    validated = sector_strength_batch_from_evidence_document(document)
    if validated != batch:
        raise ValueError("sector strength batch does not match its evidence")

    relation_counts = {status: 0 for status in _MEMBER_HISTORY_STATUS_ORDER}
    affected_sector_counts = {
        status: 0 for status in _MEMBER_HISTORY_STATUS_ORDER
    }
    statuses_by_symbol: dict[str, str] = {}
    sector_ids_by_status = {
        status: set() for status in _MEMBER_HISTORY_STATUS_ORDER
    }
    raw_sectors = document["sectors"]
    if not isinstance(raw_sectors, list):  # pragma: no cover - parser guards
        raise ValueError("sector strength sector evidence is invalid")
    for raw_sector in raw_sectors:
        if not isinstance(raw_sector, Mapping):  # pragma: no cover
            raise ValueError("sector strength sector evidence is invalid")
        sector_id = str(raw_sector["sector_id"])
        raw_statuses = raw_sector["member_history_statuses"]
        if not isinstance(raw_statuses, list):  # pragma: no cover
            raise ValueError("sector member history statuses are invalid")
        statuses_in_sector: set[str] = set()
        for raw_status in raw_statuses:
            if not isinstance(raw_status, list) or len(raw_status) != 2:
                raise ValueError("sector member history status is invalid")
            symbol, status = raw_status
            if (
                not isinstance(symbol, str)
                or not isinstance(status, str)
                or status not in _MEMBER_HISTORY_STATUSES
            ):
                raise ValueError("sector member history status is invalid")
            existing = statuses_by_symbol.get(symbol)
            if existing is not None and existing != status:
                raise ValueError(
                    f"member history status conflicts across sectors: {symbol}"
                )
            statuses_by_symbol[symbol] = status
            relation_counts[status] += 1
            statuses_in_sector.add(status)
            sector_ids_by_status[status].add(sector_id)
        for status in statuses_in_sector:
            affected_sector_counts[status] += 1

    unique_counts = {status: 0 for status in _MEMBER_HISTORY_STATUS_ORDER}
    for status in statuses_by_symbol.values():
        unique_counts[status] += 1
    return {
        "schema": SECTOR_MEMBER_HISTORY_DIAGNOSTICS_SCHEMA,
        "evidence_revision": validated.evidence_revision,
        "sector_count": len(raw_sectors),
        "sector_member_relation_count": sum(relation_counts.values()),
        "unique_symbol_count": len(statuses_by_symbol),
        "relation_status_counts": relation_counts,
        "unique_symbol_status_counts": unique_counts,
        "affected_sector_counts": affected_sector_counts,
        "new_listing_symbols": sorted(
            symbol
            for symbol, status in statuses_by_symbol.items()
            if status == "NEW_LISTING"
        ),
        "suspended_symbols": sorted(
            symbol
            for symbol, status in statuses_by_symbol.items()
            if status == "SUSPENDED"
        ),
        "unexplained_gap_symbols": sorted(
            symbol
            for symbol, status in statuses_by_symbol.items()
            if status == "UNEXPLAINED_GAP"
        ),
        "unexplained_gap_sector_ids": sorted(
            sector_ids_by_status["UNEXPLAINED_GAP"]
        ),
    }


__all__ = (
    "MIN_MEMBER_HISTORY_COVERAGE",
    "SECTOR_MEMBER_HISTORY_DIAGNOSTICS_SCHEMA",
    "SECTOR_STRENGTH_EVIDENCE_SCHEMA",
    "SectorStrengthBatch",
    "SectorStrengthEvidence",
    "SectorMemberCategoryFact",
    "build_horizontal_sector_strength",
    "build_horizontal_sector_strength_batch",
    "build_horizontal_sector_strength_batch_from_categories",
    "build_sector_member_history_diagnostics",
    "sector_strength_batch_from_evidence_document",
)
