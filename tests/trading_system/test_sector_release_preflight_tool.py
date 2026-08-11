from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID,
    QmtSectorSameBaseCoverageEvidence,
)
from tools import preflight_sector_release as subject


CN = ZoneInfo("Asia/Shanghai")
SHA = "sha256:" + "1" * 64


def _candidate(
    *,
    symbol: str,
    decision_at: str,
    accepted: bool,
    representatives: int,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "decision_at": decision_at,
        "accepted": accepted,
        "sector_id": "qmt-gics3:" + "a" * 64,
        "sector_name": "sector",
        "sector_eligible": True,
        "sector_hard_block": False,
        "sector_regime": "neutral",
        "sector_rank_reason_codes": ["structural_ranking_only"],
        "sector_risk_warmup_evidence": {
            "strict_same_5m_source_coverage": {
                "contract_id": (
                    QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID
                ),
                "physical_source_representative_member_count": representatives,
            }
        },
    }


def _coverage() -> dict[str, object]:
    return QmtSectorSameBaseCoverageEvidence(
        observed_at=datetime(2026, 4, 20, 10, 0, tzinfo=CN),
        calendar_first_session=date(2023, 5, 4),
        first_visible_bar_at=datetime(2025, 4, 30, 10, 55, tzinfo=CN),
        last_visible_bar_at=datetime(2026, 4, 20, 10, 0, tzinfo=CN),
        first_completed_session=date(2025, 5, 6),
        last_completed_session=date(2026, 4, 17),
        visible_five_minute_bar_count=11300,
        completed_daily_bar_count=235,
        required_daily_bar_count=480,
        remaining_daily_bar_count=245,
        missing_leading_calendar_session_count=484,
        warmup_converged=False,
        warmup_reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        boundary_status="VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP",
        physical_source_boundary_status=(
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
        ),
        physical_source_requested_start_at=datetime(
            2023, 5, 1, 9, 30, tzinfo=CN
        ),
        physical_source_required_contributor_start_at=datetime(
            2025, 4, 30, 10, 50, tzinfo=CN
        ),
        physical_source_representative_member_count=15,
        physical_source_available_member_count=14,
        physical_source_required_contributor_count=9,
        physical_source_inventory_revision=SHA,
    ).document()


def _raw_physical() -> dict[str, object]:
    stable: dict[str, object] = {
        "boundary_status": (
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
        ),
        "requested_start_at": "2023-05-01T09:30:00+08:00",
        "required_contributor_physical_start_at": "2025-04-30T10:50:00+08:00",
        "representative_member_count": 15,
        "available_member_file_count": 14,
        "required_contributor_count": 9,
        "source_inventory_revision": SHA,
        "diagnostic_only": True,
        "decision_core_input": False,
        "warmup_requirement_unchanged": True,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "audit_sha256": sha256_json(stable)}


