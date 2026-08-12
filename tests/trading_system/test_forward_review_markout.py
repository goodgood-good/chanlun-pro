from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    DECISION_SOURCE_SNAPSHOT_SCHEMA,
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_completed_one_minute_closes,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.trading_system.forward_review_markout import (
    FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
    FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID,
    ForwardReviewSample,
    build_forward_review_markout as _build_forward_review_markout,
    review_price_bars_revision,
    select_first_strategic_buy_samples,
    validate_forward_review_markout_document,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HumanReviewAlert,
    ReviewPriceBar,
    human_review_screening_parameters,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
)


CN = ZoneInfo("Asia/Shanghai")
D0 = date(2026, 7, 1)
POLICY_A = "sha256:" + "a" * 64
POLICY_B = "sha256:" + "b" * 64
CORE_A = "sha256:" + "c" * 64


def _session_qualification(
    samples: Sequence[ForwardReviewSample],
    *,
    through_session: date,
    excluded: Sequence[tuple[date, str]] = (),
) -> dict[str, object]:
    qualified = sorted({sample.source_session for sample in samples})
    observed_at = datetime(
        through_session.year,
        through_session.month,
        through_session.day,
        15,
        20,
        tzinfo=CN,
    )
    qualified_evidence = []
    for index, session in enumerate(qualified, start=1):
        implementation_id = "sha256:" + "a" * 64
        capture_id = "sha256:" + format(index % 15 + 1, "x") * 64
        data_ready_id = "sha256:" + format((index + 1) % 15 + 1, "x") * 64
        evaluated_id = "sha256:" + format((index + 2) % 15 + 1, "x") * 64
        delivery = {
            "schema": "chanlun-forward-paper-session-delivery",
            "session": session.isoformat(),
            "observed_at": observed_at.isoformat(),
            "required": True,
            "requirement_resolved": True,
            "trading_session_evidence_proven": True,
            "ready": True,
            "status": "ready",
            "reason_code": "READY",
            "capture_event_present": True,
            "data_ready_event_present": True,
            "evaluation_event_present": True,
            "capture_ready": True,
            "evaluation_ready": True,
            "capture_evidence_proven": True,
            "data_ready_evidence_proven": True,
            "evaluation_artifacts_proven": True,
            "implementation_provenance_present": True,
            "implementation_provenance_proven": True,
            "capture_implementation_provenance_id": implementation_id,
            "data_ready_implementation_provenance_id": implementation_id,
            "evaluation_implementation_provenance_id": implementation_id,
            "capture_event_sha256": capture_id,
            "data_ready_event_sha256": data_ready_id,
            "evaluation_event_sha256": evaluated_id,
            "latest_terminal_event_status": "EVALUATED",
            "latest_terminal_event_sha256": evaluated_id,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "paper_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }
        qualified_evidence.append(
            {
                "session": session.isoformat(),
                "delivery_audit": delivery,
                "delivery_audit_content_sha256": sha256_json(delivery),
            }
        )
    stable: dict[str, object] = {
        "schema": "chanlun-forward-review-session-qualification",
        "through_session": through_session.isoformat(),
        "observed_at": observed_at.isoformat(),
        "qualified_sessions": [value.isoformat() for value in qualified],
        "qualified_session_evidence": qualified_evidence,
        "excluded_sessions": [
            {"session": session.isoformat(), "reason_code": reason}
            for session, reason in excluded
        ],
        "qualified_session_count": len(qualified),
        "excluded_session_count": len(excluded),
        "current_session_excluded_until_terminal_event": True,
        "forward_ledger_content_sha256": "sha256:" + "f" * 64,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def build_forward_review_markout(
    samples: Sequence[ForwardReviewSample],
    *,
    through_session: date,
    trading_sessions: Sequence[date],
    bars_by_symbol: Mapping[str, Sequence[ReviewPriceBar]],
    source_audits: Mapping[str, Mapping[str, object]] | None = None,
    feedback: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return _build_forward_review_markout(
        samples,
        through_session=through_session,
        trading_sessions=trading_sessions,
        bars_by_symbol=bars_by_symbol,
        source_session_qualification=_session_qualification(
            samples,
            through_session=through_session,
        ),
        source_audits=source_audits,
        feedback=feedback,
    )


def _future_sessions(count: int) -> tuple[date, ...]:
    output = []
    cursor = D0
    while len(output) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            output.append(cursor)
    return tuple(output)


def _complete_session_bars(
    session: date,
    close: Decimal,
) -> list[ReviewPriceBar]:
    return [
        ReviewPriceBar(
            observed_at=value,
            high=close + Decimal("0.2"),
            low=close - Decimal("0.2"),
            close=close,
        )
        for value in a_share_completed_one_minute_closes(session)
    ]


def _causal_reference_prefix(
    session: date,
    *,
    cutoff: datetime,
    close: Decimal,
) -> list[ReviewPriceBar]:
    return [
        ReviewPriceBar(
            observed_at=value,
            high=close + Decimal("0.1"),
            low=close - Decimal("0.1"),
            close=close,
        )
        for value in a_share_completed_one_minute_closes(session)
        if value <= cutoff
    ]


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _source_audit(
    *,
    symbol: str,
    bars: list[ReviewPriceBar],
    through_session: date,
    events: tuple[QmtCausalFactorEvent, ...],
) -> dict[str, object]:
    ordered = tuple(sorted(bars, key=lambda value: value.observed_at))
    return {
        "status": "AVAILABLE",
        "source_audit_contract_id": FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID,
        "row_count": len(ordered),
        "raw_row_count": len(ordered),
        "normalized_row_count": len(ordered),
        "first_at": ordered[0].observed_at.isoformat(),
        "last_at": ordered[-1].observed_at.isoformat(),
        "transport": "TEST_READ_ONLY",
        "opening_event_normalization": QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
        "price_adjustment": "CAUSAL_KNOWN_EX_DATE_RAW_PRICE_DIVISOR",
        "factor_contract_id": QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
        "factor_known_through": through_session.isoformat(),
        "factor_event_count": len(events),
        "factor_revision": qmt_causal_factor_revision(
            members=(symbol,),
            events_by_code={symbol: events},
            known_through=through_session,
        ),
        "factor_events": tuple(event.canonical_payload() for event in events),
        "raw_bar_revision": "sha256:" + "f" * 64,
        "adjusted_bar_revision": review_price_bars_revision(
            symbol=symbol,
            through_session=through_session,
            bars=ordered,
        ),
    }


def _alert(*, session: date, snapshot: str) -> HumanReviewAlert:
    parameters = human_review_screening_parameters()
    at = datetime(session.year, session.month, session.day, 10, tzinfo=CN)
    return HumanReviewAlert(
        symbol="SH.600000",
        alert_type="POSSIBLE_30M_BUY",
        signal_at=at,
        review_available_at=at,
        source_point_id="sha256:" + "1" * 64,
        structure_snapshot_id=snapshot,
        sector_id="qmt-gics3:test",
        confidence="LOW",
        review_priority=10,
        reference_price=None,
        structural_invalidation_price=Decimal("9"),
        market_risk_gate="AMBER",
        sector_risk_gate="AMBER",
        symbol_risk_gate="AMBER",
        warning_codes=("HIGHER_TIMEFRAME_GATE_NOT_GREEN",),
        source_fact_ids=(snapshot,),
        screening_parameter_set_id=parameters.parameter_set_id,
        signal_alignment_parameter_set_id=(
            parameters.signal_alignment_parameter_set_id
        ),
    )


def _report(
    session: date,
    alert: HumanReviewAlert,
    *,
    screening_policy_id: str = POLICY_A,
    decision_core_id: str | None = None,
    decision_source_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if decision_core_id is None:
        decision_core_id = HumanAssistedDecisionCore().contract_id
    if decision_source_snapshot is None:
        decision_source_snapshot = _decision_source_snapshot("0")
    input_hashes: dict[str, object] = {
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "decision_source_snapshot_id": decision_source_snapshot_id(
            decision_source_snapshot
        ),
    }
    stable = {
        "schema": "chanlun-human-review-screen",
        "forward_paper_session": session.isoformat(),
        "review_queue": [
            {
                **_jsonable(asdict(alert)),  # type: ignore[arg-type]
                "candidate_id": alert.candidate_id,
                "signal_lifecycle_id": alert.signal_lifecycle_id,
            }
        ],
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
        "input_hashes": input_hashes,
        "decision_source_snapshot": decision_source_snapshot,
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _decision_source_snapshot(digest_digit: str) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": DECISION_SOURCE_SNAPSHOT_SCHEMA,
        "files": (
            {
                "path": "src/chanlun/example.py",
                "sha256": "sha256:" + digest_digit * 64,
            },
        ),
    }
    return {**stable, "aggregate_sha256": sha256_json(stable)}


def test_markout_uses_first_lifecycle_appearance_and_keeps_risk_blocked() -> None:
    first_alert = _alert(session=D0, snapshot="sha256:" + "2" * 64)
    later_alert = _alert(
        session=D0 + timedelta(days=1),
        snapshot="sha256:" + "3" * 64,
    )
    first_report = _report(D0, first_alert)
    later_report = _report(D0 + timedelta(days=1), later_alert)
    samples = select_first_strategic_buy_samples(
        (
            (D0 + timedelta(days=1), later_report["content_sha256"], later_report),
            (D0, first_report["content_sha256"], first_report),
        ),
        through_session=D0 + timedelta(days=5),
    )

    assert len(samples) == 1
    assert samples[0].alert.candidate_id == first_alert.candidate_id
    assert samples[0].source_session == D0

    bars = _causal_reference_prefix(
        D0,
        cutoff=first_alert.review_available_at,
        close=Decimal("10"),
    )
    sessions = _future_sessions(5)
    for offset, session in enumerate(sessions, start=1):
        bars.extend(_complete_session_bars(session, Decimal(10 + offset)))
    report = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={"SH.600000": bars},
        feedback=(
            {
                "candidate_id": later_alert.candidate_id,
                "signal_lifecycle_id": later_alert.signal_lifecycle_id,
                "reviewed_at": "2026-07-02T12:00:00+08:00",
                "feedback_id": "sha256:" + "4" * 64,
                "disposition": "PAPER_OBSERVE",
                "point_judgement": "BUY_3",
            },
        ),
    )

    assert report["portfolio_performance_evaluable"] is False
    assert report["orders_created"] == report["fills_created"] == 0
    assert report["sample"]["unique_lifecycle_count"] == 1
    assert report["sample"]["feedback_linked_lifecycle_count"] == 1
    assert report["sample"]["eligible_by_horizon"] == {"5": 1, "10": 0, "20": 0}
    assert report["summary_by_risk_class"]["BLOCKED"]["5"]["eligible_count"] == 1
    five = next(row for row in report["observations"] if row["horizon_sessions"] == 5)
    assert five["risk_class"] == "BLOCKED"
    assert five["close_return"] == "0.5"
    assert five["source_structure_anchor_price"] is None
    assert five["latest_human_disposition"] == "PAPER_OBSERVE"
    assert "SCREENING_MARKOUT_IS_NOT_A_TRADE_RETURN" in report["reason_codes"]
    assert "STRATEGIC_MARKOUT_SAMPLE_INSUFFICIENT" in report["reason_codes"]


def test_markout_names_the_three_independent_green_gates_exactly() -> None:
    alert = replace(
        _alert(session=D0, snapshot="sha256:" + "9" * 64),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
    )
    report_source = _report(D0, alert)
    samples = select_first_strategic_buy_samples(
        ((D0, report_source["content_sha256"], report_source),),
        through_session=D0 + timedelta(days=1),
    )

    report = build_forward_review_markout(
        samples,
        through_session=D0 + timedelta(days=1),
        trading_sessions=(),
        bars_by_symbol={},
    )

    assert report["schema"] == "chanlun-forward-review-markout"
    assert report["sample_cohort_contract"] == (
        FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID
    )
    assert report["risk_gate_contract"] == "MARKET_SECTOR_SYMBOL_ALL_GREEN"
    assert "ALL_GREEN" in report["summary_by_risk_class"]
    assert "BOTH_GREEN" not in report["summary_by_risk_class"]


def test_markout_binds_delivery_qualification_and_rejects_rehashed_forgery() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "7" * 64)
    source = _report(D0, alert)
    through_session = D0 + timedelta(days=1)
    samples = select_first_strategic_buy_samples(
        ((D0, source["content_sha256"], source),),
        through_session=through_session,
    )
    unqualified = _session_qualification((), through_session=through_session)
    with pytest.raises(ValueError, match="unqualified delivery session"):
        _build_forward_review_markout(
            samples,
            through_session=through_session,
            trading_sessions=(),
            bars_by_symbol={},
            source_session_qualification=unqualified,
        )

    qualification = _session_qualification(
        samples,
        through_session=through_session,
        excluded=((through_session, "CURRENT_SESSION_TERMINAL_EVENT_PENDING"),),
    )
    report = _build_forward_review_markout(
        samples,
        through_session=through_session,
        trading_sessions=(),
        bars_by_symbol={},
        source_session_qualification=qualification,
    )
    assert report["source_session_qualification"] == qualification
    assert "FORWARD_SOURCE_SESSIONS_EXCLUDED" in report["reason_codes"]

    forged = copy.deepcopy(report)
    forged_qualification = forged["source_session_qualification"]
    forged_qualification["qualified_sessions"] = []
    forged_qualification["qualified_session_evidence"] = []
    forged_qualification["qualified_session_count"] = 0
    forged_qualification["excluded_sessions"].insert(
        0,
        {
            "session": D0.isoformat(),
            "reason_code": "FORWARD_DELIVERY_NOT_READY",
        },
    )
    forged_qualification["excluded_session_count"] = 2
    qualification_stable = {
        key: forged_qualification[key]
        for key in forged_qualification
        if key != "content_sha256"
    }
    forged_qualification["content_sha256"] = sha256_json(qualification_stable)
    forged_stable = {key: forged[key] for key in forged if key != "content_sha256"}
    forged["content_sha256"] = sha256_json(forged_stable)
    with pytest.raises(ValueError, match="observation identity changed"):
        validate_forward_review_markout_document(forged)


def test_markout_replays_source_audit_instead_of_trusting_sha256_shapes() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "a" * 64)
    source = _report(D0, alert)
    sessions = _future_sessions(5)
    samples = select_first_strategic_buy_samples(
        ((D0, source["content_sha256"], source),),
        through_session=sessions[-1],
    )
    bars = _causal_reference_prefix(
        D0,
        cutoff=alert.review_available_at,
        close=Decimal("10"),
    )
    for session in sessions:
        bars.extend(_complete_session_bars(session, Decimal("11")))
    event = QmtCausalFactorEvent(
        code=alert.symbol,
        effective_on=D0,
        interest=Decimal("0"),
        stock_bonus=Decimal("0"),
        stock_gift=Decimal("0"),
        allot_num=Decimal("0"),
        allot_price=Decimal("0"),
        gugai=Decimal("0"),
        raw_price_divisor=Decimal("1"),
    )
    audit = _source_audit(
        symbol=alert.symbol,
        bars=bars,
        through_session=sessions[-1],
        events=(event,),
    )

    valid = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={alert.symbol: bars},
        source_audits={alert.symbol: audit},
    )
    assert valid["source_provenance_status"] == "COMPLETE"
    assert valid["source_audit_requirement"] == (
        FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID
    )

    mutations = (
        ("source_audit_contract_id", "FOREIGN_CONTRACT"),
        ("opening_event_normalization", "FOREIGN_GRID"),
        ("price_adjustment", "front"),
        ("factor_known_through", D0.isoformat()),
        ("factor_event_count", 0),
        ("factor_revision", "sha256:" + "0" * 64),
        ("row_count", len(bars) - 1),
        ("normalized_row_count", len(bars) - 1),
        ("raw_row_count", -1),
        ("first_at", bars[1].observed_at.isoformat()),
        ("last_at", bars[-2].observed_at.isoformat()),
        ("transport", ""),
        ("raw_bar_revision", "sha256:not-a-digest"),
        ("adjusted_bar_revision", "sha256:" + "1" * 64),
    )
    for field, bad_value in mutations:
        changed = {**audit, field: bad_value}
        report = build_forward_review_markout(
            samples,
            through_session=sessions[-1],
            trading_sessions=sessions,
            bars_by_symbol={alert.symbol: bars},
            source_audits={alert.symbol: changed},
        )
        assert report["source_provenance_status"] == "INCOMPLETE", field

    changed_event = dict(audit["factor_events"][0])  # type: ignore[index]
    changed_event["raw_price_divisor"] = "2"
    changed = {**audit, "factor_events": (changed_event,)}
    report = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={alert.symbol: bars},
        source_audits={alert.symbol: changed},
    )
    assert report["source_provenance_status"] == "INCOMPLETE"


