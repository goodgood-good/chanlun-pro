"""Fail-closed promotion gates for decision-support strategy tracks.

This module is deliberately domain-only.  It evaluates immutable metric
snapshots and returns a JSON-serializable decision; it does not start paper
trading, mutate persistence, or expose an order-execution state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
from numbers import Real
import re

from .fingerprints import normalize_datetime, sha256_json
from .models import StrategyTrack


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TRACKS = frozenset(track.value for track in StrategyTrack)
_SEQUENCE_FIELDS = (
    "paper_trading_dates",
    "corpus_manifest_fingerprints",
    "rule_set_fingerprints",
    "algorithm_fingerprints",
    "data_fingerprints",
)


class PromotionState(str, Enum):
    """The only supported rollout states; automatic live trading is absent."""

    RESEARCH = "research"
    PAPER = "paper"
    SMALL_CAP_MANUAL = "small_cap_manual"


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fingerprint_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and _FINGERPRINT_RE.fullmatch(value) is not None
    )


def _normalized_track(track: str | StrategyTrack) -> str:
    value = track.value if isinstance(track, StrategyTrack) else track
    if not isinstance(value, str) or value not in _TRACKS:
        raise ValueError("track must be a supported strategy track")
    return value


def _safe_json_value(value: object) -> object:
    """Return deterministic JSON data even for rejected hostile metrics."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$non_finite": "nan"}
        if value == math.inf:
            return {"$non_finite": "positive_infinity"}
        if value == -math.inf:
            return {"$non_finite": "negative_infinity"}
        return value
    if isinstance(value, Real):
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError):
            return {"$invalid_number": type(value).__name__}
        return _safe_json_value(converted)
    if isinstance(value, datetime):
        try:
            return normalize_datetime(value, "datetime").isoformat()
        except ValueError:
            return {"$invalid_datetime": "not_timezone_aware"}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, ComplianceConfirmation):
        return value.to_dict()
    if isinstance(value, (tuple, list)):
        return [_safe_json_value(item) for item in value]
    return {"$invalid_type": type(value).__name__}


@dataclass(frozen=True, slots=True)
class ComplianceConfirmation:
    """Audited, time-bounded permission for small-cap manual reference."""

    confirmation_id: str
    confirmed_by: str
    confirmed_at: datetime
    expires_at: datetime
    tracks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _non_empty_string(self.confirmation_id):
            raise ValueError("confirmation_id must be a non-empty string")
        if not _non_empty_string(self.confirmed_by):
            raise ValueError("confirmed_by must be a non-empty string")
        confirmed_at = normalize_datetime(self.confirmed_at, "confirmed_at")
        expires_at = normalize_datetime(self.expires_at, "expires_at")
        if expires_at <= confirmed_at:
            raise ValueError("expires_at must be after confirmed_at")
        if isinstance(self.tracks, (str, bytes)) or not isinstance(
            self.tracks, (tuple, list)
        ):
            raise ValueError("tracks must be a sequence of strategy tracks")
        tracks = tuple(self.tracks)
        if not tracks or any(track not in _TRACKS for track in tracks):
            raise ValueError("tracks must contain supported strategy tracks")
        if len(tracks) != len(set(tracks)):
            raise ValueError("tracks must not contain duplicates")
        object.__setattr__(self, "confirmed_at", confirmed_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "tracks", tracks)

    def to_dict(self) -> dict[str, object]:
        return {
            "confirmation_id": self.confirmation_id,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": _safe_json_value(self.confirmed_at),
            "expires_at": _safe_json_value(self.expires_at),
            "tracks": _safe_json_value(self.tracks),
        }


