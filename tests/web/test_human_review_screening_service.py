from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from threading import Event
import time as time_module
from zoneinfo import ZoneInfo

import pytest

import cl_app.services.human_review_screening as human_review_screening_subject
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.human_paper_accounting import (
    load_human_paper_accounting_parameters,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    HumanPaperIntent,
    load_human_paper_ledger,
    parse_human_paper_entry_selection_evidence,
)
from chanlun.decision_support.trading_system.live_review_materialization import (
    live_review_materialization_receipt,
    live_review_web_bundle_receipt,
)
from chanlun.decision_support.trading_system.human_paper_valuation import (
    build_human_paper_valuation_document,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeSessionEvidence,
    QmtSectorSameBaseCoverageEvidence,
)
from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (
    build_candidate_warmup_diagnostic_document,
    candidate_warmup_diagnostic_path,
    candidate_warmup_parameter_document,
)
from chanlun.decision_support.trading_system.models import EntryExecutionBoundary
from chanlun.decision_support.trading_system.human_review_screening import (
    HumanReviewAlert,
    HumanReviewFeedback,
    HigherTimeframeReviewSourceSupport,
    HigherTimeframeReviewSideEvidence,
    MarketSymbolHigherTimeframeReviewEvidence,
    SectorHigherTimeframeReviewEvidence,
    SectorRankingReviewEvidence,
    append_human_review_feedback,
    human_review_alert_document,
    human_review_screening_parameters,
    load_human_review_feedback_ledger,
)
from chanlun.decision_support.trading_system.forward_paper import (
    append_forward_paper_event,
    load_forward_contract,
)
from chanlun.decision_support.trading_system.forward_review_markout import (
    FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
    build_forward_review_markout,
)
from chanlun.decision_support.trading_system.forward_warmup_structure_lineage import (
    build_forward_warmup_structure_lineage_rollup,
)
from chanlun.decision_support.trading_system.live_human_review import (
    live_screening_snapshot_content_sha256,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    append_sector_catalog,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupPrefixObservation,
    classify_warmup_convergence_envelope,
)
from tests.trading_system.test_live_human_review import live_snapshot
from cl_app.services.human_review_screening import (
    HumanReviewScreenUnavailable,
    HumanReviewScreeningService,
    _review_lane,
)
import tools.validate_trading_screening_review as review_validator_subject


TZ = ZoneInfo("Asia/Shanghai")
SECTOR_ID = "qmt-gics3:" + "a" * 64
SYMBOL = "SZ.000001"
PARAMETER_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "forward_paper"
    / "parameter_snapshot_human_review.json"
)


def _implementation_provenance(source_digit: str = "a") -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": "chanlun-forward-implementation-provenance",
        "application_source_revision": (
            source_digit * 40 + ".tree." + source_digit * 24
        ),
        "forward_scheduler_module_sha256": "sha256:" + "1" * 64,
        "forward_python_tool_sha256": "sha256:" + "2" * 64,
        "sector_capture_tool_sha256": "sha256:" + "3" * 64,
        "python_implementation": "CPython",
        "python_version": "3.11.0",
        "pandas_version": "2.0.0",
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _trading_session_provider(*, session, observed_at):
    return build_trading_session_evidence(
        session=session,
        observed_at=observed_at,
        returned_sessions=() if session.weekday() >= 5 else (session,),
        published_through=None if session.weekday() >= 5 else session,
        query_attempted=session.weekday() < 5,
        query_succeeded=session.weekday() < 5,
    )


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _alert() -> HumanReviewAlert:
    signal_at = datetime(2026, 7, 20, 10, 4, tzinfo=TZ)
    return HumanReviewAlert(
        symbol=SYMBOL,
        alert_type="POSSIBLE_30M_BUY",
        signal_at=signal_at,
        review_available_at=signal_at + timedelta(minutes=26),
        source_point_id="sha256:" + "1" * 64,
        structure_snapshot_id="sha256:" + "2" * 64,
        sector_id=SECTOR_ID,
        confidence="MEDIUM",
        review_priority=63,
        reference_price=Decimal("12.34"),
        structural_invalidation_price=Decimal("11.80"),
        market_risk_gate="AMBER",
        sector_risk_gate="UNRESOLVED",
        symbol_risk_gate="GREEN",
        warning_codes=("UNRESOLVED_CENTER_DECOMPOSITION",),
        source_fact_ids=("sha256:" + "3" * 64,),
        screening_parameter_set_id=human_review_screening_parameters().parameter_set_id,
        signal_alignment_parameter_set_id=(
            human_review_screening_parameters().signal_alignment_parameter_set_id
        ),
        entry_confirmation_bar_closed_at=signal_at,
        entry_price_cap=Decimal("1000"),
        # This helper feeds historical-service tests whose review timestamps
        # run through 2026-07-28.  Production boundaries are created by the
        # gateway and remain one completed locator bar only.
        entry_valid_until=signal_at + timedelta(days=10),
        entry_boundary_evidence_id="sha256:" + "4" * 64,
    )


