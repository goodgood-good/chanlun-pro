from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from tests.core.strict_structure.helpers import BASE


class FakeLine:
    def __init__(
        self,
        index: int,
        direction: str,
        start_value,
        end_value,
        *,
        done: bool,
        forming: bool | None = None,
    ) -> None:
        start_date = BASE + timedelta(minutes=index * 5)
        end_date = start_date + timedelta(minutes=5)
        self.type = direction
        self.start = SimpleNamespace(
            val=start_value,
            k=SimpleNamespace(k_index=index * 10, date=start_date),
        )
        self.end = SimpleNamespace(
            val=end_value,
            k=SimpleNamespace(k_index=(index + 1) * 10, date=end_date),
        )
        self.locked_at = end_date + timedelta(minutes=5) if done else None
        self.zs_low = min(start_value, end_value) - 1000
        self.zs_high = max(start_value, end_value) + 1000
        self._done = done
        self.forming = (not done) if forming is None else forming
        self.formed_at = (
            None
            if self.forming
            else end_date + timedelta(minutes=2)
        )

    def is_done(self) -> bool:
        return self._done


@pytest.fixture
def fake_lines():
    return [
        FakeLine(0, "down", 120, 90, done=True),
        FakeLine(1, "up", 90, 125, done=True),
        FakeLine(2, "down", 125, 100, done=False),
    ]


@pytest.fixture
def fake_done_line():
    return FakeLine(0, "up", 100, 120, done=True)


def test_adapter_keeps_unfinished_lines_out_of_the_locked_prefix(fake_lines):
    registry = UnitLockRegistry("test-raw")
    values = adapt_lines(
        fake_lines,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_quantum=Decimal("0.01"),
        as_of=BASE + timedelta(hours=2),
        registry=registry,
    )

    assert [item.locked for item in values] == [True, True, False]
    assert [item.forming for item in values] == [False, False, True]
    assert values[-1].confirmed_at is None
    assert values[-1].available_at == BASE + timedelta(hours=2)


def test_adapter_distinguishes_formed_unlocked_tail_from_forming_tail() -> None:
    lines = [
        FakeLine(0, "down", 120, 90, done=True),
        FakeLine(1, "up", 90, 125, done=False, forming=False),
        FakeLine(2, "down", 125, 100, done=False, forming=True),
    ]

    values = adapt_lines(
        lines,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_quantum=Decimal("0.01"),
        as_of=BASE + timedelta(hours=2),
        registry=UnitLockRegistry("test-raw"),
    )

    assert [(item.locked, item.forming) for item in values] == [
        (True, False),
        (False, False),
        (False, True),
    ]
    assert values[1].formed_at == lines[1].formed_at
    assert values[1].available_at == lines[1].formed_at
    assert values[2].formed_at is None


def test_first_lock_time_does_not_move_on_later_updates(fake_done_line):
    registry = UnitLockRegistry("test-raw")
    first = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        BASE + timedelta(hours=2),
        registry,
    )[0]
    later = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        BASE + timedelta(hours=3),
        registry,
    )[0]

    assert later.confirmed_at == first.confirmed_at
    assert first.confirmed_at == fake_done_line.locked_at
    assert first.confirmed_at != BASE + timedelta(hours=2)


def test_adapter_rejects_done_line_without_causal_lock_time(fake_done_line):
    fake_done_line.locked_at = None

    with pytest.raises(ValueError, match="done line requires causal locked_at"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            BASE + timedelta(hours=2),
            UnitLockRegistry("test-raw"),
        )


def test_adapter_rejects_unfinished_line_with_a_lock_time(fake_done_line):
    fake_done_line._done = False

    with pytest.raises(ValueError, match="unfinished line cannot have locked_at"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            BASE + timedelta(hours=2),
            UnitLockRegistry("test-raw"),
        )


def test_adapter_rejects_lock_evidence_from_after_as_of(fake_done_line):
    with pytest.raises(ValueError, match="locked_at cannot exceed as_of"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            fake_done_line.locked_at - timedelta(seconds=1),
            UnitLockRegistry("test-raw"),
        )


def test_formal_segment_interval_uses_endpoints_not_auxiliary_zs_fields(
    fake_done_line,
):
    fake_done_line.start.val = 100
    fake_done_line.end.val = 120
    fake_done_line.zs_low = 80
    fake_done_line.zs_high = 140
    value = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("1"),
        BASE + timedelta(hours=2),
        UnitLockRegistry("test-raw"),
    )[0]

    assert (value.low_tick, value.high_tick) == (100, 120)


def test_adapter_identity_uses_normalized_ticks(fake_done_line):
    registry = UnitLockRegistry("test-raw")
    first = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        BASE + timedelta(hours=2),
        registry,
    )[0]
    fake_done_line.start.val = Decimal("100.000")
    fake_done_line.end.val = Decimal("120.00")
    second = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.010"),
        BASE + timedelta(hours=2),
        registry,
    )[0]

    assert second.unit_id == first.unit_id
    assert (second.start_tick, second.end_tick) == (10000, 12000)


def test_registry_rejects_a_changed_confirmation_for_same_unit(fake_done_line):
    registry = UnitLockRegistry("test-raw")
    adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        BASE + timedelta(hours=2),
        registry,
    )
    fake_done_line.locked_at += timedelta(minutes=1)

    with pytest.raises(ValueError, match="locked unit confirmation time changed"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            BASE + timedelta(hours=2),
            registry,
        )


def test_adapter_rejects_non_positive_price_quantum(fake_done_line):
    with pytest.raises(ValueError, match="price_quantum must be positive"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0"),
            BASE + timedelta(hours=2),
            UnitLockRegistry("test-raw"),
        )


def test_adapter_rejects_line_endpoint_after_observation_time(fake_done_line):
    fake_done_line._done = False
    fake_done_line.locked_at = None

    with pytest.raises(ValueError, match="line endpoint cannot exceed as_of"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            fake_done_line.end.k.date - timedelta(seconds=1),
            UnitLockRegistry("test-raw"),
        )


def test_invalid_early_lock_does_not_poison_registry(fake_done_line):
    registry = UnitLockRegistry("test-raw")
    fake_done_line.locked_at = fake_done_line.end.k.date - timedelta(seconds=1)

    with pytest.raises(ValueError, match="locked_at must not precede line end"):
        adapt_lines(
            [fake_done_line],
            0,
            SourceKind.SEGMENT,
            Decimal("0.01"),
            BASE + timedelta(hours=2),
            registry,
        )

    fake_done_line.locked_at = fake_done_line.end.k.date + timedelta(minutes=5)
    corrected = adapt_lines(
        [fake_done_line],
        0,
        SourceKind.SEGMENT,
        Decimal("0.01"),
        BASE + timedelta(hours=2),
        registry,
    )[0]
    assert corrected.confirmed_at == fake_done_line.locked_at


def test_line_adapter_rejects_recursive_trend_source_kind(fake_done_line):
    with pytest.raises(ValueError, match="line adapter does not build trend-type units"):
        adapt_lines(
            [fake_done_line],
            1,
            SourceKind.TREND_TYPE,
            Decimal("0.01"),
            BASE + timedelta(hours=2),
            UnitLockRegistry("test-raw"),
        )
