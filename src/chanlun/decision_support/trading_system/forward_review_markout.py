"""Causal mark-outs for immutable forward human-review candidates.

This module deliberately evaluates *screening observations*, not trades.  A
candidate remains eligible for a mark-out even when the frozen monthly/weekly/
daily risk gate correctly blocks a paper intent.  That lets the forward process
learn whether the program narrowed the market usefully without relaxing the
risk contract, inventing a fill, or reporting portfolio P&L.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HumanReviewAlert,
    ReviewEventStudyObservation,
    ReviewPriceBar,
    evaluate_review_alert,
    parse_human_review_alert,
    summarize_event_study,
)
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_PAPER_SESSION_DELIVERY_SCHEMA,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
)


FORWARD_REVIEW_MARKOUT_SCHEMA = "chanlun-forward-review-markout"
FORWARD_REVIEW_SESSION_QUALIFICATION_SCHEMA = (
    "chanlun-forward-review-session-qualification"
)
FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID = (
    "QMT_FORWARD_MARKOUT_RAW_OPENING_FACTOR_ADJUSTED_BARS"
)
FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID = (
    "SCREENING_POLICY_DECISION_CORE_AND_SOURCE_SNAPSHOT"
)
_HUMAN_REVIEW_SCREEN_SCHEMA = "chanlun-human-review-screen"
_STRATEGIC_SAMPLE_MINIMUM = 100
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_FACTOR_EVENT_FIELDS = frozenset(
    {
        "code",
        "effective_on",
        "interest",
        "stock_bonus",
        "stock_gift",
        "allot_num",
        "allot_price",
        "gugai",
        "raw_price_divisor",
    }
)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _qualified_delivery_audit(
    value: object,
    *,
    source_session: date,
    qualification_observed_at: datetime,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("forward review qualified delivery evidence is invalid")
    delivery = value.get("delivery_audit")
    if not isinstance(delivery, Mapping):
        raise ValueError("forward review qualified delivery audit is invalid")
    delivery_hash = value.get("delivery_audit_content_sha256")
    implementation_ids = (
        delivery.get("capture_implementation_provenance_id"),
        delivery.get("data_ready_implementation_provenance_id"),
        delivery.get("evaluation_implementation_provenance_id"),
    )
    event_ids = (
        delivery.get("capture_event_sha256"),
        delivery.get("data_ready_event_sha256"),
        delivery.get("evaluation_event_sha256"),
    )
    if (
        value.get("session") != source_session.isoformat()
        or delivery_hash != sha256_json(delivery)
        or delivery.get("schema") != FORWARD_PAPER_SESSION_DELIVERY_SCHEMA
        or delivery.get("session") != source_session.isoformat()
        or delivery.get("observed_at") != qualification_observed_at.isoformat()
        or delivery.get("required") is not True
        or delivery.get("requirement_resolved") is not True
        or delivery.get("trading_session_evidence_proven") is not True
        or delivery.get("ready") is not True
        or delivery.get("status") != "ready"
        or delivery.get("reason_code") != "READY"
        or delivery.get("capture_event_present") is not True
        or delivery.get("data_ready_event_present") is not True
        or delivery.get("evaluation_event_present") is not True
        or delivery.get("capture_ready") is not True
        or delivery.get("evaluation_ready") is not True
        or delivery.get("capture_evidence_proven") is not True
        or delivery.get("data_ready_evidence_proven") is not True
        or delivery.get("evaluation_artifacts_proven") is not True
        or delivery.get("implementation_provenance_present") is not True
        or delivery.get("implementation_provenance_proven") is not True
        or len(set(implementation_ids)) != 1
        or any(
            not isinstance(identity, str) or _SHA256_ID.fullmatch(identity) is None
            for identity in implementation_ids
        )
        or any(
            not isinstance(identity, str) or _SHA256_ID.fullmatch(identity) is None
            for identity in event_ids
        )
        or delivery.get("latest_terminal_event_status") != "EVALUATED"
        or delivery.get("latest_terminal_event_sha256") != event_ids[-1]
        or delivery.get("real_account_accessed") is not False
        or delivery.get("real_order_transport_enabled") is not False
        or delivery.get("paper_status") != "REVIEW_REQUIRED"
        or delivery.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("forward review qualified delivery audit is invalid")
    return delivery


def qualified_forward_review_session_dates(
    qualification: Mapping[str, object],
    *,
    through_session: date,
) -> frozenset[date]:
    """Validate the delivery qualification bound into one mark-out.

    A content hash alone cannot prove that a screening report came from a
    complete Capture -> DataReady -> Evaluate session.  This contract makes
    that admission decision part of the mark-out itself and gives the report
    validator enough evidence to reject observations from excluded sessions.
    """

    stable = {
        key: qualification[key] for key in qualification if key != "content_sha256"
    }
    raw_qualified = qualification.get("qualified_sessions")
    raw_excluded = qualification.get("excluded_sessions")
    raw_evidence = qualification.get("qualified_session_evidence")
    ledger_content_sha256 = qualification.get("forward_ledger_content_sha256")
    if (
        qualification.get("schema") != FORWARD_REVIEW_SESSION_QUALIFICATION_SCHEMA
        or qualification.get("through_session") != through_session.isoformat()
        or not isinstance(raw_qualified, list)
        or not isinstance(raw_excluded, list)
        or not isinstance(raw_evidence, list)
        or len(raw_evidence) != len(raw_qualified)
        or qualification.get("qualified_session_count") != len(raw_qualified)
        or qualification.get("excluded_session_count") != len(raw_excluded)
        or qualification.get("current_session_excluded_until_terminal_event")
        is not True
        or qualification.get("real_account_accessed") is not False
        or qualification.get("real_order_transport_enabled") is not False
        or qualification.get("live_status") != "LIVE_DISABLED"
        or qualification.get("content_sha256") != sha256_json(stable)
    ):
        raise ValueError("forward review session qualification is invalid")
    try:
        observed_at = datetime.fromisoformat(str(qualification["observed_at"]))
        qualified = tuple(date.fromisoformat(str(value)) for value in raw_qualified)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "forward review session qualification timestamp is invalid"
        ) from exc
    if observed_at.tzinfo is None or observed_at.date() != through_session:
        raise ValueError("forward review session qualification observation is invalid")
    if (
        qualified != tuple(sorted(set(qualified)))
        or any(value >= through_session for value in qualified)
        or (
            ledger_content_sha256 is not None
            and (
                not isinstance(ledger_content_sha256, str)
                or _SHA256_ID.fullmatch(ledger_content_sha256) is None
            )
        )
        or (qualified and ledger_content_sha256 is None)
    ):
        raise ValueError("forward review qualified sessions are invalid")
    evidence_sessions: list[date] = []
    for source_session, raw in zip(qualified, raw_evidence, strict=True):
        _qualified_delivery_audit(
            raw,
            source_session=source_session,
            qualification_observed_at=observed_at,
        )
        evidence_sessions.append(source_session)
    if tuple(evidence_sessions) != qualified:
        raise ValueError("forward review qualified delivery evidence changed")

    excluded_sessions: list[date] = []
    for raw in raw_excluded:
        if not isinstance(raw, Mapping):
            raise ValueError("forward review excluded session is invalid")
        try:
            excluded_session = date.fromisoformat(str(raw["session"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("forward review excluded session is invalid") from exc
        reason_code = raw.get("reason_code")
        if (
            excluded_session > through_session
            or not isinstance(reason_code, str)
            or not reason_code.strip()
        ):
            raise ValueError("forward review excluded session is invalid")
        excluded_audit = raw.get("delivery_audit")
        excluded_audit_hash = raw.get("delivery_audit_content_sha256")
        if excluded_audit is not None or excluded_audit_hash is not None:
            if (
                not isinstance(excluded_audit, Mapping)
                or excluded_audit_hash != sha256_json(excluded_audit)
                or excluded_audit.get("schema") != FORWARD_PAPER_SESSION_DELIVERY_SCHEMA
                or excluded_audit.get("session") != excluded_session.isoformat()
                or excluded_audit.get("reason_code") != reason_code
                or excluded_audit.get("ready") is not False
                or raw.get("delivery_status") != excluded_audit.get("status")
            ):
                raise ValueError("forward review excluded delivery audit is invalid")
        excluded_sessions.append(excluded_session)
    if tuple(excluded_sessions) != tuple(sorted(set(excluded_sessions))) or set(
        excluded_sessions
    ).intersection(qualified):
        raise ValueError("forward review session qualification overlaps")
    return frozenset(qualified)


def review_price_bars_revision(
    *,
    symbol: str,
    through_session: date,
    bars: Sequence[ReviewPriceBar],
) -> str:
    """Bind the exact normalized price observations consumed by a mark-out."""

    ordered = tuple(sorted(bars, key=lambda value: value.observed_at))
    if len({value.observed_at for value in ordered}) != len(ordered):
        raise ValueError("forward markout price bars contain duplicate timestamps")
    if any(value.observed_at.date() > through_session for value in ordered):
        raise ValueError("forward markout price bar is after through_session")
    return sha256_json(
        {
            "schema": "chanlun-forward-markout-adjusted-1m-bars",
            "symbol": symbol,
            "through_session": through_session.isoformat(),
            "rows": tuple(
                {
                    "observed_at": value.observed_at.isoformat(),
                    "high": format(value.high, "f"),
                    "low": format(value.low, "f"),
                    "close": format(value.close, "f"),
                }
                for value in ordered
            ),
        }
    )


def _source_factor_events(
    *,
    symbol: str,
    raw_events: object,
) -> tuple[QmtCausalFactorEvent, ...]:
    if not isinstance(raw_events, (tuple, list)):
        raise ValueError("forward markout factor events are not a sequence")
    events: list[QmtCausalFactorEvent] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping) or set(raw) != _FACTOR_EVENT_FIELDS:
            raise ValueError("forward markout factor event shape changed")
        event = QmtCausalFactorEvent(
            code=str(raw["code"]),
            effective_on=date.fromisoformat(str(raw["effective_on"])),
            interest=Decimal(str(raw["interest"])),
            stock_bonus=Decimal(str(raw["stock_bonus"])),
            stock_gift=Decimal(str(raw["stock_gift"])),
            allot_num=Decimal(str(raw["allot_num"])),
            allot_price=Decimal(str(raw["allot_price"])),
            gugai=Decimal(str(raw["gugai"])),
            raw_price_divisor=Decimal(str(raw["raw_price_divisor"])),
        )
        if event.code != symbol or dict(raw) != event.canonical_payload():
            raise ValueError("forward markout factor event is not canonical")
        events.append(event)
    keys = tuple(event.effective_on for event in events)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("forward markout factor events are not unique and ordered")
    return tuple(events)


def _source_audit_is_complete(
    *,
    symbol: str,
    audit: Mapping[str, object],
    bars: Sequence[ReviewPriceBar],
    through_session: date,
    adjusted_revision: str,
) -> bool:
    """Independently replay every compact source claim available to core."""

    try:
        ordered = tuple(sorted(bars, key=lambda value: value.observed_at))
        events = _source_factor_events(
            symbol=symbol,
            raw_events=audit.get("factor_events"),
        )
        factor_revision = qmt_causal_factor_revision(
            members=(symbol,),
            events_by_code={symbol: events},
            known_through=through_session,
        )
        row_count = audit.get("row_count")
        normalized_row_count = audit.get("normalized_row_count")
        raw_row_count = audit.get("raw_row_count")
        if any(
            type(value) is not int or value < 0
            for value in (row_count, normalized_row_count, raw_row_count)
        ):
            return False
        first_at = None if not ordered else ordered[0].observed_at.isoformat()
        last_at = None if not ordered else ordered[-1].observed_at.isoformat()
        return (
            audit.get("status") == "AVAILABLE"
            and audit.get("source_audit_contract_id")
            == FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID
            and audit.get("opening_event_normalization")
            == QMT_COMPLETED_ONE_MINUTE_GRID_REVISION
            and audit.get("price_adjustment")
            == "CAUSAL_KNOWN_EX_DATE_RAW_PRICE_DIVISOR"
            and audit.get("factor_contract_id")
            == QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
            and audit.get("factor_known_through") == through_session.isoformat()
            and audit.get("factor_event_count") == len(events)
            and audit.get("factor_revision") == factor_revision
            and row_count == len(ordered)
            and normalized_row_count == len(ordered)
            and raw_row_count >= normalized_row_count
            and audit.get("first_at") == first_at
            and audit.get("last_at") == last_at
            and isinstance(audit.get("transport"), str)
            and bool(str(audit["transport"]).strip())
            and _SHA256_ID.fullmatch(str(audit.get("raw_bar_revision"))) is not None
            and audit.get("adjusted_bar_revision") == adjusted_revision
        )
    except (ArithmeticError, TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class ForwardReviewSample:
    """First immutable appearance of one strategic review lifecycle."""

    source_session: date
    source_report_content_sha256: str
    source_screening_policy_id: str
    source_decision_core_id: str
    source_decision_source_snapshot_id: str
    alert: HumanReviewAlert
    source_decision_source_snapshot: Mapping[str, object]

    @property
    def signal_lifecycle_id(self) -> str:
        return self.alert.signal_lifecycle_id

    @property
    def source_cohort_id(self) -> str:
        """Identity of one statistically compatible forward implementation."""

        return sha256_json(
            {
                "contract": FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
                "screening_policy_id": self.source_screening_policy_id,
                "decision_core_id": self.source_decision_core_id,
                "decision_source_snapshot_id": (
                    self.source_decision_source_snapshot_id
                ),
            }
        )


def _alert_from_document(value: Mapping[str, object]) -> HumanReviewAlert:
    try:
        return parse_human_review_alert(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward human-review alert is invalid") from exc


def _report_screening_policy_id(report: Mapping[str, object]) -> str:
    input_hashes = report.get("input_hashes")
    if not isinstance(input_hashes, Mapping):
        raise ValueError("forward human-review input identities are unavailable")
    value = input_hashes.get("screening_policy_id")
    if not isinstance(value, str) or _SHA256_ID.fullmatch(value) is None:
        raise ValueError("forward human-review screening policy identity is invalid")
    return value


def _report_decision_core_id(report: Mapping[str, object]) -> str:
    input_hashes = report.get("input_hashes")
    if not isinstance(input_hashes, Mapping):
        raise ValueError("forward human-review input identities are unavailable")
    value = input_hashes.get("decision_core_id")
    if not isinstance(value, str) or _SHA256_ID.fullmatch(value) is None:
        raise ValueError("forward human-review decision core identity is invalid")
    return value


def _report_decision_source_snapshot(
    report: Mapping[str, object],
) -> tuple[str, Mapping[str, object]]:
    snapshot = report.get("decision_source_snapshot")
    input_hashes = report.get("input_hashes")
    declared = (
        input_hashes.get("decision_source_snapshot_id")
        if isinstance(input_hashes, Mapping)
        else None
    )
    if not isinstance(snapshot, Mapping):
        raise ValueError("forward human-review decision source snapshot is missing")
    identity = decision_source_snapshot_id(snapshot)
    if declared != identity:
        raise ValueError("forward human-review decision source identity changed")
    return identity, snapshot


def _sample_source_identity_is_attested(sample: ForwardReviewSample) -> bool:
    identities = (
        sample.source_screening_policy_id,
        sample.source_decision_core_id,
        sample.source_decision_source_snapshot_id,
    )
    if any(_SHA256_ID.fullmatch(value) is None for value in identities):
        return False
    if sample.source_decision_source_snapshot is None:
        return False
    try:
        return (
            decision_source_snapshot_id(sample.source_decision_source_snapshot)
            == sample.source_decision_source_snapshot_id
        )
    except (TypeError, ValueError):
        return False


def select_first_strategic_buy_samples(
    reports: Sequence[tuple[date, str, Mapping[str, object]]],
    *,
    through_session: date,
) -> tuple[ForwardReviewSample, ...]:
    """Select one causal observation per lifecycle, at its first appearance."""

    first: dict[str, ForwardReviewSample] = {}
    for source_session, source_sha256, report in sorted(
        reports,
        key=lambda item: (item[0], item[1]),
    ):
        if source_session > through_session:
            raise ValueError("forward review report is after the evaluation session")
        stable = {key: report[key] for key in report if key != "content_sha256"}
        queue = report.get("review_queue")
        if (
            report.get("schema") != _HUMAN_REVIEW_SCREEN_SCHEMA
            or report.get("forward_paper_session") != source_session.isoformat()
            or report.get("content_sha256") != source_sha256
            or source_sha256 != sha256_json(stable)
            or not isinstance(queue, list)
            or report.get("automated_order_authorized") is not False
            or report.get("live_status") != "LIVE_DISABLED"
        ):
            raise ValueError("forward human-review report contract or hash changed")
        screening_policy_id = _report_screening_policy_id(report)
        decision_core_id = _report_decision_core_id(report)
        decision_source_id, decision_source_snapshot = _report_decision_source_snapshot(
            report
        )
        for raw in queue:
            if not isinstance(raw, Mapping):
                raise ValueError("forward human-review queue entry is invalid")
            if raw.get("alert_type") != "POSSIBLE_30M_BUY":
                continue
            alert = _alert_from_document(raw)
            sample = ForwardReviewSample(
                source_session=source_session,
                source_report_content_sha256=source_sha256,
                source_screening_policy_id=screening_policy_id,
                source_decision_core_id=decision_core_id,
                source_decision_source_snapshot_id=decision_source_id,
                alert=alert,
                source_decision_source_snapshot=decision_source_snapshot,
            )
            existing = first.get(sample.signal_lifecycle_id)
            if existing is None or (
                alert.review_available_at,
                source_session,
                alert.candidate_id,
            ) < (
                existing.alert.review_available_at,
                existing.source_session,
                existing.alert.candidate_id,
            ):
                first[sample.signal_lifecycle_id] = sample
    return tuple(
        sorted(
            first.values(),
            key=lambda item: (
                item.alert.review_available_at,
                item.alert.symbol,
                item.signal_lifecycle_id,
            ),
        )
    )


def _feedback_for_sample(
    sample: ForwardReviewSample,
    feedback: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    values = tuple(
        row
        for row in feedback
        if row.get("signal_lifecycle_id") == sample.signal_lifecycle_id
        or row.get("candidate_id") == sample.alert.candidate_id
    )
    return tuple(
        sorted(
            values,
            key=lambda row: (
                str(row.get("reviewed_at") or ""),
                str(row.get("feedback_id") or ""),
            ),
        )
    )


def build_forward_review_markout(
    samples: Sequence[ForwardReviewSample],
    *,
    through_session: date,
    trading_sessions: Sequence[date],
    bars_by_symbol: Mapping[str, Sequence[ReviewPriceBar]],
    source_session_qualification: Mapping[str, object],
    source_audits: Mapping[str, Mapping[str, object]] | None = None,
    feedback: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Build a diagnostic-only 5/10/20-session event study.

    Returns never become fills, positions, orders, or portfolio returns.  The
    risk-gate split is counterfactual evidence: ``BLOCKED`` means the frozen
    strategy correctly declined to create a paper intent, not that a trade was
    taken outside the contract.
    """

    qualified_source_sessions = qualified_forward_review_session_dates(
        source_session_qualification,
        through_session=through_session,
    )
    sample_source_sessions = {sample.source_session for sample in samples}
    if not sample_source_sessions.issubset(qualified_source_sessions):
        raise ValueError(
            "forward markout sample came from an unqualified delivery session"
        )
    if any(not _sample_source_identity_is_attested(sample) for sample in samples):
        raise ValueError("forward markout sample source identity is unattested")

    calendar = tuple(trading_sessions)
    if (
        calendar != tuple(sorted(set(calendar)))
        or any(not isinstance(value, date) for value in calendar)
        or any(value > through_session for value in calendar)
    ):
        raise ValueError("forward markout trading calendar is invalid")
    calendar_document = {
        "contract": "EXPLICIT_SORTED_MARKET_TRADING_SESSIONS",
        "through_session": through_session.isoformat(),
        "sessions": tuple(value.isoformat() for value in calendar),
    }
    audits = {
        str(symbol): dict(audit)
        for symbol, audit in sorted((source_audits or {}).items())
    }
    sample_symbols = tuple(sorted({sample.alert.symbol for sample in samples}))
    adjusted_revisions = {
        symbol: review_price_bars_revision(
            symbol=symbol,
            through_session=through_session,
            bars=bars_by_symbol.get(symbol, ()),
        )
        for symbol in sample_symbols
    }
    source_provenance_complete = set(audits) == set(sample_symbols) and all(
        _source_audit_is_complete(
            symbol=symbol,
            audit=audit,
            bars=bars_by_symbol.get(symbol, ()),
            through_session=through_session,
            adjusted_revision=adjusted_revisions[symbol],
        )
        for symbol, audit in audits.items()
    )
    observations = []
    observation_objects: list[ReviewEventStudyObservation] = []
    by_risk: dict[str, list[ReviewEventStudyObservation]] = {
        "ALL_GREEN": [],
        "BLOCKED": [],
    }
    by_screening_policy: dict[str, list[ReviewEventStudyObservation]] = {}
    by_source_cohort: dict[str, list[ReviewEventStudyObservation]] = {}
    source_cohort_metadata: dict[str, tuple[str, str, str]] = {}
    source_cohort_attestation: dict[str, bool] = {}
    decision_source_snapshots: dict[str, object] = {}
    feedback_linked_lifecycles = 0
    for sample in samples:
        linked_feedback = _feedback_for_sample(sample, feedback)
        feedback_linked_lifecycles += bool(linked_feedback)
        risk_class = (
            "ALL_GREEN"
            if sample.alert.market_risk_gate == "GREEN"
            and sample.alert.sector_risk_gate == "GREEN"
            and sample.alert.symbol_risk_gate == "GREEN"
            else "BLOCKED"
        )
        values = evaluate_review_alert(
            sample.alert,
            bars_by_symbol.get(sample.alert.symbol, ()),
            trading_sessions=calendar,
            require_complete_one_minute_sessions=True,
        )
        by_screening_policy.setdefault(sample.source_screening_policy_id, []).extend(
            values
        )
        source_cohort_id = sample.source_cohort_id
        metadata = (
            sample.source_screening_policy_id,
            sample.source_decision_core_id,
            sample.source_decision_source_snapshot_id,
        )
        existing_metadata = source_cohort_metadata.setdefault(
            source_cohort_id,
            metadata,
        )
        if existing_metadata != metadata:
            raise ValueError("forward review source cohort identity collided")
        source_cohort_attestation[source_cohort_id] = source_cohort_attestation.get(
            source_cohort_id, True
        ) and _sample_source_identity_is_attested(sample)
        if sample.source_decision_source_snapshot is not None:
            try:
                snapshot_id = decision_source_snapshot_id(
                    sample.source_decision_source_snapshot
                )
            except (TypeError, ValueError):
                snapshot_id = None
            if snapshot_id == sample.source_decision_source_snapshot_id:
                snapshot_document = _jsonable(sample.source_decision_source_snapshot)
                existing_snapshot = decision_source_snapshots.setdefault(
                    snapshot_id,
                    snapshot_document,
                )
                if existing_snapshot != snapshot_document:
                    raise ValueError("forward review decision source snapshot collided")
        by_source_cohort.setdefault(source_cohort_id, []).extend(values)
        for value in values:
            observation_objects.append(value)
            by_risk[risk_class].append(value)
            latest_feedback = None if not linked_feedback else linked_feedback[-1]
            observations.append(
                {
                    **_jsonable(asdict(value)),  # type: ignore[arg-type]
                    "signal_lifecycle_id": sample.signal_lifecycle_id,
                    "source_session": sample.source_session.isoformat(),
                    "source_report_content_sha256": (
                        sample.source_report_content_sha256
                    ),
                    "source_screening_policy_id": (sample.source_screening_policy_id),
                    "source_decision_core_id": sample.source_decision_core_id,
                    "source_decision_source_snapshot_id": (
                        sample.source_decision_source_snapshot_id
                    ),
                    "source_cohort_id": source_cohort_id,
                    "alert_type": sample.alert.alert_type,
                    "market_risk_gate": sample.alert.market_risk_gate,
                    "sector_risk_gate": sample.alert.sector_risk_gate,
                    "symbol_risk_gate": sample.alert.symbol_risk_gate,
                    "risk_class": risk_class,
                    "human_feedback_count": len(linked_feedback),
                    "latest_human_disposition": (
                        None
                        if latest_feedback is None
                        else latest_feedback.get("disposition")
                    ),
                    "latest_human_point_judgement": (
                        None
                        if latest_feedback is None
                        else latest_feedback.get("point_judgement")
                    ),
                    "source_structure_anchor_price": (
                        None
                        if sample.alert.reference_price is None
                        else format(sample.alert.reference_price, "f")
                    ),
                }
            )
    summary = _jsonable(summarize_event_study(observation_objects))
    eligible_by_horizon = {
        horizon: int(values["eligible_count"]) for horizon, values in summary.items()
    }
    policy_ids = tuple(sorted(by_screening_policy))
    mixed_policy_cohorts = len(policy_ids) > 1
    source_cohort_ids = tuple(sorted(by_source_cohort))
    mixed_sample_cohorts = len(source_cohort_ids) > 1
    decision_implementation_ids = {
        (
            sample.source_decision_core_id,
            sample.source_decision_source_snapshot_id,
        )
        for sample in samples
    }
    mixed_decision_source_cohorts = len(decision_implementation_ids) > 1
    all_source_cohorts_attested = bool(source_cohort_ids) and all(
        source_cohort_attestation[cohort_id] for cohort_id in source_cohort_ids
    )
    policy_cohorts: dict[str, object] = {}
    for policy_id in policy_ids:
        cohort_summary = _jsonable(
            summarize_event_study(by_screening_policy[policy_id])
        )
        cohort_eligible = {
            horizon: int(values["eligible_count"])
            for horizon, values in cohort_summary.items()
        }
        policy_source_cohort_ids = tuple(
            sorted(
                {
                    sample.source_cohort_id
                    for sample in samples
                    if sample.source_screening_policy_id == policy_id
                }
            )
        )
        policy_source_attested = all(
            source_cohort_attestation[cohort_id]
            for cohort_id in policy_source_cohort_ids
        )
        policy_cohorts[policy_id] = {
            "unique_lifecycle_count": sum(
                sample.source_screening_policy_id == policy_id for sample in samples
            ),
            "eligible_by_horizon": cohort_eligible,
            "source_cohort_ids": list(policy_source_cohort_ids),
            "mixed_decision_source_cohorts": (len(policy_source_cohort_ids) > 1),
            "source_identity_attested": policy_source_attested,
            "sample_sufficient_by_horizon": {
                horizon: (
                    source_provenance_complete
                    and policy_source_attested
                    and len(policy_source_cohort_ids) == 1
                    and count >= _STRATEGIC_SAMPLE_MINIMUM
                )
                for horizon, count in cohort_eligible.items()
            },
            "summary": cohort_summary,
        }
    source_cohorts: dict[str, object] = {}
    for cohort_id in source_cohort_ids:
        policy_id, decision_core_id, source_snapshot_id = source_cohort_metadata[
            cohort_id
        ]
        cohort_summary = _jsonable(summarize_event_study(by_source_cohort[cohort_id]))
        cohort_eligible = {
            horizon: int(values["eligible_count"])
            for horizon, values in cohort_summary.items()
        }
        source_cohorts[cohort_id] = {
            "screening_policy_id": policy_id,
            "decision_core_id": decision_core_id,
            "decision_source_snapshot_id": source_snapshot_id,
            "unique_lifecycle_count": sum(
                sample.source_cohort_id == cohort_id for sample in samples
            ),
            "source_identity_attested": source_cohort_attestation[cohort_id],
            "eligible_by_horizon": cohort_eligible,
            "sample_sufficient_by_horizon": {
                horizon: (
                    source_provenance_complete
                    and source_cohort_attestation[cohort_id]
                    and count >= _STRATEGIC_SAMPLE_MINIMUM
                )
                for horizon, count in cohort_eligible.items()
            },
            "summary": cohort_summary,
        }
    stable: dict[str, object] = {
        "schema": FORWARD_REVIEW_MARKOUT_SCHEMA,
        "sample_cohort_contract": FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
        "through_session": through_session.isoformat(),
        "population": "FIRST_APPEARANCE_PER_POSSIBLE_30M_BUY_LIFECYCLE",
        "horizon_definition": (
            "5th/10th/20th complete trading session after review date; "
            "signal session excluded"
        ),
        "reference_definition": (
            "last completed 1m close in the exact gap-free same-session "
            "prefix known at review_available_at"
        ),
        "source_structure_anchor_definition": (
            "alert.reference_price is diagnostic structure evidence and is "
            "never substituted for the causal market-close return baseline"
        ),
        "trading_session_calendar": {
            **_jsonable(calendar_document),
            "revision": sha256_json(calendar_document),
        },
        "mature_session_grid_requirement": (
            "EXACT_CAUSAL_REFERENCE_PREFIX_AND_240_COMPLETED_A_SHARE_1M_"
            "CLOSES_PER_MATURE_MARKET_SESSION"
        ),
        "source_audit_requirement": FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID,
        "source_session_qualification": _jsonable(dict(source_session_qualification)),
        "decision_source_snapshots": decision_source_snapshots,
        "source_provenance_status": (
            "COMPLETE" if source_provenance_complete else "INCOMPLETE"
        ),
        "diagnostic_only": True,
        "counterfactual_risk_gate_split": True,
        "risk_gate_contract": "MARKET_SECTOR_SYMBOL_ALL_GREEN",
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "automated_order_authorized": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
        "sample": {
            "unique_lifecycle_count": len(samples),
            "feedback_linked_lifecycle_count": feedback_linked_lifecycles,
            "screening_policy_ids": list(policy_ids),
            "mixed_screening_policy_cohorts": mixed_policy_cohorts,
            "source_cohort_ids": list(source_cohort_ids),
            "mixed_sample_cohorts": mixed_sample_cohorts,
            "mixed_decision_source_cohorts": (mixed_decision_source_cohorts),
            "source_identity_status": ("NOT_APPLICABLE" if not samples else "ATTESTED"),
            "minimum_strategic_observations": _STRATEGIC_SAMPLE_MINIMUM,
            "eligible_by_horizon": eligible_by_horizon,
            "sample_sufficient_by_horizon": {
                horizon: (
                    source_provenance_complete
                    and all_source_cohorts_attested
                    and not mixed_sample_cohorts
                    and count >= _STRATEGIC_SAMPLE_MINIMUM
                )
                for horizon, count in eligible_by_horizon.items()
            },
            "by_screening_policy_id": policy_cohorts,
            "by_source_cohort_id": source_cohorts,
        },
        "summary": summary,
        "summary_by_risk_class": {
            key: _jsonable(summarize_event_study(values))
            for key, values in by_risk.items()
        },
        "observations": observations,
        "source_audits": audits,
        "reason_codes": [
            "SCREENING_MARKOUT_IS_NOT_A_TRADE_RETURN",
            "RISK_BLOCKED_CANDIDATES_NEVER_CREATE_VIRTUAL_FILLS",
            "NO_PARAMETER_FEEDBACK_OR_OPTIMIZATION",
            "FORWARD_SOURCE_SESSIONS_DELIVERY_QUALIFIED",
            *(
                ("FORWARD_SOURCE_SESSIONS_EXCLUDED",)
                if source_session_qualification.get("excluded_session_count")
                else ()
            ),
            *(
                ()
                if source_provenance_complete
                else ("MARKOUT_SOURCE_PROVENANCE_INCOMPLETE",)
            ),
            *(
                ("MIXED_SCREENING_POLICY_COHORTS_MUST_NOT_BE_POOLED",)
                if mixed_policy_cohorts
                else ()
            ),
            *(
                ("MIXED_DECISION_SOURCE_COHORTS_MUST_NOT_BE_POOLED",)
                if mixed_decision_source_cohorts
                else ()
            ),
            *(
                ()
                if source_provenance_complete
                and all_source_cohorts_attested
                and not mixed_sample_cohorts
                and all(
                    count >= _STRATEGIC_SAMPLE_MINIMUM
                    for count in eligible_by_horizon.values()
                )
                and eligible_by_horizon
                else ("STRATEGIC_MARKOUT_SAMPLE_INSUFFICIENT",)
            ),
        ],
    }
    document = {**stable, "content_sha256": sha256_json(stable)}
    validate_forward_review_markout_document(document)
    return document


