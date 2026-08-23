from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.models import (
    StructuralPoint,
    build_point_id,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
    StrictRealtimeMonitorEvent,
    collect_strict_monitor_events,
)
import chanlun.decision_support.trading_system.strict_realtime_monitor as monitor_module
from chanlun.decision_support.trading_system.runtime_config import (
    strict_snapshot_price_metadata,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)


CN = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 8, 5, 10, 0, tzinfo=CN)


def _point(
    point_type: str,
    *,
    center_id: str = "strict-center",
    recursive_level: int = 0,
    frequency: str = "5m",
    anchor_at: datetime = AT,
    confirmed_at: datetime = AT,
    available_at: datetime = AT,
) -> StructuralPoint:
    side = "buy" if point_type.endswith("buy") else "sell"
    point_id = build_point_id(
        code="TSLA.US",
        price_basis_revision="provider-basis",
        point_type=point_type,
        source_frequency=frequency,
        tower="formal",
        recursive_level=recursive_level,
        anchor_at=anchor_at,
        center_id=center_id,
        parent_point_id=None,
        variant="standard",
        structure_anchor_price=100.0,
        structure_invalidation_price=99.0 if side == "buy" else 101.0,
        center_zd=98.0 if side == "buy" else 101.0,
        center_zg=99.0 if side == "buy" else 102.0,
        center_ordinal=1 if point_type.startswith("3") else None,
        divergence_kind="trend" if point_type.startswith("1") else None,
    )
    return StructuralPoint(
        point_id=point_id,
        code="TSLA.US",
        point_type=point_type,
        side=side,
        status="confirmed",
        variant="standard",
        source_frequency=frequency,
        price_basis_revision="provider-basis",
        tower="formal",
        recursive_level=recursive_level,
        anchor_at=anchor_at,
        confirmed_at=confirmed_at,
        available_at=available_at,
        structure_anchor_price=100.0,
        structure_invalidation_price=99.0 if side == "buy" else 101.0,
        center_id=center_id,
        center_zd=98.0 if side == "buy" else 101.0,
        center_zg=99.0 if side == "buy" else 102.0,
        center_ordinal=1 if point_type.startswith("3") else None,
        divergence_kind="trend" if point_type.startswith("1") else None,
        parent_point_id=None,
        evidence_codes=("strict",),
    )


def test_monitor_event_rejects_old_point_aliases_and_side_mismatches() -> None:
    common = {
        "code": "TSLA.US",
        "name": "特斯拉",
        "kind": "strict_buy_point",
        "signal_time": AT.isoformat(),
        "price": 100.0,
        "big_dir": "up",
        "reason": "strict_confirmed_1buy",
    }

    with pytest.raises(ValueError, match="统一六类买卖点"):
        StrictRealtimeMonitorEvent(side="buy", bs_type="1b", **common)
    with pytest.raises(ValueError, match="类型与方向不一致"):
        StrictRealtimeMonitorEvent(side="sell", bs_type="1buy", **common)
    with pytest.raises(ValueError, match="统一严格通道"):
        StrictRealtimeMonitorEvent(
            side="buy",
            bs_type="1buy",
            **{**common, "kind": "small_buy"},
        )
    with pytest.raises(ValueError, match="物理 5m/L0"):
        StrictRealtimeMonitorEvent(
            side="buy",
            bs_type="1buy",
            recursive_level=1,
            evidence_id="point:tsla:5m:l1:1buy",
            anchor_time=AT.isoformat(),
            **common,
        )
    with pytest.raises(ValueError, match="1 分钟段差证据不完整"):
        StrictRealtimeMonitorEvent(
            side="buy",
            bs_type="1buy",
            evidence_id="point:tsla:5m:l0:1buy",
            anchor_time=AT.isoformat(),
            segment_difference_point_type="1buy",
            segment_difference_evidence_id="point:tsla:1m:l1:1buy",
            segment_difference_recursive_level=1,
            segment_difference_anchor_time=AT.isoformat(),
            segment_difference_confirmed_time=AT.isoformat(),
            segment_difference_available_time=AT.isoformat(),
            **common,
        )


