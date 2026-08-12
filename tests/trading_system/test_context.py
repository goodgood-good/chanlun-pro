from datetime import timedelta

from chanlun.decision_support.trading_system.context import classify_context
from tests.trading_system.helpers import AS_OF, confirmed_point


def test_neutral_thirty_minute_without_fresh_point_is_not_rejected() -> None:
    context = classify_context(
        frequency="30m",
        current_direction="neutral",
        points=(),
        as_of=AS_OF,
    )

    assert context.disposition == "neutral"
    assert context.hard_block is False
    assert context.reason_codes == ("no_active_directional_point",)


def test_confirmed_higher_level_sell_in_down_structure_is_hostile() -> None:
    context = classify_context(
        frequency="30m",
        current_direction="down",
        points=(confirmed_point("1sell", tower="formal", level=1),),
        as_of=AS_OF,
    )

    assert context.disposition == "hostile"
    assert context.hard_block is True
    assert context.dominant_point_type == "1sell"


def test_point_is_hidden_until_its_available_at() -> None:
    future = confirmed_point(
        "1buy",
        available_minutes_after=int(
            (AS_OF + timedelta(minutes=1) - AS_OF.replace(hour=10)).total_seconds()
            / 60
        ),
    )

    context = classify_context(
        frequency="30m",
        current_direction="up",
        points=(future,),
        as_of=AS_OF,
    )

    assert context.dominant_point_id is None
    assert context.reason_codes == ("no_active_directional_point",)


def test_expired_point_cannot_control_current_context() -> None:
    expired = confirmed_point("1buy", frequency="30m", minutes_after=-(31 * 24 * 60))

    context = classify_context(
        frequency="30m",
        current_direction="neutral",
        points=(expired,),
        as_of=AS_OF,
    )

    assert context.disposition == "neutral"
    assert context.dominant_point_id is None
    assert context.reason_codes == ("directional_points_expired",)


def test_old_anchor_recently_confirmed_cannot_reenter_current_context() -> None:
    delayed = confirmed_point(
        "1buy",
        frequency="30m",
        minutes_after=-(31 * 24 * 60),
        available_minutes_after=31 * 24 * 60,
    )

    context = classify_context(
        frequency="30m",
        current_direction="up",
        points=(delayed,),
        as_of=AS_OF,
    )

    assert delayed.available_at <= AS_OF
    assert context.disposition == "neutral"
    assert context.dominant_point_id is None
    assert context.reason_codes == ("directional_points_expired",)


def test_latest_causal_point_wins_before_recursive_level() -> None:
    old_higher_sell = confirmed_point(
        "1sell",
        frequency="30m",
        level=2,
        minutes_after=-60,
    )
    latest_buy = confirmed_point("1buy", frequency="30m", level=0)

    context = classify_context(
        frequency="30m",
        current_direction="neutral",
        points=(old_higher_sell, latest_buy),
        as_of=AS_OF,
    )

    assert context.disposition == "supportive"
    assert context.dominant_point_id == latest_buy.point_id