def _markout_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"forward markout {label} is not a mapping")
    return value


def _markout_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"forward markout {label} is not a nonnegative integer")
    return value


def _markout_summary_counts(
    value: object,
    expected: Mapping[str, tuple[int, int]],
    label: str,
) -> None:
    summary = _markout_mapping(value, label)
    if set(summary) != set(expected):
        raise ValueError(f"forward markout {label} horizons changed")
    for horizon, (eligible, pending) in expected.items():
        row = _markout_mapping(summary[horizon], f"{label}.{horizon}")
        if row.get("eligible_count") != eligible or row.get("pending_count") != pending:
            raise ValueError(f"forward markout {label} counts changed")


def _markout_count_map(
    value: object,
    expected: Mapping[str, int],
    label: str,
) -> None:
    counts = _markout_mapping(value, label)
    if dict(counts) != dict(expected) or any(
        type(item) is not int or item < 0 for item in counts.values()
    ):
        raise ValueError(f"forward markout {label} changed")


def _markout_boolean_map(
    value: object,
    expected: Mapping[str, bool],
    label: str,
) -> None:
    values = _markout_mapping(value, label)
    if dict(values) != dict(expected) or any(
        type(item) is not bool for item in values.values()
    ):
        raise ValueError(f"forward markout {label} changed")


