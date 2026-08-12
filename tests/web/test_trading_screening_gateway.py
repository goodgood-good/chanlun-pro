from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal

import pandas as pd
import pytest

from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config
import chanlun.decision_support.trading_system.screening_runtime as screening_runtime_module
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeDataUnavailable,
    HigherTimeframeGateBundle,
    HigherTimeframeGateEvidence,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthEvidence,
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.selection import (
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from tests.trading_system.helpers import confirmed_point, provisional_point
from tests.trading_system.strict_helpers import (
    StrictOnlyCL,
    strict_evidence_result,
    strict_point,
)
from cl_app.services import trading_screening_gateway as gateway_module
from cl_app.services.trading_screening_gateway import (
    FrameStructureAnalysis,
    NativeTradingDataGateway,
    NativeTradingGatewayConfig,
    analyze_native_frame,
    analyze_native_frame_with_warmup,
    audit_native_frame_warmup_envelope,
)


NOW = datetime.fromisoformat("2026-07-20T10:02:00+08:00")


def test_entry_boundary_uses_unadjusted_confirmation_high_not_anchor() -> None:
    point = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=8.0,
    )
    raw = _frame()
    raw.attrs.update(
        price_basis_provider="qmt",
        price_basis_adjustment="none",
    )

    boundaries = gateway_module._entry_execution_boundaries(
        code="SZ.000001",
        points=(point,),
        raw_frame=raw,
    )

    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary.raw_high == Decimal("10.2")
    assert boundary.raw_high != Decimal(str(point.structure_anchor_price))
    assert boundary.confirmation_bar_closed_at == point.available_at
    assert boundary.entry_valid_until == datetime.fromisoformat(
        "2026-07-20T10:01:00+08:00"
    )
    assert boundary.tick_data_used is False


@pytest.mark.parametrize(
    ("confirmed", "expected"),
    (
        ("2026-07-20T09:31:00+08:00", "2026-07-20T09:32:00+08:00"),
        ("2026-07-20T11:29:00+08:00", "2026-07-20T11:30:00+08:00"),
        ("2026-07-20T11:30:00+08:00", "2026-07-20T11:30:00+08:00"),
        ("2026-07-20T13:01:00+08:00", "2026-07-20T13:02:00+08:00"),
        ("2026-07-20T14:59:00+08:00", "2026-07-20T15:00:00+08:00"),
        ("2026-07-20T15:00:00+08:00", "2026-07-20T15:00:00+08:00"),
    ),
)
def test_entry_ttl_never_crosses_lunch_or_session_close(
    confirmed: str,
    expected: str,
) -> None:
    assert gateway_module._entry_valid_until(datetime.fromisoformat(confirmed)) == (
        datetime.fromisoformat(expected)
    )


@pytest.mark.parametrize(
    "confirmed",
    (
        "2026-07-20T09:30:00+08:00",
        "2026-07-20T12:00:00+08:00",
        "2026-07-20T13:00:00+08:00",
        "2026-07-20T15:01:00+08:00",
    ),
)
def test_entry_ttl_rejects_non_continuous_auction_close(confirmed: str) -> None:
    with pytest.raises(ValueError, match="outside A-share continuous auction"):
        gateway_module._entry_valid_until(datetime.fromisoformat(confirmed))


def test_entry_ttl_rejects_second_level_confirmation() -> None:
    with pytest.raises(ValueError, match="align to exchange minutes"):
        gateway_module._entry_valid_until(
            datetime.fromisoformat("2026-07-20T10:02:30+08:00")
        )


def test_default_gateway_requests_complete_recursive_cold_start_context() -> None:
    config = NativeTradingGatewayConfig()

    assert dict(config.request_bars_by_frequency) == {
        "d": 1600,
        "30m": 4000,
        "5m": 12000,
        "1m": 12000,
    }


def test_analyzer_uses_canonical_recursive_screening_builder(monkeypatch) -> None:
    closed_at = datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    confirmed = strict_point("1buy", available_at=closed_at)
    approaching = strict_point(
        "2buy",
        status=StrictPointStatus.APPROACHING,
        available_at=closed_at,
    )
    approaching = replace(
        approaching,
        parent_point_id=confirmed.point_id,
    )
    evidence = strict_evidence_result(
        code="SZ.000001",
        source_frequency="5m",
        source_closed_at=closed_at,
        confirmed_points=(confirmed,),
        approaching_points=(approaching,),
    )
    builder_calls = []

    def build(**kwargs):
        builder_calls.append(kwargs)
        return evidence

    monkeypatch.setattr(gateway_module, "screening_evidence_from_frame", build)

    analysis = analyze_native_frame(
        code="SZ.000001",
        frequency="5m",
        frame=_frame(),
        as_of=closed_at,
    )

    assert len(builder_calls) == 1
    assert builder_calls[0]["code"] == "SZ.000001"
    assert builder_calls[0]["frequency"] == "5m"
    assert builder_calls[0]["as_of"] == closed_at
    assert analysis.direction == "neutral"
    assert tuple(point.point_type for point in analysis.confirmed_points) == ("1buy",)
    assert tuple(point.point_type for point in analysis.provisional_points) == ("2buy",)


def test_analyzer_builds_strict_cl_from_snapshot_metadata(monkeypatch) -> None:
    closed_at = datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    state = StrictOnlyCL(
        strict_evidence_result(
            code="SZ.000001",
            source_frequency="5m",
            source_closed_at=closed_at,
        )
    )
    captured = {}

    def factory(code, frequency, config, *, market):
        captured.update(
            code=code,
            frequency=frequency,
            config=config,
            market=market,
        )
        return state

    monkeypatch.setattr(screening_runtime_module, "CL", factory)
    monkeypatch.setattr(
        screening_runtime_module,
        "build_screening_evidence",
        lambda *_args, **_kwargs: state.evidence,
    )
    analyze_native_frame(
        code="SZ.000001",
        frequency="5m",
        frame=_frame(),
        as_of=closed_at,
    )

    assert captured == {
        "code": "SZ.000001",
        "frequency": "5m",
        "config": strict_cl_config(
            structure_price_quantum=Decimal("0.01"),
            price_basis_revision="test-raw",
        ),
        "market": "a",
    }


def test_analyzer_rejects_snapshot_without_price_basis_metadata(monkeypatch) -> None:
    frame = _frame()
    frame.attrs.clear()
    monkeypatch.setattr(
        screening_runtime_module,
        "CL",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CL must not be created without metadata")
        ),
    )

    with pytest.raises(ValueError, match="structure_price_quantum metadata"):
        analyze_native_frame(
            code="SZ.000001",
            frequency="5m",
            frame=frame,
            as_of=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
        )