def _report(
    *,
    forward_session: str | None = None,
    alert: HumanReviewAlert | None = None,
) -> dict[str, object]:
    alert = _alert() if alert is None else alert
    stable: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "forward_paper_session": forward_session,
        "sample": {
            "effective_start": "2025-08-01",
            "requested_end": "2026-07-24",
            "forward_session": forward_session,
            "minimum_bar_period": "1m",
        },
        "scope": {"selection_order": ["QMT_CURRENT_SECTOR_TRIGGER"]},
        "candidate_funnel": {"all_review_alert_count": 1},
        "signal_counts": {"candidate_accepted": 1},
        "event_study": {"summary": {"5": {"eligible_count": 0}}},
        "review_queue": [
            {
                **_jsonable(human_review_alert_document(alert)),
                "candidate_id": alert.candidate_id,
                "signal_lifecycle_id": alert.signal_lifecycle_id,
            }
        ],
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _write_report(
    path: Path,
    *,
    forward_session: str | None = None,
    alert: HumanReviewAlert | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _report(forward_session=forward_session, alert=alert),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_sector_ledger(path: Path) -> None:
    sectors = (
        {
            "sector_id": SECTOR_ID,
            "name": "银行",
            "source_key": "GICS3银行",
            "member_codes": (SYMBOL,),
        },
    )
    append_sector_catalog(
        path,
        {
            "source": "qmt_gics3_components",
            "captured_at": "2026-07-28T09:15:00+08:00",
            "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
            "catalog_revision": sha256_json(
                {"schema": "chanlun-qmt-gics3-catalog", "sectors": sectors}
            ),
            "sectors": sectors,
        },
    )


def _live_ranked_alert(
    *,
    catalog_revision: str,
    alert_type: str = "POSSIBLE_30M_BUY",
) -> HumanReviewAlert:
    signal_at = datetime(2026, 7, 28, 9, 31, tzinfo=TZ)
    base = _alert()
    boundary = EntryExecutionBoundary(
        symbol=base.symbol,
        point_id=base.source_point_id,
        source_frequency="1m",
        confirmation_bar_closed_at=signal_at,
        raw_open=Decimal("12.30"),
        raw_high=Decimal("12.40"),
        raw_low=Decimal("12.20"),
        raw_close=Decimal("12.35"),
        raw_volume=Decimal("10000"),
        entry_valid_until=signal_at + timedelta(minutes=1),
        raw_price_basis_revision="qmt-none-test",
    )
    ranking = SectorRankingReviewEvidence(
        sector_id=SECTOR_ID,
        sector_name="银行",
        observed_at=signal_at,
        eligible=True,
        hard_block=False,
        regime="supportive",
        ordinal=1,
        rank_score=45,
        rank_components=(("neutral_access", 5), ("thirty_support", 40)),
        reason_codes=("structural_ranking_only",),
        horizontal_strength=Decimal("7.5"),
        horizontal_rank=1,
        strength_observed_at=signal_at,
        strength_anchor_session=signal_at.date() - timedelta(days=1),
        strength_member_count=1,
        strength_source_revision="sha256:" + "7" * 64,
        strength_evidence_revision="sha256:" + "8" * 64,
        sector_catalog_revision=catalog_revision,
    )
    return replace(
        base,
        alert_type=alert_type,
        signal_at=signal_at,
        review_available_at=signal_at,
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        entry_confirmation_bar_closed_at=boundary.confirmation_bar_closed_at,
        entry_price_cap=boundary.raw_high,
        entry_valid_until=boundary.entry_valid_until,
        entry_boundary_evidence_id=boundary.evidence_id,
        entry_execution_boundary=boundary,
        sector_ranking_evidence=ranking,
        source_fact_ids=(
            *base.source_fact_ids,
            ranking.evidence_id,
            boundary.evidence_id,
        ),
    )


def _forward_ranked_service(
    root: Path,
    *,
    ranking_revision: str | None = None,
    alert_type: str = "POSSIBLE_30M_BUY",
) -> HumanReviewScreeningService:
    historical = root / "historical.json"
    sector_ledger = root / "sectors.json"
    _write_report(historical)
    _write_sector_ledger(sector_ledger)
    catalog = json.loads(sector_ledger.read_text(encoding="utf-8"))["entries"][0]
    revision = str(catalog["catalog_revision"])
    alert = _live_ranked_alert(
        catalog_revision=revision if ranking_revision is None else ranking_revision,
        alert_type=alert_type,
    )
    forward = (
        root
        / "forward"
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    _write_report(forward, forward_session="2026-07-28", alert=alert)
    return HumanReviewScreeningService(
        repository_root=root,
        historical_report=historical,
        forward_root=root / "forward",
        feedback_ledger=root / "feedback.json",
        sector_ledger=sector_ledger,
        paper_ledger=root / "paper.json",
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )


def _forward_scheduler_observation(
    *,
    ready: bool,
    observed_at: str = "2026-07-28T09:05:00+08:00",
) -> dict[str, object]:
    reasons = [] if ready else ["SCHEDULED_TASK_PRINCIPAL_MISMATCH"]
    tasks = [
        {
            "name": name,
            "phase": phase,
            "ready": ready,
            "configuration_ready": ready,
            "operationally_verified": ready,
            "operational_status": "verified" if ready else "not_verified",
            "status": "ready" if ready else "not_ready",
            "reason_codes": list(reasons),
            "operational_reason_codes": list(reasons),
        }
        for name, phase in (
            ("chanlun-app-forward-capture", "CAPTURE"),
            ("chanlun-app-forward-evaluate", "EVALUATE"),
        )
    ]
    return {
        "schema": "chanlun-forward-scheduler-readiness",
        "contract_id": ("chanlun-forward-scheduler/app-runtime-contract"),
        "execution_owner": "APP_RUNTIME",
        "observed_at": observed_at,
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "reason_code": "READY" if ready else reasons[0],
        "reason_codes": reasons,
        "configuration_ready": ready,
        "operationally_verified": ready,
        "operational_status": "verified" if ready else "not_verified",
        "operational_reason_codes": list(reasons),
        "first_success_after_registration": ready,
        "registered_at": "2026-07-28T08:55:00+08:00",
        "pinned_python_executable": "D:\\software\\Python310\\python.exe",
        "upstream_qmt": {
            "schema": "chanlun-qmt-runtime-readiness",
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "reason_code": "READY" if ready else reasons[0],
            "reason_codes": list(reasons),
            "configuration_ready": ready,
            "operationally_verified": ready,
            "operational_status": "verified" if ready else "not_verified",
            "operational_reason_codes": list(reasons),
            "upstream_ready_now": ready,
            "upstream_reason_code": "READY" if ready else reasons[0],
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        },
        "tasks": tasks,
        "task_count": len(tasks),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


@pytest.fixture
def service(tmp_path: Path) -> HumanReviewScreeningService:
    historical = tmp_path / "historical.json"
    sector_ledger = tmp_path / "sectors.json"
    _write_report(historical)
    _write_sector_ledger(sector_ledger)
    return HumanReviewScreeningService(
        repository_root=tmp_path,
        historical_report=historical,
        forward_root=tmp_path / "forward",
        feedback_ledger=tmp_path / "feedback.json",
        sector_ledger=sector_ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        trading_session_provider=_trading_session_provider,
    )


def test_immutable_report_validation_is_cached_by_exact_file_identity(
    service: HumanReviewScreeningService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = human_review_screening_subject._validate_report
    calls = 0

    def counted(payload):
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(
        human_review_screening_subject,
        "_validate_report",
        counted,
    )
    first = service._load_path("historical", service.historical_report)
    second = service._load_path("historical", service.historical_report)

    assert second is first
    assert calls == 1

    # Even semantically harmless byte changes invalidate the exact-stat cache;
    # the new immutable file must pass the common boundary again.
    service.historical_report.write_text(
        service.historical_report.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    service._load_path("historical", service.historical_report)
    assert calls == 2


def test_compact_live_bundle_avoids_full_report_walk_and_loads_one_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "screening.json"
    source.write_text("{}", encoding="utf-8")
    source_hash = "sha256:" + "9" * 64
    sector_ledger = tmp_path / "sectors.json"
    _write_sector_ledger(sector_ledger)
    catalog = json.loads(sector_ledger.read_text(encoding="utf-8"))["entries"][0]
    alert = _live_ranked_alert(catalog_revision=str(catalog["catalog_revision"]))
    original = _report(forward_session="2026-07-28", alert=alert)
    stable = {key: value for key, value in original.items() if key != "content_sha256"}
    stable["sample"] = {
        **dict(stable["sample"]),
        "market_data_as_of": "2026-07-28T15:00:00+08:00",
    }
    decision_source_id = (
        human_review_screening_subject._WEB_PROCESS_DECISION_SOURCE_SNAPSHOT_ID
    )
    stable["input_hashes"] = {
        "decision_source_snapshot_id": decision_source_id,
    }
    report = {**stable, "content_sha256": sha256_json(stable)}
    archive = tmp_path / "live_archive"
    report_path = (
        archive
        / "2026-07-28"
        / f"{str(report['content_sha256']).removeprefix('sha256:')}.json"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    index_path, detail_path, index = review_validator_subject._materialize_web_bundle(
        report=report,
        archive_root=archive,
        source_snapshot_content_sha256=source_hash,
        decision_source_snapshot_id=decision_source_id,
    )
    receipt = live_review_web_bundle_receipt(
        source_path=source,
        source_stat=source.stat(),
        source_snapshot_content_sha256=source_hash,
        report_path=report_path,
        report_stat=report_path.stat(),
        report_content_sha256=str(report["content_sha256"]),
        index_path=index_path,
        index_stat=index_path.stat(),
        index_content_sha256=str(index["content_sha256"]),
        detail_path=detail_path,
        detail_stat=detail_path.stat(),
        decision_source_snapshot_id=decision_source_id,
        archive_root=archive,
    )
    (archive / ".current_live_review_web_bundle.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    historical = tmp_path / "historical.json"
    _write_report(historical)
    service = HumanReviewScreeningService(
        repository_root=tmp_path,
        historical_report=historical,
        forward_root=tmp_path / "forward",
        feedback_ledger=tmp_path / "feedback.json",
        sector_ledger=sector_ledger,
        live_screening_snapshot=source,
        live_archive_root=archive,
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )
    monkeypatch.setattr(
        human_review_screening_subject,
        "_validate_report",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("compact page must not deep-walk the full report")
        ),
    )

    snapshot = service.snapshot(source="live", include_evidence=False)

    candidate = snapshot["review_queue"][0]
    assert snapshot["review_presentation_contract"] == {
        "default_focus_lanes": [
            "POSITION_MANAGEMENT",
            "ACTIONABLE_REVIEW",
        ],
        "initial_candidate_payload": "COMPACT_SUMMARY",
        "candidate_evidence_loaded_on_demand": True,
        "sector_horizontal_rank_used_for_display_order": True,
        "source_review_priority_unchanged": True,
        "candidate_identity_unchanged": True,
        "trade_authorization_changed": False,
        "live_status": "LIVE_DISABLED",
    }
    assert candidate["candidate_id"] == alert.candidate_id
    assert candidate["sector_ranking_evidence"] is None
    assert candidate["sector_ranking_evidence_id"] == (
        alert.sector_ranking_evidence.evidence_id
    )
    assert candidate["evidence_detail_available"] is True
    assert "detail_locator" not in candidate

    # A code revision makes this report historical, but must not force the
    # read-only page to deep-parse the 80+ MiB archive.  The compact bundle is
    # independently bound to its producing decision-source identity and every
    # immutable file hash; only the *current-source* readiness path requires
    # equality with the running code revision.
    monkeypatch.setattr(
        human_review_screening_subject,
        "_WEB_PROCESS_DECISION_SOURCE_SNAPSHOT_ID",
        "sha256:" + "8" * 64,
    )
    monkeypatch.setattr(
        human_review_screening_subject,
        "_MAX_SYNCHRONOUS_LIVE_SNAPSHOT_BYTES",
        1,
    )
    archived = service.snapshot(source="latest", include_evidence=False)
    assert archived["review_queue"][0]["candidate_id"] == alert.candidate_id
    assert archived["live_status"] == "LIVE_DISABLED"

    detail = service.candidate_detail(
        candidate_id=alert.candidate_id,
        source_sha256=str(report["content_sha256"]),
    )
    assert detail["candidate_id"] == alert.candidate_id
    assert detail["sector_ranking_evidence"]["evidence_id"] == (
        alert.sector_ranking_evidence.evidence_id
    )
    assert detail["automated_order_authorized"] is False
    assert detail["live_status"] == "LIVE_DISABLED"


def test_virtual_holding_codes_are_rebuilt_from_verified_paper_fills(
    service: HumanReviewScreeningService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live scanner must keep every non-zero virtual holding monitored."""

    monkeypatch.setattr(
        service,
        "_paper_events",
        lambda: (
            {
                "kind": "FILL",
                "payload": {"symbol": "SZ.000001", "side": "BUY", "quantity": 100},
            },
            {
                "kind": "FILL",
                "payload": {"symbol": "SH.600000", "side": "BUY", "quantity": 100},
            },
            {
                "kind": "FILL",
                "payload": {"symbol": "SZ.000001", "side": "SELL", "quantity": 100},
            },
        ),
    )

    assert service.virtual_holding_codes() == ("SH.600000",)


def test_snapshot_exposes_hash_bound_sector_ranking_without_inference(
    service: HumanReviewScreeningService,
) -> None:
    base = _alert()
    evidence = SectorRankingReviewEvidence(
        sector_id=SECTOR_ID,
        sector_name="银行",
        observed_at=base.signal_at,
        eligible=True,
        hard_block=False,
        regime="supportive",
        ordinal=2,
        rank_score=45,
        rank_components=(("neutral_access", 5), ("thirty_support", 40)),
        reason_codes=("structural_ranking_only",),
        horizontal_strength=Decimal("7.5"),
        horizontal_rank=1,
        strength_observed_at=base.signal_at - timedelta(minutes=4),
        strength_anchor_session=base.signal_at.date() - timedelta(days=1),
        strength_member_count=42,
        strength_source_revision="sha256:" + "7" * 64,
        strength_evidence_revision="sha256:" + "8" * 64,
        sector_catalog_revision="sha256:" + "9" * 64,
    )
    alert = replace(
        base,
        sector_ranking_evidence=evidence,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
    )
    _write_report(service.historical_report, alert=alert)

    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_ranking_evidence"] == evidence.document()
    assert candidate["sector_ranking_attestation"] == "FULL_STRUCTURAL_COMPONENTS"
    assert evidence.evidence_id in candidate["source_fact_ids"]
    assert candidate["review_lane"] == "ACTIONABLE_REVIEW"
    assert candidate["sector_horizontal_rank"] == 1
    assert candidate["sector_horizontal_strength"] == "7.5"


def test_review_lanes_are_display_only_and_put_open_position_risk_first() -> None:
    buy = _alert()
    assert (
        _review_lane(
            buy,
            virtual_position_quantity=0,
            paper_reconciliation_pending=False,
        )
        == "ACTIONABLE_REVIEW"
    )
    assert (
        _review_lane(
            replace(buy, confidence="LOW"),
            virtual_position_quantity=0,
            paper_reconciliation_pending=False,
        )
        == "WATCHLIST"
    )
    exit_alert = replace(buy, alert_type="POSSIBLE_30M_EXIT")
    assert (
        _review_lane(
            exit_alert,
            virtual_position_quantity=100,
            paper_reconciliation_pending=False,
        )
        == "POSITION_MANAGEMENT"
    )
    assert (
        _review_lane(
            exit_alert,
            virtual_position_quantity=0,
            paper_reconciliation_pending=False,
        )
        == "RESEARCH_ARCHIVE"
    )


def test_entry_boundary_source_audit_rejects_an_internally_rehashed_intent(
    service: HumanReviewScreeningService,
) -> None:
    confirmed_at = datetime(2026, 7, 20, 10, 4, tzinfo=TZ)
    boundary = EntryExecutionBoundary(
        symbol=SYMBOL,
        point_id="sha256:" + "9" * 64,
        source_frequency="1m",
        confirmation_bar_closed_at=confirmed_at,
        raw_open=Decimal("10.00"),
        raw_high=Decimal("10.05"),
        raw_low=Decimal("9.98"),
        raw_close=Decimal("10.03"),
        raw_volume=Decimal("10000"),
        entry_valid_until=confirmed_at + timedelta(minutes=1),
        raw_price_basis_revision="qmt-none-test",
    )
    base = _alert()
    alert = replace(
        base,
        signal_at=confirmed_at,
        source_fact_ids=(*base.source_fact_ids, boundary.evidence_id),
        entry_confirmation_bar_closed_at=boundary.confirmation_bar_closed_at,
        entry_price_cap=boundary.raw_high,
        entry_valid_until=boundary.entry_valid_until,
        entry_boundary_evidence_id=boundary.evidence_id,
        entry_execution_boundary=boundary,
    )
    source_hash = "sha256:" + "5" * 64
    intent = HumanPaperIntent(
        feedback_id="sha256:" + "6" * 64,
        candidate_id=alert.candidate_id,
        source_screen_content_sha256=source_hash,
        symbol=SYMBOL,
        side="BUY",
        created_at=confirmed_at,
        earliest_fill_at=confirmed_at,
        quantity=100,
        reference_price=alert.reference_price,
        structural_invalidation_price=alert.structural_invalidation_price,
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
        status="PENDING",
        reason_codes=("HUMAN_CONFIRMED_PAPER_OBSERVE",),
        signal_lifecycle_id=alert.signal_lifecycle_id,
        entry_confirmation_bar_closed_at=boundary.confirmation_bar_closed_at,
        entry_price_cap=boundary.raw_high,
        entry_valid_until=boundary.entry_valid_until,
        entry_boundary_evidence_id=boundary.evidence_id,
        entry_execution_boundary=boundary,
    )
    events = ({"kind": "INTENT", "payload": intent.document()},)

    complete = service._paper_entry_boundary_source_audit(
        events,
        current_source_sha256=source_hash,
        current_alerts=(alert,),
    )
    assert complete["status"] == "COMPLETE"
    assert complete["verified_source_binding_count"] == 1

    forged_boundary = replace(boundary, raw_high=Decimal("10.06"))
    forged_intent = replace(
        intent,
        entry_price_cap=forged_boundary.raw_high,
        entry_boundary_evidence_id=forged_boundary.evidence_id,
        entry_execution_boundary=forged_boundary,
    )
    forged = service._paper_entry_boundary_source_audit(
        ({"kind": "INTENT", "payload": forged_intent.document()},),
        current_source_sha256=source_hash,
        current_alerts=(alert,),
    )
    assert forged["status"] == "INVALID"
    assert forged["invalid_source_bindings"] == [
        {
            "intent_id": forged_intent.intent_id,
            "reason": "LEDGER_BOUNDARY_DIFFERS_FROM_SOURCE_ALERT",
        }
    ]


def test_snapshot_is_sector_first_review_only_and_builds_causal_chart_urls(
    service: HumanReviewScreeningService,
) -> None:
    snapshot = service.snapshot(source="latest")
    accounting_contract_id = load_human_paper_accounting_parameters(
        PARAMETER_SNAPSHOT
    ).accounting_contract_id

    assert snapshot["source_kind"] == "historical"
    assert snapshot["paper_observation_eligible"] is False
    assert snapshot["paper_observation_reason"] == "HISTORICAL_SOURCE_REVIEW_ONLY"
    assert snapshot["review_queue_count"] == 1
    assert snapshot["review_lane_counts"] == {"ACTIONABLE_REVIEW": 1}
    assert snapshot["review_presentation_contract"] == {
        "default_focus_lanes": [
            "POSITION_MANAGEMENT",
            "ACTIONABLE_REVIEW",
        ],
        "sector_horizontal_rank_used_for_display_order": True,
        "source_review_priority_unchanged": True,
        "candidate_identity_unchanged": True,
        "trade_authorization_changed": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["source_currentness"]["status"] == ("CURRENT_RELEASE_SIDECAR")
    assert snapshot["orders_created"] == snapshot["fills_created"] == 0
    assert snapshot["automated_order_authorized"] is False
    assert snapshot["highest_status"] == "REVIEW_REQUIRED"
    assert snapshot["portfolio_backtest_performed"] is False
    assert snapshot["portfolio_performance_evaluable"] is False
    assert snapshot["forward_markout"]["status"] == "NOT_AVAILABLE"
    assert snapshot["forward_markout"]["portfolio_performance_evaluable"] is False
    assert snapshot["forward_markout"]["source_provenance_status"] == "UNAVAILABLE"
    assert snapshot["forward_warmup_structure_lineage"]["status"] == ("NOT_AVAILABLE")
    assert snapshot["forward_warmup_structure_lineage"]["diagnostic_only"] is True
    assert snapshot["sector_capture_receipts"]["status"] == "REQUIRED_RECEIPT_GAPS"
    assert snapshot["sector_capture_receipts"]["entry_count"] == 1
    assert snapshot["sector_capture_receipts"]["valid_receipt_count"] == 0
    assert (
        snapshot["sector_capture_receipts"]["historical_receipts_synthesized"] is False
    )
    assert "QMT_SECTOR_RECEIPTS_REQUIRED_RECEIPT_GAPS" in snapshot["warnings"]
    accounting = snapshot["paper_accounting"]
    assert accounting["status"] == "NO_FILLS"
    assert accounting["accounting_valid"] is True
    assert accounting["performance_evaluable"] is False
    assert accounting["fee_model_attached"] is True
    assert accounting["fee_schedule_id"] == "A_SHARE_RESEARCH_2025"
    assert accounting["cash_ledger_attached"] is True
    assert accounting["cash_ledger_complete"] is True
    assert accounting["cash_balance"] == "1000000.00"
    assert accounting["total_fees"] == "0.00"
    assert accounting["equity_curve_available"] is False
    assert accounting["content_sha256"].startswith("sha256:")
    assert "NO_VIRTUAL_FILL_SAMPLE" in accounting["reason_codes"]
    valuation = snapshot["paper_valuation"]
    assert valuation["status"] == "NOT_STARTED"
    assert valuation["valuation_count"] == 0
    assert valuation["equity_curve_available"] is False
    assert valuation["performance_evaluable"] is False
    assert valuation["source_provenance_available"] is True
    assert valuation["source_provenance_verified"] is False
    assert snapshot["virtual_pending_intent_count"] == 0
    assert snapshot["virtual_cancelled_intent_count"] == 0
    assert snapshot["virtual_operations_cancelled_intent_count"] == 0
    assert snapshot["paper_pending_continuity"] == {
        "status": "NO_PENDING_INTENTS",
        "pending_intent_count": 0,
        "pending_intent_ids": [],
        "gap_intent_count": 0,
        "gap_intent_ids": [],
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["virtual_reserved_sell_quantity"] == 0
    assert snapshot["virtual_reserved_sell_quantities"] == {}
    assert snapshot["paper_execution_evidence"] == {
        "status": "NO_FILLS",
        "fill_count": 0,
        "verified_fill_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_execution_rejection_evidence"] == {
        "schema": "chanlun-human-paper-execution-rejection-evidence-audit",
        "status": "NO_REJECTIONS",
        "rejection_count": 0,
        "verified_rejection_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "first_eligible_bar_verified": True,
        "price_cap_and_ttl_verified": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_operations_cancellation_evidence"] == {
        "schema": ("chanlun-human-paper-operations-cancellation-evidence-audit"),
        "status": "NO_CANCELLATIONS",
        "cancellation_count": 0,
        "verified_cancellation_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "data_fault_cancellation_count": 0,
        "execution_fact_incomplete_cancellation_count": 0,
        "execution_fact_incomplete_reason_counts": {
            "SECURITY_STATUS_INCOMPLETE": 0,
            "CORPORATE_ACTION_RECONCILIATION_REQUIRED": 0,
        },
        "security_gate_cancellation_count": 0,
        "security_gate_reason_counts": {
            "SUSPENDED": 0,
            "EXPIRED": 0,
            "ST_BUY_PROHIBITED": 0,
        },
        "optional_buy_operations_cancellation_verified": True,
        "optional_buy_data_fault_cancellation_verified": True,
        "optional_buy_security_gate_cancellation_verified": True,
        "persistent_exit_untouched": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_portfolio_rejection_evidence"] == {
        "schema": "chanlun-human-paper-portfolio-rejection-evidence-audit",
        "status": "NO_REJECTIONS",
        "rejection_count": 0,
        "verified_rejection_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "first_eligible_bar_verified": True,
        "synchronous_position_marks_verified": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_portfolio_decision_audit"] == {
        "schema": "chanlun-human-paper-portfolio-decision-audit",
        "status": "NO_REJECTIONS",
        "rejection_count": 0,
        "verified_rejection_count": 0,
        "invalid_decisions": [],
        "accounting_contract_id": accounting_contract_id,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_portfolio_fill_decision_audit"] == {
        "schema": "chanlun-human-paper-portfolio-fill-decision-audit",
        "status": "NO_APPROVED_FILLS",
        "approved_fill_count": 0,
        "verified_approved_fill_count": 0,
        "invalid_decisions": [],
        "accounting_contract_id": accounting_contract_id,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "synchronous_open_position_one_minute_marks_required": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_entry_selection_attestation"] == {
        "schema": ("chanlun-human-paper-entry-selection-attestation-audit"),
        "status": "NO_SELECTION_ATTESTATIONS",
        "attested_buy_intent_count": 0,
        "verified_catalog_binding_count": 0,
        "verified_buy_intent_ids": [],
        "catalog_unavailable_intent_ids": [],
        "invalid_attestations": [],
        "selection_evidence_ids": [],
        "catalog_entry_sha256s": [],
        "exact_qmt_revision_name_and_membership_verified": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_entry_selection_source_audit"] == {
        "schema": "chanlun-human-paper-entry-selection-source-audit",
        "status": "NO_REQUIRED_SELECTION_INTENTS",
        "required_live_ranked_buy_intent_count": 0,
        "verified_source_binding_count": 0,
        "verified_required_buy_intent_ids": [],
        "source_unavailable_intent_ids": [],
        "invalid_source_bindings": [],
        "immutable_source_ranking_resolved": True,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert snapshot["paper_execution_capabilities"] == {
        "fill_source": "ADVERSE_OBSERVED_BAR_EXTREME_WITHIN_LIMIT",
        "fill_timestamp_rule": "COMPLETED_BAR_CLOSE",
        "tick_data_used": False,
        "t_plus_one_sell_enforced": True,
        "pending_sell_quantity_reserved": True,
        "prior_session_continuity_enforced": True,
        "pending_intent_cancellation_supported": True,
        "pending_intent_expiry_supported": True,
        "optional_buy_intent_expiry_supported": True,
        "persistent_strategic_sell_never_expires": True,
        "later_feedback_mutates_existing_intent": False,
        "later_feedback_supersedes_pending_intent": True,
        "fee_model_attached": True,
        "cash_accounting_attached": True,
        "cash_and_slot_pretrade_enforced": True,
        "portfolio_rejection_exact_1m_evidence_audited": True,
        "portfolio_rejection_ledger_prefix_recomputed": True,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "synchronous_open_position_one_minute_marks_required": True,
        "unresolved_position_marks_block_new_buys": True,
        "portfolio_approved_fill_ledger_prefix_recomputed": True,
        "one_security_one_strategic_slot_enforced": True,
        "terminal_signal_lifecycle_one_shot_enforced": True,
        "fixed_one_lot_tactical_review_only": True,
        "fixed_one_lot_diagnostic": True,
        "human_trend_type_confirmation_required": True,
        "warmup_divergence_blocks_strategic_virtual_buy": True,
        "warmup_divergence_never_blocks_existing_virtual_exit": True,
        "strategic_buy_confirmation_bar_price_cap_enforced": True,
        "strategic_buy_no_chase_reject_independent_of_volume": True,
        "strategic_buy_entire_bar_strict_cross_enforced": True,
        "strategic_buy_five_percent_bar_volume_cap_enforced": True,
        "persistent_sell_five_percent_bar_volume_cap_enforced": True,
        "adverse_observed_bar_extreme_fill_price_enforced": True,
        "completed_bar_close_fill_timestamp_enforced": True,
        "strategic_buy_one_locator_bar_ttl_enforced": True,
        "strategic_buy_causal_full_1m_window_prechecked": True,
        "full_session_240_bar_grid_required": True,
        "opening_auction_event_merged_into_0931": True,
        "optional_buy_data_fault_cancelled": True,
        "optional_buy_security_gate_cancelled": True,
        "execution_fact_incomplete_optional_buy_cancelled": True,
        "operations_cancellation_exact_evidence_audited": True,
        "persistent_exit_independent_symbol_continues": True,
        "persistent_exit_security_blocked_remains_pending": True,
        "persistent_exit_fact_incomplete_remains_pending": True,
        "fill_and_rejection_full_session_grid_audited": True,
        "pending_continuity_requires_gap_free_240_bar_grid": True,
        "current_pending_continuity_proven": True,
        "current_review_queue_raw_1m_boundaries_self_contained": False,
        "raw_1m_entry_boundary_self_contained": True,
        "raw_1m_entry_boundary_source_resolved": True,
        "live_ranked_entry_exact_qmt_catalog_attested": True,
        "structure_anchor_never_used_as_execution_cap": True,
        "execution_rejection_exact_1m_evidence_audited": True,
        "cash_and_equity_accounting_attached": False,
        "daily_valuation_supported": True,
        "daily_valuation_attached": False,
        "exact_one_minute_bar_evidence_attached": True,
        "immutable_execution_evidence_objects": True,
        "contract_change_required_for_new_execution_semantics": True,
    }
    candidate = snapshot["review_queue"][0]
    assert candidate["entry_price_cap"] == "1000"
    assert candidate["entry_confirmation_bar_closed_at"] is not None
    assert candidate["entry_valid_until"] is not None
    assert candidate["entry_boundary_attestation"] == (
        "MISSING_CURRENT_BOUNDARY_EVIDENCE"
    )
    assert candidate["sector_name"] == "板块名称待映射"
    assert candidate["sector_name_attestation"] == ("UNRESOLVED")
    assert candidate["sector_name_point_in_time"] is False
    assert candidate["sector_membership_attestation"] == ("UNRESOLVED")
    assert candidate["sector_membership_point_in_time"] is False

    assert candidate["sector_name_captured_at"] is None
    assert candidate["sector_name_entry_sha256"] is None
    assert candidate["sector_name_catalog_revision"] is None
    assert "review_candidate_id=" in candidate["chart_urls"]["30m"]
    assert "review_source_sha256=" in candidate["chart_urls"]["30m"]
    assert (
        f"review_as_of={candidate['review_as_of_unix']}"
        in candidate["chart_urls"]["30m"]
    )
    assert "intervals=30" in candidate["chart_urls"]["30m"]


def test_sector_name_is_point_in_time_only_after_same_session_capture(
    service: HumanReviewScreeningService,
) -> None:
    capture_at = datetime(2026, 7, 28, 9, 15, tzinfo=TZ)
    base = _alert()
    at_capture = replace(
        base,
        signal_at=capture_at,
        review_available_at=capture_at + timedelta(minutes=26),
        entry_confirmation_bar_closed_at=capture_at,
        entry_valid_until=capture_at + timedelta(hours=1),
    )
    _write_report(service.historical_report, alert=at_capture)
    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_name"] == "银行"
    assert candidate["sector_name_attestation"] == ("POINT_IN_TIME_SAME_SESSION")
    assert candidate["sector_name_point_in_time"] is True
    assert candidate["sector_membership_attestation"] == ("POINT_IN_TIME_SAME_SESSION")
    assert candidate["sector_membership_point_in_time"] is True
    assert candidate["sector_name_captured_at"] == capture_at.isoformat()
    assert candidate["sector_name_entry_sha256"].startswith("sha256:")
    assert candidate["sector_name_catalog_revision"].startswith("sha256:")

    before_capture = capture_at - timedelta(minutes=1)
    too_early = replace(
        base,
        signal_at=before_capture,
        review_available_at=before_capture + timedelta(minutes=26),
        entry_confirmation_bar_closed_at=before_capture,
        entry_valid_until=before_capture + timedelta(hours=1),
    )
    _write_report(service.historical_report, alert=too_early)
    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_name"] == "板块名称待映射"
    assert candidate["sector_name_attestation"] == ("UNRESOLVED")
    assert candidate["sector_name_point_in_time"] is False
    assert candidate["sector_membership_attestation"] == ("UNRESOLVED")
    assert candidate["sector_membership_point_in_time"] is False

    wrong_member = replace(at_capture, symbol="SZ.000002")
    _write_report(service.historical_report, alert=wrong_member)
    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_name"] == "银行"
    assert candidate["sector_name_point_in_time"] is True
    assert candidate["sector_membership_attestation"] == (
        "SAME_SESSION_SYMBOL_NOT_MEMBER"
    )
    assert candidate["sector_membership_point_in_time"] is False


def test_live_ranking_uses_its_exact_qmt_catalog_revision(
    service: HumanReviewScreeningService,
) -> None:
    """A later same-session catalog may not be spliced onto an older rank."""

    source_ledger = json.loads(service.sector_ledger.read_text(encoding="utf-8"))
    source_entry = source_ledger["entries"][0]
    source_revision = str(source_entry["catalog_revision"])
    later_sectors = (
        {
            "sector_id": SECTOR_ID,
            "name": "银行新目录",
            "source_key": "GICS3银行新目录",
            "member_codes": ("SZ.000002",),
        },
    )
    append_sector_catalog(
        service.sector_ledger,
        {
            "source": "qmt_gics3_components",
            "captured_at": "2026-07-28T09:18:00+08:00",
            "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
            "catalog_revision": sha256_json(
                {
                    "schema": "chanlun-qmt-gics3-catalog",
                    "sectors": later_sectors,
                }
            ),
            "sectors": later_sectors,
        },
    )
    signal_at = datetime(2026, 7, 28, 9, 20, tzinfo=TZ)
    base = _alert()
    ranking = SectorRankingReviewEvidence(
        sector_id=SECTOR_ID,
        sector_name="银行",
        observed_at=signal_at,
        eligible=True,
        hard_block=False,
        regime="supportive",
        ordinal=1,
        rank_score=45,
        rank_components=(("neutral_access", 5), ("thirty_support", 40)),
        reason_codes=("structural_ranking_only",),
        horizontal_strength=Decimal("7.5"),
        horizontal_rank=1,
        strength_observed_at=signal_at,
        strength_anchor_session=signal_at.date() - timedelta(days=1),
        strength_member_count=1,
        strength_source_revision="sha256:" + "7" * 64,
        strength_evidence_revision="sha256:" + "8" * 64,
        sector_catalog_revision=source_revision,
    )
    alert = replace(
        base,
        signal_at=signal_at,
        review_available_at=signal_at + timedelta(minutes=26),
        entry_confirmation_bar_closed_at=signal_at,
        entry_valid_until=signal_at + timedelta(hours=1),
        sector_ranking_evidence=ranking,
        source_fact_ids=(*base.source_fact_ids, ranking.evidence_id),
    )
    _write_report(service.historical_report, alert=alert)

    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_name"] == "银行"
    assert candidate["sector_name_captured_at"] == "2026-07-28T09:15:00+08:00"
    assert candidate["sector_name_catalog_revision"] == source_revision
    assert candidate["sector_membership_point_in_time"] is True
    assert candidate["sector_ranking_catalog_attestation"] == (
        "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH"
    )

    unavailable = replace(
        ranking,
        sector_catalog_revision="sha256:" + "f" * 64,
    )
    unavailable_alert = replace(
        alert,
        sector_ranking_evidence=unavailable,
        source_fact_ids=(*base.source_fact_ids, unavailable.evidence_id),
    )
    _write_report(service.historical_report, alert=unavailable_alert)
    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["sector_name"] == "银行"
    assert candidate["sector_name_captured_at"] is None
    assert candidate["sector_membership_point_in_time"] is False
    assert candidate["sector_ranking_catalog_attestation"] == (
        "EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION"
    )


def test_live_ranked_buy_requires_exact_catalog_before_virtual_intent(
    tmp_path: Path,
) -> None:
    """A page warning alone is insufficient: the paper path must fail closed."""

    exact = _forward_ranked_service(tmp_path / "exact")
    exact_snapshot = exact.snapshot(source="forward")
    exact_candidate = exact_snapshot["review_queue"][0]
    assert exact_snapshot["paper_observation_eligible"] is True
    assert exact_candidate["paper_observation_eligible"] is True
    assert exact_candidate["paper_entry_sector_eligible"] is True
    exact_result = exact.append_feedback(
        candidate_id=exact_candidate["candidate_id"],
        source_sha256=exact_snapshot["source_content_sha256"],
        reviewer="catalog-gate-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        request_id="catalog-exact-buy",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "decomposition_judgement": "COMBINED",
            "center_expansion_judgement": "REJECTED",
            "nine_segment_upgrade_judgement": "CONFIRMED",
            "locator_judgement": "CONFIRMED",
            "disposition": "PAPER_OBSERVE",
            "notes": "exact QMT catalog may enter the virtual decision path",
        },
    )
    assert exact_result["paper_observation_eligible"] is True
    assert exact_result["paper_intent"] is not None
    assert exact_result["paper_intent"]["status"] == "PENDING"
    selection = exact_result["paper_intent"]["entry_selection_evidence"]
    assert selection["candidate_id"] == exact_candidate["candidate_id"]
    assert selection["sector_id"] == SECTOR_ID
    assert selection["sector_name"] == "银行"
    assert (
        selection["sector_ranking_evidence_id"]
        == (exact_candidate["sector_ranking_evidence"]["evidence_id"])
    )
    assert (
        selection["sector_catalog_entry_sha256"]
        == (exact_candidate["sector_name_entry_sha256"])
    )
    assert (
        selection["sector_catalog_revision"]
        == (exact_candidate["sector_name_catalog_revision"])
    )
    assert selection["attestation"] == ("EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH")
    audited = exact.snapshot(source="forward")
    assert audited["paper_entry_selection_attestation"]["status"] == "COMPLETE"
    assert (
        audited["paper_entry_selection_attestation"]["verified_catalog_binding_count"]
        == 1
    )
    assert audited["paper_entry_selection_source_audit"]["status"] == "COMPLETE"
    assert (
        audited["paper_entry_selection_source_audit"]["verified_source_binding_count"]
        == 1
    )

    # Losing the catalog proof must stop another buy, but a later WATCH/REJECT
    # remains authorized to cancel the already-pending optional entry.
    without_catalog = HumanReviewScreeningService(
        repository_root=exact.repository_root,
        historical_report=exact.historical_report,
        forward_root=exact.forward_root,
        feedback_ledger=exact.feedback_ledger,
        sector_ledger=exact.repository_root / "missing-sectors.json",
        paper_ledger=exact.paper_ledger,
        parameter_snapshot=exact.parameter_snapshot,
    )
    cancellation = without_catalog.append_feedback(
        candidate_id=exact_candidate["candidate_id"],
        source_sha256=exact_snapshot["source_content_sha256"],
        reviewer="catalog-gate-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, 30, tzinfo=TZ),
        request_id="catalog-missing-cancel",
        values={
            "center_judgement": "REJECTED",
            "trend_judgement": "UNCERTAIN",
            "level_judgement": "UNCERTAIN",
            "point_judgement": "NONE",
            "disposition": "WATCH",
            "notes": "risk-reducing cancellation bypasses the entry gate",
        },
    )
    assert cancellation["paper_observation_eligible"] is False
    assert cancellation["paper_intent"] is None
    assert len(cancellation["superseded_paper_intents"]) == 1
    assert cancellation["superseded_paper_intents"][0]["status"] == "CANCELLED"

    missing = _forward_ranked_service(
        tmp_path / "missing",
        ranking_revision="sha256:" + "f" * 64,
    )
    missing_snapshot = missing.snapshot(source="forward")
    missing_candidate = missing_snapshot["review_queue"][0]
    assert missing_snapshot["paper_observation_eligible"] is True
    assert missing_candidate["paper_observation_eligible"] is False
    assert missing_candidate["paper_entry_sector_eligible"] is False
    assert missing_candidate["paper_observation_reason"] == (
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY"
    )
    missing_result = missing.append_feedback(
        candidate_id=missing_candidate["candidate_id"],
        source_sha256=missing_snapshot["source_content_sha256"],
        reviewer="catalog-gate-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        request_id="catalog-missing-buy",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "decomposition_judgement": "COMBINED",
            "center_expansion_judgement": "REJECTED",
            "nine_segment_upgrade_judgement": "CONFIRMED",
            "locator_judgement": "CONFIRMED",
            "disposition": "PAPER_OBSERVE",
            "notes": "feedback is retained while the virtual entry is withheld",
        },
    )
    assert missing_result["paper_observation_eligible"] is False
    assert missing_result["paper_observation_reason"] == (
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY"
    )
    assert missing_result["paper_intent"] is None
    assert missing_result["paper_ledger_changed"] is False
    assert not missing.paper_ledger.exists()
    assert (
        len(load_human_review_feedback_ledger(missing.feedback_ledger)["entries"]) == 1
    )
    pending = missing.snapshot(source="forward")["review_queue"][0]
    assert pending["paper_reconciliation_pending"] is True
    assert pending["paper_reconciliation_eligible"] is False


def test_entry_selection_source_audit_rejects_rehashed_ranking_binding(
    tmp_path: Path,
) -> None:
    service = _forward_ranked_service(tmp_path)
    snapshot = service.snapshot(source="forward")
    candidate = snapshot["review_queue"][0]
    service.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="selection-source-auditor",
        reviewed_at=datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        request_id="selection-source-audit",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "create one exact evidence-bearing intent",
        },
    )
    events = tuple(load_human_paper_ledger(service.paper_ledger)["events"])
    payload = json.loads(json.dumps(events[0]["payload"]))
    evidence = parse_human_paper_entry_selection_evidence(
        payload["entry_selection_evidence"]
    )
    forged = replace(
        evidence,
        sector_ranking_evidence_id="sha256:" + "9" * 64,
    )
    payload["entry_selection_evidence"] = forged.document()
    _kind, _path, _report, alerts = service._load_source("forward")
    audit = service._paper_entry_selection_source_audit(
        ({"kind": "INTENT", "payload": payload},),
        current_source_sha256=snapshot["source_content_sha256"],
        current_alerts=alerts,
    )

    assert audit["status"] == "INVALID"
    assert audit["verified_source_binding_count"] == 0
    assert (
        "differs from source ranking" in audit["invalid_source_bindings"][0]["reason"]
    )


def test_catalog_entry_gate_does_not_block_virtual_exit_review(
    tmp_path: Path,
) -> None:
    """Selection provenance blocks new entries, never risk-reducing exits."""

    guarded = _forward_ranked_service(
        tmp_path,
        ranking_revision="sha256:" + "f" * 64,
        alert_type="POSSIBLE_30M_EXIT",
    )

    snapshot = guarded.snapshot(source="forward")
    candidate = snapshot["review_queue"][0]
    assert candidate["sector_ranking_catalog_attestation"] == (
        "EXACT_REVISION_UNAVAILABLE_AT_OBSERVATION"
    )
    assert candidate["paper_entry_sector_eligible"] is True
    assert candidate["paper_observation_eligible"] is True
    result = guarded.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="catalog-exit-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        request_id="catalog-mismatch-exit",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "DOWN",
            "level_judgement": "30M",
            "point_judgement": "SELL_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "exit review must bypass the new-entry provenance gate",
        },
    )
    assert result["paper_observation_eligible"] is True
    assert result["paper_intent"] is not None
    assert result["paper_intent"]["reason_codes"] == [
        "SELL_REVIEW_HAS_NO_VIRTUAL_POSITION"
    ]


def test_operational_outage_does_not_block_pending_virtual_buy_cancellation(
    tmp_path: Path,
) -> None:
    """A broken scheduler may block creation, but never a risk-reducing cancel."""

    service = _forward_ranked_service(tmp_path)
    snapshot = service.snapshot(source="forward")
    candidate = snapshot["review_queue"][0]
    created = service.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="operations-cancel-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        request_id="operations-create-buy",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "create one pending virtual buy before the outage",
        },
    )
    assert created["paper_intent"]["status"] == "PENDING"

    blocked = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: datetime(2026, 7, 28, 9, 31, 30, tzinfo=TZ),
        forward_scheduler_provider=lambda **_kwargs: _forward_scheduler_observation(
            ready=False,
            observed_at="2026-07-28T09:31:30+08:00",
        ),
    )
    cancelled = blocked.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="operations-cancel-reviewer",
        reviewed_at=datetime(2026, 7, 28, 9, 31, 30, tzinfo=TZ),
        request_id="operations-cancel-buy",
        values={
            "center_judgement": "REJECTED",
            "trend_judgement": "UNCERTAIN",
            "level_judgement": "UNCERTAIN",
            "point_judgement": "NONE",
            "disposition": "WATCH",
            "notes": "cancel remains possible while the scheduler is down",
        },
    )
    assert cancelled["paper_observation_source_eligible"] is False
    assert cancelled["paper_observation_source_reason"] == (
        "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"
    )
    assert cancelled["paper_intent"] is None
    assert len(cancelled["superseded_paper_intents"]) == 1
    assert cancelled["superseded_paper_intents"][0]["status"] == "CANCELLED"


def test_missing_exact_catalog_can_idempotently_reconcile_after_capture_arrives(
    tmp_path: Path,
) -> None:
    """One saved review may be promoted only after its exact catalog is archived."""

    reference_ledger = tmp_path / "reference-sectors.json"
    _write_sector_ledger(reference_ledger)
    revision = str(
        json.loads(reference_ledger.read_text(encoding="utf-8"))["entries"][0][
            "catalog_revision"
        ]
    )
    root = tmp_path / "late-capture"
    historical = root / "historical.json"
    sector_ledger = root / "sectors.json"
    _write_report(historical)
    alert = _live_ranked_alert(catalog_revision=revision)
    forward = (
        root
        / "forward"
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    _write_report(forward, forward_session="2026-07-28", alert=alert)
    service = HumanReviewScreeningService(
        repository_root=root,
        historical_report=historical,
        forward_root=root / "forward",
        feedback_ledger=root / "feedback.json",
        sector_ledger=sector_ledger,
        paper_ledger=root / "paper.json",
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )
    snapshot = service.snapshot(source="forward")
    candidate = snapshot["review_queue"][0]
    values = {
        "center_judgement": "CONFIRMED",
        "trend_judgement": "UP",
        "level_judgement": "30M",
        "point_judgement": "BUY_3",
        "disposition": "PAPER_OBSERVE",
        "notes": "retain once and reconcile after exact capture",
    }
    common = {
        "candidate_id": candidate["candidate_id"],
        "source_sha256": snapshot["source_content_sha256"],
        "reviewer": "late-catalog-reviewer",
        "reviewed_at": datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        "request_id": "late-catalog-review",
        "values": values,
    }
    waiting = service.append_feedback(**common)
    assert waiting["paper_intent"] is None
    assert waiting["paper_observation_reason"] == (
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY"
    )
    assert (
        len(load_human_review_feedback_ledger(service.feedback_ledger)["entries"]) == 1
    )
    queued = service.snapshot(source="forward")["review_queue"][0]
    assert queued["paper_reconciliation_pending"] is True
    assert queued["paper_reconciliation_eligible"] is False

    _write_sector_ledger(sector_ledger)
    recovered = service.snapshot(source="forward")["review_queue"][0]
    assert recovered["paper_observation_eligible"] is True
    assert recovered["paper_reconciliation_pending"] is True
    assert recovered["paper_reconciliation_eligible"] is True
    promoted = service.append_feedback(**common)
    assert promoted["feedback"]["feedback_id"] == waiting["feedback"]["feedback_id"]
    assert promoted["paper_intent"]["status"] == "PENDING"
    assert (
        promoted["paper_intent"]["entry_selection_evidence"]["feedback_id"]
        == waiting["feedback"]["feedback_id"]
    )
    assert promoted["paper_ledger_changed"] is True
    assert (
        len(load_human_review_feedback_ledger(service.feedback_ledger)["entries"]) == 1
    )
    final = service.snapshot(source="forward")["review_queue"][0]
    assert final["paper_reconciliation_pending"] is False


def test_sector_source_evidence_survives_report_reload_and_rejects_tampering(
    service: HumanReviewScreeningService,
) -> None:
    """Portable M/W/D source evidence must survive JSON restart boundaries."""

    warmup = QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=480,
        suffix_daily_bar_count=320,
        converged=True,
        reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
        full_signature="sha256:" + "7" * 64,
        suffix_signature="sha256:" + "7" * 64,
    )
    base = _alert()
    convergence = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=base.signal_at,
        parameter_set_id=(QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID),
        observations=tuple(
            WarmupPrefixObservation(
                bar_count=count,
                starts_at=base.signal_at - timedelta(days=count),
                signature_sha256="sha256:" + "6" * 64,
            )
            for count in (480, 640, 800, 960)
        ),
    )
    evidence = SectorHigherTimeframeReviewEvidence(
        source_mode="PAGE_PARITY_SAME_5M_BASE",
        strict_same_5m_warmup_evidence=warmup,
        research_bridge_parameter_set_id=None,
        strict_same_5m_source_coverage_evidence=(
            QmtSectorSameBaseCoverageEvidence(
                observed_at=base.signal_at,
                calendar_first_session=(base.signal_at - timedelta(days=800)).date(),
                first_visible_bar_at=base.signal_at - timedelta(days=700),
                last_visible_bar_at=base.signal_at - timedelta(minutes=5),
                first_completed_session=(base.signal_at - timedelta(days=700)).date(),
                last_completed_session=(base.signal_at - timedelta(days=1)).date(),
                visible_five_minute_bar_count=480 * 48,
                completed_daily_bar_count=480,
                required_daily_bar_count=480,
                remaining_daily_bar_count=0,
                missing_leading_calendar_session_count=0,
                warmup_converged=True,
                warmup_reason_code=("QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"),
                boundary_status="REQUIRED_HISTORY_CONVERGED",
                physical_source_boundary_status=(
                    "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
                ),
                physical_source_requested_start_at=(
                    base.signal_at - timedelta(days=800)
                ),
                physical_source_required_contributor_start_at=(
                    base.signal_at - timedelta(days=700)
                ),
                physical_source_representative_member_count=10,
                physical_source_available_member_count=10,
                physical_source_required_contributor_count=8,
                physical_source_inventory_revision="sha256:" + "f" * 64,
            )
        ),
        warmup_convergence_evidence=convergence,
        strict_same_5m_warmup_convergence_evidence=convergence,
        sector_id=base.sector_id,
        observed_at=base.signal_at,
        gate=base.sector_risk_gate,
        states=tuple((period, "UNRESOLVED") for period in ("M", "W", "D")),
        reason_codes=("HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED",),
        period_diagnostics=(),
    )
    alert = replace(
        base,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
        sector_higher_timeframe_evidence=evidence,
    )
    _write_report(service.historical_report, alert=alert)

    restarted = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        trading_session_provider=_trading_session_provider,
    )
    snapshot = restarted.snapshot(source="historical")
    candidate = snapshot["review_queue"][0]
    assert candidate["candidate_id"] == alert.candidate_id
    assert candidate["sector_higher_timeframe_evidence"] == evidence.document()
    assert evidence.evidence_id in candidate["source_fact_ids"]
    assert candidate["live_status"] == "LIVE_DISABLED"

    # Recompute the outer report hash to prove that nested semantic identity,
    # rather than only the JSON file checksum, catches rewritten evidence.
    payload = json.loads(service.historical_report.read_text(encoding="utf-8"))
    payload["review_queue"][0]["sector_higher_timeframe_evidence"][
        "strict_same_5m_warmup_evidence"
    ]["full_signature"] = "sha256:" + "8" * 64
    stable = dict(payload)
    stable.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable)
    service.historical_report.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(
        HumanReviewScreenUnavailable,
        match="human_review_candidate_malformed",
    ):
        restarted.snapshot(source="historical")


def test_market_symbol_mwd_evidence_is_exposed_with_explicit_attestation(
    service: HumanReviewScreeningService,
) -> None:
    base = _alert()
    unresolved_states = tuple((period, "UNRESOLVED") for period in ("M", "W", "D"))
    evidence = MarketSymbolHigherTimeframeReviewEvidence(
        symbol=base.symbol,
        observed_at=base.signal_at,
        market=HigherTimeframeReviewSideEvidence(
            subject="MARKET",
            gate="UNRESOLVED",
            states=unresolved_states,
            reason_codes=("MARKET_MWD_SOURCE_UNRESOLVED",),
            period_diagnostics=(),
        ),
        symbol_evidence=HigherTimeframeReviewSideEvidence(
            subject="SYMBOL",
            gate="UNRESOLVED",
            states=unresolved_states,
            reason_codes=("SYMBOL_MWD_SOURCE_UNRESOLVED",),
            period_diagnostics=(),
        ),
    )
    alert = replace(
        base,
        market_risk_gate="UNRESOLVED",
        symbol_risk_gate="UNRESOLVED",
        market_symbol_higher_timeframe_evidence=evidence,
        source_fact_ids=(*base.source_fact_ids, evidence.evidence_id),
    )
    _write_report(service.historical_report, alert=alert)

    candidate = service.snapshot(source="historical")["review_queue"][0]
    assert candidate["market_symbol_higher_timeframe_evidence"] == (evidence.document())
    assert (
        candidate["market_symbol_higher_timeframe_source_attestation"]
        == "STRUCTURE_ONLY"
    )
    assert evidence.evidence_id in candidate["source_fact_ids"]


def test_market_symbol_source_support_survives_service_restart(
    service: HumanReviewScreeningService,
) -> None:
    """The page receives replayable source facts, not only coloured M/W/D gates."""

    base = _alert()
    unresolved_states = tuple((period, "UNRESOLVED") for period in ("M", "W", "D"))
    market_support = HigherTimeframeReviewSourceSupport(
        subject="MARKET",
        session_evidence=HigherTimeframeSessionEvidence.exact(),
    )
    symbol_support = HigherTimeframeReviewSourceSupport(
        subject="SYMBOL",
        session_evidence=HigherTimeframeSessionEvidence.exact(),
    )
    evidence = MarketSymbolHigherTimeframeReviewEvidence(
        symbol=base.symbol,
        observed_at=base.signal_at,
        market=HigherTimeframeReviewSideEvidence(
            subject="MARKET",
            gate="UNRESOLVED",
            states=unresolved_states,
            reason_codes=("MARKET_MWD_SOURCE_UNRESOLVED",),
            period_diagnostics=(),
            source_support=market_support,
        ),
        symbol_evidence=HigherTimeframeReviewSideEvidence(
            subject="SYMBOL",
            gate="UNRESOLVED",
            states=unresolved_states,
            reason_codes=("SYMBOL_MWD_SOURCE_UNRESOLVED",),
            period_diagnostics=(),
            source_support=symbol_support,
        ),
    )
    alert = replace(
        base,
        market_risk_gate="UNRESOLVED",
        symbol_risk_gate="UNRESOLVED",
        market_symbol_higher_timeframe_evidence=evidence,
        source_fact_ids=(
            *base.source_fact_ids,
            market_support.support_id,
            symbol_support.support_id,
            evidence.evidence_id,
        ),
    )
    _write_report(service.historical_report, alert=alert)

    restarted = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        trading_session_provider=_trading_session_provider,
    )
    candidate = restarted.snapshot(source="historical")["review_queue"][0]
    portable = candidate["market_symbol_higher_timeframe_evidence"]
    assert candidate["market_symbol_higher_timeframe_source_attestation"] == (
        "SELF_CONTAINED"
    )
    assert portable["market"]["source_support"] == market_support.document()
    assert portable["symbol_evidence"]["source_support"] == (symbol_support.document())
    assert market_support.support_id in candidate["source_fact_ids"]
    assert symbol_support.support_id in candidate["source_fact_ids"]

    partial_evidence = replace(
        evidence,
        symbol_evidence=replace(
            evidence.symbol_evidence,
            source_support=None,
        ),
    )
    partial_alert = replace(
        alert,
        market_symbol_higher_timeframe_evidence=partial_evidence,
        source_fact_ids=(
            *base.source_fact_ids,
            market_support.support_id,
            partial_evidence.evidence_id,
        ),
    )
    _write_report(service.historical_report, alert=partial_alert)
    partial = restarted.snapshot(source="historical")["review_queue"][0]
    assert partial["market_symbol_higher_timeframe_source_attestation"] == (
        "PARTIAL_SOURCE_SUPPORT"
    )


def test_sector_gate_omission_is_rejected(
    service: HumanReviewScreeningService,
) -> None:
    current = replace(
        _alert(),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    incomplete_identity = human_review_alert_document(current)
    for field in (
        "sector_risk_gate",
        "entry_confirmation_bar_closed_at",
        "entry_price_cap",
        "entry_valid_until",
        "entry_boundary_evidence_id",
    ):
        incomplete_identity.pop(field, None)
    incomplete_candidate_id = sha256_json(incomplete_identity)
    report = _report()
    report["review_queue"] = [
        {
            **_jsonable(incomplete_identity),
            "candidate_id": incomplete_candidate_id,
            "signal_lifecycle_id": current.signal_lifecycle_id,
        }
    ]
    report.pop("content_sha256")
    report["content_sha256"] = sha256_json(report)
    service.historical_report.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(HumanReviewScreenUnavailable):
        service.snapshot(source="historical")


def test_snapshot_attaches_only_valid_immutable_daily_valuation(
    service: HumanReviewScreeningService,
) -> None:
    paper = load_human_paper_ledger(service.paper_ledger)
    accounting = rebuild_human_paper_accounting(
        tuple(paper["events"]),
        parameters=load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT),
        execution_evidence_status="NO_FILLS",
    )
    document = build_human_paper_valuation_document(
        session=datetime(2026, 7, 28, tzinfo=TZ).date(),
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=str(paper["content_sha256"]),
        accounting=accounting,
        marks=(),
        errors=(),
    )
    session_root = service.forward_root / "sessions" / "2026-07-28"
    object_path = (
        session_root
        / "objects"
        / "paper_valuation"
        / f"{str(document['content_sha256'])[7:]}.json"
    )
    object_path.parent.mkdir(parents=True)
    encoded = json.dumps(document, ensure_ascii=False)
    object_path.write_text(encoded, encoding="utf-8")
    (session_root / "paper_valuation.json").write_text(encoded, encoding="utf-8")
    append_forward_paper_event(
        service.forward_root / "forward_paper_ledger.json",
        contract=load_forward_contract(PARAMETER_SNAPSHOT),
        session=datetime(2026, 7, 28, tzinfo=TZ).date(),
        phase="DECISION",
        status="EVALUATED",
        evidence={
            "human_paper_valuation": {
                "status": "VALUATION_COMPLETE",
                "session": "2026-07-28",
                "valuation_content_sha256": document["content_sha256"],
            }
        },
        recorded_at=datetime(2026, 7, 28, 15, 21, tzinfo=TZ),
    )

    snapshot = service.snapshot()

    assert snapshot["paper_valuation"]["status"] == "COMPLETE"
    assert snapshot["paper_valuation"]["latest"]["equity"] == "1000000.00"
    capabilities = snapshot["paper_execution_capabilities"]
    assert capabilities["daily_valuation_supported"] is True
    assert capabilities["daily_valuation_attached"] is True
    assert capabilities["cash_and_equity_accounting_attached"] is True
    assert snapshot["portfolio_performance_evaluable"] is False


def test_snapshot_reports_due_daily_sector_capture_missing(
    service: HumanReviewScreeningService,
) -> None:
    due_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: datetime(2026, 7, 29, 9, 11, tzinfo=TZ),
        sector_capture_due=time(9, 10),
        trading_session_provider=_trading_session_provider,
    )

    snapshot = due_service.snapshot(source="latest")
    audit = snapshot["sector_capture_receipts"]
    assert audit["status"] == "REQUIRED_CAPTURE_MISSING"
    assert audit["required_capture_session"] == "2026-07-29"
    assert audit["required_capture_present"] is False
    assert audit["required_capture_missing_sessions"] == ("2026-07-29",)
    assert "QMT_SECTOR_RECEIPTS_REQUIRED_CAPTURE_MISSING" in snapshot["warnings"]


def test_forward_archive_readiness_requires_the_capture_receipt(
    service: HumanReviewScreeningService,
) -> None:
    # The service fixture deliberately has a valid hash-chained catalog row but
    # no daily Capture receipt.  It remains usable for page labels, not for a
    # complete forward archive.
    result = service.forward_archive_capture_readiness(
        session=datetime(2026, 7, 28, tzinfo=TZ).date()
    )

    assert result["ready"] is False
    assert result["reason_code"] == ("SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN")
    assert result["receipt_proven"] is False
    assert result["catalog_entry_sha256"].startswith("sha256:")


def test_forward_capture_readiness_health_adapter_is_single_flight(
    service: HumanReviewScreeningService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """严格采集审计可以较慢，但 HTTP 就绪检查不得等待它。"""

    session = date(2026, 7, 30)
    started = Event()
    release = Event()
    calls: list[date | None] = []

    def slow_audit(
        *,
        session: date | None,
        _calendar_requirement=None,
    ) -> dict[str, object]:
        calls.append(session)
        started.set()
        assert release.wait(timeout=2)
        return {
            "schema": "chanlun-qmt-forward-capture-readiness",
            "required": True,
            "requirement_resolved": True,
            "ready": True,
            "status": "ready",
            "reason_code": "READY",
            "session": session.isoformat() if session is not None else None,
            "receipt_proven": True,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }

    monkeypatch.setattr(service, "forward_archive_capture_readiness", slow_audit)

    first = service.forward_archive_capture_readiness_nonblocking(session=session)
    assert first["reason_code"] == "FORWARD_CAPTURE_VALIDATION_PENDING"
    assert first["ready"] is False
    assert started.wait(timeout=1)

    second = service.forward_archive_capture_readiness_nonblocking(session=session)
    assert second["reason_code"] == "FORWARD_CAPTURE_VALIDATION_PENDING"
    assert calls == [session]

    release.set()
    deadline = time_module.monotonic() + 2
    cached = second
    while time_module.monotonic() < deadline:
        cached = service.forward_archive_capture_readiness_nonblocking(session=session)
        if cached["reason_code"] == "READY":
            break
        time_module.sleep(0.01)
    assert cached["ready"] is True
    assert calls == [session]


def test_forward_delivery_readiness_rejects_self_reported_evaluated_event(
    service: HumanReviewScreeningService,
) -> None:
    session = datetime(2026, 7, 30, tzinfo=TZ).date()
    observed_at = datetime(2026, 7, 30, 23, 1, tzinfo=TZ)
    delivery_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: observed_at,
        trading_session_provider=_trading_session_provider,
        forward_implementation_provenance_provider=(
            lambda: _implementation_provenance()
        ),
    )

    missing = delivery_service.forward_delivery_readiness(session=session)
    assert missing["ready"] is False
    assert missing["reason_code"] == "CAPTURE_MISSING_AFTER_DUE"

    contract = load_forward_contract(service.parameter_snapshot)
    ledger_path = service.forward_root / "forward_paper_ledger.json"
    append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=session,
        phase="CAPTURE",
        status="CAPTURED",
        evidence={"receipt_sha256": "sha256:" + "1" * 64},
        recorded_at=datetime(2026, 7, 30, 9, 12, tzinfo=TZ),
    )
    captured = delivery_service.forward_delivery_readiness(session=session)
    assert captured["ready"] is False
    assert captured["reason_code"] == ("CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED")
    assert captured["implementation_continuity_preflight"]["ready"] is False

    append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=session,
        phase="DECISION",
        status="EVALUATED",
        evidence={"candidate_count": 0},
        recorded_at=datetime(2026, 7, 30, 19, 22, tzinfo=TZ),
    )
    rejected = delivery_service.forward_delivery_readiness(session=session)
    assert rejected["ready"] is False
    assert rejected["reason_code"] == "DATA_READY_EVENT_MISSING"
    assert rejected["capture_event_present"] is True
    assert rejected["evaluation_event_present"] is True
    assert rejected["capture_ready"] is False
    assert rejected["evaluation_ready"] is False


def test_forward_delivery_readiness_health_adapter_is_single_flight(
    service: HumanReviewScreeningService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict delivery audit may be slow, but HTTP readiness must not wait."""

    session = date(2026, 7, 30)
    started = Event()
    release = Event()
    calls: list[date | None] = []

    def slow_audit(
        *,
        session: date | None,
        _calendar_requirement=None,
    ) -> dict[str, object]:
        calls.append(session)
        started.set()
        assert release.wait(timeout=2)
        return {
            "schema": "chanlun-forward-paper-session-delivery",
            "required": True,
            "requirement_resolved": True,
            "ready": True,
            "status": "ready",
            "reason_code": "READY",
            "session": session.isoformat() if session is not None else None,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "paper_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }

    monkeypatch.setattr(service, "forward_delivery_readiness", slow_audit)

    first = service.forward_delivery_readiness_nonblocking(session=session)
    assert first["reason_code"] == "FORWARD_DELIVERY_VALIDATION_PENDING"
    assert first["ready"] is False
    assert started.wait(timeout=1)

    second = service.forward_delivery_readiness_nonblocking(session=session)
    assert second["reason_code"] == "FORWARD_DELIVERY_VALIDATION_PENDING"
    assert calls == [session]

    release.set()
    deadline = time_module.monotonic() + 2
    cached = second
    while time_module.monotonic() < deadline:
        cached = service.forward_delivery_readiness_nonblocking(session=session)
        if cached["reason_code"] == "READY":
            break
        time_module.sleep(0.01)
    assert cached["ready"] is True
    assert calls == [session]


def test_forward_delivery_preflight_blocks_changed_source_and_recovers(
    service: HumanReviewScreeningService,
) -> None:
    session = date(2026, 7, 30)
    observed_at = datetime(2026, 7, 30, 23, 1, tzinfo=TZ)
    contract = load_forward_contract(service.parameter_snapshot)
    ledger_path = service.forward_root / "forward_paper_ledger.json"
    captured_implementation = _implementation_provenance("a")
    append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=session,
        phase="CAPTURE",
        status="CAPTURED",
        evidence={
            "receipt_sha256": "sha256:" + "1" * 64,
            "implementation_provenance": captured_implementation,
        },
        recorded_at=datetime(2026, 7, 30, 9, 12, tzinfo=TZ),
    )

    def loaded(current_digit: str) -> HumanReviewScreeningService:
        return HumanReviewScreeningService(
            repository_root=service.repository_root,
            historical_report=service.historical_report,
            forward_root=service.forward_root,
            feedback_ledger=service.feedback_ledger,
            sector_ledger=service.sector_ledger,
            paper_ledger=service.paper_ledger,
            parameter_snapshot=service.parameter_snapshot,
            clock=lambda: observed_at,
            trading_session_provider=_trading_session_provider,
            forward_implementation_provenance_provider=(
                lambda: _implementation_provenance(current_digit)
            ),
        )

    changed = loaded("b").forward_delivery_readiness(session=session)
    assert changed["ready"] is False
    assert changed["reason_code"] == "IMPLEMENTATION_CHANGED_SINCE_CAPTURE"
    assert changed["implementation_continuity_preflight"]["ready"] is False
    assert (
        changed["implementation_continuity_preflight"]["market_data_read_authorized"]
        is False
    )

    restored = loaded("a").forward_delivery_readiness(session=session)
    assert restored["reason_code"] == "EVALUATION_MISSING_AFTER_DEADLINE"
    assert restored["implementation_continuity_preflight"]["ready"] is True
    assert (
        restored["implementation_continuity_preflight"][
            "same_implementation_as_capture"
        ]
        is True
    )


def test_forward_surfaces_share_fail_closed_qmt_calendar_requirement(
    service: HumanReviewScreeningService,
) -> None:
    session = datetime(2026, 7, 31, tzinfo=TZ).date()
    observed_at = datetime(2026, 7, 31, 9, 11, tzinfo=TZ)

    def unpublished(*, session, observed_at):
        return build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            returned_sessions=(),
            published_through=datetime(2026, 7, 30, tzinfo=TZ).date(),
            query_attempted=True,
            query_succeeded=True,
        )

    unresolved_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: observed_at,
        sector_capture_due=time(9, 10),
        trading_session_provider=unpublished,
    )

    archive = unresolved_service.forward_archive_capture_readiness(session=session)
    default_archive = unresolved_service.forward_archive_capture_readiness(session=None)
    delivery = unresolved_service.forward_delivery_readiness(session=session)

    for result in (archive, default_archive, delivery):
        assert result["required"] is None
        assert result["requirement_resolved"] is False
        assert result["reason_code"] == "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
        assert result["trading_session_status"] == "UNRESOLVED"
    assert unresolved_service._required_sector_capture_session() is None

    holiday = datetime(2026, 7, 29, tzinfo=TZ).date()

    def historical_holiday(*, session, observed_at):
        return build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            returned_sessions=(),
            published_through=datetime(2026, 7, 30, tzinfo=TZ).date(),
            query_attempted=True,
            query_succeeded=True,
        )

    holiday_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: datetime(2026, 7, 30, 16, tzinfo=TZ),
        trading_session_provider=historical_holiday,
    )
    not_due = holiday_service.forward_delivery_readiness(session=holiday)
    assert not_due["required"] is False
    assert not_due["reason_code"] == "NON_TRADING_SESSION_NOT_DUE"


def test_snapshot_rejects_markout_without_session_qualification(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    markout_stable = {
        "schema": "chanlun-forward-review-markout",
        "through_session": "2026-07-28",
        "diagnostic_only": True,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "automated_order_authorized": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
        "sample": {
            "unique_lifecycle_count": 25,
            "eligible_by_horizon": {"5": 0, "10": 0, "20": 0},
        },
        "summary": {
            "5": {"eligible_count": 0, "pending_count": 25},
            "10": {"eligible_count": 0, "pending_count": 25},
            "20": {"eligible_count": 0, "pending_count": 25},
        },
        "summary_by_risk_class": {"BLOCKED": {}},
        "reason_codes": [
            "SCREENING_MARKOUT_IS_NOT_A_TRADE_RETURN",
            "STRATEGIC_MARKOUT_SAMPLE_INSUFFICIENT",
        ],
    }
    markout = tmp_path / "forward_markout.json"
    markout.write_text(
        json.dumps(
            {
                **markout_stable,
                "content_sha256": sha256_json(markout_stable),
            }
        ),
        encoding="utf-8",
    )
    linked = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        forward_markout_report=markout,
    )

    with pytest.raises(
        HumanReviewScreenUnavailable,
        match="human_review_forward_markout_invalid",
    ):
        linked._forward_markout()


def test_snapshot_requires_the_current_forward_source_audit_contract(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    through_session = date(2026, 7, 28)
    qualification_stable = {
        "schema": "chanlun-forward-review-session-qualification",
        "through_session": through_session.isoformat(),
        "observed_at": datetime(2026, 7, 28, 15, 20, tzinfo=TZ).isoformat(),
        "qualified_sessions": [],
        "qualified_session_evidence": [],
        "excluded_sessions": [],
        "qualified_session_count": 0,
        "excluded_session_count": 0,
        "current_session_excluded_until_terminal_event": True,
        "forward_ledger_content_sha256": None,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    qualification = {
        **qualification_stable,
        "content_sha256": sha256_json(qualification_stable),
    }
    report = build_forward_review_markout(
        (),
        through_session=through_session,
        trading_sessions=(),
        bars_by_symbol={},
        source_session_qualification=qualification,
    )
    markout = tmp_path / "current_forward_markout.json"
    markout.write_text(json.dumps(report), encoding="utf-8")
    linked = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        forward_markout_report=markout,
    )

    assert linked.snapshot()["forward_markout"]["status"] == "AVAILABLE"
    assert report["sample_cohort_contract"] == (
        FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID
    )
    assert report["source_session_qualification"] == qualification

    for required_field in (
        "source_audit_requirement",
        "sample_cohort_contract",
    ):
        tampered = dict(report)
        tampered.pop(required_field)
        stable = {key: tampered[key] for key in tampered if key != "content_sha256"}
        tampered["content_sha256"] = sha256_json(stable)
        markout.write_text(json.dumps(tampered), encoding="utf-8")
        reloaded = HumanReviewScreeningService(
            repository_root=service.repository_root,
            historical_report=service.historical_report,
            forward_root=service.forward_root,
            feedback_ledger=service.feedback_ledger,
            sector_ledger=service.sector_ledger,
            paper_ledger=service.paper_ledger,
            parameter_snapshot=service.parameter_snapshot,
            forward_markout_report=markout,
        )
        invalid = reloaded.snapshot()
        assert invalid["forward_markout"]["status"] == "INVALID"
        assert invalid["forward_markout"]["reason_codes"] == [
            "human_review_forward_markout_invalid"
        ]
        assert "human_review_forward_markout_invalid" in invalid["warnings"]

    forged = json.loads(json.dumps(report))
    forged["sample"]["sample_sufficient_by_horizon"] = {"5": True}
    stable = {key: forged[key] for key in forged if key != "content_sha256"}
    forged["content_sha256"] = sha256_json(stable)
    markout.write_text(json.dumps(forged), encoding="utf-8")
    reloaded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        forward_markout_report=markout,
    )
    assert reloaded.snapshot()["forward_markout"]["status"] == "INVALID"


def test_snapshot_validates_forward_warmup_lineage_rollup(
    service: HumanReviewScreeningService,
) -> None:
    report = build_forward_warmup_structure_lineage_rollup(
        (),
        through_session=date(2026, 7, 28),
        source_session_qualification_sha256="sha256:" + "9" * 64,
    )
    path = service.forward_warmup_lineage_report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")

    lineage = service.snapshot()["forward_warmup_structure_lineage"]
    assert lineage["status"] == "NO_QUALIFIED_SESSIONS"
    assert lineage["qualified_session_count"] == 0
    assert lineage["structure_event_count"] == 0
    assert lineage["validation_scope"] == ("SELF_CONTAINED_DERIVED_INVARIANTS")
    assert lineage["source_rebuild_required_for_full_verification"] is True

    forged = dict(report)
    forged["qualified_session_count"] = 1
    stable = {key: forged[key] for key in forged if key != "content_sha256"}
    forged["content_sha256"] = sha256_json(stable)
    path.write_text(json.dumps(forged), encoding="utf-8")
    invalid = service.snapshot()
    assert invalid["forward_warmup_structure_lineage"]["status"] == "INVALID"
    assert invalid["forward_warmup_structure_lineage"]["reason_codes"] == [
        "human_review_forward_warmup_structure_lineage_invalid"
    ]
    assert (
        "human_review_forward_warmup_structure_lineage_invalid" in invalid["warnings"]
    )


def test_latest_prefers_the_newest_forward_screen(
    service: HumanReviewScreeningService,
) -> None:
    report = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    _write_report(report, forward_session="2026-07-28")

    snapshot = service.snapshot(source="latest")

    assert snapshot["source_kind"] == "forward"
    assert snapshot["sample"]["forward_session"] == "2026-07-28"
    assert "forward" in snapshot["source_options"]


def test_historical_source_requires_the_current_release_sidecar(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current_release" / "human_review_screen.json"
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=current,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        trading_session_provider=_trading_session_provider,
    )

    with pytest.raises(
        HumanReviewScreenUnavailable,
        match="human_review_report_unavailable",
    ):
        guarded.snapshot(source="historical")

    _write_report(current)
    promoted = guarded.snapshot(source="historical")
    assert promoted["source_path"].endswith("current_release/human_review_screen.json")
    assert promoted["source_currentness"]["status"] == ("CURRENT_RELEASE_SIDECAR")


def test_latest_materializes_live_scan_and_keeps_old_chart_lock(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "trading_screening_snapshot.json"
    live_path.write_text(
        json.dumps(live_snapshot(), ensure_ascii=False),
        encoding="utf-8",
    )
    live_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        live_screening_snapshot=live_path,
        live_archive_root=tmp_path / "live_archive",
    )

    first = live_service.snapshot(source="latest")
    assert first["source_kind"] == "live"
    assert str(first["decision_source_snapshot_id"]).startswith("sha256:")
    assert first["decision_core_id"] == live_snapshot()["decision_core_id"]
    assert first["review_queue_count"] == 2
    assert first["source_options"][0] == "live"
    old = next(
        row
        for row in first["review_queue"]
        if row["alert_type"] == "POSSIBLE_5M_TRADE_BUY"
    )
    feedback = live_service.append_feedback(
        candidate_id=old["candidate_id"],
        source_sha256=first["source_content_sha256"],
        reviewer="reviewer-live",
        reviewed_at=datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
        request_id="live-lifecycle-review-1",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "bind review and virtual intent to the signal lifecycle",
        },
    )
    # The synthetic live fixture intentionally carries a catalog identity that
    # is absent from this service's QMT ledger.  Human feedback remains bound
    # to the lifecycle, but the new-entry paper path now fails closed.
    assert feedback["paper_intent"] is None
    assert feedback["paper_observation_reason"] == (
        "QMT_RANKING_CATALOG_EXACT_REVISION_UNAVAILABLE_FOR_PAPER_ENTRY"
    )

    changed = live_snapshot()
    changed["signals"][0]["name"] = "revision-with-same-signal-lifecycle"
    changed["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(changed)
    live_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    second = live_service.snapshot(source="live")
    assert second["source_content_sha256"] != first["source_content_sha256"]
    assert len(tuple((tmp_path / "live_archive").glob("*/*.json"))) == 2
    current = next(
        row
        for row in second["review_queue"]
        if row["signal_lifecycle_id"] == old["signal_lifecycle_id"]
    )
    assert current["candidate_id"] != old["candidate_id"]
    assert second["reviewed_candidate_count"] == 1
    assert current["latest_feedback"]["request_id"] == "live-lifecycle-review-1"
    assert current["paper_events"] == []

    stale_feedback = live_service.append_feedback(
        candidate_id=old["candidate_id"],
        source_sha256=first["source_content_sha256"],
        reviewer="reviewer-live",
        reviewed_at=datetime(2026, 7, 28, 12, 5, tzinfo=TZ),
        request_id="stale-live-review-1",
        values={
            "center_judgement": "REJECTED",
            "trend_judgement": "UNCERTAIN",
            "level_judgement": "UNCERTAIN",
            "point_judgement": "NONE",
            "disposition": "WATCH",
            "notes": "旧快照仍可留痕，但不能改写当前虚拟意图",
        },
    )
    assert stale_feedback["paper_observation_eligible"] is False
    assert stale_feedback["paper_observation_reason"] == ("SOURCE_SUPERSEDED_FOR_PAPER")
    assert stale_feedback["superseded_paper_intents"] == []
    assert not live_service.paper_ledger.exists()

    lock = live_service.validate_chart_lock(
        candidate_id=old["candidate_id"],
        source_sha256=first["source_content_sha256"],
        review_as_of=old["review_as_of_unix"],
    )
    assert lock["candidate_id"] == old["candidate_id"]


def test_live_materialization_uses_semantic_not_operational_snapshot_identity(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "trading_screening_snapshot.json"
    first_payload = live_snapshot()
    first_payload.update(
        {
            "generated_at": "2026-07-28T15:01:00+08:00",
            "scanned_at": "2026-07-28T15:01:00+08:00",
        }
    )
    first_payload["scan_audit"].update(
        {"batch_duration_ms": 100, "coverage_cycle_batch_count": 3}
    )
    first_payload["coverage_manifest"]["batch_count"] = 3
    first_payload["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        first_payload
    )
    live_path.write_text(
        json.dumps(first_payload, ensure_ascii=False), encoding="utf-8"
    )
    live_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        live_screening_snapshot=live_path,
        live_archive_root=tmp_path / "live_archive",
    )

    first = live_service.snapshot(source="live")
    second_payload = json.loads(json.dumps(first_payload))
    second_payload["generated_at"] = "2026-07-28T15:09:00+08:00"
    second_payload["scanned_at"] = "2026-07-28T15:09:00+08:00"
    second_payload["scan_audit"]["batch_duration_ms"] = 900
    second_payload["scan_audit"]["coverage_cycle_batch_count"] = 4
    second_payload["coverage_manifest"]["batch_count"] = 4
    second_payload["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        second_payload
    )
    assert sha256_json(second_payload) != sha256_json(first_payload)
    assert (
        second_payload["snapshot_content_sha256"]
        == first_payload["snapshot_content_sha256"]
    )
    live_path.write_text(
        json.dumps(second_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    second = live_service.snapshot(source="live")

    assert second["source_content_sha256"] == first["source_content_sha256"]
    assert [row["candidate_id"] for row in second["review_queue"]] == [
        row["candidate_id"] for row in first["review_queue"]
    ]
    assert len(tuple((tmp_path / "live_archive").glob("*/*.json"))) == 1


def test_large_live_materialization_reuses_exact_child_receipt(
    service: HumanReviewScreeningService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_path = tmp_path / "trading_screening_snapshot.json"
    live_path.write_text(
        json.dumps(live_snapshot(), ensure_ascii=False),
        encoding="utf-8",
    )
    archive_root = tmp_path / "live_archive"
    live_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        live_screening_snapshot=live_path,
        live_archive_root=archive_root,
    )
    first = live_service.snapshot(source="live")
    report_path = next(archive_root.glob("*/*.json")).resolve()
    source_stat = live_path.stat()
    report_stat = report_path.stat()
    source_hash = live_snapshot()["snapshot_content_sha256"]
    report_hash = first["source_content_sha256"]
    receipt = live_review_materialization_receipt(
        source_path=live_path,
        source_stat=source_stat,
        source_snapshot_content_sha256=source_hash,
        report_path=report_path,
        report_stat=report_stat,
        report_content_sha256=report_hash,
        decision_source_snapshot_id=first["decision_source_snapshot_id"],
        archive_root=archive_root,
    )
    (archive_root / ".current_live_review.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        human_review_screening_subject,
        "validate_live_review_snapshot",
        lambda *_args, **_kwargs: pytest.fail(
            "receipt fast path reopened the full live snapshot"
        ),
    )

    assert live_service._materialize_live_report() == report_path


def test_incomplete_live_epoch_keeps_last_archive_and_does_not_block_forward(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "trading_screening_snapshot.json"
    live_path.write_text(
        json.dumps(live_snapshot(), ensure_ascii=False),
        encoding="utf-8",
    )
    forward = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    _write_report(forward, forward_session="2026-07-28")
    live_service = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        live_screening_snapshot=live_path,
        live_archive_root=tmp_path / "live_archive",
    )

    promoted = live_service.snapshot(source="live")
    assert promoted["source_kind"] == "live"
    assert len(tuple((tmp_path / "live_archive").glob("*/*.json"))) == 1

    incomplete = live_snapshot()
    incomplete["scan_state"] = "scanning"
    incomplete["scan_audit"]["coverage_cycle_complete"] = False
    incomplete["scan_audit"]["pending_symbol_count"] = 1
    incomplete["coverage_manifest"]["complete"] = False
    incomplete["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
        incomplete
    )
    live_path.write_text(
        json.dumps(incomplete, ensure_ascii=False),
        encoding="utf-8",
    )

    # The mutable, incomplete epoch is never archived or shown.  The latest
    # verified live object stays available, while an explicitly selected
    # forward source remains independent from the live source altogether.
    latest = live_service.snapshot(source="latest")
    assert latest["source_kind"] == "live"
    assert latest["source_content_sha256"] == promoted["source_content_sha256"]
    assert len(tuple((tmp_path / "live_archive").glob("*/*.json"))) == 1
    assert live_service.snapshot(source="forward")["source_kind"] == "forward"


def test_older_forward_session_is_review_only_when_current_market_is_newer(
    service: HumanReviewScreeningService,
    tmp_path: Path,
) -> None:
    """An immutable old report stays reviewable but cannot create a new intent."""

    live_path = tmp_path / "trading_screening_snapshot.json"
    current = live_snapshot()
    live_path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    current_session = datetime.fromisoformat(str(current["market_data_as_of"])).date()
    source_session = current_session - timedelta(days=1)
    forward = (
        service.forward_root
        / "sessions"
        / source_session.isoformat()
        / "forward_human_review_screen.json"
    )
    _write_report(forward, forward_session=source_session.isoformat())
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        live_screening_snapshot=live_path,
        live_archive_root=tmp_path / "live_archive",
    )

    snapshot = guarded.snapshot(source="forward")
    assert snapshot["source_kind"] == "forward"
    assert snapshot["paper_observation_eligible"] is False
    assert snapshot["paper_observation_reason"] == (
        "SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER"
    )
    assert snapshot["paper_observation_source_session"] == source_session.isoformat()
    assert snapshot["paper_observation_current_market_session"] == (
        current_session.isoformat()
    )

    candidate = snapshot["review_queue"][0]
    result = guarded.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-stale-session",
        reviewed_at=datetime(2026, 7, 20, 12, 0, tzinfo=TZ),
        request_id="stale-session-review-1",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "disposition": "PAPER_OBSERVE",
            "notes": "旧会话只留存人工识别",
        },
    )
    assert result["paper_observation_eligible"] is False
    assert result["paper_observation_reason"] == (
        "SOURCE_MARKET_SESSION_NOT_CURRENT_FOR_PAPER"
    )
    assert result["paper_intent"] is None
    assert result["paper_ledger_changed"] is False
    assert not guarded.paper_ledger.exists()


def test_chart_lock_rejects_forged_time_or_identity(
    service: HumanReviewScreeningService,
) -> None:
    snapshot = service.snapshot()
    candidate = snapshot["review_queue"][0]

    lock = service.validate_chart_lock(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        review_as_of=candidate["review_as_of_unix"],
    )
    assert lock["symbol"] == SYMBOL
    with pytest.raises(HumanReviewScreenUnavailable, match="chart_lock_mismatch"):
        service.validate_chart_lock(
            candidate_id=candidate["candidate_id"],
            source_sha256=snapshot["source_content_sha256"],
            review_as_of=candidate["review_as_of_unix"] + 60,
        )


def test_historical_feedback_is_hash_chained_but_never_creates_paper_intent(
    service: HumanReviewScreeningService,
) -> None:
    snapshot = service.snapshot()
    candidate = snapshot["review_queue"][0]
    result = service.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-1",
        reviewed_at=datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
        request_id="review-request-1",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "decomposition_judgement": "COMBINED",
            "center_expansion_judgement": "REJECTED",
            "nine_segment_upgrade_judgement": "CONFIRMED",
            "locator_judgement": "CONFIRMED",
            "disposition": "PAPER_OBSERVE",
            "notes": "人工确认后仅进入模拟观察。",
        },
    )
    retry = service.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-1",
        reviewed_at=datetime(2026, 7, 28, 12, 5, tzinfo=TZ),
        request_id="review-request-1",
        values={
            "center_judgement": "CONFIRMED",
            "trend_judgement": "UP",
            "level_judgement": "30M",
            "point_judgement": "BUY_3",
            "decomposition_judgement": "COMBINED",
            "center_expansion_judgement": "REJECTED",
            "nine_segment_upgrade_judgement": "CONFIRMED",
            "locator_judgement": "CONFIRMED",
            "disposition": "PAPER_OBSERVE",
            "notes": "人工确认后仅进入模拟观察。",
        },
    )

    assert result["automated_order_authorized"] is False
    assert result["paper_intent"] is None
    assert result["paper_ledger_content_sha256"] is None
    assert result["paper_ledger_changed"] is False
    assert result["paper_observation_eligible"] is False
    assert result["paper_observation_reason"] == "HISTORICAL_SOURCE_REVIEW_ONLY"
    assert result["broker_transport_available"] is False
    ledger = load_human_review_feedback_ledger(service.feedback_ledger)
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["candidate_id"] == candidate["candidate_id"]
    assert ledger["entries"][0]["request_id"] == "review-request-1"
    assert (
        ledger["entries"][0]["signal_lifecycle_id"] == candidate["signal_lifecycle_id"]
    )
    assert ledger["entries"][0]["decomposition_judgement"] == "COMBINED"
    assert ledger["entries"][0]["center_expansion_judgement"] == "REJECTED"
    assert ledger["entries"][0]["nine_segment_upgrade_judgement"] == ("CONFIRMED")
    assert ledger["entries"][0]["locator_judgement"] == "CONFIRMED"
    assert retry["feedback"]["feedback_id"] == result["feedback"]["feedback_id"]
    assert retry["paper_intent"] is None
    assert not service.paper_ledger.exists()
    refreshed = service.snapshot()
    assert refreshed["reviewed_candidate_count"] == 1
    assert refreshed["virtual_intent_count"] == 0
    assert refreshed["virtual_fill_count"] == 0
    assert refreshed["virtual_pending_intent_count"] == 0
    assert refreshed["virtual_blocked_intent_count"] == 0
    assert refreshed["virtual_observation_only_intent_count"] == 0
    assert refreshed["virtual_open_position_count"] == 0
    assert refreshed["virtual_open_positions"] == {}
    assert (
        refreshed["review_queue"][0]["latest_feedback"]["disposition"]
        == "PAPER_OBSERVE"
    )
    assert refreshed["review_queue"][0]["paper_reconciliation_pending"] is False
    assert refreshed["review_queue"][0]["paper_reconciliation_eligible"] is False