class _StrictState:
    op_level = "5m"
    mid_level = "1m"
    big_level = "30m"
    last_big = pd.Timestamp(AT)
    last_px = 123.45
    consecutive_refresh_failures = 0
    warmup_ready = True

    def __init__(self, points, *, big="up", mid="up") -> None:
        self._points = list(points)
        self._big = big
        self._mid = mid

    def refresh(self):
        return list(self._points)

    def big_dir(self):
        return self._big

    def mid_dir(self):
        return self._mid

def test_strict_collector_carries_point_identity_and_exact_point_type() -> None:
    point = _point("3buy")
    state = _StrictState((point,))

    [event] = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.bs_type == "3buy"
    assert event.side == "buy"
    assert event.kind == "strict_buy_point"
    assert event.evidence_id == point.point_id
    assert event.recursive_level == 0
    assert event.anchor_time == AT.isoformat(timespec="seconds")
    assert event.confirmed_time == AT.isoformat(timespec="seconds")
    assert event.identity == (
        "strict_buy_point|TSLA.US|5m|3buy|L0|"
        f"{AT.isoformat(timespec='seconds')}|{AT.isoformat(timespec='seconds')}|"
        f"{point.point_id}"
    )
    assert event.delivery_identity == (
        "strict_buy_point|TSLA.US|5m|3buy|L0|"
        f"{AT.isoformat(timespec='seconds')}|{AT.isoformat(timespec='seconds')}"
    )
    assert event.signal_time == AT.isoformat(timespec="seconds")
    assert event.price == 123.45
    assert event.structure_anchor_price == 100.0


def test_strict_collector_attaches_optional_one_minute_segment_difference() -> None:
    point = _point("3buy")
    segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=2),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT,
    )
    state = _StrictState((point,))
    state.segment_difference_for_trade_point = lambda _point: segment

    [event] = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.signal_role == "TRADE_SIGNAL_5M"
    assert event.segment_difference_point_type == "1buy"
    assert event.segment_difference_evidence_id == segment.point_id
    assert event.segment_difference_recursive_level == 0
    assert event.segment_difference_available_time == AT.isoformat(timespec="seconds")
    assert event.segment_difference_divergence_kind == "trend"


def test_stage_stable_new_one_minute_segment_emits_a_distinct_enrichment() -> None:
    point = _point(
        "3buy",
        anchor_at=AT - timedelta(hours=1),
        confirmed_at=AT - timedelta(minutes=20),
        available_at=AT - timedelta(minutes=20),
    )
    segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=2),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT,
    )
    state = _StrictState(())
    state.new_segment_difference_updates = lambda: ((point, segment),)

    [event] = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.signal_role == "SEGMENT_DIFFERENCE_1M"
    assert event.kind == "strict_segment_difference_update"
    assert event.bs_type == "3buy"
    assert event.signal_time == segment.available_at.isoformat(timespec="seconds")
    assert event.setup_available_time == point.available_at.isoformat(
        timespec="seconds"
    )
    assert event.segment_difference_point_type == "1buy"
    assert event.segment_difference_divergence_kind == "trend"
    assert event.delivery_identity == (
        "strict_segment_difference_update|TSLA.US|5m|3buy|L0|"
        f"{point.anchor_at.isoformat(timespec='seconds')}|"
        f"{point.available_at.isoformat(timespec='seconds')}|1m|1buy|L0|"
        f"{segment.anchor_at.isoformat(timespec='seconds')}|"
        f"{segment.available_at.isoformat(timespec='seconds')}"
    )
    assert event.identity.endswith(f"|{point.point_id}|{segment.point_id}")


def test_same_round_trade_signal_carries_segment_without_duplicate_enrichment() -> None:
    point = _point("3buy")
    segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=2),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT,
    )
    state = _StrictState((point,))
    state.segment_difference_for_trade_point = lambda _point: segment
    state.new_segment_difference_updates = lambda: ((point, segment),)

    events = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert len(events) == 1
    assert events[0].signal_role == "TRADE_SIGNAL_5M"
    assert events[0].segment_difference_evidence_id == segment.point_id