def test_analyzer_wraps_known_strict_structure_contract_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_module,
        "screening_evidence_from_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            gateway_module.StrictStructureContractError(
                "unit directions must alternate"
            )
        ),
    )

    with pytest.raises(
        gateway_module.StrictStructureAnalysisError,
        match="unit directions must alternate",
    ):
        analyze_native_frame(
            code="SH.880471",
            frequency="5m",
            frame=_frame(),
            as_of=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
        )


def test_analyzer_does_not_wrap_unrelated_type_error(monkeypatch) -> None:
    def invalid_factory(*_args, **_kwargs):
        raise TypeError("unrelated decoder failure")

    monkeypatch.setattr(
        gateway_module,
        "screening_evidence_from_frame",
        invalid_factory,
    )

    with pytest.raises(TypeError, match="unrelated decoder failure"):
        analyze_native_frame(
            code="SH.880471",
            frequency="5m",
            frame=_frame(),
            as_of=datetime.fromisoformat("2026-07-20T10:01:00+08:00"),
        )


def test_warmup_requires_same_active_tail_under_shorter_left_history(
    monkeypatch,
) -> None:
    dates = pd.date_range(
        "2026-01-01T10:00:00+08:00",
        periods=600,
        freq="30min",
    )
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000.0,
        }
    )
    calls = []

    def stable(**kwargs):
        calls.append(len(kwargs["frame"]))
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up",
            confirmed_points=(),
            provisional_points=(),
        )

    monkeypatch.setattr(gateway_module, "analyze_native_frame", stable)
    result = analyze_native_frame_with_warmup(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )

    assert calls == [600, 400]
    assert result.warmup_converged is True
    assert result.warmup_reason_codes == ("WARMUP_TAIL_STABLE",)
    assert result.warmup_difference_codes == ()

    def stable_semantics_with_prefix_scoped_ids(**kwargs):
        point = provisional_point("3buy", frequency="30m")
        point = replace(
            point,
            candidate_id=f"candidate:prefix:{len(kwargs['frame'])}",
        )
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up",
            confirmed_points=(),
            provisional_points=(point,),
        )

    monkeypatch.setattr(
        gateway_module,
        "analyze_native_frame",
        stable_semantics_with_prefix_scoped_ids,
    )
    result = analyze_native_frame_with_warmup(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )
    assert result.warmup_converged is True

    def provisional_preview_differs(**kwargs):
        approaching = (
            (provisional_point("3buy", frequency="30m"),)
            if len(kwargs["frame"]) == 600
            else ()
        )
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up",
            confirmed_points=(),
            provisional_points=approaching,
        )

    monkeypatch.setattr(
        gateway_module,
        "analyze_native_frame",
        provisional_preview_differs,
    )
    result = analyze_native_frame_with_warmup(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )
    assert result.warmup_converged is True
    assert result.warmup_difference_codes == ()

    def divergent(**kwargs):
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up" if len(kwargs["frame"]) == 600 else "down",
            confirmed_points=(),
            provisional_points=(),
        )

    monkeypatch.setattr(gateway_module, "analyze_native_frame", divergent)
    result = analyze_native_frame_with_warmup(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )
    assert result.warmup_converged is False
    assert result.warmup_reason_codes == ("WARMUP_TAIL_DIVERGED",)
    assert result.warmup_difference_codes == ("WARMUP_DIRECTION_CHANGED",)


def test_warmup_envelope_detects_non_monotonic_prefix_stability(
    monkeypatch,
) -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2025-01-01T10:00:00+08:00",
                periods=2400,
                freq="30min",
            ),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000.0,
        }
    )
    calls = []

    def non_monotonic(**kwargs):
        bar_count = len(kwargs["frame"])
        calls.append(bar_count)
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up" if bar_count in {1200, 2400} else "down",
            confirmed_points=(),
            provisional_points=(),
        )

    monkeypatch.setattr(gateway_module, "analyze_native_frame", non_monotonic)

    result = audit_native_frame_warmup_envelope(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )

    assert calls == [1200, 1600, 2000, 2400]
    assert result.status == "NON_MONOTONIC"
    assert result.match_longest_pattern == (True, False, False, True)
    assert result.stable_all_prefixes is False
    assert result.diagnostic_only is True
    assert result.active_gate_unchanged is True


def test_warmup_envelope_requires_three_qualified_prefixes(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-01-01T10:00:00+08:00",
                periods=600,
                freq="30min",
            ),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000.0,
        }
    )
    calls = []

    def stable(**kwargs):
        calls.append(len(kwargs["frame"]))
        return FrameStructureAnalysis(
            closed_at=kwargs["as_of"],
            direction="up",
            confirmed_points=(),
            provisional_points=(),
        )

    monkeypatch.setattr(gateway_module, "analyze_native_frame", stable)

    result = audit_native_frame_warmup_envelope(
        code="SZ.000001",
        frequency="30m",
        frame=frame,
        as_of=NOW,
    )

    assert calls == [500, 600]
    assert result.status == "INSUFFICIENT_PREFIXES"
    assert result.stable_all_prefixes is False


def _frame(*, with_metadata: bool = True) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-07-20T10:00:00+08:00", "2026-07-20T10:01:00+08:00"]
            ),
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [1000, 1200],
        }
    )
    if with_metadata:
        frame.attrs["structure_price_quantum"] = "0.01"
        frame.attrs["price_basis_revision"] = "test-raw"
    return frame


def _qmt_sector_five_frame(
    *,
    request_bars: int,
    member_count: int,
    as_of: datetime,
) -> pd.DataFrame:
    previous_session = tuple(
        pd.Timestamp(f"2026-07-17T{hour:02d}:{minute:02d}:00+08:00")
        for start, end in ((9 * 60 + 35, 11 * 60 + 30), (13 * 60 + 5, 15 * 60))
        for value in range(start, end + 1, 5)
        for hour, minute in (divmod(value, 60),)
    )
    # NOW is 10:02.  Model a completed prior session plus the exact completed
    # prefix of the current session; no mock bar may come from after NOW.
    current_prefix = tuple(
        pd.date_range(
            "2026-07-20T09:35:00+08:00",
            periods=6,
            freq="5min",
        )
    )
    dates = previous_session + current_prefix
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0 + index / 100 for index in range(len(dates))],
            "high": [10.1 + index / 100 for index in range(len(dates))],
            "low": [9.9 + index / 100 for index in range(len(dates))],
            "close": [10.05 + index / 100 for index in range(len(dates))],
            "volume": [member_count] * len(dates),
            "member_mask": [(1 << member_count) - 1] * len(dates),
        }
    )
    # Model the provider contract: it must never return a completed bar later
    # than the requested decision time, even when its backing fixture contains
    # later rows from the same session.
    frame = (
        frame.loc[frame["date"] <= pd.Timestamp(as_of)]
        .tail(request_bars)
        .reset_index(drop=True)
    )
    frame.attrs.update(
        structure_price_quantum="0.000001",
        price_basis_revision="qmt-gics3-composite",
        price_basis_provider="qmt-gics3-composite",
        price_basis_adjustment=(gateway_module.QMT_GICS3_COMPOSITE_ADJUSTMENT),
        sector_factor_adjustment_contract_id=(
            gateway_module.QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
        ),
        sector_factor_revision="sha256:" + "a" * 64,
        sector_composite_member_path_revision=(
            f"sha256:test-path-{member_count}-{len(frame)}"
        ),
    )
    return frame