def test_markout_rejects_a_tampered_promoted_report() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "5" * 64)
    report = _report(D0, alert)
    report["review_queue"] = []

    with pytest.raises(ValueError, match="contract or hash changed"):
        select_first_strategic_buy_samples(
            ((D0, report["content_sha256"], report),),
            through_session=D0,
        )


def test_markout_separates_screening_policy_cohorts() -> None:
    first_alert = _alert(session=D0, snapshot="sha256:" + "6" * 64)
    second_alert = replace(
        _alert(
            session=D0 + timedelta(days=1),
            snapshot="sha256:" + "7" * 64,
        ),
        symbol="SZ.000001",
        source_point_id="sha256:" + "8" * 64,
    )
    first = _report(D0, first_alert, screening_policy_id=POLICY_A)
    second = _report(
        D0 + timedelta(days=1),
        second_alert,
        screening_policy_id=POLICY_B,
    )
    samples = select_first_strategic_buy_samples(
        (
            (D0, first["content_sha256"], first),
            (D0 + timedelta(days=1), second["content_sha256"], second),
        ),
        through_session=D0 + timedelta(days=2),
    )

    report = build_forward_review_markout(
        samples,
        through_session=D0 + timedelta(days=2),
        trading_sessions=_future_sessions(2),
        bars_by_symbol={},
    )

    assert report["sample"]["screening_policy_ids"] == [POLICY_A, POLICY_B]
    assert report["sample"]["mixed_screening_policy_cohorts"] is True
    assert set(report["sample"]["by_screening_policy_id"]) == {
        POLICY_A,
        POLICY_B,
    }
    assert not any(report["sample"]["sample_sufficient_by_horizon"].values())
    assert all(
        row["source_screening_policy_id"] in {POLICY_A, POLICY_B}
        for row in report["observations"]
    )
    assert "MIXED_SCREENING_POLICY_COHORTS_MUST_NOT_BE_POOLED" in report["reason_codes"]


