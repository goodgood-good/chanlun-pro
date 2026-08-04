from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "ops" / "cleanup_local_generated_artifacts.ps1"
WORKSPACE_MANIFEST = (
    ROOT / "audit" / "chanlun_live_integration" / "workspace_manifest.json"
)


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _visible_to_git(path: str) -> bool:
    tracked = _git("ls-files", "--error-unmatch", "--", path)
    if tracked.returncode == 0:
        return True
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", path)
    return path in untracked.stdout.splitlines()


def test_unique_specification_and_formal_audits_are_not_hidden() -> None:
    visible = (
        "audit/chanlun_live_strategy/complete_strategy_v3.md",
        "audit/chanlun_live_integration/v3_final_report.md",
        (
            "audit/chanlun_trading_system_forward_paper/"
            "forward_paper_setup_2026-07-28.md"
        ),
    )

    assert all(_visible_to_git(path) for path in visible)
    corpus_probe = "audit/chanlun_lesson_corpus/lessons/001.md"
    ignored = _git("check-ignore", "-v", "--", corpus_probe)
    assert ignored.returncode == 0
    assert "*.md" in ignored.stdout
    assert "!audit/" not in ignored.stdout


def test_workspace_manifest_pins_the_unique_specification_and_corpus() -> None:
    payload = json.loads(WORKSPACE_MANIFEST.read_text(encoding="utf-8"))
    spec = ROOT / payload["protected_spec"]["path"]
    spec_sha256 = "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()

    assert payload["schema"] == "chanlun-v3-workspace-manifest/v1"
    assert payload["protected_spec"]["sha256"] == spec_sha256
    assert payload["protected_corpus"]["path"] == "audit/chanlun_lesson_corpus"
    assert payload["protected_corpus"]["file_count"] == 220
    assert payload["protected_corpus"]["tree_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    ("path", "expected_text", "expected_eol"),
    (
        ("src/chanlun/core/cl.py", "set", "lf"),
        ("ops/manage_qmt_runtime.ps1", "set", "crlf"),
        (
            "audit/chanlun_live_strategy/complete_strategy_v3.md",
            "set",
            "lf",
        ),
        ("tests/fixtures/SZ.002299_1m.parquet", "unset", "unspecified"),
    ),
)
def test_line_endings_and_binary_evidence_are_explicit(
    path: str,
    expected_text: str,
    expected_eol: str,
) -> None:
    result = _git("check-attr", "text", "eol", "--", path)

    assert result.returncode == 0, result.stderr
    values = {
        line.split(": ", 2)[1]: line.split(": ", 2)[2]
        for line in result.stdout.splitlines()
    }
    assert values == {"text": expected_text, "eol": expected_eol}


def test_local_cleanup_is_dry_run_by_default_and_path_bounded() -> None:
    source = CLEANUP.read_text(encoding="utf-8")

    assert "[switch]$Execute" in source
    assert "if ($Execute)" in source
    assert "Refusing cleanup target outside repository" in source
    assert 'mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }' in source
    assert ".cache\\chanlun_v3_human_review_forward" in source
    assert ".cache\\chanlun_v3_human_review" in source
    assert ".cache\\chanlun_v3_qmt_sector_ledger" in source
    assert "qmt_runtime_*.log" in source


@pytest.mark.skipif(os.name != "nt", reason="cleanup helper targets Windows")
def test_local_cleanup_dry_run_never_removes_candidates() -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CLEANUP),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "chanlun-local-generated-artifact-cleanup/v1"
    assert payload["mode"] == "DRY_RUN"
    assert payload["removed_count"] == 0