class RecordingExchange:
    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.frame = _frame() if frame is None else frame
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.info_calls: list[str] = []
        self.type_calls: list[tuple[str, ...]] = []

    def klines(self, code: str, frequency: str, *, args: dict[str, object]):
        assert args["req_counts"] == 4
        self.calls.append((code, frequency, dict(args)))
        return self.frame.copy(deep=True)

    def stock_info(self, code: str):
        self.info_calls.append(code)
        return {"code": code, "name": "测试名称", "precision": 2}

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> dict[str, str]:
        self.type_calls.append(codes)
        return {
            code: (
                "index_cn"
                if code == "SH.000001"
                else "etf_cn"
                if code in {"SH.510300", "SZ.159915"}
                else "stock_cn"
            )
            for code in codes
        }

    def now_trading(self, market: str) -> bool:
        assert market == "a"
        return False


class RecordingAnalyzer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.frames: list[pd.DataFrame] = []

    def __call__(self, *, code, frequency, frame, as_of):
        self.calls.append((code, frequency))
        self.frames.append(frame.copy(deep=True))
        approaching = (
            (provisional_point("2buy"),)
            if code == "SZ.000001" and frequency == "5m"
            else ()
        )
        return FrameStructureAnalysis(
            closed_at=as_of,
            direction="neutral",
            confirmed_points=(),
            provisional_points=approaching,
        )


def _qmt_native_one_minute_session(*, adjustment: str) -> pd.DataFrame:
    opening = pd.Timestamp("2026-07-20T09:30:00+08:00")
    morning = pd.date_range(
        "2026-07-20T09:31:00+08:00",
        periods=120,
        freq="min",
    )
    afternoon = pd.date_range(
        "2026-07-20T13:01:00+08:00",
        periods=120,
        freq="min",
    )
    dates = (opening, *morning, *afternoon)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0, *([10.1] * 240)],
            "high": [10.4, *([10.3] * 240)],
            "low": [9.8, *([9.9] * 240)],
            "close": [10.1, *([10.2] * 240)],
            "volume": [100.0, *([200.0] * 240)],
        }
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision=f"qmt-{adjustment}-test",
        price_basis_provider="qmt",
        price_basis_adjustment=adjustment,
    )
    return frame


class OpeningAuctionExchange:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def klines(self, _code: str, frequency: str, *, args: dict[str, object]):
        assert frequency == "1m"
        self.calls.append(dict(args))
        adjustment = "none" if args.get("dividend_type") == "none" else "front_ratio"
        return _qmt_native_one_minute_session(adjustment=adjustment)


class OpeningAuctionAnalyzer(RecordingAnalyzer):
    def __call__(self, *, code, frequency, frame, as_of):
        self.calls.append((code, frequency))
        self.frames.append(frame.copy(deep=True))
        first_closed_at = gateway_module._market_datetime(
            frame["date"].iloc[0],
            "first normalized bar",
        )
        minutes_after = int(
            (
                first_closed_at - datetime.fromisoformat("2026-07-20T10:00:00+08:00")
            ).total_seconds()
            // 60
        )
        return FrameStructureAnalysis(
            closed_at=as_of,
            direction="neutral",
            confirmed_points=(
                confirmed_point(
                    "1buy",
                    frequency="1m",
                    minutes_after=minutes_after,
                ),
            ),
            provisional_points=(),
        )


def test_native_gateway_merges_opening_event_for_structure_and_entry_boundary() -> None:
    """The page and replay must consume the same completed 240-bar 1m grid."""

    exchange = OpeningAuctionExchange()
    analyzer = OpeningAuctionAnalyzer()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: exchange,
        sector_provider=lambda: {"source": "test", "sectors": ()},
        watchlist_provider=lambda: (),
        holdings_provider=lambda: (),
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 300)),
            minimum_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )

    analysis = gateway._load_analysis(
        exchange=exchange,
        code="SZ.300412",
        analysis_code="SZ.300412",
        frequency="1m",
        as_of=datetime.fromisoformat("2026-07-20T15:00:00+08:00"),
    )

    [structure_frame] = analyzer.frames
    assert len(structure_frame) == 240
    first = structure_frame.iloc[0]
    assert first["date"] == pd.Timestamp("2026-07-20T09:31:00+08:00")
    assert first["open"] == 10.0
    assert first["high"] == 10.4
    assert first["low"] == 9.8
    assert first["close"] == 10.2
    assert first["volume"] == 300.0
    assert structure_frame.attrs["price_basis_adjustment"] == "front_ratio"

    [boundary] = analysis.entry_execution_boundaries
    assert boundary.confirmation_bar_closed_at == datetime.fromisoformat(
        "2026-07-20T09:31:00+08:00"
    )
    assert boundary.raw_high == Decimal("10.4")
    assert boundary.raw_volume == Decimal("300.0")
    assert boundary.raw_price_basis_revision == "qmt-none-test"
    assert len(exchange.calls) == 2


class SparseOpeningAuctionExchange(OpeningAuctionExchange):
    def klines(self, _code: str, frequency: str, *, args: dict[str, object]):
        frame = super().klines(_code, frequency, args=args)
        return frame.drop(index=[1, 2]).reset_index(drop=True)


def test_native_gateway_keeps_serving_when_live_qmt_omits_0931() -> None:
    exchange = SparseOpeningAuctionExchange()
    analyzer = OpeningAuctionAnalyzer()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: exchange,
        sector_provider=lambda: {"source": "test", "sectors": ()},
        watchlist_provider=lambda: (),
        holdings_provider=lambda: (),
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 300)),
            minimum_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )

    analysis = gateway._load_analysis(
        exchange=exchange,
        code="SH.603768",
        analysis_code="SH.603768",
        frequency="1m",
        as_of=datetime.fromisoformat("2026-07-20T15:00:00+08:00"),
    )

    [structure_frame] = analyzer.frames
    assert tuple(structure_frame.iloc[:2]["date"].dt.time) == (
        time(9, 31),
        time(9, 33),
    )
    assert structure_frame.iloc[0]["volume"] == 100.0
    [boundary] = analysis.entry_execution_boundaries
    assert boundary.confirmation_bar_closed_at == datetime.fromisoformat(
        "2026-07-20T09:31:00+08:00"
    )
    assert boundary.raw_volume == Decimal("100.0")
    assert len(exchange.calls) == 2


