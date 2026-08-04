#!/usr/bin/env python3
"""Rebuild cached charts and audit same-level XD-center consistency.

The chart cache stores raw OHLCV alongside rendered structures.  This tool
deliberately ignores the cached center payload, rebuilds every selected series
with the current implementation, and fails closed on duplicated live centers,
incoherent five-role metadata, or an unresolved active center being displaced
by a shifted live-edge seed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
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
)
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
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
        if not isinstance(entry, Mapping) or not isinstance(leave, Mapping):
            issues.append(f"{prefix}_ROLE_METADATA_MISSING")
            continue
        if direction not in ("up", "down"):
            issues.append(f"{prefix}_DIRECTION_INVALID")
            continue
        if entry.get("direction") != direction:
            issues.append(f"{prefix}_ENTRY_DIRECTION_MISMATCH")
        if leave.get("direction") != direction:
            issues.append(f"{prefix}_LEAVE_DIRECTION_MISMATCH")
        expected_core = (
            ["down", "up", "down"]
            if direction == "up"
            else ["up", "down", "up"]
        )
        if value.get("core_directions") != expected_core:
            issues.append(f"{prefix}_CORE_DIRECTION_MISMATCH")

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
        if start >= end or zd >= zg:
            issues.append(f"{prefix}_GEOMETRY_INVALID")
        if previous_start is not None and start < previous_start:
            issues.append(f"{prefix}_TIME_ORDER_REGRESSED")
        previous_start = start
        if int(entry.get("end_time", -1)) != start:
            issues.append(f"{prefix}_ENTRY_CORE_BOUNDARY_MISMATCH")
        if int(leave.get("start_time", -1)) != end:
            issues.append(f"{prefix}_CORE_LEAVE_BOUNDARY_MISMATCH")

        state = value.get("center_state", value.get("state"))
        done = value.get("done") is True
        provisional = value.get("provisional") is True
        point_type = value.get("completion_point_type")
        return_segment = value.get("completion_return_segment")
        if done and (state != "completed" or provisional):
            issues.append(f"{prefix}_DONE_STATE_CONFLICT")
        if state == "completed" and (
            point_type not in ("3buy", "3sell")
            or not isinstance(return_segment, Mapping)
            or value.get("linestyle") != "0"
        ):
            issues.append(f"{prefix}_COMPLETION_EVIDENCE_INCOMPLETE")
        if state in ("forming", "ongoing") and point_type is not None:
            issues.append(f"{prefix}_UNRESOLVED_WITH_COMPLETION_POINT")
        if state in ("forming", "ongoing") and value.get("linestyle") != "1":
            issues.append(f"{prefix}_UNRESOLVED_NOT_DASHED")
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


def _audit_dataset(spec: Mapping[str, object], prefix_depth: int) -> dict[str, object]:
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
        config = query_cl_chart_config(market, code)
        calculation = CL(code, frequency, config, market=market).process_klines(frame)
        lines = tuple(calculation.get_xds())
        payloads = xd_segment_centers_to_chart_dicts(lines)
        issues = _payload_issues(payloads)
        result, units = _raw_result(lines, price_basis=f"audit:{identity}")
        if result is not None:
            issues.extend(_preview_ownership_issues(result))
        strict_structure = calculation.get_strict_structure_levels()
        for level in strict_structure.levels:
            issues.extend(
                f"STRICT_LEVEL_{level.structural_level}_{issue}"
                for issue in _preview_ownership_issues(level.center_result)
            )
        stroke_observations = calculation.get_stroke_observation_centers()
        issues.extend(
            f"STROKE_OBSERVATION_{issue}"
            for issue in _preview_ownership_issues(stroke_observations)
        )
        if prefix_depth and units:
            first = max(5, len(units) - prefix_depth)
            for size in range(first, len(units) + 1):
                prefix = calculate_centers(units[:size], 0, SourceKind.SEGMENT)
                issues.extend(
                    f"PREFIX_{size}_{issue}"
                    for issue in _preview_ownership_issues(prefix)
                )
        issues = sorted(set(issues))
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
            executor.submit(_audit_dataset, spec, args.prefix_depth): spec
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
        "duration_seconds": round(time.perf_counter() - started, 3),
        "ready": not failed and not errors,
        "failed": failed,
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
