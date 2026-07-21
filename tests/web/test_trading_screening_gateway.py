from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest

from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config
from tests.trading_system.helpers import provisional_point
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
)


NOW = datetime.fromisoformat("2026-07-20T10:02:00+08:00")


def test_analyzer_never_reads_legacy_structure_methods(monkeypatch) -> None:
    closed_at = datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    confirmed = strict_point("1buy", available_at=closed_at)
    approaching = strict_point(
        "2buy",
        status=StrictPointStatus.APPROACHING,
        available_at=closed_at,
    )
    state = StrictOnlyCL(
        strict_evidence_result(
            code="SZ.000001",
            source_frequency="5m",
            source_closed_at=closed_at,
            confirmed_points=(confirmed,),
            approaching_points=(approaching,),
        )
    )
    monkeypatch.setattr(gateway_module, "CL", lambda *_args, **_kwargs: state)

    analysis = analyze_native_frame(
        code="SZ.000001",
        frequency="5m",
        frame=_frame(),
        as_of=closed_at,
    )

    assert state.evidence_calls == 1
    assert state.process_calls == 1
    assert analysis.direction == "neutral"
    assert tuple(point.point_type for point in analysis.confirmed_points) == (
        "1buy",
    )
    assert tuple(point.point_type for point in analysis.provisional_points) == (
        "2buy",
    )


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

    monkeypatch.setattr(gateway_module, "CL", factory)

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
            price_basis_revision="test-raw-v1",
        ),
        "market": "a",
    }


def test_analyzer_rejects_snapshot_without_price_basis_metadata(monkeypatch) -> None:
    frame = _frame()
    frame.attrs.clear()
    monkeypatch.setattr(
        gateway_module,
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
        frame.attrs["price_basis_revision"] = "test-raw-v1"
    return frame


class RecordingExchange:
    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.frame = _frame() if frame is None else frame
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def klines(self, code: str, frequency: str, *, args: dict[str, object]):
        assert args["req_counts"] == 4
        self.calls.append((code, frequency, dict(args)))
        return self.frame.copy(deep=True)


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


def _gateway(
    *,
    sector_frame: pd.DataFrame | None = None,
    analyzer: RecordingAnalyzer | None = None,
    sector_code: str = "SH.880471",
) -> tuple[NativeTradingDataGateway, RecordingAnalyzer, RecordingExchange]:
    stock_exchange = RecordingExchange()
    sector_exchange = RecordingExchange(sector_frame)
    analyzer = analyzer if analyzer is not None else RecordingAnalyzer()
    gateway = NativeTradingDataGateway(
        exchange_provider=lambda: stock_exchange,
        sector_exchange_provider=lambda: sector_exchange,
        universe_provider=lambda _exchange: (
            {"type": "stock_cn", "code": "SZ.000001", "name": "平安银行"},
            {"type": "stock_cn", "code": "SH.600000", "name": "浦发银行"},
        ),
        sector_provider=lambda: {
            "source": "tdx_880_industry_index",
            "sectors": [
                {
                    "sector_id": f"tdx-industry:{sector_code}",
                    "name": "银行",
                    "kline_code": sector_code,
                    "member_codes": ["000001", "600000"],
                }
            ],
        },
        watchlist_provider=lambda: ({"code": "SZ.000001"},),
        holdings_provider=lambda: ("SH.600000",),
        analyzer=analyzer,
        config=NativeTradingGatewayConfig(
            request_bars_by_frequency=(("30m", 4), ("5m", 4), ("1m", 4)),
            minimum_bars_by_frequency=(("30m", 2), ("5m", 2), ("1m", 2)),
            minimum_sector_members=1,
        ),
    )
    return gateway, analyzer, sector_exchange


def test_native_gateway_ranks_real_sector_bars_and_emits_only_changed_keys() -> None:
    gateway, analyzer, _sector_exchange = _gateway()

    batch = gateway.native_sector_assessments(as_of=NOW)
    assessments = batch.assessments

    assert len(assessments) == 1
    assert batch.completed_count == batch.discovered_count == 1
    assert assessments[0].sector_id == "tdx-industry:SH.880471"
    assert assessments[0].eligible is True
    assert gateway.members() == {
        "tdx-industry:SH.880471": ("SH.600000", "SZ.000001")
    }
    first = gateway.changed_bars(None)
    second = gateway.changed_bars(NOW)
    assert {item.frequency for item in first} == {"5m", "30m"}
    assert {item.code for item in first} == {"tdx-industry:SH.880471"}
    assert second == ()
    assert analyzer.calls[:2] == [
        ("tdx-industry:SH.880471", "30m"),
        ("tdx-industry:SH.880471", "5m"),
    ]


def test_native_sector_loader_forces_none_and_attaches_metadata_before_analysis() -> None:
    analyzer = RecordingAnalyzer()
    gateway, analyzer, exchange = _gateway(
        sector_frame=_frame(with_metadata=False),
        analyzer=analyzer,
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert exchange.calls
    assert {call[2]["fq"] for call in exchange.calls} == {"none"}
    assert analyzer.frames[0].attrs["price_basis_provider"] == (
        "tdx-industry-index"
    )
    assert analyzer.frames[0].attrs["price_basis_adjustment"] == "none"
    assert analyzer.frames[0].attrs["structure_price_quantum"] == "0.01"
    assert batch.completed_count == batch.discovered_count == 1


def test_unknown_sector_code_fails_closed_before_strict_analysis() -> None:
    gateway, analyzer, _exchange = _gateway(
        sector_frame=_frame(with_metadata=False),
        sector_code="SZ.880471",
    )

    batch = gateway.native_sector_assessments(as_of=NOW)

    assert batch.discovered_count == 1
    assert batch.completed_count == 0
    assert batch.failure_counts == (
        ("sector_price_basis_unavailable", 1),
    )
    assert analyzer.calls == []


def test_native_gateway_reuses_sector_analysis_when_closed_bar_is_unchanged() -> None:
    gateway, analyzer, _sector_exchange = _gateway()

    first = gateway.native_sector_assessments(as_of=NOW)
    second = gateway.native_sector_assessments(as_of=NOW)

    assert first == second
    assert analyzer.calls == [
        ("tdx-industry:SH.880471", "30m"),
        ("tdx-industry:SH.880471", "5m"),
    ]


def test_native_gateway_builds_three_level_bundle_and_keeps_watch_scopes() -> None:
    gateway, _analyzer, _sector_exchange = _gateway()
    sector = gateway.native_sector_assessments(as_of=NOW).assessments[0]

    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.as_of == datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    assert bundle.thirty_direction == "neutral"
    assert len(bundle.five_points) == 1
    assert bundle.five_points[0].status == "provisional"
    assert gateway.active_watchlist() == ("SZ.000001",)
    assert gateway.holdings() == ("SH.600000",)
    assert gateway.symbol_name("SZ.000001") == "平安银行"


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
        ("SZ.000001", "1m", {"req_counts": 4})
    ]


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


def test_native_gateway_rejects_synthetic_sector_catalog() -> None:
    gateway, _analyzer, _sector_exchange = _gateway()
    gateway._sector_provider = lambda: {"source": "synthetic", "sectors": []}

    with pytest.raises(ValueError, match="native TDX"):
        gateway.native_sector_assessments(as_of=NOW)
