from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
    _TechnicalSignalEvaluator,
)
from chanlun.decision_support.trading_system.lifecycle import (
    current_five_minute_setup_points,
)
from tests.trading_system.helpers import (
    AS_OF,
    confirmed_point,
    deterministic_bundle,
    eligible_sector,
    hostile_sector,
    provisional_point,
)


def symbol_bundle(
    *,
    five_points=(),
    one_points=(),
    opposite_points=(),
    sector=None,
    thirty_direction="neutral",
    thirty_points=(),
    latest_price=None,
) -> SymbolStructureBundle:
    return SymbolStructureBundle(
        code="SZ.000001",
        as_of=AS_OF,
        sector=eligible_sector() if sector is None else sector,
        thirty_direction=thirty_direction,
        thirty_points=tuple(thirty_points),
        five_points=tuple(five_points),
        one_points=tuple(one_points),
        opposite_points=tuple(opposite_points),
        latest_price=latest_price,
    )


def test_engine_keeps_three_buy_lanes_and_triggers_independent() -> None:
    bundle = symbol_bundle(
        five_points=(
            confirmed_point("1buy"),
            confirmed_point("2buy"),
            confirmed_point("3buy", center_ordinal=1),
        ),
        one_points=(confirmed_point("1buy", frequency="1m", minutes_after=1),),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(bundle)

    assert {item.setup.point.point_type for item in evaluated} == {
        "1buy",
        "2buy",
        "3buy",
    }
    assert len({item.lifecycle.signal_id for item in evaluated}) == 3


def test_neutral_sector_is_retained() -> None:
    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        deterministic_bundle()
    )

    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is True


def test_hostile_sector_downgrades_but_does_not_erase_confirmed_entry() -> None:
    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        replace(deterministic_bundle(), sector=hostile_sector())
    )

    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is True
    assert "sector_hostile" not in evaluated[0].entry.reason_codes
    assert "sector_hostile" in evaluated[0].advisory_reason_codes


def test_recursive_5m_context_is_not_a_trade_setup() -> None:
    with pytest.raises(ValueError, match="物理 5m/L0"):
        symbol_bundle(
            five_points=(confirmed_point("2buy", tower="formal", level=1),),
            one_points=(
                confirmed_point("1buy", frequency="1m", minutes_after=1),
            ),
            opposite_points=(confirmed_point("1sell", tower="formal", level=0),),
        )


def test_confirmed_five_minute_point_survives_while_execution_waits_for_one_minute() -> None:
    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(confirmed_point("2buy"),))
    )

    assert evaluated[0].lifecycle.stage == "triggered"
    assert evaluated[0].entry is not None
    assert evaluated[0].technical_entry_allowed is True
    assert evaluated[0].entry.allowed is False
    assert evaluated[0].entry.reason_codes == ("one_minute_not_confirmed",)
    assert evaluated[0].trigger is None


def test_engine_invalidates_setup_when_latest_price_crosses_structure_stop() -> None:
    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(
            five_points=(confirmed_point("2buy", anchor=10.0, stop=9.8),),
            latest_price=9.79,
        )
    )

    assert evaluated[0].lifecycle.stage == "invalidated"
    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is False


def test_engine_keeps_provisional_five_minute_points_as_approaching() -> None:
    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(provisional_point("2buy"),))
    )

    assert len(evaluated) == 1
    assert evaluated[0].setup.point.point_type == "2buy"
    assert evaluated[0].lifecycle.stage == "approaching"
    assert evaluated[0].entry is not None
    assert evaluated[0].entry.allowed is False


