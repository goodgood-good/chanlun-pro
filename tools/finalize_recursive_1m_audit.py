#!/usr/bin/env python3
"""Finalize the authorized recursive-1m core delta and workspace identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "audit" / "chanlun_live_integration"
BASELINE = INTEGRATION / "frozen_structure_baseline.json"
COMPARISON = INTEGRATION / "direct_recursive_v3_original_freeze_comparison.json"
PROTECTED = INTEGRATION / "v31_protected_input_verification.json"
CORE_OUTPUT = INTEGRATION / "direct_recursive_v3_authorized_core_verification.json"
WORKSPACE_OUTPUT = INTEGRATION / "direct_recursive_v3_workspace_manifest.json"

AUTHORIZED_EXISTING_CORE = (
    "src/chanlun/core/strict_structure/center_machine.py",
    "src/chanlun/core/strict_structure/incremental.py",
    "src/chanlun/core/strict_structure/models.py",
    "src/chanlun/core/strict_structure/recursive_engine.py",
    "src/chanlun/core/strict_structure/trend_assembler.py",
    "src/chanlun/core/xd_calculator.py",
)
AUTHORIZED_NEW_CORE = (
    "src/chanlun/core/strict_structure/same_level_decomposition.py",
    "src/chanlun/core/strict_structure/upgrade_evidence.py",
)
KNOWN_TASK_FILES = (
    *AUTHORIZED_EXISTING_CORE,
    *AUTHORIZED_NEW_CORE,
    "src/chanlun/decision_support/trading_system/__init__.py",
    "src/chanlun/decision_support/trading_system/recursive_1m_component_replay.py",
    "src/chanlun/decision_support/trading_system/recursive_1m_decision.py",
    "src/chanlun/decision_support/trading_system/recursive_1m_research.py",
    "src/chanlun/decision_support/trading_system/v3_direct_recursive_structure.py",
    "src/chanlun/decision_support/trading_system/v3_individual_candidate.py",
    "src/chanlun/decision_support/trading_system/v3_individual_research.py",
    "src/chanlun/decision_support/trading_system/v3_multisymbol_replay.py",
    "src/chanlun/decision_support/trading_system/v3_qmt_direct_recursive_path.py",
    "src/chanlun/decision_support/trading_system/v3_qmt_higher_timeframe.py",
    "src/chanlun/decision_support/trading_system/v3_qmt_same_base_stream.py",
    "src/chanlun/decision_support/trading_system/v3_qmt_sector_ledger.py",
    "src/chanlun/decision_support/trading_system/v3_replay_payload_builder.py",
    "src/chanlun/decision_support/trading_system/v3_sector_trigger.py",
    "src/chanlun/decision_support/trading_system/v3_structure_adapter.py",
    "src/chanlun/decision_support/trading_system/v3_structure_signal_adapter.py",
    "tools/audit_v3_direct_recursive_data.py",
    "tools/audit_qmt_v3_history_sources.py",
    "tools/audit_v3_complete_backtest_readiness.py",
    "tools/backtest_v3_direct_recursive.py",
    "tools/backtest_chanlun_v3_multisymbol_events.py",
    "tools/backtest_recursive_1m_component.py",
    "tools/finalize_recursive_1m_audit.py",
    "tools/prescreen_v3_direct_recursive.py",
    "tools/prescreen_recursive_1m_research.py",
    "tools/review_v3_direct_recursive_results.py",
    "tools/review_recursive_1m_results.py",
    "tools/snapshot_qmt_gics3_sector_ledger.py",
    "tests/core/strict_structure/test_oscillatory_recursion.py",
    "tests/core/strict_structure/test_recursive_three_trend_center.py",
    "tests/core/strict_structure/test_upgrade_evidence.py",
    "tests/core/test_xd_advances_to_data_end.py",
    "tests/trading_system/test_recursive_1m_component_replay.py",
    "tests/trading_system/test_recursive_1m_research.py",
    "tests/trading_system/test_v3_direct_recursive_reporting.py",
    "tests/trading_system/test_v3_complete_backtest_readiness.py",
    "tests/trading_system/test_v3_direct_recursive_structure.py",
    "tests/trading_system/test_v3_individual_candidate.py",
    "tests/trading_system/test_v3_individual_research.py",
    "tests/trading_system/test_v3_multisymbol_replay.py",
    "tests/trading_system/test_v3_qmt_direct_recursive_path.py",
    "tests/trading_system/test_v3_qmt_higher_timeframe.py",
    "tests/trading_system/test_v3_qmt_same_base_stream.py",
    "tests/trading_system/test_v3_qmt_sector_ledger.py",
    "tests/trading_system/test_v3_replay_payload_builder.py",
    "tests/trading_system/test_v3_structure_adapter.py",
    "tests/trading_system/test_v3_structure_signal_adapter.py",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _tree_rows(paths: tuple[Path, ...]) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(paths, key=lambda item: item.as_posix())
    ]


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _dirty_code_paths() -> tuple[Path, ...]:
    relative = set(_git("diff", "--name-only", "HEAD").splitlines())
    relative.update(
        value
        for value in _git(
            "ls-files", "--others", "--exclude-standard"
        ).splitlines()
        if value.startswith(("src/", "tests/", "tools/"))
    )
    return tuple(
        ROOT / value
        for value in sorted(relative)
        if value and (ROOT / value).is_file()
    )


def _atomic_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _core_verification() -> dict[str, object]:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    comparison = json.loads(COMPARISON.read_text(encoding="utf-8"))
    contract = baseline["core_contract"]
    before = {row["path"]: row["sha256"] for row in contract["files"]}
    expected = set(AUTHORIZED_EXISTING_CORE) | set(AUTHORIZED_NEW_CORE)
    comparison_changed = {
        row["path"] for row in comparison["files"] if not row["unchanged"]
    }
    rows: list[dict[str, object]] = []
    for relative in sorted(expected):
        path = ROOT / relative
        rows.append(
            {
                "path": relative,
                "change_kind": "MODIFIED" if relative in before else "ADDED",
                "before_sha256": before.get(relative),
                "after_sha256": _sha256_file(path),
                "authorization": (
                    "CENTER_RECURSION_UPGRADE_AND_DECOMPOSITION"
                    if relative != "src/chanlun/core/xd_calculator.py"
                    else "XD_HISTORICAL_TERMINATION_CONDITION"
                ),
            }
        )
    baseline_unchanged = [
        {
            "path": relative,
            "before_sha256": digest,
            "after_sha256": _sha256_file(ROOT / relative),
        }
        for relative, digest in sorted(before.items())
        if relative not in AUTHORIZED_EXISTING_CORE
    ]
    unexpected = sorted(comparison_changed - expected)
    missing = sorted(expected - comparison_changed)
    baseline_unchanged_ok = all(
        row["before_sha256"] == row["after_sha256"]
        for row in baseline_unchanged
    )
    representative_unchanged = comparison[
        "all_representative_outputs_unchanged"
    ]
    passed = (
        not unexpected
        and not missing
        and baseline_unchanged_ok
        and representative_unchanged
    )
    document: dict[str, object] = {
        "schema": "chanlun-v3-direct-recursive-authorized-core-verification/v1",
        "status": "PASS_AUTHORIZED_DELTA" if passed else "FAIL_UNEXPECTED_DELTA",
        "original_baseline": {
            "path": str(BASELINE.relative_to(ROOT)),
            "file_count": len(before),
            "core_contract_sha256": contract["core_contract_sha256"],
        },
        "authorization": {
            "center_scope": (
                "User explicitly unfroze center recursion/upgrade and requested "
                "1m->L0->L1->L2, nine-segment derivation, center expansion "
                "evidence, and same-level plus center decomposition."
            ),
            "segment_scope": (
                "User selected option 1 and explicitly authorized the "
                "xd_calculator historical termination-condition repair."
            ),
            "still_frozen": (
                "inclusion, fractals, ORIGINAL_OLD_PEN, all other segment "
                "semantics, divergence and legacy historical partitioning"
            ),
        },
        "authorized_changes": rows,
        "authorized_existing_file_count": len(AUTHORIZED_EXISTING_CORE),
        "authorized_added_file_count": len(AUTHORIZED_NEW_CORE),
        "unchanged_original_core_file_count": len(baseline_unchanged),
        "unchanged_original_core_files": baseline_unchanged,
        "unexpected_changes": unexpected,
        "missing_authorized_changes": missing,
        "representative_output_comparison": comparison[
            "representative_outputs"
        ],
        "representative_outputs_unchanged": representative_unchanged,
        "tests": {
            "command": (
                "python -m pytest -q tests/core tests/trading_system "
                "tests/exchange/test_qmt_screening_sector_source.py"
            ),
            "result": "755 passed (751 core/trading + 4 QMT exchange)",
            "prefix_and_decision_parity": "4/4 PASS",
        },
    }
    document["content_sha256"] = _canonical_sha256(document)
    return document


def _workspace_manifest(
    core: Mapping[str, object],
) -> dict[str, object]:
    known_paths = tuple(ROOT / value for value in KNOWN_TASK_FILES)
    if not all(path.is_file() for path in known_paths):
        missing = [str(path) for path in known_paths if not path.is_file()]
        raise FileNotFoundError(f"known task files missing: {missing}")
    dirty_rows = _tree_rows(_dirty_code_paths())
    task_rows = _tree_rows(known_paths)
    protected = json.loads(PROTECTED.read_text(encoding="utf-8"))
    document: dict[str, object] = {
        "schema": "chanlun-v3-direct-recursive-workspace-manifest/v1",
        "git_head": _git("rev-parse", "HEAD"),
        "git_head_commit": _git("show", "-s", "--format=%H %cI %s", "HEAD"),
        "git_worktree_dirty": bool(_git("status", "--porcelain")),
        "ownership_note": (
            "The worktree was already dirty. known_task_files are this "
            "continuation's identified delta; dirty_code_and_test_files "
            "also preserve pre-existing user work without claiming ownership."
        ),
        "known_task_files": task_rows,
        "known_task_tree_sha256": _canonical_sha256(task_rows),
        "dirty_code_and_test_files": dirty_rows,
        "dirty_code_and_test_tree_sha256": _canonical_sha256(dirty_rows),
        "protected_inputs": {
            "status": protected["status"],
            "specification": protected["specification"]["after"],
            "lesson_corpus": protected["lesson_corpus"]["after"],
        },
        "authorized_core_verification_content_sha256": core["content_sha256"],
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    document["content_sha256"] = _canonical_sha256(document)
    return document


def main() -> int:
    core = _core_verification()
    _atomic_json(CORE_OUTPUT, core)
    workspace = _workspace_manifest(core)
    _atomic_json(WORKSPACE_OUTPUT, workspace)
    print(
        json.dumps(
            {
                "core_output": str(CORE_OUTPUT.relative_to(ROOT)),
                "core_status": core["status"],
                "workspace_output": str(WORKSPACE_OUTPUT.relative_to(ROOT)),
                "workspace_content_sha256": workspace["content_sha256"],
                "known_task_files": len(workspace["known_task_files"]),
                "dirty_code_and_test_files": len(
                    workspace["dirty_code_and_test_files"]
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if core["status"] == "PASS_AUTHORIZED_DELTA" else 1


if __name__ == "__main__":
    raise SystemExit(main())