def test_physical_monitor_reports_each_segment_occurrence_only_once(
    monkeypatch,
) -> None:
    point = _point(
        "3buy",
        anchor_at=AT - timedelta(hours=1),
        confirmed_at=AT - timedelta(minutes=20),
        available_at=AT - timedelta(minutes=20),
    )
    segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=2),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT,
    )
    segment_points: list[StructuralPoint] = []
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    state.last_px = 100.0
    monkeypatch.setattr(
        state,
        "_process_level",
        lambda *_args, **_kwargs: SimpleNamespace(
            structure=SimpleNamespace(levels=()),
            approaching_points=(),
        ),
    )
    monkeypatch.setattr(
        state,
        "_process_optional_segment_level",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(state, "_refresh_visible_price", lambda: None)
    monkeypatch.setattr(monitor_module, "_strict_direction", lambda _value: "up")
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (point,) if source_frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_one_minute_segment_difference_points",
        lambda *_args, **_kwargs: tuple(segment_points),
    )
    monkeypatch.setattr(
        monitor_module,
        "current_five_minute_setup_points",
        lambda *_args, **_kwargs: (point,),
    )

    assert state.refresh() == [point]
    assert state.new_segment_difference_updates() == ()
    segment_points.append(segment)
    assert state.refresh() == []
    assert state.new_segment_difference_updates() == ((point, segment),)
    assert state.refresh() == []
    assert state.new_segment_difference_updates() == ()


def test_physical_monitor_accepts_locator_older_than_parent_setup(
    monkeypatch,
) -> None:
    point = _point(
        "3buy",
        anchor_at=AT - timedelta(minutes=5),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT - timedelta(minutes=1),
    )
    segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=22),
        confirmed_at=AT - timedelta(minutes=21),
        available_at=AT - timedelta(minutes=20),
    )
    segment_points: list[StructuralPoint] = []
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    monkeypatch.setattr(
        state,
        "_process_level",
        lambda *_args, **_kwargs: SimpleNamespace(
            structure=SimpleNamespace(levels=()),
            approaching_points=(),
        ),
    )
    monkeypatch.setattr(
        state,
        "_process_optional_segment_level",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(state, "_refresh_visible_price", lambda: None)
    monkeypatch.setattr(monitor_module, "_strict_direction", lambda _value: "up")
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (point,) if source_frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_one_minute_segment_difference_points",
        lambda *_args, **_kwargs: tuple(segment_points),
    )
    monkeypatch.setattr(
        monitor_module,
        "current_five_minute_setup_points",
        lambda *_args, **_kwargs: (point,),
    )
    monkeypatch.setattr(
        monitor_module,
        "match_one_minute_segment_difference_for_point",
        lambda _point, candidates, **_kwargs: segment if candidates else None,
    )

    state.refresh()
    segment_points.append(segment)
    state.refresh()

    assert state.new_segment_difference_updates() == ((point, segment),)


def test_later_locator_on_same_parent_is_a_new_notification_occurrence(
    monkeypatch,
) -> None:
    point = _point(
        "3buy",
        anchor_at=AT - timedelta(hours=1),
        confirmed_at=AT - timedelta(minutes=20),
        available_at=AT - timedelta(minutes=20),
    )
    stale_segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=22),
        confirmed_at=AT - timedelta(minutes=21),
        available_at=AT - timedelta(minutes=20),
    )
    fresh_segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=2),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT,
    )
    segment_points: list[StructuralPoint] = [stale_segment]
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    monkeypatch.setattr(
        state,
        "_process_level",
        lambda *_args, **_kwargs: SimpleNamespace(
            structure=SimpleNamespace(levels=()),
            approaching_points=(),
        ),
    )
    monkeypatch.setattr(
        state,
        "_process_optional_segment_level",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(state, "_refresh_visible_price", lambda: None)
    monkeypatch.setattr(monitor_module, "_strict_direction", lambda _value: "up")
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (point,) if source_frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_one_minute_segment_difference_points",
        lambda *_args, **_kwargs: tuple(segment_points),
    )
    monkeypatch.setattr(
        monitor_module,
        "current_five_minute_setup_points",
        lambda *_args, **_kwargs: (point,),
    )
    monkeypatch.setattr(
        monitor_module,
        "match_one_minute_segment_difference_for_point",
        lambda _point, candidates, **_kwargs: max(
            candidates,
            key=lambda candidate: candidate.available_at,
            default=None,
        ),
    )

    state.refresh()
    assert state.new_segment_difference_updates() == ((point, stale_segment),)

    segment_points.append(fresh_segment)
    state.refresh()

    assert state.new_segment_difference_updates() == ((point, fresh_segment),)