def test_engine_exposes_preconfirmation_divergence_without_triggering_setup() -> None:
    candidate = provisional_point("3buy")
    candidate = replace(
        candidate,
        terminal_segment=TerminalSegmentReference(
            role="latest_unfinished",
            structural_level=0,
            unit_id="segment:5m:forming",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="forming",
            market_start=candidate.anchor_at - timedelta(minutes=30),
            market_end=candidate.anchor_at,
            available_at=candidate.available_at,
        ),
    )
    divergence = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=-5,
    )
    divergence = replace(
        divergence,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:1m:divergence",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=divergence.anchor_at - timedelta(minutes=1),
            market_end=divergence.anchor_at,
            available_at=divergence.available_at,
        ),
    )

    [evaluated] = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(
            five_points=(candidate,),
            one_points=(divergence,),
        )
    )

    assert evaluated.lifecycle.stage == "approaching"
    assert evaluated.lifecycle.actionable is False
    assert evaluated.lifecycle.trigger_point_id is None
    assert evaluated.trigger is None
    assert evaluated.preconfirmation_divergences == (divergence,)
    assert evaluated.entry is not None
    assert evaluated.entry.allowed is False


def test_engine_exposes_completed_preview_as_formed_not_approaching() -> None:
    point = replace(
        provisional_point("3buy"),
        evidence_codes=(
            "physical_timeframe_recursive_base_level",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(point,))
    )

    assert len(evaluated) == 1
    assert evaluated[0].lifecycle.stage == "formed"
    assert evaluated[0].lifecycle.actionable is False


def test_newer_later_center_candidate_coexists_with_current_confirmed_setup() -> None:
    confirmed = confirmed_point("3buy", center_ordinal=1)
    provisional = replace(
        provisional_point("3buy"),
        candidate_id="candidate:SZ.000001:3buy:newer",
        observed_at=confirmed.available_at + timedelta(minutes=60),
        center_id="center-newer",
        center_ordinal=2,
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(confirmed, provisional))
    )

    assert len(evaluated) == 2
    assert {item.setup.point for item in evaluated} == {confirmed, provisional}
    later = next(item for item in evaluated if item.setup.point == provisional)
    assert later.lifecycle.stage == "approaching"
    assert later.entry is not None
    assert later.entry.allowed is False
    assert provisional.center_ordinal == 2


def test_newer_formed_three_buy_supersedes_older_confirmed_three_sell() -> None:
    old_sell = confirmed_point(
        "3sell",
        minutes_after=-120,
        center_id="old-sell-center",
        stop=10.2,
        center_zd=10.1,
        center_zg=10.3,
    )
    formed_buy = replace(
        provisional_point("3buy"),
        candidate_id="candidate:SZ.000001:3buy:formed-frontier",
        observed_at=AS_OF,
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(old_sell, formed_buy))
    )

    assert [item.setup.point for item in evaluated] == [formed_buy]
    assert evaluated[0].lifecycle.stage == "formed"


def test_latest_completed_and_unfinished_segment_states_are_both_retained() -> None:
    completed_base = provisional_point("2sell")
    completed = replace(
        completed_base,
        candidate_id="candidate:SZ.000001:2sell:latest-completed",
        observed_at=AS_OF,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:latest-completed",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="formed",
            market_start=completed_base.anchor_at - timedelta(minutes=60),
            market_end=completed_base.anchor_at,
            available_at=AS_OF,
        ),
    )
    unfinished_anchor = completed_base.anchor_at + timedelta(minutes=60)
    unfinished = replace(
        provisional_point("3buy"),
        candidate_id="candidate:SZ.000001:3buy:latest-unfinished",
        anchor_at=unfinished_anchor,
        observed_at=AS_OF,
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
        terminal_segment=TerminalSegmentReference(
            role="latest_unfinished",
            structural_level=0,
            unit_id="segment:latest-unfinished",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="forming",
            market_start=completed_base.anchor_at,
            market_end=unfinished_anchor,
            available_at=AS_OF,
        ),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(completed, unfinished))
    )

    assert {item.setup.point for item in evaluated} == {completed, unfinished}
    assert {item.lifecycle.stage for item in evaluated} == {"formed", "approaching"}


def test_newer_forming_opposite_candidate_does_not_hide_confirmed_setup() -> None:
    confirmed = confirmed_point(
        "3sell",
        minutes_after=-120,
        center_id="confirmed-sell-center",
        stop=10.2,
        center_zd=10.1,
        center_zg=10.3,
    )
    forming = replace(
        provisional_point("3buy"),
        candidate_id="candidate:SZ.000001:3buy:forming-frontier",
        observed_at=AS_OF,
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(confirmed, forming))
    )

    assert {item.setup.point for item in evaluated} == {confirmed, forming}