def test_markout_separates_source_code_under_the_same_policy() -> None:
    first_alert = _alert(session=D0, snapshot="sha256:" + "d" * 64)
    second_alert = replace(
        _alert(
            session=D0 + timedelta(days=1),
            snapshot="sha256:" + "e" * 64,
        ),
        symbol="SZ.000001",
        source_point_id="sha256:" + "f" * 64,
    )
    source_a = _decision_source_snapshot("1")
    source_b = _decision_source_snapshot("2")
    first = _report(
        D0,
        first_alert,
        screening_policy_id=POLICY_A,
        decision_core_id=CORE_A,
        decision_source_snapshot=source_a,
    )
    second = _report(
        D0 + timedelta(days=1),
        second_alert,
        screening_policy_id=POLICY_A,
        decision_core_id=CORE_A,
        decision_source_snapshot=source_b,
    )
    samples = select_first_strategic_buy_samples(
        (
            (D0, first["content_sha256"], first),
            (D0 + timedelta(days=1), second["content_sha256"], second),
        ),
        through_session=D0 + timedelta(days=2),
    )

    report = build_forward_review_markout(
        samples,
        through_session=D0 + timedelta(days=2),
        trading_sessions=_future_sessions(2),
        bars_by_symbol={},
    )

    sample = report["sample"]
    assert sample["screening_policy_ids"] == [POLICY_A]
    assert sample["mixed_screening_policy_cohorts"] is False
    assert sample["mixed_decision_source_cohorts"] is True
    assert len(sample["by_source_cohort_id"]) == 2
    assert (
        sample["by_screening_policy_id"][POLICY_A]["mixed_decision_source_cohorts"]
        is True
    )
    assert not any(sample["sample_sufficient_by_horizon"].values())
    assert {
        row["source_decision_source_snapshot_id"] for row in report["observations"]
    } == {
        decision_source_snapshot_id(source_a),
        decision_source_snapshot_id(source_b),
    }
    assert "MIXED_DECISION_SOURCE_COHORTS_MUST_NOT_BE_POOLED" in report["reason_codes"]


