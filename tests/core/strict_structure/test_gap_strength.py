"""L077 gap fill needs trade-price coverage, not an OHLC range crossing."""

from datetime import datetime, timedelta, timezone

import pytest

from chanlun.core.strict_structure.gap_strength import (
    GapPostFillEvidence, GapTradePrint, GapTradeWindowEvidence,
    observe_u297_gap_strength,
)
from chanlun.core.strict_structure.general_decomposition import GapBarEvidence


BASE = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)


def _bars(up=True):
    if up:
        left, right = (90, 100), (105, 115)
    else:
        left, right = (105, 115), (90, 100)
    return (
        GapBarEvidence("before", BASE, *left, "raw"),
        GapBarEvidence("after", BASE + timedelta(minutes=5), *right, "raw"),
    )


def _window(prices, *, complete=True):
    trades = tuple(
        GapTradePrint(price, BASE + timedelta(minutes=6 + index),
                      BASE + timedelta(minutes=6 + index))
        for index, price in enumerate(prices)
    )
    return GapTradeWindowEvidence(
        source_ref="tick-ledger", source_sha256="a" * 64,
        price_basis_revision="raw", complete_from=BASE,
        complete_through=BASE + timedelta(minutes=20),
        available_at=BASE + timedelta(minutes=20), prints=trades,
        complete_source_window_certified=complete,
    )


@pytest.mark.parametrize("up", [True, False])
def test_bar_gap_without_transaction_ledger_never_becomes_filled(up):
    before, after = _bars(up)
    state = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20),
    )
    assert state["status"] == "pending_transaction_coverage"
    assert state["required_closed_tick_count"] == 6
    assert state["full_transaction_fill_certified"] is False
    assert state["gap_strength_classification_certified"] is False
    assert state["structural_trend_grade"] is None
    assert state["tradable"] is False


def test_partial_trades_are_only_unfilled_as_of_a_complete_window():
    before, after = _bars()
    state = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20),
        transaction_window=_window((100, 101, 102, 105)),
    )
    assert state["status"] == "unfilled_as_of_complete_trade_window"
    assert state["observed_gap_ticks"] == 4
    assert state["gap_strength_state"] == "strong_as_of_only"
    assert state["full_transaction_fill_certified"] is False
    assert state["tradable"] is False

    partial_feed = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20),
        transaction_window=_window((100, 101, 102, 105), complete=False),
    )
    assert partial_feed["status"] == "pending_complete_trade_window"


@pytest.mark.parametrize("up", [True, False])
def test_all_ticks_can_be_observed_before_strength_or_grade_is_known(up):
    before, after = _bars(up)
    window = _window((100, 101, 102, 103, 104, 105))
    base = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20), transaction_window=window,
    )
    assert base["status"] == "filled_strength_pending"
    assert base["full_transaction_fill_certified"] is True
    assert base["gap_strength_classification_certified"] is False

    uncertified = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20),
        transaction_window=_window((100, 101, 102, 103, 104, 105), complete=False),
    )
    assert uncertified["status"] == "conditional_tick_coverage_unverified_source_window"
    assert uncertified["full_transaction_fill_certified"] is False

    flat = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20), transaction_window=window,
        aftermath=GapPostFillEvidence(
            True, False, 0, None, BASE + timedelta(minutes=20), "formal-price-extreme",
        ),
    )
    assert flat["status"] == "flat_after_fill_new_extreme"
    assert flat["gap_strength_classification_certified"] is True
    assert flat["author_higher_grade_adjustment_certified"] is False

    weak_pending = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20), transaction_window=window,
        aftermath=GapPostFillEvidence(
            False, True, 0, None, BASE + timedelta(minutes=20), "reversal-only",
        ),
    )
    assert weak_pending["status"] == "weak_price_path_grade_pending"
    assert weak_pending["gap_strength_classification_certified"] is False

    weak = observe_u297_gap_strength(
        before, after, physical_frequency="5m",
        as_of=BASE + timedelta(minutes=20), transaction_window=window,
        aftermath=GapPostFillEvidence(
            False, True, 0, 1, BASE + timedelta(minutes=20), "graded-reversal",
        ),
    )
    assert weak["status"] == "weak_after_fill_reversal"
    assert weak["author_higher_grade_adjustment_certified"] is True
    assert weak["tradable"] is False


def test_borrowed_trade_basis_or_future_trade_clock_cannot_fill_gap():
    before, after = _bars()
    wrong = _window((100, 101, 102, 103, 104, 105))
    wrong = GapTradeWindowEvidence(
        wrong.source_ref, wrong.source_sha256, "other", wrong.complete_from,
        wrong.complete_through, wrong.available_at, wrong.prints, True,
    )
    with pytest.raises(ValueError, match="basis"):
        observe_u297_gap_strength(
            before, after, physical_frequency="5m",
            as_of=BASE + timedelta(minutes=20), transaction_window=wrong,
        )
    with pytest.raises(ValueError, match="clock"):
        observe_u297_gap_strength(
            before, after, physical_frequency="5m",
            as_of=BASE + timedelta(minutes=10),
            transaction_window=_window((100, 101, 102, 103, 104, 105)),
        )
