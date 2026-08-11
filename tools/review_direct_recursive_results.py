#!/usr/bin/env python3
"""Independently review serialized direct-recursive strict strategy artifacts."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "audit" / "chanlun_live_integration"
PRESCREEN = INTEGRATION / "direct_recursive_etf_prescreen.json"
BACKTEST = INTEGRATION / "direct_recursive_component_backtest.json"
DATA_AUDIT = INTEGRATION / "direct_recursive_data_acceptance.json"
CORE = INTEGRATION / "direct_recursive_authorized_core_verification.json"
PROTECTED = INTEGRATION / "protected_input_verification.json"
OUTPUT = INTEGRATION / "direct_recursive_independent_review.json"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"artifact must be a mapping: {path}")
    stable = dict(value)
    recorded = stable.pop("content_sha256", None)
    if recorded is not None and recorded != _canonical_hash(stable):
        raise ValueError(f"artifact content hash changed: {path}")
    return value


def _atomic_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def review() -> dict[str, object]:
    prescreen = _load(PRESCREEN)
    backtest = _load(BACKTEST)
    data_audit = _load(DATA_AUDIT)
    core = _load(CORE)
    protected = _load(PROTECTED)
    reports = prescreen["instrument_reports"]
    totals = prescreen["totals"]
    replay = backtest["replay"]
    metrics = replay["metrics"]
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, evidence: object) -> None:
        checks.append({"name": name, "passed": bool(passed), "evidence": evidence})

    diagnostic_strategic = sum(
        int(item["diagnostic_strategic_point_count"]) for item in reports
    )
    formal_strategic = sum(int(item["strategic_point_count"]) for item in reports)
    diagnostic_aligned = sum(
        int(item["diagnostic_aligned_entry_count"]) for item in reports
    )
    formal_aligned = sum(int(item["aligned_entry_count"]) for item in reports)
    eligible_instruments = sum(bool(item["formal_signal_eligible"]) for item in reports)
    check(
        "prescreen_totals_recomputed",
        (
            diagnostic_strategic == totals["diagnostic_strategic_points"]
            and formal_strategic == totals["strategic_points"]
            and diagnostic_aligned == totals["diagnostic_aligned_entries"]
            and formal_aligned == totals["aligned_entries"]
            and eligible_instruments == totals["adjustment_eligible_instruments"]
        ),
        {
            "diagnostic_strategic": diagnostic_strategic,
            "formal_strategic": formal_strategic,
            "diagnostic_aligned": diagnostic_aligned,
            "formal_aligned": formal_aligned,
            "eligible_instruments": eligible_instruments,
        },
    )
    ineligible_promotions = tuple(
        item["provider_symbol"]
        for item in reports
        if not item["formal_signal_eligible"]
        and (
            item["strategic_point_count"]
            or item["aligned_entry_count"]
            or item["replay_eligible_structure_signal_count"]
        )
    )
    check(
        "missing_adjustment_never_promoted",
        not ineligible_promotions,
        ineligible_promotions,
    )
    wrong_level_points = tuple(
        point["point_id"]
        for item in reports
        for point in item["strategic_points"]
        if not (
            point["raw_source_frequency"] == "1m"
            and point["raw_recursive_level"] == 2
            and point["logical_level"] == "L0_30M"
        )
    )
    check("strategic_points_are_raw_level2", not wrong_level_points, wrong_level_points)
    recomputed_rejections = Counter()
    for item in reports:
        recomputed_rejections.update(item["alignment_rejection_counts"])
    check(
        "alignment_rejections_recomputed",
        dict(sorted(recomputed_rejections.items()))
        == totals["alignment_rejection_counts"],
        dict(sorted(recomputed_rejections.items())),
    )
    check(
        "backtest_binds_prescreen",
        (
            backtest["prescreen_content_sha256"] == prescreen["content_sha256"]
            and backtest["prescreen_file_sha256"] == _file_hash(PRESCREEN)
        ),
        backtest["prescreen_content_sha256"],
    )
    check(
        "empty_return_is_not_performance",
        (
            metrics["empty_replay"] is True
            and metrics["performance_evaluable"] is False
            and backtest["return_claim_allowed"] is False
            and metrics["order_count"] == 0
            and metrics["fill_count"] == 0
            and metrics["strategic_cycle_count"] == 0
            and metrics["tactical_cycle_count"] == 0
        ),
        {
            "net_return_field": metrics["net_return"],
            "max_drawdown_field": metrics["max_drawdown"],
            "interpretation": backtest["return_field_interpretation"],
        },
    )
    check(
        "data_audit_binds_results",
        (
            data_audit["prescreen"]["file_sha256"] == _file_hash(PRESCREEN)
            and data_audit["backtest"]["file_sha256"] == _file_hash(BACKTEST)
            and data_audit["technical_component_grade"] == "COMPONENT_ONLY"
            and data_audit["full_system_data_gate"]["eligibility"]
            == "RESEARCH_ONLY"
        ),
        data_audit["return_evaluation"],
    )
    check(
        "authorized_core_delta_only",
        (
            core["status"] == "PASS_AUTHORIZED_DELTA"
            and not core["unexpected_changes"]
            and core["representative_outputs_unchanged"] is True
        ),
        {
            "authorized_changes": len(core["authorized_changes"]),
            "unchanged_original_core_files": core[
                "unchanged_original_core_file_count"
            ],
        },
    )
    check(
        "protected_inputs_unchanged",
        protected["status"] == "PASS_ZERO_CHANGE",
        protected["status"],
    )
    live_values = (
        prescreen["live_status"],
        backtest["live_status"],
        data_audit["live_status"],
    )
    check(
        "live_disabled_everywhere",
        live_values == ("LIVE_DISABLED",) * 3,
        live_values,
    )
    passed = all(item["passed"] for item in checks)
    document: dict[str, object] = {
        "schema": "chanlun-direct-recursive-independent-review",
        "passed": passed,
        "check_count": len(checks),
        "passed_count": sum(bool(item["passed"]) for item in checks),
        "checks": checks,
        "artifact_file_hashes": {
            str(path.relative_to(ROOT)): _file_hash(path)
            for path in (PRESCREEN, BACKTEST, DATA_AUDIT, CORE, PROTECTED)
        },
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    document["content_sha256"] = _canonical_hash(document)
    return document


def main() -> int:
    document = review()
    _atomic_json(OUTPUT, document)
    print(
        json.dumps(
            {
                "output": str(OUTPUT.relative_to(ROOT)),
                "passed": document["passed"],
                "checks": f"{document['passed_count']}/{document['check_count']}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if document["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