def test_feedback_cannot_predate_candidate_review_availability(
    service: HumanReviewScreeningService,
) -> None:
    snapshot = service.snapshot()
    candidate = snapshot["review_queue"][0]
    available = datetime.fromisoformat(candidate["review_available_at"])

    with pytest.raises(HumanReviewScreenUnavailable, match="feedback_invalid"):
        service.append_feedback(
            candidate_id=candidate["candidate_id"],
            source_sha256=snapshot["source_content_sha256"],
            reviewer="reviewer-1",
            reviewed_at=available - timedelta(seconds=1),
            request_id="backdated-review",
            values={
                "center_judgement": "CONFIRMED",
                "trend_judgement": "UP",
                "level_judgement": "30M",
                "point_judgement": "BUY_3",
                "disposition": "PAPER_OBSERVE",
            },
        )
    assert not service.feedback_ledger.exists()
    assert not service.paper_ledger.exists()


def test_feedback_cannot_be_future_dated_by_a_direct_service_caller(
    service: HumanReviewScreeningService,
) -> None:
    observed_at = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: observed_at,
        trading_session_provider=_trading_session_provider,
    )
    snapshot = guarded.snapshot(source="historical")
    candidate = snapshot["review_queue"][0]

    with pytest.raises(HumanReviewScreenUnavailable, match="feedback_invalid"):
        guarded.append_feedback(
            candidate_id=candidate["candidate_id"],
            source_sha256=snapshot["source_content_sha256"],
            reviewer="reviewer-future",
            reviewed_at=observed_at + timedelta(microseconds=1),
            request_id="future-review",
            values={
                "center_judgement": "CONFIRMED",
                "trend_judgement": "UP",
                "level_judgement": "30M",
                "point_judgement": "BUY_3",
                "disposition": "PAPER_OBSERVE",
            },
        )
    assert not guarded.feedback_ledger.exists()
    assert not guarded.paper_ledger.exists()


