#!/usr/bin/env python3
"""Offline, fail-closed validation for Chanlun decision-support evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


FINGERPRINT_NAMES = ("data", "algorithm", "rule_set", "corpus", "model", "prompt")
TRACK_NAMES = ("trend_continuation", "bottom_reversal")
SHA256_LENGTH = 64


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remaining_count(value: Any, threshold: int) -> int | None:
    if type(value) is not int:
        return None
    return max(threshold - value, 0)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "non_finite"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _unknown_fields(fixture: dict[str, Any]) -> list[str]:
    unknown: list[str] = []

    def check(value: Any, allowed: set[str], prefix: str = "") -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        for key in value:
            path = f"{prefix}.{key}" if prefix else str(key)
            if not isinstance(key, str) or key not in allowed:
                unknown.append(path)
        return value

    check(
        fixture,
        {"schema_version", "fingerprints", "replay", "tracks", "safety", "compliance"},
    )
    check(fixture.get("fingerprints"), set(FINGERPRINT_NAMES), "fingerprints")
    check(
        fixture.get("replay"),
        {
            "prefix_invariance",
            "prefix_cases",
            "no_future_data",
            "future_perturbation_cases",
            "incomplete_bar_rejected",
            "incomplete_bar_cases",
            "event_parity",
        },
        "replay",
    )
    tracks = check(fixture.get("tracks"), set(TRACK_NAMES), "tracks")
    for track_name in TRACK_NAMES:
        track = check(
            tracks.get(track_name), {"event_ids", "oos", "paper"}, f"tracks.{track_name}"
        )
        check(
            track.get("oos"),
            {
                "split",
                "parameter_tuned_on_oos",
                "completed_trades",
                "net_expectancy",
                "profit_factor",
                "max_drawdown",
                "event_parity",
            },
            f"tracks.{track_name}.oos",
        )
        check(
            track.get("paper"),
            {"trading_days", "executable_events"},
            f"tracks.{track_name}.paper",
        )
    check(
        fixture.get("safety"),
        {
            "risk_violations",
            "lookahead_events",
            "zero_fill_fake_positions",
            "uncited_executable_reviews",
            "critical_ledger_mismatches",
            "restart_recovery",
        },
        "safety",
    )
    compliance = check(
        fixture.get("compliance"), {"broker", "regulatory"}, "compliance"
    )
    for authority in ("broker", "regulatory"):
        check(
            compliance.get(authority),
            {"confirmed", "attestation_id", "evidence_sha256"},
            f"compliance.{authority}",
        )
    return sorted(set(unknown))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_validation_report(
    fixture: dict[str, Any] | None,
    *,
    evaluated_at: str,
    input_error_codes: Sequence[str] = (),
) -> dict[str, Any]:
    fixture = fixture or {}
    rejections: list[dict[str, Any]] = []

    def reject(code: str, scope: str, observed: Any, required: str) -> None:
        rejections.append(
            {
                "code": code,
                "scope": scope,
                "observed": observed,
                "required": required,
            }
        )

    unknown_fields = _unknown_fields(fixture)
    for input_error_code in input_error_codes:
        reject(
            input_error_code,
            "fixture",
            "unreadable_or_invalid",
            "a readable UTF-8 JSON object",
        )

    schema_passed = (
        fixture.get("schema_version") == "decision-support-validation-input-v1"
        and not unknown_fields
        and not input_error_codes
    )
    if fixture.get("schema_version") != "decision-support-validation-input-v1":
        reject(
            "invalid_input_schema",
            "input",
            fixture.get("schema_version"),
            "decision-support-validation-input-v1",
        )
    for path in unknown_fields:
        reject(
            f"unknown_field:{path}",
            path,
            "present",
            "field must be removed because unknown inputs never participate in validation",
        )

    supplied = fixture.get("fingerprints")
    supplied = supplied if isinstance(supplied, dict) else {}
    algorithm_fingerprint = _sha256(Path(__file__).resolve())
    fingerprints: dict[str, str | None] = {}
    for name in FINGERPRINT_NAMES:
        claimed = supplied.get(name)
        if name == "algorithm":
            fingerprints[name] = algorithm_fingerprint
            if claimed is None:
                reject(
                    "missing_fingerprint:algorithm",
                    "fingerprints.algorithm",
                    None,
                    "the current CLI SHA-256",
                )
            elif not _is_sha256(claimed):
                reject(
                    "invalid_fingerprint:algorithm",
                    "fingerprints.algorithm",
                    claimed,
                    "64 hexadecimal SHA-256 characters matching the current CLI",
                )
            elif claimed.lower() != algorithm_fingerprint:
                reject(
                    "algorithm_fingerprint_mismatch",
                    "fingerprints.algorithm",
                    claimed,
                    algorithm_fingerprint,
                )
            continue
        fingerprints[name] = claimed.lower() if _is_sha256(claimed) else None
        if claimed is None:
            reject(
                f"missing_fingerprint:{name}",
                f"fingerprints.{name}",
                None,
                "64 hexadecimal SHA-256 characters",
            )
        elif not _is_sha256(claimed):
            reject(
                f"invalid_fingerprint:{name}",
                f"fingerprints.{name}",
                claimed,
                "64 hexadecimal SHA-256 characters",
            )
    fingerprints_passed = not any(
        item["scope"].startswith("fingerprints.") for item in rejections
    )

    replay = fixture.get("replay")
    replay = replay if isinstance(replay, dict) else {}
    replay_checks = {
        "prefix_invariance": replay.get("prefix_invariance") is True,
        "prefix_cases": type(replay.get("prefix_cases")) is int
        and replay.get("prefix_cases") > 0,
        "no_future_data": replay.get("no_future_data") is True,
        "future_perturbation_cases": type(replay.get("future_perturbation_cases"))
        is int
        and replay.get("future_perturbation_cases") > 0,
        "incomplete_bar_rejected": replay.get("incomplete_bar_rejected") is True,
        "incomplete_bar_cases": type(replay.get("incomplete_bar_cases")) is int
        and replay.get("incomplete_bar_cases") > 0,
        "event_parity": _is_number(replay.get("event_parity"))
        and replay.get("event_parity") == 1.0,
    }
    replay_requirements = {
        "prefix_invariance": "exact boolean true",
        "prefix_cases": "positive integer count of executed prefix cases",
        "no_future_data": "exact boolean true",
        "future_perturbation_cases": "positive integer count of future perturbations",
        "incomplete_bar_rejected": "exact boolean true",
        "incomplete_bar_cases": "positive integer count of incomplete-bar cases",
        "event_parity": "finite number exactly 1.0",
    }
    for check_name, passed in replay_checks.items():
        if not passed:
            reject(
                f"replay_{check_name}_failed",
                f"replay.{check_name}",
                replay.get(check_name),
                replay_requirements[check_name],
            )

    tracks_input = fixture.get("tracks")
    tracks_input = tracks_input if isinstance(tracks_input, dict) else {}
    track_results: dict[str, Any] = {}
    event_id_sets: dict[str, set[str]] = {}
    for track_name in TRACK_NAMES:
        track = tracks_input.get(track_name)
        track = track if isinstance(track, dict) else {}
        if track_name not in tracks_input:
            reject(
                f"missing_track:{track_name}",
                f"tracks.{track_name}",
                None,
                "track validation object",
            )

        event_ids = track.get("event_ids")
        valid_event_ids = (
            isinstance(event_ids, list)
            and len(event_ids) > 0
            and all(isinstance(item, str) and item.strip() for item in event_ids)
            and len(event_ids) == len(set(event_ids))
        )
        if not valid_event_ids:
            reject(
                "invalid_track_event_ids",
                f"tracks.{track_name}.event_ids",
                event_ids,
                "a non-empty list of unique non-empty strings",
            )
        event_id_sets[track_name] = set(event_ids) if valid_event_ids else set()

        oos = track.get("oos")
        oos = oos if isinstance(oos, dict) else {}
        oos_checks = {
            "chronological_holdout": oos.get("split") == "chronological_holdout",
            "not_tuned_on_oos": oos.get("parameter_tuned_on_oos") is False,
            "completed_trades": type(oos.get("completed_trades")) is int
            and oos.get("completed_trades") >= 100,
            "net_expectancy": _is_number(oos.get("net_expectancy"))
            and oos.get("net_expectancy") > 0,
            "profit_factor": _is_number(oos.get("profit_factor"))
            and oos.get("profit_factor") > 1.1,
            "max_drawdown": _is_number(oos.get("max_drawdown"))
            and 0 <= oos.get("max_drawdown") <= 0.08,
            "event_parity": _is_number(oos.get("event_parity"))
            and oos.get("event_parity") == 1.0,
        }
        oos_required = {
            "chronological_holdout": "split must be chronological_holdout",
            "not_tuned_on_oos": "parameter_tuned_on_oos must be exact boolean false",
            "completed_trades": "integer >= 100",
            "net_expectancy": "finite number > 0 after costs",
            "profit_factor": "finite number > 1.1",
            "max_drawdown": "finite number in [0, 0.08]",
            "event_parity": "finite number exactly 1.0 for this strategy track",
        }
        oos_fields = {
            "chronological_holdout": "split",
            "not_tuned_on_oos": "parameter_tuned_on_oos",
            "completed_trades": "completed_trades",
            "net_expectancy": "net_expectancy",
            "profit_factor": "profit_factor",
            "max_drawdown": "max_drawdown",
            "event_parity": "event_parity",
        }
        for check_name, passed in oos_checks.items():
            if not passed:
                field = oos_fields[check_name]
                reject(
                    f"oos_{check_name}_failed",
                    f"tracks.{track_name}.oos.{field}",
                    oos.get(field),
                    oos_required[check_name],
                )

        paper = track.get("paper")
        paper = paper if isinstance(paper, dict) else {}
        paper_checks = {
            "trading_days": type(paper.get("trading_days")) is int
            and paper.get("trading_days") >= 20,
            "executable_events": type(paper.get("executable_events")) is int
            and paper.get("executable_events") >= 30,
        }
        for check_name, passed in paper_checks.items():
            if not passed:
                reject(
                    f"paper_{check_name}_below_threshold",
                    f"tracks.{track_name}.paper.{check_name}",
                    paper.get(check_name),
                    "integer >= 20" if check_name == "trading_days" else "integer >= 30",
                )

        track_results[track_name] = {
            "event_count": len(event_id_sets[track_name]),
            "oos": {
                "split": oos.get("split"),
                "parameter_tuned_on_oos": oos.get("parameter_tuned_on_oos"),
                "completed_trades": oos.get("completed_trades"),
                "remaining_completed_trades": _remaining_count(
                    oos.get("completed_trades"), 100
                ),
                "net_expectancy": oos.get("net_expectancy"),
                "profit_factor": oos.get("profit_factor"),
                "max_drawdown": oos.get("max_drawdown"),
                "event_parity": oos.get("event_parity"),
            },
            "oos_gate_passed": all(oos_checks.values()),
            "paper": {
                "trading_days": paper.get("trading_days"),
                "executable_events": paper.get("executable_events"),
                "remaining_trading_days": _remaining_count(
                    paper.get("trading_days"), 20
                ),
                "remaining_executable_events": _remaining_count(
                    paper.get("executable_events"), 30
                ),
            },
            "paper_gate_passed": all(paper_checks.values()),
        }

    overlap = sorted(
        event_id_sets[TRACK_NAMES[0]].intersection(event_id_sets[TRACK_NAMES[1]])
    )
    track_separation_passed = not overlap and all(event_id_sets.values())
    if overlap:
        reject(
            "strategy_track_event_overlap",
            "tracks",
            overlap,
            "no event ID may belong to both strategy tracks",
        )

    safety = fixture.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    safety_checks: dict[str, bool] = {}
    for field in (
        "risk_violations",
        "lookahead_events",
        "zero_fill_fake_positions",
        "uncited_executable_reviews",
        "critical_ledger_mismatches",
    ):
        safety_checks[field] = type(safety.get(field)) is int and safety.get(field) == 0
        if not safety_checks[field]:
            reject(
                f"safety_{field}_nonzero_or_unknown",
                f"safety.{field}",
                safety.get(field),
                "integer exactly 0",
            )
    safety_checks["restart_recovery"] = safety.get("restart_recovery") is True
    if not safety_checks["restart_recovery"]:
        reject(
            "restart_recovery_not_proven",
            "safety.restart_recovery",
            safety.get("restart_recovery"),
            "exact boolean true",
        )

    compliance = fixture.get("compliance")
    compliance = compliance if isinstance(compliance, dict) else {}
    compliance_results: dict[str, Any] = {}
    for authority in ("broker", "regulatory"):
        attestation = compliance.get(authority)
        attestation = attestation if isinstance(attestation, dict) else {}
        checks = {
            "confirmed": attestation.get("confirmed") is True,
            "attestation_id": isinstance(attestation.get("attestation_id"), str)
            and bool(attestation.get("attestation_id", "").strip()),
            "evidence_sha256": _is_sha256(attestation.get("evidence_sha256")),
        }
        if not all(checks.values()):
            reject(
                f"{authority}_compliance_confirmation_missing",
                f"compliance.{authority}",
                attestation or None,
                "confirmed=true with non-empty attestation_id and evidence_sha256",
            )
        compliance_results[authority] = {
            "passed": all(checks.values()),
            "attestation_id": attestation.get("attestation_id"),
            "evidence_sha256": attestation.get("evidence_sha256"),
        }

    gates = {
        "input_schema": {"passed": schema_passed},
        "fingerprints": {"passed": fingerprints_passed},
        "replay": {"passed": all(replay_checks.values())},
        "track_separation": {"passed": bool(track_separation_passed)},
        "oos_by_track": {
            "passed": all(row["oos_gate_passed"] for row in track_results.values())
        },
        "paper_by_track": {
            "passed": all(row["paper_gate_passed"] for row in track_results.values())
        },
        "safety": {"passed": all(safety_checks.values())},
        "compliance": {
            "passed": all(row["passed"] for row in compliance_results.values())
        },
    }
    eligible = all(gate["passed"] for gate in gates.values()) and not rejections
    rejections.sort(key=lambda item: (item["scope"], item["code"]))
    report = {
        "schema_version": "decision-support-validation-report-v1",
        "evaluated_at": evaluated_at,
        "mode": "offline_validation",
        "no_order_execution": True,
        "input_fixture_fingerprint": _canonical_fingerprint(fixture),
        "fingerprints": fingerprints,
        "replay": {**replay, "checks": replay_checks},
        "track_separation": {
            "passed": bool(track_separation_passed),
            "overlap_event_ids": overlap,
        },
        "tracks": track_results,
        "safety": {"metrics": safety, "checks": safety_checks},
        "compliance": compliance_results,
        "gates": gates,
        "status": "small_cap_manual_eligible" if eligible else "paper_gate_pending",
        "paper_gate_pending": not eligible,
        "eligible_for_small_cap_manual": eligible,
        "rejections": rejections,
        "rejection_codes": sorted({item["code"] for item in rejections}),
    }
    return _json_safe(report)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--evaluated-at",
        default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    fixture: dict[str, Any] | None = None
    input_error_codes: list[str] = []
    if args.fixture is not None:
        try:
            fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            fixture = {}
            input_error_codes.append("fixture_parse_error")
        except (OSError, UnicodeError):
            fixture = {}
            input_error_codes.append("fixture_read_error")
        if not isinstance(fixture, dict):
            fixture = {}
            input_error_codes.append("fixture_root_not_object")
    report = build_validation_report(
        fixture,
        evaluated_at=args.evaluated_at,
        input_error_codes=input_error_codes,
    )
    _atomic_json_write(args.output_dir / "validation.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if report["eligible_for_small_cap_manual"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
