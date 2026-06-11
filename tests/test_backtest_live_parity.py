import datetime
import json
import pickle

import pytest
import numpy as np
import pandas as pd

from chanlun.backtesting.backtest_trader import BackTestTrader
from chanlun.recursive_bt.engine import Signal


class _State:
    def __init__(self, last_open, last_px, prev_close):
        self.last_open = last_open
        self.last_px = last_px
        self.prev_close = prev_close


def test_get_opt_close_uids_supports_direction_mapping():
    trader = BackTestTrader("test")

    assert trader.get_opt_close_uids(
        "SHFE.RB", "1buy", {"buy": ["risk"], "sell": ["target"]}
    ) == ["risk"]
    assert trader.get_opt_close_uids(
        "SHFE.RB", "1sell", {"buy": ["risk"], "sell": ["target"]}
    ) == ["target"]
    assert trader.get_opt_close_uids(
        "SHFE.RB", "1buy", {"SHFE.RB": {"buy": ["rb-risk"]}}
    ) == ["rb-risk"]


def test_paper_broker_fills_pending_at_latest_open(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"))
    broker.pending.append({"code": "SZ.000001", "act": "buy", "bs": "3"})
    broker.fill_pending(
        {"SZ.000001": _State(last_open=10.0, last_px=99.0, prev_close=9.9)},
        "2026-06-10 09:35:00",
    )

    pos = broker.positions["SZ.000001"]
    assert pos["entry_px"] == 10.0
    assert broker.pending == []


def test_paper_broker_uses_target_weight_for_buy_budget(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"))
    broker.pending.append({
        "code": "SZ.000001",
        "act": "buy",
        "bs": "1",
        "target_weight": 0.05,
    })
    broker.fill_pending(
        {"SZ.000001": _State(last_open=10.0, last_px=10.0, prev_close=9.9)},
        "2026-06-10 09:35:00",
    )

    pos = broker.positions["SZ.000001"]
    assert pos["shares"] == 4900


def test_paper_broker_uses_us_t0_and_lot_one_rules(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"), market="us")
    broker.pending.append({
        "code": "AAPL.US",
        "act": "buy",
        "bs": "3",
        "target_weight": 0.5,
    })
    broker.fill_pending(
        {"AAPL.US": _State(last_open=100.0, last_px=100.0, prev_close=99.0)},
        "2026-06-10 09:35:00",
    )

    pos = broker.positions["AAPL.US"]
    assert pos["shares"] > 4900
    assert pos["shares"] % 100 != 0

    broker.pending.append({"code": "AAPL.US", "act": "sell", "reason": "same-day"})
    broker.fill_pending(
        {"AAPL.US": _State(last_open=101.0, last_px=101.0, prev_close=100.0)},
        "2026-06-10 14:55:00",
    )

    assert broker.positions == {}
    assert broker.trades[-1]["reason"] == "same-day"


def test_paper_broker_respects_a_share_t1_and_bj_limit(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"), market="a")
    broker.pending.append({
        "code": "BJ.920001",
        "act": "buy",
        "bs": "3",
        "target_weight": 0.1,
    })
    broker.fill_pending(
        {"BJ.920001": _State(last_open=12.0, last_px=12.0, prev_close=10.0)},
        "2026-06-10 09:35:00",
    )

    assert "BJ.920001" in broker.positions
    assert broker.positions["BJ.920001"]["shares"] % 100 == 0

    broker.pending.append({"code": "BJ.920001", "act": "sell", "reason": "same-day"})
    broker.fill_pending(
        {"BJ.920001": _State(last_open=12.1, last_px=12.1, prev_close=12.0)},
        "2026-06-10 14:55:00",
    )
    assert any(order["act"] == "sell" for order in broker.pending)

    broker.pending = [{
        "code": "BJ.920002",
        "act": "buy",
        "bs": "3",
        "target_weight": 0.1,
    }]
    broker.fill_pending(
        {"BJ.920002": _State(last_open=13.0, last_px=13.0, prev_close=10.0)},
        "2026-06-11 09:35:00",
    )

    assert "BJ.920002" not in broker.positions
    assert broker.pending == []


def test_paper_broker_queues_monitor_events_with_ratios(tmp_path):
    from types import SimpleNamespace

    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"), market="us")
    broker.positions["MSFT.US"] = {
        "shares": 100.0,
        "entry_px": 100.0,
        "entry_date": "2026-06-09 10:00:00",
        "bs": "3buy",
    }

    queued = broker.queue_events(
        [
            SimpleNamespace(
                code="AAPL.US",
                side="buy",
                bs_type="3buy",
                buy_ratio=0.0625,
                reason="1m 3buy | 5m soft_down_discount",
                identity="buy-1",
            ),
            SimpleNamespace(
                code="MSFT.US",
                side="sell",
                sell_ratio=0.5,
                reason="1m sell",
                identity="sell-1",
            ),
            SimpleNamespace(
                code="TSLA.US",
                side="buy",
                bs_type="1buy",
                buy_ratio=0.0,
                reason="zero ratio",
                identity="skip",
            ),
        ]
    )

    assert queued == 2
    assert broker.pending[0] == {
        "code": "AAPL.US",
        "act": "buy",
        "bs": "3buy",
        "target_weight": pytest.approx(0.0625),
        "reason": "1m 3buy | 5m soft_down_discount",
        "event_id": "buy-1",
    }
    assert broker.pending[1] == {
        "code": "MSFT.US",
        "act": "sell",
        "sell_ratio": pytest.approx(0.5),
        "reason": "1m sell",
        "event_id": "sell-1",
    }


def test_paper_broker_queue_events_respects_max_pos(tmp_path):
    from types import SimpleNamespace

    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"), market="us", max_pos=1)

    queued = broker.queue_events(
        [
            SimpleNamespace(
                code="AAPL.US",
                side="buy",
                bs_type="3buy",
                buy_ratio=0.5,
                reason="first",
                identity="buy-1",
            ),
            SimpleNamespace(
                code="MSFT.US",
                side="buy",
                bs_type="3buy",
                buy_ratio=0.5,
                reason="second",
                identity="buy-2",
            ),
        ]
    )

    assert queued == 1
    assert [order["code"] for order in broker.pending] == ["AAPL.US"]


def test_paper_broker_partial_sell_uses_sell_ratio(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    broker = PaperBroker(str(tmp_path / "ledger.json"), market="us")
    broker.positions["AAPL.US"] = {
        "shares": 100.0,
        "entry_px": 100.0,
        "entry_date": "2026-06-09 10:00:00",
        "bs": "3buy",
    }
    broker.pending.append({
        "code": "AAPL.US",
        "act": "sell",
        "bs": "2sell",
        "sell_ratio": 0.25,
        "reason": "scale out",
    })

    broker.fill_pending(
        {"AAPL.US": _State(last_open=110.0, last_px=110.0, prev_close=109.0)},
        "2026-06-10 10:00:00",
    )

    assert broker.pending == []
    assert broker.positions["AAPL.US"]["shares"] == pytest.approx(75.0)
    assert broker.trades[-1]["shares"] == pytest.approx(25.0)
    assert broker.trades[-1]["bs_type"] == "3buy"
    assert broker.trades[-1]["exit_bs_type"] == "2sell"
    assert broker.trades[-1]["sell_ratio"] == pytest.approx(0.25)
    assert broker.trades[-1]["reason"] == "scale out"


def test_paper_broker_records_equity_curve_and_summary(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    ledger = tmp_path / "ledger.json"
    broker = PaperBroker(str(ledger), market="us")
    broker.cash = 900.0
    broker.positions["AAPL.US"] = {
        "shares": 1.0,
        "entry_px": 100.0,
        "entry_date": "2026-06-09 10:00:00",
        "bs": "3buy",
    }

    first = broker.record_snapshot(
        {"AAPL.US": _State(last_open=100.0, last_px=100.0, prev_close=99.0)},
        "2026-06-10 10:00:00",
    )
    second = broker.record_snapshot(
        {"AAPL.US": _State(last_open=80.0, last_px=80.0, prev_close=100.0)},
        "2026-06-10 10:01:00",
    )
    summary = broker.performance_summary()
    broker.save()
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    reloaded = PaperBroker(str(ledger), market="us")

    assert first["equity"] == pytest.approx(1000.0)
    assert second["equity"] == pytest.approx(980.0)
    assert summary["latest_equity"] == pytest.approx(980.0)
    assert summary["total_return"] == pytest.approx(-0.02)
    assert summary["max_drawdown"] == pytest.approx(0.02)
    assert saved["summary"]["latest_equity"] == pytest.approx(980.0)
    assert len(reloaded.equity_curve) == 2


def test_paper_broker_ensures_one_baseline_snapshot(tmp_path):
    from chanlun.recursive_bt.paper import PaperBroker

    ledger = tmp_path / "ledger.json"
    broker = PaperBroker(str(ledger), market="us")
    broker.cash = 900.0
    broker.positions["AAPL.US"] = {
        "shares": 1.0,
        "entry_px": 100.0,
        "entry_date": "2026-06-09 10:00:00",
        "bs": "3buy",
    }

    baseline = broker.ensure_baseline_snapshot("2026-06-11 09:30:00")
    duplicate = broker.ensure_baseline_snapshot("2026-06-11 09:31:00")
    summary = broker.performance_summary()
    broker.save()
    reloaded = PaperBroker(str(ledger), market="us")

    assert baseline["equity"] == pytest.approx(1000.0)
    assert baseline["cash"] == pytest.approx(900.0)
    assert baseline["positions"] == 1
    assert baseline["baseline"] is True
    assert baseline["reason"] == "ledger_baseline"
    assert duplicate is None
    assert summary["latest_equity"] == pytest.approx(1000.0)
    assert len(reloaded.equity_curve) == 1


def test_recommended_buy_ratio_caps_at_one_slot():
    from chanlun.recursive_bt.engine import recommended_buy_ratio, recommended_sell_ratio

    assert recommended_buy_ratio("3buy", 10, big_dir="up", daily_resonance=True) == 0.1
    assert recommended_buy_ratio("2buy", 10) == 0.075
    assert recommended_buy_ratio("1buy", 10) == 0.05
    assert recommended_buy_ratio("1buy", 10, big_dir="up", daily_resonance=True) == 0.08
    assert recommended_buy_ratio("3buy", 10, regime_mode="adaptive") == 0.075
    assert recommended_buy_ratio("3buy", 10, big_dir="down", regime_mode="adaptive") == 0.0
    assert recommended_buy_ratio("3buy", 10, big_dir="up", trend_boost=True) == 0.125
    assert recommended_buy_ratio("2buy", 10, big_dir="up", trend_boost=True) == 0.09
    assert recommended_buy_ratio(
        "3buy", 10, big_dir="up", regime_mode="adaptive", mid_dir="down"
    ) == 0.05
    assert recommended_buy_ratio(
        "1buy", 10, nest_mode="soft", nest_operable=False, nest_depth=1
    ) == 0.0375
    assert recommended_buy_ratio(
        "1buy", 10, nest_mode="soft", nest_operable=False, nest_depth=0
    ) == 0.025
    assert recommended_buy_ratio("3buy", 10, nest_mode="soft") == 0.1
    assert recommended_sell_ratio("1sell") == 1.0
    assert recommended_sell_ratio("3sell", big_dir="up") == 1.0
    assert recommended_sell_ratio("", big_dir="down") == 1.0


def test_pick_buy_class_respects_priority():
    from chanlun.recursive_bt.portfolio import (
        _pick_buy_class,
        _pick_buy_signal,
        _pick_sell_signal,
    )

    class Sig:
        def __init__(self, bs_type):
            self.bs_type = bs_type

    buys = [Sig("1buy"), Sig("3buy"), Sig("2buy")]

    assert _pick_buy_class(buys, "3first") == 3
    assert _pick_buy_class(buys, "1first") == 1
    assert _pick_buy_signal(buys, "3first").bs_type == "3buy"
    assert _pick_buy_signal(buys, "1first").bs_type == "1buy"
    assert _pick_sell_signal([Sig("3sell"), Sig("1sell"), Sig("2sell")]).bs_type == "1sell"


def test_nest_filter_requires_operable_for_divergence_buys():
    from chanlun.recursive_bt.portfolio import _nest_filter_ok

    class Sig:
        def __init__(self, bs_type, nest_operable=None):
            self.bs_type = bs_type
            self.nest_operable = nest_operable

    assert _nest_filter_ok(Sig("1buy", True))
    assert _nest_filter_ok(Sig("2buy", True))
    assert not _nest_filter_ok(Sig("1buy", False))
    assert not _nest_filter_ok(Sig("2buy"))
    assert _nest_filter_ok(Sig("3buy"))


def test_collect_branch_signals_matches_interval_nest_by_stable_divergence_key():
    import pandas as pd
    from types import SimpleNamespace
    from chanlun.recursive_bt.engine import collect_branch_signals

    def fx(idx, date, val):
        return SimpleNamespace(k=SimpleNamespace(k_index=idx, date=pd.Timestamp(date)), val=val)

    def div():
        return SimpleNamespace(
            kind="qs",
            leave_seg=SimpleNamespace(_type="down", start=fx(10, "2026-01-01", 12.0), end=fx(15, "2026-01-02", 10.0)),
        )

    class FakeCD:
        def get_branch_interval_nest(self):
            node = SimpleNamespace(level=0, divergence=div())
            return [SimpleNamespace(node=node, operable=True, depth=2)]

        def get_branch_bspoints(self, use_xd=False):
            return [
                SimpleNamespace(
                    anchor_fx=fx(15, "2026-01-02", 10.0),
                    level=None,
                    bs_type="1buy",
                    divergence=div(),
                )
            ]

    signals = collect_branch_signals(FakeCD(), annotate_nest=True)

    assert len(signals) == 1
    assert signals[0].nest_operable is True
    assert signals[0].nest_depth == 2


def test_mid_signals_map_to_first_main_bar_after_confirmation_delay():
    from types import SimpleNamespace
    from chanlun.recursive_bt.live_backtest import _signals_by_main_bar

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="min", tz="Asia/Shanghai"))
    sig = SimpleNamespace(date=dates[0], is_buy=True, bs_type="3buy")

    mapped = _signals_by_main_bar([sig], dates, pd.Timedelta(minutes=5))

    assert list(mapped) == [5]
    assert mapped[5][0].bs_type == "3buy"


def test_live_backtest_resolves_auto_max_pos():
    from chanlun.recursive_bt.live_backtest import resolve_max_pos

    assert resolve_max_pos(None, 2) == 2
    assert resolve_max_pos(0, 2) == 2
    assert resolve_max_pos(None, 50) == 10
    assert resolve_max_pos(7, 2) == 7


def _write_bt_symbol(path, code, signals=(), big_dir="up"):
    dates = list(pd.date_range("2026-01-01 09:30:00", periods=600, freq="5min"))
    small_by_bar = {}
    for idx, bs_type, price in signals:
        small_by_bar.setdefault(idx, []).append(Signal(dates[idx], 0, bs_type, price))
    data = {
        "code": code,
        "dates": dates,
        "open": [10.0] * len(dates),
        "close": [10.0] * len(dates),
        "small_by_bar": small_by_bar,
        "big_dir_at": [big_dir] * len(dates),
        "limit_pct": 0.10,
    }
    with open(path / f"{code}.pkl", "wb") as fp:
        pickle.dump(data, fp)


def test_live_backtest_bt_data_selector_pool_avoids_sorted_prefix(tmp_path):
    from chanlun.recursive_bt.live_backtest import load_bt_data_syms

    _write_bt_symbol(tmp_path, "BJ.810011")
    _write_bt_symbol(tmp_path, "SH.600000", [(599, "3buy", 10.5)])
    _write_bt_symbol(tmp_path, "SZ.000001", [(599, "3buy", 11.0)], big_dir="down")

    selected = load_bt_data_syms(
        "a",
        bt_data_dir=str(tmp_path),
        pool_size=1,
        pool_mode="selector",
        selection_lookback_bars=3,
        selection_require_three_systems=False,
    )
    sorted_prefix = load_bt_data_syms(
        "a",
        bt_data_dir=str(tmp_path),
        pool_size=1,
        pool_mode="sorted",
    )

    assert list(selected) == ["SH.600000"]
    assert list(sorted_prefix) == ["BJ.810011"]


def test_live_backtest_walk_forward_scan_limit_is_stratified(tmp_path):
    from chanlun.recursive_bt.live_backtest import load_bt_data_syms

    _write_bt_symbol(tmp_path, "BJ.810011")
    _write_bt_symbol(tmp_path, "BJ.920001")
    _write_bt_symbol(tmp_path, "SH.600000")
    _write_bt_symbol(tmp_path, "SH.688001")
    _write_bt_symbol(tmp_path, "SZ.300001")

    selected = load_bt_data_syms(
        "a",
        bt_data_dir=str(tmp_path),
        pool_mode="walk_forward",
        selection_scan_limit=4,
    )
    sorted_selected = load_bt_data_syms(
        "a",
        bt_data_dir=str(tmp_path),
        pool_mode="walk_forward",
        selection_scan_limit=4,
        selection_sample_mode="sorted",
    )

    assert list(selected) == ["SH.600000", "SZ.300001", "SH.688001", "BJ.810011"]
    assert list(sorted_selected)[:2] == ["BJ.810011", "BJ.920001"]


def test_live_backtest_walk_forward_board_filter_excludes_bj(tmp_path):
    from chanlun.recursive_bt.live_backtest import load_bt_data_syms

    _write_bt_symbol(tmp_path, "BJ.810011")
    _write_bt_symbol(tmp_path, "SH.600000")
    _write_bt_symbol(tmp_path, "SH.688001")
    _write_bt_symbol(tmp_path, "SZ.300001")

    selected = load_bt_data_syms(
        "a",
        bt_data_dir=str(tmp_path),
        pool_mode="walk_forward",
        selection_board_filter="shsz",
        selection_scan_limit=10,
    )

    assert set(selected) == {"SH.600000", "SH.688001", "SZ.300001"}


def test_live_backtest_board_counts_cover_full_universe():
    from chanlun.recursive_bt.live_backtest import _board_counts

    syms = {
        "SH.600000": {},
        "SH.688001": {},
        "SZ.300001": {},
        "BJ.920001": {},
        "BJ.810011": {},
    }

    assert _board_counts("a", syms) == {
        "bj": 2,
        "gem": 1,
        "main": 1,
        "star": 1,
    }
    assert _board_counts("us", {"AAPL.US": {}}) == {}


def test_live_backtest_market_regime_segments_classify_bull_and_bear():
    from chanlun.recursive_bt.live_backtest import _market_regime_segments

    master = list(pd.date_range("2026-01-01", periods=60, freq="D", tz="Asia/Shanghai"))
    bench = np.r_[np.linspace(1.0, 1.30, 30), np.linspace(1.30, 0.95, 30)]
    equity = np.r_[np.linspace(1.0, 1.45, 30), np.linspace(1.45, 1.20, 30)]
    result = {
        "master": master,
        "bench": bench,
        "equity": equity,
        "trades": [
            {"entry_date": master[24]},
            {"entry_date": master[45]},
        ],
    }

    regime_report = _market_regime_segments(result, lookback_days=20)
    segments = regime_report["segments"]

    assert segments["bull"]["days"] > 0
    assert segments["bear"]["days"] > 0
    assert segments["bull"]["strategy_return"] > 0
    assert segments["bear"]["benchmark_return"] < 0
    assert segments["bull"]["trade_count"] == 1
    assert segments["bear"]["trade_count"] == 1
    assert len(regime_report["daily_regimes"]) == 60
    assert {"date", "regime", "benchmark_lookback_return"} <= set(
        regime_report["daily_regimes"][0]
    )


def test_merge_require_preserves_order_and_deduplicates():
    from chanlun.recursive_bt.live_backtest import _merge_require

    assert _merge_require(("tech", "fund"), ("fund", "value")) == (
        "tech",
        "fund",
        "value",
    )


def test_live_backtest_walk_forward_pool_loads_universe_and_attaches_scores(monkeypatch):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    calls = {}

    def fake_load_bt_data_syms(*args, **kwargs):
        calls["pool_mode"] = kwargs["pool_mode"]
        return {
            "SH.600000": {
                "dates": list(pd.date_range("2026-01-01", periods=600, freq="5min")),
                "open": [10.0] * 600,
                "close": [10.0] * 600,
                "d2i": {},
                "small_by_bar": {},
                "big_dir_at": ["neutral"] * 600,
                "rules": SimpleNamespace(limit_pct=None),
                "code": "SH.600000",
            }
        }

    def fake_attach(syms, bt_data, fund_data):
        calls["attach"] = (list(syms), bt_data, fund_data)
        return True

    def fake_portfolio_backtest(**kwargs):
        calls["require"] = kwargs["require"]
        return {
            "master": list(pd.date_range("2026-01-01", periods=2, freq="5min")),
            "total": 0.0,
            "bh": 0.0,
            "max_dd": 0.0,
            "sharpe": 0.0,
            "wr": 0.0,
            "n": 0,
            "trades": [],
        }

    monkeypatch.setattr(live_backtest, "load_bt_data_syms", fake_load_bt_data_syms)
    monkeypatch.setattr(live_backtest, "_attach_walk_forward_scores", fake_attach)
    monkeypatch.setattr(live_backtest.portfolio_mod, "portfolio_backtest", fake_portfolio_backtest)

    args = SimpleNamespace(
        market="a",
        source="bt_data",
        codes=None,
        bt_data="D:/bt",
        pool_size=20,
        bt_pool_mode="walk_forward",
        selection_fund_data="D:/fund",
        selection_scan_limit=0,
        selection_sample_mode="stratified",
        selection_board_filter="all",
        selection_lookback_bars=240,
        selection_buy_classes=(3, 2, 1),
        selection_require_three_systems=True,
        op_level="5m",
        big_level="30m",
        mid_level=None,
        max_pos=None,
        requested_max_pos=None,
        start=None,
        end=None,
        buy_priority="3first",
        require=("tech",),
        big_gate="bsp",
        regime_mode="off",
        mid_gate="strict",
        init_cash=1_000_000,
    )

    live_backtest.run_backtest(args)

    assert calls["pool_mode"] == "walk_forward"
    assert calls["attach"] == (["SH.600000"], "D:/bt", "D:/fund")
    assert calls["require"] == ("tech", "fund", "value")
    assert args.walk_forward_scores is True


def test_live_backtest_passes_confirmed_bs_point_ratio_multipliers(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    dates = list(pd.date_range("2026-01-01", periods=120, freq="5min"))
    overrides = tmp_path / "bs_overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "overrides": [
                    {
                        "market": "us",
                        "bs_point_ratio_multipliers": {"3": 1.1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = {}

    def fake_load_chart_cache_syms(*_args, **_kwargs):
        return {
            "QQQ.US": {
                "code": "QQQ.US",
                "dates": dates,
                "d2i": {d: i for i, d in enumerate(dates)},
                "small_by_bar": {},
                "big_dir_at": ["neutral"] * len(dates),
            }
        }

    def fake_portfolio_backtest(**kwargs):
        calls["multipliers"] = kwargs["bs_point_ratio_multipliers"]
        calls["sell_classes"] = kwargs["sell_classes"]
        calls["sell_ratio_overrides"] = kwargs["sell_ratio_overrides"]
        calls["sell_ratio_override_scope"] = kwargs["sell_ratio_override_scope"]
        calls["after_3sell_reentry_buy_classes"] = kwargs["after_3sell_reentry_buy_classes"]
        calls["after_3sell_reentry_mid_buy_classes"] = kwargs[
            "after_3sell_reentry_mid_buy_classes"
        ]
        return {
            "master": dates,
            "total": 0.0,
            "bh": 0.0,
            "max_dd": 0.0,
            "sharpe": 0.0,
            "wr": 0.0,
            "n": 0,
            "trades": [],
        }

    monkeypatch.setattr(live_backtest, "load_chart_cache_syms", fake_load_chart_cache_syms)
    monkeypatch.setattr(live_backtest.portfolio_mod, "portfolio_backtest", fake_portfolio_backtest)

    args = SimpleNamespace(
        market="us",
        source="chart_cache",
        codes="QQQ.US",
        chart_cache="D:/cache",
        pool_size=0,
        op_level="1m",
        big_level="30m",
        mid_level="5m",
        max_pos=9,
        requested_max_pos=None,
        start=None,
        end=None,
        buy_priority="3first",
        require=("tech",),
        big_gate="bsp",
        regime_mode="off",
        mid_gate="soft",
        sell_classes=(1, 2, 3),
        sell_ratio_overrides={"3": 0.5},
        sell_ratio_override_scope="up",
        after_3sell_reentry_buy_classes=(3,),
        after_3sell_reentry_mid_buy_classes=(3,),
        init_cash=1_000_000,
        bs_point_ratio_overrides_enabled=True,
        bs_point_ratio_overrides_json=str(overrides),
    )

    live_backtest.run_backtest(args)

    assert calls["multipliers"] == {"3": 1.1}
    assert calls["sell_classes"] == {1, 2, 3}
    assert calls["sell_ratio_overrides"] == {"3": 0.5}
    assert calls["sell_ratio_override_scope"] == "up"
    assert calls["after_3sell_reentry_buy_classes"] == {3}
    assert calls["after_3sell_reentry_mid_buy_classes"] == {3}
    assert args.bs_point_ratio_multipliers == {"3": 1.1}


def test_live_backtest_accepts_bt_data_mtf3_cache(monkeypatch):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    calls = {}
    dates = list(pd.date_range("2026-01-01", periods=3, freq="min"))

    def fake_load_bt_data_syms(*_args, **_kwargs):
        return {
            "SH.600000": {
                "code": "SH.600000",
                "dates": dates,
                "d2i": {d: i for i, d in enumerate(dates)},
                "mid_dir_at": ["neutral"] * len(dates),
            }
        }

    def fake_portfolio_backtest(**kwargs):
        calls["label"] = kwargs["label"]
        calls["syms"] = kwargs["syms"]
        return {
            "master": dates,
            "total": 0.0,
            "bh": 0.0,
            "max_dd": 0.0,
            "sharpe": 0.0,
            "wr": 0.0,
            "n": 0,
            "trades": [],
        }

    monkeypatch.setattr(live_backtest, "load_bt_data_syms", fake_load_bt_data_syms)
    monkeypatch.setattr(live_backtest.portfolio_mod, "portfolio_backtest", fake_portfolio_backtest)

    args = SimpleNamespace(
        market="a",
        source="bt_data",
        codes=None,
        bt_data="D:/chanlun_pro/bt_data_mtf3",
        pool_size=0,
        bt_pool_mode="all",
        selection_fund_data="D:/fund",
        selection_scan_limit=0,
        selection_sample_mode="stratified",
        selection_board_filter="all",
        selection_lookback_bars=240,
        selection_buy_classes=(3, 2, 1),
        selection_require_three_systems=False,
        op_level="1m",
        big_level="30m",
        mid_level="5m",
        max_pos=10,
        requested_max_pos=None,
        start=None,
        end=None,
        buy_priority="3first",
        require=("tech",),
        big_gate="bsp",
        regime_mode="off",
        mid_gate="strict",
        init_cash=1_000_000,
    )

    live_backtest.run_backtest(args)

    assert calls["label"] == "a-1-30m+5m+1m"
    assert "mid_dir_at" in calls["syms"]["SH.600000"]


def test_live_backtest_rejects_plain_bt_data_as_mtf3(monkeypatch):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    dates = list(pd.date_range("2026-01-01", periods=3, freq="min"))

    def fake_load_bt_data_syms(*_args, **_kwargs):
        return {
            "SH.600000": {
                "code": "SH.600000",
                "dates": dates,
                "d2i": {d: i for i, d in enumerate(dates)},
            }
        }

    monkeypatch.setattr(live_backtest, "load_bt_data_syms", fake_load_bt_data_syms)

    args = SimpleNamespace(
        market="a",
        source="bt_data",
        codes=None,
        bt_data="D:/chanlun_pro/bt_data",
        pool_size=0,
        bt_pool_mode="all",
        selection_fund_data="D:/fund",
        selection_scan_limit=0,
        selection_sample_mode="stratified",
        selection_board_filter="all",
        selection_lookback_bars=240,
        selection_buy_classes=(3, 2, 1),
        selection_require_three_systems=False,
        op_level="1m",
        big_level="30m",
        mid_level="5m",
        max_pos=10,
        requested_max_pos=None,
        start=None,
        end=None,
        buy_priority="3first",
        require=("tech",),
        big_gate="bsp",
        regime_mode="off",
        mid_gate="strict",
        init_cash=1_000_000,
    )

    with pytest.raises(ValueError, match="mid_dir_at"):
        live_backtest.run_backtest(args)


def test_fetch_build_annotates_operation_level_with_interval_nest(monkeypatch):
    import numpy as np
    import pandas as pd
    from chanlun.recursive_bt import fetch

    calls = []

    class FakeCL:
        def __init__(self, *args, **kwargs):
            pass

        def process_klines(self, df):
            self.df = df

    class FakeStrategy:
        def __init__(self, small, big, dates, *_args, **_kwargs):
            self.small_by_bar = {}
            self.big_dir_at = ["neutral"] * len(dates)

    class FakeExchange:
        def klines(self, code, tf, start_date=None):
            periods = 210 if tf == "5m" else 60
            dates = pd.date_range("2026-01-01", periods=periods, freq="5min")
            return pd.DataFrame(
                {
                    "date": dates,
                    "open": np.ones(periods),
                    "high": np.ones(periods),
                    "low": np.ones(periods),
                    "close": np.ones(periods),
                    "volume": np.ones(periods),
                }
            )

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        calls.append(annotate_nest)
        return []

    monkeypatch.setattr(fetch, "CL", FakeCL)
    monkeypatch.setattr(fetch, "MTFStrategy", FakeStrategy)
    monkeypatch.setattr(fetch, "collect_branch_signals", fake_collect)

    data = fetch.build("SH.600000", FakeExchange())

    assert data["signal_schema"]["nest_operable"] is True
    assert calls == [True, False]


def test_fetch_run_writes_build_manifest(monkeypatch, tmp_path):
    from chanlun.recursive_bt import fetch

    def fake_build(code, ex, small_tf, big_tf, start, end, big_delay, min_small,
                   mid_tf=None, mid_delay=None):
        if code == "FAIL":
            return None
        return {
            "code": code,
            "dates": [],
            "open": [],
            "close": [],
            "small_by_bar": {},
            "big_dir_at": [],
            "mid_by_bar": {},
            "mid_dir_at": [],
            "n_small": 3,
            "n_mid": 2,
            "n_big": 1,
        }

    monkeypatch.setattr(fetch, "build", fake_build)

    manifest = fetch.run(
        ["OK", "FAIL"],
        str(tmp_path),
        "1m",
        "30m",
        None,
        None,
        big_delay=None,
        min_small=1,
        mid_tf="5m",
        manifest_label="test_mtf3",
        exchange=object(),
    )

    saved = json.loads((tmp_path / "_build_manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"] == {"ok": 1, "skip": 0, "fail": 1}
    assert saved["label"] == "test_mtf3"
    assert saved["levels"] == {"small_tf": "1m", "mid_tf": "5m", "big_tf": "30m"}
    assert [item["status"] for item in saved["entries"]] == ["ok", "fail"]
    assert saved["entries"][0]["has_mid_by_bar"] is True


def test_fetch_limit_pct_supports_bj_board():
    from chanlun.recursive_bt.fetch import limit_pct

    assert limit_pct("BJ.920001") == 0.30
    assert limit_pct("SZ.300001") == 0.20
    assert limit_pct("SH.600000") == 0.10


def test_fetch_board_filter_selects_requested_a_share_boards():
    from chanlun.recursive_bt.fetch import filter_codes_by_board

    codes = ["SH.600000", "SZ.300001", "SH.688001", "BJ.920001"]

    assert filter_codes_by_board(codes, "gem") == ["SZ.300001"]
    assert filter_codes_by_board(codes, "shsz") == [
        "SH.600000",
        "SZ.300001",
        "SH.688001",
    ]
    assert filter_codes_by_board(codes, "bj") == ["BJ.920001"]


def test_fetch_board_sample_stratifies_a_share_mtf3_pool():
    from chanlun.recursive_bt.fetch import sample_codes_by_board

    codes = [
        "SH.600000",
        "SH.600001",
        "SZ.000001",
        "SZ.000002",
        "SZ.300001",
        "SZ.300002",
        "SH.688001",
        "SH.688002",
        "BJ.920001",
    ]

    assert sample_codes_by_board(codes, 6) == [
        "SH.600000",
        "SZ.300001",
        "SH.688001",
        "BJ.920001",
        "SH.600001",
        "SZ.300002",
    ]
    assert sample_codes_by_board(codes, 3, "sorted") == [
        "BJ.920001",
        "SH.600000",
        "SH.600001",
    ]


def test_fundamentals_board_filter_matches_fetch_filter():
    from chanlun.recursive_bt.fundamentals import filter_codes_by_board

    codes = ["SH.600000", "SZ.300001", "SH.688001", "BJ.920001"]

    assert filter_codes_by_board(codes, "gem,star") == ["SZ.300001", "SH.688001"]


def test_recursive_portfolio_limit_locked_matches_paper_rules():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import _limit_locked

    s = {
        "open": [10.0, 11.0, 9.0],
        "close": [10.0, 10.5, 9.5],
        "rules": MarketRules("A", limit_pct=0.10),
    }

    assert _limit_locked(s, 1, "buy")
    assert _limit_locked(s, 2, "sell")


def test_portfolio_value_bull_relaxed_only_relaxes_value_in_confirmed_bull():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=30, freq="5min", tz="Asia/Shanghai"))

    def make_sym(market_bull: bool):
        return {
            "code": "SH.600000",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {5: [Signal(dates[5], 0, "3buy", 10.0)]},
            "big_dir_at": ["up"] * len(dates),
            "fund_ok": np.ones(len(dates), dtype=bool),
            "value_ok": np.zeros(len(dates), dtype=bool),
            "market_bull_at": np.full(len(dates), market_bull, dtype=bool),
            "rules": MarketRules("A", commission=0.0, stamp_duty=0.0, lot=1),
        }

    strict = portfolio_backtest(
        syms={"S": make_sym(True)},
        max_pos=1,
        require=("tech", "fund", "value"),
        label="strict",
    )
    relaxed_bear = portfolio_backtest(
        syms={"S": make_sym(False)},
        max_pos=1,
        require=("tech", "fund", "value", "value_bull_relaxed"),
        label="relaxed-bear",
    )
    relaxed_bull = portfolio_backtest(
        syms={"S": make_sym(True)},
        max_pos=1,
        require=("tech", "fund", "value", "value_bull_relaxed"),
        label="relaxed-bull",
    )

    assert strict["n"] == 0
    assert relaxed_bear["n"] == 0
    assert relaxed_bull["n"] == 1


def test_portfolio_backtest_records_exit_sell_point_class():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "SH.600000",
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.5, 11.0, 12.0, 12.0, 12.0, 12.0]),
            "close": np.array([10.0, 10.0, 10.5, 11.0, 12.0, 12.0, 12.0, 12.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "2sell", 11.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("A", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="exit-class",
    )

    trade = result["trades"][0]
    assert result["n"] == 1
    assert trade.bs_type == "3"
    assert trade.reason == "small_level_sell_point"
    assert trade.exit_bs_type == "2sell"
    assert trade.sell_ratio == pytest.approx(1.0)
    assert trade.post_exit_bars == 3
    assert trade.post_exit_ret_5 == pytest.approx(0.0)


def test_portfolio_backtest_can_ignore_3sell_for_sell_policy_candidate():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=9, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "SH.600000",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "1sell", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("A", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    default = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="default-sells",
    )
    sell12 = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="sell12",
        sell_classes={1, 2},
    )

    assert default["trades"][0].exit_bs_type == "3sell"
    assert sell12["trades"][0].exit_bs_type == "1sell"


def test_portfolio_backtest_can_half_exit_on_3sell_candidate():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=9, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.0, 10.0, 12.0, 12.0, 14.0, 14.0, 14.0]),
            "close": np.array([10.0, 10.0, 10.0, 10.0, 12.0, 12.0, 14.0, 14.0, 14.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "1sell", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="sell3half",
        sell_ratio_overrides={"3": 0.5},
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].exit_bs_type == "3sell"
    assert trades[0].sell_ratio == pytest.approx(0.5)
    assert trades[1].exit_bs_type == "1sell"
    assert trades[1].sell_ratio == pytest.approx(1.0)
    assert trades[0].shares == pytest.approx(trades[1].shares)
    assert result["total"] > 0.0


def test_portfolio_backtest_limits_3sell_half_exit_to_big_level_up():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=9, freq="5min", tz="Asia/Shanghai"))

    def make_symbol(code: str, big_dir: str):
        return {
            "code": code,
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.0, 10.0, 12.0, 12.0, 14.0, 14.0, 14.0]),
            "close": np.array([10.0, 10.0, 10.0, 10.0, 12.0, 12.0, 14.0, 14.0, 14.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "1sell", 10.0)],
            },
            "big_dir_at": [big_dir] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }

    up = portfolio_backtest(
        syms={"S": make_symbol("QQQ.US", "up")},
        max_pos=1,
        require=("tech",),
        label="sell3half-up",
        sell_ratio_overrides={"3": 0.5},
        sell_ratio_override_scope="up",
    )
    neutral = portfolio_backtest(
        syms={"S": make_symbol("SPY.US", "neutral")},
        max_pos=1,
        require=("tech",),
        label="sell3half-neutral",
        sell_ratio_overrides={"3": 0.5},
        sell_ratio_override_scope="up",
    )

    assert len(up["trades"]) == 2
    assert up["trades"][0].sell_ratio == pytest.approx(0.5)
    assert up["trades"][1].sell_ratio == pytest.approx(1.0)
    assert len(neutral["trades"]) == 1
    assert neutral["trades"][0].exit_bs_type == "3sell"
    assert neutral["trades"][0].sell_ratio == pytest.approx(1.0)