def test_snapshot_rejects_a_preexisting_future_dated_feedback_ledger(
    service: HumanReviewScreeningService,
) -> None:
    observed_at = datetime(2026, 7, 28, 12, 0, tzinfo=TZ)
    alert = _alert()
    append_human_review_feedback(
        service.feedback_ledger,
        HumanReviewFeedback(
            candidate_id=alert.candidate_id,
            source_screen_content_sha256=str(_report()["content_sha256"]),
            reviewer="preexisting-future-writer",
            reviewed_at=observed_at + timedelta(microseconds=1),
            center_judgement="CONFIRMED",
            trend_judgement="UP",
            level_judgement="30M",
            point_judgement="BUY_3",
            disposition="WATCH",
            signal_lifecycle_id=alert.signal_lifecycle_id,
        ),
    )
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: observed_at,
        trading_session_provider=_trading_session_provider,
    )

    with pytest.raises(
        HumanReviewScreenUnavailable,
        match="feedback_ledger_invalid",
    ):
        guarded.snapshot(source="historical")
    assert not guarded.paper_ledger.exists()


def test_scheduler_failure_keeps_feedback_but_blocks_and_recovers_paper_intent(
    service: HumanReviewScreeningService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken daily delivery path must not create an unserviceable intent."""

    alert = replace(
        _alert(),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
    )
    report = _report(forward_session="2026-07-28")
    report["review_queue"] = [
        {
            **_jsonable(human_review_alert_document(alert)),
            "candidate_id": alert.candidate_id,
            "signal_lifecycle_id": alert.signal_lifecycle_id,
        }
    ]
    report.pop("content_sha256")
    report["content_sha256"] = sha256_json(report)
    forward_report = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    forward_report.parent.mkdir(parents=True, exist_ok=True)
    forward_report.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )
    common = {
        "repository_root": service.repository_root,
        "historical_report": service.historical_report,
        "forward_root": service.forward_root,
        "feedback_ledger": service.feedback_ledger,
        "sector_ledger": service.sector_ledger,
        "paper_ledger": service.paper_ledger,
        "parameter_snapshot": service.parameter_snapshot,
        "clock": lambda: datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        "sector_capture_due": time(9, 10),
        "trading_session_provider": _trading_session_provider,
    }
    scheduler_refresh_flags: list[bool] = []

    def broken_scheduler(*, force_refresh: bool = False) -> dict[str, object]:
        scheduler_refresh_flags.append(force_refresh)
        return _forward_scheduler_observation(ready=False)

    blocked = HumanReviewScreeningService(
        **common,
        forward_scheduler_provider=broken_scheduler,
    )
    snapshot = blocked.snapshot(source="forward")
    assert scheduler_refresh_flags == [False]
    assert snapshot["paper_observation_eligible"] is False
    assert snapshot["paper_observation_reason"] == (
        "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"
    )
    candidate = snapshot["review_queue"][0]
    values = {
        "center_judgement": "CONFIRMED",
        "trend_judgement": "UP",
        "level_judgement": "30M",
        "point_judgement": "BUY_3",
        "disposition": "PAPER_OBSERVE",
        "notes": "任务未就绪时只留人工反馈",
    }
    first = blocked.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-scheduler-gate",
        reviewed_at=datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        request_id="scheduler-gated-review",
        values=values,
    )
    assert first["paper_intent"] is None
    assert first["paper_ledger_changed"] is False
    assert scheduler_refresh_flags == [False, True]
    assert service.feedback_ledger.is_file()
    assert not service.paper_ledger.exists()
    blocked_after_review = blocked.snapshot(source="forward")
    blocked_candidate = blocked_after_review["review_queue"][0]
    assert blocked_candidate["paper_reconciliation_pending"] is True
    assert blocked_candidate["paper_reconciliation_eligible"] is False

    # Repairing only the scheduler is insufficient.  Before the 09:10 Capture
    # has produced immutable same-session sector evidence, screening and the
    # hash-chained feedback remain available but no virtual intent may exist.
    recovered = HumanReviewScreeningService(
        **common,
        forward_scheduler_provider=lambda **_kwargs: _forward_scheduler_observation(
            ready=True
        ),
    )
    still_waiting = recovered.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-scheduler-gate",
        reviewed_at=datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        request_id="scheduler-gated-review",
        values=values,
    )
    assert still_waiting["feedback"]["feedback_id"] == first["feedback"]["feedback_id"]
    assert still_waiting["paper_intent"] is None
    assert still_waiting["paper_ledger_changed"] is False
    assert still_waiting["paper_observation_reason"] == (
        "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER"
    )
    assert not service.paper_ledger.exists()

    # The exact same idempotent feedback can be promoted after Capture.  Stub
    # only the already-audited archive surface; intent construction and ledger
    # reconciliation still run through the production decision core.
    monkeypatch.setattr(
        recovered,
        "forward_archive_capture_readiness",
        lambda *, session: {
            "ready": session == date(2026, 7, 28),
            "receipt_proven": True,
        },
    )
    ready_to_reconcile = recovered.snapshot(source="forward")
    ready_candidate = ready_to_reconcile["review_queue"][0]
    assert ready_candidate["paper_reconciliation_pending"] is True
    assert ready_candidate["paper_reconciliation_eligible"] is True
    retry = recovered.append_feedback(
        candidate_id=candidate["candidate_id"],
        source_sha256=snapshot["source_content_sha256"],
        reviewer="reviewer-scheduler-gate",
        reviewed_at=datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        request_id="scheduler-gated-review",
        values=values,
    )
    assert retry["feedback"]["feedback_id"] == first["feedback"]["feedback_id"]
    assert retry["paper_intent"]["status"] == "PENDING"
    assert retry["paper_ledger_changed"] is True
    reconciled = recovered.snapshot(source="forward")["review_queue"][0]
    assert reconciled["paper_reconciliation_pending"] is False
    assert reconciled["paper_reconciliation_eligible"] is False


def test_scheduler_probe_exception_fails_virtual_paper_gate_closed(
    service: HumanReviewScreeningService,
) -> None:
    def unavailable_scheduler(**_kwargs) -> dict[str, object]:
        raise RuntimeError("read-only scheduler probe unavailable")

    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        sector_capture_due=time(9, 10),
        trading_session_provider=_trading_session_provider,
        forward_scheduler_provider=unavailable_scheduler,
    )

    ready, reason = guarded._paper_forward_operations_eligibility(
        source_session=date(2026, 7, 28)
    )

    assert ready is False
    assert reason == "FORWARD_SCHEDULER_NOT_READY_FOR_PAPER"


def test_stale_ready_scheduler_observation_fails_virtual_paper_gate_closed(
    service: HumanReviewScreeningService,
) -> None:
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        clock=lambda: datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        sector_capture_due=time(9, 10),
        trading_session_provider=_trading_session_provider,
        forward_scheduler_provider=(
            lambda **_kwargs: _forward_scheduler_observation(
                ready=True,
                observed_at="2026-07-28T09:03:29+08:00",
            )
        ),
    )

    ready, reason = guarded._paper_forward_operations_eligibility(
        source_session=date(2026, 7, 28)
    )

    assert ready is False
    assert reason == "FORWARD_SCHEDULER_OBSERVATION_STALE_FOR_PAPER"


def test_paper_intent_always_requires_same_session_capture_receipt(
    service: HumanReviewScreeningService,
) -> None:
    alert = replace(
        _alert(),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
    )
    report = _report(forward_session="2026-07-28")
    report["review_queue"] = [
        {
            **_jsonable(human_review_alert_document(alert)),
            "candidate_id": alert.candidate_id,
            "signal_lifecycle_id": alert.signal_lifecycle_id,
        }
    ]
    report.pop("content_sha256")
    report["content_sha256"] = sha256_json(report)
    forward_report = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    forward_report.parent.mkdir(parents=True, exist_ok=True)
    forward_report.write_text(json.dumps(report), encoding="utf-8")
    guarded = HumanReviewScreeningService(
        repository_root=service.repository_root,
        historical_report=service.historical_report,
        forward_root=service.forward_root,
        feedback_ledger=service.feedback_ledger,
        sector_ledger=service.sector_ledger,
        paper_ledger=service.paper_ledger,
        parameter_snapshot=service.parameter_snapshot,
        # Before the scheduled 09:10 Capture, selection remains usable but
        # paper intent creation must already be fail-closed.
        clock=lambda: datetime(2026, 7, 28, 9, 5, tzinfo=TZ),
        sector_capture_due=time(9, 10),
        trading_session_provider=_trading_session_provider,
        forward_scheduler_provider=lambda **_kwargs: _forward_scheduler_observation(
            ready=True,
            observed_at="2026-07-28T09:05:00+08:00",
        ),
    )

    snapshot = guarded.snapshot(source="forward")

    assert snapshot["paper_observation_eligible"] is False
    assert snapshot["paper_observation_reason"] == (
        "SAME_SESSION_FORWARD_CAPTURE_NOT_READY_FOR_PAPER"
    )
    assert not service.paper_ledger.exists()


def test_later_human_feedback_supersedes_pending_virtual_intent(
    service: HumanReviewScreeningService,
) -> None:
    alert = replace(
        _alert(),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
    )
    report = _report(forward_session="2026-07-28")
    report["review_queue"] = [
        {
            **_jsonable(human_review_alert_document(alert)),
            "candidate_id": alert.candidate_id,
            "signal_lifecycle_id": alert.signal_lifecycle_id,
        }
    ]
    report.pop("content_sha256")
    report["content_sha256"] = sha256_json(report)
    forward_report = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    forward_report.parent.mkdir(parents=True, exist_ok=True)
    forward_report.write_text(
        json.dumps(report, ensure_ascii=False),
        encoding="utf-8",
    )
    snapshot = service.snapshot(source="forward")
    assert snapshot["paper_observation_eligible"] is True
    assert snapshot["paper_observation_reason"] is None
    candidate = snapshot["review_queue"][0]
    common = {
        "candidate_id": candidate["candidate_id"],
        "source_sha256": snapshot["source_content_sha256"],
        "reviewer": "reviewer-1",
    }
    confirmed = {
        "center_judgement": "CONFIRMED",
        "trend_judgement": "UP",
        "level_judgement": "30M",
        "point_judgement": "BUY_3",
        "disposition": "PAPER_OBSERVE",
        "notes": "先建立虚拟观察",
    }
    first = service.append_feedback(
        **common,
        values=confirmed,
        reviewed_at=datetime(2026, 7, 28, 12, 5, tzinfo=TZ),
        request_id="paper-pending-1",
    )
    assert first["paper_intent"]["status"] == "PENDING"
    assert first["superseded_paper_intents"] == []

    watch = {**confirmed, "disposition": "WATCH", "notes": "人工改判，撤销待成交"}
    second = service.append_feedback(
        **common,
        values=watch,
        reviewed_at=datetime(2026, 7, 28, 12, 10, tzinfo=TZ),
        request_id="paper-cancel-1",
    )
    retry = service.append_feedback(
        **common,
        values=watch,
        reviewed_at=datetime(2026, 7, 28, 12, 10, tzinfo=TZ),
        request_id="paper-cancel-1",
    )

    assert second["paper_intent"] is None
    assert len(second["superseded_paper_intents"]) == 1
    assert second["superseded_paper_intents"][0]["status"] == "CANCELLED"
    assert retry["paper_ledger_changed"] is False
    paper = load_human_paper_ledger(service.paper_ledger)
    assert [event["kind"] for event in paper["events"]] == ["INTENT", "CANCEL"]
    refreshed = service.snapshot(source="forward")
    assert refreshed["virtual_pending_intent_count"] == 0
    assert refreshed["virtual_cancelled_intent_count"] == 1
    assert refreshed["paper_pending_continuity"]["status"] == ("NO_PENDING_INTENTS")


def test_feedback_request_id_cannot_be_reused_for_different_values(
    service: HumanReviewScreeningService,
) -> None:
    snapshot = service.snapshot()
    candidate = snapshot["review_queue"][0]
    common = {
        "candidate_id": candidate["candidate_id"],
        "source_sha256": snapshot["source_content_sha256"],
        "reviewer": "reviewer-1",
        "reviewed_at": datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
        "request_id": "review-request-conflict",
    }
    values = {
        "center_judgement": "CONFIRMED",
        "trend_judgement": "UP",
        "level_judgement": "30M",
        "point_judgement": "BUY_3",
        "disposition": "WATCH",
        "notes": "first",
    }
    service.append_feedback(**common, values=values)

    with pytest.raises(HumanReviewScreenUnavailable, match="request_conflict"):
        service.append_feedback(
            **common,
            values={**values, "disposition": "REJECT"},
        )


def test_tampered_or_order_authorizing_report_is_rejected(
    service: HumanReviewScreeningService,
) -> None:
    payload = json.loads(service.historical_report.read_text(encoding="utf-8"))
    payload["orders_created"] = 1
    stable = dict(payload)
    stable.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable)
    service.historical_report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HumanReviewScreenUnavailable, match="boundary_invalid"):
        service.snapshot(source="historical")


def test_candidate_warmup_sidecar_is_hash_bound_and_presentation_only(
    tmp_path: Path,
) -> None:
    service = _forward_ranked_service(tmp_path)
    report_path = (
        service.forward_root
        / "sessions"
        / "2026-07-28"
        / "forward_human_review_screen.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("content_sha256")
    source_identity = "sha256:" + "d" * 64
    report["input_hashes"] = {"live_screening_snapshot": source_identity}
    report["content_sha256"] = sha256_json(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    as_of = datetime(2026, 7, 28, 15, 0, tzinfo=TZ)
    parameters = candidate_warmup_parameter_document()
    rows = []
    for frequency in parameters["frequencies"]:
        envelope = classify_warmup_convergence_envelope(
            frequency=str(frequency),
            as_of=as_of,
            parameter_set_id="sha256:" + "e" * 64,
            observations=tuple(
                WarmupPrefixObservation(
                    bar_count=count,
                    starts_at=as_of - timedelta(days=count),
                    signature_sha256="sha256:" + "f" * 64,
                )
                for count in (480, 960, 1440)
            ),
        )
        rows.append(
            {
                "code": SYMBOL,
                "frequency": frequency,
                "source": "qmt_local_completed_kline",
                "available_bar_count": 1600,
                "market_data_as_of": as_of.isoformat(),
                "envelope": envelope.document(),
                "semantic_diagnostic": None,
                "mapping_supply_diagnostic": None,
                "structure_lineage_diagnostic": None,
            }
        )
    diagnostic = build_candidate_warmup_diagnostic_document(
        source_content_sha256=source_identity,
        source_wrapper_content_sha256=None,
        requested_as_of=as_of,
        selected_candidates=(
            {
                "rank": 1,
                "code": SYMBOL,
                "source_position": 0,
                "lifecycle_stage": "approaching",
                "sector_horizontal_rank": 1,
                "point_type": "1buy",
                "selection_profile": "MODERN_BUY_REVIEW_ORDER",
            },
        ),
        rows=rows,
        errors=(),
        parameter_document=parameters,
    )
    sidecar = candidate_warmup_diagnostic_path(
        service.forward_root,
        source_content_sha256=source_identity,
        parameter_set_id=str(diagnostic["diagnostic_parameter_set_id"]),
    )
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(diagnostic), encoding="utf-8")

    snapshot = service.snapshot(source="forward")
    candidate = snapshot["review_queue"][0]

    assert (
        candidate["candidate_id"]
        == _live_ranked_alert(
            catalog_revision=json.loads(
                service.sector_ledger.read_text(encoding="utf-8")
            )["entries"][0]["catalog_revision"]
        ).candidate_id
    )
    assert candidate["deep_warmup_diagnostic"]["status"] == "AVAILABLE"
    assert len(candidate["deep_warmup_diagnostic"]["frequencies"]) == 4
    assert snapshot["candidate_warmup_diagnostic"]["status"] == "COMPLETE"
    assert (
        snapshot["candidate_warmup_diagnostic"]["ranking_parameters_unchanged"] is True
    )
    assert (
        snapshot["candidate_warmup_diagnostic"][
            "paper_observation_eligibility_unchanged"
        ]
        is True
    )