def test_json_loader_rejects_duplicate_and_non_finite_values(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        subject._load_json(duplicate)

    non_finite = tmp_path / "nan.json"
    non_finite.write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        subject._load_json(non_finite)


def test_sample_selection_prefers_accepted_smallest_representative_set() -> None:
    artifact = {
        "candidate_audit": [
            _candidate(
                symbol="SH.LARGE",
                decision_at="2026-02-01T10:00:00+08:00",
                accepted=True,
                representatives=24,
            ),
            _candidate(
                symbol="SH.REJECT",
                decision_at="2026-01-01T10:00:00+08:00",
                accepted=False,
                representatives=8,
            ),
            _candidate(
                symbol="SH.SMALL",
                decision_at="2026-03-01T10:00:00+08:00",
                accepted=True,
                representatives=15,
            ),
        ]
    }

    selected = subject._coverage_candidate(
        artifact,
        symbol=None,
        decision_at=None,
    )

    assert selected["symbol"] == "SH.SMALL"


def test_explicit_sample_selector_must_be_unique() -> None:
    artifact = {
        "candidate_audit": [
            _candidate(
                symbol="SH.SAME",
                decision_at="2026-02-01T10:00:00+08:00",
                accepted=True,
                representatives=15,
            ),
            _candidate(
                symbol="SH.SAME",
                decision_at="2026-03-01T10:00:00+08:00",
                accepted=True,
                representatives=15,
            ),
        ]
    }

    with pytest.raises(ValueError, match="ambiguous"):
        subject._coverage_candidate(
            artifact,
            symbol="SH.SAME",
            decision_at=None,
        )


def test_physical_projection_is_exact_and_safety_bound() -> None:
    receipt = subject._validate_physical_projection(
        _raw_physical(),
        _coverage(),
    )

    assert receipt["physical_projection_unchanged"] is True
    assert receipt["physical_source_available_member_count"] == 14
    assert receipt["live_status"] == "LIVE_DISABLED"


def test_recomputed_sector_decision_projection_exposes_match_and_difference() -> None:
    row = _candidate(
        symbol="SH.SAMPLE",
        decision_at="2026-04-20T10:00:00+08:00",
        accepted=True,
        representatives=15,
    )
    same = SimpleNamespace(
        eligible=True,
        hard_block=False,
        regime="neutral",
        reason_codes=("structural_ranking_only",),
    )
    changed = SimpleNamespace(
        eligible=False,
        hard_block=True,
        regime="hostile",
        reason_codes=("sector_data_incomplete",),
    )

    matched = subject._sector_decision_projection(row, same)
    diverged = subject._sector_decision_projection(row, changed)

    assert matched["decision_recomputed"] is True
    assert matched["decision_projection_unchanged"] is True
    assert diverged["decision_projection_unchanged"] is False
    assert diverged["artifact_sector_decision"] != diverged["current_sector_decision"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("available_member_file_count", 13, "diverged"),
        ("decision_core_input", True, "safety role"),
    ),
)
def test_physical_projection_fails_closed_on_divergence_or_role_change(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = _raw_physical()
    raw[field] = value
    stable = dict(raw)
    stable.pop("audit_sha256")
    raw["audit_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match=message):
        subject._validate_physical_projection(raw, _coverage())


def test_artifact_validation_recomputes_both_audits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subject,
        "replay_decision_source_snapshot_matches_current",
        lambda value, root: True,
    )
    monkeypatch.setattr(
        subject,
        "higher_timeframe_effectiveness_audit",
        lambda rows: {"audit_sha256": "risk"},
    )
    monkeypatch.setattr(
        subject,
        "higher_timeframe_execution_attribution",
        lambda rows, replay, terminal: {"audit_sha256": "execution"},
    )
    stable: dict[str, object] = {
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "decision_source_snapshot": {"aggregate_sha256": SHA},
        "candidate_audit": [],
        "higher_timeframe_effectiveness_audit": {"audit_sha256": "risk"},
        "higher_timeframe_execution_attribution": {
            "audit_sha256": "execution"
        },
        "research_variant_result": {
            "replay": {},
            "terminal_accounting_attribution": {},
        },
    }
    artifact = {**stable, "content_sha256": sha256_json(stable)}

    receipt = subject._validate_artifact(
        artifact,
        root=tmp_path,
        allow_stale_source=False,
    )

    assert receipt["decision_source_matches_current"] is True
    assert receipt["risk_audit_sha256"] == "risk"
    assert receipt["execution_audit_sha256"] == "execution"


def test_artifact_validation_rejects_tampering_before_audit_rebuild(
    tmp_path: Path,
) -> None:
    artifact = {
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "content_sha256": SHA,
    }

    with pytest.raises(ValueError, match="content_sha256"):
        subject._validate_artifact(
            artifact,
            root=tmp_path,
            allow_stale_source=True,
        )


def test_main_emits_machine_readable_failed_closed_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = tmp_path / "broken.json"
    artifact.write_text("{}", encoding="utf-8")

    exit_code = subject.main(
        [
            "--root",
            str(tmp_path),
            "--artifact",
            str(artifact),
        ]
    )

    assert exit_code == 2
    receipt = json.loads(capsys.readouterr().err)
    assert receipt["status"] == "FAILED_CLOSED"
    assert receipt["full_replay_executed"] is False
    assert receipt["live_status"] == "LIVE_DISABLED"


def _run_args(
    root: Path,
    artifact: Path,
    *,
    release_manifest: Path | None = None,
    allow_stale_source: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        artifact=artifact,
        pit_snapshot=root / "pit.json",
        catalog_ledger=root / "catalog.json",
        release_manifest=release_manifest,
        qmt_local_data_dir=None,
        sample_symbol=None,
        sample_decision_at=None,
        allow_stale_source=allow_stale_source,
        require_qmt_sample=False,
    )


def test_current_preflight_requires_release_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "result.json"
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subject, "_load_json", lambda path: {})
    monkeypatch.setattr(
        subject,
        "_validate_artifact",
        lambda *args, **kwargs: {"decision_source_matches_current": True},
    )

    with pytest.raises(ValueError, match="requires release_manifest"):
        subject.run(_run_args(tmp_path, artifact))


