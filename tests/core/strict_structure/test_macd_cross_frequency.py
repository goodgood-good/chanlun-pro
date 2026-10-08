"""U293's bounded 5m/15m MACD facts never certify a formal signal."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from chanlun.core.macd import MACD
from chanlun.core.strict_structure.macd_cross_frequency import (
    observe_u293_macd_cross_frequency,
)
from chanlun.core.types import Kline


CN = ZoneInfo("Asia/Shanghai")


def source_day():
    day = date(2026, 1, 5)
    closes = tuple(
        datetime.combine(day, time(hour, minute), CN) + timedelta(minutes=offset)
        for hour, minute in ((9, 30), (13, 0))
        for offset in range(5, 121, 5)
    )
    histogram = [-1.0] * len(closes)
    histogram[5:8] = [2.0, 3.0, 2.0]
    histogram[15:18] = [1.0, 1.0, 1.0]
    return closes, tuple(100.0 + index for index in range(len(closes))), histogram


def observe(labels, known, prices, histogram, *, as_of=None, closed_through=None,
            label="end", market="a"):
    return observe_u293_macd_cross_frequency(
        labels, known, prices, histogram,
        physical_frequency="5m", market=market, bar_time_label=label,
        as_of=as_of or known[-1], closed_through=closed_through or labels[-1],
        price_basis_revision="test-raw",
    )


def test_completed_red_groups_and_fifteen_buckets_are_facts_only():
    labels, prices, histogram = source_day()
    state = observe(labels, labels, prices, histogram)

    assert state["status"] == "cross_frequency_facts_observed_author_condition_unresolved"
    assert state["five_minute_completed_red_group_count"] == 2
    assert state["five_minute_last_red_group_area"] == 3.0
    assert state["five_minute_previous_red_group_area"] == 7.0
    assert state["five_minute_red_group_area_decayed"] is True
    assert state["fifteen_minute_complete_bar_count"] == 16
    assert state["fifteen_minute_axis_relation"] == "both_above_zero"
    assert state["last_fifteen_minute_known_at"] == int(labels[-1].timestamp())
    assert state["joined_facts_known_at"] <= int(labels[-1].timestamp())
    assert state["source_policy"] == "complete_15m_buckets_derived_from_native_5m"
    native_fifteen = [
        Kline(index=index, date=labels[(index + 1) * 3 - 1],
              h=prices[(index + 1) * 3 - 1], l=prices[(index + 1) * 3 - 1],
              o=prices[(index + 1) * 3 - 1], c=prices[(index + 1) * 3 - 1], a=0)
        for index in range(16)
    ]
    reference = MACD()
    reference.process_macd(native_fifteen)
    assert state["fifteen_minute_dif"] == pytest.approx(reference.dif[-1])
    assert state["fifteen_minute_dea"] == pytest.approx(reference.dea[-1])
    assert state["author_zero_axis_line_policy"] == "unspecified"
    assert state["author_condition_certified"] is False
    assert state["formal_divergence_independent"] is True
    assert state["formal_point_independent"] is True
    assert state["tradable"] is False


def test_open_red_group_and_future_bar_do_not_finish_the_case():
    labels, prices, histogram = source_day()
    histogram[-1] = 2.0
    pending = observe(labels, labels, prices, histogram)
    assert pending["status"] == "current_red_group_unfinished"
    assert pending["five_minute_red_group_area_decayed"] is None
    future = labels[-1] + timedelta(days=1, minutes=5)
    repeated = observe(
        (*labels, future), (*labels, None), (*prices, 999.0),
        (*histogram, 999.0), as_of=labels[-1], closed_through=labels[-1],
    )
    assert repeated == pending


def test_missing_or_incomplete_source_never_synthesizes_fifteen_minute_evidence():
    labels, prices, histogram = source_day()
    gap = observe(
        labels[:10] + labels[11:], labels[:10] + labels[11:],
        prices[:10] + prices[11:], histogram[:10] + histogram[11:],
    )
    assert gap["status"] == "pending_five_minute_session_gap"
    early = observe(labels[:2], labels[:2], prices[:2], histogram[:2])
    assert early["status"] == "pending_complete_fifteen_minute_bucket"
    assert early["author_condition_certified"] is False


def test_start_labels_reconstruct_the_same_closed_fifteen_minute_facts():
    closes, prices, histogram = source_day()
    end_label = observe(closes, closes, prices, histogram)
    starts = tuple(item - timedelta(minutes=5) for item in closes)
    start_label = observe(
        starts, closes, prices, histogram,
        as_of=closes[-1], closed_through=starts[-1], label="start",
    )
    for key in (
        "fifteen_minute_complete_bar_count", "fifteen_minute_dif",
        "fifteen_minute_dea", "fifteen_minute_axis_relation",
        "five_minute_red_group_area_decayed", "joined_facts_known_at",
    ):
        assert start_label[key] == end_label[key]


def test_incomplete_next_fifteen_bucket_cannot_change_closed_higher_macd():
    labels, prices, histogram = source_day()
    baseline = observe(labels, labels, prices, histogram)
    next_day = date(2026, 1, 6)
    two_closes = tuple(
        datetime.combine(next_day, time(9, 30), CN) + timedelta(minutes=offset)
        for offset in (5, 10)
    )
    extended = observe(
        labels + two_closes, labels + two_closes,
        prices + (200.0, 205.0), tuple(histogram) + (-1.0, -1.0),
        as_of=two_closes[-1], closed_through=two_closes[-1],
    )
    assert extended["fifteen_minute_complete_bar_count"] == baseline[
        "fifteen_minute_complete_bar_count"
    ]
    assert extended["fifteen_minute_dif"] == baseline["fifteen_minute_dif"]
    assert extended["fifteen_minute_dea"] == baseline["fifteen_minute_dea"]
    assert extended["last_fifteen_minute_known_at"] == baseline[
        "last_fifteen_minute_known_at"
    ]


def test_other_market_is_outside_bounded_a_share_case():
    labels, prices, histogram = source_day()
    state = observe(labels, labels, prices, histogram, market="us")
    assert state["status"] == "not_applicable_source_period_or_market"
    assert state["tradable"] is False
