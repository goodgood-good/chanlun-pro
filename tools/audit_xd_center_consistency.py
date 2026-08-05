#!/usr/bin/env python3
"""Rebuild cached charts and audit original-text XD-center consistency.

The chart cache stores raw OHLCV alongside rendered structures.  This tool
deliberately ignores the cached center payload, rebuilds every selected series
with the current implementation, and fails closed on duplicated live centers,
incoherent first-three geometry, phantom departure/return evidence, or an
unresolved active center being displaced by a shifted live-edge seed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, DecimalException
import json
import os
from pathlib import Path
import pickle
import re
import sys
import time
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.cl_utils import query_cl_chart_config
from chanlun.cl_utils.strict_chart import build_strict_structure_snapshot
from chanlun.cl_utils.tv_chart import (
    _display_segment_price_quantum,
    xd_segment_centers_to_chart_dicts,
)
from chanlun.core.cl import CL
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
    StrictPointStatus,
)
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
from chanlun.decision_support.trading_system.provisional import (
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.runtime_config import (
    screening_cl_config,
    strict_cl_config,
)
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_FREQUENCIES,
    build_screening_evidence,
    merge_provisional_candidates,
    unfinished_segment_candidates,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FRAME_COLUMNS,
    final_confirmed_structure_events,
)
from chanlun.exchange.kline_precision import resolve_structure_price_quantum
from chanlun.exchange.price_basis import build_provider_price_basis_metadata
from chanlun.tools.cache_version import source_fingerprint


SCHEMA = "chanlun-xd-center-consistency-audit/v1"
_CACHE_NAME = re.compile(
    r"^v\d+_[0-9a-f]{8}_(a|us|hk|fx)_(.+)_"
    r"(1m|2m|3m|5m|10m|15m|30m|60m|120m|d|w|m|q|y)_"
    r"([0-9a-f]{32})\.pkl$"
)
_TIMEZONES = {
    "a": ZoneInfo("Asia/Shanghai"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
    "us": ZoneInfo("America/New_York"),
    "fx": ZoneInfo("Asia/Shanghai"),
}


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _non_negative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--cache-root",
        type=Path,
        default=Path(r"D:\chanlun_pro\chart_cache"),
    )
    result.add_argument(
        "--workers",
        type=_positive_int,
        default=max(1, min(8, (os.cpu_count() or 2) // 2)),
    )
    result.add_argument(
        "--prefix-depth",
        type=_non_negative_int,
        default=6,
        help="also audit this many trailing segment-count prefixes",
    )
    result.add_argument(
        "--bar-prefix-depth",
        type=_non_negative_int,
        default=0,
        help=(
            "rebuild this many deterministic historical bar prefixes and "
            "prove completed centers/trends/points/divergences are immutable"
        ),
    )
    result.add_argument(
        "--max-datasets",
        type=_positive_int,
        help="deterministic small-sample gate before a full run",
    )
    return result


def _restore_code(code_token: str) -> str:
    return code_token.replace("_", ".")


def discover_latest_datasets(cache_root: Path) -> list[dict[str, object]]:
    """Return the newest full snapshot per market/code/frequency."""

    selected: dict[tuple[str, str, str], dict[str, object]] = {}
    for path in cache_root.glob("*.pkl"):
        match = _CACHE_NAME.match(path.name)
        if match is None:
            continue
        market, code_token, frequency, _config_hash = match.groups()
        key = (market, code_token, frequency)
        candidate = {
            "market": market,
            "code": _restore_code(code_token),
            "frequency": frequency,
            "path": str(path.resolve()),
            "modified_ns": path.stat().st_mtime_ns,
        }
        existing = selected.get(key)
        if existing is None or int(candidate["modified_ns"]) > int(
            existing["modified_ns"]
        ):
            selected[key] = candidate
    return sorted(
        selected.values(),
        key=lambda value: (
            str(value["market"]),
            str(value["code"]),
            str(value["frequency"]),
        ),
    )


def _payload_issues(payloads: Iterable[Mapping[str, object]]) -> list[str]:
    """Validate the page payload against the original first-three contract.

    There is no synthetic external entry segment. Three consecutive same-level
    segments establish ``[ZD, ZG]`` (equality is valid); a later segment may
    leave, and only its first opposite return outside completes a third-class
    point. A forming center therefore legitimately has no leaving segment yet.
    """

    values = list(payloads)
    issues: list[str] = []
    active = [
        value
        for value in values
        if value.get("center_state", value.get("state")) in ("forming", "ongoing")
    ]
    if len(active) > 1:
        issues.append("MULTIPLE_UNRESOLVED_CENTERS")
    ids = [str(value.get("center_id") or "") for value in values]
    if "" in ids or len(ids) != len(set(ids)):
        issues.append("CENTER_ID_MISSING_OR_DUPLICATED")

    previous_start: int | None = None
    for index, value in enumerate(values):
        prefix = f"CENTER_{index}"
        direction = value.get("type")
        entry = value.get("entering_segment")
        leave = value.get("leaving_segment")
        if entry is not None:
            issues.append(f"{prefix}_SYNTHETIC_ENTRY_PRESENT")
        core_directions = value.get("core_directions")
        if (
            not isinstance(core_directions, list)
            or len(core_directions) != 3
            or any(item not in ("up", "down") for item in core_directions)
            or core_directions[0] == core_directions[1]
            or core_directions[1] == core_directions[2]
        ):
            issues.append(f"{prefix}_CORE_DIRECTION_MISMATCH")
        first_three_ids = value.get("first_three_component_ids")
        first_three = value.get("first_three_components")
        if (
            value.get("core_line_count") != 3
            or not isinstance(first_three_ids, list)
            or len(first_three_ids) != 3
            or len(set(first_three_ids)) != 3
            or not isinstance(first_three, list)
            or len(first_three) != 3
            or any(not isinstance(item, Mapping) for item in first_three)
        ):
            issues.append(f"{prefix}_FIRST_THREE_EVIDENCE_INVALID")

        points = value.get("points")
        if not isinstance(points, list) or len(points) != 2:
            issues.append(f"{prefix}_POINTS_INVALID")
            continue
        try:
            start = int(points[0]["time"])
            end = int(points[1]["time"])
            zd = float(value["zd"])
            zg = float(value["zg"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"{prefix}_GEOMETRY_INVALID")
            continue
        # ZD == ZG is a valid zero-width center in the original definition.
        if start >= end or zd > zg:
            issues.append(f"{prefix}_GEOMETRY_INVALID")
        if previous_start is not None and start < previous_start:
            issues.append(f"{prefix}_TIME_ORDER_REGRESSED")
        previous_start = start
        if isinstance(first_three, list) and first_three and isinstance(
            first_three[0], Mapping
        ):
            if int(first_three[0].get("start_time", -1)) != start:
                issues.append(f"{prefix}_FIRST_CORE_BOUNDARY_MISMATCH")
        if isinstance(leave, Mapping):
            if direction not in ("up", "down"):
                issues.append(f"{prefix}_LEAVE_DIRECTION_INVALID")
            elif leave.get("direction") != direction:
                issues.append(f"{prefix}_LEAVE_DIRECTION_MISMATCH")
            if int(leave.get("start_time", -1)) != end:
                issues.append(f"{prefix}_CORE_LEAVE_BOUNDARY_MISMATCH")
        elif leave is not None:
            issues.append(f"{prefix}_LEAVE_METADATA_INVALID")
        elif direction != "zd":
            issues.append(f"{prefix}_DIRECTION_WITHOUT_LEAVE")

        state = value.get("center_state", value.get("state"))
        done = value.get("done") is True
        provisional = value.get("provisional") is True
        point_type = value.get("completion_point_type")
        return_segment = value.get("completion_return_segment")
        if state not in ("forming", "ongoing", "completed"):
            issues.append(f"{prefix}_STATE_INVALID")
        if done and (state != "completed" or provisional):
            issues.append(f"{prefix}_DONE_STATE_CONFLICT")
        if state == "completed" and (
            point_type not in ("3buy", "3sell")
            or not isinstance(return_segment, Mapping)
            or value.get("linestyle") != "0"
        ):
            issues.append(f"{prefix}_COMPLETION_EVIDENCE_INCOMPLETE")
        if state == "completed" and isinstance(return_segment, Mapping):
            if not isinstance(leave, Mapping) or direction not in ("up", "down"):
                issues.append(f"{prefix}_COMPLETION_LEAVE_MISSING")
                continue
            expected_point = "3buy" if direction == "up" else "3sell"
            expected_return_direction = "down" if direction == "up" else "up"
            if (
                point_type != expected_point
                or value.get("expected_completion_point_type") != expected_point
                or return_segment.get("direction") != expected_return_direction
            ):
                issues.append(f"{prefix}_COMPLETION_DIRECTION_MISMATCH")
            if (
                leave.get("end_time") != return_segment.get("start_time")
                or leave.get("end_price") != return_segment.get("start_price")
            ):
                issues.append(f"{prefix}_COMPLETION_RETURN_DISCONNECTED")
            try:
                return_prices = (
                    float(return_segment["start_price"]),
                    float(return_segment["end_price"]),
                )
            except (KeyError, TypeError, ValueError):
                issues.append(f"{prefix}_COMPLETION_RETURN_PRICE_INVALID")
            else:
                return_stays_outside = (
                    min(return_prices) >= zg
                    if direction == "up"
                    else max(return_prices) <= zd
                )
                if not return_stays_outside:
                    issues.append(f"{prefix}_COMPLETION_RETURN_CROSSES_CORE")
            expected_status = "confirmed" if done else "provisional"
            if value.get("completion_point_status") != expected_status:
                issues.append(f"{prefix}_COMPLETION_STATUS_MISMATCH")
        if state in ("forming", "ongoing") and point_type is not None:
            issues.append(f"{prefix}_UNRESOLVED_WITH_COMPLETION_POINT")
        if state in ("forming", "ongoing") and return_segment is not None:
            issues.append(f"{prefix}_UNRESOLVED_WITH_COMPLETION_RETURN")
        if state in ("forming", "ongoing") and value.get("linestyle") != "1":
            issues.append(f"{prefix}_UNRESOLVED_NOT_DASHED")
        if state in ("forming", "ongoing"):
            expected_phase = (
                "AWAITING_SAME_LEVEL_RETURN"
                if isinstance(leave, Mapping)
                else "AWAITING_SAME_LEVEL_DEPARTURE"
            )
            if value.get("completion_phase") != expected_phase:
                issues.append(f"{prefix}_UNRESOLVED_PHASE_MISMATCH")
            expected_point = (
                None
                if not isinstance(leave, Mapping)
                else "3buy" if direction == "up" else "3sell"
            )
            if value.get("expected_completion_point_type") != expected_point:
                issues.append(f"{prefix}_EXPECTED_POINT_MISMATCH")
        if value.get("tradable") is True and (provisional or not done):
            issues.append(f"{prefix}_TRADABLE_PROVISIONAL_CONFLICT")
    return sorted(set(issues))


def _preview_ownership_issues(result) -> list[str]:
    issues: list[str] = []
    forming = [
        value
        for value in result.previews
        if value.state is CenterPreviewState.FORMING
    ]
    if len(forming) > 1:
        issues.append("MULTIPLE_FORMING_PREVIEWS")
    active = (
        result.centers[-1]
        if result.centers
        and result.centers[-1].state is CenterState.ONGOING
        else None
    )
    if active is None or not forming:
        return issues

    width = len(active.initial_units)
    active_seed = tuple(item.unit_id for item in active.initial_units)
    forming_seed = tuple(forming[0].unit_ids[:width])
    if forming_seed == active_seed:
        return issues
    completed_active_projection = any(
        value.state is CenterPreviewState.COMPLETED
        and tuple(value.unit_ids[:width]) == active_seed
        for value in result.previews
    )
    if not completed_active_projection:
        issues.append("SHIFTED_FORMING_PREVIEW_DISPLACED_ACTIVE_EXTENSION")
    return issues


def _strict_evidence_issues(evidence) -> list[str]:
    """Cross-check point/divergence references against their formal structure."""

    issues: list[str] = []
    levels = {
        level.structural_level: level for level in evidence.structure.levels
    }
    confirmed = tuple(evidence.confirmed_points)
    approaching = tuple(evidence.approaching_points)
    confirmed_by_id = {point.point_id: point for point in confirmed}

    confirmed_semantics = {
        (
            point.point_type,
            point.structural_level,
            point.anchor_unit_id,
            point.center_id,
            point.parent_point_id,
        )
        for point in confirmed
    }
    approaching_semantics = {
        (
            point.point_type,
            point.structural_level,
            point.anchor_unit_id,
            point.center_id,
            point.parent_point_id,
        )
        for point in approaching
    }
    if confirmed_semantics & approaching_semantics:
        issues.append("CONFIRMED_AND_APPROACHING_POINT_COLLISION")

    for point in confirmed + approaching:
        prefix = f"POINT_{point.point_id}"
        level = levels.get(point.structural_level)
        if level is None:
            issues.append(f"{prefix}_LEVEL_MISSING")
            continue
        anchors = [
            unit for unit in level.units if unit.unit_id == point.anchor_unit_id
        ]
        if len(anchors) != 1:
            issues.append(f"{prefix}_ANCHOR_NOT_UNIQUE")
            continue
        anchor = anchors[0]
        if (
            anchor.source_kind is not point.source_kind
            or anchor.price_basis_revision != point.price_basis_revision
        ):
            issues.append(f"{prefix}_ANCHOR_CONTEXT_MISMATCH")
        if point.anchor_at != anchor.market_end:
            issues.append(f"{prefix}_ANCHOR_TIME_MISMATCH")
        expected_tick = anchor.low_tick if point.side == "buy" else anchor.high_tick
        if point.anchor_tick != expected_tick:
            issues.append(f"{prefix}_ANCHOR_PRICE_MISMATCH")
        if point.status is StrictPointStatus.CONFIRMED:
            if (
                not anchor.locked
                or point.confirmed_at is None
                or anchor.confirmed_at is None
                or point.confirmed_at < anchor.confirmed_at
            ):
                issues.append(f"{prefix}_CONFIRMATION_MISMATCH")
            elif point.point_type in ("1buy", "1sell"):
                matching_trends = [
                    trend
                    for trend in level.completed_trends
                    if trend.terminal_unit.unit_id == point.anchor_unit_id
                    and trend.confirmed_at == point.confirmed_at
                ]
                if len(matching_trends) != 1:
                    issues.append(f"{prefix}_TREND_CONFIRMATION_MISMATCH")
            elif point.confirmed_at != anchor.confirmed_at:
                issues.append(f"{prefix}_CONFIRMATION_MISMATCH")
        elif anchor.locked:
            issues.append(f"{prefix}_APPROACHING_ANCHOR_ALREADY_LOCKED")
        if not point.evidence_codes:
            issues.append(f"{prefix}_EVIDENCE_CODES_EMPTY")

        centers = {
            center.center_id: center for center in level.center_result.centers
        }
        if point.center_id is not None:
            center = centers.get(point.center_id)
            if center is None:
                issues.append(f"{prefix}_CENTER_MISSING")
            elif (
                point.center_zd_tick != center.zd_tick
                or point.center_zg_tick != center.zg_tick
                or point.available_at < center.available_at
            ):
                issues.append(f"{prefix}_CENTER_EVIDENCE_MISMATCH")
        if point.point_type in ("2buy", "2sell"):
            parent = confirmed_by_id.get(point.parent_point_id or "")
            if (
                parent is None
                or parent.point_type
                != ("1buy" if point.side == "buy" else "1sell")
                or parent.structural_level != point.structural_level
                or parent.available_at > point.available_at
            ):
                issues.append(f"{prefix}_PARENT_EVIDENCE_MISMATCH")
        if point.divergence is not None and (
            point.divergence.signal_unit_id != point.anchor_unit_id
            or point.divergence.available_at > point.available_at
        ):
            issues.append(f"{prefix}_DIVERGENCE_EVIDENCE_MISMATCH")

    for divergence in evidence.divergences:
        prefix = f"DIVERGENCE_{divergence.divergence_id}"
        level = levels.get(divergence.structural_level)
        if level is None:
            issues.append(f"{prefix}_LEVEL_MISSING")
            continue
        units = {unit.unit_id: unit for unit in level.units}
        compare = units.get(divergence.compare_unit_id)
        signal = units.get(divergence.signal_unit_id)
        if compare is None or signal is None:
            issues.append(f"{prefix}_UNIT_MISSING")
            continue
        if (
            compare.direction != divergence.direction
            or signal.direction != divergence.direction
            or compare.market_end > signal.market_start
            or divergence.anchor_at != signal.market_end
            or divergence.anchor_tick
            != (signal.low_tick if signal.direction == "down" else signal.high_tick)
        ):
            issues.append(f"{prefix}_GEOMETRY_MISMATCH")
        if not signal.locked or divergence.confirmed_at != signal.confirmed_at:
            issues.append(f"{prefix}_CONFIRMATION_MISMATCH")
        if not divergence.is_divergent:
            issues.append(f"{prefix}_NOT_FULLY_CONFIRMED")
    return issues


def _upgrade_evidence_issues(evidence, upgrades) -> list[str]:
    """Validate nine-segment and expansion context against source structure."""

    issues: list[str] = []
    levels = {
        level.structural_level: level for level in evidence.structure.levels
    }
    if len({item.evidence_id for item in upgrades}) != len(upgrades):
        issues.append("UPGRADE_EVIDENCE_ID_DUPLICATED")
    for item in upgrades:
        prefix = f"UPGRADE_{item.evidence_id}"
        source = levels.get(item.source_level)
        if source is None:
            issues.append(f"{prefix}_SOURCE_LEVEL_MISSING")
            continue
        if (
            item.target_level != item.source_level + 1
            or item.price_basis_revision != evidence.price_basis_revision
            or item.available_at > evidence.source_closed_at
        ):
            issues.append(f"{prefix}_CONTEXT_MISMATCH")
        source_centers = {
            center.center_id: center for center in source.center_result.centers
        }
        if any(
            center_id not in source_centers
            or source_centers[center_id].state is not CenterState.COMPLETED
            for center_id in item.source_center_ids
        ):
            issues.append(f"{prefix}_SOURCE_CENTER_MISSING_OR_UNRESOLVED")
        source_units = {unit.unit_id: unit for unit in source.units}
        evidence_unit_ids = item.source_unit_ids + item.extension_unit_ids
        if any(unit_id not in source_units for unit_id in evidence_unit_ids):
            issues.append(f"{prefix}_SOURCE_UNIT_MISSING")
        elif item.source_unit_ids:
            selected = tuple(
                source_units[unit_id] for unit_id in item.source_unit_ids
            )
            if (
                min(unit.market_start for unit in selected) < item.market_start
                or max(unit.market_end for unit in selected) > item.market_end
                or any(unit.available_at > item.available_at for unit in selected)
            ):
                issues.append(f"{prefix}_SOURCE_UNIT_TIME_MISMATCH")
        if item.resolved_by_standard_center_id is not None:
            target = levels.get(item.target_level)
            target_ids = (
                {
                    center.center_id
                    for center in target.center_result.centers
                    if center.state is CenterState.COMPLETED
                }
                if target is not None
                else set()
            )
            if item.resolved_by_standard_center_id not in target_ids:
                issues.append(f"{prefix}_STANDARD_CENTER_RESOLUTION_MISSING")
        if item.signal_eligible:
            issues.append(f"{prefix}_IMPROPERLY_SIGNAL_ELIGIBLE")
    return issues


def _snapshot_issues(evidence, snapshot) -> list[str]:
    """Exercise the production serializer and audit its cardinality contract."""

    issues: list[str] = []
    if snapshot.get("schema") != "chanlun-chart-structure/v5":
        issues.append("STRICT_SNAPSHOT_SCHEMA_MISMATCH")
    levels = snapshot.get("levels")
    if not isinstance(levels, list) or len(levels) != len(
        evidence.structure.levels
    ):
        issues.append("STRICT_SNAPSHOT_LEVEL_COUNT_MISMATCH")
        return issues
    serialized_confirmed = 0
    serialized_approaching = 0
    serialized_divergences = 0
    for index, level in enumerate(levels):
        if level.get("structural_level") != index:
            issues.append(f"STRICT_SNAPSHOT_LEVEL_{index}_ORDER_MISMATCH")
        previews = level.get("center_previews")
        if not isinstance(previews, list):
            issues.append(f"STRICT_SNAPSHOT_LEVEL_{index}_PREVIEWS_INVALID")
        elif sum(item.get("state") == "forming" for item in previews) > 1:
            issues.append(
                f"STRICT_SNAPSHOT_LEVEL_{index}_MULTIPLE_FORMING_PREVIEWS"
            )
        serialized_confirmed += len(level.get("confirmed_points") or ())
        serialized_approaching += len(level.get("approaching_points") or ())
        serialized_divergences += len(level.get("divergences") or ())
    if serialized_confirmed != len(evidence.confirmed_points):
        issues.append("STRICT_SNAPSHOT_CONFIRMED_POINT_COUNT_MISMATCH")
    if serialized_approaching != len(evidence.approaching_points):
        issues.append("STRICT_SNAPSHOT_APPROACHING_POINT_COUNT_MISMATCH")
    if serialized_divergences != len(evidence.divergences):
        issues.append("STRICT_SNAPSHOT_DIVERGENCE_COUNT_MISMATCH")
    return issues


def _bar_prefix_sizes(bar_count: int, depth: int) -> tuple[int, ...]:
    """Spread prefix checks across the latter half of the available history."""

    if depth <= 0 or bar_count < 20:
        return ()
    start = max(10, bar_count // 2)
    span = bar_count - start
    return tuple(
        sorted(
            {
                start + max(1, round(span * step / (depth + 1)))
                for step in range(1, depth + 1)
            }
        )
    )


def _immutable_prefix_issues(prefix_evidence, final_evidence, size: int) -> list[str]:
    """Describe terminal-projection rewrites without treating them as trades.

    The terminal XD/recursive snapshot is explicitly a current-state
    projection.  It may reclassify its live tail when later bars arrive.  The
    append-only trading contract is audited separately through
    ``final_confirmed_structure_events``; these diagnostics remain visible so
    a projection can never be mistaken for that causal ledger.
    """

    issues: list[str] = []
    final_levels = {
        level.structural_level: level
        for level in final_evidence.structure.levels
    }
    for level in prefix_evidence.structure.levels:
        final_level = final_levels.get(level.structural_level)
        if final_level is None:
            issues.append(f"BAR_PREFIX_{size}_LEVEL_{level.structural_level}_MISSING")
            continue
        final_centers = {
            center.center_id: center for center in final_level.center_result.centers
        }
        for center in level.center_result.centers:
            if center.state is not CenterState.COMPLETED:
                continue
            if final_centers.get(center.center_id) != center:
                issues.append(
                    f"BAR_PREFIX_{size}_COMPLETED_CENTER_REWRITTEN_"
                    f"L{level.structural_level}_{center.center_id}"
                )
        final_trends = {
            trend.trend_id: trend for trend in final_level.completed_trends
        }
        for trend in level.completed_trends:
            if final_trends.get(trend.trend_id) != trend:
                issues.append(
                    f"BAR_PREFIX_{size}_COMPLETED_TREND_REWRITTEN_"
                    f"L{level.structural_level}_{trend.trend_id}"
                )

    final_points = {
        point.point_id: point for point in final_evidence.confirmed_points
    }
    for point in prefix_evidence.confirmed_points:
        if final_points.get(point.point_id) != point:
            issues.append(
                f"BAR_PREFIX_{size}_CONFIRMED_POINT_REWRITTEN_{point.point_id}"
            )
    final_divergences = {
        item.divergence_id: item for item in final_evidence.divergences
    }
    for item in prefix_evidence.divergences:
        if final_divergences.get(item.divergence_id) != item:
            issues.append(
                f"BAR_PREFIX_{size}_DIVERGENCE_REWRITTEN_{item.divergence_id}"
            )

    prefix_upgrades = collect_recursive_upgrade_evidence(
        prefix_evidence.structure,
        as_of=prefix_evidence.source_closed_at,
    )
    final_upgrades = {
        item.evidence_id: item
        for item in collect_recursive_upgrade_evidence(
            final_evidence.structure,
            as_of=final_evidence.source_closed_at,
        )
        if item.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
    }
    for item in prefix_upgrades:
        # Expansion is deliberately a live reclassification state of the
        # terminal completed pair.  It may disappear as history advances;
        # confirmed nine-segment derivations are immutable.
        if item.kind is not UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION:
            continue
        if final_upgrades.get(item.evidence_id) != item:
            issues.append(
                f"BAR_PREFIX_{size}_NINE_SEGMENT_UPGRADE_REWRITTEN_"
                f"{item.evidence_id}"
            )
    return issues


def _causal_frame(
    frame: pd.DataFrame,
    *,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
) -> pd.DataFrame:
    """Bind cached OHLCV to the causal replay frame contract."""

    value = frame.copy()
    for field in ("open", "high", "low", "close"):
        value[f"raw_{field}"] = value[field]
    value = value.loc[:, list(FRAME_COLUMNS)]
    value.attrs.update(
        structure_price_quantum=str(structure_price_quantum),
        price_basis_revision=price_basis_revision,
    )
    return value


def _causal_ledger_prefix_issues(
    prefix,
    final,
    *,
    cutoff: datetime,
    size: int,
) -> list[str]:
    """Every first-seen trading fact must remain byte-for-byte append-only."""

    issues: list[str] = []
    comparisons = (
        (
            "POINTS",
            prefix.points,
            tuple(item for item in final.points if item.available_at <= cutoff),
        ),
        (
            "TRENDS",
            prefix.completed_trends,
            tuple(
                item
                for item in final.completed_trends
                if item.available_at <= cutoff
            ),
        ),
        (
            "UNITS",
            prefix.completed_units,
            tuple(
                item for item in final.completed_units if item.available_at <= cutoff
            ),
        ),
        (
            "CENTER_COMPLETIONS",
            prefix.center_completions,
            tuple(
                item
                for item in final.center_completions
                if item.available_at <= cutoff
            ),
        ),
        (
            "DIRECT_DECISIONS",
            prefix.direct_recursive_decisions,
            tuple(
                item
                for item in final.direct_recursive_decisions
                if item.first_seen_at <= cutoff
            ),
        ),
    )
    for label, observed, expected in comparisons:
        if observed != expected:
            issues.append(f"BAR_PREFIX_{size}_CAUSAL_{label}_REWRITTEN")
    expected_point_ids = {item.point_id for item in prefix.points}
    expected_anchors = tuple(
        item
        for item in final.point_anchor_unit_ids
        if item[0] in expected_point_ids
    )
    if prefix.point_anchor_unit_ids != expected_anchors:
        issues.append(f"BAR_PREFIX_{size}_CAUSAL_POINT_ANCHORS_REWRITTEN")
    return issues


def _raw_result(lines, *, price_basis: str):
    evidence_times = [
        value
        for line in lines
        for value in (
            line.start.k.date,
            line.end.k.date,
            getattr(line, "locked_at", None),
        )
        if value is not None
    ]
    if not evidence_times:
        return None, ()
    units = adapt_lines(
        lines,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_quantum=_display_segment_price_quantum(lines),
        as_of=max(evidence_times),
        registry=UnitLockRegistry(price_basis),
    )
    return calculate_centers(units, 0, SourceKind.SEGMENT), units


def _audit_dataset(
    spec: Mapping[str, object],
    prefix_depth: int,
    bar_prefix_depth: int,
) -> dict[str, object]:
    started = time.perf_counter()
    path = Path(str(spec["path"]))
    market = str(spec["market"])
    code = str(spec["code"])
    frequency = str(spec["frequency"])
    identity = f"{market}:{code}:{frequency}"
    try:
        with path.open("rb") as handle:
            cached = pickle.load(handle)
        if cached.get("is_full_snapshot") is not True:
            raise ValueError("cache entry is not a full snapshot")
        data = cached["data"]
        lengths = {
            len(data[key]) for key in ("t", "o", "h", "l", "c", "v")
        }
        if len(lengths) != 1 or not lengths or next(iter(lengths)) <= 0:
            raise ValueError("OHLCV arrays must have one positive length")
        bar_count = next(iter(lengths))
        timezone = _TIMEZONES[market]
        frame = pd.DataFrame(
            {
                "code": code,
                "date": pd.to_datetime(data["t"], unit="s", utc=True).tz_convert(
                    timezone
                ),
                "open": data["o"],
                "high": data["h"],
                "low": data["l"],
                "close": data["c"],
                "volume": data["v"],
            }
        )
        strict_snapshot = data.get("strict_structure")
        if isinstance(strict_snapshot, Mapping):
            raw_basis = strict_snapshot.get("price_basis_revision")
            if not isinstance(raw_basis, str) or not raw_basis.strip():
                raise ValueError("cached strict price basis is missing")
            try:
                strict_quantum = Decimal(
                    str(strict_snapshot["structure_price_quantum"])
                )
            except (KeyError, DecimalException, ValueError) as exc:
                raise ValueError("cached strict price quantum is invalid") from exc
            if not strict_quantum.is_finite() or strict_quantum <= 0:
                raise ValueError("cached strict price quantum is invalid")
            price_basis_source = "cached_production"
        else:
            # Pre-strict chart caches still contain the exact OHLCV basis that
            # was rendered, but predate formal metadata.  They remain valuable
            # regression inputs.  Give the immutable cached values an explicit
            # audit-only identity; this validates structure invariants without
            # claiming provider/adjustment provenance that the old cache did
            # not preserve.
            strict_quantum = resolve_structure_price_quantum(market, code)
            if strict_quantum is None:
                raise ValueError("cannot resolve audit price quantum")
            metadata = build_provider_price_basis_metadata(
                provider="chart-cache-audit",
                market=market,
                code=code,
                adjustment="as-cached-unknown",
                structure_price_quantum=strict_quantum,
            )
            raw_basis = metadata.price_basis_revision
            price_basis_source = "audit_as_cached"

        # The visible XD-center layer deliberately uses the user's legacy chart
        # profile.  Formal recursive centers and trading points use a separate,
        # immutable strict profile in production.  Rebuild both; calling strict
        # APIs on the legacy object would silently audit a configuration that no
        # production signal path consumes.
        config = query_cl_chart_config(market, code)
        calculation = CL(code, frequency, config, market=market).process_klines(frame)
        lines = tuple(calculation.get_xds())
        payloads = xd_segment_centers_to_chart_dicts(lines)
        issues = _payload_issues(payloads)
        result, units = _raw_result(lines, price_basis=raw_basis)
        if result is not None:
            issues.extend(_preview_ownership_issues(result))
        strict_config = strict_cl_config(
            structure_price_quantum=strict_quantum,
            price_basis_revision=raw_basis,
        )
        strict_calculation = CL(
            code,
            frequency,
            strict_config,
            market=market,
        ).process_klines(frame)
        strict_structure = strict_calculation.get_strict_structure_levels()
        for level in strict_structure.levels:
            issues.extend(
                f"STRICT_LEVEL_{level.structural_level}_{issue}"
                for issue in _preview_ownership_issues(level.center_result)
            )
        stroke_observations = strict_calculation.get_stroke_observation_centers()
        issues.extend(
            f"STROKE_OBSERVATION_{issue}"
            for issue in _preview_ownership_issues(stroke_observations)
        )
        evidence = strict_calculation.get_strict_evidence()
        issues.extend(_strict_evidence_issues(evidence))
        upgrades = collect_recursive_upgrade_evidence(
            evidence.structure,
            as_of=evidence.source_closed_at,
        )
        issues.extend(_upgrade_evidence_issues(evidence, upgrades))
        serialized = build_strict_structure_snapshot(
            evidence,
            interval=frequency,
        )
        issues.extend(_snapshot_issues(evidence, serialized))

        # Live screening intentionally uses the specification's original old
        # stroke and one physical level zero, not the recursive research
        # profile above.  Audit the actual production path independently so a
        # clean recursive snapshot cannot mask a page/notification defect.
        screening_evidence = None
        screening_provisional = ()
        if frequency in SCREENING_STRUCTURE_FREQUENCIES:
            screening_config = screening_cl_config(
                structure_price_quantum=strict_quantum,
                price_basis_revision=raw_basis,
            )
            screening_calculation = CL(
                code,
                frequency,
                screening_config,
                market=market,
            ).process_klines(frame)
            screening_evidence = build_screening_evidence(
                screening_calculation,
                source_closed_at=frame.iloc[-1]["date"].to_pydatetime(),
                structure_price_quantum=strict_quantum,
                price_basis_revision=raw_basis,
                strict_config_revision=str(
                    screening_config["strict_config_revision"]
                ),
            )
            issues.extend(
                f"SCREENING_{issue}"
                for issue in _strict_evidence_issues(screening_evidence)
            )
            screening_snapshot = build_strict_structure_snapshot(
                screening_evidence,
                interval=frequency,
            )
            issues.extend(
                f"SCREENING_{issue}"
                for issue in _snapshot_issues(
                    screening_evidence,
                    screening_snapshot,
                )
            )
            screening_provisional = merge_provisional_candidates(
                extract_provisional_candidates(
                    screening_evidence,
                    code=code,
                    source_frequency=frequency,
                    as_of=screening_evidence.source_closed_at,
                ),
                unfinished_segment_candidates(
                    screening_evidence,
                    code=code,
                    source_frequency=frequency,
                ),
            )
        if prefix_depth and units:
            first = max(5, len(units) - prefix_depth)
            for size in range(first, len(units) + 1):
                prefix = calculate_centers(units[:size], 0, SourceKind.SEGMENT)
                issues.extend(
                    f"PREFIX_{size}_{issue}"
                    for issue in _preview_ownership_issues(prefix)
                )
        bar_prefix_sizes = _bar_prefix_sizes(bar_count, bar_prefix_depth)
        terminal_projection_rewrites: list[str] = []
        causal_frame = None
        causal_final = None
        if bar_prefix_sizes:
            causal_frame = _causal_frame(
                frame,
                structure_price_quantum=strict_quantum,
                price_basis_revision=raw_basis,
            )
            causal_final = final_confirmed_structure_events(
                code,
                frequency,
                causal_frame,
            )
        for size in bar_prefix_sizes:
            prefix_calculation = CL(
                code,
                frequency,
                strict_config,
                market=market,
            ).process_klines(frame.iloc[:size])
            prefix_evidence = prefix_calculation.get_strict_evidence()
            issues.extend(_strict_evidence_issues(prefix_evidence))
            prefix_upgrades = collect_recursive_upgrade_evidence(
                prefix_evidence.structure,
                as_of=prefix_evidence.source_closed_at,
            )
            issues.extend(
                f"BAR_PREFIX_{size}_{issue}"
                for issue in _upgrade_evidence_issues(
                    prefix_evidence,
                    prefix_upgrades,
                )
            )
            prefix_snapshot = build_strict_structure_snapshot(
                prefix_evidence,
                interval=frequency,
            )
            issues.extend(
                f"BAR_PREFIX_{size}_{issue}"
                for issue in _snapshot_issues(prefix_evidence, prefix_snapshot)
            )
            terminal_projection_rewrites.extend(
                _immutable_prefix_issues(prefix_evidence, evidence, size)
            )
            if causal_frame is None or causal_final is None:
                raise RuntimeError("causal prefix audit was not initialized")
            prefix_causal_frame = causal_frame.iloc[:size].copy()
            prefix_causal_frame.attrs = dict(causal_frame.attrs)
            causal_prefix = final_confirmed_structure_events(
                code,
                frequency,
                prefix_causal_frame,
            )
            cutoff = frame.iloc[size - 1]["date"].to_pydatetime()
            issues.extend(
                _causal_ledger_prefix_issues(
                    causal_prefix,
                    causal_final,
                    cutoff=cutoff,
                    size=size,
                )
            )
        issues = sorted(set(issues))
        terminal_projection_rewrites = sorted(
            set(terminal_projection_rewrites)
        )
        return {
            "identity": identity,
            "path": str(path),
            "status": "pass" if not issues else "fail",
            "issues": issues,
            "bar_count": bar_count,
            "segment_count": len(lines),
            "center_count": len(payloads),
            "recursive_level_count": len(strict_structure.levels),
            "stroke_observation_center_count": len(
                stroke_observations.centers
            ),
            "confirmed_point_count": len(evidence.confirmed_points),
            "approaching_point_count": len(evidence.approaching_points),
            "divergence_count": len(evidence.divergences),
            "screening_path_audited": screening_evidence is not None,
            "screening_center_count": (
                0
                if screening_evidence is None
                else len(
                    screening_evidence.structure.levels[0].center_result.centers
                )
            ),
            "screening_confirmed_point_count": (
                0
                if screening_evidence is None
                else len(screening_evidence.confirmed_points)
            ),
            "screening_provisional_candidate_count": len(
                screening_provisional
            ),
            "nine_segment_upgrade_count": sum(
                item.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
                for item in upgrades
            ),
            "expansion_upgrade_count": sum(
                item.kind is UpgradeEvidenceKind.CENTER_EXPANSION
                for item in upgrades
            ),
            "bar_prefix_count": len(bar_prefix_sizes),
            "terminal_projection_rewrite_count": len(
                terminal_projection_rewrites
            ),
            "terminal_projection_rewrites": terminal_projection_rewrites,
            "price_basis_source": price_basis_source,
            "active_center_count": sum(
                value.get("center_state", value.get("state"))
                in ("forming", "ongoing")
                for value in payloads
            ),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:
        return {
            "identity": identity,
            "path": str(path),
            "status": "error",
            "issues": [f"{type(exc).__name__}: {exc}"],
            "bar_count": 0,
            "segment_count": 0,
            "center_count": 0,
            "recursive_level_count": 0,
            "stroke_observation_center_count": 0,
            "confirmed_point_count": 0,
            "approaching_point_count": 0,
            "divergence_count": 0,
            "screening_path_audited": False,
            "screening_center_count": 0,
            "screening_confirmed_point_count": 0,
            "screening_provisional_candidate_count": 0,
            "nine_segment_upgrade_count": 0,
            "expansion_upgrade_count": 0,
            "bar_prefix_count": 0,
            "terminal_projection_rewrite_count": 0,
            "terminal_projection_rewrites": [],
            "price_basis_source": "unavailable",
            "active_center_count": 0,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def main() -> int:
    args = parser().parse_args()
    started = time.perf_counter()
    datasets = discover_latest_datasets(args.cache_root)
    if args.max_datasets is not None:
        # A deterministic stride samples the whole catalog instead of only A-share
        # alphabetic prefixes.
        if len(datasets) > args.max_datasets:
            step = len(datasets) / args.max_datasets
            datasets = [datasets[int(index * step)] for index in range(args.max_datasets)]

    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _audit_dataset,
                spec,
                args.prefix_depth,
                args.bar_prefix_depth,
            ): spec
            for spec in datasets
        }
        completed = 0
        for future in as_completed(futures):
            results.append(future.result())
            completed += 1
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"audited {completed}/{len(futures)} datasets",
                    file=sys.stderr,
                    flush=True,
                )

    results.sort(key=lambda value: str(value["identity"]))
    failed = [value for value in results if value["status"] == "fail"]
    errors = [value for value in results if value["status"] == "error"]
    payload = {
        "schema": SCHEMA,
        "observed_at": datetime.now().astimezone().isoformat(),
        "source_fingerprint": source_fingerprint(),
        "cache_root": str(args.cache_root.resolve()),
        "workers": args.workers,
        "prefix_depth": args.prefix_depth,
        "bar_prefix_depth": args.bar_prefix_depth,
        "dataset_count": len(results),
        "pass_count": sum(value["status"] == "pass" for value in results),
        "fail_count": len(failed),
        "error_count": len(errors),
        "bar_count": sum(int(value["bar_count"]) for value in results),
        "segment_count": sum(int(value["segment_count"]) for value in results),
        "center_count": sum(int(value["center_count"]) for value in results),
        "recursive_level_count": sum(
            int(value["recursive_level_count"]) for value in results
        ),
        "stroke_observation_center_count": sum(
            int(value["stroke_observation_center_count"])
            for value in results
        ),
        "confirmed_point_count": sum(
            int(value["confirmed_point_count"]) for value in results
        ),
        "approaching_point_count": sum(
            int(value["approaching_point_count"]) for value in results
        ),
        "divergence_count": sum(
            int(value["divergence_count"]) for value in results
        ),
        "screening_dataset_count": sum(
            bool(value["screening_path_audited"]) for value in results
        ),
        "screening_center_count": sum(
            int(value["screening_center_count"]) for value in results
        ),
        "screening_confirmed_point_count": sum(
            int(value["screening_confirmed_point_count"])
            for value in results
        ),
        "screening_provisional_candidate_count": sum(
            int(value["screening_provisional_candidate_count"])
            for value in results
        ),
        "nine_segment_upgrade_count": sum(
            int(value["nine_segment_upgrade_count"]) for value in results
        ),
        "expansion_upgrade_count": sum(
            int(value["expansion_upgrade_count"]) for value in results
        ),
        "bar_prefix_count": sum(
            int(value["bar_prefix_count"]) for value in results
        ),
        "terminal_projection_rewrite_count": sum(
            int(value["terminal_projection_rewrite_count"])
            for value in results
        ),
        "datasets_with_terminal_projection_rewrites": sum(
            int(value["terminal_projection_rewrite_count"]) > 0
            for value in results
        ),
        "production_basis_count": sum(
            value["price_basis_source"] == "cached_production"
            for value in results
        ),
        "audit_as_cached_basis_count": sum(
            value["price_basis_source"] == "audit_as_cached"
            for value in results
        ),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "ready": not failed and not errors,
        "failed": failed,
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
