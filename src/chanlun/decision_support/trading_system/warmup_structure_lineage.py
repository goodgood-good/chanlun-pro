"""Strict structure lineage for warmup mapping-supply changes.

The diagnostic explains why a lower-timeframe mapping point can disappear when
more left history is supplied. Lines, centers and trigger links are projected
from the same immutable strict evidence used by charting, screening and replay.

All cross-prefix conclusions are derived canonically from recorded lines,
centers and point-to-trigger links.  A caller cannot edit a conclusion and
merely recompute the outer hash; strict parsing recomputes every derived row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import TYPE_CHECKING, Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)

if TYPE_CHECKING:
    from chanlun.decision_support.trading_system.warmup_convergence import (
        WarmupConvergenceEnvelope,
    )


WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_SCHEMA = (
    "chanlun-warmup-structure-lineage-diagnostic"
)
WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID = sha256_json(
    {
        "schema": WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_SCHEMA,
        "binding": (
            "warmup-envelope-semantic-and-mapping-supply-content-sha256"
        ),
        "comparison": "changed-prefix-period-vs-longest-left-history-prefix",
        "structure_source": "strict-recursive-evidence",
        "derived_facts": (
            "common-line-suffix",
            "center-partition-delta",
            "point-trigger-role-change",
        ),
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }
)

StructureLineKind = Literal["SEGMENT", "TREND_TYPE"]
_LINE_KINDS: tuple[StructureLineKind, ...] = ("SEGMENT", "TREND_TYPE")
_PERIODS = ("M", "W", "D")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STRICT_STRUCTURE_ID = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 identity")


def _require_strict_structure_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _STRICT_STRUCTURE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a strict structure identity")


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


@dataclass(frozen=True, slots=True)
class WarmupStructureLineFacts:
    line_id: str
    source_kind: StructureLineKind
    ordinal: int
    direction: str
    start_at: datetime
    end_at: datetime
    start_value: Decimal
    end_value: Decimal
    locked_at: datetime | None
    completed: bool

    @staticmethod
    def identity(
        *,
        source_symbol: str,
        source_frequency: str,
        source_kind: StructureLineKind,
        direction: str,
        start_at: datetime,
        end_at: datetime,
        start_value: Decimal,
        end_value: Decimal,
        locked_at: datetime | None,
        completed: bool,
    ) -> str:
        return sha256_json(
            {
                "schema": "chanlun-warmup-strict-structure-unit-identity",
                "source_symbol": source_symbol,
                "source_frequency": source_frequency,
                "source_kind": source_kind,
                "direction": direction,
                "start_at": start_at,
                "end_at": end_at,
                "start_value": _decimal_text(start_value),
                "end_value": _decimal_text(end_value),
                "locked_at": locked_at,
                "completed": completed,
            }
        )

    def __post_init__(self) -> None:
        _require_sha256(self.line_id, "line_id")
        if self.source_kind not in _LINE_KINDS:
            raise ValueError("structure line kind is invalid")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("structure line ordinal is invalid")
        if self.direction not in {"up", "down"}:
            raise ValueError("structure line direction is invalid")
        start = normalize_datetime(self.start_at, "line_start_at")
        end = normalize_datetime(self.end_at, "line_end_at")
        if start >= end:
            raise ValueError("structure line interval is invalid")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        object.__setattr__(
            self, "start_value", _decimal(self.start_value, "start_value")
        )
        object.__setattr__(self, "end_value", _decimal(self.end_value, "end_value"))
        locked = self.locked_at
        if locked is not None:
            locked = normalize_datetime(locked, "line_locked_at")
            if locked < end:
                raise ValueError("structure line lock precedes its endpoint")
            object.__setattr__(self, "locked_at", locked)
        if type(self.completed) is not bool or self.completed != (locked is not None):
            raise ValueError("structure line completion/lock is inconsistent")

    def validate_identity(self, *, source_symbol: str, source_frequency: str) -> None:
        expected = self.identity(
            source_symbol=source_symbol,
            source_frequency=source_frequency,
            source_kind=self.source_kind,
            direction=self.direction,
            start_at=self.start_at,
            end_at=self.end_at,
            start_value=self.start_value,
            end_value=self.end_value,
            locked_at=self.locked_at,
            completed=self.completed,
        )
        if self.line_id != expected:
            raise ValueError("structure line identity changed")

    def document(self) -> dict[str, object]:
        return {
            "line_id": self.line_id,
            "source_kind": self.source_kind,
            "ordinal": self.ordinal,
            "direction": self.direction,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "start_value": _decimal_text(self.start_value),
            "end_value": _decimal_text(self.end_value),
            "locked_at": (
                None if self.locked_at is None else self.locked_at.isoformat()
            ),
            "completed": self.completed,
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> WarmupStructureLineFacts:
        try:
            if type(raw["ordinal"]) is not int or type(raw["completed"]) is not bool:
                raise ValueError("structure line scalar types changed")
            result = cls(
                line_id=str(raw["line_id"]),
                source_kind=str(raw["source_kind"]),  # type: ignore[arg-type]
                ordinal=raw["ordinal"],  # type: ignore[arg-type]
                direction=str(raw["direction"]),
                start_at=datetime.fromisoformat(str(raw["start_at"])),
                end_at=datetime.fromisoformat(str(raw["end_at"])),
                start_value=_decimal(raw["start_value"], "start_value"),
                end_value=_decimal(raw["end_value"], "end_value"),
                locked_at=(
                    None
                    if raw["locked_at"] is None
                    else datetime.fromisoformat(str(raw["locked_at"]))
                ),
                completed=raw["completed"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("structure line facts are malformed") from exc
        if dict(raw) != result.document():
            raise ValueError("structure line facts are non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupStructureCenterFacts:
    center_id: str
    source_kind: StructureLineKind
    level_rank: int
    center_index: int
    direction: str | None
    start_at: datetime
    end_at: datetime
    core_low: Decimal
    core_high: Decimal
    range_low: Decimal
    range_high: Decimal
    completed: bool
    real: bool
    expanded: bool
    entry_line_id: str | None
    constituent_line_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_strict_structure_id(self.center_id, "center_id")
        if self.source_kind not in _LINE_KINDS:
            raise ValueError("center source kind is invalid")
        if type(self.level_rank) is not int or self.level_rank < 0:
            raise ValueError("center level rank is invalid")
        if type(self.center_index) is not int or self.center_index < 0:
            raise ValueError("center index is invalid")
        if self.direction not in {None, "up", "down", "zd"}:
            raise ValueError("center direction is invalid")
        start = normalize_datetime(self.start_at, "center_start_at")
        end = normalize_datetime(self.end_at, "center_end_at")
        if start >= end:
            raise ValueError("center interval is invalid")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        for name in ("core_low", "core_high", "range_low", "range_high"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if (
            self.range_low > self.core_low
            or self.core_low >= self.core_high
            or self.core_high > self.range_high
        ):
            raise ValueError("center price intervals are inconsistent")
        if any(type(value) is not bool for value in (self.completed, self.real, self.expanded)):
            raise ValueError("center flags must be exact booleans")
        if self.entry_line_id is not None:
            _require_sha256(self.entry_line_id, "entry_line_id")
        values = tuple(self.constituent_line_ids)
        if not values or len(values) != len(set(values)):
            raise ValueError("center constituent line identities are invalid")
        for value in values:
            _require_sha256(value, "constituent_line_id")
        object.__setattr__(self, "constituent_line_ids", values)

    def document(self) -> dict[str, object]:
        return {
            "center_id": self.center_id,
            "source_kind": self.source_kind,
            "level_rank": self.level_rank,
            "center_index": self.center_index,
            "direction": self.direction,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "core_interval": [
                _decimal_text(self.core_low),
                _decimal_text(self.core_high),
            ],
            "range_interval": [
                _decimal_text(self.range_low),
                _decimal_text(self.range_high),
            ],
            "completed": self.completed,
            "real": self.real,
            "expanded": self.expanded,
            "entry_line_id": self.entry_line_id,
            "constituent_line_ids": list(self.constituent_line_ids),
            "constituent_line_count": len(self.constituent_line_ids),
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object]) -> WarmupStructureCenterFacts:
        try:
            core = raw["core_interval"]
            envelope = raw["range_interval"]
            lines = raw["constituent_line_ids"]
            if (
                not isinstance(core, list)
                or len(core) != 2
                or not isinstance(envelope, list)
                or len(envelope) != 2
                or not isinstance(lines, list)
                or type(raw["level_rank"]) is not int
                or type(raw["center_index"]) is not int
                or any(
                    type(raw[name]) is not bool
                    for name in ("completed", "real", "expanded")
                )
            ):
                raise ValueError("center scalar types changed")
            result = cls(
                center_id=str(raw["center_id"]),
                source_kind=str(raw["source_kind"]),  # type: ignore[arg-type]
                level_rank=raw["level_rank"],  # type: ignore[arg-type]
                center_index=raw["center_index"],  # type: ignore[arg-type]
                direction=(
                    None if raw["direction"] is None else str(raw["direction"])
                ),
                start_at=datetime.fromisoformat(str(raw["start_at"])),
                end_at=datetime.fromisoformat(str(raw["end_at"])),
                core_low=_decimal(core[0], "core_low"),
                core_high=_decimal(core[1], "core_high"),
                range_low=_decimal(envelope[0], "range_low"),
                range_high=_decimal(envelope[1], "range_high"),
                completed=raw["completed"],  # type: ignore[arg-type]
                real=raw["real"],  # type: ignore[arg-type]
                expanded=raw["expanded"],  # type: ignore[arg-type]
                entry_line_id=(
                    None
                    if raw["entry_line_id"] is None
                    else str(raw["entry_line_id"])
                ),
                constituent_line_ids=tuple(str(value) for value in lines),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("structure center facts are malformed") from exc
        if raw.get("constituent_line_count") != len(result.constituent_line_ids):
            raise ValueError("center constituent line count changed")
        if dict(raw) != result.document():
            raise ValueError("structure center facts are non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupStructurePointLineageFacts:
    point: RiskMappingPointEvidenceFacts
    trigger_line_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.point, RiskMappingPointEvidenceFacts):
            raise ValueError("mapping point lineage is invalid")
        if self.trigger_line_id is not None:
            _require_sha256(self.trigger_line_id, "trigger_line_id")

    def document(self) -> dict[str, object]:
        return {
            "point": self.point.document(),
            "trigger_line_id": self.trigger_line_id,
        }

    @classmethod
    def from_document(
        cls, raw: Mapping[str, object]
    ) -> WarmupStructurePointLineageFacts:
        try:
            result = cls(
                point=RiskMappingPointEvidenceFacts.from_document(raw["point"]),
                trigger_line_id=(
                    None
                    if raw["trigger_line_id"] is None
                    else str(raw["trigger_line_id"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("mapping point lineage facts are malformed") from exc
        if dict(raw) != result.document():
            raise ValueError("mapping point lineage facts are non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupStructureLineageSnapshot:
    period: str
    source_symbol: str
    source_frequency: str
    source_bar_count: int
    source_start_at: datetime
    source_end_at: datetime
    source_content_sha256: str
    lines: tuple[WarmupStructureLineFacts, ...]
    centers: tuple[WarmupStructureCenterFacts, ...]
    points: tuple[WarmupStructurePointLineageFacts, ...]

    def __post_init__(self) -> None:
        if self.period not in _PERIODS:
            raise ValueError("structure lineage period is invalid")
        if not self.source_symbol or not self.source_frequency:
            raise ValueError("structure lineage source identity is required")
        if type(self.source_bar_count) is not int or self.source_bar_count <= 0:
            raise ValueError("structure lineage source bar count is invalid")
        start = normalize_datetime(self.source_start_at, "source_start_at")
        end = normalize_datetime(self.source_end_at, "source_end_at")
        if start > end:
            raise ValueError("structure lineage source interval is invalid")
        object.__setattr__(self, "source_start_at", start)
        object.__setattr__(self, "source_end_at", end)
        _require_sha256(self.source_content_sha256, "source_content_sha256")
        lines = tuple(self.lines)
        centers = tuple(self.centers)
        points = tuple(self.points)
        line_keys = tuple((_LINE_KINDS.index(row.source_kind), row.ordinal) for row in lines)
        if line_keys != tuple(sorted(set(line_keys))):
            raise ValueError("structure lineage lines are not uniquely ordered")
        line_ids = {row.line_id for row in lines}
        if len(line_ids) != len(lines):
            raise ValueError("structure lineage line identities are duplicated")
        for row in lines:
            row.validate_identity(
                source_symbol=self.source_symbol,
                source_frequency=self.source_frequency,
            )
        center_keys = tuple(
            (row.level_rank, row.center_index, row.center_id) for row in centers
        )
        if center_keys != tuple(sorted(set(center_keys))):
            raise ValueError("structure lineage centers are not uniquely ordered")
        center_ids = {row.center_id for row in centers}
        if len(center_ids) != len(centers):
            raise ValueError("structure lineage center identities are duplicated")
        for row in centers:
            referenced = set(row.constituent_line_ids)
            if row.entry_line_id is not None:
                referenced.add(row.entry_line_id)
            if not referenced.issubset(line_ids):
                raise ValueError("center references an unrecorded structure line")
        point_ids = tuple(row.point.point_id for row in points)
        if point_ids != tuple(sorted(set(point_ids))):
            raise ValueError("structure lineage points are not uniquely ordered")
        for row in points:
            if row.point.center_id not in center_ids:
                raise ValueError("mapping point references an unrecorded center")
            if row.trigger_line_id is not None and row.trigger_line_id not in line_ids:
                raise ValueError("mapping point trigger line is unrecorded")
        object.__setattr__(self, "lines", lines)
        object.__setattr__(self, "centers", centers)
        object.__setattr__(self, "points", points)

    def document(self) -> dict[str, object]:
        return {
            "period": self.period,
            "source_symbol": self.source_symbol,
            "source_frequency": self.source_frequency,
            "source_bar_count": self.source_bar_count,
            "source_start_at": self.source_start_at.isoformat(),
            "source_end_at": self.source_end_at.isoformat(),
            "source_content_sha256": self.source_content_sha256,
            "lines": [value.document() for value in self.lines],
            "line_count": len(self.lines),
            "completed_line_count": sum(value.completed for value in self.lines),
            "centers": [value.document() for value in self.centers],
            "center_count": len(self.centers),
            "points": [value.document() for value in self.points],
            "point_count": len(self.points),
        }

    @classmethod
    def from_document(
        cls, raw: Mapping[str, object]
    ) -> WarmupStructureLineageSnapshot:
        try:
            raw_lines = raw["lines"]
            raw_centers = raw["centers"]
            raw_points = raw["points"]
            if (
                not isinstance(raw_lines, list)
                or not isinstance(raw_centers, list)
                or not isinstance(raw_points, list)
                or type(raw["source_bar_count"]) is not int
            ):
                raise ValueError("structure lineage snapshot scalar types changed")
            result = cls(
                period=str(raw["period"]),
                source_symbol=str(raw["source_symbol"]),
                source_frequency=str(raw["source_frequency"]),
                source_bar_count=raw["source_bar_count"],  # type: ignore[arg-type]
                source_start_at=datetime.fromisoformat(str(raw["source_start_at"])),
                source_end_at=datetime.fromisoformat(str(raw["source_end_at"])),
                source_content_sha256=str(raw["source_content_sha256"]),
                lines=tuple(
                    WarmupStructureLineFacts.from_document(value)
                    for value in raw_lines
                    if isinstance(value, Mapping)
                ),
                centers=tuple(
                    WarmupStructureCenterFacts.from_document(value)
                    for value in raw_centers
                    if isinstance(value, Mapping)
                ),
                points=tuple(
                    WarmupStructurePointLineageFacts.from_document(value)
                    for value in raw_points
                    if isinstance(value, Mapping)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("structure lineage snapshot is malformed") from exc
        document = result.document()
        for name in ("line_count", "completed_line_count", "center_count", "point_count"):
            if raw.get(name) != document[name]:
                raise ValueError(f"structure lineage snapshot {name} changed")
        if dict(raw) != document:
            raise ValueError("structure lineage snapshot is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupStructureLineageSnapshotSet:
    periods: tuple[tuple[str, WarmupStructureLineageSnapshot | None], ...]

    def __post_init__(self) -> None:
        values = tuple(self.periods)
        if tuple(period for period, _snapshot in values) != _PERIODS:
            raise ValueError("structure lineage periods must be ordered M/W/D")
        if any(
            snapshot is not None
            and (
                not isinstance(snapshot, WarmupStructureLineageSnapshot)
                or snapshot.period != period
            )
            for period, snapshot in values
        ):
            raise ValueError("structure lineage snapshot set is malformed")
        object.__setattr__(self, "periods", values)

    def for_period(self, period: str) -> WarmupStructureLineageSnapshot | None:
        return dict(self.periods)[period]


def _line_sequences(snapshot: WarmupStructureLineageSnapshot, kind: str) -> tuple[str, ...]:
    return tuple(value.line_id for value in snapshot.lines if value.source_kind == kind)


def _common_suffix(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    count = 0
    limit = min(len(left), len(right))
    while count < limit and left[-1 - count] == right[-1 - count]:
        count += 1
    return () if count == 0 else left[-count:]


def _line_sequence_delta(
    prefix: WarmupStructureLineageSnapshot,
    reference: WarmupStructureLineageSnapshot,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    prefix_lines = {value.line_id: value for value in prefix.lines}
    reference_lines = {value.line_id: value for value in reference.lines}
    for kind in _LINE_KINDS:
        left = _line_sequences(prefix, kind)
        right = _line_sequences(reference, kind)
        common = _common_suffix(left, right)
        output.append(
            {
                "source_kind": kind,
                "prefix_line_count": len(left),
                "reference_line_count": len(right),
                "common_suffix_line_ids": list(common),
                "common_suffix_line_count": len(common),
                "completed_common_suffix_line_count": sum(
                    prefix_lines[value].completed
                    and reference_lines[value].completed
                    for value in common
                ),
                "prefix_leading_line_count": len(left) - len(common),
                "reference_leading_line_count": len(right) - len(common),
                "prefix_is_reference_suffix": bool(left) and left == right[-len(left):],
            }
        )
    return output


def _best_peer(
    center: WarmupStructureCenterFacts,
    candidates: Sequence[WarmupStructureCenterFacts],
) -> tuple[WarmupStructureCenterFacts | None, int, int]:
    source = set(center.constituent_line_ids)
    scored: list[tuple[int, int, bool, str, WarmupStructureCenterFacts]] = []
    for candidate in candidates:
        if candidate.level_rank != center.level_rank:
            continue
        target = set(candidate.constituent_line_ids)
        shared = len(source.intersection(target))
        union = len(source.union(target))
        same_core = (
            center.core_low == candidate.core_low
            and center.core_high == candidate.core_high
        )
        scored.append((shared, -union, same_core, candidate.center_id, candidate))
    if not scored:
        return None, 0, len(source)
    shared, negative_union, _same_core, _identity, peer = max(scored)
    return peer, shared, -negative_union


def _point_role_changes(
    prefix: WarmupStructureLineageSnapshot,
    reference: WarmupStructureLineageSnapshot,
) -> list[dict[str, object]]:
    reference_centers = tuple(reference.centers)
    prefix_centers = {value.center_id: value for value in prefix.centers}
    reference_points = {value.point.point_id for value in reference.points}
    output: list[dict[str, object]] = []
    for lineage in prefix.points:
        point = lineage.point
        if point.point_id in reference_points:
            continue
        center = prefix_centers.get(point.center_id)
        if center is None:
            continue
        peer, shared, union = _best_peer(center, reference_centers)
        trigger = lineage.trigger_line_id
        prefix_role = (
            "UNRESOLVED"
            if trigger is None
            else (
                "CENTER_CONSTITUENT"
                if trigger in center.constituent_line_ids
                else "AFTER_CENTER"
            )
        )
        reference_role = (
            "NO_OVERLAPPING_REFERENCE_CENTER"
            if peer is None or shared == 0
            else (
                "UNRESOLVED"
                if trigger is None
                else (
                    "CENTER_CONSTITUENT"
                    if trigger in peer.constituent_line_ids
                    else "AFTER_CENTER"
                )
            )
        )
        same_core = bool(
            peer is not None
            and center.core_low == peer.core_low
            and center.core_high == peer.core_high
        )
        one_line_phase_shift = bool(
            peer is not None
            and len(center.constituent_line_ids)
            == len(peer.constituent_line_ids)
            and shared + 1 == len(center.constituent_line_ids)
            and union == len(center.constituent_line_ids) + 1
        )
        output.append(
            {
                "point_id": point.point_id,
                "point_type": point.point_type,
                "point_anchor_at": point.point_anchor_at.isoformat(),
                "trigger_line_id": trigger,
                "prefix_center_id": center.center_id,
                "reference_peer_center_id": (
                    None if peer is None or shared == 0 else peer.center_id
                ),
                "shared_constituent_line_count": shared,
                "union_constituent_line_count": union,
                "same_core_interval": same_core,
                "one_line_phase_shift": one_line_phase_shift,
                "prefix_trigger_role": prefix_role,
                "reference_trigger_role": reference_role,
            }
        )
    return sorted(output, key=lambda value: str(value["point_id"]))


def _structure_lineage_delta_document(
    prefix: WarmupStructureLineageSnapshot | None,
    reference: WarmupStructureLineageSnapshot | None,
) -> dict[str, object]:
    codes: list[str] = []
    if prefix is None or reference is None:
        codes.append("STRUCTURE_LINEAGE_NOT_RECORDED")
        return {
            "line_sequences": [],
            "retained_center_ids": [],
            "lost_center_ids_from_longest": [],
            "gained_center_ids_in_longest": [],
            "point_trigger_role_changes": [],
            "transition_codes": codes,
        }
    line_sequences = _line_sequence_delta(prefix, reference)
    prefix_centers = {value.center_id: value for value in prefix.centers}
    reference_centers = {value.center_id: value for value in reference.centers}
    retained = tuple(sorted(set(prefix_centers).intersection(reference_centers)))
    lost = tuple(sorted(set(prefix_centers).difference(reference_centers)))
    gained = tuple(sorted(set(reference_centers).difference(prefix_centers)))
    roles = _point_role_changes(prefix, reference)
    if any(value["common_suffix_line_count"] for value in line_sequences):
        codes.append("LOWER_LINE_COMMON_SUFFIX_IDENTICAL")
    if any(value["prefix_is_reference_suffix"] for value in line_sequences):
        codes.append("SHORTER_LINE_SEQUENCE_IS_REFERENCE_SUFFIX")
    if lost and gained and any(
        int(value["shared_constituent_line_count"]) > 0 for value in roles
    ):
        codes.append("CENTER_PARTITION_CHANGED_WITH_IDENTICAL_COMMON_LINES")
    if any(
        value["same_core_interval"] and value["one_line_phase_shift"]
        for value in roles
    ):
        codes.append("CENTER_CORE_RETAINED_WITH_ONE_LINE_PHASE_SHIFT")
    if any(
        value["point_type"] in {"1sell", "2sell"}
        and value["prefix_trigger_role"] == "AFTER_CENTER"
        and value["reference_trigger_role"] == "CENTER_CONSTITUENT"
        for value in roles
    ):
        codes.append("LOST_SELL_TRIGGER_LINE_ABSORBED_INTO_REFERENCE_CENTER")
    if any(
        value["prefix_trigger_role"] != value["reference_trigger_role"]
        for value in roles
    ):
        codes.append("POINT_TRIGGER_ROLE_CHANGED_WITH_LONGER_HISTORY")
    return {
        "line_sequences": line_sequences,
        "retained_center_ids": list(retained),
        "lost_center_ids_from_longest": list(lost),
        "gained_center_ids_in_longest": list(gained),
        "point_trigger_role_changes": roles,
        "transition_codes": codes,
    }


@dataclass(frozen=True, slots=True)
class WarmupStructureLineageComparison:
    period: str
    prefix_bar_count: int
    reference_bar_count: int
    mapping_supply_comparison_sha256: str
    prefix_snapshot: WarmupStructureLineageSnapshot | None
    reference_snapshot: WarmupStructureLineageSnapshot | None

    def __post_init__(self) -> None:
        if self.period not in _PERIODS:
            raise ValueError("structure lineage comparison period is invalid")
        if (
            type(self.prefix_bar_count) is not int
            or type(self.reference_bar_count) is not int
            or self.prefix_bar_count <= 0
            or self.prefix_bar_count >= self.reference_bar_count
        ):
            raise ValueError("structure lineage comparison counts are invalid")
        _require_sha256(
            self.mapping_supply_comparison_sha256,
            "mapping_supply_comparison_sha256",
        )
        for snapshot in (self.prefix_snapshot, self.reference_snapshot):
            if snapshot is not None and (
                not isinstance(snapshot, WarmupStructureLineageSnapshot)
                or snapshot.period != self.period
            ):
                raise ValueError("structure lineage comparison snapshot is invalid")

    def document(self) -> dict[str, object]:
        return {
            "period": self.period,
            "prefix_bar_count": self.prefix_bar_count,
            "reference_bar_count": self.reference_bar_count,
            "mapping_supply_comparison_sha256": (
                self.mapping_supply_comparison_sha256
            ),
            "prefix_snapshot": (
                None if self.prefix_snapshot is None else self.prefix_snapshot.document()
            ),
            "reference_snapshot": (
                None
                if self.reference_snapshot is None
                else self.reference_snapshot.document()
            ),
            "delta": _structure_lineage_delta_document(
                self.prefix_snapshot, self.reference_snapshot
            ),
        }

    @classmethod
    def from_document(
        cls, raw: Mapping[str, object]
    ) -> WarmupStructureLineageComparison:
        try:
            prefix_raw = raw["prefix_snapshot"]
            reference_raw = raw["reference_snapshot"]
            if (
                type(raw["prefix_bar_count"]) is not int
                or type(raw["reference_bar_count"]) is not int
            ):
                raise ValueError("structure lineage comparison scalar types changed")
            result = cls(
                period=str(raw["period"]),
                prefix_bar_count=raw["prefix_bar_count"],  # type: ignore[arg-type]
                reference_bar_count=raw["reference_bar_count"],  # type: ignore[arg-type]
                mapping_supply_comparison_sha256=str(
                    raw["mapping_supply_comparison_sha256"]
                ),
                prefix_snapshot=(
                    None
                    if prefix_raw is None
                    else WarmupStructureLineageSnapshot.from_document(prefix_raw)
                ),
                reference_snapshot=(
                    None
                    if reference_raw is None
                    else WarmupStructureLineageSnapshot.from_document(reference_raw)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("structure lineage comparison is malformed") from exc
        if dict(raw) != result.document():
            raise ValueError("structure lineage comparison is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupStructureLineageDiagnosticEnvelope:
    frequency: str
    as_of: datetime
    parameter_set_id: str
    envelope_content_sha256: str
    semantic_diagnostic_content_sha256: str
    mapping_supply_diagnostic_content_sha256: str
    status: str
    comparisons: tuple[WarmupStructureLineageComparison, ...]
    schema: str = WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_SCHEMA
    contract_id: str = WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
    diagnostic_only: bool = True
    active_gate_unchanged: bool = True
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if not self.frequency:
            raise ValueError("structure lineage diagnostic frequency is required")
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        for name in (
            "parameter_set_id",
            "envelope_content_sha256",
            "semantic_diagnostic_content_sha256",
            "mapping_supply_diagnostic_content_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if self.status not in {
            "STABLE_ALL_PREFIXES",
            "CONVERGED_ONLY_WITH_LONGER_HISTORY",
            "NON_MONOTONIC",
            "INSUFFICIENT_PREFIXES",
        }:
            raise ValueError("structure lineage diagnostic status is invalid")
        values = tuple(self.comparisons)
        keys = tuple((value.prefix_bar_count, _PERIODS.index(value.period)) for value in values)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("structure lineage comparisons are not uniquely ordered")
        object.__setattr__(self, "comparisons", values)
        if (
            self.schema != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_SCHEMA
            or self.contract_id != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
            or self.diagnostic_only is not True
            or self.active_gate_unchanged is not True
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("structure lineage diagnostic safety contract is immutable")

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "contract_id": self.contract_id,
            "frequency": self.frequency,
            "as_of": self.as_of.isoformat(),
            "parameter_set_id": self.parameter_set_id,
            "envelope_content_sha256": self.envelope_content_sha256,
            "semantic_diagnostic_content_sha256": (
                self.semantic_diagnostic_content_sha256
            ),
            "mapping_supply_diagnostic_content_sha256": (
                self.mapping_supply_diagnostic_content_sha256
            ),
            "status": self.status,
            "comparisons": [value.document() for value in self.comparisons],
            "comparison_count": len(self.comparisons),
            "diagnostic_only": self.diagnostic_only,
            "active_gate_unchanged": self.active_gate_unchanged,
            "live_status": self.live_status,
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        return {**self._stable_document(), "content_sha256": self.content_sha256}

    def validate_against(self, envelope: WarmupConvergenceEnvelope) -> None:
        semantic = envelope.diagnostic
        supply = envelope.mapping_supply_diagnostic
        if semantic is None or supply is None:
            raise ValueError("structure lineage diagnostic requires prior siblings")
        semantic.validate_against(envelope)
        supply.validate_against(envelope)
        if (
            self.frequency != envelope.frequency
            or self.as_of != envelope.as_of
            or self.parameter_set_id != envelope.parameter_set_id
            or self.envelope_content_sha256 != envelope.content_sha256
            or self.semantic_diagnostic_content_sha256 != semantic.content_sha256
            or self.mapping_supply_diagnostic_content_sha256 != supply.content_sha256
            or self.status != envelope.status
        ):
            raise ValueError("structure lineage diagnostic does not bind its envelope")
        expected = {
            (value.prefix_bar_count, value.period): value
            for value in supply.comparisons
        }
        actual = {
            (value.prefix_bar_count, value.period): value
            for value in self.comparisons
        }
        if set(expected) != set(actual):
            raise ValueError("structure lineage comparison coverage changed")
        for key, comparison in actual.items():
            supply_comparison = expected[key]
            if (
                comparison.reference_bar_count
                != supply_comparison.reference_bar_count
                or comparison.mapping_supply_comparison_sha256
                != sha256_json(supply_comparison.document())
            ):
                raise ValueError("structure lineage supply binding changed")

    @classmethod
    def from_document(
        cls, raw: Mapping[str, object]
    ) -> WarmupStructureLineageDiagnosticEnvelope:
        try:
            raw_comparisons = raw["comparisons"]
            if (
                not isinstance(raw_comparisons, list)
                or type(raw["comparison_count"]) is not int
                or raw["diagnostic_only"] is not True
                or raw["active_gate_unchanged"] is not True
            ):
                raise ValueError("structure lineage diagnostic scalar types changed")
            result = cls(
                frequency=str(raw["frequency"]),
                as_of=datetime.fromisoformat(str(raw["as_of"])),
                parameter_set_id=str(raw["parameter_set_id"]),
                envelope_content_sha256=str(raw["envelope_content_sha256"]),
                semantic_diagnostic_content_sha256=str(
                    raw["semantic_diagnostic_content_sha256"]
                ),
                mapping_supply_diagnostic_content_sha256=str(
                    raw["mapping_supply_diagnostic_content_sha256"]
                ),
                status=str(raw["status"]),
                comparisons=tuple(
                    WarmupStructureLineageComparison.from_document(value)
                    for value in raw_comparisons
                    if isinstance(value, Mapping)
                ),
                schema=str(raw["schema"]),
                contract_id=str(raw["contract_id"]),
                diagnostic_only=raw["diagnostic_only"],  # type: ignore[arg-type]
                active_gate_unchanged=raw["active_gate_unchanged"],  # type: ignore[arg-type]
                live_status=str(raw["live_status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("structure lineage diagnostic is malformed") from exc
        if raw.get("comparison_count") != len(result.comparisons):
            raise ValueError("structure lineage diagnostic comparison count changed")
        if dict(raw) != result.document():
            raise ValueError("structure lineage diagnostic is non-canonical")
        return result


def _strict_line_facts(
    unit: object,
    *,
    source_symbol: str,
    source_frequency: str,
    source_kind: StructureLineKind,
    ordinal: int,
    price_quantum: Decimal,
) -> WarmupStructureLineFacts:
    start_at = normalize_datetime(unit.market_start, "line_start_at")
    end_at = normalize_datetime(unit.market_end, "line_end_at")
    locked = (
        None
        if unit.confirmed_at is None
        else normalize_datetime(unit.confirmed_at, "line_locked_at")
    )
    start_value = price_quantum * unit.start_tick
    end_value = price_quantum * unit.end_tick
    completed = bool(unit.locked)
    identity = WarmupStructureLineFacts.identity(
        source_symbol=source_symbol,
        source_frequency=source_frequency,
        source_kind=source_kind,
        direction=str(unit.direction),
        start_at=start_at,
        end_at=end_at,
        start_value=start_value,
        end_value=end_value,
        locked_at=locked,
        completed=completed,
    )
    return WarmupStructureLineFacts(
        line_id=identity,
        source_kind=source_kind,
        ordinal=ordinal,
        direction=str(unit.direction),
        start_at=start_at,
        end_at=end_at,
        start_value=start_value,
        end_value=end_value,
        locked_at=locked,
        completed=completed,
    )


def capture_warmup_structure_lineage_snapshot(
    *,
    period: str,
    source_symbol: str,
    source_frequency: str,
    source_bars: Sequence[object],
    state: object,
    mapping_supply: RiskMappingSupplyFacts,
) -> WarmupStructureLineageSnapshot:
    """Project lineage exclusively from the immutable strict evidence authority."""

    getter = getattr(state, "get_strict_evidence", None)
    if not callable(getter):
        raise ValueError("structure lineage state has no strict evidence authority")
    evidence = getter()
    evidence_closed_at = normalize_datetime(
        evidence.source_closed_at, "strict_source_closed_at"
    )
    if (
        evidence.symbol != source_symbol
        or evidence.source_frequency != source_frequency
    ):
        raise ValueError("structure lineage strict source identity is inconsistent")
    visible = tuple(
        value
        for value in source_bars
        if bool(value.completed)
        and normalize_datetime(value.end_at, "source_bar_end")
        <= evidence_closed_at
    )
    if not visible:
        raise ValueError("structure lineage source bars are empty")
    if normalize_datetime(visible[-1].end_at, "source_end_at") != evidence_closed_at:
        raise ValueError("structure lineage bars do not close the strict evidence")

    from chanlun.core.strict_structure.center_relation import (
        classify_center_relation,
    )
    from chanlun.core.strict_structure.models import (
        CenterRelation,
        CenterState,
        SourceKind,
    )

    quantum = evidence.structure_price_quantum
    strict_kind = {
        SourceKind.SEGMENT: "SEGMENT",
        SourceKind.TREND_TYPE: "TREND_TYPE",
    }
    lines: list[WarmupStructureLineFacts] = []
    line_by_unit_id: dict[str, WarmupStructureLineFacts] = {}
    units_by_kind = {
        kind: tuple(
            unit
            for level in evidence.structure.levels
            for unit in level.units
            if strict_kind.get(unit.source_kind) == kind
        )
        for kind in _LINE_KINDS
    }
    for kind in _LINE_KINDS:
        for ordinal, unit in enumerate(units_by_kind[kind]):
            fact = _strict_line_facts(
                unit,
                source_symbol=source_symbol,
                source_frequency=source_frequency,
                source_kind=kind,
                ordinal=ordinal,
                price_quantum=quantum,
            )
            lines.append(fact)
            if unit.unit_id in line_by_unit_id:
                raise ValueError("strict structure unit identities are duplicated")
            line_by_unit_id[unit.unit_id] = fact

    centers: list[WarmupStructureCenterFacts] = []
    centers_by_id: dict[str, object] = {}
    expanded_center_ids: set[str] = set()
    for level in evidence.structure.levels:
        level_centers = tuple(level.center_result.centers)
        for previous, current in zip(level_centers, level_centers[1:]):
            if classify_center_relation(previous, current) is CenterRelation.UPGRADE:
                expanded_center_ids.update((previous.center_id, current.center_id))
        for center_index, center in enumerate(level_centers):
            kind = strict_kind.get(center.source_kind)
            if kind is None:
                raise ValueError("tradable strict center has an unsupported source")
            constituents = tuple(
                line_by_unit_id[value.unit_id].line_id for value in center.body_units
            )
            entry = line_by_unit_id.get(center.entry_unit.unit_id)
            if entry is None:
                raise ValueError("strict center entry unit was not captured")
            fact = WarmupStructureCenterFacts(
                center_id=center.center_id,
                source_kind=kind,
                level_rank=center.structural_level,
                center_index=center_index,
                direction=center.completion_direction,
                start_at=normalize_datetime(
                    center.entry_unit.market_start, "center_start"
                ),
                end_at=normalize_datetime(
                    max(
                        center.established_market_time,
                        center.last_touch_market_time,
                    ),
                    "center_end",
                ),
                core_low=quantum * center.zd_tick,
                core_high=quantum * center.zg_tick,
                range_low=quantum * center.dd_tick,
                range_high=quantum * center.gg_tick,
                completed=center.state
                in {CenterState.COMPLETED, CenterState.DIVERGENCE_CLOSED},
                real=bool(center.tradable),
                expanded=center.center_id in expanded_center_ids,
                entry_line_id=entry.line_id,
                constituent_line_ids=constituents,
            )
            centers.append(fact)
            if center.center_id in centers_by_id:
                raise ValueError("strict center identities are duplicated")
            centers_by_id[center.center_id] = center

    if mapping_supply.point_evidence is None:
        raise ValueError("structure lineage requires current mapping point evidence")
    supply_points = {value.point_id: value for value in mapping_supply.point_evidence}
    trigger_by_point: dict[str, str] = {}
    for point in evidence.confirmed_points:
        if point.center_id is None:
            continue
        point_id = RiskMappingPointEvidenceFacts.identity(
            source_symbol=source_symbol,
            source_frequency=source_frequency,
            center_id=point.center_id,
            center_level_rank=point.structural_level,
            point_type=point.point_type,
            point_anchor_at=point.anchor_at,
            point_available_at=point.available_at,
        )
        if point_id not in supply_points:
            continue
        line_fact = line_by_unit_id.get(point.anchor_unit_id)
        if line_fact is None or not line_fact.completed or line_fact.locked_at is None:
            raise ValueError("mapping point trigger is not a locked strict unit")
        trigger_by_point[point_id] = line_fact.line_id
    if set(trigger_by_point) != set(supply_points):
        raise ValueError("mapping supply is not closed over strict point evidence")

    points = tuple(
        WarmupStructurePointLineageFacts(
            point=point,
            trigger_line_id=trigger_by_point.get(point_id),
        )
        for point_id, point in sorted(supply_points.items())
    )
    unique_centers = {value.center_id: value for value in centers}
    ordered_centers = tuple(
        sorted(
            unique_centers.values(),
            key=lambda value: (
                value.level_rank,
                value.center_index,
                value.center_id,
            ),
        )
    )
    return WarmupStructureLineageSnapshot(
        period=period,
        source_symbol=source_symbol,
        source_frequency=source_frequency,
        source_bar_count=len(visible),
        source_start_at=visible[0].end_at,
        source_end_at=visible[-1].end_at,
        source_content_sha256=sha256_json(
            {
                "schema": "chanlun-warmup-structure-source-bars",
                "source_symbol": source_symbol,
                "source_frequency": source_frequency,
                "bars": [
                    (
                        value.end_at,
                        str(value.open),
                        str(value.high),
                        str(value.low),
                        str(value.close),
                        str(value.volume),
                    )
                    for value in visible
                ],
            }
        ),
        lines=tuple(lines),
        centers=ordered_centers,
        points=points,
    )


def bind_warmup_structure_lineage_diagnostic(
    envelope: WarmupConvergenceEnvelope,
    *,
    snapshots: tuple[WarmupStructureLineageSnapshotSet, ...],
) -> WarmupConvergenceEnvelope:
    """Attach structure lineage without changing any prior sibling identity."""

    supply = envelope.mapping_supply_diagnostic
    if envelope.diagnostic is None or supply is None:
        raise ValueError("structure lineage requires semantic and supply diagnostics")
    values = tuple(snapshots)
    if len(values) != len(envelope.observations):
        raise ValueError("structure lineage snapshots must align with observations")
    by_count = {
        observation.bar_count: values[index]
        for index, observation in enumerate(envelope.observations)
    }
    comparisons = tuple(
        WarmupStructureLineageComparison(
            period=comparison.period,
            prefix_bar_count=comparison.prefix_bar_count,
            reference_bar_count=comparison.reference_bar_count,
            mapping_supply_comparison_sha256=sha256_json(comparison.document()),
            prefix_snapshot=by_count[comparison.prefix_bar_count].for_period(
                comparison.period
            ),
            reference_snapshot=by_count[comparison.reference_bar_count].for_period(
                comparison.period
            ),
        )
        for comparison in supply.comparisons
    )
    diagnostic = WarmupStructureLineageDiagnosticEnvelope(
        frequency=envelope.frequency,
        as_of=envelope.as_of,
        parameter_set_id=envelope.parameter_set_id,
        envelope_content_sha256=envelope.content_sha256,
        semantic_diagnostic_content_sha256=envelope.diagnostic.content_sha256,
        mapping_supply_diagnostic_content_sha256=supply.content_sha256,
        status=envelope.status,
        comparisons=comparisons,
    )
    return replace(envelope, structure_lineage_diagnostic=diagnostic)


__all__ = [
    "WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID",
    "WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_SCHEMA",
    "WarmupStructureCenterFacts",
    "WarmupStructureLineFacts",
    "WarmupStructureLineageComparison",
    "WarmupStructureLineageDiagnosticEnvelope",
    "WarmupStructureLineageSnapshot",
    "WarmupStructureLineageSnapshotSet",
    "WarmupStructurePointLineageFacts",
    "bind_warmup_structure_lineage_diagnostic",
    "capture_warmup_structure_lineage_snapshot",
]