def test_portfolio_backtest_can_require_3buy_reentry_after_3sell():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "1buy", 10.0)],
                6: [Signal(dates[6], 0, "3buy", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    default = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="default-reentry",
    )
    rebuy3 = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="sell3-rebuy3",
        after_3sell_reentry_buy_classes={3},
    )

    assert [trade.exit_bs_type for trade in default["trades"]] == ["3sell", ""]
    assert default["trades"][1].reason == "final_close"
    assert default["trades"][1].bs_type == "1"
    assert [trade.exit_bs_type for trade in rebuy3["trades"]] == ["3sell", ""]
    assert rebuy3["trades"][1].reason == "final_close"
    assert rebuy3["trades"][1].bs_type == "3"


def test_portfolio_backtest_limits_3sell_reentry_lock_by_big_direction_scope():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="5min", tz="Asia/Shanghai"))

    def make_symbol(big_dir: str):
        return {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "1buy", 10.0)],
                6: [Signal(dates[6], 0, "3buy", 10.0)],
            },
            "big_dir_at": [big_dir] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }

    up = portfolio_backtest(
        syms={"S": make_symbol("up")},
        max_pos=1,
        require=("tech",),
        label="sell3-rebuy3-not-up-scope-up",
        after_3sell_reentry_buy_classes={3},
        after_3sell_reentry_scope="not_up",
    )
    neutral = portfolio_backtest(
        syms={"S": make_symbol("neutral")},
        max_pos=1,
        require=("tech",),
        label="sell3-rebuy3-not-up-scope-neutral",
        after_3sell_reentry_buy_classes={3},
        after_3sell_reentry_scope="not_up",
    )

    assert up["trades"][1].bs_type == "1"
    assert neutral["trades"][1].bs_type == "3"