def test_markout_replays_embedded_decision_source_snapshot() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "3" * 64)
    source = _decision_source_snapshot("4")
    report = _report(
        D0,
        alert,
        decision_core_id=CORE_A,
        decision_source_snapshot=source,
    )
    report["decision_source_snapshot"]["files"][0]["sha256"] = "sha256:" + "5" * 64
    stable = {key: report[key] for key in report if key != "content_sha256"}
    report["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="aggregate changed"):
        select_first_strategic_buy_samples(
            ((D0, report["content_sha256"], report),),
            through_session=D0,
        )


def test_sample_minimum_requires_attested_cohort_and_price_source() -> None:
    sessions = _future_sessions(5)
    base_alert = _alert(session=D0, snapshot="sha256:" + "6" * 64)
    source_snapshot = _decision_source_snapshot("7")
    source_identity = decision_source_snapshot_id(source_snapshot)
    samples = tuple(
        ForwardReviewSample(
            source_session=D0,
            source_report_content_sha256=("sha256:" + f"{index + 1000:064x}"),
            source_screening_policy_id=POLICY_A,
            source_decision_core_id=CORE_A,
            source_decision_source_snapshot_id=source_identity,
            alert=replace(
                base_alert,
                source_point_id="sha256:" + f"{index + 1:064x}",
            ),
            source_decision_source_snapshot=source_snapshot,
        )
        for index in range(100)
    )
    bars = _causal_reference_prefix(
        D0,
        cutoff=base_alert.review_available_at,
        close=Decimal("10"),
    )
    for session in sessions:
        bars.extend(_complete_session_bars(session, Decimal("11")))
    audit = _source_audit(
        symbol=base_alert.symbol,
        bars=bars,
        through_session=sessions[-1],
        events=(),
    )

    attested = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={base_alert.symbol: bars},
        source_audits={base_alert.symbol: audit},
    )
    assert attested["sample"]["eligible_by_horizon"]["5"] == 100
    assert attested["sample"]["sample_sufficient_by_horizon"]["5"] is True
    assert attested["sample"]["source_identity_status"] == "ATTESTED"

    missing_price_audit = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={base_alert.symbol: bars},
    )
    assert missing_price_audit["sample"]["sample_sufficient_by_horizon"]["5"] is False

    unattested_samples = tuple(
        replace(
            sample,
            source_decision_core_id="UNATTESTED_DECISION_CORE",
            source_decision_source_snapshot_id=(
                "UNATTESTED_DECISION_SOURCE_SNAPSHOT"
            ),
        )
        for sample in samples
    )
    with pytest.raises(ValueError, match="source identity is unattested"):
        build_forward_review_markout(
            unattested_samples,
            through_session=sessions[-1],
            trading_sessions=sessions,
            bars_by_symbol={base_alert.symbol: bars},
            source_audits={base_alert.symbol: audit},
        )