class InvalidNativeThirtyExchange:
    def __init__(self, *, invalid_five: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.invalid_five = invalid_five

    def klines(self, code: str, frequency: str, *, args: dict[str, object]):
        self.calls.append((code, frequency, dict(args)))
        if frequency == "30m":
            frame = pd.DataFrame(
                {
                    "date": pd.to_datetime(
                        ("2026-07-20T10:00:00+08:00", "2026-07-20T10:30:00+08:00")
                    ),
                    "open": (10.0, 10.1),
                    "high": (10.2, 10.2),
                    "low": (9.9, 10.0),
                    "close": (10.1, 10.3),
                    "volume": (600.0, 600.0),
                }
            )
        elif frequency == "5m":
            dates = pd.date_range(
                "2026-07-20T09:35:00+08:00",
                periods=12,
                freq="5min",
            )
            frame = pd.DataFrame(
                {
                    "date": dates,
                    "open": [10.0 + index / 100 for index in range(12)],
                    "high": [10.1 + index / 100 for index in range(12)],
                    "low": [9.9 + index / 100 for index in range(12)],
                    "close": [10.05 + index / 100 for index in range(12)],
                    "volume": [100.0] * 12,
                }
            )
            if self.invalid_five:
                frame.loc[0, "high"] = 9.0
        else:  # pragma: no cover - contract guard
            raise AssertionError(f"unexpected frequency: {frequency}")
        frame.attrs.update(
            structure_price_quantum="0.01",
            price_basis_revision="qmt-front-ratio-test",
        )
        return frame


class FailingAnalyzer(RecordingAnalyzer):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def __call__(self, *, code, frequency, frame, as_of):
        self.calls.append((code, frequency))
        self.frames.append(frame.copy(deep=True))
        raise self.exc


def _gateway(
    *,
    analyzer: RecordingAnalyzer | None = None,
    sector_strength_provider=None,
    higher_timeframe_provider=None,
) -> tuple[NativeTradingDataGateway, RecordingAnalyzer, RecordingExchange]:
    stock_exchange = RecordingExchange()
    analyzer = analyzer if analyzer is not None else RecordingAnalyzer()

    def sector_frame_provider(**kwargs):
        return _qmt_sector_five_frame(
            request_bars=kwargs["request_bars"],
            member_count=len(kwargs["members"]),
            as_of=kwargs["as_of"],
        )

    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_provider=lambda: {
            "source": gateway_module.QMT_GICS3_CATALOG_SOURCE,
            "sectors": [
                {
                    "sector_id": "qmt-gics3:bank",
                    "name": "银行",
                    "source_key": "GICS3银行",
                    "member_codes": ["SZ.000001", "SH.600000"],
                }
            ],
        },
        sector_frame_provider=sector_frame_provider,
        sector_strength_provider=sector_strength_provider,
        higher_timeframe_provider=higher_timeframe_provider,
        watchlist_provider=lambda: ({"code": "SZ.000001"},),
        holdings_provider=lambda: ("SH.600000",),
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 4), ("30m", 4), ("5m", 4), ("1m", 4)),
            minimum_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )
    return gateway, analyzer, stock_exchange


def test_native_gateway_attaches_horizontal_strength_and_rank() -> None:
    calls = []

    def strength_provider(**kwargs):
        calls.append(kwargs)
        return {
            "qmt-gics3:bank": SectorStrengthEvidence(
                sector_id="qmt-gics3:bank",
                observed_at=NOW,
                anchor_session=date(2026, 7, 1),
                member_count=2,
                strength=Decimal("7.5"),
                rank=1,
                source_revision="sha256:strength-test",
                reason_codes=("EQUAL_WEIGHT_MEMBER_MA_CATEGORY_MEAN",),
            )
        }

    gateway, _analyzer, _exchange = _gateway(sector_strength_provider=strength_provider)
    [assessment] = gateway.native_sector_assessments(as_of=NOW).assessments

    assert assessment.horizontal_strength == Decimal("7.5")
    assert assessment.horizontal_rank == 1
    assert assessment.strength_anchor_session == date(2026, 7, 1)
    assert assessment.strength_member_count == 2
    assert calls[0]["members_by_sector"] == {
        "qmt-gics3:bank": ("SH.600000", "SZ.000001")
    }


def test_native_gateway_carries_recomputable_strength_batch() -> None:
    def strength_provider(**kwargs):
        return build_horizontal_sector_strength_batch(
            decision_time=kwargs["as_of"],
            benchmark_symbol="SH.000300",
            benchmark_daily=(),
            members_by_sector={
                sector_id: tuple(
                    SectorMemberHistory(
                        symbol,
                        kwargs["as_of"].date(),
                        "UNEXPLAINED_GAP",
                        (),
                    )
                    for symbol in members
                )
                for sector_id, members in kwargs["members_by_sector"].items()
            },
            membership_revision=kwargs["membership_revision"],
        )

    gateway, _analyzer, _exchange = _gateway(sector_strength_provider=strength_provider)
    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.strength_evidence is not None
    assert (
        batch.strength_evidence.evidence_document()["membership_revision"]
        == batch.catalog_revision
    )
    [assessment] = batch.assessments
    assert assessment.strength_source_revision == (
        batch.strength_evidence[assessment.sector_id].source_revision
    )


def test_sector_strength_time_uses_completed_market_cutoff_not_worker_clock() -> None:
    market_cutoff = datetime.fromisoformat("2026-07-20T15:00:00+08:00")
    overnight_scan = datetime.fromisoformat("2026-07-21T02:35:00+08:00")
    calls: list[dict[str, object]] = []

    class PriorCloseAnalyzer(RecordingAnalyzer):
        def __call__(self, *, code, frequency, frame, as_of):
            del frame, as_of
            self.calls.append((code, frequency))
            return FrameStructureAnalysis(
                closed_at=market_cutoff,
                direction="neutral",
                confirmed_points=(),
                provisional_points=(),
            )

    def strength_provider(**kwargs):
        calls.append(kwargs)
        return build_horizontal_sector_strength_batch(
            decision_time=kwargs["as_of"],
            benchmark_symbol="SH.000300",
            benchmark_daily=(),
            members_by_sector={
                sector_id: tuple(
                    SectorMemberHistory(
                        symbol,
                        market_cutoff.date(),
                        "UNEXPLAINED_GAP",
                        (),
                    )
                    for symbol in members
                )
                for sector_id, members in kwargs["members_by_sector"].items()
            },
            membership_revision=kwargs["membership_revision"],
        )

    gateway, _analyzer, _exchange = _gateway(
        analyzer=PriorCloseAnalyzer(),
        sector_strength_provider=strength_provider,
    )
    batch = gateway.native_sector_assessments(as_of=overnight_scan)

    assert calls[0]["as_of"] == market_cutoff
    assert batch.strength_evidence is not None
    assert batch.strength_evidence.evidence_document()["decision_time"] == (
        market_cutoff.isoformat()
    )