def test_physical_monitor_recovers_a_confirmed_segment_outside_current_tail(
    monkeypatch,
) -> None:
    point = _point(
        "3buy",
        anchor_at=AT - timedelta(minutes=5),
        confirmed_at=AT - timedelta(minutes=1),
        available_at=AT - timedelta(minutes=1),
    )
    historical_segment = _point(
        "1buy",
        frequency="1m",
        anchor_at=AT - timedelta(minutes=3),
        confirmed_at=AT - timedelta(minutes=2),
        # The structural anchor may sit outside the current tail, but an
        # execution locator must become observable after the formal 5m setup.
        available_at=AT,
    )
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    monkeypatch.setattr(
        state,
        "_process_level",
        lambda *_args, **_kwargs: SimpleNamespace(
            structure=SimpleNamespace(levels=()),
            approaching_points=(),
        ),
    )
    monkeypatch.setattr(
        state,
        "_process_optional_segment_level",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(state, "_refresh_visible_price", lambda: None)
    monkeypatch.setattr(monitor_module, "_strict_direction", lambda _value: "up")
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (point,) if source_frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_one_minute_segment_difference_points",
        lambda *_args, **_kwargs: (historical_segment,),
    )
    monkeypatch.setattr(
        monitor_module,
        "current_five_minute_setup_points",
        lambda *_args, **_kwargs: (point,),
    )

    state.refresh()

    assert state.segment_difference_for_trade_point(point) == historical_segment
    assert state.new_segment_difference_updates() == ((point, historical_segment),)


def test_optional_one_minute_outage_does_not_block_five_minute_signal(
    monkeypatch,
) -> None:
    point = _point("3buy")
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    state._runtime_by_frequency["5m"] = SimpleNamespace(
        cd=SimpleNamespace(
            get_src_klines=lambda: [SimpleNamespace(c=100.25)],
        ),
    )

    def process(frequency, _last_attr, _observed_at):
        if frequency == "1m":
            raise monitor_module._WarmupIncomplete("1m segment warming")
        return SimpleNamespace(
            structure=SimpleNamespace(levels=()),
            approaching_points=(),
        )

    monkeypatch.setattr(state, "_process_level", process)
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (point,) if source_frequency == "5m" else ()
        ),
    )

    [event] = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.bs_type == "3buy"
    assert event.price == 100.25
    assert event.price_source == "latest_completed_5m_close"
    assert event.segment_difference_point_type == ""
    assert state.warmup_ready is True
    assert state.segment_difference_ready is False
    assert state.consecutive_refresh_failures == 0


