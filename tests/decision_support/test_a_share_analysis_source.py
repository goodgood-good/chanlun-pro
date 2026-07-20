from __future__ import annotations

import ast
from datetime import date, datetime, time, timedelta
import inspect
import math
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import chanlun.decision_support.a_share_analysis_source as analysis_source_module
import chanlun.decision_support.a_share_analysis_state as analysis_state_module
from chanlun.decision_support.a_share_analysis_source import (
    AResearchSourceConfig,
    build_a_share_selector_universe_resolver,
    build_production_a_share_analysis_source,
)
from chanlun.recursive_bt.select.chanlun_selector import ASelectionConfig
from chanlun.recursive_bt.engine.engine import Signal


CN = ZoneInfo("Asia/Shanghai")


class _QuoteBackendWithForbiddenTradingMethods:
    kline_time_label = "start"

    def klines(self, code, frequency, **kwargs):
        raise AssertionError("capability-surface test does not fetch bars")

    def stock_info(self, code):
        return {"code": code, "name": "浦发银行"}

    def order(self, *args, **kwargs):
        raise AssertionError("order must be unreachable")

    def positions(self):
        raise AssertionError("positions must be unreachable")

    def balance(self):
        raise AssertionError("balance must be unreachable")


def test_source_exposes_only_research_read_capabilities():
    backend = _QuoteBackendWithForbiddenTradingMethods()
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    assert source.market == "a"
    assert source.resolve_universe()[0] == ("SH.600000",)
    state = source.create_state("SH.600000")
    for forbidden in (
        "run_once",
        "ledger",
        "notifier",
        "broker",
        "order",
        "positions",
        "balance",
    ):
        assert not hasattr(source, forbidden)
        assert not hasattr(state, forbidden)


def test_source_requires_explicit_legal_kline_time_label():
    class _BackendWithoutTimeLabel:
        def klines(self, code, frequency, **kwargs):
            raise AssertionError("source construction must not fetch bars")

        def stock_info(self, code):
            return {"code": code, "name": "Pinned"}

    backend = _BackendWithoutTimeLabel()
    with pytest.raises(ValueError, match="kline_time_label"):
        build_production_a_share_analysis_source(
            exchange=backend,
            universe_resolver=lambda: (("SH.600000",), {"SH.600000": "Pinned"}, ()),
        )

    backend.kline_time_label = "close"
    with pytest.raises(ValueError, match="kline_time_label"):
        build_production_a_share_analysis_source(
            exchange=backend,
            universe_resolver=lambda: (("SH.600000",), {"SH.600000": "Pinned"}, ()),
        )