@dataclass(frozen=True, slots=True)
class PromotionMetrics:
    """Immutable evidence snapshot consumed by :func:`evaluate_promotion`.

    ``None`` means unknown, never success.  Fingerprint tuples represent the
    identities observed across independently produced validation artifacts;
    every tuple must be non-empty, valid, and internally uniform.
    """

    evaluated_at: datetime | None = None
    oos_trades: int | None = None
    net_expectancy: float | None = None
    profit_factor: float | None = None
    max_drawdown: float | None = None
    event_parity: float | None = None
    risk_violations: int | None = None
    lookahead_events: int | None = None
    zero_fill_fake_positions: int | None = None
    paper_trading_dates: tuple[date, ...] | None = None
    exchange_calendar_verified: bool | None = None
    exchange_calendar_fingerprint: str | None = None
    paper_executable_events: int | None = None
    critical_ledger_mismatches: int | None = None
    uncited_executable_reviews: int | None = None
    restart_recovery: bool | None = None
    corpus_manifest_fingerprints: tuple[str, ...] | None = None
    rule_set_fingerprints: tuple[str, ...] | None = None
    algorithm_fingerprints: tuple[str, ...] | None = None
    data_fingerprints: tuple[str, ...] | None = None
    compliance_confirmation: ComplianceConfirmation | None = None

    def __post_init__(self) -> None:
        if self.evaluated_at is not None:
            object.__setattr__(
                self,
                "evaluated_at",
                normalize_datetime(self.evaluated_at, "evaluated_at"),
            )
        for field_name in _SEQUENCE_FIELDS:
            value = getattr(self, field_name)
            if isinstance(value, (tuple, list)):
                object.__setattr__(self, field_name, tuple(value))

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated_at": _safe_json_value(self.evaluated_at),
            "oos_trades": _safe_json_value(self.oos_trades),
            "net_expectancy": _safe_json_value(self.net_expectancy),
            "profit_factor": _safe_json_value(self.profit_factor),
            "max_drawdown": _safe_json_value(self.max_drawdown),
            "event_parity": _safe_json_value(self.event_parity),
            "risk_violations": _safe_json_value(self.risk_violations),
            "lookahead_events": _safe_json_value(self.lookahead_events),
            "zero_fill_fake_positions": _safe_json_value(
                self.zero_fill_fake_positions
            ),
            "paper_trading_dates": _safe_json_value(
                self.paper_trading_dates
            ),
            "exchange_calendar_verified": _safe_json_value(
                self.exchange_calendar_verified
            ),
            "exchange_calendar_fingerprint": _safe_json_value(
                self.exchange_calendar_fingerprint
            ),
            "paper_executable_events": _safe_json_value(
                self.paper_executable_events
            ),
            "critical_ledger_mismatches": _safe_json_value(
                self.critical_ledger_mismatches
            ),
            "uncited_executable_reviews": _safe_json_value(
                self.uncited_executable_reviews
            ),
            "restart_recovery": _safe_json_value(self.restart_recovery),
            "corpus_manifest_fingerprints": _safe_json_value(
                self.corpus_manifest_fingerprints
            ),
            "rule_set_fingerprints": _safe_json_value(
                self.rule_set_fingerprints
            ),
            "algorithm_fingerprints": _safe_json_value(
                self.algorithm_fingerprints
            ),
            "data_fingerprints": _safe_json_value(self.data_fingerprints),
            "compliance_confirmation": _safe_json_value(
                self.compliance_confirmation
            ),
        }


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Deterministic result for one strategy track."""

    track: str
    state: PromotionState
    promoted: bool
    paper_gate_pending: bool
    reasons: tuple[str, ...]
    metrics: PromotionMetrics
    metrics_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "track": self.track,
            "state": self.state.value,
            "promoted": self.promoted,
            "paper_gate_pending": self.paper_gate_pending,
            "reasons": list(self.reasons),
            "evaluated_at": _safe_json_value(self.metrics.evaluated_at),
            "metrics_fingerprint": self.metrics_fingerprint,
            "metrics": self.metrics.to_dict(),
        }


def evaluate_promotion(
    track: str | StrategyTrack,
    metrics: PromotionMetrics,
) -> PromotionDecision:
    """Evaluate all historical, paper, provenance, and compliance gates.

    The output is fail-closed: missing, malformed, non-finite, mixed-version,
    or stale evidence always yields a non-promoted state with stable reason
    codes.  Each invocation evaluates exactly one strategy track.
    """

    track_value = _normalized_track(track)
    if not isinstance(metrics, PromotionMetrics):
        raise TypeError("metrics must be PromotionMetrics")

    reasons: list[str] = []
    historical_blockers: set[str] = set()
    paper_blockers: set[str] = set()

    def add(reason: str, group: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
        if group in {"historical", "both"}:
            historical_blockers.add(reason)
        if group in {"paper", "both"}:
            paper_blockers.add(reason)

    evaluated_at: datetime | None = None
    if metrics.evaluated_at is None:
        add("evaluation_time_unknown", "historical")
    elif not isinstance(metrics.evaluated_at, datetime):
        add("evaluation_time_invalid", "historical")
    else:
        try:
            evaluated_at = normalize_datetime(
                metrics.evaluated_at,
                "evaluated_at",
            )
        except ValueError:
            add("evaluation_time_invalid", "historical")

    def check_min_count(
        value: object,
        *,
        minimum: int,
        name: str,
        insufficient_reason: str,
        group: str,
    ) -> None:
        if value is None:
            add(f"{name}_unknown", group)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            add(f"{name}_invalid", group)
        elif value < minimum:
            add(insufficient_reason, group)

    def finite_metric(name: str) -> float | None:
        value = getattr(metrics, name)
        if value is None:
            add(f"{name}_unknown", "historical")
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            add(f"{name}_invalid", "historical")
            return None
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError):
            add(f"{name}_invalid", "historical")
            return None
        if not math.isfinite(converted):
            add(f"{name}_not_finite", "historical")
            return None
        return converted

    check_min_count(
        metrics.oos_trades,
        minimum=100,
        name="oos_trades",
        insufficient_reason="insufficient_oos_trades",
        group="historical",
    )
    net_expectancy = finite_metric("net_expectancy")
    if net_expectancy is not None and net_expectancy <= 0:
        add("non_positive_expectancy", "historical")
    profit_factor = finite_metric("profit_factor")
    if profit_factor is not None and profit_factor <= 1.1:
        add("profit_factor_not_above_1_1", "historical")
    max_drawdown = finite_metric("max_drawdown")
    if max_drawdown is not None:
        if max_drawdown < 0:
            add("max_drawdown_invalid", "historical")
        elif max_drawdown > 0.08:
            add("drawdown_above_8_percent", "historical")
    event_parity = finite_metric("event_parity")
    if event_parity is not None:
        if event_parity < 0 or event_parity > 1:
            add("event_parity_invalid", "historical")
        elif event_parity != 1.0:
            add("event_parity_not_100_percent", "historical")

    def check_zero_count(name: str, violation_reason: str, group: str) -> None:
        value = getattr(metrics, name)
        if value is None:
            add(f"{name}_unknown", group)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            add(f"{name}_invalid", group)
        elif value != 0:
            add(violation_reason, group)

    check_zero_count("risk_violations", "risk_violation", "historical")
    check_zero_count("lookahead_events", "lookahead_event", "historical")
    check_zero_count(
        "zero_fill_fake_positions",
        "zero_fill_fake_position",
        "historical",
    )

    dates_value = metrics.paper_trading_dates
    if dates_value is None:
        add("paper_trading_dates_unknown", "paper")
    elif isinstance(dates_value, (str, bytes)) or not isinstance(
        dates_value, (tuple, list)
    ):
        add("paper_trading_dates_invalid", "paper")
    else:
        dates = tuple(dates_value)
        if any(
            not isinstance(item, date) or isinstance(item, datetime)
            for item in dates
        ):
            add("paper_trading_dates_invalid", "paper")
        else:
            distinct_dates = set(dates)
            if len(distinct_dates) != len(dates):
                add("duplicate_paper_trading_date", "paper")
            if len(distinct_dates) < 20:
                add("insufficient_paper_trading_days", "paper")

    if metrics.exchange_calendar_verified is None:
        add("exchange_calendar_verification_unknown", "paper")
    elif not isinstance(metrics.exchange_calendar_verified, bool):
        add("exchange_calendar_verification_invalid", "paper")
    elif not metrics.exchange_calendar_verified:
        add("paper_trading_dates_unverified", "paper")
    if metrics.exchange_calendar_fingerprint is None:
        add("exchange_calendar_fingerprint_missing", "paper")
    elif not _fingerprint_is_valid(metrics.exchange_calendar_fingerprint):
        add("exchange_calendar_fingerprint_invalid", "paper")

    check_min_count(
        metrics.paper_executable_events,
        minimum=30,
        name="paper_executable_events",
        insufficient_reason="insufficient_paper_executable_events",
        group="paper",
    )
    check_zero_count(
        "critical_ledger_mismatches",
        "critical_ledger_mismatch",
        "paper",
    )
    check_zero_count(
        "uncited_executable_reviews",
        "uncited_executable_review",
        "both",
    )
    if metrics.restart_recovery is None:
        add("restart_recovery_unknown", "paper")
    elif not isinstance(metrics.restart_recovery, bool):
        add("restart_recovery_invalid", "paper")
    elif not metrics.restart_recovery:
        add("restart_recovery_failed", "paper")

    fingerprint_fields = (
        ("corpus_manifest_fingerprints", "corpus_manifest_fingerprint"),
        ("rule_set_fingerprints", "rule_set_fingerprint"),
        ("algorithm_fingerprints", "algorithm_fingerprint"),
        ("data_fingerprints", "data_fingerprint"),
    )
    fingerprint_counts: list[int] = []
    for field_name, reason_prefix in fingerprint_fields:
        values = getattr(metrics, field_name)
        if values is None:
            add(f"{reason_prefix}_missing", "historical")
            continue
        if isinstance(values, (str, bytes)) or not isinstance(
            values, (tuple, list)
        ):
            add(f"{reason_prefix}_invalid", "historical")
            continue
        items = tuple(values)
        if not items:
            add(f"{reason_prefix}_missing", "historical")
            continue
        fingerprint_counts.append(len(items))
        if any(not _fingerprint_is_valid(item) for item in items):
            add(f"{reason_prefix}_invalid", "historical")
        elif len(set(items)) != 1:
            add(f"{reason_prefix}_mixed", "historical")
    if len(fingerprint_counts) == 4 and len(set(fingerprint_counts)) != 1:
        add("fingerprint_evidence_count_mismatch", "historical")

    confirmation = metrics.compliance_confirmation
    if confirmation is None:
        add("compliance_confirmation_missing", "compliance")
    elif not isinstance(confirmation, ComplianceConfirmation):
        add("compliance_confirmation_invalid", "compliance")
    else:
        valid_confirmation = True
        if not _non_empty_string(
            confirmation.confirmation_id
        ) or not _non_empty_string(confirmation.confirmed_by):
            valid_confirmation = False
        try:
            confirmed_at = normalize_datetime(
                confirmation.confirmed_at,
                "confirmed_at",
            )
            expires_at = normalize_datetime(
                confirmation.expires_at,
                "expires_at",
            )
        except (AttributeError, ValueError):
            valid_confirmation = False
            confirmed_at = None
            expires_at = None
        if (
            confirmed_at is not None
            and expires_at is not None
            and expires_at <= confirmed_at
        ):
            valid_confirmation = False
        tracks = confirmation.tracks
        if (
            isinstance(tracks, (str, bytes))
            or not isinstance(tracks, (tuple, list))
            or not tracks
            or any(
                not isinstance(item, str) or item not in _TRACKS
                for item in tracks
            )
            or len(set(tracks)) != len(tracks)
        ):
            valid_confirmation = False
        if not valid_confirmation:
            add("compliance_confirmation_invalid", "compliance")
        else:
            if track_value not in tracks:
                add("compliance_confirmation_track_mismatch", "compliance")
            if evaluated_at is not None and confirmed_at is not None:
                if confirmed_at > evaluated_at:
                    add(
                        "compliance_confirmation_not_yet_effective",
                        "compliance",
                    )
                elif expires_at is not None and expires_at <= evaluated_at:
                    add("compliance_confirmation_expired", "compliance")

    paper_gate_pending = bool(paper_blockers)
    if paper_gate_pending:
        add("paper_gate_pending", "paper_status")

    promoted = not reasons
    if promoted:
        state = PromotionState.SMALL_CAP_MANUAL
    elif not historical_blockers:
        state = PromotionState.PAPER
    else:
        state = PromotionState.RESEARCH

    return PromotionDecision(
        track=track_value,
        state=state,
        promoted=promoted,
        paper_gate_pending=paper_gate_pending,
        reasons=tuple(reasons),
        metrics=metrics,
        metrics_fingerprint=sha256_json(metrics.to_dict()),
    )


__all__ = [
    "ComplianceConfirmation",
    "PromotionDecision",
    "PromotionMetrics",
    "PromotionState",
    "evaluate_promotion",
]