def test_newer_formed_opposite_frontier_suppresses_delayed_lock_notification(
    monkeypatch,
) -> None:
    old_sell = _point(
        "3sell",
        center_id="old-sell-center",
        anchor_at=AT - timedelta(hours=1),
        confirmed_at=AT,
        available_at=AT,
    )
    approaching = [
        SimpleNamespace(
            point_type="3buy",
            structural_level=0,
            anchor_at=AT,
            evidence_codes=(
                "unfinished_segment_participates",
                "provisional_center_completion",
                "core_boundary_held",
            ),
        )
    ]
    op_evidence = SimpleNamespace(
        approaching_points=approaching,
        structure=object(),
    )
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    state._runtime_by_frequency["5m"] = SimpleNamespace(
        cd=SimpleNamespace(
            get_src_klines=lambda: [SimpleNamespace(c=100.25)],
        ),
    )

    def process(frequency, _last_attr, _observed_at):
        if frequency == "1m":
            raise monitor_module._WarmupIncomplete("1m segment warming")
        return op_evidence if frequency == "5m" else None

    monkeypatch.setattr(state, "_process_level", process)
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, source_frequency, **_kwargs: (
            (old_sell,) if source_frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_current_provisional_candidates",
        lambda *_args, **_kwargs: (
            ProvisionalCandidate(
                candidate_id="candidate:TSLA.US:3buy:formed-frontier",
                code="TSLA.US",
                point_type="3buy",
                side="buy",
                status="provisional",
                source_frequency="5m",
                tower="formal",
                recursive_level=0,
                observed_at=AT,
                anchor_at=AT,
                available_at=AT,
                anchor_price=100.0,
                invalidation_price=99.0,
                price_basis_revision="provider-basis",
                variant="standard",
                center_id="new-buy-center",
                center_zd=98.0,
                center_zg=99.0,
                center_ordinal=1,
                divergence_kind=None,
                missing_conditions=("unfinished_segment_lock",),
                evidence_codes=(
                    "unfinished_segment_participates",
                    "provisional_center_completion",
                    "core_boundary_held",
                ),
            ),
        ),
    )

    assert state.refresh() == []
    approaching.clear()
    assert state.refresh() == []


def test_trade_point_keeps_confirmation_distinct_from_availability() -> None:
    confirmed_at = datetime(2026, 8, 5, 10, 1, tzinfo=CN)
    available_at = datetime(2026, 8, 5, 10, 4, tzinfo=CN)
    point = _point(
        "3buy",
        recursive_level=0,
        confirmed_at=confirmed_at,
        available_at=available_at,
    )

    [event] = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((point,))},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.anchor_time == AT.isoformat(timespec="seconds")
    assert event.confirmed_time == confirmed_at.isoformat(timespec="seconds")
    assert event.signal_time == available_at.isoformat(timespec="seconds")
    assert event.recursive_level == 0


def test_recursive_5m_context_does_not_create_a_second_trade_event() -> None:
    lower = _point("3sell", center_id="lower", recursive_level=0)
    higher = _point("3sell", center_id="higher", recursive_level=1)

    events = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((lower, higher))},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert len(events) == 1
    assert events[0].evidence_id == lower.point_id
    assert events[0].recursive_level == 0


def test_chart_occurrence_resolution_uses_full_recursive_evidence_identity(
    monkeypatch,
) -> None:
    lower = _point("3sell", center_id="lower", recursive_level=0)
    higher = _point("3sell", center_id="higher", recursive_level=1)
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    monkeypatch.setattr(state, "evidence", lambda _frequency: object())
    monkeypatch.setattr(
        monitor_module,
        "extract_confirmed_points",
        lambda *_args, **_kwargs: (lower, higher),
    )

    resolved = state.confirmed_point_occurrence(
        "3sell",
        AT.isoformat(),
        frequency="5m",
        evidence_id=higher.point_id,
        recursive_level=1,
        anchor_time=higher.anchor_at.isoformat(),
    )

    assert resolved == higher


def test_chart_occurrence_resolution_accepts_one_semantic_evidence_revision(
    monkeypatch,
) -> None:
    old = _point("3buy", center_id="old-center")
    rebuilt = _point("3buy", center_id="rebuilt-center")
    assert old.point_id != rebuilt.point_id
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    monkeypatch.setattr(state, "evidence", lambda _frequency: object())
    monkeypatch.setattr(
        monitor_module,
        "extract_confirmed_points",
        lambda *_args, **_kwargs: (rebuilt,),
    )

    resolved = state.confirmed_point_occurrence(
        "3buy",
        AT.isoformat(),
        frequency="5m",
        evidence_id=old.point_id,
        recursive_level=0,
        anchor_time=old.anchor_at.isoformat(),
    )

    assert resolved == rebuilt