def test_structure_bundle_attaches_and_enforces_mwd_risk_gates() -> None:
    calls: list[dict[str, object]] = []

    def evidence(
        subject: str,
        observed_at: datetime,
    ) -> HigherTimeframeGateEvidence:
        return HigherTimeframeGateEvidence(
            subject=subject,
            observed_at=observed_at,
            monthly="NONE",
            weekly="NONE",
            daily="NONE",
            gate="GREEN",
            grade="RESEARCH_ONLY",
            snapshot_id=f"snapshot:{subject}",
            source_revision=f"source:{subject}",
        )

    def higher_timeframe_provider(**kwargs):
        calls.append(kwargs)
        return HigherTimeframeGateBundle(
            market=evidence("MARKET", kwargs["as_of"]),
            sector=evidence("QMT:GICS3:bank", kwargs["as_of"]),
            symbol=evidence("SZ.000001", kwargs["as_of"]),
        )

    gateway, _analyzer, _exchange = _gateway(
        higher_timeframe_provider=higher_timeframe_provider
    )
    [sector] = gateway.native_sector_assessments(as_of=NOW).assessments
    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.enforce_higher_timeframe_entry_gate is True
    assert bundle.higher_timeframe_gates is not None
    assert bundle.higher_timeframe_gates.market.gate == "GREEN"
    assert bundle.higher_timeframe_gates.sector is not None
    assert bundle.higher_timeframe_gates.sector.gate == "GREEN"
    assert len(calls) == 1
    assert calls[0]["symbol"] == "SZ.000001"
    assert calls[0]["as_of"] == bundle.as_of
    assert calls[0]["sector_id"] == sector.sector_id
    assert calls[0]["sector_name"] == sector.sector_name
    assert calls[0]["sector_members"] == ("SH.600000", "SZ.000001")


def test_structure_bundle_keeps_newer_1m_signal_but_freezes_mwd_cutoff() -> None:
    """09:47 low-level precision may consume only 09:45 M/W/D evidence."""

    calls: list[dict[str, object]] = []
    cutoff = datetime.fromisoformat("2026-07-20T10:00:00+08:00")

    def evidence(
        subject: str,
        observed_at: datetime,
    ) -> HigherTimeframeGateEvidence:
        return HigherTimeframeGateEvidence(
            subject=subject,
            observed_at=observed_at,
            monthly="NONE",
            weekly="NONE",
            daily="NONE",
            gate="GREEN",
            grade="RESEARCH_ONLY",
            snapshot_id=f"snapshot:{subject}",
            source_revision=f"source:{subject}",
        )

    def higher_timeframe_provider(**kwargs):
        calls.append(kwargs)
        observed_at = kwargs["as_of"]
        return HigherTimeframeGateBundle(
            market=evidence("MARKET", observed_at),
            sector=evidence("QMT:GICS3:bank", observed_at),
            symbol=evidence("SZ.000001", observed_at),
        )

    gateway, _analyzer, _exchange = _gateway(
        higher_timeframe_provider=higher_timeframe_provider
    )
    [sector] = gateway.native_sector_assessments(as_of=NOW).assessments
    bundle = gateway.structure_bundle(
        "SZ.000001",
        as_of=NOW,
        sector=sector,
        higher_timeframe_as_of=cutoff,
    )

    assert bundle.as_of == datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    assert calls[0]["as_of"] == cutoff
    assert bundle.higher_timeframe_gates is not None
    assert bundle.higher_timeframe_gates.market.observed_at == cutoff
    assert bundle.higher_timeframe_gates.symbol.observed_at == cutoff
    assert bundle.higher_timeframe_gates.sector is not None
    assert bundle.higher_timeframe_gates.sector.observed_at == cutoff