def test_portfolio_backtest_applies_regime_bs_ratio_multipliers_point_in_time():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 15:00:00", periods=9, freq="1D", tz="Asia/Shanghai"))
    px = np.array([10.0, 10.0, 10.0, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9])

    def make_symbol(code: str, signal_bar: int):
        return {
            "code": code,
            "dates": dates,
            "open": px.copy(),
            "close": px.copy(),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                signal_bar: [Signal(dates[signal_bar], 0, "3buy", float(px[signal_bar]))],
                6: [Signal(dates[6], 0, "1sell", float(px[6]))],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }

    # 等权基准在 bar3 当日收盘跌至 0.89(回撤-11%),当日收盘才判 bear:
    # X 的 3buy 在 bar3 当天,点时只能看到前一日 range,不允许放大;
    # Y 的 3buy 在 bar4,前一交易日(bar3)收盘已判 bear,允许放大。
    syms = {
        "X": make_symbol("SPY.US", 3),
        "Y": make_symbol("QQQ.US", 4),
    }

    boosted = portfolio_backtest(
        syms=syms,
        max_pos=2,
        require=("tech",),
        label="regime-bear3-boost",
        regime_bs_ratio_multipliers={"bear": {"3": 1.25}},
        regime_lookback_days=2,
    )
    plain = portfolio_backtest(
        syms=syms,
        max_pos=2,
        require=("tech",),
        label="regime-off",
    )

    boosted_by_code = {t.code: t for t in boosted["trades"]}
    plain_by_code = {t.code: t for t in plain["trades"]}
    assert boosted_by_code["SPY.US"].buy_ratio == pytest.approx(0.5)
    assert boosted_by_code["QQQ.US"].buy_ratio == pytest.approx(0.625)
    assert plain_by_code["SPY.US"].buy_ratio == pytest.approx(0.5)
    assert plain_by_code["QQQ.US"].buy_ratio == pytest.approx(0.5)