def test_chart_occurrence_resolution_rejects_ambiguous_evidence_revision(
    monkeypatch,
) -> None:
    old = _point("3buy", center_id="old-center")
    rebuilt_a = _point("3buy", center_id="rebuilt-a")
    rebuilt_b = _point("3buy", center_id="rebuilt-b")
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    monkeypatch.setattr(state, "evidence", lambda _frequency: object())
    monkeypatch.setattr(
        monitor_module,
        "extract_confirmed_points",
        lambda *_args, **_kwargs: (rebuilt_a, rebuilt_b),
    )

    assert state.confirmed_point_occurrence(
        "3buy",
        AT.isoformat(),
        frequency="5m",
        evidence_id=old.point_id,
        recursive_level=0,
        anchor_time=old.anchor_at.isoformat(),
    ) is None


def test_strict_collector_keeps_buy_and_sell_facts_under_high_level_downtrend() -> None:
    buy = _point("3buy")
    sell = _point("3sell")

    events = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((buy, sell), big="down")},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert [(event.side, event.evidence_id) for event in events] == [
        ("buy", buy.point_id),
        ("sell", sell.point_id),
    ]
    assert {event.big_dir for event in events} == {"down"}


def test_thirty_minute_downturn_is_context_warning_not_sell_signal() -> None:
    [event] = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((), big="down")},
        names={"TSLA.US": "Tesla"},
        holdings={"TSLA.US"},
    )

    assert event.side == "risk"
    assert event.kind == "strict_30m_context_warning"
    assert event.signal_role == "CONTEXT_WARNING_30M"
    assert event.bs_type == ""


def test_strict_collector_reviews_first_sell_before_third_buy() -> None:
    third_buy = _point("3buy")
    first_sell = _point("1sell", center_id="sell-center")

    events = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((third_buy, first_sell))},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert [event.bs_type for event in events] == ["1sell", "3buy"]


def test_strict_collector_fails_closed_without_reusing_cached_points() -> None:
    class _FailedState(_StrictState):
        def refresh(self):
            raise ValueError("price basis metadata missing")

    state = _FailedState((_point("3buy"),))

    events = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert events == []
    assert state.consecutive_refresh_failures == 1
    assert state.warmup_ready is False


def _frame(*, metadata: bool) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-08-04 09:30",
                periods=4,
                freq="min",
                tz=CN,
            ),
            "open": [100.0, 100.1, 100.2, 100.3],
            "high": [100.2, 100.3, 100.4, 100.5],
            "low": [99.9, 100.0, 100.1, 100.2],
            "close": [100.1, 100.2, 100.3, 100.4],
            "volume": [1000.0] * 4,
        }
    )
    if metadata:
        attach_price_basis_metadata(
            frame,
            build_provider_price_basis_metadata(
                provider="test-provider",
                market="us",
                code="TSLA.US",
                adjustment="none",
                structure_price_quantum=Decimal("0.01"),
            ),
        )
    return frame


def test_monitor_snapshot_requires_and_preserves_price_basis_metadata() -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )

    with pytest.raises(ValueError, match="structure_price_quantum"):
        state._closed_frame(_frame(metadata=False), "1m")

    closed = state._closed_frame(_frame(metadata=True), "1m")
    assert closed.attrs["structure_price_quantum"] == "0.01"
    assert closed.attrs["price_basis_revision"].startswith("sha256:")


