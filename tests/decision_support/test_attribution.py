from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.attribution import (
    AttributionEventInput,
    AttributionReviewInput,
    AttributionRiskInput,
    CompletedTradeInput,
    TradeAttribution,
    attribute_trades,
    group_attribution,
)
from chanlun.decision_support.models import StrategyTrack


TZ = ZoneInfo("Asia/Shanghai")
FP_ENTRY_TREND = "sha256:" + "1" * 64
FP_EXIT_TREND = "sha256:" + "2" * 64
FP_ENTRY_REVERSAL = "sha256:" + "3" * 64
FP_EXIT_REVERSAL = "sha256:" + "4" * 64


def ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 13, hour, minute, tzinfo=TZ)


def event(
    event_id: str,
    track: StrategyTrack,
    fingerprint: str,
    *,
    observed_at: datetime,
    bs_type: str,
    level: int,
    nest_depth: int,
    regime: str,
    code: str = "SH.600001",
) -> AttributionEventInput:
    return AttributionEventInput(
        event_id=event_id,
        strategy_track=track,
        data_fingerprint=fingerprint,
        code=code,
        buy_sell_class=bs_type,
        level=level,
        nest_depth=nest_depth,
        regime=regime,
        observed_at=observed_at,
    )


def review(
    review_id: str,
    event_id: str,
    fingerprint: str,
    *,
    original_text: int,
    original_chart: int,
    secondary: int,
) -> AttributionReviewInput:
    return AttributionReviewInput(
        review_id=review_id,
        event_id=event_id,
        reviewed_data_fingerprint=fingerprint,
        model_verdict="CONFIRM",
        original_text_evidence_count=original_text,
        original_chart_evidence_count=original_chart,
        secondary_evidence_count=secondary,
        reviewed_at=ts(10, 2),
    )


def risk(
    snapshot_id: str,
    event_id: str,
    fingerprint: str,
) -> AttributionRiskInput:
    return AttributionRiskInput(
        risk_snapshot_id=snapshot_id,
        event_id=event_id,
        event_data_fingerprint=fingerprint,
        approved=True,
        evaluated_at=ts(10, 3),
    )


def trade(
    trade_id: str,
    track: StrategyTrack,
    *,
    entry_event_id: str,
    exit_event_id: str,
    review_id: str,
    risk_snapshot_id: str,
    entry_fingerprint: str,
    exit_fingerprint: str,
    return_net: Decimal,
) -> CompletedTradeInput:
    return CompletedTradeInput(
        trade_id=trade_id,
        strategy_track=track,
        code="SH.600001",
        entry_event_id=entry_event_id,
        exit_event_id=exit_event_id,
        review_id=review_id,
        risk_snapshot_id=risk_snapshot_id,
        entry_event_data_fingerprint=entry_fingerprint,
        exit_event_data_fingerprint=exit_fingerprint,
        entered_at=ts(10, 5),
        exited_at=ts(11, 5),
        entry_price=Decimal("10.00"),
        exit_price=Decimal("10.80"),
        commission_cost=Decimal("0.001"),
        stamp_duty_cost=Decimal("0.0005"),
        slippage_cost=Decimal("0.0008"),
        other_cost=Decimal("0.0001"),
        return_net=return_net,
        mae=Decimal("-0.02"),
        mfe=Decimal("0.11"),
        holding_bars=12,
    )