def test_portfolio_backtest_uses_external_regime_source(monkeypatch):
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 15:00:00", periods=9, freq="1D", tz="Asia/Shanghai"))
    flat = np.full(len(dates), 10.0)
    falling = np.array([10.0, 10.0, 10.0, 8.9, 8.9, 8.9, 8.9, 8.9, 8.9])

    def make_symbol(code: str, px: np.ndarray, signal_bar: int | None = None):
        by_bar = {}
        if signal_bar is not None:
            by_bar[signal_bar] = [Signal(dates[signal_bar], 0, "3buy", float(px[signal_bar]))]
            by_bar[6] = [Signal(dates[6], 0, "1sell", float(px[6]))]
        return {
            "code": code,
            "dates": dates,
            "open": px.copy(),
            "close": px.copy(),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": by_bar,
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }

    # 交易标的本身平盘(等权基准恒 range),只有外部指数源在 bar3 跌入 bear:
    # 不传外部源时 bear 乘数不可能触发;传外部源时 bar4 的 3buy 吃到 ×1.25。
    syms = {"X": make_symbol("QQQ.US", flat, signal_bar=4)}
    index_sym = make_symbol("SH.000001", falling)

    with_index = portfolio_backtest(
        syms=syms,
        max_pos=2,
        require=("tech",),
        label="regime-index-source",
        regime_bs_ratio_multipliers={"bear": {"3": 1.25}},
        regime_lookback_days=2,
        regime_source_sym=index_sym,
    )
    bench_only = portfolio_backtest(
        syms=syms,
        max_pos=2,
        require=("tech",),
        label="regime-bench-source",
        regime_bs_ratio_multipliers={"bear": {"3": 1.25}},
        regime_lookback_days=2,
    )

    assert with_index["trades"][0].buy_ratio == pytest.approx(0.625)
    assert bench_only["trades"][0].buy_ratio == pytest.approx(0.5)