def test_source_and_state_import_no_legacy_monitor_or_writer():
    forbidden = ("live_monitor", "paperbroker", "notifier", "optimizer", "ledger")
    for path in (
        Path("src/chanlun/decision_support/a_share_analysis_source.py"),
        Path("src/chanlun/decision_support/a_share_analysis_state.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.casefold() for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append((node.module or "").casefold())
        assert all(
            fragment not in module
            for module in imported
            for fragment in forbidden
        )


def test_source_config_rejects_every_non_exact_read_only_value():
    config = AResearchSourceConfig()
    assert config.op_level == "5m"
    assert config.big_level == "30m"
    assert not hasattr(config, "ledger")
    assert config.paper_enabled is False
    assert config.dingtalk_webhook == ""
    assert config.dry_run is True
    assert config.optimization_report_enabled is False
    assert config.runtime_overrides_enabled is False
    assert config.warmup_new_symbols is False

    invalid_overrides = (
        {"op_level": "1m"},
        {"big_level": "5m"},
        {"paper_enabled": True},
        {"paper_enabled": 0},
        {"dingtalk_webhook": "https://example.invalid/hook"},
        {"dry_run": False},
        {"optimization_report_enabled": True},
        {"runtime_overrides_enabled": True},
        {"warmup_new_symbols": True},
    )
    for override in invalid_overrides:
        with pytest.raises(ValueError, match="read-only research source"):
            AResearchSourceConfig(**override)
    with pytest.raises(TypeError, match="unexpected keyword argument.*ledger"):
        AResearchSourceConfig(ledger=object())


def test_production_source_factory_has_exact_config_default():
    parameter = inspect.signature(
        build_production_a_share_analysis_source
    ).parameters["config"]

    assert type(parameter.default) is AResearchSourceConfig
    assert parameter.default == AResearchSourceConfig()


def test_production_source_factory_rejects_explicit_none_config():
    with pytest.raises(TypeError, match="config must be AResearchSourceConfig"):
        build_production_a_share_analysis_source(
            exchange=_QuoteBackendWithForbiddenTradingMethods(),
            universe_resolver=lambda: (
                ("SH.600000",),
                {"SH.600000": "浦发银行"},
                (),
            ),
            config=None,
        )


def test_selector_universe_resolver_returns_exact_source_contract(monkeypatch):
    candidates = (
        SimpleNamespace(code="SH.600000", name="浦发银行"),
        SimpleNamespace(code="SZ.000001", name=None),
        SimpleNamespace(code="SH.600000", name="浦发银行"),
    )

    selectors = []

    class _Selector:
        def __init__(self, selection_config):
            self.selection_config = selection_config
            self.calls = 0
            selectors.append(self)

        def select(self):
            self.calls += 1
            return list(candidates)

    monkeypatch.setattr(
        analysis_source_module,
        "OriginalChanlunASelector",
        _Selector,
        raising=False,
    )
    selection_config = ASelectionConfig(bt_data="D:/research/bt_data")
    resolver = build_a_share_selector_universe_resolver(selection_config)

    codes, names, resolved_candidates = resolver()

    assert codes == ("SH.600000", "SZ.000001")
    assert names == {"SH.600000": "浦发银行"}
    assert resolved_candidates == candidates
    assert len(selectors) == 1
    assert selectors[0].selection_config is selection_config
    assert selectors[0].calls == 1


class _NameQuoteBackend:
    kline_time_label = "end"

    def __init__(self, names):
        self.names = dict(names)
        self.stock_info_calls = []
        self.kline_calls = []

    def klines(self, code, frequency, **kwargs):
        self.kline_calls.append((code, frequency, kwargs))
        raise AssertionError("resolving names must not construct or refresh state")

    def stock_info(self, code):
        self.stock_info_calls.append(code)
        return {"code": code, "name": self.names.get(code)}


def test_required_code_missing_name_uses_narrow_stock_info_once(monkeypatch):
    backend = _NameQuoteBackend({"SZ.000001": "平安银行"})
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )

    first = source.resolve_universe(("SZ.000001",))
    second = source.resolve_universe(("SZ.000001",))

    assert first[0] == ("SH.600000", "SZ.000001")
    assert second[0] == first[0]
    assert dict(first[1]) == {
        "SH.600000": "浦发银行",
        "SZ.000001": "平安银行",
    }
    assert backend.stock_info_calls == ["SZ.000001", "SZ.000001"]
    assert backend.kline_calls == []

    state_construction_calls = []

    def forbid_state_construction(code, quote):
        state_construction_calls.append((code, quote))
        raise AssertionError("name resolution must not construct state")

    monkeypatch.setattr(
        analysis_source_module,
        "AResearchSymbolState",
        forbid_state_construction,
    )
    missing_backend = _NameQuoteBackend({"SZ.000001": ""})
    missing_source = build_production_a_share_analysis_source(
        exchange=missing_backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    with pytest.raises(RuntimeError, match="missing security name for SZ.000001"):
        missing_source.resolve_universe(("SZ.000001",))
    assert missing_backend.stock_info_calls == ["SZ.000001"]
    assert missing_backend.kline_calls == []
    assert state_construction_calls == []


class _RecordingCL:
    def __init__(self, code, frequency, config=None):
        self.code = code
        self.frequency = frequency
        self.config = dict(config or {})
        self.frames = []

    def process_klines(self, frame):
        self.frames.append(frame.copy())

    def get_bis(self):
        return ()


def _minute_endpoints(day: date, minutes: int) -> tuple[datetime, ...]:
    endpoints = []
    for opened, closed in ((time(9, 30), time(11, 30)), (time(13), time(15))):
        cursor = datetime.combine(day, opened, tzinfo=CN) + timedelta(minutes=minutes)
        session_close = datetime.combine(day, closed, tzinfo=CN)
        while cursor <= session_close:
            endpoints.append(cursor)
            cursor += timedelta(minutes=minutes)
    return tuple(endpoints)


def _minute_frame(labels) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": tuple(labels),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0,
            "volume": 100_000.0,
        }
    )


class _FrameQuoteBackend:
    def __init__(self, *, kline_time_label: str):
        self.kline_time_label = kline_time_label
        self.calls = []
        frames = {}
        for frequency, minutes in (("5m", 5), ("30m", 30)):
            closes = _minute_endpoints(date(2026, 7, 13), minutes) + _minute_endpoints(
                date(2026, 7, 14),
                minutes,
            )
            labels = (
                closes
                if kline_time_label == "end"
                else tuple(value - timedelta(minutes=minutes) for value in closes)
            )
            frames[frequency] = _minute_frame(labels)
        frames["d"] = _minute_frame(())
        self.frames = frames

    def klines(self, code, frequency, **kwargs):
        self.calls.append((code, frequency, dict(kwargs)))
        return self.frames[frequency].copy()

    def stock_info(self, code):
        return {"code": code, "name": "浦发银行"}