def test_structure_bundle_preserves_higher_timeframe_data_blockers() -> None:
    def unavailable(**_kwargs):
        raise HigherTimeframeDataUnavailable(
            (
                "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
                "QMT_ONE_MINUTE_SESSION_GRID_INVALID",
                "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            ),
            session_issues=(
                QmtMinuteSessionIssue(
                    session=date(2026, 7, 22),
                    code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
                    observed_rows=0,
                    detail=(
                        "trading-calendar session is absent from the QMT 1m prefix"
                    ),
                ),
                QmtMinuteSessionIssue(
                    session=date(2026, 7, 23),
                    code="QMT_ONE_MINUTE_SESSION_GRID_INVALID",
                    observed_rows=239,
                    detail=(
                        "expected 09:30 opening event plus the completed 240-bar "
                        "grid, or an exact current-session prefix"
                    ),
                ),
            ),
        )

    gateway, _analyzer, _exchange = _gateway(higher_timeframe_provider=unavailable)
    [sector] = gateway.native_sector_assessments(as_of=NOW).assessments
    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.higher_timeframe_gates is not None
    expected = (
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
        "QMT_ONE_MINUTE_SESSION_GRID_INVALID",
    )
    assert bundle.higher_timeframe_gates.market.reason_codes == expected
    assert bundle.higher_timeframe_gates.symbol.reason_codes == expected
    assert bundle.higher_timeframe_gates.symbol.session_evidence is not None
    document = bundle.higher_timeframe_gates.symbol.session_evidence.document()
    assert document["issue_count"] == 2
    assert [value["session"] for value in document["issues"]] == [
        "2026-07-22",
        "2026-07-23",
    ]
    assert all(
        value["historical_trade_status_proven"] is False for value in document["issues"]
    )


def test_structure_bundle_skips_mwd_read_when_decision_output_is_provably_empty() -> (
    None
):
    """No current 5m setup means the shared decision core emits no row.

    The M/W/D provider derives roughly 300 sessions from a QMT 1m base stream.
    Calling it on this empty branch changes no decision but makes a full-market
    coverage cycle hours slower.
    """

    calls: list[dict[str, object]] = []

    def higher_timeframe_provider(**kwargs):
        calls.append(kwargs)
        raise AssertionError("empty decision branch must not read M/W/D history")

    gateway, _analyzer, _exchange = _gateway(
        higher_timeframe_provider=higher_timeframe_provider
    )
    [sector] = gateway.native_sector_assessments(as_of=NOW).assessments

    # RecordingAnalyzer creates a current 5m setup only for SZ.000001.
    bundle = gateway.structure_bundle("SH.600000", as_of=NOW, sector=sector)

    assert calls == []
    assert bundle.higher_timeframe_gates is None
    assert bundle.enforce_higher_timeframe_entry_gate is True
    assert HumanAssistedDecisionCore().evaluate_symbol(bundle) == ()


def test_native_gateway_ranks_real_sector_bars_and_emits_only_changed_keys() -> None:
    gateway, analyzer, _sector_exchange = _gateway()

    batch = gateway.native_sector_assessments(as_of=NOW)
    assessments = batch.assessments

    assert len(assessments) == 1
    assert batch.completed_count == batch.discovered_count == 1
    assert assessments[0].sector_id == "qmt-gics3:bank"
    assert assessments[0].eligible is True
    assert gateway.members() == {"qmt-gics3:bank": ("SH.600000", "SZ.000001")}
    first = gateway.changed_bars(None)
    second = gateway.changed_bars(NOW)
    assert {item.frequency for item in first} == {"5m", "30m"}
    assert {item.code for item in first} == {"qmt-gics3:bank"}
    assert second == ()
    assert analyzer.calls[:2] == [
        ("qmt-gics3:bank", "30m"),
        ("qmt-gics3:bank", "5m"),
    ]


def test_fresh_gateway_does_not_reemit_bars_at_cached_market_cutoff() -> None:
    """A web restart must not turn an unchanged close into a full-market scan."""

    gateway, _analyzer, _sector_exchange = _gateway()
    gateway.native_sector_assessments(as_of=NOW)

    assert gateway.changed_bars(NOW) == ()


def test_gateway_uses_qmt_gics3_component_frames_for_sector_assessment() -> None:
    stock_exchange = RecordingExchange()
    analyzer = RecordingAnalyzer()
    frame_calls: list[dict[str, object]] = []

    def qmt_sector_frame(**kwargs):
        frame_calls.append(dict(kwargs))
        return _qmt_sector_five_frame(
            request_bars=kwargs["request_bars"],
            member_count=2,
            as_of=kwargs["as_of"],
        )

    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_frame_provider=qmt_sector_frame,
        sector_provider=lambda: {
            "source": "qmt_gics3_components",
            "sectors": [
                {
                    "sector_id": "qmt-gics3:bank",
                    "name": "商业银行",
                    "source_key": "GICS3商业银行",
                    "member_codes": ["SZ.000001", "SH.600000"],
                }
            ],
        },
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 4), ("30m", 4), ("5m", 4), ("1m", 4)),
            # At NOW=10:02 only the 10:00 30m bucket is completed.  This test
            # exercises source routing, so require one causal strategic bar.
            minimum_bars_by_frequency=(("d", 2), ("30m", 1), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.completed_count == batch.discovered_count == 1
    assert batch.assessments[0].sector_id == "qmt-gics3:bank"
    assert batch.assessments[0].eligible is True
    assert gateway.members() == {"qmt-gics3:bank": ("SH.600000", "SZ.000001")}
    assert [call["frequency"] for call in frame_calls] == ["5m", "5m"]
    assert [call["request_bars"] for call in frame_calls] == [71, 4]
    assert all(call["members"] == ("SH.600000", "SZ.000001") for call in frame_calls)
    assert {frame.attrs["price_basis_provider"] for frame in analyzer.frames} == {
        "qmt-gics3-composite"
    }
    assert analyzer.frames[0].attrs["source_base_frequency"] == "5m"
    assert (
        analyzer.frames[0].attrs["sector_thirty_minute_derivation_contract"]
        == "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
    )
    assert str(batch.catalog_revision).startswith("sha256:")


def test_gateway_rejects_self_asserted_qmt_catalog_revision() -> None:
    gateway = NativeTradingDataGateway(
        exchange_provider=RecordingExchange,
        sector_provider=lambda: {
            "source": "qmt_gics3_components",
            "catalog_revision": "sha256:" + "0" * 64,
            "sectors": [
                {
                    "sector_id": "qmt-gics3:bank",
                    "name": "银行",
                    "source_key": "GICS3银行",
                    "member_codes": ["SH.600000"],
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="revision does not match its members"):
        gateway.native_sector_assessments(as_of=NOW)


def test_small_qmt_sector_is_counted_and_rejected_instead_of_silently_dropped() -> None:
    stock_exchange = RecordingExchange()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_frame_provider=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("undersized sector must not request a composite")
        ),
        sector_provider=lambda: {
            "source": "qmt_gics3_components",
            "sectors": [
                {
                    "sector_id": "qmt-gics3:small",
                    "name": "小板块",
                    "source_key": "GICS3小板块",
                    "member_codes": ["SZ.000001", "SH.600000"],
                }
            ],
        },
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 4), ("30m", 4), ("5m", 4), ("1m", 4)),
            minimum_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=3,
        ),
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.discovered_count == 1
    assert batch.completed_count == 0
    assert batch.failure_counts == ()
    assert batch.errors == ()
    assert batch.exclusion_counts == (("sector_member_coverage_insufficient", 1),)
    [exclusion] = batch.exclusions
    assert exclusion.detail_code == ("sector_constituent_count_below_minimum")
    assert exclusion.catalog_member_count == 2
    assert exclusion.universe_member_count == 2
    assert exclusion.required_member_count == 3
    assert exclusion.reason == ("catalog_members=2; universe_members=2; required=3")
    assert batch.resolution_ratio == Decimal("1")
    assert batch.assessments[0].hard_block is True
    assert batch.assessments[0].reason_codes == (
        "sector_member_coverage_insufficient",
        "sector_constituent_count_below_minimum",
    )


def test_qmt_sector_missing_catalog_members_remains_explicit() -> None:
    stock_exchange = RecordingExchange()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_frame_provider=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("undersized sector must not request a composite")
        ),
        sector_provider=lambda: {
            "source": "qmt_gics3_components",
            "sectors": [
                {
                    "sector_id": "qmt-gics3:coverage",
                    "name": "覆盖语义",
                    "source_key": "GICS3覆盖语义",
                    "member_codes": [],
                }
            ],
        },
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 4), ("30m", 4), ("5m", 4), ("1m", 4)),
            minimum_bars_by_frequency=(("d", 2), ("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=3,
        ),
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.errors == ()
    [exclusion] = batch.exclusions
    assert exclusion.reason_code == "sector_member_coverage_insufficient"
    assert exclusion.detail_code == "sector_catalog_members_missing"
    assert exclusion.catalog_member_count == 0
    assert exclusion.universe_member_count == 0
    assert exclusion.required_member_count == 3


def test_qmt_catalog_members_are_not_filtered_by_tick_derived_universe() -> None:
    stock_exchange = RecordingExchange()

    def sector_frame(**kwargs):
        return _qmt_sector_five_frame(
            request_bars=kwargs["request_bars"],
            member_count=3,
            as_of=kwargs["as_of"],
        )

    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_frame_provider=sector_frame,
        sector_provider=lambda: {
            "source": "qmt_gics3_components",
            "sectors": [
                {
                    "sector_id": "qmt-gics3:coverage",
                    "name": "覆盖语义",
                    "source_key": "GICS3覆盖语义",
                    "member_codes": [
                        "SZ.000001",
                        "SH.600000",
                        "SH.600001",
                    ],
                }
            ],
        },
        analyzer=RecordingAnalyzer(),
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("d", 4), ("30m", 4), ("5m", 4), ("1m", 4)),
            # Keep the fixture causal at 10:02; the 10:30 bucket is future.
            minimum_bars_by_frequency=(("d", 2), ("30m", 1), ("5m", 2), ("1m", 2)),
            minimum_sector_members=3,
        ),
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.discovered_count == batch.completed_count == 1
    assert batch.errors == ()
    assert gateway.members()["qmt-gics3:coverage"] == (
        "SH.600000",
        "SH.600001",
        "SZ.000001",
    )

    assert gateway.symbol_name("SH.600001") == "测试名称"
    assert gateway.symbol_name("SH.600001") == "测试名称"
    assert stock_exchange.info_calls == ["SH.600001"]