def _markout_optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"forward markout {label} is not a datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"forward markout {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"forward markout {label} lacks a timezone")
    return parsed


def _markout_optional_date(value: object, label: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"forward markout {label} is not a date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"forward markout {label} is invalid") from exc


def _markout_optional_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"forward markout {label} is not a decimal string")
    try:
        parsed = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError(f"forward markout {label} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"forward markout {label} is not finite")
    return parsed


def _markout_observation(
    row: Mapping[str, object],
    *,
    horizon: int,
    complete: bool,
    through_session: date,
    source_session: date,
    trading_sessions: Sequence[date],
) -> ReviewEventStudyObservation:
    candidate_id = row.get("candidate_id")
    symbol = row.get("symbol")
    if (
        not isinstance(candidate_id, str)
        or _SHA256_ID.fullmatch(candidate_id) is None
        or not isinstance(symbol, str)
        or not symbol
    ):
        raise ValueError("forward markout observation candidate changed")
    reference_at = _markout_optional_datetime(
        row.get("reference_at"),
        "observation reference_at",
    )
    reference_price = _markout_optional_decimal(
        row.get("reference_price"),
        "observation reference_price",
    )
    end_session = _markout_optional_date(
        row.get("end_session"),
        "observation end_session",
    )
    close_return = _markout_optional_decimal(
        row.get("close_return"),
        "observation close_return",
    )
    favorable = _markout_optional_decimal(
        row.get("maximum_favorable_excursion"),
        "observation maximum_favorable_excursion",
    )
    adverse = _markout_optional_decimal(
        row.get("maximum_adverse_excursion"),
        "observation maximum_adverse_excursion",
    )
    invalidation = row.get("invalidation_observed")
    if invalidation is not None and type(invalidation) is not bool:
        raise ValueError("forward markout observation invalidation changed")
    first_invalidation = _markout_optional_datetime(
        row.get("first_invalidation_at"),
        "observation first_invalidation_at",
    )
    reason = row.get("reason_code")
    if reference_price is not None and reference_price <= 0:
        raise ValueError("forward markout observation reference price is invalid")
    if reference_at is not None and reference_at.date() != source_session:
        raise ValueError("forward markout observation reference session changed")
    if end_session is not None and end_session > through_session:
        raise ValueError("forward markout observation end session is in the future")
    future_sessions = tuple(
        value for value in trading_sessions if value > source_session
    )
    expected_end_session = (
        None if len(future_sessions) < horizon else future_sessions[horizon - 1]
    )
    if end_session is not None and end_session != expected_end_session:
        raise ValueError("forward markout observation fixed horizon changed")
    if complete:
        if (
            reference_at is None
            or reference_price is None
            or end_session is None
            or close_return is None
            or favorable is None
            or adverse is None
            or reason is not None
            or end_session != expected_end_session
        ):
            raise ValueError("complete forward markout observation is incomplete")
    elif (
        close_return is not None
        or favorable is not None
        or adverse is not None
        or invalidation is not None
        or first_invalidation is not None
        or not isinstance(reason, str)
        or not reason
    ):
        raise ValueError("pending forward markout observation has outcomes")
    if (invalidation is True and first_invalidation is None) or (
        invalidation is not True and first_invalidation is not None
    ):
        raise ValueError("forward markout invalidation evidence changed")
    if first_invalidation is not None and (
        reference_at is None
        or first_invalidation <= reference_at
        or end_session is None
        or first_invalidation.date() > end_session
    ):
        raise ValueError("forward markout invalidation time changed")
    allowed_pending_reasons = {
        "NO_CAUSAL_REFERENCE_BAR",
        "INCOMPLETE_CAUSAL_REFERENCE_SESSION_GRID",
        "INSUFFICIENT_FUTURE_SESSIONS",
        "INCOMPLETE_FUTURE_SESSION_GRID",
    }
    if not complete and reason not in allowed_pending_reasons:
        raise ValueError("forward markout pending reason changed")
    if reason == "INSUFFICIENT_FUTURE_SESSIONS" and expected_end_session is not None:
        raise ValueError("forward markout session insufficiency changed")
    if reason == "INCOMPLETE_FUTURE_SESSION_GRID" and end_session is None:
        raise ValueError("forward markout incomplete session horizon changed")
    return ReviewEventStudyObservation(
        candidate_id=candidate_id,
        symbol=symbol,
        horizon_sessions=horizon,
        reference_at=reference_at,
        reference_price=reference_price,
        end_session=end_session,
        close_return=close_return,
        maximum_favorable_excursion=favorable,
        maximum_adverse_excursion=adverse,
        invalidation_observed=invalidation,
        first_invalidation_at=first_invalidation,
        complete=complete,
        reason_code=reason,
    )


def validate_forward_review_markout_document(
    payload: Mapping[str, object],
) -> None:
    """Replay the current report's safety and sample-accounting invariants.

    The content hash detects accidental edits only when the editor does not
    recompute it.  This validator independently reconstructs every cohort ID,
    observation count and sufficiency decision, and replays every embedded
    decision-source manifest.  It intentionally does not turn diagnostic
    mark-outs into trades or portfolio performance.
    """

    stable = {key: payload[key] for key in payload if key != "content_sha256"}
    claimed = payload.get("content_sha256")
    if (
        not isinstance(claimed, str)
        or _SHA256_ID.fullmatch(claimed) is None
        or claimed != sha256_json(stable)
    ):
        raise ValueError("forward markout content hash changed")
    if (
        payload.get("schema") != FORWARD_REVIEW_MARKOUT_SCHEMA
        or payload.get("sample_cohort_contract")
        != FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID
        or payload.get("source_audit_requirement")
        != FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID
        or payload.get("diagnostic_only") is not True
        or payload.get("portfolio_performance_evaluable") is not False
        or payload.get("orders_created") != 0
        or payload.get("fills_created") != 0
        or payload.get("positions_created") != 0
        or payload.get("automated_order_authorized") is not False
        or payload.get("broker_transport_available") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("forward markout safety contract changed")

    try:
        through_session = date.fromisoformat(str(payload["through_session"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("forward markout through session is invalid") from exc
    qualification = _markout_mapping(
        payload.get("source_session_qualification"),
        "source session qualification",
    )
    qualified_source_sessions = qualified_forward_review_session_dates(
        qualification,
        through_session=through_session,
    )
    calendar = _markout_mapping(
        payload.get("trading_session_calendar"),
        "trading session calendar",
    )
    sessions = calendar.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("forward markout trading sessions are not a list")
    try:
        parsed_sessions = tuple(date.fromisoformat(str(value)) for value in sessions)
    except ValueError as exc:
        raise ValueError("forward markout trading session is invalid") from exc
    calendar_stable = {
        "contract": calendar.get("contract"),
        "through_session": calendar.get("through_session"),
        "sessions": sessions,
    }
    if (
        calendar.get("contract") != "EXPLICIT_SORTED_MARKET_TRADING_SESSIONS"
        or calendar.get("through_session") != through_session.isoformat()
        or calendar.get("revision") != sha256_json(calendar_stable)
        or parsed_sessions != tuple(sorted(set(parsed_sessions)))
        or any(value > through_session for value in parsed_sessions)
        or payload.get("mature_session_grid_requirement")
        != (
            "EXACT_CAUSAL_REFERENCE_PREFIX_AND_240_COMPLETED_A_SHARE_1M_"
            "CLOSES_PER_MATURE_MARKET_SESSION"
        )
    ):
        raise ValueError("forward markout trading calendar contract changed")

    source_status = payload.get("source_provenance_status")
    if source_status not in {"COMPLETE", "INCOMPLETE"}:
        raise ValueError("forward markout source provenance status changed")
    source_complete = source_status == "COMPLETE"
    proofs = _markout_mapping(
        payload.get("decision_source_snapshots"),
        "decision source snapshots",
    )
    for source_id, snapshot in proofs.items():
        if (
            _SHA256_ID.fullmatch(str(source_id)) is None
            or decision_source_snapshot_id(snapshot) != source_id
        ):
            raise ValueError("forward markout decision source proof changed")

    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("forward markout observations are not a list")
    horizon_values = (5, 10, 20)
    all_counts = {str(value): [0, 0] for value in horizon_values}
    risk_counts = {
        risk: {str(value): [0, 0] for value in horizon_values}
        for risk in ("ALL_GREEN", "BLOCKED")
    }
    cohort_counts: dict[str, dict[str, list[int]]] = {}
    policy_counts: dict[str, dict[str, list[int]]] = {}
    lifecycle_rows: dict[str, list[Mapping[str, object]]] = {}
    cohort_lifecycles: dict[str, set[str]] = {}
    policy_lifecycles: dict[str, set[str]] = {}
    feedback_lifecycles: set[str] = set()
    observation_cohort_metadata: dict[str, tuple[str, str, str]] = {}
    observation_objects: list[ReviewEventStudyObservation] = []
    risk_observation_objects: dict[str, list[ReviewEventStudyObservation]] = {
        "ALL_GREEN": [],
        "BLOCKED": [],
    }
    cohort_observation_objects: dict[str, list[ReviewEventStudyObservation]] = {}
    policy_observation_objects: dict[str, list[ReviewEventStudyObservation]] = {}
    for raw in observations:
        row = _markout_mapping(raw, "observation")
        horizon = row.get("horizon_sessions")
        complete = row.get("complete")
        if type(horizon) is not int or horizon not in horizon_values:
            raise ValueError("forward markout observation horizon changed")
        if type(complete) is not bool:
            raise ValueError("forward markout observation completion changed")
        lifecycle_id = row.get("signal_lifecycle_id")
        cohort_id = row.get("source_cohort_id")
        policy_id = row.get("source_screening_policy_id")
        decision_core_id = row.get("source_decision_core_id")
        source_snapshot_id = row.get("source_decision_source_snapshot_id")
        try:
            source_session = date.fromisoformat(str(row["source_session"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "forward markout observation source session changed"
            ) from exc
        if (
            not isinstance(lifecycle_id, str)
            or _SHA256_ID.fullmatch(lifecycle_id) is None
            or not isinstance(cohort_id, str)
            or _SHA256_ID.fullmatch(cohort_id) is None
            or not isinstance(policy_id, str)
            or not isinstance(decision_core_id, str)
            or not isinstance(source_snapshot_id, str)
            or row.get("alert_type") != "POSSIBLE_30M_BUY"
            or _SHA256_ID.fullmatch(str(row.get("source_report_content_sha256")))
            is None
            or source_session not in qualified_source_sessions
        ):
            raise ValueError("forward markout observation identity changed")
        expected_cohort_id = sha256_json(
            {
                "contract": FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
                "screening_policy_id": policy_id,
                "decision_core_id": decision_core_id,
                "decision_source_snapshot_id": source_snapshot_id,
            }
        )
        if cohort_id != expected_cohort_id:
            raise ValueError("forward markout observation cohort changed")
        metadata = (policy_id, decision_core_id, source_snapshot_id)
        prior_metadata = observation_cohort_metadata.setdefault(
            cohort_id,
            metadata,
        )
        if prior_metadata != metadata:
            raise ValueError("forward markout observation cohort collided")
        gates = (
            row.get("market_risk_gate"),
            row.get("sector_risk_gate"),
            row.get("symbol_risk_gate"),
        )
        if any(value not in {"GREEN", "AMBER", "RED", "UNRESOLVED"} for value in gates):
            raise ValueError("forward markout observation risk gate changed")
        risk_class = "ALL_GREEN" if gates == ("GREEN",) * 3 else "BLOCKED"
        if row.get("risk_class") != risk_class:
            raise ValueError("forward markout observation risk class changed")
        feedback_count = _markout_nonnegative_int(
            row.get("human_feedback_count"),
            "observation feedback count",
        )
        if feedback_count:
            feedback_lifecycles.add(lifecycle_id)
        try:
            source_session = date.fromisoformat(str(row["source_session"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("forward markout source session changed") from exc
        if source_session > through_session:
            raise ValueError("forward markout source session is in the future")
        observation = _markout_observation(
            row,
            horizon=horizon,
            complete=complete,
            through_session=through_session,
            source_session=source_session,
            trading_sessions=parsed_sessions,
        )
        horizon_key = str(horizon)
        count_index = 0 if complete else 1
        all_counts[horizon_key][count_index] += 1
        risk_counts[risk_class][horizon_key][count_index] += 1
        cohort_counts.setdefault(
            cohort_id,
            {str(value): [0, 0] for value in horizon_values},
        )[horizon_key][count_index] += 1
        policy_counts.setdefault(
            policy_id,
            {str(value): [0, 0] for value in horizon_values},
        )[horizon_key][count_index] += 1
        lifecycle_rows.setdefault(lifecycle_id, []).append(row)
        cohort_lifecycles.setdefault(cohort_id, set()).add(lifecycle_id)
        policy_lifecycles.setdefault(policy_id, set()).add(lifecycle_id)
        observation_objects.append(observation)
        risk_observation_objects[risk_class].append(observation)
        cohort_observation_objects.setdefault(cohort_id, []).append(observation)
        policy_observation_objects.setdefault(policy_id, []).append(observation)

    for lifecycle_id, rows in lifecycle_rows.items():
        if {row["horizon_sessions"] for row in rows} != set(horizon_values) or len(
            rows
        ) != len(horizon_values):
            raise ValueError(
                f"forward markout lifecycle horizons changed: {lifecycle_id}"
            )
        immutable_fields = (
            "candidate_id",
            "source_session",
            "source_report_content_sha256",
            "source_screening_policy_id",
            "source_decision_core_id",
            "source_decision_source_snapshot_id",
            "source_cohort_id",
            "risk_class",
            "human_feedback_count",
        )
        if any(
            len({str(row.get(field)) for row in rows}) != 1
            for field in immutable_fields
        ):
            raise ValueError("forward markout lifecycle evidence changed")

    expected_all = {key: (values[0], values[1]) for key, values in all_counts.items()}
    if not observations:
        expected_all = {}
    _markout_summary_counts(payload.get("summary"), expected_all, "summary")
    if payload.get("summary") != _jsonable(summarize_event_study(observation_objects)):
        raise ValueError("forward markout summary values changed")
    summary_by_risk = _markout_mapping(
        payload.get("summary_by_risk_class"),
        "summary by risk class",
    )
    if set(summary_by_risk) != {"ALL_GREEN", "BLOCKED"}:
        raise ValueError("forward markout risk classes changed")
    for risk, values in risk_counts.items():
        expected = {key: (counts[0], counts[1]) for key, counts in values.items()}
        if not any(sum(counts) for counts in values.values()):
            expected = {}
        _markout_summary_counts(
            summary_by_risk[risk],
            expected,
            f"summary by risk class {risk}",
        )
        if summary_by_risk[risk] != _jsonable(
            summarize_event_study(risk_observation_objects[risk])
        ):
            raise ValueError("forward markout risk summary values changed")

    sample = _markout_mapping(payload.get("sample"), "sample")
    source_cohorts = _markout_mapping(
        sample.get("by_source_cohort_id"),
        "source cohorts",
    )
    policy_cohorts = _markout_mapping(
        sample.get("by_screening_policy_id"),
        "screening policy cohorts",
    )
    source_cohort_ids = tuple(sorted(str(value) for value in source_cohorts))
    policy_ids = tuple(sorted(str(value) for value in policy_cohorts))
    if sample.get("source_cohort_ids") != list(source_cohort_ids):
        raise ValueError("forward markout source cohort list changed")
    if sample.get("screening_policy_ids") != list(policy_ids):
        raise ValueError("forward markout screening policy list changed")
    if set(source_cohorts) != set(cohort_counts):
        raise ValueError("forward markout source cohort population changed")
    if set(policy_cohorts) != set(policy_counts):
        raise ValueError("forward markout policy cohort population changed")

    source_attested: dict[str, bool] = {}
    referenced_source_proofs: set[str] = set()
    for cohort_id in source_cohort_ids:
        row = _markout_mapping(source_cohorts[cohort_id], "source cohort")
        policy_id = row.get("screening_policy_id")
        decision_core_id = row.get("decision_core_id")
        source_snapshot_id = row.get("decision_source_snapshot_id")
        metadata = (str(policy_id), str(decision_core_id), str(source_snapshot_id))
        if observation_cohort_metadata.get(cohort_id) != metadata:
            raise ValueError("forward markout source cohort metadata changed")
        if (
            sha256_json(
                {
                    "contract": FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID,
                    "screening_policy_id": policy_id,
                    "decision_core_id": decision_core_id,
                    "decision_source_snapshot_id": source_snapshot_id,
                }
            )
            != cohort_id
        ):
            raise ValueError("forward markout source cohort ID changed")
        attested = (
            all(
                isinstance(value, str) and _SHA256_ID.fullmatch(value) is not None
                for value in (policy_id, decision_core_id, source_snapshot_id)
            )
            and source_snapshot_id in proofs
        )
        source_attested[cohort_id] = attested
        if isinstance(source_snapshot_id, str) and source_snapshot_id in proofs:
            referenced_source_proofs.add(source_snapshot_id)
        if row.get("source_identity_attested") is not attested:
            raise ValueError("forward markout source attestation changed")
        lifecycle_count = len(cohort_lifecycles.get(cohort_id, set()))
        if row.get("unique_lifecycle_count") != lifecycle_count:
            raise ValueError("forward markout source lifecycle count changed")
        counts = cohort_counts[cohort_id]
        eligible = {key: value[0] for key, value in counts.items()}
        _markout_count_map(
            row.get("eligible_by_horizon"),
            eligible,
            "source cohort eligible counts",
        )
        sufficient = {
            key: source_complete and attested and count >= _STRATEGIC_SAMPLE_MINIMUM
            for key, count in eligible.items()
        }
        _markout_boolean_map(
            row.get("sample_sufficient_by_horizon"),
            sufficient,
            "source cohort sufficiency",
        )
        _markout_summary_counts(
            row.get("summary"),
            {key: (value[0], value[1]) for key, value in counts.items()},
            "source cohort summary",
        )
        if row.get("summary") != _jsonable(
            summarize_event_study(cohort_observation_objects[cohort_id])
        ):
            raise ValueError("forward markout source summary values changed")
    if set(proofs) != referenced_source_proofs:
        raise ValueError("forward markout decision source proofs are unbound")

    for policy_id in policy_ids:
        row = _markout_mapping(policy_cohorts[policy_id], "policy cohort")
        linked_cohorts = tuple(
            sorted(
                cohort_id
                for cohort_id, metadata in observation_cohort_metadata.items()
                if metadata[0] == policy_id
            )
        )
        if row.get("source_cohort_ids") != list(linked_cohorts):
            raise ValueError("forward markout policy source cohorts changed")
        mixed = len(linked_cohorts) > 1
        attested = all(source_attested[value] for value in linked_cohorts)
        if (
            row.get("mixed_decision_source_cohorts") is not mixed
            or row.get("source_identity_attested") is not attested
            or row.get("unique_lifecycle_count")
            != len(policy_lifecycles.get(policy_id, set()))
        ):
            raise ValueError("forward markout policy cohort status changed")
        counts = policy_counts[policy_id]
        eligible = {key: value[0] for key, value in counts.items()}
        _markout_count_map(
            row.get("eligible_by_horizon"),
            eligible,
            "policy eligible counts",
        )
        sufficient = {
            key: source_complete
            and attested
            and len(linked_cohorts) == 1
            and count >= _STRATEGIC_SAMPLE_MINIMUM
            for key, count in eligible.items()
        }
        _markout_boolean_map(
            row.get("sample_sufficient_by_horizon"),
            sufficient,
            "policy sufficiency",
        )
        _markout_summary_counts(
            row.get("summary"),
            {key: (value[0], value[1]) for key, value in counts.items()},
            "policy cohort summary",
        )
        if row.get("summary") != _jsonable(
            summarize_event_study(policy_observation_objects[policy_id])
        ):
            raise ValueError("forward markout policy summary values changed")

    unique_lifecycles = len(lifecycle_rows)
    all_attested = bool(source_cohort_ids) and all(source_attested.values())
    if observations and not all_attested:
        raise ValueError("forward markout source identity is unattested")
    implementations = {
        (metadata[1], metadata[2]) for metadata in observation_cohort_metadata.values()
    }
    global_eligible = {key: value[0] for key, value in all_counts.items()}
    if not observations:
        global_eligible = {}
    if (
        sample.get("unique_lifecycle_count") != unique_lifecycles
        or sample.get("feedback_linked_lifecycle_count") != len(feedback_lifecycles)
        or sample.get("mixed_screening_policy_cohorts") is not (len(policy_ids) > 1)
        or sample.get("mixed_sample_cohorts") is not (len(source_cohort_ids) > 1)
        or sample.get("mixed_decision_source_cohorts") is not (len(implementations) > 1)
        or sample.get("minimum_strategic_observations") != _STRATEGIC_SAMPLE_MINIMUM
    ):
        raise ValueError("forward markout global sample status changed")
    expected_identity_status = "NOT_APPLICABLE" if not observations else "ATTESTED"
    if sample.get("source_identity_status") != expected_identity_status:
        raise ValueError("forward markout source identity status changed")
    _markout_count_map(
        sample.get("eligible_by_horizon"),
        global_eligible,
        "global eligible counts",
    )
    global_sufficient = {
        key: source_complete
        and all_attested
        and len(source_cohort_ids) == 1
        and count >= _STRATEGIC_SAMPLE_MINIMUM
        for key, count in global_eligible.items()
    }
    _markout_boolean_map(
        sample.get("sample_sufficient_by_horizon"),
        global_sufficient,
        "global sufficiency",
    )

    reasons = payload.get("reason_codes")
    if not isinstance(reasons, list) or len(reasons) != len(set(reasons)):
        raise ValueError("forward markout reason codes changed")
    reason_set = set(reasons)
    required_reasons = {
        "SCREENING_MARKOUT_IS_NOT_A_TRADE_RETURN",
        "RISK_BLOCKED_CANDIDATES_NEVER_CREATE_VIRTUAL_FILLS",
        "NO_PARAMETER_FEEDBACK_OR_OPTIMIZATION",
        "FORWARD_SOURCE_SESSIONS_DELIVERY_QUALIFIED",
    }
    if not required_reasons <= reason_set:
        raise ValueError("forward markout safety reasons changed")
    conditional_reasons = {
        "MARKOUT_SOURCE_PROVENANCE_INCOMPLETE": not source_complete,
        "MIXED_SCREENING_POLICY_COHORTS_MUST_NOT_BE_POOLED": len(policy_ids) > 1,
        "MIXED_DECISION_SOURCE_COHORTS_MUST_NOT_BE_POOLED": (len(implementations) > 1),
        "DECISION_SOURCE_COHORT_UNATTESTED": (bool(observations) and not all_attested),
        "FORWARD_SOURCE_SESSIONS_EXCLUDED": bool(
            qualification.get("excluded_session_count")
        ),
        "STRATEGIC_MARKOUT_SAMPLE_INSUFFICIENT": not (
            source_complete
            and all_attested
            and len(source_cohort_ids) == 1
            and bool(global_eligible)
            and all(
                count >= _STRATEGIC_SAMPLE_MINIMUM for count in global_eligible.values()
            )
        ),
    }
    for reason, expected in conditional_reasons.items():
        if (reason in reason_set) is not expected:
            raise ValueError("forward markout conditional reasons changed")


__all__ = (
    "FORWARD_REVIEW_MARKOUT_SCHEMA",
    "FORWARD_REVIEW_SAMPLE_COHORT_CONTRACT_ID",
    "FORWARD_REVIEW_SESSION_QUALIFICATION_SCHEMA",
    "FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID",
    "ForwardReviewSample",
    "build_forward_review_markout",
    "qualified_forward_review_session_dates",
    "review_price_bars_revision",
    "select_first_strategic_buy_samples",
    "validate_forward_review_markout_document",
)
