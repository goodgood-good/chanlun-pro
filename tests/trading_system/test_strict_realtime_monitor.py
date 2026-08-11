from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
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


def _point(point_type: str, *, point_id: str = "strict-point") -> StructuralPoint:
    side = "buy" if point_type.endswith("buy") else "sell"
    return StructuralPoint(
        point_id=point_id,
        code="TSLA.US",
        point_type=point_type,
        side=side,
        status="confirmed",
        variant="standard",
        source_frequency="1m",
        price_basis_revision="provider-basis",
        tower="formal",
        recursive_level=0,
        anchor_at=AT,
        confirmed_at=AT,
        available_at=AT,
        structure_anchor_price=100.0,
        structure_invalidation_price=99.0 if side == "buy" else 101.0,
        center_id="strict-center",
        center_zd=99.0,
        center_zg=100.0,
        center_ordinal=1,
        divergence_kind=None,
        parent_point_id=None,
        evidence_codes=("strict",),
    )


class _StrictState:
    op_level = "1m"
    mid_level = "5m"
    big_level = "30m"
    last_big = pd.Timestamp(AT)
    last_px = 100.0
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
    point = _point("3buy", point_id="sha256:strict-third-buy")

    [event] = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((point,))},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert event.bs_type == "3buy"
    assert event.side == "buy"
    assert event.kind == "strict_buy_point"
    assert event.evidence_id == point.point_id
    assert event.identity == ("strict_buy_point|TSLA.US|3buy|2026-08-05T10:00:00+08:00")
    assert event.signal_time == AT.isoformat(timespec="seconds")


def test_strict_collector_applies_high_level_gate_but_keeps_sell_points() -> None:
    buy = _point("3buy", point_id="buy")
    sell = _point("3sell", point_id="sell")

    events = collect_strict_monitor_events(
        {"TSLA.US": _StrictState((buy, sell), big="down")},
        names={"TSLA.US": "Tesla"},
        holdings=set(),
    )

    assert [(event.side, event.evidence_id) for event in events] == [("sell", "sell")]


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


def test_poll_without_new_completed_bar_reuses_exact_evidence(monkeypatch) -> None:
    frame = _frame(metadata=True)
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    monkeypatch.setattr(state, "_fetch_klines", lambda *_args: frame)
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


def test_new_completed_bar_rebuilds_from_the_complete_authoritative_frame(
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
    monkeypatch.setattr(state, "_fetch_klines", lambda *_args: next(frames))
    monkeypatch.setattr(
        state,
        "MINIMUM_BARS_BY_FREQ",
        {**state.MINIMUM_BARS_BY_FREQ, "1m": 1},
    )
    processed_lengths: list[int] = []

    class _CD:
        def process_klines(self, frame):
            processed_lengths.append(len(frame))

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
        lambda *_args, **_kwargs: SimpleNamespace(marker=len(processed_lengths)),
    )

    state._process_level("1m", "last_op", AT)
    state._process_level("1m", "last_op", AT)

    assert processed_lengths == [4, 5]
    assert len(state._runtime_by_frequency["1m"].source_frame) == 5


def test_first_successful_refresh_only_builds_a_semantic_baseline(monkeypatch) -> None:
    state = StrictPhysicalMonitorState(
        "TSLA.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    current_points = [_point("3buy", point_id="first-build-id")]
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
        "extract_confirmed_points",
        lambda evidence, **_kwargs: (
            tuple(current_points) if evidence.frequency == "1m" else ()
        ),
    )

    assert state.refresh() == []
    current_points[:] = [_point("3buy", point_id="rebuilt-same-occurrence")]
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