def test_structural_anchor_wins_over_later_opposite_lock_time() -> None:
    newer_buy = confirmed_point(
        "3buy",
        center_id="newer-buy-center",
    )
    delayed_older_sell = confirmed_point(
        "3sell",
        minutes_after=-60,
        available_minutes_after=120,
        center_id="delayed-older-sell-center",
        stop=10.2,
        center_zd=10.1,
        center_zg=10.3,
    )
    assert delayed_older_sell.available_at > newer_buy.available_at
    assert delayed_older_sell.anchor_at < newer_buy.anchor_at

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(newer_buy, delayed_older_sell))
    )

    assert [item.setup.point for item in evaluated] == [newer_buy]


def test_live_formed_candidate_uses_current_frontier_not_old_geometry_age() -> None:
    formed = replace(
        provisional_point("3buy"),
        candidate_id="candidate:SZ.000001:3buy:long-lock-wait",
        anchor_at=AS_OF - timedelta(days=5),
        observed_at=AS_OF,
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(formed,))
    )

    assert [item.setup.point for item in evaluated] == [formed]
    assert evaluated[0].lifecycle.stage == "formed"


def test_repeated_evaluation_is_deterministic() -> None:
    bundle = deterministic_bundle()
    engine = _TechnicalSignalEvaluator()

    first = engine.evaluate_symbol(bundle)
    second = engine.evaluate_symbol(replace(bundle))

    assert first == second


def test_later_nested_witness_keeps_first_execution_boundary() -> None:
    engine = _TechnicalSignalEvaluator()
    first_bundle = deterministic_bundle()
    [first] = engine.evaluate_symbol(first_bundle)
    first_witness = first_bundle.one_points[0]
    old_boundary = first_bundle.entry_execution_boundaries[0]
    later_witness = confirmed_point(
        "1buy",
        frequency="1m",
        minutes_after=293,
        available_minutes_after=5,
    )
    later_witness = replace(
        later_witness,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id=f"segment:1m:{later_witness.point_id}",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=later_witness.anchor_at - timedelta(minutes=1),
            market_end=later_witness.anchor_at,
            available_at=later_witness.available_at,
        ),
    )
    new_boundary = replace(
        old_boundary,
        point_id=later_witness.point_id,
        confirmation_bar_closed_at=later_witness.available_at,
        entry_valid_until=later_witness.available_at + timedelta(minutes=1),
    )
    second_bundle = replace(
        first_bundle,
        as_of=later_witness.available_at + timedelta(seconds=30),
        one_points=(first_witness, later_witness),
        entry_execution_boundaries=(old_boundary, new_boundary),
        previous_lifecycles=(first.lifecycle,),
        previous_trigger_points=(first_witness,),
    )

    [second] = engine.evaluate_symbol(second_bundle)

    assert first.trigger == first_witness
    assert old_boundary.entry_valid_until < second_bundle.as_of
    assert second.trigger == first_witness
    assert second.lifecycle == first.lifecycle
    assert second.entry_execution_boundary == old_boundary
    assert second.entry is not None
    assert second.entry.allowed is False


def test_shared_witness_boundary_is_not_reused_when_setup_pair_time_ties() -> None:
    base = deterministic_bundle()
    first_setup = base.five_points[0]
    witness = base.one_points[0]
    first_boundary = base.entry_execution_boundaries[0]
    later_setup = confirmed_point(
        "1buy",
        minutes_after=295,
    )
    later_setup = replace(
        later_setup,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id=f"segment:5m:{later_setup.point_id}",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=later_setup.anchor_at - timedelta(minutes=30),
            market_end=later_setup.anchor_at,
            available_at=later_setup.available_at,
        ),
    )
    assert first_boundary.confirmation_bar_closed_at == witness.available_at
    assert later_setup.available_at < witness.available_at
    bundle = replace(
        base,
        as_of=witness.available_at + timedelta(seconds=30),
        five_points=(first_setup, later_setup),
        entry_execution_boundaries=(first_boundary,),
    )

    evaluated = {
        item.setup.point.point_type: item
        for item in _TechnicalSignalEvaluator().evaluate_symbol(bundle)
    }

    assert evaluated["2buy"].entry_execution_boundary == first_boundary
    assert evaluated["1buy"].trigger == witness
    assert evaluated["1buy"].entry_execution_boundary is None