def test_live_backtest_passes_regime_bs_ratio_multipliers(monkeypatch):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    dates = list(pd.date_range("2026-01-01", periods=10, freq="5min"))
    calls = {}

    def fake_load_chart_cache_syms(*_args, **_kwargs):
        return {
            "QQQ.US": {
                "code": "QQQ.US",
                "dates": dates,
                "d2i": {d: i for i, d in enumerate(dates)},
                "small_by_bar": {},
                "big_dir_at": ["neutral"] * len(dates),
            }
        }

    def fake_portfolio_backtest(**kwargs):
        calls["regime_multipliers"] = kwargs["regime_bs_ratio_multipliers"]
        calls["regime_lookback_days"] = kwargs["regime_lookback_days"]
        return {
            "master": dates,
            "total": 0.0,
            "bh": 0.0,
            "max_dd": 0.0,
            "sharpe": 0.0,
            "wr": 0.0,
            "n": 0,
            "trades": [],
        }

    monkeypatch.setattr(live_backtest, "load_chart_cache_syms", fake_load_chart_cache_syms)
    monkeypatch.setattr(live_backtest.portfolio_mod, "portfolio_backtest", fake_portfolio_backtest)

    args = SimpleNamespace(
        market="us",
        source="chart_cache",
        codes="QQQ.US",
        chart_cache="D:/cache",
        pool_size=0,
        op_level="1m",
        big_level="30m",
        mid_level="5m",
        max_pos=9,
        requested_max_pos=None,
        start=None,
        end=None,
        buy_priority="3first",
        require=("tech",),
        big_gate="bsp",
        regime_mode="off",
        mid_gate="soft",
        init_cash=1_000_000,
        regime_bs_ratio_multipliers_json='{"bear": {"3": 1.25}, "bull": {"1": 0.5}}',
    )

    live_backtest.run_backtest(args)

    assert calls["regime_multipliers"] == {"bear": {"3": 1.25}, "bull": {"1": 0.5}}
    assert calls["regime_lookback_days"] == 20
    assert args.regime_bs_ratio_multipliers == {"bear": {"3": 1.25}, "bull": {"1": 0.5}}


