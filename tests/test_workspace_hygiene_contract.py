from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLEANUP = ROOT / "ops" / "cleanup_local_generated_artifacts.ps1"
RUNTIME_CLEANUP = ROOT / "ops" / "cleanup_legacy_runtime_state.ps1"
INVALID_ALGORITHM_CLEANUP = ROOT / "ops" / "cleanup_invalid_algorithm_state.ps1"
HISTORICAL_BACKTEST = ROOT / "ops" / "run_historical_backtest.ps1"
RESEARCH_SAMPLES = {
    "smoke2": (ROOT / "config" / "research_backtest_smoke_2.txt", 2),
    "validation12": (
        ROOT / "config" / "research_backtest_validation_12.txt",
        12,
    ),
}


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


def test_historical_backtest_defaults_to_fixed_small_research_cohort() -> None:
    source = HISTORICAL_BACKTEST.read_text(encoding="utf-8-sig")

    assert '[ValidateSet("smoke2", "validation12")]' in source
    assert '[string]$Profile = "smoke2"' in source
    assert '[ValidateSet("Extract", "Prefix", "Finalize", "All")]' in source
    assert '[string]$Stage = "Extract"' in source
    assert "[switch]$ConfirmLargeScope" in source
    assert "[switch]$FullMarket" in source
    assert "if ($FullMarket -and -not $ConfirmLargeScope)" in source
    assert "FULL_MARKET_EXPLICIT" in source
    assert "RESEARCH_PROFILE_$($Profile.ToUpperInvariant())" in source
    assert "if (-not $FullMarket)" in source
    assert '@("--codes", ($researchCodes -join ","))' in source
    assert '$extractionArguments += "--confirm-large-scope"' in source
    assert (
        "$extractionArguments += @(\n"
        '                        "--full-market",\n'
        '                        "--confirm-large-scope"\n'
        "                    )"
    ) in source
    assert "$researchCodes.Count -gt 20 -and -not $ConfirmLargeScope" in source
    assert (
        "if ($researchCodes.Count -gt 20) {\n"
        '                        $extractionArguments += "--confirm-large-scope"'
    ) in source
    assert '$Stage -in @("Extract", "Finalize", "All")' in source
    assert '$Stage -in @("Prefix", "All")' in source
    assert '$Stage -in @("Finalize", "All")' in source
    assert '"--reuse-sector-cache",' in source
    assert '"--sector-workers", "$([Math]::Min($Workers, 3))"' in source
    assert '"--max-sector-count"' in source
    assert '"--max-sector-closure"' in source
    assert '"--confirm-large-sector-scope"' in source
    assert "research_sample_smoke_2" in source
    assert "research_sample_validation_12" in source
    assert "full_market_explicit" in source
    assert "pit_reference" in source
    assert "fixed_year_2025_2026" not in source
    assert "research48" not in source
    assert "research_sample_48" not in source
    assert "[switch]$GeneratePIT" in source
    assert '$pitSnapshot = Join-Path $inputDirectory "pit_metadata.json"' in source
    assert '"--codes-file", $researchCodesPath' in source
    assert '"--membership-index", $MembershipIndex' in source
    assert '@("--full-market", "--confirm-large-scope")' in source

    samples: dict[str, tuple[str, ...]] = {}
    for profile, (path, expected_count) in RESEARCH_SAMPLES.items():
        symbols = tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        assert len(symbols) == len(set(symbols)) == expected_count
        assert all(
            symbol.startswith(("SH.", "SZ.", "BJ.")) and len(symbol) == 9
            for symbol in symbols
        )
        samples[profile] = symbols

    assert set(samples["smoke2"]).isdisjoint(samples["validation12"])
    validation_contract = RESEARCH_SAMPLES["validation12"][0].read_text(
        encoding="utf-8"
    )
    assert "Pre-registered gate cohort" in validation_contract
    assert "Never select by later" in validation_contract


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
    assert "[switch]$PurgeInvalidBacktestFacts" in source
    assert "[switch]$PurgeRetiredRuntimeState" in source
    assert "if ($Execute)" in source
    assert "Refusing cleanup target outside repository" in source
    assert 'mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }' in source
    assert '@{ path = "output"; category = "generated_output" }' in source
    assert '@{ path = "tmp"; category = "temporary_artifact" }' in source
    assert '@{ path = ".omc"; category = "agent_session_artifact" }' in source
    assert "rootGeneratedLogPath" in source
    assert "appGeneratedLogPath" in source
    assert '"web\\chanlun_chart\\logs"' in source
    assert "active_app_logs" in source
    assert "active_ops_logs" in source
    assert "[IO.FileShare]::None" in source
    assert "Get-LockedCleanupCandidateFiles" in source
    assert "Refusing cleanup because candidate files are active" in source
    assert ".cache\\chanlun_human_review_forward" in source
    assert ".cache\\chanlun_human_review" in source
    assert ".cache\\chanlun_scheduler" in source
    assert ".cache\\chanlun_qmt_sector_ledger" in source
    assert 'Category "legacy_cache"' in source
    assert ".cache\\chanlun_v3_human_review_forward" in source
    assert ".cache\\chanlun_v31_csi300_broad_pool" in source
    assert ".cache\\historical_backtest_preflight_report_20260816" in source
    assert "qmt_runtime_\\d{4}-\\d{2}-\\d{2}" in source
    assert '"historical_backtest.lock"' in source
    assert "currentQmtLogName" not in source
    assert '"web_recovery_*"' in source
    assert '"web_watchdog_*"' in source
    assert "research_sample_smoke_2" not in source
    assert "research_sample_validation_12" not in source
    assert '"pit_sector_composites"' not in source
    assert '"pit_sectors"' not in source

    runtime_cleanup = (ROOT / "ops/cleanup_legacy_runtime_state.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-LockedCleanupCandidateFiles" in runtime_cleanup
    assert "Refusing cleanup because candidate files are active" in runtime_cleanup
    assert "[IO.FileShare]::None" in runtime_cleanup
    assert '"pit_metadata.json"' not in source
    assert '-Filter "*.dmp"' in source
    assert '"server-*.stdout.log"' in source
    assert '"server-*.stderr.log"' in source
    assert '"targeted_v*"' in source
    assert '"research_diagnostic_*"' in source

    invalid_algorithm_cleanup = INVALID_ALGORITHM_CLEANUP.read_text(
        encoding="utf-8-sig"
    )
    assert "[switch]$PreserveValidationGate" in invalid_algorithm_cleanup
    assert "if (-not $PreserveValidationGate)" in invalid_algorithm_cleanup
    for stale_runtime_target in (
        '"cache\\symbols"',
        '"monitor\\dingtalk_chart_images"',
        '"decision_support\\trading_screening_sector_member_status_facts"',
    ):
        assert stale_runtime_target in invalid_algorithm_cleanup
    assert (
        '"audit\\chanlun_trading_system_backtest\\research_sample_validation_12"'
        in invalid_algorithm_cleanup
    )


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


def test_legacy_runtime_cleanup_is_explicit_and_preserves_source_data() -> None:
    source = RUNTIME_CLEANUP.read_text(encoding="utf-8")

    assert "[switch]$Execute" in source
    assert 'mode = if ($Execute) { "EXECUTE" } else { "DRY_RUN" }' in source
    assert "Assert-DirectRuntimeChild" in source
    assert '"decision_support"' in source
    assert '"chart_cache"' in source
    assert '"monitor"' in source
    assert '"klines"' in source
    assert '"xdxr"' in source
    assert '".flask_secret_key"' in source