def test_engine_keeps_only_recent_terminal_point_per_independent_lane() -> None:
    stale = confirmed_point(
        "3buy",
        center_ordinal=1,
        minutes_after=-(8 * 24 * 60),
    )
    older_one_buy = confirmed_point("1buy")
    latest_one_buy = confirmed_point("1buy", minutes_after=5)
    independent_two_sell = confirmed_point("2sell", minutes_after=3)

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(
            five_points=(
                stale,
                older_one_buy,
                latest_one_buy,
                independent_two_sell,
            )
        )
    )

    assert [item.setup.point.point_id for item in evaluated] == [
        independent_two_sell.point_id,
        latest_one_buy.point_id,
    ]


def test_actual_latest_completed_segment_stays_display_current_but_not_executable() -> None:
    point = confirmed_point(
        "3buy",
        center_ordinal=1,
        minutes_after=-(8 * 24 * 60),
    )
    point = replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:slow-latest-completed",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=point.anchor_at - timedelta(hours=2),
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(five_points=(point,))
    )

    assert current_five_minute_setup_points((point,), as_of=AS_OF) == (point,)
    assert evaluated == ()


def test_late_lock_does_not_resurrect_an_expired_structure_anchor() -> None:
    delayed = confirmed_point(
        "3sell",
        anchor=18.89,
        stop=24.76,
        center_zd=24.76,
        center_zg=27.04,
        minutes_after=-(27 * 24 * 60),
        available_minutes_after=27 * 24 * 60,
    )
    delayed = replace(
        delayed,
        confirmed_at=delayed.available_at,
    )

    evaluated = _TechnicalSignalEvaluator().evaluate_symbol(
        symbol_bundle(
            five_points=(delayed,),
            latest_price=23.64,
        )
    )

    assert delayed.available_at <= AS_OF
    assert delayed.structure_anchor_price < 23.64 < delayed.structure_invalidation_price
    assert evaluated == ()


@pytest.mark.parametrize(
    ("field", "point"),
    (
        ("daily_points", confirmed_point("1buy", frequency="d", code="SZ.000002")),
        (
            "thirty_points",
            confirmed_point("1buy", frequency="30m", code="SZ.000002"),
        ),
        ("five_points", confirmed_point("1buy", code="SZ.000002")),
        ("five_points", provisional_point("2buy", code="SZ.000002")),
        (
            "one_points",
            confirmed_point("1buy", frequency="1m", code="SZ.000002"),
        ),
        ("opposite_points", confirmed_point("1sell", code="SZ.000002")),
    ),
)
def test_structure_bundle_rejects_cross_symbol_points(field, point) -> None:
    with pytest.raises(ValueError, match="标的与结构包标的不一致"):
        SymbolStructureBundle(
            code="SZ.000001",
            as_of=AS_OF,
            sector=eligible_sector(),
            thirty_direction="neutral",
            thirty_points=(point,) if field == "thirty_points" else (),
            five_points=(point,) if field == "five_points" else (),
            one_points=(point,) if field == "one_points" else (),
            opposite_points=(point,) if field == "opposite_points" else (),
            daily_points=(point,) if field == "daily_points" else (),
        )


def test_structure_bundle_rejects_wrong_frequency_even_without_recursive_flag() -> None:
    with pytest.raises(ValueError, match="各周期只能接收本周期"):
        symbol_bundle(
            five_points=(confirmed_point("1buy", frequency="30m"),),
        )


def test_structure_bundle_rejects_future_and_unconfirmed_formal_points() -> None:
    future = confirmed_point("1buy", minutes_after=24 * 60)
    with pytest.raises(ValueError, match="不能晚于结构包决策时点"):
        symbol_bundle(five_points=(future,))

    with pytest.raises(ValueError, match="正式买卖点必须已经确认"):
        replace(
            confirmed_point("1buy"),
            status="invalidated",
            confirmed_at=None,
        )
