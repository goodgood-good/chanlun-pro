from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import csv
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support import reports as reports_module
from chanlun.decision_support.promotion import (
    PromotionDecision,
    PromotionMetrics,
    PromotionState,
)
from chanlun.decision_support.reports import (
    build_rollout_report,
    main,
    write_rollout_bundle,
)


TZ = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 14, 16, 0, tzinfo=TZ)
FP = {
    "data": "sha256:" + "1" * 64,
    "algorithm": "sha256:" + "2" * 64,
    "rule_set": "sha256:" + "3" * 64,
    "corpus_manifest": "sha256:" + "4" * 64,
    "model_version": "sha256:" + "5" * 64,
}
TRACKS = ("trend_continuation", "bottom_reversal")


def _dates(count: int) -> tuple[date, ...]:
    return tuple(date(2026, 6, 1) + timedelta(days=index) for index in range(count))


def _metrics(*, days: int = 20, events: int = 30) -> PromotionMetrics:
    return PromotionMetrics(
        evaluated_at=AS_OF,
        oos_trades=100,
        net_expectancy=0.01,
        profit_factor=1.2,
        max_drawdown=0.08,
        event_parity=1.0,
        risk_violations=0,
        lookahead_events=0,
        zero_fill_fake_positions=0,
        paper_trading_dates=_dates(days),
        exchange_calendar_verified=True,
        exchange_calendar_fingerprint="sha256:" + "6" * 64,
        paper_executable_events=events,
        critical_ledger_mismatches=0,
        uncited_executable_reviews=0,
        restart_recovery=True,
        corpus_manifest_fingerprints=(FP["corpus_manifest"],),
        rule_set_fingerprints=(FP["rule_set"],),
        algorithm_fingerprints=(FP["algorithm"],),
        data_fingerprints=(FP["data"],),
    )


def _decision(
    track: str,
    *,
    metrics: PromotionMetrics | None = None,
    promoted: bool = False,
    reasons: tuple[str, ...] = ("compliance_confirmation_missing",),
) -> PromotionDecision:
    current = metrics or _metrics()
    return PromotionDecision(
        track=track,
        state=(
            PromotionState.SMALL_CAP_MANUAL
            if promoted
            else PromotionState.PAPER
        ),
        promoted=promoted,
        paper_gate_pending=False,
        reasons=reasons,
        metrics=current,
        metrics_fingerprint="sha256:" + ("7" if track == TRACKS[0] else "8") * 64,
    )


def _build(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "generated_at": AS_OF,
        "version_fingerprints": FP,
        "promotion_by_track": {
            track: _decision(track)
            for track in TRACKS
        },
        "attribution_rows": (),
        "review_outcomes_by_track": {
            TRACKS[0]: {
                "confirmed": 4,
                "rejected": 2,
                "abstained": 1,
                "rejection_reasons": {"counter_evidence": 2},
                "abstain_reasons": {"citation_missing": 1},
            },
            TRACKS[1]: {
                "confirmed": 1,
                "rejected": 3,
                "abstained": 2,
                "rejection_reasons": {"risk_gate": 3},
                "abstain_reasons": {"model_schema": 2},
            },
        },
        "corpus_integrity": {
            "status": "complete",
            "manifest_fingerprint": FP["corpus_manifest"],
        },
        "monitoring_active": True,
    }
    values.update(overrides)
    return build_rollout_report(**values)


def test_rollout_report_is_complete_versioned_and_separates_tracks() -> None:
    report = _build()

    required = {
        "corpus_integrity",
        "event_parity",
        "oos_by_track",
        "paper_by_track",
        "risk_violations",
        "review_citations",
        "restart_recovery",
        "promotion",
    }
    assert required <= set(report)
    assert report["version_fingerprints"] == FP
    assert set(report["oos_by_track"]) == set(TRACKS)
    assert set(report["paper_by_track"]) == set(TRACKS)
    assert report["review_outcomes_by_track"][TRACKS[0]]["rejected"] == 2
    assert report["review_outcomes_by_track"][TRACKS[1]]["abstained"] == 2
    assert report["rejection_reasons_by_track"][TRACKS[0]] == {
        "counter_evidence": 2
    }
    assert report["abstain_reasons_by_track"][TRACKS[1]] == {
        "model_schema": 2
    }
    assert set(report["gate_reasons_by_track"]) == set(TRACKS)
    assert report["live_trading_approved"] is False
    assert report["automatic_execution_enabled"] is False
    assert report["status"] != "live_approved"


