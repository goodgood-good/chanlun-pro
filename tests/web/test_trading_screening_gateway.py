from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from tests.trading_system.helpers import provisional_point
from cl_app.services.trading_screening_gateway import (
    FrameStructureAnalysis,
    NativeTradingDataGateway,
    NativeTradingGatewayConfig,
)


NOW = datetime.fromisoformat("2026-07-20T10:02:00+08:00")


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
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


class RecordingExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def klines(self, code: str, frequency: str, *, args: dict[str, object]):
        assert args["req_counts"] == 4
        self.calls.append((code, frequency))
        return _frame()


class RecordingAnalyzer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, code, frequency, frame, as_of):
        self.calls.append((code, frequency))
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


def _gateway() -> tuple[NativeTradingDataGateway, RecordingAnalyzer]:
    stock_exchange = RecordingExchange()
    sector_exchange = RecordingExchange()
    analyzer = RecordingAnalyzer()
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
                    "sector_id": "tdx-industry:SH.880471",
                    "name": "银行",
                    "kline_code": "SH.880471",
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
    return gateway, analyzer


def test_native_gateway_ranks_real_sector_bars_and_emits_only_changed_keys() -> None:
    gateway, analyzer = _gateway()

    assessments = gateway.native_sector_assessments(as_of=NOW)

    assert len(assessments) == 1
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


def test_native_gateway_reuses_sector_analysis_when_closed_bar_is_unchanged() -> None:
    gateway, analyzer = _gateway()

    first = gateway.native_sector_assessments(as_of=NOW)
    second = gateway.native_sector_assessments(as_of=NOW)

    assert first == second
    assert analyzer.calls == [
        ("tdx-industry:SH.880471", "30m"),
        ("tdx-industry:SH.880471", "5m"),
    ]


def test_native_gateway_builds_three_level_bundle_and_keeps_watch_scopes() -> None:
    gateway, _analyzer = _gateway()
    sector = gateway.native_sector_assessments(as_of=NOW)[0]

    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.as_of == datetime.fromisoformat("2026-07-20T10:01:00+08:00")
    assert bundle.thirty_direction == "neutral"
    assert len(bundle.five_points) == 1
    assert bundle.five_points[0].status == "provisional"
    assert gateway.active_watchlist() == ("SZ.000001",)
    assert gateway.holdings() == ("SH.600000",)
    assert gateway.symbol_name("SZ.000001") == "平安银行"


def test_native_gateway_one_minute_refresh_reuses_cached_higher_frames() -> None:
    gateway, _analyzer = _gateway()
    sector = gateway.native_sector_assessments(as_of=NOW)[0]
    gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)
    stock_exchange = gateway._exchange_provider()
    stock_exchange.calls.clear()

    gateway.structure_bundle(
        "SZ.000001",
        as_of=NOW,
        sector=sector,
        frequencies=("1m",),
    )

    assert stock_exchange.calls == [("SZ.000001", "1m")]


def test_native_gateway_skips_stock_one_minute_analysis_without_current_setup() -> None:
    gateway, analyzer = _gateway()
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
    sector = gateway.native_sector_assessments(as_of=NOW)[0]
    bundle = gateway.structure_bundle("SZ.000001", as_of=NOW, sector=sector)

    assert bundle.one_points == ()
    assert ("SZ.000001", "1m") not in analyzer.calls


def test_native_gateway_rejects_synthetic_sector_catalog() -> None:
    gateway, _analyzer = _gateway()
    gateway._sector_provider = lambda: {"source": "synthetic", "sectors": []}

    with pytest.raises(ValueError, match="native TDX"):
        gateway.native_sector_assessments(as_of=NOW)