def test_current_markout_rejects_rehashed_internal_accounting_forgery() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "8" * 64)
    source_snapshot = _decision_source_snapshot("9")
    source_report = _report(
        D0,
        alert,
        decision_core_id=CORE_A,
        decision_source_snapshot=source_snapshot,
    )
    samples = select_first_strategic_buy_samples(
        ((D0, source_report["content_sha256"], source_report),),
        through_session=D0 + timedelta(days=1),
    )
    report = build_forward_review_markout(
        samples,
        through_session=D0 + timedelta(days=1),
        trading_sessions=(),
        bars_by_symbol={},
    )

    mutations = []
    sufficient = copy.deepcopy(report)
    sufficient["sample"]["sample_sufficient_by_horizon"]["5"] = True
    mutations.append(sufficient)

    wrong_cohort = copy.deepcopy(report)
    wrong_cohort["observations"][0]["source_cohort_id"] = "sha256:" + "0" * 64
    mutations.append(wrong_cohort)

    wrong_proof = copy.deepcopy(report)
    proof = next(iter(wrong_proof["decision_source_snapshots"].values()))
    proof["files"][0]["sha256"] = "sha256:" + "a" * 64
    mutations.append(wrong_proof)

    wrong_reason = copy.deepcopy(report)
    wrong_reason["reason_codes"].remove("MARKOUT_SOURCE_PROVENANCE_INCOMPLETE")
    mutations.append(wrong_reason)

    wrong_return = copy.deepcopy(report)
    wrong_return["summary"]["5"]["mean_close_return"] = "9.99"
    mutations.append(wrong_return)

    for forged in mutations:
        stable = {key: forged[key] for key in forged if key != "content_sha256"}
        forged["content_sha256"] = sha256_json(stable)
        with pytest.raises(ValueError):
            validate_forward_review_markout_document(forged)