def test_native_gateway_maps_explicit_strict_error_to_structure_invalid() -> None:
    analyzer = FailingAnalyzer(
        gateway_module.StrictStructureAnalysisError("unit directions must alternate")
    )
    gateway, _analyzer, _exchange = _gateway(analyzer=analyzer)

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.failure_counts == (("sector_structure_invalid", 1),)
    assert batch.errors[0].error_type == "sector_structure_invalid"


def test_native_gateway_maps_unknown_analyzer_error_to_adapter_error() -> None:
    analyzer = FailingAnalyzer(RuntimeError("analyzer transport failed"))
    gateway, _analyzer, _exchange = _gateway(analyzer=analyzer)

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.failure_counts == (("sector_adapter_error", 1),)
    assert batch.errors[0].error_type == "sector_adapter_error"


def test_native_gateway_reuses_sector_analysis_when_closed_bar_is_unchanged() -> None:
    gateway, analyzer, _sector_exchange = _gateway()

    first = gateway.native_sector_assessments(as_of=NOW)
    second = gateway.native_sector_assessments(as_of=NOW)

    assert first == second
    assert analyzer.calls == [
        ("qmt-gics3:bank", "30m"),
        ("qmt-gics3:bank", "5m"),
    ]


def test_analysis_cache_invalidates_when_price_basis_revision_changes() -> None:
    gateway, analyzer, _sector_exchange = _gateway()
    stock_exchange = gateway._exchange_provider()

    gateway._load_analysis(
        exchange=stock_exchange,
        code="SZ.000001",
        analysis_code="SZ.000001",
        frequency="5m",
        as_of=NOW,
    )
    stock_exchange.frame.attrs["price_basis_revision"] = "test-adjusted"
    gateway._load_analysis(
        exchange=stock_exchange,
        code="SZ.000001",
        analysis_code="SZ.000001",
        frequency="5m",
        as_of=NOW,
    )

    assert analyzer.calls == [
        ("SZ.000001", "5m"),
        ("SZ.000001", "5m"),
    ]
    assert [frame.attrs["price_basis_revision"] for frame in analyzer.frames] == [
        "test-raw",
        "test-adjusted",
    ]


def test_analysis_cache_invalidates_when_sector_member_path_changes() -> None:
    gateway, analyzer, _sector_exchange = _gateway()
    first = _frame(with_metadata=False)
    first["member_mask"] = 1
    first.attrs.update(
        structure_price_quantum="0.000001",
        price_basis_revision="qmt-gics3-composite",
        price_basis_provider="qmt-gics3-composite",
        price_basis_adjustment=(gateway_module.QMT_GICS3_COMPOSITE_ADJUSTMENT),
        sector_factor_adjustment_contract_id=(
            gateway_module.QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
        ),
        sector_factor_revision="sha256:" + "a" * 64,
        sector_composite_member_path_revision="sha256:first-path",
    )
    second = first.copy(deep=True)
    second["member_mask"] = 2
    second.attrs = {
        **first.attrs,
        "sector_composite_member_path_revision": "sha256:second-path",
    }

    for frame in (first, second):
        gateway._load_analysis(
            exchange=None,
            code="qmt-gics3:bank",
            analysis_code="qmt-gics3:bank",
            frequency="5m",
            as_of=NOW,
            sector_source=gateway_module.QMT_GICS3_CATALOG_SOURCE,
            frame_override=frame,
        )

    assert analyzer.calls == [
        ("qmt-gics3:bank", "5m"),
        ("qmt-gics3:bank", "5m"),
    ]
    assert tuple(analyzer.frames[0]["member_mask"]) == (1, 1)
    assert tuple(analyzer.frames[1]["member_mask"]) == (2, 2)


def test_invalid_native_thirty_is_rebuilt_from_completed_same_source_five() -> None:
    exchange = InvalidNativeThirtyExchange()
    analyzer = RecordingAnalyzer()
    gateway, _unused, _sector_exchange = _gateway(analyzer=analyzer)
    gateway._exchange_provider = lambda: exchange

    analysis = gateway._load_analysis(
        exchange=exchange,
        code="SH.603869",
        analysis_code="SH.603869",
        frequency="30m",
        as_of=datetime.fromisoformat("2026-07-20T10:30:00+08:00"),
    )

    assert exchange.calls == [
        (
            "SH.603869",
            "30m",
            {"req_counts": 4, "dividend_type": "front_ratio"},
        ),
        (
            "SH.603869",
            "5m",
            {"req_counts": 24, "dividend_type": "front_ratio"},
        ),
    ]
    assert analysis.warmup_reason_codes == (
        "QMT_NATIVE_30M_INVALID_RESAMPLED_FROM_COMPLETED_5M",
    )
    assert len(analyzer.frames) == 1
    rebuilt = analyzer.frames[0]
    assert len(rebuilt) == 2
    assert bool((rebuilt["high"] >= rebuilt[["open", "close"]].max(axis=1)).all())
    assert bool((rebuilt["low"] <= rebuilt[["open", "close"]].min(axis=1)).all())
    assert rebuilt.attrs["price_basis_revision"] == "qmt-front-ratio-test"


def test_invalid_native_thirty_fails_closed_when_five_minute_evidence_is_invalid() -> (
    None
):
    exchange = InvalidNativeThirtyExchange(invalid_five=True)
    gateway, _analyzer, _sector_exchange = _gateway()

    with pytest.raises(
        ValueError,
        match="validated completed-5m fallback unavailable",
    ):
        gateway._load_analysis(
            exchange=exchange,
            code="SH.603869",
            analysis_code="SH.603869",
            frequency="30m",
            as_of=datetime.fromisoformat("2026-07-20T10:30:00+08:00"),
        )