def _closed_at_from_recording(cd, *, minutes: int, time_label: str) -> datetime:
    assert cd.frames
    label = cd.frames[-1]["date"].iloc[-1].to_pydatetime()
    return label if time_label == "end" else label + timedelta(minutes=minutes)


def _forbid_ambient_now(monkeypatch):
    class _NoAmbientDatetime(datetime):
        @classmethod
        def now(cls, *args, **kwargs):
            raise AssertionError("ambient datetime.now is forbidden")

    class _NoAmbientTimestamp(pd.Timestamp):
        @classmethod
        def now(cls, *args, **kwargs):
            raise AssertionError("ambient Timestamp.now is forbidden")

    monkeypatch.setattr(
        analysis_state_module,
        "datetime",
        _NoAmbientDatetime,
        raising=False,
    )
    state_pandas = getattr(analysis_state_module, "pd", None)
    if state_pandas is not None:
        monkeypatch.setattr(state_pandas, "Timestamp", _NoAmbientTimestamp)


@pytest.mark.parametrize("time_label", ("end", "start"))
@pytest.mark.parametrize(
    ("cutoff", "expected_5m", "expected_30m"),
    (
        ("2026-07-14 09:34:59", "2026-07-13 15:00", "2026-07-13 15:00"),
        ("2026-07-14 09:35:00", "2026-07-14 09:35", "2026-07-13 15:00"),
        ("2026-07-14 09:55:00", "2026-07-14 09:55", "2026-07-13 15:00"),
        ("2026-07-14 10:00:00", "2026-07-14 10:00", "2026-07-14 10:00"),
        ("2026-07-14 10:30:00", "2026-07-14 10:30", "2026-07-14 10:30"),
        ("2026-07-14 11:00:00", "2026-07-14 11:00", "2026-07-14 11:00"),
        ("2026-07-14 11:29:59", "2026-07-14 11:25", "2026-07-14 11:00"),
        ("2026-07-14 11:30:00", "2026-07-14 11:30", "2026-07-14 11:30"),
        ("2026-07-14 13:04:59", "2026-07-14 11:30", "2026-07-14 11:30"),
        ("2026-07-14 13:05:00", "2026-07-14 13:05", "2026-07-14 11:30"),
        ("2026-07-14 13:25:00", "2026-07-14 13:25", "2026-07-14 11:30"),
        ("2026-07-14 13:30:00", "2026-07-14 13:30", "2026-07-14 13:30"),
        ("2026-07-14 14:00:00", "2026-07-14 14:00", "2026-07-14 14:00"),
        ("2026-07-14 14:30:00", "2026-07-14 14:30", "2026-07-14 14:30"),
        ("2026-07-14 14:59:59", "2026-07-14 14:55", "2026-07-14 14:30"),
        ("2026-07-14 15:00:00", "2026-07-14 15:00", "2026-07-14 15:00"),
    ),
)
def test_qmt_endpoint_closure_is_exact_at_5m_and_30m_session_boundaries(
    monkeypatch,
    time_label,
    cutoff,
    expected_5m,
    expected_30m,
):
    monkeypatch.setattr(analysis_state_module, "CL", _RecordingCL, raising=False)
    monkeypatch.setattr(
        analysis_state_module,
        "collect_branch_signals",
        lambda *args, **kwargs: (),
        raising=False,
    )
    _forbid_ambient_now(monkeypatch)
    backend = _FrameQuoteBackend(kline_time_label=time_label)
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    state = source.create_state("SH.600000")

    state.refresh_at(datetime.fromisoformat(cutoff).replace(tzinfo=CN))

    assert state.op_level == "5m"
    assert state.big_level == "30m"
    assert state.mid_level == ""
    assert state.cd_mid is None
    assert state.cdd.frequency == "d"
    assert state.prev_close == pytest.approx(10.0)
    assert state.mid_dir() == ""
    assert state.big_dir() == "neutral"
    assert _closed_at_from_recording(
        state.cd_op,
        minutes=5,
        time_label=time_label,
    ) == datetime.fromisoformat(expected_5m).replace(tzinfo=CN)
    assert _closed_at_from_recording(
        state.cd_big,
        minutes=30,
        time_label=time_label,
    ) == datetime.fromisoformat(expected_30m).replace(tzinfo=CN)
    assert {frequency for _, frequency, _ in backend.calls} == {"5m", "30m", "d"}