def test_portfolio_backtest_can_require_mid_3buy_reentry_after_3sell():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=11, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
                5: [Signal(dates[5], 0, "3buy", 10.0)],
                7: [Signal(dates[7], 0, "1buy", 10.0)],
            },
            "mid_by_bar": {
                6: [Signal(dates[6], 0, "3buy", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    default = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="default-mid-reentry",
    )
    mid3 = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="sell3-rebuy-mid3",
        after_3sell_reentry_mid_buy_classes={3},
    )

    assert default["trades"][1].bs_type == "3"
    assert mid3["trades"][1].bs_type == "1"


def test_portfolio_backtest_applies_bs_point_ratio_multiplier_to_buy_weight():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 0, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "1sell", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=10,
        require=("tech",),
        label="buy-ratio-multiplier",
        bs_point_ratio_multipliers={"3": 1.1},
    )

    trade = result["trades"][0]
    assert result["n"] == 1
    assert trade.buy_ratio == pytest.approx(0.11)


def test_backtest_run_includes_open_position_codes():
    pytest.importorskip("pyecharts")
    pytest.importorskip("pyfolio")

    from chanlun.backtesting.backtest import BackTest

    class DummyDatas:
        def __init__(self):
            self.now_date = datetime.datetime(2024, 1, 2, 15, 0)
            self.load_data_to_cache = True
            self._did_next = False

        def init(self, base_code, frequency):
            self.base_code = base_code
            self.frequency = frequency

        def next(self, frequency):
            if self._did_next:
                return False
            self._did_next = True
            return True

    class DummyTrader:
        def __init__(self):
            self.buffer_opts = []
            self.ran_codes = []

        def update_position_record(self):
            return True

        def position_codes(self):
            return ["HELD"]

        def run(self, code, is_filter=False):
            self.ran_codes.append(code)

        def run_buffer_opts(self):
            self.buffer_ran = True

        def end(self):
            self.ended = True

    class DummyStrategy:
        def __init__(self):
            self.loop_starts = 0

        def on_bt_loop_start(self, bt):
            self.loop_starts += 1

        def is_filter_opts(self):
            return False

        def filter_opts(self, opts, trader):
            return opts

        def clear(self):
            self.cleared = True

    bt = BackTest()
    bt.base_code = "BASE"
    bt.codes = ["BASE"]
    bt.frequencys = ["d"]
    bt.load_data_to_cache = True
    bt.datas = DummyDatas()
    bt.trader = DummyTrader()
    bt.strategy = DummyStrategy()

    assert bt.run()
    assert bt.trader.ran_codes == ["HELD", "BASE"]
    assert bt.strategy.loop_starts == 1