def test_markout_never_shifts_past_an_incomplete_market_session() -> None:
    """缺失交易日必须使固定期限不可评价，不能偷偷顺延到下一完整日。"""

    alert = _alert(session=D0, snapshot="sha256:" + "c" * 64)
    report_source = _report(D0, alert)
    samples = select_first_strategic_buy_samples(
        ((D0, report_source["content_sha256"], report_source),),
        through_session=_future_sessions(6)[-1],
    )
    sessions = _future_sessions(6)
    bars = _causal_reference_prefix(
        D0,
        cutoff=alert.review_available_at,
        close=Decimal("10"),
    )
    for session in sessions:
        bars.extend(_complete_session_bars(session, Decimal("10")))
    # 第一个市场交易日缺一根 1m K；第六日即使完整，也不能替代它成为“第 5 日”。
    bars = [
        bar
        for bar in bars
        if bar.observed_at != a_share_completed_one_minute_closes(sessions[0])[100]
    ]

    report = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={alert.symbol: bars},
    )

    five = next(row for row in report["observations"] if row["horizon_sessions"] == 5)
    assert five["complete"] is False
    assert five["end_session"] == sessions[4].isoformat()
    assert five["reason_code"] == "INCOMPLETE_FUTURE_SESSION_GRID"
    assert report["sample"]["eligible_by_horizon"]["5"] == 0
    assert report["summary"]["5"]["pending_count"] == 1


