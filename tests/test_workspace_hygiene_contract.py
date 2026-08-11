from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "ops" / "cleanup_local_generated_artifacts.ps1"
def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_generated_lesson_corpus_remains_ignored() -> None:
    corpus_probe = "audit/chanlun_lesson_corpus/images/001.png"
    ignored = _git("check-ignore", "-v", "--", corpus_probe)
    assert ignored.returncode == 0
    assert "/audit/chanlun_lesson_corpus/" in ignored.stdout


@pytest.mark.parametrize(
    ("path", "expected_text", "expected_eol"),
    (
        ("src/chanlun/core/cl.py", "set", "lf"),
        ("ops/manage_qmt_runtime.ps1", "set", "crlf"),
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
    assert ".cache\\chanlun_human_review_forward" in source
    assert ".cache\\chanlun_human_review" in source
    assert ".cache\\chanlun_qmt_sector_ledger" in source
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
    assert payload["schema"] == "chanlun-local-generated-artifact-cleanup"
    assert payload["mode"] == "DRY_RUN"
    assert payload["removed_count"] == 0