def test_unknown_metrics_versions_and_missing_track_fail_closed() -> None:
    unknown_metrics = replace(
        _metrics(),
        net_expectancy=None,
        paper_trading_dates=None,
        paper_executable_events=None,
    )
    malicious_decision = _decision(
        TRACKS[0],
        metrics=unknown_metrics,
        promoted=True,
        reasons=(),
    )
    versions = dict(FP)
    versions["model_version"] = None

    report = _build(
        version_fingerprints=versions,
        promotion_by_track={TRACKS[0]: malicious_decision},
    )

    trend_reasons = report["gate_reasons_by_track"][TRACKS[0]]
    reversal_reasons = report["gate_reasons_by_track"][TRACKS[1]]
    assert "net_expectancy_unknown" in trend_reasons
    assert "paper_trading_dates_unknown" in trend_reasons
    assert "paper_executable_events_unknown" in trend_reasons
    assert "model_version_fingerprint_unknown" in trend_reasons
    assert "promotion_decision_missing" in reversal_reasons
    assert report["promotion"][TRACKS[0]]["promoted"] is False
    assert report["promotion"][TRACKS[1]]["promoted"] is False
    assert report["continue_paper"] is True
    assert report["monitoring_active"] is True
    assert report["status"] == "continue_paper"
    assert report["live_trading_approved"] is False
    json.dumps(report, allow_nan=False, sort_keys=True)


def test_insufficient_paper_duration_or_events_keeps_monitoring_active() -> None:
    cases = (
        (19, 30, "insufficient_paper_trading_days"),
        (20, 29, "insufficient_paper_executable_events"),
    )

    for days, events, expected_reason in cases:
        malicious = {
            track: _decision(
                track,
                metrics=_metrics(days=days, events=events),
                promoted=True,
                reasons=(),
            )
            for track in TRACKS
        }
        report = _build(promotion_by_track=malicious)

        assert report["operational_gate"] == {
            "minimum_trading_days": 20,
            "minimum_executable_events": 30,
            "continue_paper": True,
            "monitoring_active": True,
        }
        assert report["continue_paper"] is True
        assert report["monitoring_active"] is True
        assert report["status"] == "continue_paper"
        for track in TRACKS:
            assert expected_reason in report["paper_by_track"][track][
                "gate_reasons"
            ]
            assert report["promotion"][track]["promoted"] is False


def test_rollout_bundle_writes_authoritative_json_csv_and_markdown(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "trade_id": "trade-reversal",
            "strategy_track": TRACKS[1],
            "status": "rejected",
            "rejection_reasons": ["uncited_review"],
            "model_verdict": "ABSTAIN",
            "return_net": "-0.01",
        },
        {
            "trade_id": "trade-trend",
            "strategy_track": TRACKS[0],
            "status": "accepted",
            "rejection_reasons": [],
            "model_verdict": "CONFIRM",
            "return_net": "0.02",
        },
    ]
    report = _build(attribution_rows=rows)

    bundle = write_rollout_bundle(tmp_path / "rollout", report)

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["report_fingerprint"])
    authoritative = json.loads(bundle.report_path.read_text(encoding="utf-8"))
    assert authoritative == report
    with bundle.attribution_path.open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    assert [row["trade_id"] for row in csv_rows] == [
        "trade-trend",
        "trade-reversal",
    ]
    assert csv_rows[1]["model_verdict"] == "ABSTAIN"
    markdown = bundle.markdown_path.read_text(encoding="utf-8")
    assert "不构成实盘交易批准" in markdown
    assert "trend_continuation" in markdown
    assert "bottom_reversal" in markdown
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["report_fingerprint"] == report["report_fingerprint"]
    assert manifest["live_trading_approved"] is False
    assert set(manifest["files"]) == {
        "rollout-report.json",
        "attribution.csv",
        "rollout-report.md",
    }


