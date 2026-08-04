"""Content-addressed forward roll-up for warm-up structure lineage evidence.

Daily forward screens already retain the complete immutable screening snapshot.
This module turns those validated snapshots into a compact cumulative audit.  It
does not recompute Chanlun structures and it deliberately does not infer that an
event has converged merely because a later screen did not observe it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import re

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
    WarmupStructureLineageDiagnosticEnvelope,
)


FORWARD_WARMUP_STRUCTURE_LINEAGE_ROLLUP_SCHEMA = (
    "chanlun-forward-warmup-structure-lineage-rollup/v1"
)
FORWARD_WARMUP_STRUCTURE_LINEAGE_EVENT_SCHEMA = (
    "chanlun-forward-warmup-structure-lineage-event/v1"
)
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_SUBJECT_FIELDS = (
    ("market", "market_warmup_structure_lineage_diagnostic_evidence"),
    ("sector", "sector_warmup_structure_lineage_diagnostic_evidence"),
    ("symbol", "symbol_warmup_structure_lineage_diagnostic_evidence"),
)
_STRICT_SUBJECT_FIELD = (
    "sector_strict_same_5m",
    "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence",
)


@dataclass(frozen=True, slots=True)
class ForwardWarmupLineageSessionSnapshot:
    """One fully validated promoted live-screen object."""

    session: date
    live_object_file_sha256: str
    live_object_content_sha256: str
    snapshot_content_sha256: str
    signals: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.session, date) or isinstance(self.session, datetime):
            raise TypeError("forward lineage session must be a date")
        for name, value in (
            ("live_object_file_sha256", self.live_object_file_sha256),
            ("live_object_content_sha256", self.live_object_content_sha256),
            ("snapshot_content_sha256", self.snapshot_content_sha256),
        ):
            if _HASH.fullmatch(value) is None:
                raise ValueError(f"{name} must be a sha256 identity")
        if any(not isinstance(value, Mapping) for value in self.signals):
            raise TypeError("forward lineage signals must be mappings")


def _lineage_extension(
    risk: Mapping[str, object],
) -> tuple[bool, tuple[tuple[str, object], ...]]:
    contract_field = "warmup_structure_lineage_diagnostic_contract_id"
    main_fields = tuple(field for _subject, field in _SUBJECT_FIELDS)
    strict_field = _STRICT_SUBJECT_FIELD[1]
    present = tuple(field in risk for field in (contract_field, *main_fields))
    strict_present = strict_field in risk
    if not any(present):
        if strict_present:
            raise ValueError("strict forward lineage evidence has no main contract")
        return False, ()
    if (
        not all(present)
        or risk.get(contract_field)
        != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
    ):
        raise ValueError("forward lineage extension is partial or foreign")
    values = [(subject, risk.get(field)) for subject, field in _SUBJECT_FIELDS]
    if strict_present:
        values.append((_STRICT_SUBJECT_FIELD[0], risk.get(strict_field)))
    return True, tuple(values)


def _event_identity(
    *,
    subject: str,
    period: str,
    source_symbol: str,
    source_frequency: str,
    role: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": FORWARD_WARMUP_STRUCTURE_LINEAGE_EVENT_SCHEMA,
        "subject": subject,
        "period": period,
        "source_symbol": source_symbol,
        "source_frequency": source_frequency,
        "structural_point_id": role.get("point_id"),
        "point_type": role.get("point_type"),
        "trigger_line_id": role.get("trigger_line_id"),
        "prefix_center_id": role.get("prefix_center_id"),
        "reference_peer_center_id": role.get("reference_peer_center_id"),
        "prefix_trigger_role": role.get("prefix_trigger_role"),
        "reference_trigger_role": role.get("reference_trigger_role"),
        "same_core_interval": role.get("same_core_interval"),
        "one_line_phase_shift": role.get("one_line_phase_shift"),
    }


def _sorted_counts(values: Counter[str]) -> dict[str, int]:
    return dict(sorted(values.items()))


def build_forward_warmup_structure_lineage_rollup(
    sources: Sequence[ForwardWarmupLineageSessionSnapshot],
    *,
    through_session: date,
    source_session_qualification_sha256: str,
) -> dict[str, object]:
    """Aggregate qualified immutable screens without changing any active gate."""

    if not isinstance(through_session, date) or isinstance(
        through_session, datetime
    ):
        raise TypeError("through_session must be a date")
    if _HASH.fullmatch(source_session_qualification_sha256) is None:
        raise ValueError("source qualification must be a sha256 identity")
    ordered = tuple(sorted(sources, key=lambda value: value.session))
    if len({value.session for value in ordered}) != len(ordered):
        raise ValueError("forward lineage source sessions must be unique")
    if any(value.session > through_session for value in ordered):
        raise ValueError("forward lineage source follows through_session")

    source_signal_count = 0
    signals_with_lineage_extension = 0
    legacy_signal_count = 0
    signals_without_risk_count = 0
    unavailable_subject_evidence_count = 0
    session_rows: list[dict[str, object]] = []
    # A market diagnostic can be repeated on many symbol rows in one screen.
    # Count it once per qualified session and semantic content identity.
    diagnostics: dict[
        tuple[date, str, str], WarmupStructureLineageDiagnosticEnvelope
    ] = {}

    for source in ordered:
        session_extension_count = 0
        session_legacy_count = 0
        session_risk_missing_count = 0
        session_unavailable_count = 0
        session_diagnostic_keys: set[tuple[date, str, str]] = set()
        for signal in source.signals:
            source_signal_count += 1
            risk = signal.get("higher_timeframe_risk")
            if not isinstance(risk, Mapping):
                signals_without_risk_count += 1
                session_risk_missing_count += 1
                continue
            extension_present, values = _lineage_extension(risk)
            if not extension_present:
                legacy_signal_count += 1
                session_legacy_count += 1
                continue
            signals_with_lineage_extension += 1
            session_extension_count += 1
            for subject, raw in values:
                if raw is None:
                    unavailable_subject_evidence_count += 1
                    session_unavailable_count += 1
                    continue
                if not isinstance(raw, Mapping):
                    raise ValueError("forward lineage evidence is malformed")
                diagnostic = WarmupStructureLineageDiagnosticEnvelope.from_document(
                    raw
                )
                if diagnostic.as_of.date() > source.session:
                    raise ValueError("forward lineage diagnostic follows its session")
                key = (source.session, subject, diagnostic.content_sha256)
                diagnostics[key] = diagnostic
                session_diagnostic_keys.add(key)
        session_unrecorded_count = (
            session_legacy_count + session_risk_missing_count
        )
        if session_extension_count and session_unrecorded_count:
            recording_status = "MIXED_RECORDED_AND_LEGACY_SIGNALS"
        elif session_extension_count:
            recording_status = "RECORDED"
        elif source.signals:
            recording_status = "NOT_RECORDED_LEGACY"
        else:
            recording_status = "EMPTY_SCREEN"
        session_rows.append(
            {
                "session": source.session.isoformat(),
                "live_object_file_sha256": source.live_object_file_sha256,
                "live_object_content_sha256": source.live_object_content_sha256,
                "snapshot_content_sha256": source.snapshot_content_sha256,
                "signal_count": len(source.signals),
                "lineage_extension_signal_count": session_extension_count,
                "legacy_signal_count": session_legacy_count,
                "risk_missing_signal_count": session_risk_missing_count,
                "unrecorded_signal_count": session_unrecorded_count,
                "unavailable_subject_evidence_count": session_unavailable_count,
                "unique_lineage_diagnostic_count": len(session_diagnostic_keys),
                "recording_status": recording_status,
            }
        )

    subject_status: dict[str, Counter[str]] = defaultdict(Counter)
    subject_periods: dict[str, Counter[str]] = defaultdict(Counter)
    subject_transitions: dict[str, Counter[str]] = defaultdict(Counter)
    subject_diagnostic_count: Counter[str] = Counter()
    subject_comparison_count: Counter[str] = Counter()
    subject_role_change_count: Counter[str] = Counter()
    subject_sell_absorption_count: Counter[str] = Counter()
    event_observations: dict[str, list[dict[str, object]]] = defaultdict(list)
    event_identities: dict[str, dict[str, object]] = {}

    for (session, subject, diagnostic_sha256), diagnostic in sorted(
        diagnostics.items(),
        key=lambda value: (value[0][0], value[0][1], value[0][2]),
    ):
        subject_diagnostic_count[subject] += 1
        subject_status[subject][diagnostic.status] += 1
        for comparison in diagnostic.comparisons:
            subject_comparison_count[subject] += 1
            subject_periods[subject][comparison.period] += 1
            document = comparison.document()
            delta = document.get("delta")
            prefix = document.get("prefix_snapshot")
            if not isinstance(delta, Mapping) or not isinstance(prefix, Mapping):
                raise ValueError("forward lineage comparison is malformed")
            codes = tuple(str(value) for value in delta.get("transition_codes", []))
            subject_transitions[subject].update(codes)
            source_symbol = str(prefix.get("source_symbol") or "")
            source_frequency = str(prefix.get("source_frequency") or "")
            roles = delta.get("point_trigger_role_changes", [])
            if not isinstance(roles, list):
                raise ValueError("forward lineage role changes are malformed")
            for raw_role in roles:
                if not isinstance(raw_role, Mapping):
                    raise ValueError("forward lineage role change is malformed")
                role = dict(raw_role)
                subject_role_change_count[subject] += 1
                sell_absorbed = bool(
                    role.get("point_type") in {"1sell", "2sell"}
                    and role.get("prefix_trigger_role") == "AFTER_CENTER"
                    and role.get("reference_trigger_role")
                    == "CENTER_CONSTITUENT"
                )
                subject_sell_absorption_count[subject] += int(sell_absorbed)
                identity = _event_identity(
                    subject=subject,
                    period=comparison.period,
                    source_symbol=source_symbol,
                    source_frequency=source_frequency,
                    role=role,
                )
                event_id = sha256_json(identity)
                event_identities[event_id] = {
                    **identity,
                    "event_id": event_id,
                    "sell_trigger_absorbed": sell_absorbed,
                }
                event_observations[event_id].append(
                    {
                        "session": session.isoformat(),
                        "diagnostic_as_of": diagnostic.as_of.isoformat(),
                        "diagnostic_content_sha256": diagnostic_sha256,
                        "prefix_bar_count": comparison.prefix_bar_count,
                        "reference_bar_count": comparison.reference_bar_count,
                    }
                )

    subjects: dict[str, object] = {}
    for subject in sorted(
        set(subject_diagnostic_count)
        | set(subject_status)
        | set(subject_periods)
        | set(subject_transitions)
    ):
        subjects[subject] = {
            "unique_diagnostic_count": subject_diagnostic_count[subject],
            "diagnostic_status_counts": _sorted_counts(subject_status[subject]),
            "comparison_count": subject_comparison_count[subject],
            "comparison_period_counts": _sorted_counts(subject_periods[subject]),
            "transition_code_counts": _sorted_counts(
                subject_transitions[subject]
            ),
            "point_trigger_role_change_count": subject_role_change_count[subject],
            "sell_trigger_absorbed_count": subject_sell_absorption_count[subject],
        }

    events: list[dict[str, object]] = []
    for event_id in sorted(event_identities):
        observations = sorted(
            event_observations[event_id],
            key=lambda value: (
                str(value["session"]),
                str(value["diagnostic_as_of"]),
                str(value["diagnostic_content_sha256"]),
            ),
        )
        sessions = sorted({str(value["session"]) for value in observations})
        events.append(
            {
                **event_identities[event_id],
                "first_observed_session": sessions[0],
                "last_observed_session": sessions[-1],
                "observed_session_count": len(sessions),
                "observation_count": len(observations),
                "observations": observations,
            }
        )

    recording_counts = Counter(
        str(value["recording_status"]) for value in session_rows
    )
    recorded_session_count = sum(
        value["recording_status"]
        in {"RECORDED", "MIXED_RECORDED_AND_LEGACY_SIGNALS"}
        for value in session_rows
    )
    legacy_session_count = sum(
        value["recording_status"]
        in {"NOT_RECORDED_LEGACY", "MIXED_RECORDED_AND_LEGACY_SIGNALS"}
        for value in session_rows
    )
    if not session_rows:
        status = "NO_QUALIFIED_SESSIONS"
    elif not recorded_session_count:
        status = "NOT_RECORDED_LEGACY"
    elif legacy_session_count:
        status = "RECORDED_WITH_LEGACY_SESSIONS"
    else:
        status = "RECORDED"

    stable: dict[str, object] = {
        "schema": FORWARD_WARMUP_STRUCTURE_LINEAGE_ROLLUP_SCHEMA,
        "through_session": through_session.isoformat(),
        "source_session_qualification_sha256": (
            source_session_qualification_sha256
        ),
        "status": status,
        "qualified_session_count": len(session_rows),
        "recorded_session_count": recorded_session_count,
        "legacy_session_count": legacy_session_count,
        "session_recording_status_counts": _sorted_counts(recording_counts),
        "source_signal_count": source_signal_count,
        "lineage_extension_signal_count": signals_with_lineage_extension,
        "legacy_signal_count": legacy_signal_count,
        "risk_missing_signal_count": signals_without_risk_count,
        "unrecorded_signal_count": (
            legacy_signal_count + signals_without_risk_count
        ),
        "unavailable_subject_evidence_count": (
            unavailable_subject_evidence_count
        ),
        "unique_lineage_diagnostic_count": len(diagnostics),
        "subjects": subjects,
        "structure_event_count": len(events),
        "structure_events": events,
        "sessions": session_rows,
        "cross_session_convergence_adjudication": (
            "OBSERVATION_SERIES_ONLY_NO_ABSENCE_INFERENCE"
        ),
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "parameters_changed": False,
        "automated_order_authorized": False,
        "orders_created": 0,
        "fills_created": 0,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _count_map(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be a string count mapping")
    result = {key: _count(raw, f"{name}.{key}") for key, raw in value.items()}
    if list(result) != sorted(result):
        raise ValueError(f"{name} must be canonically ordered")
    return result


def validate_forward_warmup_structure_lineage_rollup_document(
    document: Mapping[str, object],
) -> dict[str, object]:
    """Validate a persisted roll-up without trusting its derived counters.

    The forward runner can additionally rebuild the document from its immutable
    daily sources.  The review page uses this self-contained layer to reject a
    re-hashed edit whose sessions, events, counters, or safety declarations no
    longer agree with one another.
    """

    value = dict(document)
    stable = {key: raw for key, raw in value.items() if key != "content_sha256"}
    if (
        value.get("schema")
        != FORWARD_WARMUP_STRUCTURE_LINEAGE_ROLLUP_SCHEMA
        or _HASH.fullmatch(str(value.get("content_sha256") or "")) is None
        or value.get("content_sha256") != sha256_json(stable)
    ):
        raise ValueError("forward lineage rollup identity is invalid")
    try:
        through_session = date.fromisoformat(str(value["through_session"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("forward lineage through session is invalid") from exc
    if _HASH.fullmatch(
        str(value.get("source_session_qualification_sha256") or "")
    ) is None:
        raise ValueError("forward lineage qualification identity is invalid")

    sessions = value.get("sessions")
    if not isinstance(sessions, list) or any(
        not isinstance(row, Mapping) for row in sessions
    ):
        raise ValueError("forward lineage sessions are invalid")
    session_dates: list[date] = []
    recording_counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    recorded_count = 0
    legacy_count = 0
    for index, raw_row in enumerate(sessions):
        row = dict(raw_row)
        try:
            session = date.fromisoformat(str(row["session"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("forward lineage session date is invalid") from exc
        if session > through_session:
            raise ValueError("forward lineage session follows through session")
        session_dates.append(session)
        for field in (
            "live_object_file_sha256",
            "live_object_content_sha256",
            "snapshot_content_sha256",
        ):
            if _HASH.fullmatch(str(row.get(field) or "")) is None:
                raise ValueError(f"forward lineage session {field} is invalid")
        counts = {
            field: _count(row.get(field), f"sessions[{index}].{field}")
            for field in (
                "signal_count",
                "lineage_extension_signal_count",
                "legacy_signal_count",
                "risk_missing_signal_count",
                "unrecorded_signal_count",
                "unavailable_subject_evidence_count",
                "unique_lineage_diagnostic_count",
            )
        }
        if counts["unrecorded_signal_count"] != (
            counts["legacy_signal_count"] + counts["risk_missing_signal_count"]
        ) or counts["signal_count"] != (
            counts["lineage_extension_signal_count"]
            + counts["unrecorded_signal_count"]
        ):
            raise ValueError("forward lineage session signal counts disagree")
        if counts["lineage_extension_signal_count"] and counts[
            "unrecorded_signal_count"
        ]:
            expected_status = "MIXED_RECORDED_AND_LEGACY_SIGNALS"
        elif counts["lineage_extension_signal_count"]:
            expected_status = "RECORDED"
        elif counts["signal_count"]:
            expected_status = "NOT_RECORDED_LEGACY"
        else:
            expected_status = "EMPTY_SCREEN"
        if row.get("recording_status") != expected_status:
            raise ValueError("forward lineage session recording status changed")
        recording_counts[expected_status] += 1
        recorded_count += expected_status in {
            "RECORDED",
            "MIXED_RECORDED_AND_LEGACY_SIGNALS",
        }
        legacy_count += expected_status in {
            "NOT_RECORDED_LEGACY",
            "MIXED_RECORDED_AND_LEGACY_SIGNALS",
        }
        totals.update(counts)
    if session_dates != sorted(session_dates) or len(session_dates) != len(
        set(session_dates)
    ):
        raise ValueError("forward lineage sessions are not unique and ordered")

    if _count(value.get("qualified_session_count"), "qualified_session_count") != len(
        sessions
    ) or _count(value.get("recorded_session_count"), "recorded_session_count") != (
        recorded_count
    ) or _count(value.get("legacy_session_count"), "legacy_session_count") != (
        legacy_count
    ):
        raise ValueError("forward lineage session totals changed")
    if _count_map(
        value.get("session_recording_status_counts"),
        "session_recording_status_counts",
    ) != _sorted_counts(recording_counts):
        raise ValueError("forward lineage session status totals changed")
    for top_field, session_field in (
        ("source_signal_count", "signal_count"),
        ("lineage_extension_signal_count", "lineage_extension_signal_count"),
        ("legacy_signal_count", "legacy_signal_count"),
        ("risk_missing_signal_count", "risk_missing_signal_count"),
        ("unrecorded_signal_count", "unrecorded_signal_count"),
        (
            "unavailable_subject_evidence_count",
            "unavailable_subject_evidence_count",
        ),
        ("unique_lineage_diagnostic_count", "unique_lineage_diagnostic_count"),
    ):
        if _count(value.get(top_field), top_field) != totals[session_field]:
            raise ValueError(f"forward lineage {top_field} changed")
    expected_status = (
        "NO_QUALIFIED_SESSIONS"
        if not sessions
        else (
            "NOT_RECORDED_LEGACY"
            if not recorded_count
            else (
                "RECORDED_WITH_LEGACY_SESSIONS"
                if legacy_count
                else "RECORDED"
            )
        )
    )
    if value.get("status") != expected_status:
        raise ValueError("forward lineage rollup status changed")

    subjects = value.get("subjects")
    allowed_subjects = {subject for subject, _field in _SUBJECT_FIELDS} | {
        _STRICT_SUBJECT_FIELD[0]
    }
    if (
        not isinstance(subjects, Mapping)
        or any(subject not in allowed_subjects for subject in subjects)
        or list(subjects) != sorted(subjects)
    ):
        raise ValueError("forward lineage subjects are invalid")
    for subject, raw_subject in subjects.items():
        if not isinstance(raw_subject, Mapping):
            raise ValueError("forward lineage subject is invalid")
        for field in (
            "unique_diagnostic_count",
            "comparison_count",
            "point_trigger_role_change_count",
            "sell_trigger_absorbed_count",
        ):
            _count(raw_subject.get(field), f"subjects.{subject}.{field}")
        for field in (
            "diagnostic_status_counts",
            "comparison_period_counts",
            "transition_code_counts",
        ):
            _count_map(raw_subject.get(field), f"subjects.{subject}.{field}")

    events = value.get("structure_events")
    if not isinstance(events, list) or any(
        not isinstance(event, Mapping) for event in events
    ):
        raise ValueError("forward lineage structure events are invalid")
    session_set = set(session_dates)
    event_ids: list[str] = []
    subject_role_counts: Counter[str] = Counter()
    subject_sell_counts: Counter[str] = Counter()
    identity_fields = (
        "schema",
        "subject",
        "period",
        "source_symbol",
        "source_frequency",
        "structural_point_id",
        "point_type",
        "trigger_line_id",
        "prefix_center_id",
        "reference_peer_center_id",
        "prefix_trigger_role",
        "reference_trigger_role",
        "same_core_interval",
        "one_line_phase_shift",
    )
    for raw_event in events:
        event = dict(raw_event)
        identity = {field: event.get(field) for field in identity_fields}
        event_id = str(event.get("event_id") or "")
        subject = str(event.get("subject") or "")
        if (
            identity["schema"] != FORWARD_WARMUP_STRUCTURE_LINEAGE_EVENT_SCHEMA
            or subject not in subjects
            or event_id != sha256_json(identity)
        ):
            raise ValueError("forward lineage event identity changed")
        sell_absorbed = bool(
            event.get("point_type") in {"1sell", "2sell"}
            and event.get("prefix_trigger_role") == "AFTER_CENTER"
            and event.get("reference_trigger_role") == "CENTER_CONSTITUENT"
        )
        if event.get("sell_trigger_absorbed") is not sell_absorbed:
            raise ValueError("forward lineage sell absorption changed")
        observations = event.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValueError("forward lineage event observations are invalid")
        sort_keys: list[tuple[str, str, str]] = []
        observed_sessions: set[str] = set()
        for raw_observation in observations:
            if not isinstance(raw_observation, Mapping):
                raise ValueError("forward lineage event observation is invalid")
            try:
                observed_session = date.fromisoformat(
                    str(raw_observation["session"])
                )
                diagnostic_at = datetime.fromisoformat(
                    str(raw_observation["diagnostic_as_of"])
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(
                    "forward lineage event observation time is invalid"
                ) from exc
            diagnostic_hash = str(
                raw_observation.get("diagnostic_content_sha256") or ""
            )
            if (
                observed_session not in session_set
                or diagnostic_at.tzinfo is None
                or diagnostic_at.date() > observed_session
                or _HASH.fullmatch(diagnostic_hash) is None
            ):
                raise ValueError("forward lineage event observation is unbound")
            _count(raw_observation.get("prefix_bar_count"), "prefix_bar_count")
            _count(
                raw_observation.get("reference_bar_count"),
                "reference_bar_count",
            )
            session_text = observed_session.isoformat()
            observed_sessions.add(session_text)
            sort_keys.append(
                (session_text, diagnostic_at.isoformat(), diagnostic_hash)
            )
        ordered_sessions = sorted(observed_sessions)
        if sort_keys != sorted(sort_keys) or (
            event.get("first_observed_session") != ordered_sessions[0]
            or event.get("last_observed_session") != ordered_sessions[-1]
            or _count(
                event.get("observed_session_count"), "observed_session_count"
            )
            != len(ordered_sessions)
            or _count(event.get("observation_count"), "observation_count")
            != len(observations)
        ):
            raise ValueError("forward lineage event observation summary changed")
        subject_role_counts[subject] += len(observations)
        subject_sell_counts[subject] += len(observations) * int(sell_absorbed)
        event_ids.append(event_id)
    if event_ids != sorted(event_ids) or len(event_ids) != len(set(event_ids)):
        raise ValueError("forward lineage events are not unique and ordered")
    if _count(value.get("structure_event_count"), "structure_event_count") != len(
        events
    ):
        raise ValueError("forward lineage structure event count changed")
    for subject, raw_subject in subjects.items():
        if raw_subject["point_trigger_role_change_count"] != subject_role_counts[
            subject
        ] or raw_subject["sell_trigger_absorbed_count"] != subject_sell_counts[
            subject
        ]:
            raise ValueError("forward lineage subject event totals changed")

    if (
        value.get("cross_session_convergence_adjudication")
        != "OBSERVATION_SERIES_ONLY_NO_ABSENCE_INFERENCE"
        or value.get("diagnostic_only") is not True
        or value.get("active_gate_unchanged") is not True
        or value.get("parameters_changed") is not False
        or value.get("automated_order_authorized") is not False
        or value.get("orders_created") != 0
        or value.get("fills_created") != 0
        or value.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("forward lineage safety declaration changed")
    return value


def validate_forward_warmup_structure_lineage_rollup(
    document: Mapping[str, object],
    *,
    sources: Sequence[ForwardWarmupLineageSessionSnapshot],
    through_session: date,
    source_session_qualification_sha256: str,
) -> dict[str, object]:
    """Rebuild all derived fields; a merely re-hashed tamper must fail."""

    validate_forward_warmup_structure_lineage_rollup_document(document)
    expected = build_forward_warmup_structure_lineage_rollup(
        sources,
        through_session=through_session,
        source_session_qualification_sha256=(
            source_session_qualification_sha256
        ),
    )
    if dict(document) != expected:
        raise ValueError("forward warmup structure lineage rollup changed")
    return expected