def test_monitor_snapshot_normalizes_numeric_strings_and_rejects_infinity() -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    frame = _frame(metadata=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(str)

    closed = state._closed_frame(frame, "1m")
    assert float(closed["close"].iloc[-1]) == 100.4

    broken = _frame(metadata=True)
    broken.loc[1, "high"] = float("inf")
    with pytest.raises(ValueError, match="finite numbers"):
        state._closed_frame(broken, "1m")


@pytest.mark.parametrize("bad_price", (0.0, -1.0))
def test_monitor_snapshot_rejects_non_positive_ohlc(bad_price: float) -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    broken = _frame(metadata=True)
    broken.loc[1, ["open", "high", "low", "close"]] = bad_price

    with pytest.raises(ValueError, match="geometry is invalid"):
        state._closed_frame(broken, "1m")


def test_monitor_visible_price_uses_freshest_completed_feed() -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )

    def runtime(frequency: str, at: str, price: float):
        kline = SimpleNamespace(date=pd.Timestamp(at), c=price)
        return SimpleNamespace(
            cd=SimpleNamespace(get_src_klines=lambda: [kline]),
        )

    state._runtime_by_frequency = {
        "1m": runtime("1m", "2026-08-05T10:03:00+08:00", 100.3),
        "5m": runtime("5m", "2026-08-05T10:00:00+08:00", 100.5),
    }
    state.segment_difference_ready = True

    state._refresh_visible_price()

    assert state.last_px == 100.5
    assert state.last_px_source == "latest_completed_5m_close"
    assert state.last_px_observed_at == datetime(2026, 8, 5, 10, 5, tzinfo=CN)


def test_monitor_uses_frozen_observation_time_for_completed_prefix() -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="end"),
    )
    frame = _frame(metadata=True)
    frame["date"] = pd.to_datetime(
        (
            "2026-08-05 09:55:00+08:00",
            "2026-08-05 10:00:00+08:00",
            "2026-08-05 10:05:00+08:00",
            "2026-08-05 10:10:00+08:00",
        )
    )

    closed = state._closed_frame(
        frame,
        "5m",
        as_of=datetime(2026, 8, 5, 10, 3, tzinfo=CN),
    )

    assert tuple(closed["date"]) == (
        pd.Timestamp("2026-08-05 09:55:00+08:00"),
        pd.Timestamp("2026-08-05 10:00:00+08:00"),
    )


def test_valid_but_short_history_is_warming_not_refresh_failure() -> None:
    frame = _frame(metadata=True)
    exchange = SimpleNamespace(
        market="us",
        kline_time_label="start",
        klines=lambda *_args, **_kwargs: frame,
    )
    state = StrictPhysicalMonitorState("TSLA.US", exchange)

    events = collect_strict_monitor_events(
        {"TSLA.US": state},
        names={"TSLA.US": "Tesla"},
        holdings={"TSLA.US"},
    )

    assert events == []
    assert state.warmup_ready is False
    assert state.consecutive_warmup_incomplete == 1
    assert state.consecutive_refresh_failures == 0


def test_realtime_monitor_uses_the_shared_screening_warmup_floor() -> None:
    assert StrictPhysicalMonitorState.MINIMUM_BARS_BY_FREQ == {
        "d": 480,
        "30m": 480,
        "5m": 960,
        "1m": 1440,
    }


def test_realtime_monitor_has_no_fixed_wall_clock_signal_age_window() -> None:
    exchange = SimpleNamespace(market="us", kline_time_label="start")

    state = StrictPhysicalMonitorState("TSLA.US", exchange)

    assert not hasattr(state, "signal_freshness")
    assert not hasattr(state, "_is_fresh_point")


def test_poll_without_new_completed_bar_reuses_exact_evidence(monkeypatch) -> None:
    frame = _frame(metadata=True)
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    monkeypatch.setattr(
        state,
        "_fetch_klines",
        lambda *_args, **_kwargs: frame,
    )
    monkeypatch.setattr(
        state,
        "MINIMUM_BARS_BY_FREQ",
        {**state.MINIMUM_BARS_BY_FREQ, "1m": 1},
    )
    evidence = SimpleNamespace(marker="same-evidence")
    calls = []

    def _build(*_args, **_kwargs):
        calls.append(True)
        return evidence

    monkeypatch.setattr(monitor_module, "build_screening_evidence", _build)

    first = state._process_level("1m", "last_op", AT)
    second = state._process_level("1m", "last_op", AT)

    assert first is evidence
    assert second is evidence
    assert len(calls) == 1