@pytest.mark.parametrize(
    ("time_label", "invalid_times"),
    (
        (
            "end",
            {
                "5m": ("09:30", "09:36", "11:35", "12:00", "13:00", "13:01"),
                "30m": ("09:30", "10:01", "12:00", "13:00", "13:31"),
            },
        ),
        (
            "start",
            {
                "5m": ("09:25", "09:31", "11:30", "12:00", "12:55", "13:01"),
                "30m": ("09:00", "09:31", "11:30", "12:00", "12:30", "13:01"),
            },
        ),
    ),
)
def test_analysis_state_never_feeds_illegal_session_or_grid_rows(
    monkeypatch,
    time_label,
    invalid_times,
):
    monkeypatch.setattr(analysis_state_module, "CL", _RecordingCL, raising=False)
    monkeypatch.setattr(
        analysis_state_module,
        "collect_branch_signals",
        lambda *args, **kwargs: (),
        raising=False,
    )
    _forbid_ambient_now(monkeypatch)
    backend = _FrameQuoteBackend(kline_time_label=time_label)
    for frequency, labels in invalid_times.items():
        backend.frames[frequency] = _minute_frame(
            tuple(
                datetime.combine(
                    date(2026, 7, 14),
                    time.fromisoformat(label),
                    tzinfo=CN,
                )
                for label in labels
            )
        )
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    state = source.create_state("SH.600000")

    state.refresh_at(datetime(2026, 7, 14, 15, 0, tzinfo=CN))

    assert state.cd_op.frames == []
    assert state.cd_big.frames == []
    assert state.last5 is None
    assert state.last30 is None
    assert {frequency for _, frequency, _ in backend.calls} == {"5m", "30m", "d"}


def test_analysis_state_never_feeds_future_or_current_daily_rows(monkeypatch):
    monkeypatch.setattr(analysis_state_module, "CL", _RecordingCL, raising=False)
    monkeypatch.setattr(
        analysis_state_module,
        "collect_branch_signals",
        lambda *args, **kwargs: (),
        raising=False,
    )
    _forbid_ambient_now(monkeypatch)
    backend = _FrameQuoteBackend(kline_time_label="end")
    previous_daily = datetime(2026, 7, 13, 15, 0, tzinfo=CN)
    current_daily = datetime(2026, 7, 14, 15, 0, tzinfo=CN)
    backend.frames["d"] = _minute_frame((previous_daily, current_daily))
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    state = source.create_state("SH.600000")

    state.refresh_at(datetime(2026, 7, 14, 15, 0, tzinfo=CN))

    assert len(state.cdd.frames) == 1
    fed_dates = tuple(state.cdd.frames[0]["date"])
    assert fed_dates == (previous_daily,)
    assert state.lastd.to_pydatetime() == previous_daily


def test_analysis_state_feeds_only_unseen_rows_and_returns_new_branch_signals(
    monkeypatch,
):
    monkeypatch.setattr(analysis_state_module, "CL", _RecordingCL)
    signals = (
        Signal(
            datetime(2026, 7, 14, 10, 35, tzinfo=CN),
            1,
            "3buy",
            10.0,
        ),
        Signal(
            datetime(2026, 7, 14, 10, 35, tzinfo=CN),
            2,
            "3buy",
            10.0,
        ),
    )
    operation_collections = []

    def collect(cd, *args, **kwargs):
        if cd.frequency != "5m":
            return ()
        operation_collections.append(len(cd.frames))
        return () if len(operation_collections) == 1 else signals

    monkeypatch.setattr(analysis_state_module, "collect_branch_signals", collect)
    _forbid_ambient_now(monkeypatch)
    backend = _FrameQuoteBackend(kline_time_label="end")
    source = build_production_a_share_analysis_source(
        exchange=backend,
        universe_resolver=lambda: (
            ("SH.600000",),
            {"SH.600000": "浦发银行"},
            (),
        ),
    )
    state = source.create_state("SH.600000")

    first = state.refresh_at(datetime(2026, 7, 14, 10, 35, tzinfo=CN))
    second = state.refresh_at(datetime(2026, 7, 14, 10, 40, tzinfo=CN))
    third = state.refresh_at(datetime(2026, 7, 14, 10, 40, tzinfo=CN))

    assert first == []
    assert second == list(signals)
    assert third == []
    assert len(state.cd_op.frames) == 2
    assert len(state.cd_op.frames[1]) == 1
    assert state.cd_op.frames[1]["date"].iloc[0].to_pydatetime() == datetime(
        2026,
        7,
        14,
        10,
        40,
        tzinfo=CN,
    )
    assert state.prev_close > 0
    assert math.isfinite(state.prev_close)