@pytest.fixture
def attribution_fixture() -> dict[str, tuple[object, ...]]:
    events = (
        event(
            "trend-entry",
            StrategyTrack.TREND_CONTINUATION,
            FP_ENTRY_TREND,
            observed_at=ts(10),
            bs_type="2buy",
            level=1,
            nest_depth=0,
            regime="uptrend",
        ),
        event(
            "trend-exit",
            StrategyTrack.TREND_CONTINUATION,
            FP_EXIT_TREND,
            observed_at=ts(11),
            bs_type="1sell",
            level=1,
            nest_depth=0,
            regime="uptrend",
        ),
        event(
            "reversal-entry",
            StrategyTrack.BOTTOM_REVERSAL,
            FP_ENTRY_REVERSAL,
            observed_at=ts(10),
            bs_type="1buy_nest",
            level=2,
            nest_depth=2,
            regime="bottoming",
        ),
        event(
            "reversal-exit",
            StrategyTrack.BOTTOM_REVERSAL,
            FP_EXIT_REVERSAL,
            observed_at=ts(11),
            bs_type="2sell",
            level=2,
            nest_depth=1,
            regime="bottoming",
        ),
    )
    reviews = (
        review(
            "review-trend",
            "trend-entry",
            FP_ENTRY_TREND,
            original_text=2,
            original_chart=1,
            secondary=0,
        ),
        review(
            "review-reversal",
            "reversal-entry",
            FP_ENTRY_REVERSAL,
            original_text=1,
            original_chart=0,
            secondary=3,
        ),
    )
    risks = (
        risk("risk-trend", "trend-entry", FP_ENTRY_TREND),
        risk("risk-reversal", "reversal-entry", FP_ENTRY_REVERSAL),
    )
    trades = (
        trade(
            "trade-trend",
            StrategyTrack.TREND_CONTINUATION,
            entry_event_id="trend-entry",
            exit_event_id="trend-exit",
            review_id="review-trend",
            risk_snapshot_id="risk-trend",
            entry_fingerprint=FP_ENTRY_TREND,
            exit_fingerprint=FP_EXIT_TREND,
            return_net=Decimal("0.075"),
        ),
        trade(
            "trade-reversal",
            StrategyTrack.BOTTOM_REVERSAL,
            entry_event_id="reversal-entry",
            exit_event_id="reversal-exit",
            review_id="review-reversal",
            risk_snapshot_id="risk-reversal",
            entry_fingerprint=FP_ENTRY_REVERSAL,
            exit_fingerprint=FP_EXIT_REVERSAL,
            return_net=Decimal("-0.015"),
        ),
    )
    return {
        "events": events,
        "reviews": reviews,
        "risk_snapshots": risks,
        "trades": trades,
    }


def test_attribution_never_combines_strategy_tracks(attribution_fixture) -> None:
    rows = attribute_trades(**attribution_fixture)
    grouped = group_attribution(rows)

    assert set(grouped) == {"trend_continuation", "bottom_reversal"}
    assert grouped["trend_continuation"]["all"]["net_return_sum"] == "0.075"
    assert grouped["bottom_reversal"]["all"]["net_return_sum"] == "-0.015"
    assert grouped["trend_continuation"]["secondary"]["row_count"] == 0
    assert grouped["bottom_reversal"]["secondary"]["row_count"] == 1


def test_post_exit_attribution_preserves_immutable_trade_and_event_facts(
    attribution_fixture,
) -> None:
    rows = attribute_trades(**attribution_fixture)
    row = rows[0]

    assert row.exit_event_id == "trend-exit"
    assert row.return_net == Decimal("0.075")
    assert row.entry_buy_sell_class == "2buy"
    assert row.exit_buy_sell_class == "1sell"
    assert row.entry_level == 1
    assert row.exit_level == 1
    assert row.entry_nest_depth == 0
    assert row.exit_nest_depth == 0
    assert row.original_text_evidence_count == 2
    assert row.original_chart_evidence_count == 1
    assert row.secondary_evidence_count == 0
    assert row.model_verdict == "CONFIRM"
    assert row.regime == "uptrend"
    assert row.total_cost == Decimal("0.0024")
    assert row.slippage_cost == Decimal("0.0008")
    assert row.mae == Decimal("-0.02")
    assert row.mfe == Decimal("0.11")
    assert row.holding_bars == 12
    assert row.promotion_eligible is True


@pytest.mark.parametrize(
    ("collection", "identifier", "reason"),
    (
        ("events", "trend-entry", "missing_entry_event"),
        ("events", "trend-exit", "missing_exit_event"),
        ("reviews", "review-trend", "missing_review"),
        ("risk_snapshots", "risk-trend", "missing_risk_snapshot"),
    ),
)
def test_missing_links_produce_explicit_rejected_rows(
    attribution_fixture,
    collection: str,
    identifier: str,
    reason: str,
) -> None:
    values = dict(attribution_fixture)
    id_field = {
        "events": "event_id",
        "reviews": "review_id",
        "risk_snapshots": "risk_snapshot_id",
    }[collection]
    values[collection] = tuple(
        item for item in values[collection] if getattr(item, id_field) != identifier
    )

    row = attribute_trades(**values)[0]

    assert row.status == "rejected"
    assert row.promotion_eligible is False
    assert reason in row.rejection_reasons
    assert row.strategy_track == "trend_continuation"