def test_new_completed_bar_reuses_validated_incremental_state_with_full_frame(
    monkeypatch,
) -> None:
    first_frame = _frame(metadata=True)
    second_frame = pd.concat(
        (
            first_frame,
            pd.DataFrame(
                {
                    "date": [pd.Timestamp("2026-08-04 09:34", tz=CN)],
                    "open": [100.4],
                    "high": [100.6],
                    "low": [100.3],
                    "close": [100.5],
                    "volume": [1000.0],
                }
            ),
        ),
        ignore_index=True,
    )
    attach_price_basis_metadata(
        second_frame,
        build_provider_price_basis_metadata(
            provider="test-provider",
            market="us",
            code="TSLA.US",
            adjustment="none",
            structure_price_quantum=Decimal("0.01"),
        ),
    )
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    frames = iter((first_frame, second_frame))
    monkeypatch.setattr(
        state,
        "_fetch_klines",
        lambda *_args, **_kwargs: next(frames),
    )
    monkeypatch.setattr(
        state,
        "MINIMUM_BARS_BY_FREQ",
        {**state.MINIMUM_BARS_BY_FREQ, "1m": 1},
    )
    processed: list[tuple[str, int]] = []

    class _CD:
        def process_klines(self, frame):
            processed.append(("full", len(frame)))

        def process_validated_incremental_klines(self, frame):
            processed.append(("incremental", len(frame)))

    def _runtime(_frequency, metadata, source_frame):
        return monitor_module._FrequencyRuntime(
            cd=_CD(),
            metadata=metadata,
            strict_config_revision="strict-revision",
            source_frame=source_frame,
        )

    monkeypatch.setattr(state, "_new_runtime", _runtime)
    monkeypatch.setattr(
        monitor_module,
        "build_screening_evidence",
        lambda *_args, **_kwargs: SimpleNamespace(marker=len(processed)),
    )

    state._process_level("1m", "last_op", AT)
    state._process_level("1m", "last_op", AT)

    assert processed == [("full", 4), ("incremental", 5)]
    assert len(state._runtime_by_frequency["1m"].source_frame) == 5
    assert state._runtime_by_frequency["1m"].rebuild_count == 1
    assert state._runtime_by_frequency["1m"].incremental_update_count == 1


def test_first_refresh_emits_but_evidence_id_rebuild_does_not_repeat_point(
    monkeypatch,
) -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
        clock=lambda: AT,
    )
    current_points = [_point("3buy", center_id="first-build-center")]
    evidence_by_frequency = {
        frequency: SimpleNamespace(
            frequency=frequency,
            structure=SimpleNamespace(levels=[]),
        )
        for frequency in ("1m", "5m", "30m", "d")
    }

    class _SourceCD:
        def get_src_klines(self):
            return [SimpleNamespace(o=100.0, c=100.0)]

    state._runtime_by_frequency["1m"] = SimpleNamespace(cd=_SourceCD())

    def _process(frequency, last_attr, _observed_at):
        setattr(state, last_attr, pd.Timestamp(AT))
        return evidence_by_frequency[frequency]

    monkeypatch.setattr(state, "_process_level", _process)
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda evidence, **_kwargs: (
            tuple(current_points) if evidence.frequency == "5m" else ()
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "extract_one_minute_segment_difference_points",
        lambda *_args, **_kwargs: (),
    )

    assert state.refresh() == current_points
    current_points[:] = [_point("3buy", center_id="rebuilt-center")]
    assert state.refresh() == []


def test_monitor_runtime_uses_the_same_strict_profile_as_page_and_replay() -> None:
    frame = _frame(metadata=True)
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    metadata = state._closed_frame(frame, "1m")
    runtime = state._new_runtime(
        "1m",
        # Deliberately parse through the public snapshot gate just as runtime does.
        strict_snapshot_price_metadata(metadata),
        metadata,
    )

    config = runtime.cd.get_config()
    assert config["strict_macd_source"] == "native_l0_causal_recursive"
    assert "screening_structure_scope" not in config
    assert "recursive_structure_scope" not in config
    assert config["stroke_rule"] == "strict-cl-k-distance"
    assert runtime.strict_config_revision == config["strict_config_revision"]