def test_native_gateway_builds_four_physical_period_bundle_and_keeps_watch_scopes() -> (
    None
):
    gateway, _analyzer, _sector_exchange = _gateway()
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]

    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.as_of == datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    assert bundle.thirty_direction == "neutral"
    assert bundle.daily_direction == "neutral"
    assert bundle.physical_timeframe_recursive is True
    assert len(bundle.five_points) == 1
    assert bundle.five_points[0].status == "provisional"
    assert gateway.active_watchlist() == ("SZ.000001",)
    assert gateway.holdings() == ("SH.600000",)
    assert gateway.symbol_name("SZ.000001") == "测试名称"
    assert ("SZ.000001", "d") in _analyzer.calls


def test_native_gateway_monitor_scope_keeps_only_qmt_stock_and_etf() -> None:
    gateway, _analyzer, stock_exchange = _gateway()
    progress: list[str] = []
    gateway.set_progress_callback(lambda: progress.append("progress"))
    gateway._watchlist_provider = lambda: (
        {"code": "SH.000001", "name": "上证指数"},
        {"code": "SH.600000", "name": "浦发银行"},
        {"code": "SH.510300", "name": "沪深300ETF"},
    )

    assert gateway.tradable_instrument_codes(
        ("SH.000001", "SH.600000", "SH.510300")
    ) == ("SH.510300", "SH.600000")
    assert gateway.screening_instrument_types(
        ("SH.000001", "SH.600000", "SH.510300")
    ) == {
        "SH.000001": "index_cn",
        "SH.510300": "etf_cn",
        "SH.600000": "stock_cn",
    }
    assert gateway.active_watchlist_scope() == (
        ("SH.510300", "SH.600000"),
        ("SH.000001",),
    )
    assert gateway.active_watchlist() == ("SH.510300", "SH.600000")
    assert stock_exchange.type_calls == [
        ("SH.000001",),
        ("SH.510300",),
        ("SH.600000",),
    ]
    assert progress == ["progress"] * 6


def test_native_gateway_retries_unresolved_instrument_type() -> None:
    gateway, _analyzer, stock_exchange = _gateway()
    responses = iter(("unresolved_cn", "stock_cn"))

    def instrument_types(codes: tuple[str, ...]) -> dict[str, str]:
        stock_exchange.type_calls.append(codes)
        return {codes[0]: next(responses)}

    stock_exchange.screening_instrument_types = instrument_types  # type: ignore[method-assign]

    assert gateway.screening_instrument_types(("SH.600000",)) == {
        "SH.600000": "unresolved_cn"
    }
    assert gateway.screening_instrument_types(("SH.600000",)) == {
        "SH.600000": "stock_cn"
    }
    assert stock_exchange.type_calls == [("SH.600000",), ("SH.600000",)]


def test_native_gateway_tick_probe_skips_qmt_when_market_is_closed() -> None:
    gateway, _analyzer, _sector_exchange = _gateway()

    result = gateway.tick_probe("SH.000001")

    assert result == {
        "schema": "chanlun-native-tick-probe",
        "code": "SH.000001",
        "status": "market_closed",
        "market_open": False,
        "usable": False,
        "tick_data_used": False,
        "real_account_access": False,
        "real_order_transport": False,
    }


def test_native_gateway_one_minute_refresh_reuses_cached_higher_frames() -> None:
    gateway, _analyzer, _sector_exchange = _gateway()
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]
    gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)
    stock_exchange = gateway._exchange_provider()
    stock_exchange.calls.clear()

    gateway.structure_bundle(
        "SZ.000001",
        as_of=NOW,
        sector=sector,
        frequencies=("1m",),
    )

    assert stock_exchange.calls == [
        (
            "SZ.000001",
            "1m",
            {"req_counts": 4, "dividend_type": "front_ratio"},
        )
    ]


def test_native_gateway_does_not_leak_cached_one_minute_points_into_5m_lane() -> None:
    class TriggerAnalyzer(RecordingAnalyzer):
        def __call__(self, *, code, frequency, frame, as_of):
            base = super().__call__(
                code=code,
                frequency=frequency,
                frame=frame,
                as_of=as_of,
            )
            if frequency != "1m":
                return base
            return replace(
                base,
                confirmed_points=(confirmed_point("1buy", frequency="1m"),),
            )

    gateway, _analyzer, _sector_exchange = _gateway(analyzer=TriggerAnalyzer())
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]
    first = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)
    tactical = gateway.structure_bundle(
        "SZ.000001",
        as_of=NOW,
        sector=sector,
        frequencies=("5m",),
    )

    assert first.one_points
    assert tactical.one_points == ()
    assert tactical.entry_execution_boundaries == ()


def test_native_gateway_reuses_resolved_higher_timeframe_cutoff_evidence() -> None:
    calls: list[dict[str, object]] = []

    def evidence(subject: str, observed_at: datetime) -> HigherTimeframeGateEvidence:
        return HigherTimeframeGateEvidence(
            subject=subject,
            observed_at=observed_at,
            monthly="NONE",
            weekly="NONE",
            daily="NONE",
            gate="GREEN",
            grade="RESEARCH_ONLY",
            snapshot_id=f"snapshot:{subject}",
            source_revision=f"source:{subject}",
        )

    def provider(**kwargs):
        calls.append(kwargs)
        return HigherTimeframeGateBundle(
            market=evidence("MARKET", kwargs["as_of"]),
            sector=evidence(kwargs["sector_id"], kwargs["as_of"]),
            symbol=evidence(kwargs["symbol"], kwargs["as_of"]),
        )

    gateway, _analyzer, _sector_exchange = _gateway(higher_timeframe_provider=provider)
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]

    gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)
    gateway.structure_bundle(
        "SZ.000001",
        as_of=NOW,
        sector=sector,
        frequencies=("5m", "1m"),
    )

    assert len(calls) == 1


def test_native_gateway_skips_stock_one_minute_analysis_without_current_setup() -> None:
    gateway, analyzer, _sector_exchange = _gateway()
    original = gateway._analyzer

    def without_setup(*, code, frequency, frame, as_of):
        result = original(code=code, frequency=frequency, frame=frame, as_of=as_of)
        return FrameStructureAnalysis(
            closed_at=result.closed_at,
            direction=result.direction,
            confirmed_points=(),
            provisional_points=(),
        )

    gateway._analyzer = without_setup
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]
    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.one_points == ()
    assert ("SZ.000001", "1m") not in analyzer.calls
    assert [
        frequency
        for analysis_code, frequency in analyzer.calls
        if analysis_code == "SZ.000001"
    ] == ["5m"]


def test_native_gateway_rejects_synthetic_sector_catalog() -> None:
    gateway, _analyzer, _sector_exchange = _gateway()
    gateway._sector_provider = lambda: {"source": "synthetic", "sectors": []}

    with pytest.raises(ValueError, match="QMT GICS3"):
        gateway.native_sector_assessments(as_of=NOW)