@pytest.mark.parametrize("event_index", (0, 1))
def test_entry_or_exit_track_conflict_is_rejected_without_moving_tracks(
    attribution_fixture,
    event_index: int,
) -> None:
    values = dict(attribution_fixture)
    events = list(values["events"])
    events[event_index] = replace(
        events[event_index], strategy_track=StrategyTrack.BOTTOM_REVERSAL
    )
    values["events"] = tuple(events)

    row = attribute_trades(**values)[0]
    grouped = group_attribution((row,))

    assert "track_conflict" in row.rejection_reasons
    assert row.promotion_eligible is False
    assert set(grouped) == {"trend_continuation"}


@pytest.mark.parametrize(
    ("target", "reason"),
    (
        ("trade_entry", "entry_event_fingerprint_drift"),
        ("trade_exit", "exit_event_fingerprint_drift"),
        ("review", "review_fingerprint_drift"),
        ("risk", "risk_fingerprint_drift"),
    ),
)
def test_fingerprint_drift_fails_closed(
    attribution_fixture,
    target: str,
    reason: str,
) -> None:
    values = dict(attribution_fixture)
    if target == "trade_entry":
        values["trades"] = (
            replace(values["trades"][0], entry_event_data_fingerprint=FP_EXIT_TREND),
            values["trades"][1],
        )
    elif target == "trade_exit":
        values["trades"] = (
            replace(values["trades"][0], exit_event_data_fingerprint=FP_ENTRY_TREND),
            values["trades"][1],
        )
    elif target == "review":
        values["reviews"] = (
            replace(values["reviews"][0], reviewed_data_fingerprint=FP_EXIT_TREND),
            values["reviews"][1],
        )
    else:
        values["risk_snapshots"] = (
            replace(values["risk_snapshots"][0], event_data_fingerprint=FP_EXIT_TREND),
            values["risk_snapshots"][1],
        )

    row = attribute_trades(**values)[0]

    assert reason in row.rejection_reasons
    assert row.promotion_eligible is False


def test_review_and_risk_join_only_by_stored_ids(attribution_fixture) -> None:
    values = dict(attribution_fixture)
    values["reviews"] = (
        replace(values["reviews"][0], event_id="reversal-entry"),
        values["reviews"][1],
    )
    values["risk_snapshots"] = (
        replace(values["risk_snapshots"][0], event_id="reversal-entry"),
        values["risk_snapshots"][1],
    )

    row = attribute_trades(**values)[0]

    assert "review_event_mismatch" in row.rejection_reasons
    assert "risk_event_mismatch" in row.rejection_reasons
    assert row.model_verdict == "CONFIRM"


def test_rows_and_grouped_output_are_json_safe_and_frozen(
    attribution_fixture,
) -> None:
    rows = attribute_trades(**attribution_fixture)
    payload = [row.to_dict() for row in rows]
    grouped = group_attribution(rows)

    encoded = json.dumps(
        {"rows": payload, "grouped": grouped},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )
    decoded = json.loads(encoded)

    assert '"return_net": "0.075"' in encoded
    assert payload[0]["entered_at"].endswith("+08:00")
    assert {row["strategy_track"] for row in decoded["rows"]} == {
        "trend_continuation",
        "bottom_reversal",
    }
    assert all(
        set(row["evidence_counts"])
        == {"original_text", "original_chart", "secondary"}
        for row in decoded["rows"]
    )
    with pytest.raises(FrozenInstanceError):
        rows[0].status = "accepted"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("return_net", Decimal("NaN"), "return_net"),
        ("slippage_cost", float("inf"), "slippage_cost"),
        ("entered_at", datetime(2026, 7, 13, 10, 5), "entered_at"),
        ("holding_bars", True, "holding_bars"),
    ),
)
def test_trade_input_rejects_non_decimal_non_finite_or_invalid_time_values(
    attribution_fixture,
    field: str,
    value: object,
    message: str,
) -> None:
    source = attribution_fixture["trades"][0]

    with pytest.raises((TypeError, ValueError), match=message):
        replace(source, **{field: value})