def test_rollout_bundle_rolls_back_all_files_when_commit_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "rollout"
    first = _build()
    write_rollout_bundle(output_dir, first)
    names = (
        "rollout-report.json",
        "attribution.csv",
        "rollout-report.md",
        "bundle-manifest.json",
    )
    before = {name: (output_dir / name).read_bytes() for name in names}
    second = _build(generated_at=AS_OF + timedelta(minutes=1))
    real_replace = reports_module.os.replace
    staged_replacements = 0

    def fail_second_staged_replace(source: object, destination: object) -> None:
        nonlocal staged_replacements
        if ".rollout-stage-" in str(source):
            staged_replacements += 1
            if staged_replacements == 2:
                raise OSError("simulated interrupted bundle commit")
        real_replace(source, destination)

    monkeypatch.setattr(reports_module.os, "replace", fail_second_staged_replace)

    with pytest.raises(OSError, match="simulated interrupted"):
        write_rollout_bundle(output_dir, second)

    after = {name: (output_dir / name).read_bytes() for name in names}
    assert after == before


def test_reports_cli_rebuilds_a_verified_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _build()
    input_path = tmp_path / "input-report.json"
    input_path.write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    output_dir = tmp_path / "cli-bundle"

    exit_code = main(
        [
            "--input-report",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    assert (output_dir / "rollout-report.json").is_file()
    assert report["report_fingerprint"] in capsys.readouterr().out


def test_report_header_and_track_metric_fingerprints_must_match() -> None:
    mismatched = replace(
        _metrics(),
        algorithm_fingerprints=("sha256:" + "9" * 64,),
        data_fingerprints=("sha256:" + "a" * 64,),
    )
    report = _build(
        promotion_by_track={
            TRACKS[0]: _decision(TRACKS[0], metrics=mismatched),
            TRACKS[1]: _decision(TRACKS[1]),
        }
    )

    reasons = report["gate_reasons_by_track"][TRACKS[0]]
    assert "algorithm_fingerprint_mismatch" in reasons
    assert "data_fingerprint_mismatch" in reasons
    assert report["gate_passed_by_track"][TRACKS[0]] is False
    assert report["live_trading_approved"] is False


def test_unknown_monitor_state_and_cross_track_attribution_fail_closed() -> None:
    report = _build(
        monitoring_active=None,
        attribution_rows=(
            {
                "trade_id": "combined-trade",
                "strategy_track": "combined",
                "status": "accepted",
                "rejection_reasons": [],
                "model_verdict": "CONFIRM",
                "return_net": "0.20",
            },
        ),
    )

    assert report["monitoring_active"] is False
    assert "monitoring_active_unknown" in report["report_input_reasons"]
    assert "attribution_strategy_track_invalid" in report[
        "report_input_reasons"
    ]
    for track in TRACKS:
        assert "monitoring_active_unknown" in report[
            "gate_reasons_by_track"
        ][track]
        assert "attribution_strategy_track_invalid" in report[
            "gate_reasons_by_track"
        ][track]
        assert report["gate_passed_by_track"][track] is False
    assert report["status"] == "validation_blocked"
    assert report["live_trading_approved"] is False


def test_empty_attribution_csv_keeps_a_stable_auditable_schema(
    tmp_path: Path,
) -> None:
    bundle = write_rollout_bundle(tmp_path / "rollout", _build())

    header = bundle.attribution_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == [
        "trade_id",
        "strategy_track",
        "status",
        "rejection_reasons",
        "model_verdict",
        "return_net",
    ]


def test_each_track_exposes_full_oos_and_paper_evidence_metrics() -> None:
    report = _build()

    for track in TRACKS:
        assert {
            "completed_trades",
            "net_expectancy",
            "profit_factor",
            "max_drawdown",
            "event_parity",
            "risk_violations",
            "lookahead_events",
            "zero_fill_fake_positions",
        } <= set(report["oos_by_track"][track])
        assert {
            "trading_dates",
            "trading_day_count",
            "exchange_calendar_verified",
            "exchange_calendar_fingerprint",
            "executable_events",
            "critical_ledger_mismatches",
            "uncited_executable_reviews",
            "restart_recovery",
            "gate_reasons",
        } <= set(report["paper_by_track"][track])


def test_paper_section_preserves_every_paper_gate_reason() -> None:
    failing = replace(
        _metrics(),
        exchange_calendar_verified=False,
        critical_ledger_mismatches=1,
        uncited_executable_reviews=1,
        restart_recovery=False,
    )
    report = _build(
        promotion_by_track={
            track: _decision(track, metrics=failing)
            for track in TRACKS
        }
    )

    expected = {
        "paper_trading_dates_unverified",
        "critical_ledger_mismatch",
        "uncited_executable_review",
        "restart_recovery_failed",
    }
    for track in TRACKS:
        assert expected <= set(report["paper_by_track"][track]["gate_reasons"])
