from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import (
    PointSide,
    PointType,
    StructureTower,
)


_POINT_TYPES = {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}


@dataclass(frozen=True, slots=True)
class ProvisionalCandidate:
    candidate_id: str
    code: str
    point_type: PointType
    side: PointSide
    status: Literal["provisional"]
    source_frequency: str
    tower: StructureTower
    recursive_level: int
    observed_at: datetime
    anchor_price: float
    missing_conditions: tuple[str, ...]
    evidence_codes: tuple[str, ...]
    actionable: Literal[False] = False

    def __post_init__(self) -> None:
        if self.status != "provisional" or self.actionable is not False:
            raise ValueError("provisional candidates must remain non-actionable")
        expected_side = "buy" if self.point_type.endswith("buy") else "sell"
        if self.side != expected_side:
            raise ValueError("point_type and side disagree")
        if self.recursive_level < 0:
            raise ValueError("recursive_level must be non-negative")
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.anchor_price <= 0:
            raise ValueError("anchor_price must be positive")
        if not self.missing_conditions:
            raise ValueError("missing_conditions cannot be empty")
        for name, values in (
            ("missing_conditions", self.missing_conditions),
            ("evidence_codes", self.evidence_codes),
        ):
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{name} must be non-empty and unique")


def _is_unfinished(line: object) -> bool:
    checker = getattr(line, "is_done", None)
    if callable(checker):
        return checker() is not True
    end = getattr(line, "end", None)
    return getattr(end, "done", None) is not True


def _point_names(line: object, tower: StructureTower) -> tuple[str, ...]:
    mapping = getattr(line, "zs_type_mmds", None) or {}
    entries = tuple(mapping.get(tower, ()))
    return tuple(
        sorted(
            {
                entry.name
                for entry in entries
                if getattr(getattr(entry, "zs", None), "real", True) is not False
                and getattr(entry, "name", None) in _POINT_TYPES
            }
        )
    )


def _has_live_trend_divergence(level: object, line: object) -> bool:
    for _zone, divergence in tuple(
        getattr(level, "live_qs_divergence", None) or ()
    ):
        if (
            getattr(divergence, "leave_seg", None) is line
            and getattr(divergence, "kind", None) == "qs"
            and getattr(divergence, "is_beichi", None) is True
            and getattr(divergence, "provisional", None) is True
        ):
            return True
    return False


def _candidate_id(
    *,
    code: str,
    point_type: PointType,
    source_frequency: str,
    tower: StructureTower,
    recursive_level: int,
    line: object,
) -> str:
    return sha256_json(
        {
            "schema": "chanlun-provisional-candidate/v1",
            "code": code,
            "point_type": point_type,
            "source_frequency": source_frequency,
            "tower": tower,
            "recursive_level": recursive_level,
            "line_start": normalize_datetime(
                line.start.k.date,
                "line.start",
            ).isoformat(),
            "line_end": normalize_datetime(
                line.end.k.date,
                "line.end",
            ).isoformat(),
        }
    )


def extract_provisional_candidates(
    cd: object,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[ProvisionalCandidate, ...]:
    closed_at = normalize_datetime(as_of, "as_of")
    output: dict[str, ProvisionalCandidate] = {}
    tower_specs: tuple[tuple[StructureTower, bool], ...] = (
        ("bi", False),
        ("xd", True),
    )
    for tower, use_xd in tower_specs:
        levels = tuple(cd.get_recursive_branch_levels_for_tower(use_xd=use_xd))
        for level in levels:
            units = tuple(getattr(level, "units", None) or ())
            if not units:
                continue
            latest = units[-1]
            if not _is_unfinished(latest):
                continue
            observed_at = normalize_datetime(latest.end.k.date, "observed_at")
            if observed_at > closed_at:
                raise ValueError("provisional dependency cannot be after as_of")
            direction = getattr(latest, "type", getattr(latest, "_type", None))
            if direction not in {"up", "down"}:
                continue
            side: PointSide = "buy" if direction == "down" else "sell"
            names = tuple(
                name
                for name in _point_names(latest, tower)
                if name.endswith(side)
            )
            evidence_code = "unfinished_core_mmd"
            if not names and _has_live_trend_divergence(level, latest):
                names = (f"1{side}",)
                evidence_code = "unfinished_trend_divergence"
            endpoint_condition = (
                "bottom_fractal_confirmed"
                if side == "buy"
                else "top_fractal_confirmed"
            )
            for name in names:
                point_type = cast(PointType, name)
                level_number = int(getattr(level, "level", 0) or 0)
                identity = _candidate_id(
                    code=code,
                    point_type=point_type,
                    source_frequency=source_frequency,
                    tower=tower,
                    recursive_level=level_number,
                    line=latest,
                )
                output[identity] = ProvisionalCandidate(
                    candidate_id=identity,
                    code=code,
                    point_type=point_type,
                    side=side,
                    status="provisional",
                    source_frequency=source_frequency,
                    tower=tower,
                    recursive_level=level_number,
                    observed_at=observed_at,
                    anchor_price=float(latest.end.val),
                    missing_conditions=(
                        endpoint_condition,
                        "terminal_line_confirmed",
                    ),
                    evidence_codes=(evidence_code,),
                )
    return tuple(
        sorted(
            output.values(),
            key=lambda candidate: (
                candidate.observed_at,
                candidate.tower,
                candidate.recursive_level,
                candidate.point_type,
            ),
        )
    )