def test_markout_rejects_a_stale_prior_session_reference_close() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "d" * 64)
    source = _report(D0, alert)
    sessions = _future_sessions(5)
    samples = select_first_strategic_buy_samples(
        ((D0, source["content_sha256"], source),),
        through_session=sessions[-1],
    )
    prior = D0 - timedelta(days=1)
    bars = _complete_session_bars(prior, Decimal("10"))
    for session in sessions:
        bars.extend(_complete_session_bars(session, Decimal("11")))

    report = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={alert.symbol: bars},
    )

    five = next(row for row in report["observations"] if row["horizon_sessions"] == 5)
    assert five["reference_at"] is None
    assert five["reference_price"] is None
    assert five["complete"] is False
    assert five["reason_code"] == "INCOMPLETE_CAUSAL_REFERENCE_SESSION_GRID"


def test_markout_rejects_a_gapped_intraday_reference_prefix() -> None:
    alert = _alert(session=D0, snapshot="sha256:" + "e" * 64)
    source = _report(D0, alert)
    sessions = _future_sessions(5)
    samples = select_first_strategic_buy_samples(
        ((D0, source["content_sha256"], source),),
        through_session=sessions[-1],
    )
    bars = _causal_reference_prefix(
        D0,
        cutoff=alert.review_available_at,
        close=Decimal("10"),
    )
    del bars[10]
    for session in sessions:
        bars.extend(_complete_session_bars(session, Decimal("11")))

    report = build_forward_review_markout(
        samples,
        through_session=sessions[-1],
        trading_sessions=sessions,
        bars_by_symbol={alert.symbol: bars},
    )

    assert {row["reason_code"] for row in report["observations"]} == {
        "INCOMPLETE_CAUSAL_REFERENCE_SESSION_GRID",
    }
    five = next(row for row in report["observations"] if row["horizon_sessions"] == 5)
    assert five["reason_code"] == "INCOMPLETE_CAUSAL_REFERENCE_SESSION_GRID"