def test_duplicate_join_ids_fail_closed(attribution_fixture) -> None:
    values = dict(attribution_fixture)
    values["reviews"] = (
        values["reviews"][0],
        replace(values["reviews"][0], model_verdict="WATCH"),
    )

    with pytest.raises(ValueError, match="duplicate review_id"):
        attribute_trades(**values)


def test_uncited_or_unapproved_inputs_are_rejected(attribution_fixture) -> None:
    values = dict(attribution_fixture)
    values["reviews"] = (
        replace(
            values["reviews"][0],
            original_text_evidence_count=0,
            original_chart_evidence_count=0,
            secondary_evidence_count=0,
        ),
        values["reviews"][1],
    )
    values["risk_snapshots"] = (
        replace(values["risk_snapshots"][0], approved=False),
        values["risk_snapshots"][1],
    )

    row = attribute_trades(**values)[0]

    assert "uncited_review" in row.rejection_reasons
    assert "risk_not_approved" in row.rejection_reasons
    assert row.promotion_eligible is False


@pytest.mark.parametrize(
    ("review_change", "risk_change", "exit_change", "reason"),
    (
        ({"model_verdict": "WATCH"}, {}, {}, "review_not_executable"),
        ({"reviewed_at": ts(10, 6)}, {}, {}, "review_after_entry"),
        ({}, {"evaluated_at": ts(10, 6)}, {}, "risk_after_entry"),
        ({}, {}, {"observed_at": ts(10, 4)}, "exit_before_entry"),
        (
            {
                "original_text_evidence_count": 0,
                "original_chart_evidence_count": 0,
                "secondary_evidence_count": 2,
            },
            {},
            {},
            "missing_original_evidence",
        ),
    ),
)
def test_attribution_rejects_non_executable_or_post_entry_inputs(
    attribution_fixture,
    review_change,
    risk_change,
    exit_change,
    reason,
) -> None:
    values = dict(attribution_fixture)
    values["reviews"] = (
        replace(values["reviews"][0], **review_change),
        values["reviews"][1],
    )
    values["risk_snapshots"] = (
        replace(values["risk_snapshots"][0], **risk_change),
        values["risk_snapshots"][1],
    )
    values["events"] = (
        values["events"][0],
        replace(values["events"][1], **exit_change),
        *values["events"][2:],
    )

    row = attribute_trades(**values)[0]

    assert reason in row.rejection_reasons
    assert row.status == "rejected"
    assert row.promotion_eligible is False


def test_public_output_type_requires_exactly_one_strategy_track() -> None:
    with pytest.raises(ValueError, match="strategy_track"):
        TradeAttribution(
            trade_id="trade",
            strategy_track="combined",
            entry_event_id="entry",
            exit_event_id="exit",
            review_id="review",
            risk_snapshot_id="risk",
            status="rejected",
            rejection_reasons=("track_conflict",),
            promotion_eligible=False,
            code="SH.600001",
            entry_buy_sell_class=None,
            exit_buy_sell_class=None,
            entry_level=None,
            exit_level=None,
            entry_nest_depth=None,
            exit_nest_depth=None,
            original_text_evidence_count=0,
            original_chart_evidence_count=0,
            secondary_evidence_count=0,
            model_verdict=None,
            regime=None,
            risk_approved=None,
            entered_at=ts(10),
            exited_at=ts(11),
            entry_price=Decimal("10"),
            exit_price=Decimal("11"),
            commission_cost=Decimal("0"),
            stamp_duty_cost=Decimal("0"),
            slippage_cost=Decimal("0"),
            other_cost=Decimal("0"),
            total_cost=Decimal("0"),
            return_net=Decimal("0.1"),
            mae=Decimal("-0.01"),
            mfe=Decimal("0.1"),
            holding_bars=1,
        )