def test_current_preflight_exposes_verified_release_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "result.json"
    manifest = tmp_path / "release_manifest.json"
    artifact.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subject, "_load_json", lambda path: {})
    monkeypatch.setattr(
        subject,
        "_validate_artifact",
        lambda *args, **kwargs: {"decision_source_matches_current": True},
    )
    monkeypatch.setattr(
        subject,
        "verify_sector_release_manifest",
        lambda **kwargs: {"all_bound_files_verified": True},
    )

    receipt = subject.run(
        _run_args(tmp_path, artifact, release_manifest=manifest)
    )

    assert receipt["status"] == "READY_CURRENT_ARTIFACT_ONLY"
    assert receipt["release_manifest"] == {
        "status": "VERIFIED",
        "receipt": {"all_bound_files_verified": True},
    }
    assert receipt["safety"]["full_replay_executed"] is False


def test_stale_preflight_verifies_old_graph_without_current_algorithm_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "result.json"
    manifest = tmp_path / "release_manifest.json"
    artifact.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subject, "_load_json", lambda path: {})
    monkeypatch.setattr(
        subject,
        "_validate_artifact",
        lambda *args, **kwargs: {"decision_source_matches_current": False},
    )
    captured: dict[str, object] = {}

    def verify(**kwargs):
        captured.update(kwargs)
        return {
            "all_bound_files_verified": True,
            "algorithm_matches_current": False,
        }

    monkeypatch.setattr(subject, "verify_sector_release_manifest", verify)

    receipt = subject.run(
        _run_args(
            tmp_path,
            artifact,
            release_manifest=manifest,
            allow_stale_source=True,
        )
    )

    assert captured["require_current_algorithm"] is False
    assert receipt["status"] == "SOURCE_CHANGED_QMT_SAMPLE_REQUIRED"
    assert receipt["release_manifest"]["status"] == (
        "VERIFIED_STALE_PUBLISHED_GRAPH_FOR_BOUNDED_SAMPLE"
    )


def test_qmt_sample_defaults_to_release_bound_catalog_not_mutable_cache(
    tmp_path: Path,
) -> None:
    frozen = tmp_path / "published/catalog.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b"frozen replay catalog")
    receipt = {
        "bound_files": {
            "current_catalog_ledger": {
                "path": "published/catalog.json",
                "file_sha256": subject._sha256_file(frozen),
            }
        }
    }

    selected = subject._release_input_path(
        root=tmp_path,
        explicit=None,
        release_receipt=receipt,
        key="current_catalog_ledger",
        fallback=Path("mutable/catalog.json"),
    )

    assert selected == frozen.resolve()
    frozen.write_bytes(b"tampered replay catalog")
    with pytest.raises(ValueError, match="changed"):
        subject._release_input_path(
            root=tmp_path,
            explicit=None,
            release_receipt=receipt,
            key="current_catalog_ledger",
            fallback=Path("mutable/catalog.json"),
        )
