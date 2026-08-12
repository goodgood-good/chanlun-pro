"""Current diagnostic-only convergence evidence for structure warmup.

The active screening gate currently compares one full history with one shorter
left-history prefix.  Real QMT A/B probes showed that this pairwise result is
not monotonic when more history is requested: a frame can look stable at one
budget, diverge at a longer budget, and become stable again later.  This module
classifies three or more prefix signatures without changing that active gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import TYPE_CHECKING, Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)

if TYPE_CHECKING:
    from chanlun.decision_support.trading_system.warmup_structure_lineage import (
        WarmupStructureLineageDiagnosticEnvelope,
    )


WARMUP_CONVERGENCE_ENVELOPE_SCHEMA = (
    "chanlun-warmup-convergence-envelope"
)
WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID = sha256_json(
    {
        "schema": WARMUP_CONVERGENCE_ENVELOPE_SCHEMA,
        "comparison": "common-active-tail-semantic-signature",
        "reference": "longest-left-history-prefix",
        "statuses": (
            "STABLE_ALL_PREFIXES",
            "CONVERGED_ONLY_WITH_LONGER_HISTORY",
            "NON_MONOTONIC",
            "INSUFFICIENT_PREFIXES",
        ),
        "diagnostic_only": True,
        "active_gate_unchanged": True,
    }
)

WarmupEnvelopeStatus = Literal[
    "STABLE_ALL_PREFIXES",
    "CONVERGED_ONLY_WITH_LONGER_HISTORY",
    "NON_MONOTONIC",
    "INSUFFICIENT_PREFIXES",
]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PERIODS = ("M", "W", "D")
WARMUP_CONVERGENCE_DIAGNOSTIC_SCHEMA = (
    "chanlun-warmup-convergence-semantic-diagnostic"
)
WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID = sha256_json(
    {
        "schema": WARMUP_CONVERGENCE_DIAGNOSTIC_SCHEMA,
        "binding": "warmup-convergence-envelope-content-sha256",
        "semantic_fields": (
            "state",
            "evidence_bar_end",
            "active_top_interval",
            "mapping_unique",
            "mapped_center_id",
            "mapping_candidate_ids",
            "blocker_codes",
            "warning_codes",
            "ma5",
        ),
        "difference_reference": "longest-left-history-prefix",
        "diagnostic_only": True,
        "active_gate_unchanged": True,
    }
)
WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_SCHEMA = (
    "chanlun-warmup-mapping-supply-diagnostic"
)
WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID = sha256_json(
    {
        "schema": WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_SCHEMA,
        "binding": (
            "warmup-convergence-envelope-and-semantic-diagnostic-content-sha256"
        ),
        "comparison": "changed-prefix-period-vs-longest-left-history-prefix",
        "point_identity": "chanlun-risk-mapping-point-identity",
        "diagnostic_only": True,
        "active_gate_unchanged": True,
    }
)
_MAPPING_SUPPLY_TRIGGER_FIELDS = frozenset(
    {
        "state",
        "mapping_unique",
        "mapped_center_id",
        "mapping_candidate_ids",
        "blocker_codes",
        "warning_codes",
    }
)


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 identity")


def _classification(
    signatures: tuple[str, ...],
) -> tuple[WarmupEnvelopeStatus, tuple[bool, ...], tuple[str, ...], str | None]:
    if len(signatures) < 3:
        return (
            "INSUFFICIENT_PREFIXES",
            tuple(False for _value in signatures),
            ("WARMUP_ENVELOPE_INSUFFICIENT_PREFIXES",),
            None,
        )
    reference = signatures[-1]
    pattern = tuple(value == reference for value in signatures)
    if all(pattern):
        return (
            "STABLE_ALL_PREFIXES",
            pattern,
            ("WARMUP_ENVELOPE_STABLE_ALL_PREFIXES",),
            reference,
        )
    matched_before_last = False
    non_monotonic = False
    for matches_reference in pattern[:-1]:
        if matches_reference:
            matched_before_last = True
        elif matched_before_last:
            non_monotonic = True
    if non_monotonic:
        return (
            "NON_MONOTONIC",
            pattern,
            (
                "WARMUP_ENVELOPE_NON_MONOTONIC",
                "ACTIVE_PAIRWISE_WARMUP_MAY_BE_FALSE_STABLE",
            ),
            reference,
        )
    return (
        "CONVERGED_ONLY_WITH_LONGER_HISTORY",
        pattern,
        (
            "WARMUP_ENVELOPE_PREFIX_SENSITIVE",
            "LONGER_HISTORY_REQUIRED_FOR_STABILITY_EVIDENCE",
        ),
        reference,
    )


@dataclass(frozen=True, slots=True)
class WarmupPrefixObservation:
    """One semantic tail signature under a specific left-history length."""

    bar_count: int
    starts_at: datetime
    signature_sha256: str

    def __post_init__(self) -> None:
        if type(self.bar_count) is not int or self.bar_count <= 0:
            raise ValueError("bar_count must be a positive integer")
        object.__setattr__(
            self,
            "starts_at",
            normalize_datetime(self.starts_at, "starts_at"),
        )
        _require_sha256(self.signature_sha256, "signature_sha256")

    def document(self) -> dict[str, object]:
        return {
            "bar_count": self.bar_count,
            "starts_at": self.starts_at.isoformat(),
            "signature_sha256": self.signature_sha256,
        }

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> WarmupPrefixObservation:
        try:
            if type(value["bar_count"]) is not int:
                raise ValueError("warmup prefix count must be an exact integer")
            result = cls(
                bar_count=int(value["bar_count"]),
                starts_at=datetime.fromisoformat(str(value["starts_at"])),
                signature_sha256=str(value["signature_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup prefix observation is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("warmup prefix observation is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupPeriodSemanticFacts:
    """Human-readable M/W/D facts behind one opaque semantic signature."""

    period: str
    state: str
    evidence_bar_end: datetime | None
    active_top_interval: tuple[datetime, datetime] | None
    mapping_unique: bool
    mapped_center_id: str | None
    mapping_candidate_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.period not in _PERIODS or not isinstance(self.state, str):
            raise ValueError("warmup semantic period/state is invalid")
        if type(self.mapping_unique) is not bool:
            raise ValueError("mapping_unique must be an exact bool")
        if self.evidence_bar_end is not None:
            object.__setattr__(
                self,
                "evidence_bar_end",
                normalize_datetime(self.evidence_bar_end, "evidence_bar_end"),
            )
        if self.active_top_interval is not None:
            if (
                type(self.active_top_interval) is not tuple
                or len(self.active_top_interval) != 2
            ):
                raise ValueError("active_top_interval must contain two times")
            normalized = tuple(
                normalize_datetime(value, "active_top_interval")
                for value in self.active_top_interval
            )
            if normalized[0] > normalized[1]:
                raise ValueError("active_top_interval is reversed")
            object.__setattr__(self, "active_top_interval", normalized)
        for field_name in (
            "mapping_candidate_ids",
            "blocker_codes",
            "warning_codes",
        ):
            values = tuple(getattr(self, field_name))
            if (
                any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{field_name} must contain unique strings")
            object.__setattr__(self, field_name, values)
        if self.mapped_center_id is not None and (
            not isinstance(self.mapped_center_id, str)
            or not self.mapped_center_id
        ):
            raise ValueError("mapped_center_id must be non-empty when present")

    def signature_document(self) -> dict[str, object]:
        """Recreate the exact canonical document used by the existing hash gate."""

        return {
            "period": self.period,
            "state": self.state,
            "evidence_bar_end": self.evidence_bar_end,
            "active_top_interval": self.active_top_interval,
            "mapping_unique": self.mapping_unique,
            "mapped_center_id": self.mapped_center_id,
            "mapping_candidate_ids": self.mapping_candidate_ids,
            "blocker_codes": self.blocker_codes,
            "warning_codes": self.warning_codes,
        }

    def document(self) -> dict[str, object]:
        return {
            "period": self.period,
            "state": self.state,
            "evidence_bar_end": (
                None
                if self.evidence_bar_end is None
                else self.evidence_bar_end.isoformat()
            ),
            "active_top_interval": (
                None
                if self.active_top_interval is None
                else [value.isoformat() for value in self.active_top_interval]
            ),
            "mapping_unique": self.mapping_unique,
            "mapped_center_id": self.mapped_center_id,
            "mapping_candidate_ids": list(self.mapping_candidate_ids),
            "blocker_codes": list(self.blocker_codes),
            "warning_codes": list(self.warning_codes),
        }

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> WarmupPeriodSemanticFacts:
        try:
            interval = value["active_top_interval"]
            if interval is not None and (
                not isinstance(interval, list) or len(interval) != 2
            ):
                raise ValueError("semantic active interval is malformed")
            if type(value["mapping_unique"]) is not bool:
                raise ValueError("semantic mapping flag must be an exact bool")
            result = cls(
                period=str(value["period"]),
                state=str(value["state"]),
                evidence_bar_end=(
                    None
                    if value["evidence_bar_end"] is None
                    else datetime.fromisoformat(str(value["evidence_bar_end"]))
                ),
                active_top_interval=(
                    None
                    if interval is None
                    else tuple(
                        datetime.fromisoformat(str(item)) for item in interval
                    )
                ),  # type: ignore[arg-type]
                mapping_unique=value["mapping_unique"],
                mapped_center_id=(
                    None
                    if value["mapped_center_id"] is None
                    else str(value["mapped_center_id"])
                ),
                mapping_candidate_ids=tuple(
                    str(item) for item in value["mapping_candidate_ids"]
                ),
                blocker_codes=tuple(
                    str(item) for item in value["blocker_codes"]
                ),
                warning_codes=tuple(
                    str(item) for item in value["warning_codes"]
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup semantic period is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("warmup semantic period is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupSemanticSnapshot:
    """The complete M/W/D tail state used for one convergence signature."""

    periods: tuple[WarmupPeriodSemanticFacts, ...]
    ma5: tuple[tuple[str, Decimal | None], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods", tuple(self.periods))
        object.__setattr__(self, "ma5", tuple(self.ma5))
        if tuple(value.period for value in self.periods) != _PERIODS:
            raise ValueError("semantic periods must be ordered M/W/D")
        if tuple(value[0] for value in self.ma5) != _PERIODS:
            raise ValueError("semantic MA5 values must be ordered M/W/D")
        normalized_ma5: list[tuple[str, Decimal | None]] = []
        for period, raw in self.ma5:
            value = None if raw is None else Decimal(raw)
            if value is not None and not value.is_finite():
                raise ValueError("semantic MA5 values must be finite")
            normalized_ma5.append((period, value))
        object.__setattr__(self, "ma5", tuple(normalized_ma5))

    @property
    def signature_sha256(self) -> str:
        return sha256_json(
            {
                "schema": "chanlun-qmt-mwd-warmup-semantic-tail",
                "states": tuple(
                    value.signature_document() for value in self.periods
                ),
                "ma5": self.ma5,
            }
        )

    def document(self) -> dict[str, object]:
        return {
            "periods": [value.document() for value in self.periods],
            "ma5": [
                {
                    "period": period,
                    "value": None if value is None else format(value, "f"),
                }
                for period, value in self.ma5
            ],
            "signature_sha256": self.signature_sha256,
        }

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> WarmupSemanticSnapshot:
        try:
            periods = value["periods"]
            ma5 = value["ma5"]
            if not isinstance(periods, list) or not isinstance(ma5, list):
                raise ValueError("semantic snapshot sequences are malformed")
            ma5_values: list[tuple[str, Decimal | None]] = []
            for raw in ma5:
                if not isinstance(raw, Mapping):
                    raise ValueError("semantic MA5 row is malformed")
                ma5_values.append(
                    (
                        str(raw["period"]),
                        None
                        if raw["value"] is None
                        else Decimal(str(raw["value"])),
                    )
                )
            result = cls(
                periods=tuple(
                    WarmupPeriodSemanticFacts.from_document(raw)
                    for raw in periods
                    if isinstance(raw, Mapping)
                ),
                ma5=tuple(ma5_values),
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup semantic snapshot is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("warmup semantic snapshot is non-canonical")
        return result


def _semantic_difference_paths(
    value: WarmupSemanticSnapshot,
    reference: WarmupSemanticSnapshot,
) -> tuple[str, ...]:
    paths: list[str] = []
    fields = (
        "state",
        "evidence_bar_end",
        "active_top_interval",
        "mapping_unique",
        "mapped_center_id",
        "mapping_candidate_ids",
        "blocker_codes",
        "warning_codes",
    )
    for candidate, longest in zip(value.periods, reference.periods):
        for field_name in fields:
            if getattr(candidate, field_name) != getattr(longest, field_name):
                paths.append(f"{candidate.period}.{field_name}")
    for (period, candidate), (_, longest) in zip(value.ma5, reference.ma5):
        if candidate != longest:
            paths.append(f"{period}.ma5")
    return tuple(paths)


@dataclass(frozen=True, slots=True)
class WarmupSemanticObservation:
    bar_count: int
    starts_at: datetime
    snapshot: WarmupSemanticSnapshot

    def __post_init__(self) -> None:
        if type(self.bar_count) is not int or self.bar_count <= 0:
            raise ValueError("semantic observation bar_count must be positive")
        object.__setattr__(
            self,
            "starts_at",
            normalize_datetime(self.starts_at, "starts_at"),
        )
        if not isinstance(self.snapshot, WarmupSemanticSnapshot):
            raise ValueError("semantic observation snapshot is required")

    @property
    def signature_sha256(self) -> str:
        return self.snapshot.signature_sha256

    def document(
        self,
        *,
        reference: WarmupSemanticSnapshot,
        matches_longest: bool,
    ) -> dict[str, object]:
        return {
            "bar_count": self.bar_count,
            "starts_at": self.starts_at.isoformat(),
            "signature_sha256": self.signature_sha256,
            "matches_longest": matches_longest,
            "changed_paths_from_longest": list(
                _semantic_difference_paths(self.snapshot, reference)
            ),
            "snapshot": self.snapshot.document(),
        }


@dataclass(frozen=True, slots=True)
class WarmupConvergenceDiagnosticEnvelope:
    """Auditable explanation of which M/W/D facts changed by prefix."""

    frequency: str
    as_of: datetime
    parameter_set_id: str
    envelope_content_sha256: str
    observations: tuple[WarmupSemanticObservation, ...]
    status: WarmupEnvelopeStatus
    reference_signature_sha256: str | None
    match_longest_pattern: tuple[bool, ...]
    schema: str = WARMUP_CONVERGENCE_DIAGNOSTIC_SCHEMA
    contract_id: str = WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
    diagnostic_only: bool = True
    active_gate_unchanged: bool = True
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("diagnostic frequency is required")
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        _require_sha256(self.parameter_set_id, "parameter_set_id")
        _require_sha256(self.envelope_content_sha256, "envelope_content_sha256")
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(
            self, "match_longest_pattern", tuple(self.match_longest_pattern)
        )
        counts = tuple(value.bar_count for value in self.observations)
        if counts != tuple(sorted(set(counts))):
            raise ValueError("semantic observations must have increasing counts")
        expected = _classification(
            tuple(value.signature_sha256 for value in self.observations)
        )
        if (
            self.status != expected[0]
            or self.match_longest_pattern != expected[1]
            or self.reference_signature_sha256 != expected[3]
        ):
            raise ValueError("semantic diagnostic classification is inconsistent")
        if (
            self.schema != WARMUP_CONVERGENCE_DIAGNOSTIC_SCHEMA
            or self.contract_id != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
            or self.diagnostic_only is not True
            or self.active_gate_unchanged is not True
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("semantic diagnostic safety contract is immutable")

    def _stable_document(self) -> dict[str, object]:
        reference = (
            None if not self.observations else self.observations[-1].snapshot
        )
        return {
            "schema": self.schema,
            "contract_id": self.contract_id,
            "frequency": self.frequency,
            "as_of": self.as_of.isoformat(),
            "parameter_set_id": self.parameter_set_id,
            "envelope_content_sha256": self.envelope_content_sha256,
            "observations": [
                value.document(
                    reference=reference,
                    matches_longest=self.match_longest_pattern[index],
                )
                for index, value in enumerate(self.observations)
            ]
            if reference is not None
            else [],
            "observation_count": len(self.observations),
            "status": self.status,
            "reference_signature_sha256": self.reference_signature_sha256,
            "match_longest_pattern": list(self.match_longest_pattern),
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
        if (
            self.frequency != envelope.frequency
            or self.as_of != envelope.as_of
            or self.parameter_set_id != envelope.parameter_set_id
            or self.envelope_content_sha256 != envelope.content_sha256
            or self.status != envelope.status
            or self.reference_signature_sha256
            != envelope.reference_signature_sha256
            or self.match_longest_pattern != envelope.match_longest_pattern
            or tuple(
                (value.bar_count, value.starts_at, value.signature_sha256)
                for value in self.observations
            )
            != tuple(
                (value.bar_count, value.starts_at, value.signature_sha256)
                for value in envelope.observations
            )
        ):
            raise ValueError("semantic diagnostic does not bind its envelope")

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> WarmupConvergenceDiagnosticEnvelope:
        try:
            raw_observations = value["observations"]
            pattern = value["match_longest_pattern"]
            if (
                not isinstance(raw_observations, list)
                or not isinstance(pattern, list)
                or type(value["observation_count"]) is not int
                or any(type(item) is not bool for item in pattern)
                or value["diagnostic_only"] is not True
                or value["active_gate_unchanged"] is not True
            ):
                raise ValueError("semantic diagnostic scalar types are invalid")
            observations: list[WarmupSemanticObservation] = []
            for raw in raw_observations:
                if not isinstance(raw, Mapping):
                    raise ValueError("semantic observation is malformed")
                observations.append(
                    WarmupSemanticObservation(
                        bar_count=raw["bar_count"],  # type: ignore[arg-type]
                        starts_at=datetime.fromisoformat(str(raw["starts_at"])),
                        snapshot=WarmupSemanticSnapshot.from_document(
                            raw["snapshot"]  # type: ignore[arg-type]
                        ),
                    )
                )
            result = cls(
                frequency=str(value["frequency"]),
                as_of=datetime.fromisoformat(str(value["as_of"])),
                parameter_set_id=str(value["parameter_set_id"]),
                envelope_content_sha256=str(value["envelope_content_sha256"]),
                observations=tuple(observations),
                status=str(value["status"]),  # type: ignore[arg-type]
                reference_signature_sha256=(
                    None
                    if value["reference_signature_sha256"] is None
                    else str(value["reference_signature_sha256"])
                ),
                match_longest_pattern=tuple(pattern),
                schema=str(value["schema"]),
                contract_id=str(value["contract_id"]),
                diagnostic_only=value["diagnostic_only"],  # type: ignore[arg-type]
                active_gate_unchanged=value["active_gate_unchanged"],  # type: ignore[arg-type]
                live_status=str(value["live_status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup semantic diagnostic is malformed") from exc
        if value.get("observation_count") != len(result.observations):
            raise ValueError("semantic diagnostic observation count changed")
        if dict(value) != result.document():
            raise ValueError("warmup semantic diagnostic is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupMappingSupplySnapshot:
    """M/W/D lower-structure mapping supply for one left-history prefix."""

    periods: tuple[tuple[str, RiskMappingSupplyFacts | None], ...]

    def __post_init__(self) -> None:
        values = tuple(self.periods)
        if tuple(period for period, _supply in values) != _PERIODS:
            raise ValueError("mapping supply periods must be ordered M/W/D")
        if any(
            supply is not None and not isinstance(supply, RiskMappingSupplyFacts)
            for _period, supply in values
        ):
            raise ValueError("warmup mapping supply snapshot is malformed")
        object.__setattr__(self, "periods", values)

    def for_period(self, period: str) -> RiskMappingSupplyFacts | None:
        return dict(self.periods)[period]


def _mapping_point_rows(
    supply: RiskMappingSupplyFacts | None,
) -> tuple[object, ...]:
    if supply is None or supply.point_evidence is None:
        return ()
    return tuple(sorted(supply.point_evidence, key=lambda value: value.point_id))


def _mapping_supply_delta_document(
    prefix: RiskMappingSupplyFacts | None,
    reference: RiskMappingSupplyFacts | None,
) -> dict[str, object]:
    prefix_points = {value.point_id: value for value in _mapping_point_rows(prefix)}
    reference_points = {
        value.point_id: value for value in _mapping_point_rows(reference)
    }
    lost_ids = tuple(sorted(set(prefix_points).difference(reference_points)))
    gained_ids = tuple(sorted(set(reference_points).difference(prefix_points)))
    retained_ids = tuple(sorted(set(prefix_points).intersection(reference_points)))
    lost_candidates = tuple(
        value
        for value in lost_ids
        if prefix_points[value].highest_mapping_candidate
    )
    gained_candidates = tuple(
        value
        for value in gained_ids
        if reference_points[value].highest_mapping_candidate
    )

    codes: list[str] = []
    if prefix is None and reference is None:
        codes.append("MAPPING_SUPPLY_NOT_RECORDED")
    elif prefix is None:
        codes.append("MAPPING_SUPPLY_APPEARED_WITH_LONGER_HISTORY")
    elif reference is None:
        codes.append("MAPPING_SUPPLY_DISAPPEARED_WITH_LONGER_HISTORY")
    else:
        if prefix.classification != reference.classification:
            codes.append("SUPPLY_CLASSIFICATION_CHANGED")
        prefix_sell12 = prefix.point_type_counts[0][1] + prefix.point_type_counts[1][1]
        reference_sell12 = (
            reference.point_type_counts[0][1]
            + reference.point_type_counts[1][1]
        )
        if prefix_sell12 and not reference_sell12:
            codes.append("SELL12_DISAPPEARED_WITH_LONGER_HISTORY")
        elif not prefix_sell12 and reference_sell12:
            codes.append("SELL12_APPEARED_WITH_LONGER_HISTORY")
        if (
            prefix.completed_in_top_interval_sell12_count
            and not reference.completed_in_top_interval_sell12_count
        ):
            codes.append(
                "COMPLETED_IN_INTERVAL_SELL12_DISAPPEARED_WITH_LONGER_HISTORY"
            )
        elif (
            not prefix.completed_in_top_interval_sell12_count
            and reference.completed_in_top_interval_sell12_count
        ):
            codes.append(
                "COMPLETED_IN_INTERVAL_SELL12_APPEARED_WITH_LONGER_HISTORY"
            )
        if (
            prefix.highest_candidate_center_count
            and not reference.highest_candidate_center_count
        ):
            codes.append("HIGHEST_CANDIDATE_DISAPPEARED_WITH_LONGER_HISTORY")
        elif (
            not prefix.highest_candidate_center_count
            and reference.highest_candidate_center_count
        ):
            codes.append("HIGHEST_CANDIDATE_APPEARED_WITH_LONGER_HISTORY")
        if prefix.point_evidence is None or reference.point_evidence is None:
            codes.append("POINT_IDENTITIES_NOT_RECORDED")
        else:
            if lost_ids:
                codes.append("POINT_EVIDENCE_LOST_WITH_LONGER_HISTORY")
            if gained_ids:
                codes.append("POINT_EVIDENCE_GAINED_WITH_LONGER_HISTORY")
            if lost_ids and gained_ids:
                codes.append("POINT_IDENTITY_SET_RESEGMENTED")
        if prefix.document() == reference.document():
            codes.append("MAPPING_SUPPLY_UNCHANGED")
    return {
        "prefix_classification": (
            None if prefix is None else prefix.classification
        ),
        "reference_classification": (
            None if reference is None else reference.classification
        ),
        "lost_point_ids_from_longest": list(lost_ids),
        "gained_point_ids_in_longest": list(gained_ids),
        "retained_point_ids": list(retained_ids),
        "lost_highest_candidate_point_ids": list(lost_candidates),
        "gained_highest_candidate_point_ids": list(gained_candidates),
        "lost_points_from_longest": [
            prefix_points[value].document() for value in lost_ids
        ],
        "gained_points_in_longest": [
            reference_points[value].document() for value in gained_ids
        ],
        "transition_codes": codes,
    }


@dataclass(frozen=True, slots=True)
class WarmupMappingSupplyComparison:
    """One changed prefix-period compared with the longest history prefix."""

    period: str
    prefix_bar_count: int
    prefix_starts_at: datetime
    prefix_signature_sha256: str
    reference_bar_count: int
    reference_starts_at: datetime
    reference_signature_sha256: str
    prefix_supply: RiskMappingSupplyFacts | None
    reference_supply: RiskMappingSupplyFacts | None

    def __post_init__(self) -> None:
        if self.period not in _PERIODS:
            raise ValueError("mapping supply comparison period is invalid")
        if (
            type(self.prefix_bar_count) is not int
            or type(self.reference_bar_count) is not int
            or self.prefix_bar_count <= 0
            or self.prefix_bar_count >= self.reference_bar_count
        ):
            raise ValueError("mapping supply comparison counts are invalid")
        object.__setattr__(
            self,
            "prefix_starts_at",
            normalize_datetime(self.prefix_starts_at, "prefix_starts_at"),
        )
        object.__setattr__(
            self,
            "reference_starts_at",
            normalize_datetime(self.reference_starts_at, "reference_starts_at"),
        )
        _require_sha256(self.prefix_signature_sha256, "prefix_signature_sha256")
        _require_sha256(
            self.reference_signature_sha256, "reference_signature_sha256"
        )
        if self.prefix_supply is not None and not isinstance(
            self.prefix_supply, RiskMappingSupplyFacts
        ):
            raise ValueError("prefix mapping supply is malformed")
        if self.reference_supply is not None and not isinstance(
            self.reference_supply, RiskMappingSupplyFacts
        ):
            raise ValueError("reference mapping supply is malformed")

    def document(self) -> dict[str, object]:
        return {
            "period": self.period,
            "prefix_bar_count": self.prefix_bar_count,
            "prefix_starts_at": self.prefix_starts_at.isoformat(),
            "prefix_signature_sha256": self.prefix_signature_sha256,
            "reference_bar_count": self.reference_bar_count,
            "reference_starts_at": self.reference_starts_at.isoformat(),
            "reference_signature_sha256": self.reference_signature_sha256,
            "prefix_supply": (
                None if self.prefix_supply is None else self.prefix_supply.document()
            ),
            "reference_supply": (
                None
                if self.reference_supply is None
                else self.reference_supply.document()
            ),
            "delta": _mapping_supply_delta_document(
                self.prefix_supply, self.reference_supply
            ),
        }

    @classmethod
    def from_document(
        cls, value: Mapping[str, object]
    ) -> WarmupMappingSupplyComparison:
        try:
            prefix_raw = value["prefix_supply"]
            reference_raw = value["reference_supply"]
            result = cls(
                period=str(value["period"]),
                prefix_bar_count=value["prefix_bar_count"],  # type: ignore[arg-type]
                prefix_starts_at=datetime.fromisoformat(
                    str(value["prefix_starts_at"])
                ),
                prefix_signature_sha256=str(value["prefix_signature_sha256"]),
                reference_bar_count=value["reference_bar_count"],  # type: ignore[arg-type]
                reference_starts_at=datetime.fromisoformat(
                    str(value["reference_starts_at"])
                ),
                reference_signature_sha256=str(
                    value["reference_signature_sha256"]
                ),
                prefix_supply=(
                    None
                    if prefix_raw is None
                    else RiskMappingSupplyFacts.from_document(prefix_raw)
                ),
                reference_supply=(
                    None
                    if reference_raw is None
                    else RiskMappingSupplyFacts.from_document(reference_raw)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup mapping supply comparison is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("warmup mapping supply comparison is non-canonical")
        return result


def _supply_matches_semantic_period(
    supply: RiskMappingSupplyFacts | None,
    semantic: WarmupPeriodSemanticFacts,
) -> bool:
    if supply is None:
        return semantic.active_top_interval is None
    if semantic.active_top_interval is None:
        return False
    if supply.highest_candidate_center_count != len(
        semantic.mapping_candidate_ids
    ):
        return False
    if semantic.mapped_center_id is not None:
        if (
            semantic.mapping_candidate_ids != (semantic.mapped_center_id,)
            or supply.classification != "UNIQUE_MAPPING"
        ):
            return False
        if supply.point_evidence is not None:
            highest = {
                value.center_id
                for value in supply.point_evidence
                if value.highest_mapping_candidate
            }
            if highest != {semantic.mapped_center_id}:
                return False
    return True


@dataclass(frozen=True, slots=True)
class WarmupMappingSupplyDiagnosticEnvelope:
    """Point-level explanation of prefix-sensitive lower-center mapping supply."""

    frequency: str
    as_of: datetime
    parameter_set_id: str
    envelope_content_sha256: str
    semantic_diagnostic_content_sha256: str
    status: WarmupEnvelopeStatus
    comparisons: tuple[WarmupMappingSupplyComparison, ...]
    schema: str = WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_SCHEMA
    contract_id: str = WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
    diagnostic_only: bool = True
    active_gate_unchanged: bool = True
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("mapping supply diagnostic frequency is required")
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        _require_sha256(self.parameter_set_id, "parameter_set_id")
        _require_sha256(self.envelope_content_sha256, "envelope_content_sha256")
        _require_sha256(
            self.semantic_diagnostic_content_sha256,
            "semantic_diagnostic_content_sha256",
        )
        values = tuple(self.comparisons)
        keys = tuple(
            (_PERIODS.index(value.period), value.prefix_bar_count)
            for value in values
        )
        if len(keys) != len(set(keys)):
            raise ValueError("mapping supply comparisons must be unique")
        if values != tuple(
            sorted(
                values,
                key=lambda value: (value.prefix_bar_count, _PERIODS.index(value.period)),
            )
        ):
            raise ValueError("mapping supply comparisons must be ordered")
        object.__setattr__(self, "comparisons", values)
        if self.status not in {
            "STABLE_ALL_PREFIXES",
            "CONVERGED_ONLY_WITH_LONGER_HISTORY",
            "NON_MONOTONIC",
            "INSUFFICIENT_PREFIXES",
        }:
            raise ValueError("mapping supply diagnostic status is invalid")
        if (
            self.schema != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_SCHEMA
            or self.contract_id != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
            or self.diagnostic_only is not True
            or self.active_gate_unchanged is not True
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("mapping supply diagnostic safety contract is immutable")

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
        if semantic is None:
            raise ValueError("mapping supply diagnostic requires semantic evidence")
        semantic.validate_against(envelope)
        if (
            self.frequency != envelope.frequency
            or self.as_of != envelope.as_of
            or self.parameter_set_id != envelope.parameter_set_id
            or self.envelope_content_sha256 != envelope.content_sha256
            or self.semantic_diagnostic_content_sha256 != semantic.content_sha256
            or self.status != envelope.status
        ):
            raise ValueError("mapping supply diagnostic does not bind its envelope")
        reference = semantic.observations[-1] if semantic.observations else None
        expected: dict[tuple[int, str], tuple[WarmupSemanticObservation, str]] = {}
        if reference is not None:
            for observation in semantic.observations[:-1]:
                changed = _semantic_difference_paths(
                    observation.snapshot, reference.snapshot
                )
                periods = {
                    period
                    for path in changed
                    for period, separator, field_name in (path.partition("."),)
                    if separator and field_name in _MAPPING_SUPPLY_TRIGGER_FIELDS
                }
                for period in periods:
                    expected[(observation.bar_count, period)] = (
                        observation,
                        period,
                    )
        actual = {
            (value.prefix_bar_count, value.period): value
            for value in self.comparisons
        }
        if set(actual) != set(expected):
            raise ValueError("mapping supply diagnostic comparison coverage changed")
        if reference is None:
            return
        reference_periods = {
            value.period: value for value in reference.snapshot.periods
        }
        for key, (observation, period) in expected.items():
            comparison = actual[key]
            prefix_periods = {
                value.period: value for value in observation.snapshot.periods
            }
            if (
                comparison.prefix_starts_at != observation.starts_at
                or comparison.prefix_signature_sha256
                != observation.signature_sha256
                or comparison.reference_bar_count != reference.bar_count
                or comparison.reference_starts_at != reference.starts_at
                or comparison.reference_signature_sha256
                != reference.signature_sha256
                or not _supply_matches_semantic_period(
                    comparison.prefix_supply, prefix_periods[period]
                )
                or not _supply_matches_semantic_period(
                    comparison.reference_supply, reference_periods[period]
                )
            ):
                raise ValueError("mapping supply diagnostic facts changed")

    @classmethod
    def from_document(
        cls, value: Mapping[str, object]
    ) -> WarmupMappingSupplyDiagnosticEnvelope:
        try:
            raw_comparisons = value["comparisons"]
            if (
                not isinstance(raw_comparisons, list)
                or type(value["comparison_count"]) is not int
                or value["diagnostic_only"] is not True
                or value["active_gate_unchanged"] is not True
            ):
                raise ValueError("mapping supply diagnostic scalar types are invalid")
            result = cls(
                frequency=str(value["frequency"]),
                as_of=datetime.fromisoformat(str(value["as_of"])),
                parameter_set_id=str(value["parameter_set_id"]),
                envelope_content_sha256=str(value["envelope_content_sha256"]),
                semantic_diagnostic_content_sha256=str(
                    value["semantic_diagnostic_content_sha256"]
                ),
                status=str(value["status"]),  # type: ignore[arg-type]
                comparisons=tuple(
                    WarmupMappingSupplyComparison.from_document(raw)
                    for raw in raw_comparisons
                    if isinstance(raw, Mapping)
                ),
                schema=str(value["schema"]),
                contract_id=str(value["contract_id"]),
                diagnostic_only=value["diagnostic_only"],  # type: ignore[arg-type]
                active_gate_unchanged=value["active_gate_unchanged"],  # type: ignore[arg-type]
                live_status=str(value["live_status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup mapping supply diagnostic is malformed") from exc
        if value.get("comparison_count") != len(result.comparisons):
            raise ValueError("mapping supply diagnostic comparison count changed")
        if dict(value) != result.document():
            raise ValueError("warmup mapping supply diagnostic is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class WarmupConvergenceEnvelope:
    """Multi-prefix evidence that cannot be mistaken for a trading gate."""

    frequency: str
    as_of: datetime
    parameter_set_id: str
    observations: tuple[WarmupPrefixObservation, ...]
    status: WarmupEnvelopeStatus
    stable_all_prefixes: bool
    reference_signature_sha256: str | None
    match_longest_pattern: tuple[bool, ...]
    reason_codes: tuple[str, ...]
    schema: str = WARMUP_CONVERGENCE_ENVELOPE_SCHEMA
    contract_id: str = WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
    diagnostic_only: bool = True
    active_gate_unchanged: bool = True
    live_status: str = "LIVE_DISABLED"
        # 收敛信封逐字节保持稳定。便于阅读的语义事实位于单独计算哈希、并绑定信封的
        # 诊断文件中，特意不参与相等性与收敛内容标识计算。
    diagnostic: WarmupConvergenceDiagnosticEnvelope | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    mapping_supply_diagnostic: WarmupMappingSupplyDiagnosticEnvelope | None = field(
        default=None,
        compare=False,
        repr=False,
    )
        # 第三个独立哈希的同级文件记录严格笔、中枢和触发线血缘。它特意不进入
        # 收敛文档与相等性计算，避免新增人工复核证据改变正在使用的门控。
    structure_lineage_diagnostic: (
        WarmupStructureLineageDiagnosticEnvelope | None
    ) = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency is required")
        object.__setattr__(
            self,
            "as_of",
            normalize_datetime(self.as_of, "as_of"),
        )
        _require_sha256(self.parameter_set_id, "parameter_set_id")
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(
            self,
            "match_longest_pattern",
            tuple(self.match_longest_pattern),
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        counts = tuple(item.bar_count for item in self.observations)
        if counts != tuple(sorted(set(counts))):
            raise ValueError("observations must have unique increasing bar counts")
        if len(self.match_longest_pattern) != len(self.observations):
            raise ValueError("match pattern must align with observations")
        if any(type(value) is not bool for value in self.match_longest_pattern):
            raise ValueError("match pattern values must be exact booleans")
        if (
            any(not isinstance(value, str) or not value for value in self.reason_codes)
            or len(self.reason_codes) != len(set(self.reason_codes))
        ):
            raise ValueError("reason_codes must be unique")
        if self.status not in {
            "STABLE_ALL_PREFIXES",
            "CONVERGED_ONLY_WITH_LONGER_HISTORY",
            "NON_MONOTONIC",
            "INSUFFICIENT_PREFIXES",
        }:
            raise ValueError("invalid warmup envelope status")
        if type(self.stable_all_prefixes) is not bool:
            raise ValueError("stable_all_prefixes must be an exact bool")
        if self.reference_signature_sha256 is not None:
            _require_sha256(
                self.reference_signature_sha256,
                "reference_signature_sha256",
            )
        expected = _classification(
            tuple(value.signature_sha256 for value in self.observations)
        )
        if (
            self.status != expected[0]
            or self.match_longest_pattern != expected[1]
            or self.reason_codes != expected[2]
            or self.reference_signature_sha256 != expected[3]
            or self.stable_all_prefixes
            != (expected[0] == "STABLE_ALL_PREFIXES")
        ):
            raise ValueError("warmup envelope classification is inconsistent")
        if (
            self.schema != WARMUP_CONVERGENCE_ENVELOPE_SCHEMA
            or self.contract_id != WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
            or self.diagnostic_only is not True
            or self.active_gate_unchanged is not True
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("warmup envelope safety contract is immutable")
        if self.diagnostic is not None:
            if not isinstance(
                self.diagnostic, WarmupConvergenceDiagnosticEnvelope
            ):
                raise ValueError("warmup convergence diagnostic is invalid")
            self.diagnostic.validate_against(self)
        if self.mapping_supply_diagnostic is not None:
            if not isinstance(
                self.mapping_supply_diagnostic,
                WarmupMappingSupplyDiagnosticEnvelope,
            ):
                raise ValueError("warmup mapping supply diagnostic is invalid")
            self.mapping_supply_diagnostic.validate_against(self)
        if self.structure_lineage_diagnostic is not None:
            from chanlun.decision_support.trading_system.warmup_structure_lineage import (
                WarmupStructureLineageDiagnosticEnvelope,
            )

            if not isinstance(
                self.structure_lineage_diagnostic,
                WarmupStructureLineageDiagnosticEnvelope,
            ):
                raise ValueError("warmup structure lineage diagnostic is invalid")
            self.structure_lineage_diagnostic.validate_against(self)

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "contract_id": self.contract_id,
            "frequency": self.frequency,
            "as_of": self.as_of.isoformat(),
            "parameter_set_id": self.parameter_set_id,
            "observations": [value.document() for value in self.observations],
            "observation_count": len(self.observations),
            "status": self.status,
            "stable_all_prefixes": self.stable_all_prefixes,
            "reference_signature_sha256": self.reference_signature_sha256,
            "match_longest_pattern": list(self.match_longest_pattern),
            "reason_codes": list(self.reason_codes),
            "diagnostic_only": self.diagnostic_only,
            "active_gate_unchanged": self.active_gate_unchanged,
            "live_status": self.live_status,
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        return {**self._stable_document(), "content_sha256": self.content_sha256}

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> WarmupConvergenceEnvelope:
        try:
            observations = value["observations"]
            pattern = value["match_longest_pattern"]
            reasons = value["reason_codes"]
            if (
                not isinstance(observations, list)
                or not isinstance(pattern, list)
                or not isinstance(reasons, list)
                or type(value["observation_count"]) is not int
                or type(value["stable_all_prefixes"]) is not bool
                or any(type(item) is not bool for item in pattern)
                or value["diagnostic_only"] is not True
                or value["active_gate_unchanged"] is not True
            ):
                raise ValueError("warmup convergence scalar types are invalid")
            result = cls(
                frequency=str(value["frequency"]),
                as_of=datetime.fromisoformat(str(value["as_of"])),
                parameter_set_id=str(value["parameter_set_id"]),
                observations=tuple(
                    WarmupPrefixObservation.from_document(item)
                    for item in observations
                    if isinstance(item, Mapping)
                ),
                status=str(value["status"]),  # type: ignore[arg-type]
                stable_all_prefixes=bool(value["stable_all_prefixes"]),
                reference_signature_sha256=(
                    None
                    if value["reference_signature_sha256"] is None
                    else str(value["reference_signature_sha256"])
                ),
                match_longest_pattern=tuple(pattern),
                reason_codes=tuple(str(item) for item in reasons),
                schema=str(value["schema"]),
                contract_id=str(value["contract_id"]),
                diagnostic_only=bool(value["diagnostic_only"]),
                active_gate_unchanged=bool(value["active_gate_unchanged"]),
                live_status=str(value["live_status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("warmup convergence envelope is malformed") from exc
        if value.get("observation_count") != len(result.observations):
            raise ValueError("warmup convergence observation count changed")
        if dict(value) != result.document():
            raise ValueError("warmup convergence envelope is non-canonical")
        return result


def classify_warmup_convergence_envelope(
    *,
    frequency: str,
    as_of: datetime,
    parameter_set_id: str,
    observations: tuple[WarmupPrefixObservation, ...],
) -> WarmupConvergenceEnvelope:
    """Classify prefix sensitivity against the longest-history signature.

    Observations are ordered from the shortest to the longest left-history
    prefix.  ``A, B, A`` is explicitly non-monotonic: the shortest prefix
    agrees with the longest, while an intermediate prefix does not.  A simple
    ``A, B, C`` sequence remains prefix-sensitive and is reported as requiring
    longer history; it is not promoted to stable.
    """

    values = tuple(observations)
    status, pattern, reasons, reference = _classification(
        tuple(value.signature_sha256 for value in values)
    )
    return WarmupConvergenceEnvelope(
        frequency=frequency,
        as_of=as_of,
        parameter_set_id=parameter_set_id,
        observations=values,
        status=status,
        stable_all_prefixes=status == "STABLE_ALL_PREFIXES",
        reference_signature_sha256=reference,
        match_longest_pattern=pattern,
        reason_codes=reasons,
    )


def bind_warmup_convergence_diagnostic(
    envelope: WarmupConvergenceEnvelope,
    *,
    snapshots: tuple[WarmupSemanticSnapshot, ...],
) -> WarmupConvergenceEnvelope:
    """Attach explanatory M/W/D facts without changing the convergence gate identity."""

    values = tuple(snapshots)
    if len(values) != len(envelope.observations):
        raise ValueError("semantic snapshots must align with prefix observations")
    observations = tuple(
        WarmupSemanticObservation(
            bar_count=prefix.bar_count,
            starts_at=prefix.starts_at,
            snapshot=snapshot,
        )
        for prefix, snapshot in zip(envelope.observations, values)
    )
    diagnostic = WarmupConvergenceDiagnosticEnvelope(
        frequency=envelope.frequency,
        as_of=envelope.as_of,
        parameter_set_id=envelope.parameter_set_id,
        envelope_content_sha256=envelope.content_sha256,
        observations=observations,
        status=envelope.status,
        reference_signature_sha256=envelope.reference_signature_sha256,
        match_longest_pattern=envelope.match_longest_pattern,
    )
    return replace(envelope, diagnostic=diagnostic)


def bind_warmup_mapping_supply_diagnostic(
    envelope: WarmupConvergenceEnvelope,
    *,
    snapshots: tuple[WarmupMappingSupplySnapshot, ...],
) -> WarmupConvergenceEnvelope:
    """Attach point-level supply deltas without changing either convergence identity."""

    semantic = envelope.diagnostic
    if semantic is None:
        raise ValueError("semantic diagnostic must be bound before mapping supply")
    values = tuple(snapshots)
    if len(values) != len(semantic.observations):
        raise ValueError("mapping supply snapshots must align with observations")
    reference = semantic.observations[-1] if semantic.observations else None
    comparisons: list[WarmupMappingSupplyComparison] = []
    if reference is not None:
        reference_supply = values[-1]
        for index, observation in enumerate(semantic.observations[:-1]):
            changed = _semantic_difference_paths(
                observation.snapshot, reference.snapshot
            )
            periods = {
                period
                for path in changed
                for period, separator, field_name in (path.partition("."),)
                if separator and field_name in _MAPPING_SUPPLY_TRIGGER_FIELDS
            }
            for period in _PERIODS:
                if period not in periods:
                    continue
                comparisons.append(
                    WarmupMappingSupplyComparison(
                        period=period,
                        prefix_bar_count=observation.bar_count,
                        prefix_starts_at=observation.starts_at,
                        prefix_signature_sha256=observation.signature_sha256,
                        reference_bar_count=reference.bar_count,
                        reference_starts_at=reference.starts_at,
                        reference_signature_sha256=reference.signature_sha256,
                        prefix_supply=values[index].for_period(period),
                        reference_supply=reference_supply.for_period(period),
                    )
                )
    diagnostic = WarmupMappingSupplyDiagnosticEnvelope(
        frequency=envelope.frequency,
        as_of=envelope.as_of,
        parameter_set_id=envelope.parameter_set_id,
        envelope_content_sha256=envelope.content_sha256,
        semantic_diagnostic_content_sha256=semantic.content_sha256,
        status=envelope.status,
        comparisons=tuple(comparisons),
    )
    return replace(envelope, mapping_supply_diagnostic=diagnostic)


__all__ = [
    "WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID",
    "WARMUP_CONVERGENCE_DIAGNOSTIC_SCHEMA",
    "WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID",
    "WARMUP_CONVERGENCE_ENVELOPE_SCHEMA",
    "WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID",
    "WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_SCHEMA",
    "WarmupConvergenceEnvelope",
    "WarmupConvergenceDiagnosticEnvelope",
    "WarmupEnvelopeStatus",
    "WarmupMappingSupplyComparison",
    "WarmupMappingSupplyDiagnosticEnvelope",
    "WarmupMappingSupplySnapshot",
    "WarmupPrefixObservation",
    "WarmupPeriodSemanticFacts",
    "WarmupSemanticObservation",
    "WarmupSemanticSnapshot",
    "bind_warmup_convergence_diagnostic",
    "bind_warmup_mapping_supply_diagnostic",
    "classify_warmup_convergence_envelope",
]
