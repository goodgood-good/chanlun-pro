from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import re
from typing import Iterable

from .fingerprints import normalize_datetime
from .models import StrategyTrack


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL_VERDICTS = frozenset({"CONFIRM", "WATCH", "REJECT", "ABSTAIN"})


def _text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 255
    ):
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must use sha256:<64 lowercase hex>"
        )
    return value


def _track(value: object) -> str:
    if isinstance(value, StrategyTrack):
        return value.value
    try:
        return StrategyTrack(value).value
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy_track must identify exactly one track") from exc


def _count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_count(value: object, field_name: str) -> int:
    normalized = _count(value, field_name)
    if normalized == 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return normalized


def _decimal(
    value: object,
    field_name: str,
    *,
    non_negative: bool = False,
    positive: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if non_negative and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _moment(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a timezone-aware datetime")
    return normalize_datetime(value, field_name)


def _model_verdict(value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or value not in _MODEL_VERDICTS:
        raise ValueError("model_verdict is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AttributionEventInput:
    event_id: str
    strategy_track: StrategyTrack | str
    data_fingerprint: str
    code: str
    buy_sell_class: str
    level: int
    nest_depth: int
    regime: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "strategy_track", _track(self.strategy_track))
        object.__setattr__(
            self,
            "data_fingerprint",
            _fingerprint(self.data_fingerprint, "data_fingerprint"),
        )
        for field_name in ("code", "buy_sell_class", "regime"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "level", _count(self.level, "level"))
        object.__setattr__(
            self,
            "nest_depth",
            _count(self.nest_depth, "nest_depth"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _moment(self.observed_at, "observed_at"),
        )


@dataclass(frozen=True, slots=True)
class AttributionReviewInput:
    review_id: str
    event_id: str
    reviewed_data_fingerprint: str
    model_verdict: str
    original_text_evidence_count: int
    original_chart_evidence_count: int
    secondary_evidence_count: int
    reviewed_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("review_id", "event_id"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "reviewed_data_fingerprint",
            _fingerprint(
                self.reviewed_data_fingerprint,
                "reviewed_data_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "model_verdict",
            _model_verdict(self.model_verdict),
        )
        for field_name in (
            "original_text_evidence_count",
            "original_chart_evidence_count",
            "secondary_evidence_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "reviewed_at",
            _moment(self.reviewed_at, "reviewed_at"),
        )

    @property
    def evidence_count(self) -> int:
        return (
            self.original_text_evidence_count
            + self.original_chart_evidence_count
            + self.secondary_evidence_count
        )


@dataclass(frozen=True, slots=True)
class AttributionRiskInput:
    risk_snapshot_id: str
    event_id: str
    event_data_fingerprint: str
    approved: bool
    evaluated_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("risk_snapshot_id", "event_id"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "event_data_fingerprint",
            _fingerprint(
                self.event_data_fingerprint,
                "event_data_fingerprint",
            ),
        )
        if not isinstance(self.approved, bool):
            raise ValueError("approved must be boolean")
        object.__setattr__(
            self,
            "evaluated_at",
            _moment(self.evaluated_at, "evaluated_at"),
        )


@dataclass(frozen=True, slots=True)
class CompletedTradeInput:
    trade_id: str
    strategy_track: StrategyTrack | str
    code: str
    entry_event_id: str
    exit_event_id: str
    review_id: str
    risk_snapshot_id: str
    entry_event_data_fingerprint: str
    exit_event_data_fingerprint: str
    entered_at: datetime
    exited_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    commission_cost: Decimal
    stamp_duty_cost: Decimal
    slippage_cost: Decimal
    other_cost: Decimal
    return_net: Decimal
    mae: Decimal
    mfe: Decimal
    holding_bars: int

    def __post_init__(self) -> None:
        for field_name in (
            "trade_id",
            "code",
            "entry_event_id",
            "exit_event_id",
            "review_id",
            "risk_snapshot_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "strategy_track", _track(self.strategy_track))
        for field_name in (
            "entry_event_data_fingerprint",
            "exit_event_data_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _fingerprint(getattr(self, field_name), field_name),
            )
        entered_at = _moment(self.entered_at, "entered_at")
        exited_at = _moment(self.exited_at, "exited_at")
        if exited_at <= entered_at:
            raise ValueError("exited_at must be after entered_at")
        object.__setattr__(self, "entered_at", entered_at)
        object.__setattr__(self, "exited_at", exited_at)
        for field_name in ("entry_price", "exit_price"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name, positive=True),
            )
        for field_name in (
            "commission_cost",
            "stamp_duty_cost",
            "slippage_cost",
            "other_cost",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(
                    getattr(self, field_name),
                    field_name,
                    non_negative=True,
                ),
            )
        for field_name in ("return_net", "mae", "mfe"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name),
            )
        if self.mae > 0:
            raise ValueError("mae must be non-positive")
        if self.mfe < 0:
            raise ValueError("mfe must be non-negative")
        object.__setattr__(
            self,
            "holding_bars",
            _positive_count(self.holding_bars, "holding_bars"),
        )

    @property
    def total_cost(self) -> Decimal:
        return (
            self.commission_cost
            + self.stamp_duty_cost
            + self.slippage_cost
            + self.other_cost
        )


@dataclass(frozen=True, slots=True)
class TradeAttribution:
    trade_id: str
    strategy_track: StrategyTrack | str
    entry_event_id: str
    exit_event_id: str
    review_id: str
    risk_snapshot_id: str
    status: str
    rejection_reasons: tuple[str, ...]
    promotion_eligible: bool
    code: str
    entry_buy_sell_class: str | None
    exit_buy_sell_class: str | None
    entry_level: int | None
    exit_level: int | None
    entry_nest_depth: int | None
    exit_nest_depth: int | None
    original_text_evidence_count: int
    original_chart_evidence_count: int
    secondary_evidence_count: int
    model_verdict: str | None
    regime: str | None
    risk_approved: bool | None
    entered_at: datetime
    exited_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    commission_cost: Decimal
    stamp_duty_cost: Decimal
    slippage_cost: Decimal
    other_cost: Decimal
    total_cost: Decimal
    return_net: Decimal
    mae: Decimal
    mfe: Decimal
    holding_bars: int

    def __post_init__(self) -> None:
        for field_name in (
            "trade_id",
            "entry_event_id",
            "exit_event_id",
            "review_id",
            "risk_snapshot_id",
            "code",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "strategy_track", _track(self.strategy_track))
        if self.status not in {"accepted", "rejected"}:
            raise ValueError("status must be accepted or rejected")
        if not isinstance(self.rejection_reasons, tuple) or any(
            not isinstance(reason, str) or not reason
            for reason in self.rejection_reasons
        ):
            raise ValueError("rejection_reasons must be a tuple of codes")
        reasons = tuple(dict.fromkeys(self.rejection_reasons))
        object.__setattr__(self, "rejection_reasons", reasons)
        if not isinstance(self.promotion_eligible, bool):
            raise ValueError("promotion_eligible must be boolean")
        if (
            self.promotion_eligible != (self.status == "accepted" and not reasons)
            or (self.status == "rejected" and not reasons)
        ):
            raise ValueError("promotion eligibility and rejection status conflict")
        for field_name in (
            "entry_buy_sell_class",
            "exit_buy_sell_class",
            "regime",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        for field_name in (
            "entry_level",
            "exit_level",
            "entry_nest_depth",
            "exit_nest_depth",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _count(value, field_name))
        for field_name in (
            "original_text_evidence_count",
            "original_chart_evidence_count",
            "secondary_evidence_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        if self.model_verdict is not None:
            object.__setattr__(
                self,
                "model_verdict",
                _model_verdict(self.model_verdict),
            )
        if self.risk_approved is not None and not isinstance(
            self.risk_approved, bool
        ):
            raise ValueError("risk_approved must be boolean or None")
        entered_at = _moment(self.entered_at, "entered_at")
        exited_at = _moment(self.exited_at, "exited_at")
        if exited_at <= entered_at:
            raise ValueError("exited_at must be after entered_at")
        object.__setattr__(self, "entered_at", entered_at)
        object.__setattr__(self, "exited_at", exited_at)
        for field_name in ("entry_price", "exit_price"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name, positive=True),
            )
        for field_name in (
            "commission_cost",
            "stamp_duty_cost",
            "slippage_cost",
            "other_cost",
            "total_cost",
        ):
            object.__setattr__(
                self,
                field_name,
                _decimal(
                    getattr(self, field_name),
                    field_name,
                    non_negative=True,
                ),
            )
        expected_cost = (
            self.commission_cost
            + self.stamp_duty_cost
            + self.slippage_cost
            + self.other_cost
        )
        if self.total_cost != expected_cost:
            raise ValueError("total_cost does not match cost components")
        for field_name in ("return_net", "mae", "mfe"):
            object.__setattr__(
                self,
                field_name,
                _decimal(getattr(self, field_name), field_name),
            )
        if self.mae > 0 or self.mfe < 0:
            raise ValueError("MAE/MFE signs are invalid")
        object.__setattr__(
            self,
            "holding_bars",
            _positive_count(self.holding_bars, "holding_bars"),
        )

    @property
    def evidence_count(self) -> int:
        return (
            self.original_text_evidence_count
            + self.original_chart_evidence_count
            + self.secondary_evidence_count
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "strategy_track": self.strategy_track,
            "entry_event_id": self.entry_event_id,
            "exit_event_id": self.exit_event_id,
            "review_id": self.review_id,
            "risk_snapshot_id": self.risk_snapshot_id,
            "status": self.status,
            "rejection_reasons": list(self.rejection_reasons),
            "promotion_eligible": self.promotion_eligible,
            "code": self.code,
            "entry_buy_sell_class": self.entry_buy_sell_class,
            "exit_buy_sell_class": self.exit_buy_sell_class,
            "entry_level": self.entry_level,
            "exit_level": self.exit_level,
            "entry_nest_depth": self.entry_nest_depth,
            "exit_nest_depth": self.exit_nest_depth,
            "evidence_counts": {
                "original_text": self.original_text_evidence_count,
                "original_chart": self.original_chart_evidence_count,
                "secondary": self.secondary_evidence_count,
            },
            "model_verdict": self.model_verdict,
            "regime": self.regime,
            "risk_approved": self.risk_approved,
            "entered_at": self.entered_at.isoformat(),
            "exited_at": self.exited_at.isoformat(),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "commission_cost": str(self.commission_cost),
            "stamp_duty_cost": str(self.stamp_duty_cost),
            "slippage_cost": str(self.slippage_cost),
            "other_cost": str(self.other_cost),
            "total_cost": str(self.total_cost),
            "return_net": str(self.return_net),
            "mae": str(self.mae),
            "mfe": str(self.mfe),
            "holding_bars": self.holding_bars,
        }


def _typed_tuple(
    values: Iterable[object],
    expected_type: type,
    field_name: str,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of typed inputs")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(
            f"{field_name} must be an iterable of typed inputs"
        ) from exc
    if any(not isinstance(value, expected_type) for value in result):
        raise TypeError(f"{field_name} contains an invalid input type")
    return result


def _unique_index(
    values: tuple[object, ...],
    field_name: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in values:
        identifier = getattr(value, field_name)
        if identifier in result:
            raise ValueError(f"duplicate {field_name}: {identifier}")
        result[identifier] = value
    return result


def attribute_trades(
    events: Iterable[AttributionEventInput],
    reviews: Iterable[AttributionReviewInput],
    trades: Iterable[CompletedTradeInput],
    risk_snapshots: Iterable[AttributionRiskInput] = (),
) -> tuple[TradeAttribution, ...]:
    event_values = _typed_tuple(events, AttributionEventInput, "events")
    review_values = _typed_tuple(reviews, AttributionReviewInput, "reviews")
    trade_values = _typed_tuple(trades, CompletedTradeInput, "trades")
    risk_values = _typed_tuple(
        risk_snapshots,
        AttributionRiskInput,
        "risk_snapshots",
    )
    events_by_id = _unique_index(event_values, "event_id")
    reviews_by_id = _unique_index(review_values, "review_id")
    risks_by_id = _unique_index(risk_values, "risk_snapshot_id")
    _unique_index(trade_values, "trade_id")

    rows = []
    for trade in trade_values:
        reasons: list[str] = []
        entry = events_by_id.get(trade.entry_event_id)
        exit_event = events_by_id.get(trade.exit_event_id)
        review = reviews_by_id.get(trade.review_id)
        risk_snapshot = risks_by_id.get(trade.risk_snapshot_id)
        if entry is None:
            reasons.append("missing_entry_event")
        if exit_event is None:
            reasons.append("missing_exit_event")
        if review is None:
            reasons.append("missing_review")
        if risk_snapshot is None:
            reasons.append("missing_risk_snapshot")

        if entry is not None:
            if entry.strategy_track != trade.strategy_track:
                reasons.append("track_conflict")
            if entry.code != trade.code:
                reasons.append("code_conflict")
            if entry.data_fingerprint != trade.entry_event_data_fingerprint:
                reasons.append("entry_event_fingerprint_drift")
            if entry.observed_at > trade.entered_at:
                reasons.append("entry_time_conflict")
        if exit_event is not None:
            if exit_event.strategy_track != trade.strategy_track:
                reasons.append("track_conflict")
            if exit_event.code != trade.code:
                reasons.append("code_conflict")
            if exit_event.data_fingerprint != trade.exit_event_data_fingerprint:
                reasons.append("exit_event_fingerprint_drift")
            if exit_event.observed_at > trade.exited_at:
                reasons.append("exit_time_conflict")
            if exit_event.observed_at < trade.entered_at:
                reasons.append("exit_before_entry")
        if (
            entry is not None
            and exit_event is not None
            and exit_event.observed_at < entry.observed_at
        ):
            reasons.append("event_chronology_conflict")

        original_text_count = 0
        original_chart_count = 0
        secondary_count = 0
        model_verdict = None
        if review is not None:
            model_verdict = review.model_verdict
            original_text_count = review.original_text_evidence_count
            original_chart_count = review.original_chart_evidence_count
            secondary_count = review.secondary_evidence_count
            if review.event_id != trade.entry_event_id:
                reasons.append("review_event_mismatch")
            if (
                entry is not None
                and review.reviewed_data_fingerprint
                != entry.data_fingerprint
            ):
                reasons.append("review_fingerprint_drift")
            if entry is not None and review.reviewed_at < entry.observed_at:
                reasons.append("review_time_conflict")
            if review.reviewed_at > trade.entered_at:
                reasons.append("review_after_entry")
            if review.evidence_count == 0:
                reasons.append("uncited_review")
            if (
                review.original_text_evidence_count == 0
                and review.original_chart_evidence_count == 0
            ):
                reasons.append("missing_original_evidence")
            if review.model_verdict != "CONFIRM":
                reasons.append("review_not_executable")

        risk_approved = None
        if risk_snapshot is not None:
            risk_approved = risk_snapshot.approved
            if risk_snapshot.event_id != trade.entry_event_id:
                reasons.append("risk_event_mismatch")
            if (
                entry is not None
                and risk_snapshot.event_data_fingerprint
                != entry.data_fingerprint
            ):
                reasons.append("risk_fingerprint_drift")
            if entry is not None and risk_snapshot.evaluated_at < entry.observed_at:
                reasons.append("risk_time_conflict")
            if risk_snapshot.evaluated_at > trade.entered_at:
                reasons.append("risk_after_entry")
            if not risk_snapshot.approved:
                reasons.append("risk_not_approved")

        rejection_reasons = tuple(dict.fromkeys(reasons))
        eligible = not rejection_reasons
        rows.append(
            TradeAttribution(
                trade_id=trade.trade_id,
                strategy_track=trade.strategy_track,
                entry_event_id=trade.entry_event_id,
                exit_event_id=trade.exit_event_id,
                review_id=trade.review_id,
                risk_snapshot_id=trade.risk_snapshot_id,
                status="accepted" if eligible else "rejected",
                rejection_reasons=rejection_reasons,
                promotion_eligible=eligible,
                code=trade.code,
                entry_buy_sell_class=(
                    entry.buy_sell_class if entry is not None else None
                ),
                exit_buy_sell_class=(
                    exit_event.buy_sell_class
                    if exit_event is not None
                    else None
                ),
                entry_level=entry.level if entry is not None else None,
                exit_level=(
                    exit_event.level if exit_event is not None else None
                ),
                entry_nest_depth=(
                    entry.nest_depth if entry is not None else None
                ),
                exit_nest_depth=(
                    exit_event.nest_depth
                    if exit_event is not None
                    else None
                ),
                original_text_evidence_count=original_text_count,
                original_chart_evidence_count=original_chart_count,
                secondary_evidence_count=secondary_count,
                model_verdict=model_verdict,
                regime=entry.regime if entry is not None else None,
                risk_approved=risk_approved,
                entered_at=trade.entered_at,
                exited_at=trade.exited_at,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                commission_cost=trade.commission_cost,
                stamp_duty_cost=trade.stamp_duty_cost,
                slippage_cost=trade.slippage_cost,
                other_cost=trade.other_cost,
                total_cost=trade.total_cost,
                return_net=trade.return_net,
                mae=trade.mae,
                mfe=trade.mfe,
                holding_bars=trade.holding_bars,
            )
        )
    return tuple(rows)


def _summary(
    rows: tuple[TradeAttribution, ...],
    *,
    strategy_track: str,
    evidence_source: str,
) -> dict[str, object]:
    eligible = tuple(row for row in rows if row.promotion_eligible)
    net_return_sum = sum(
        (row.return_net for row in eligible),
        Decimal("0"),
    )
    total_cost_sum = sum(
        (row.total_cost for row in eligible),
        Decimal("0"),
    )
    slippage_sum = sum(
        (row.slippage_cost for row in eligible),
        Decimal("0"),
    )
    evidence_counts = {
        "all": lambda row: row.evidence_count,
        "original_text": lambda row: row.original_text_evidence_count,
        "original_chart": lambda row: row.original_chart_evidence_count,
        "secondary": lambda row: row.secondary_evidence_count,
        "none": lambda row: 0,
    }
    return {
        "strategy_track": strategy_track,
        "evidence_source": evidence_source,
        "row_count": len(rows),
        "accepted_count": sum(row.status == "accepted" for row in rows),
        "rejected_count": sum(row.status == "rejected" for row in rows),
        "promotion_eligible_count": len(eligible),
        "completed_return_count": len(eligible),
        "evidence_citation_count": sum(
            evidence_counts[evidence_source](row) for row in rows
        ),
        "net_return_sum": str(net_return_sum),
        "net_return_mean": (
            str(net_return_sum / len(eligible)) if eligible else None
        ),
        "total_cost_sum": str(total_cost_sum),
        "slippage_cost_sum": str(slippage_sum),
        "holding_bars_total": sum(row.holding_bars for row in eligible),
    }


def group_attribution(
    rows: Iterable[TradeAttribution],
) -> dict[str, dict[str, dict[str, object]]]:
    values = _typed_tuple(rows, TradeAttribution, "rows")
    tracks = sorted({row.strategy_track for row in values})
    grouped: dict[str, dict[str, dict[str, object]]] = {}
    for track in tracks:
        track_rows = tuple(row for row in values if row.strategy_track == track)
        by_source = {
            "all": track_rows,
            "original_text": tuple(
                row
                for row in track_rows
                if row.original_text_evidence_count > 0
            ),
            "original_chart": tuple(
                row
                for row in track_rows
                if row.original_chart_evidence_count > 0
            ),
            "secondary": tuple(
                row for row in track_rows if row.secondary_evidence_count > 0
            ),
            "none": tuple(row for row in track_rows if row.evidence_count == 0),
        }
        grouped[track] = {
            source: _summary(
                source_rows,
                strategy_track=track,
                evidence_source=source,
            )
            for source, source_rows in by_source.items()
        }
    return grouped


__all__ = (
    "AttributionEventInput",
    "AttributionReviewInput",
    "AttributionRiskInput",
    "CompletedTradeInput",
    "TradeAttribution",
    "attribute_trades",
    "group_attribution",
)
