from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "tools" / "validate_decision_support.py"


def _run_cli(output_dir: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    command = [
        sys.executable,
        str(CLI),
        "--output-dir",
        str(output_dir),
        "--evaluated-at",
        "2026-07-14T08:00:00Z",
        *args,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    report_path = output_dir / "validation.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    return completed, report


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _valid_fixture() -> dict:
    return {
        "schema_version": "decision-support-validation-input-v1",
        "fingerprints": {
            "data": _digest("data"),
            "algorithm": hashlib.sha256(CLI.read_bytes()).hexdigest(),
            "rule_set": _digest("rule-set"),
            "corpus": _digest("corpus"),
            "model": _digest("model"),
            "prompt": _digest("prompt"),
        },
        "replay": {
            "prefix_invariance": True,
            "prefix_cases": 250,
            "no_future_data": True,
            "future_perturbation_cases": 250,
            "incomplete_bar_rejected": True,
            "incomplete_bar_cases": 50,
            "event_parity": 1.0,
        },
        "tracks": {
            "trend_continuation": {
                "event_ids": ["trend-001"],
                "oos": {
                    "split": "chronological_holdout",
                    "parameter_tuned_on_oos": False,
                    "completed_trades": 100,
                    "net_expectancy": 0.001,
                    "profit_factor": 1.1001,
                    "max_drawdown": 0.08,
                    "event_parity": 1.0,
                },
                "paper": {"trading_days": 20, "executable_events": 30},
            },
            "bottom_reversal": {
                "event_ids": ["reversal-001"],
                "oos": {
                    "split": "chronological_holdout",
                    "parameter_tuned_on_oos": False,
                    "completed_trades": 100,
                    "net_expectancy": 0.001,
                    "profit_factor": 1.1001,
                    "max_drawdown": 0.08,
                    "event_parity": 1.0,
                },
                "paper": {"trading_days": 20, "executable_events": 30},
            },
        },
        "safety": {
            "risk_violations": 0,
            "lookahead_events": 0,
            "zero_fill_fake_positions": 0,
            "uncited_executable_reviews": 0,
            "critical_ledger_mismatches": 0,
            "restart_recovery": True,
        },
        "compliance": {
            "broker": {
                "confirmed": True,
                "attestation_id": "broker-attestation-001",
                "evidence_sha256": _digest("broker-evidence"),
            },
            "regulatory": {
                "confirmed": True,
                "attestation_id": "regulatory-attestation-001",
                "evidence_sha256": _digest("regulatory-evidence"),
            },
        },
    }


def _write_fixture(path: Path, payload: dict) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return path


def test_cli_without_fixture_fails_closed_and_records_every_fingerprint(tmp_path: Path) -> None:
    completed, report = _run_cli(tmp_path / "audit")

    assert completed.returncode == 2
    assert report["status"] == "paper_gate_pending"
    assert report["paper_gate_pending"] is True
    assert set(report["fingerprints"]) == {
        "data",
        "algorithm",
        "rule_set",
        "corpus",
        "model",
        "prompt",
    }
    assert len(report["fingerprints"]["algorithm"]) == 64
    assert "missing_fingerprint:data" in report["rejection_codes"]
    assert report["eligible_for_small_cap_manual"] is False
    assert "auto_live" not in json.dumps(report)


def test_cli_passes_only_when_every_research_paper_and_compliance_gate_passes(
    tmp_path: Path,
) -> None:
    fixture = _write_fixture(tmp_path / "fixture.json", _valid_fixture())

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 0
    assert report["status"] == "small_cap_manual_eligible"
    assert report["paper_gate_pending"] is False
    assert report["eligible_for_small_cap_manual"] is True
    assert report["rejection_codes"] == []
    assert all(gate["passed"] for gate in report["gates"].values())
    assert report["track_separation"] == {
        "passed": True,
        "overlap_event_ids": [],
    }
    assert set(report["tracks"]) == {"trend_continuation", "bottom_reversal"}


def test_cli_rejects_unknown_metrics_and_top_level_fields(tmp_path: Path) -> None:
    payload = _valid_fixture()
    payload["execution"] = {"auto_order_enabled": True}
    payload["tracks"]["trend_continuation"]["oos"]["sharpe_ratio"] = 99.0
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["paper_gate_pending"] is True
    assert "unknown_field:execution" in report["rejection_codes"]
    assert (
        "unknown_field:tracks.trend_continuation.oos.sharpe_ratio"
        in report["rejection_codes"]
    )
    assert report["gates"]["input_schema"]["passed"] is False


def test_cli_keeps_track_gates_independent_and_reports_paper_shortfall(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    payload["tracks"]["trend_continuation"]["paper"] = {
        "trading_days": 19,
        "executable_events": 29,
    }
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["tracks"]["trend_continuation"]["paper_gate_passed"] is False
    assert report["tracks"]["bottom_reversal"]["paper_gate_passed"] is True
    assert report["tracks"]["trend_continuation"]["paper"] == {
        "trading_days": 19,
        "executable_events": 29,
        "remaining_trading_days": 1,
        "remaining_executable_events": 1,
    }
    assert "paper_trading_days_below_threshold" in report["rejection_codes"]
    assert "paper_executable_events_below_threshold" in report["rejection_codes"]
    assert report["paper_gate_pending"] is True


def test_cli_rejects_replay_claims_without_executed_prefix_cases(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    del payload["replay"]["prefix_cases"]
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["gates"]["replay"]["passed"] is False
    assert "replay_prefix_cases_failed" in report["rejection_codes"]
    assert report["replay"]["checks"]["prefix_cases"] is False


def test_cli_rejects_non_finite_metrics_but_still_writes_strict_json(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    payload["tracks"]["bottom_reversal"]["oos"]["net_expectancy"] = float("nan")
    fixture = _write_fixture(tmp_path / "fixture.json", payload)
    output_dir = tmp_path / "audit"

    completed, report = _run_cli(
        output_dir, "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert "oos_net_expectancy_failed" in report["rejection_codes"]
    report_text = (output_dir / "validation.json").read_text(encoding="utf-8")
    assert "NaN" not in report_text
    assert "Infinity" not in report_text
    assert report["tracks"]["bottom_reversal"]["oos"]["net_expectancy"] == "non_finite"


def test_cli_turns_malformed_fixture_into_a_fail_closed_audit_record(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "malformed.json"
    fixture.write_text('{"schema_version":', encoding="utf-8")

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert report["status"] == "paper_gate_pending"
    assert "fixture_parse_error" in report["rejection_codes"]
    assert report["gates"]["input_schema"]["passed"] is False


def test_cli_rejects_non_string_algorithm_fingerprint_without_crashing(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    payload["fingerprints"]["algorithm"] = 123
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert "invalid_fingerprint:algorithm" in report["rejection_codes"]
    assert report["fingerprints"]["algorithm"] == hashlib.sha256(CLI.read_bytes()).hexdigest()


def test_cli_rejects_every_oos_boundary_and_reports_trade_shortfall(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    oos = payload["tracks"]["trend_continuation"]["oos"]
    oos.update(
        {
            "parameter_tuned_on_oos": True,
            "completed_trades": 99,
            "net_expectancy": 0.0,
            "profit_factor": 1.1,
            "max_drawdown": 0.0800001,
        }
    )
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["tracks"]["trend_continuation"]["oos_gate_passed"] is False
    assert report["tracks"]["bottom_reversal"]["oos_gate_passed"] is True
    assert report["tracks"]["trend_continuation"]["oos"]["remaining_completed_trades"] == 1
    assert {
        "oos_not_tuned_on_oos_failed",
        "oos_completed_trades_failed",
        "oos_net_expectancy_failed",
        "oos_profit_factor_failed",
        "oos_max_drawdown_failed",
    } <= set(report["rejection_codes"])


def test_cli_rejects_event_identity_shared_across_strategy_tracks(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    payload["tracks"]["bottom_reversal"]["event_ids"] = ["trend-001"]
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["gates"]["track_separation"]["passed"] is False
    assert report["track_separation"]["overlap_event_ids"] == ["trend-001"]
    assert "strategy_track_event_overlap" in report["rejection_codes"]


def test_cli_never_promotes_when_external_compliance_attestation_is_missing(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    del payload["compliance"]["regulatory"]
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["gates"]["compliance"]["passed"] is False
    assert "regulatory_compliance_confirmation_missing" in report["rejection_codes"]
    assert report["status"] == "paper_gate_pending"
    assert report["eligible_for_small_cap_manual"] is False


def test_cli_requires_full_replay_event_parity_for_each_track(
    tmp_path: Path,
) -> None:
    payload = _valid_fixture()
    payload["tracks"]["bottom_reversal"]["oos"]["event_parity"] = 0.999
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["tracks"]["trend_continuation"]["oos_gate_passed"] is True
    assert report["tracks"]["bottom_reversal"]["oos_gate_passed"] is False
    assert "oos_event_parity_failed" in report["rejection_codes"]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("prefix_invariance", False, "replay_prefix_invariance_failed"),
        ("prefix_cases", 0, "replay_prefix_cases_failed"),
        ("no_future_data", False, "replay_no_future_data_failed"),
        (
            "future_perturbation_cases",
            0,
            "replay_future_perturbation_cases_failed",
        ),
        ("incomplete_bar_rejected", False, "replay_incomplete_bar_rejected_failed"),
        ("incomplete_bar_cases", 0, "replay_incomplete_bar_cases_failed"),
        ("event_parity", 0.999, "replay_event_parity_failed"),
    ],
)
def test_cli_fails_each_replay_and_no_future_boundary(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    payload = _valid_fixture()
    payload["replay"][field] = value
    fixture = _write_fixture(tmp_path / "fixture.json", payload)

    completed, report = _run_cli(
        tmp_path / "audit", "--fixture", str(fixture)
    )

    assert completed.returncode == 2
    assert report["gates"]["replay"]["passed"] is False
    assert code in report["rejection_codes"]


def test_cli_is_reproducible_and_never_modifies_the_input_fixture(
    tmp_path: Path,
) -> None:
    fixture = _write_fixture(tmp_path / "fixture.json", _valid_fixture())
    before = hashlib.sha256(fixture.read_bytes()).hexdigest()

    first, first_report = _run_cli(
        tmp_path / "audit-one", "--fixture", str(fixture)
    )
    second, second_report = _run_cli(
        tmp_path / "audit-two", "--fixture", str(fixture)
    )

    assert first.returncode == second.returncode == 0
    assert first_report == second_report
    assert (tmp_path / "audit-one" / "validation.json").read_bytes() == (
        tmp_path / "audit-two" / "validation.json"
    ).read_bytes()
    assert json.loads(first.stdout) == first_report
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == before
    assert first_report["mode"] == "offline_validation"
    assert first_report["no_order_execution"] is True
