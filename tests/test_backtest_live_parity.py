import datetime
import json
import pickle

import pytest
import numpy as np
import pandas as pd

from chanlun.backtesting.backtest_trader import BackTestTrader
from chanlun.recursive_bt.engine import Signal


def test_structural_signal_fields_preserves_explicit_second_class_stop():
    from types import SimpleNamespace

    from chanlun.recursive_bt.engine import _structural_signal_fields

    p = SimpleNamespace(
        bs_type="2buy",
        structural_stop_below=403.41,
        anchor_fx=SimpleNamespace(val=418.35),
        zs=SimpleNamespace(zd=418.35, zg=421.71),
    )

    fields = _structural_signal_fields(p)

    assert fields["structural_stop_below"] == pytest.approx(403.41)
    assert fields["structural_stop_above"] is None
    assert fields["zs_zd"] == pytest.approx(418.35)
    assert fields["zs_zg"] == pytest.approx(421.71)


def test_no_future_policy_distinguishes_full_history_from_bounded_warmup():
    from types import SimpleNamespace

    from chanlun.recursive_bt.live_backtest import _no_future_policy_summary

    full = _no_future_policy_summary(
        SimpleNamespace(
            signal_mode="walk_forward",
            signal_warmup_bars=-1,
            signal_scan_chunk_bars=5000,
            start=None,
        )
    )
    full_with_start = _no_future_policy_summary(
        SimpleNamespace(
            signal_mode="walk_forward",
            signal_warmup_bars=-1,
            signal_scan_chunk_bars=5000,
            start="2026-04-14T16:00:00+00:00",
        )
    )
    full_with_complete_registry = _no_future_policy_summary(
        SimpleNamespace(
            signal_mode="walk_forward",
            signal_warmup_bars=-1,
            signal_scan_chunk_bars=5000,
            start="2026-04-14T16:00:00+00:00",
        ),
        signal_seen_registry_complete=True,
    )
    bounded = _no_future_policy_summary(
        SimpleNamespace(
            signal_mode="walk_forward",
            signal_warmup_bars=1200,
            signal_scan_chunk_bars=3000,
            start="2026-04-14T16:00:00+00:00",
        )
    )

    assert full["strict_no_future"] is True
    assert full["history_state_complete"] is True
    assert full["history_state"] == "full_prior_history"
    assert full["anchor_time_tradeable"] is False
    assert full["chunked_signal_scan"] is False
    assert full["signal_scan_chunk_bars"] == 0
    assert full["requested_signal_scan_chunk_bars"] == 5000
    assert full["signal_seen_registry_complete"] is True
    assert full["stale_reappearing_signal_risk"] is False
    assert full_with_start["history_state_complete"] is True
    assert full_with_start["signal_seen_registry_complete"] is False
    assert full_with_start["stale_reappearing_signal_risk"] is True
    assert "first-seen registry" in full_with_start["warning"]
    assert full_with_complete_registry["signal_seen_registry_complete"] is True
    assert full_with_complete_registry["stale_reappearing_signal_risk"] is False
    assert full_with_complete_registry["warning"] == ""
    assert bounded["strict_no_future"] is True
    assert bounded["history_state_complete"] is False
    assert bounded["history_state"] == "bounded_warmup"
    assert bounded["chunked_signal_scan"] is True
    assert bounded["signal_seen_registry_complete"] is False
    assert "bounded warmup" in bounded["warning"]


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
    assert recommended_sell_ratio(
        "3sell",
        big_dir="up",
        policy="original_layered",
        exit_level=0,
        core_signal_level=2,
        swing_signal_level=1,
    ) == 0.25
    assert recommended_sell_ratio(
        "3sell",
        big_dir="up",
        policy="original_layered",
        exit_level=1,
        core_signal_level=2,
        swing_signal_level=1,
    ) == 0.5
    assert recommended_sell_ratio(
        "3sell",
        big_dir="up",
        policy="original_layered",
        exit_level=2,
        core_signal_level=2,
        swing_signal_level=1,
    ) == 1.0
    assert recommended_sell_ratio(
        "2sell",
        big_dir="neutral",
        policy="original_layered",
        exit_level=1,
        core_signal_level=2,
        swing_signal_level=1,
    ) == 0.75


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


def test_apply_buy_ratio_multiplier_honors_explicit_zero():
    """显式 0.0 乘数=该买点类彻底跳过(regime 过滤原语),不得被 `or 1.0` 还原成不变。

    回归:`multipliers.get(cls, 1.0) or 1.0` 把 0.0(falsy)当缺失→还原 1.0,
    使 `{"bear": {"3": 0.0}}` 静默失效(bear 仍满仓买 3 买)。"""
    from chanlun.recursive_bt.portfolio import _apply_buy_ratio_multiplier

    # 0.0 → 跳过(ratio 归零,调用方据此 continue)
    assert _apply_buy_ratio_multiplier(1.0, "3buy", {"3": 0.0}) == 0.0
    # 0.5 → 减半
    assert _apply_buy_ratio_multiplier(1.0, "3buy", {"3": 0.5}) == 0.5
    # 缺失键 / None → 不变(1.0 兜底,保持既有语义)
    assert _apply_buy_ratio_multiplier(0.8, "3buy", {"1": 0.5}) == 0.8
    assert _apply_buy_ratio_multiplier(0.8, "3buy", {"3": None}) == 0.8
    # 无乘数表 → 原样
    assert _apply_buy_ratio_multiplier(0.6, "3buy", None) == 0.6


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


def test_walk_forward_trend_dir_waits_for_high_level_delay_and_warmup(monkeypatch):
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, *_args, **_kwargs):
            self.rows = 0

        def process_klines(self, df):
            self.rows += len(df)

        def get_bis(self):
            if self.rows < 2:
                return []
            return [SimpleNamespace(type="up")]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    main_dates = list(pd.date_range("2026-01-01 09:30:00", periods=12, freq="min", tz="UTC"))
    high_dates = list(pd.date_range("2026-01-01 09:30:00", periods=2, freq="5min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": high_dates,
            "open": np.ones(2),
            "high": np.ones(2),
            "low": np.ones(2),
            "close": np.ones(2),
            "volume": np.ones(2),
        }
    )

    dirs = live_backtest._walk_forward_trend_dir_by_main_bar(
        "QQQ.US",
        "5m",
        df,
        main_dates,
        available_delay=pd.Timedelta(minutes=5),
        warmup_bars=2,
    )

    assert dirs[:10] == ["neutral"] * 10
    assert dirs[10:] == ["up", "up"]


def test_native_5m_events_shift_to_closing_1m_bar_before_fill():
    from scripts.wf_native_recursive_context_tsla import _native_events_to_1m

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="min"))
    mapped = _native_events_to_1m([(dates[0], "buy")], dates)

    assert mapped[0]["bar"] == 4
    assert mapped[0]["anchor_time"] == str(dates[0])
    assert mapped[0]["visible_time"] == str(dates[4])


def test_build_symbol_walk_forward_uses_visible_new_signals(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [Signal(last, 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="5min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(6) * 10,
            "high": np.ones(6) * 11,
            "low": np.ones(6) * 9,
            "close": np.ones(6) * 10,
            "volume": np.ones(6),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="5m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
    )

    assert min(sym["small_by_bar"]) == 2
    assert [sig.date for sig in sym["small_by_bar"][2]] == [dates[2]]
    assert sym["signal_mode"] == "walk_forward"
    assert sym["signal_events"][0]["anchor_time"] == str(dates[2])
    assert sym["signal_events"][0]["visible_time"] == str(dates[2])
    assert sym["signal_events"][0]["next_fill_time"] == str(dates[3])
    assert sym["signal_events"][0]["anchor_to_visible_bars"] == 0


def test_build_symbol_walk_forward_keeps_prior_history_but_trades_window(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    chunk_lengths = []

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []
            self._last_mmd_sig = None

        def process_klines(self, df):
            chunk_lengths.append(len(df))
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("tail", str(self.dates[-1]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [Signal(last, 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.arange(10, dtype=float) + 10,
            "high": np.arange(10, dtype=float) + 11,
            "low": np.arange(10, dtype=float) + 9,
            "close": np.arange(10, dtype=float) + 10,
            "volume": np.ones(10),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        signal_mode="walk_forward",
        signal_warmup_bars=-1,
        trade_start=str(dates[5]),
        trade_end=str(dates[9]),
    )

    assert sym["dates"] == dates[5:10]
    assert list(sym["open"]) == pytest.approx(list(np.arange(5, 10, dtype=float) + 10))
    assert chunk_lengths[:6] == [1, 1, 1, 1, 1, 1]
    assert min(sym["small_by_bar"]) == 1
    assert sym["small_by_bar"][1][0].date == dates[6]


def test_walk_forward_preserves_initial_direction_without_reemitting_old_signal(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []
            self._last_mmd_sig = None

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("stable",)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="min", tz="UTC"))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        return [Signal(dates[1], 0, "3sell", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(8) * 10,
            "high": np.ones(8) * 11,
            "low": np.ones(8) * 9,
            "close": np.ones(8) * 10,
            "volume": np.ones(8),
        }
    )
    state = {}
    by_bar = live_backtest._walk_forward_signals_by_main_bar(
        "TSLA.US",
        "1m",
        df,
        dates[5:],
        available_delay=pd.Timedelta(0),
        warmup_bars=-1,
        initial_state_out=state,
        signal_cache_dir="",
    )

    assert by_bar == {}
    assert state["dir"] == "down"
    assert live_backtest._dir_series_from_signal_bars(
        by_bar,
        len(dates[5:]),
        initial_dir=state["dir"],
    ) == ["down", "down", "down"]


def test_walk_forward_full_history_registry_suppresses_stale_reappearing_signal(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []
            self._last_mmd_sig = None

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("rows", len(self.dates))

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=8, freq="min", tz="UTC"))
    stale = Signal(dates[2], 0, "3sell", 10.0)

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        n = len(cd.dates)
        if n in {3, 7}:
            return [stale]
        return []

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(8) * 10,
            "high": np.ones(8) * 11,
            "low": np.ones(8) * 9,
            "close": np.ones(8) * 10,
            "volume": np.ones(8),
        }
    )
    state = {}
    by_bar = live_backtest._walk_forward_signals_by_main_bar(
        "TSLA.US",
        "1m",
        df,
        dates,
        available_delay=pd.Timedelta(0),
        warmup_bars=-1,
        initial_state_out=state,
        signal_cache_dir="",
        emit_start_idx=5,
    )

    assert by_bar == {}
    assert state["dir"] == "neutral"


def test_build_symbol_walk_forward_signal_cache_reuses_visible_events(monkeypatch, tmp_path):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [Signal(last, 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="5min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(6) * 10,
            "high": np.ones(6) * 11,
            "low": np.ones(6) * 9,
            "close": np.ones(6) * 10,
            "volume": np.ones(6),
        }
    )

    first = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="5m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_cache_dir=str(tmp_path),
    )

    assert first["signal_cache_stats"]["misses"] == 1
    assert first["signal_cache_stats"]["writes"] == 1
    assert list(tmp_path.glob("*.pkl"))

    class ExplodingCL:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("cache hit should not instantiate CL")

    monkeypatch.setattr(live_backtest, "CL", ExplodingCL)
    second = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="5m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_cache_dir=str(tmp_path),
    )

    assert second["signal_cache_stats"]["hits"] == 1
    assert second["signal_cache_stats"]["misses"] == 0
    assert second["small_by_bar"].keys() == first["small_by_bar"].keys()
    assert [sig.date for sig in second["small_by_bar"][2]] == [dates[2]]


def test_build_symbol_walk_forward_can_use_segment_level_signals(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    calls = []

    class FakeCL:
        def __init__(self, *_args, **_kwargs):
            self.dates = []

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        calls.append(bool(use_xd))
        if not cd.dates:
            return []
        return [Signal(cd.dates[-1], 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="5min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(6) * 10,
            "high": np.ones(6) * 11,
            "low": np.ones(6) * 9,
            "close": np.ones(6) * 10,
            "volume": np.ones(6),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="5m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_unit="xd",
    )

    assert sym["signal_unit"] == "xd"
    assert calls and all(calls)


def test_build_symbol_walk_forward_can_use_recursive_upgrade_signals(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    calls = {"upgrade": 0}
    configs = []

    class FakeCL:
        def __init__(self, _code, _frequency, config):
            self.dates = []
            self._last_mmd_sig = None
            configs.append(dict(config))

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("tail", str(self.dates[-1]))

    def fake_collect_upgrade(cd):
        calls["upgrade"] += 1
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [
            Signal(last, 1, "3buy", 10.0),
            Signal(last, 2, "1sell", 11.0),
        ]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_upgrade_signals", fake_collect_upgrade)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(6) * 10,
            "high": np.ones(6) * 11,
            "low": np.ones(6) * 9,
            "close": np.ones(6) * 10,
            "volume": np.ones(6),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_source="upgrade",
        recursive_l0_min_zs_lines=3,
    )

    assert sym["signal_source"] == "upgrade"
    assert sym["recursive_l0_min_zs_lines"] == 3
    assert configs and all(cfg["recursive_l0_min_zs_lines"] == 3 for cfg in configs)
    assert all(cfg.get("skip_legacy_mmd") is True for cfg in configs)
    assert calls["upgrade"] >= 3
    assert [sig.level for sig in sym["small_by_bar"][2]] == [1, 2]
    assert [sig.level for sig in sym["mid_by_bar"][2]] == [1]
    assert sym["mid_dir_at"][1] == "up"
    assert sym["mid_dir_at"][2] == "up"
    assert sym["big_dir_at"][1] == "down"
    assert sym["big_dir_at"][2] == "down"


def test_build_symbol_walk_forward_can_include_l0_upgrade_scalp_signals(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    branch_calls = []

    class FakeCL:
        def __init__(self, _code, _frequency, config):
            self.dates = []
            self._last_mmd_sig = None
            self.config = dict(config)

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("tail", str(self.dates[-1]))

    def fake_collect_upgrade(cd):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [
            Signal(last, 1, "3buy", 10.0),
            Signal(last, 2, "1sell", 11.0),
        ]

    def fake_collect_branch(cd, use_xd=False, annotate_nest=False):
        branch_calls.append((use_xd, annotate_nest))
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [
            Signal(last, 0, "1buy", 9.0),
            Signal(last, 1, "3buy", 10.0),
        ]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_upgrade_signals", fake_collect_upgrade)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect_branch)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(6) * 10,
            "high": np.ones(6) * 11,
            "low": np.ones(6) * 9,
            "close": np.ones(6) * 10,
            "volume": np.ones(6),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_source="upgrade",
        recursive_l0_min_zs_lines=3,
        include_l0_upgrade_signals=True,
    )

    assert sym["include_l0_upgrade_signals"] is True
    assert branch_calls and all(call == (True, False) for call in branch_calls)
    assert [sig.level for sig in sym["small_by_bar"][2]] == [0, 1, 2]
    assert [sig.level for sig in sym["mid_by_bar"][2]] == [1]
    assert sym["big_dir_at"][2] == "down"


def test_walk_forward_skips_signal_collection_when_structure_signature_unchanged(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    calls = {"collect": 0}

    class StableCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []
            self._last_mmd_sig = ("stable",)

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("stable",)

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        calls["collect"] += 1
        return [Signal(cd.dates[-1], 0, "3buy", 10.0)] if cd.dates else []

    monkeypatch.setattr(live_backtest, "CL", StableCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=20, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(20) * 10,
            "high": np.ones(20) * 11,
            "low": np.ones(20) * 9,
            "close": np.ones(20) * 10,
            "volume": np.ones(20),
        }
    )

    live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        signal_mode="walk_forward",
        signal_warmup_bars=1,
    )

    assert calls["collect"] == 1


def test_walk_forward_chunked_scan_matches_continuous_scan(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []
            self._last_mmd_sig = None

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))
            self._last_mmd_sig = ("tail", str(self.dates[-1]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [Signal(last, 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=20, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(20) * 10,
            "high": np.ones(20) * 11,
            "low": np.ones(20) * 9,
            "close": np.ones(20) * 10,
            "volume": np.ones(20),
        }
    )

    continuous = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
    )
    chunked = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df,
        op_level="1m",
        signal_mode="walk_forward",
        signal_warmup_bars=2,
        signal_scan_chunk_bars=5,
    )

    assert chunked["signal_scan_chunk_bars"] == 5
    assert sorted(chunked["small_by_bar"]) == sorted(continuous["small_by_bar"])
    assert [sig.date for sig in chunked["small_by_bar"][5]] == [dates[5]]


def test_build_symbol_walk_forward_delays_higher_level_to_main_clock(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    class FakeCL:
        def __init__(self, _code, frequency, _config):
            self.frequency = frequency
            self.dates = []

        def process_klines(self, df):
            self.dates.extend(list(df["date"]))

    def fake_collect(cd, use_xd=False, annotate_nest=False):
        if not cd.dates:
            return []
        last = cd.dates[-1]
        return [Signal(last, 0, "3buy", 10.0)]

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "collect_branch_signals", fake_collect)

    main_dates = list(pd.date_range("2026-01-01 09:30:00", periods=260, freq="min", tz="UTC"))
    mid_dates = list(pd.date_range(main_dates[0], periods=52, freq="5min", tz="UTC"))
    df_main = pd.DataFrame(
        {
            "date": main_dates,
            "open": np.ones(len(main_dates)) * 10,
            "high": np.ones(len(main_dates)) * 11,
            "low": np.ones(len(main_dates)) * 9,
            "close": np.ones(len(main_dates)) * 10,
            "volume": np.ones(len(main_dates)),
        }
    )
    df_mid = pd.DataFrame(
        {
            "date": mid_dates,
            "open": np.ones(len(mid_dates)) * 10,
            "high": np.ones(len(mid_dates)) * 11,
            "low": np.ones(len(mid_dates)) * 9,
            "close": np.ones(len(mid_dates)) * 10,
            "volume": np.ones(len(mid_dates)),
        }
    )

    sym = live_backtest.build_symbol_from_klines(
        "us",
        "TSLA.US",
        df_main,
        op_level="1m",
        df_mid=df_mid,
        mid_level="5m",
        signal_mode="walk_forward",
        signal_warmup_bars=1,
    )

    assert min(sym["mid_by_bar"]) == 10
    assert sym["mid_by_bar"][10][0].date == mid_dates[1]
    assert sym["mid_dir_at"][9] == "up"
    assert sym["mid_dir_at"][10] == "up"


def test_slice_df_for_signal_window_keeps_only_prior_warmup_and_no_future():
    from chanlun.recursive_bt.live_backtest import _slice_df_for_signal_window

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.arange(10, dtype=float),
            "high": np.arange(10, dtype=float),
            "low": np.arange(10, dtype=float),
            "close": np.arange(10, dtype=float),
            "volume": np.ones(10),
        }
    )

    sliced = _slice_df_for_signal_window(
        df,
        start="2026-01-01 09:35:00+00:00",
        end="2026-01-01 09:37:00+00:00",
        warmup_bars=2,
    )

    assert list(sliced["date"]) == dates[3:8]
    assert sliced["date"].min() == dates[3]
    assert sliced["date"].max() == dates[7]


def test_slice_df_for_signal_window_negative_warmup_keeps_all_prior_history():
    from chanlun.recursive_bt.live_backtest import _slice_df_for_signal_window

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=10, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.arange(10, dtype=float),
            "high": np.arange(10, dtype=float),
            "low": np.arange(10, dtype=float),
            "close": np.arange(10, dtype=float),
            "volume": np.ones(10),
        }
    )

    sliced = _slice_df_for_signal_window(
        df,
        start="2026-01-01 09:35:00+00:00",
        end="2026-01-01 09:37:00+00:00",
        warmup_bars=-1,
    )

    assert list(sliced["date"]) == dates[:8]
    assert sliced["date"].max() == dates[7]


def test_load_chart_cache_syms_slices_requested_window_before_signal_scan(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    captured = {}
    dates = list(pd.date_range("2026-01-01 09:30:00", periods=150, freq="min", tz="UTC"))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": np.ones(150) * 10,
            "high": np.ones(150) * 11,
            "low": np.ones(150) * 9,
            "close": np.ones(150) * 10,
            "volume": np.ones(150),
        }
    )

    def fake_load_chart_cache_klines(_market, _code, _freq, _cache_dir):
        return df.copy()

    def fake_build_symbol_from_klines(_market, code, df_op, df_big=None, **kwargs):
        captured[code] = {
            "op_dates": list(df_op["date"]),
            "big_dates": list(df_big["date"]),
            "kwargs": kwargs,
        }
        return {"dates": list(df_op["date"])}

    monkeypatch.setattr(live_backtest, "load_chart_cache_klines", fake_load_chart_cache_klines)
    monkeypatch.setattr(live_backtest, "build_symbol_from_klines", fake_build_symbol_from_klines)

    syms = live_backtest.load_chart_cache_syms(
        "us",
        ["TSLA.US"],
        cache_dir="unused",
        pool_size=1,
        op_level="1m",
        big_level="30m",
        signal_warmup_bars=100,
        start=str(dates[120]),
        end=str(dates[140]),
    )

    assert "TSLA.US" in syms
    assert captured["TSLA.US"]["op_dates"] == dates[20:141]
    assert captured["TSLA.US"]["big_dates"] == dates[20:141]


def test_live_backtest_rejects_walk_forward_signal_mode_on_bt_data():
    from types import SimpleNamespace
    from chanlun.recursive_bt import live_backtest

    args = SimpleNamespace(
        market="a",
        codes=None,
        source="bt_data",
        signal_mode="walk_forward",
        signal_warmup_bars=1,
        require=("tech",),
    )

    with pytest.raises(ValueError, match="requires raw klines"):
        live_backtest.run_backtest(args)


def test_live_backtest_cli_defaults_to_walk_forward_signal_mode():
    from chanlun.recursive_bt.live_backtest import (
        _cl_config,
        _signal_cache_meta,
        make_arg_parser,
    )

    args = make_arg_parser().parse_args([])

    assert args.signal_mode == "walk_forward"
    assert args.signal_cache_dir.endswith("live_backtest_signal_cache")
    assert args.signal_scan_chunk_bars == 0
    assert args.recursive_l0_min_zs_lines == 3

    cfg = _cl_config()
    assert cfg["recursive_l0_min_zs_lines"] == 3

    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01 09:30:00", periods=2, freq="min", tz="UTC"),
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        }
    )
    meta = _signal_cache_meta(
        "TSLA.US",
        "1m",
        df,
        list(df["date"]),
        available_delay=pd.Timedelta("0min"),
        warmup_bars=-1,
        annotate_nest=False,
    )
    assert meta["recursive_l0_min_zs_lines"] == 3


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

    def fake_load_chart_cache_syms(*_args, **kwargs):
        calls["include_l0_upgrade_signals"] = kwargs["include_l0_upgrade_signals"]
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
        calls["sell_ratio_policy"] = kwargs["sell_ratio_policy"]
        calls["after_3sell_reentry_buy_classes"] = kwargs["after_3sell_reentry_buy_classes"]
        calls["after_3sell_reentry_mid_buy_classes"] = kwargs[
            "after_3sell_reentry_mid_buy_classes"
        ]
        calls["trend_core_hold_ratio"] = kwargs["trend_core_hold_ratio"]
        calls["core_signal_level"] = kwargs["core_signal_level"]
        calls["swing_signal_level"] = kwargs["swing_signal_level"]
        calls["big_down_activity_buy_ratio_multiplier"] = kwargs[
            "big_down_activity_buy_ratio_multiplier"
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
        signal_source="upgrade",
        include_l0_upgrade_signals=True,
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
        sell_ratio_policy="original_layered",
        after_3sell_reentry_buy_classes=(3,),
        after_3sell_reentry_mid_buy_classes=(3,),
        trend_core_hold_ratio=0.5,
        big_down_activity_buy_ratio_multiplier=0.25,
        init_cash=1_000_000,
        bs_point_ratio_overrides_enabled=True,
        bs_point_ratio_overrides_json=str(overrides),
    )

    live_backtest.run_backtest(args)

    assert calls["multipliers"] == {"3": 1.1}
    assert calls["sell_classes"] == {1, 2, 3}
    assert calls["sell_ratio_overrides"] == {"3": 0.5}
    assert calls["sell_ratio_override_scope"] == "up"
    assert calls["sell_ratio_policy"] == "original_layered"
    assert calls["after_3sell_reentry_buy_classes"] == {3}
    assert calls["after_3sell_reentry_mid_buy_classes"] == {3}
    assert calls["trend_core_hold_ratio"] == pytest.approx(0.5)
    assert calls["core_signal_level"] == 2
    assert calls["swing_signal_level"] == 1
    assert calls["include_l0_upgrade_signals"] is True
    assert calls["big_down_activity_buy_ratio_multiplier"] == pytest.approx(0.25)
    assert args.bs_point_ratio_multipliers == {"3": 1.1}


def test_walk_forward_dedupes_reappearing_signal_identity(monkeypatch):
    from chanlun.recursive_bt import live_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=5, freq="min", tz="UTC"))
    df = pd.DataFrame({
        "date": dates,
        "open": np.full(len(dates), 10.0),
        "high": np.full(len(dates), 10.0),
        "low": np.full(len(dates), 10.0),
        "close": np.full(len(dates), 10.0),
        "volume": np.full(len(dates), 100.0),
    })
    sig = Signal(dates[1], 1, "3sell", 10.0)
    snapshots = [[], [sig], [], [sig], []]

    class FakeCL:
        def __init__(self, *_args, **_kwargs):
            self.seen_rows = 0

        def process_klines(self, rows):
            self.seen_rows += len(rows)

    def fake_collect(cd, **_kwargs):
        return list(snapshots[min(cd.seen_rows - 1, len(snapshots) - 1)])

    monkeypatch.setattr(live_backtest, "CL", FakeCL)
    monkeypatch.setattr(live_backtest, "_collection_state_signature", lambda cd, _source: cd.seen_rows)
    monkeypatch.setattr(live_backtest, "_collect_visible_signals", fake_collect)

    by_bar = live_backtest._walk_forward_signals_by_main_bar(
        "TSLA.US",
        "1m",
        df,
        dates,
        available_delay=pd.Timedelta("0min"),
        warmup_bars=0,
        signal_source="upgrade",
        signal_cache_dir="",
    )

    assert by_bar == {1: [sig]}


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
        "dates": list(pd.date_range("2026-01-05", periods=3, freq="1D", tz="Asia/Shanghai")),
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


def test_portfolio_backtest_trend_core_holds_through_small_sells_until_big_down():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=9, freq="5min", tz="Asia/Shanghai"))
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
                5: [Signal(dates[5], 0, "1sell", 10.0)],
            },
            "big_dir_at": ["up", "up", "up", "up", "up", "up", "down", "down", "down"],
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="trend-core",
        trend_core_hold_ratio=0.5,
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].reason == "small_level_sell_point"
    assert trades[0].sell_ratio == pytest.approx(0.5)
    assert trades[1].reason == "big_level_down"
    assert trades[1].sell_ratio == pytest.approx(1.0)
    assert trades[0].shares == pytest.approx(trades[1].shares)


def test_portfolio_backtest_trend_core_refills_activity_on_later_buy_point():
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
            },
            "big_dir_at": ["up", "up", "up", "up", "up", "up", "up", "up", "down", "down"],
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="trend-core-refill",
        trend_core_hold_ratio=0.5,
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].reason == "small_level_sell_point"
    assert trades[0].sell_ratio == pytest.approx(0.5)
    assert trades[1].reason == "big_level_down"
    assert trades[1].sell_ratio == pytest.approx(1.0)
    assert trades[1].shares == pytest.approx(trades[0].shares * 2)


def test_portfolio_backtest_core_signal_level_exits_trend_core():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=9, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 2, "3buy", 10.0)],
                3: [Signal(dates[3], 1, "3sell", 10.0)],
                5: [Signal(dates[5], 2, "3sell", 10.0)],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="core-level-exit",
        trend_core_hold_ratio=0.5,
        core_signal_level=2,
        swing_signal_level=1,
        sell_ratio_overrides={"3": 0.25},
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].reason == "small_level_sell_point"
    assert trades[0].sell_ratio == pytest.approx(0.25)
    assert trades[0].entry_level == 2
    assert trades[0].exit_level == 1
    assert trades[0].entry_layer == "core_swing"
    assert trades[0].exit_layer == "swing"
    assert trades[0].core_shares_before > 0
    assert trades[0].activity_shares_before > 0
    assert trades[0].swing_shares_before > 0
    assert trades[0].scalp_shares_before == pytest.approx(0.0)
    assert trades[1].reason == "big_level_sell_point"
    assert trades[1].sell_ratio == pytest.approx(1.0)
    assert trades[1].entry_level == 2
    assert trades[1].exit_level == 2
    assert trades[1].exit_layer == "core_all"
    assert trades[1].shares > trades[0].shares


def test_portfolio_backtest_low_level_buy_does_not_create_core_when_core_level_set():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=7, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 1, "3buy", 10.0)],
                3: [Signal(dates[3], 1, "3sell", 10.0)],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="low-level-no-core",
        trend_core_hold_ratio=0.5,
        core_signal_level=2,
        swing_signal_level=1,
        sell_ratio_overrides={"3": 0.25},
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].entry_level == 1
    assert trades[0].entry_layer == "swing"
    assert trades[0].core_shares_before == pytest.approx(0.0)
    assert trades[0].activity_shares_before == pytest.approx(trades[0].shares * 4)
    assert trades[0].swing_shares_before == pytest.approx(trades[0].activity_shares_before)
    assert trades[0].scalp_shares_before == pytest.approx(0.0)
    assert trades[1].reason == "final_close"
    assert trades[1].core_shares_before == pytest.approx(0.0)


def test_portfolio_backtest_scalp_sell_does_not_sell_swing_layer():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=7, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 1, "3buy", 10.0)],
                3: [Signal(dates[3], 0, "3sell", 10.0)],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="scalp-sell-keeps-swing",
        core_signal_level=2,
        swing_signal_level=1,
    )
    trades = result["trades"]

    assert len(trades) == 1
    assert trades[0].reason == "final_close"
    assert trades[0].entry_layer == "swing"
    assert trades[0].swing_shares_before > 0
    assert trades[0].scalp_shares_before == pytest.approx(0.0)


def test_portfolio_backtest_same_bar_scalp_sell_rolls_into_higher_level_buy():
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
                3: [
                    Signal(dates[3], 0, "3sell", 10.0),
                    Signal(dates[3], 0, "3buy", 10.0),
                    Signal(dates[3], 1, "3buy", 10.0),
                ],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="scalp-to-swing-roll",
        core_signal_level=2,
        swing_signal_level=1,
    )
    trades = result["trades"]

    assert len(trades) == 2
    assert trades[0].entry_date == dates[2]
    assert trades[0].exit_date == dates[4]
    assert trades[0].reason == "small_level_sell_point"
    assert trades[0].exit_bs_type == "3sell"
    assert trades[0].entry_layer == "scalp"
    assert trades[0].exit_layer == "scalp"
    assert trades[0].entry_level == 0
    assert trades[0].exit_level == 0
    assert trades[0].sell_ratio == pytest.approx(1.0)
    assert trades[1].entry_date == dates[4]
    assert trades[1].reason == "final_close"
    assert trades[1].bs_type == "3"
    assert trades[1].entry_layer == "swing"
    assert trades[1].entry_level == 1
    assert trades[1].swing_shares_before > 0
    assert trades[1].scalp_shares_before == pytest.approx(0.0)


def test_portfolio_backtest_cancels_buy_when_fill_breaks_structural_boundary():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.array([10.0, 10.0, 9.8, 10.0, 10.0, 10.0]),
            "close": np.array([10.0, 10.2, 9.8, 10.0, 10.0, 10.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(
                    dates[1],
                    1,
                    "3buy",
                    10.2,
                    structural_stop_below=10.0,
                    zs_zg=10.0,
                )],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="cancel-invalid-3buy",
        core_signal_level=2,
        swing_signal_level=1,
    )

    assert result["trades"] == []


def test_portfolio_backtest_exits_next_bar_after_structural_invalidation():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=6, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.2, 9.8, 10.0, 10.0]),
            "high": np.array([10.1, 10.3, 10.3, 10.0, 10.0, 10.0]),
            "low": np.array([9.9, 10.1, 9.9, 9.7, 10.0, 10.0]),
            "close": np.array([10.0, 10.2, 10.1, 9.9, 10.0, 10.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(
                    dates[1],
                    1,
                    "3buy",
                    10.2,
                    structural_stop_below=10.0,
                    zs_zg=10.0,
                )],
            },
            "big_dir_at": ["up"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="exit-invalid-3buy",
        core_signal_level=2,
        swing_signal_level=1,
    )
    trades = result["trades"]

    assert len(trades) == 1
    assert trades[0].entry_date == dates[2]
    assert trades[0].exit_date == dates[3]
    assert trades[0].reason == "structural_invalidation"
    assert trades[0].exit_bs_type == "structural_stop_below"
    assert trades[0].exit_layer == "all"
    assert trades[0].entry_layer == "swing"
    assert trades[0].ret == pytest.approx(9.8 / 10.2 - 1)


def test_portfolio_backtest_blocks_big_down_activity_by_default():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=7, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.0, 10.0, 11.0, 11.0, 11.0]),
            "close": np.array([10.0, 10.0, 10.0, 10.0, 11.0, 11.0, 11.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 1, "3buy", 10.0)],
                4: [Signal(dates[4], 1, "3sell", 11.0)],
            },
            "big_dir_at": ["down"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="strict-big-down",
        core_signal_level=2,
    )

    assert result["trades"] == []


def test_portfolio_backtest_allows_lower_level_activity_in_big_down_when_enabled():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=7, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.array([10.0, 10.0, 10.0, 10.0, 11.0, 11.0, 11.0]),
            "close": np.array([10.0, 10.0, 10.0, 10.0, 11.0, 11.0, 11.0]),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 1, "3buy", 10.0)],
                4: [Signal(dates[4], 1, "3sell", 11.0)],
            },
            "big_dir_at": ["down"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="big-down-activity",
        core_signal_level=2,
        swing_signal_level=1,
        big_down_activity_buy_ratio_multiplier=0.25,
    )
    trades = result["trades"]

    assert len(trades) == 1
    assert trades[0].entry_date == dates[2]
    assert trades[0].exit_date == dates[5]
    assert trades[0].bs_type == "3"
    assert trades[0].reason == "small_level_sell_point"
    assert trades[0].exit_bs_type == "3sell"
    assert trades[0].buy_ratio == pytest.approx(0.25)
    assert trades[0].entry_level == 1
    assert trades[0].exit_level == 1
    assert trades[0].entry_layer == "swing"
    assert trades[0].exit_layer == "swing"
    assert trades[0].core_shares_before == pytest.approx(0.0)
    assert trades[0].swing_shares_before == pytest.approx(trades[0].shares)
    assert trades[0].scalp_shares_before == pytest.approx(0.0)
    assert trades[0].ret == pytest.approx(0.1)


def test_portfolio_backtest_preserves_buy_ratio_on_final_close():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    dates = list(pd.date_range("2026-01-01 09:30:00", periods=5, freq="5min", tz="Asia/Shanghai"))
    syms = {
        "S": {
            "code": "QQQ.US",
            "dates": dates,
            "open": np.full(len(dates), 10.0),
            "close": np.full(len(dates), 10.0),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {
                1: [Signal(dates[1], 1, "3buy", 10.0)],
            },
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("US", commission=0.0, stamp_duty=0.0, t_plus=0, lot=1),
        }
    }

    result = portfolio_backtest(
        syms=syms,
        max_pos=1,
        require=("tech",),
        label="final-buy-ratio",
    )

    assert len(result["trades"]) == 1
    assert result["trades"][0].reason == "final_close"
    assert result["trades"][0].buy_ratio == pytest.approx(1.0)
    assert result["trades"][0].entry_level == 1
    assert result["trades"][0].entry_layer == "activity"
    assert result["trades"][0].exit_layer == "all"


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


def test_prev_day_close_series_minute_and_daily_bars():
    from chanlun.recursive_bt.engine import prev_day_close_series

    minute_dates = list(
        pd.date_range("2026-01-05 09:30:00", periods=3, freq="5min", tz="Asia/Shanghai")
    ) + list(
        pd.date_range("2026-01-06 09:30:00", periods=3, freq="5min", tz="Asia/Shanghai")
    )
    minute_closes = [10.0, 10.1, 10.2, 11.0, 11.1, 11.2]
    out = prev_day_close_series(minute_dates, minute_closes)
    # 首日三根无昨收;次日三根的昨收都=首日最后一根 10.2
    assert all(np.isnan(out[:3]))
    assert list(out[3:]) == [10.2, 10.2, 10.2]

    daily_dates = list(pd.date_range("2026-01-05", periods=3, freq="1D", tz="Asia/Shanghai"))
    daily_closes = [10.0, 10.5, 11.0]
    daily = prev_day_close_series(daily_dates, daily_closes)
    assert np.isnan(daily[0])
    assert list(daily[1:]) == [10.0, 10.5]


def test_latest_prev_day_close_uses_previous_session():
    from chanlun.recursive_bt.engine import latest_prev_day_close

    df = pd.DataFrame(
        {
            "date": list(
                pd.date_range("2026-01-05 09:30:00", periods=3, freq="5min", tz="Asia/Shanghai")
            )
            + list(
                pd.date_range("2026-01-06 09:30:00", periods=2, freq="5min", tz="Asia/Shanghai")
            ),
            "close": [10.0, 10.1, 10.2, 11.0, 11.1],
        }
    )
    assert latest_prev_day_close(df) == pytest.approx(10.2)
    same_day = df.iloc[3:].reset_index(drop=True)
    assert latest_prev_day_close(same_day) == 0.0


def test_market_rules_for_code_st_main_board_uses_5pct():
    from chanlun.recursive_bt.market_runtime import market_rules_for_code

    assert market_rules_for_code("a", "SH.600000", name="ST海航").limit_pct == 0.05
    assert market_rules_for_code("a", "SZ.000001", name="*ST凯撒").limit_pct == 0.05
    # 主板非 ST 仍 10%
    assert market_rules_for_code("a", "SH.600000", name="浦发银行").limit_pct == 0.10
    # 创业板/科创板 ST 涨跌幅仍 20%,北交所无 ST 制度
    assert market_rules_for_code("a", "SZ.300001", name="ST特锐德").limit_pct == 0.20
    assert market_rules_for_code("a", "SH.688001", name="*ST华兴").limit_pct == 0.20
    assert market_rules_for_code("a", "BJ.920111", name="ST某某").limit_pct == 0.30


def test_build_st_list_filters_main_board_st_only(tmp_path):
    from chanlun.recursive_bt.fetch import build_st_list

    names = {
        "SH.600001": "ST海航",
        "SH.600002": "浦发银行",
        "SZ.000003": "*ST凯撒",
        "SZ.300001": "ST特锐德",   # 创业板 ST 仍 20%,不入名单
        "BJ.920111": "ST北证",     # 北交所无 ST 制度,不入名单
    }
    out_path = tmp_path / "_st_list.json"

    result = build_st_list(
        str(out_path),
        codes=list(names),
        detail_fn=lambda code: {"InstrumentName": names[code]},
    )

    assert result == {"SH.600001": "ST海航", "SZ.000003": "*ST凯撒"}
    assert json.loads(out_path.read_text(encoding="utf-8")) == result


def test_load_cached_applies_st_limit_from_list(tmp_path, monkeypatch):
    from chanlun.recursive_bt import portfolio as portfolio_mod
    from chanlun.recursive_bt import market_runtime as runtime_mod

    dates = list(pd.date_range("2026-01-05", periods=3, freq="1D", tz="Asia/Shanghai"))
    payload = {
        "code": "SH.600001",
        "dates": dates,
        "open": np.full(3, 10.0),
        "close": np.full(3, 10.0),
        "small_by_bar": {},
        "big_dir_at": ["neutral"] * 3,
        "limit_pct": 0.10,
    }
    with open(tmp_path / "SH.600001.pkl", "wb") as fp:
        pickle.dump(payload, fp)
    with open(tmp_path / "SH.600002.pkl", "wb") as fp:
        pickle.dump({**payload, "code": "SH.600002"}, fp)
    (tmp_path / "_st_list.json").write_text(
        json.dumps({"SH.600001": "ST示例"}), encoding="utf-8"
    )

    monkeypatch.setattr(portfolio_mod, "BT_DATA", str(tmp_path))
    monkeypatch.setattr(runtime_mod, "_ST_LIST_CACHE", None)
    monkeypatch.setattr(runtime_mod, "BT_DATA_DIR", str(tmp_path))

    st = portfolio_mod.load_cached("SH.600001")
    normal = portfolio_mod.load_cached("SH.600002")

    assert st["rules"].limit_pct == 0.05
    assert normal["rules"].limit_pct == 0.10


def test_portfolio_backtest_limit_lock_blocks_intraday_limit_up_buy():
    from chanlun.recursive_bt.engine import MarketRules
    from chanlun.recursive_bt.portfolio import portfolio_backtest

    # 两个交易日的 5m bar:day2 盘中逐步推到涨停(昨收 10.0 → 11.0 封板)。
    # 信号在 10.8 的 bar,挂单在封板 bar 开盘 11.0:相对前一根 bar 只 +1.9%,
    # 但相对**昨日收盘**是 +10% 涨停——实盘买不进,回测必须锁死。
    # 旧实现用前一根 bar 收盘判定,会在涨停价照常成交。
    dates = list(
        pd.date_range("2026-01-05 09:30:00", periods=4, freq="5min", tz="Asia/Shanghai")
    ) + list(
        pd.date_range("2026-01-06 09:30:00", periods=4, freq="5min", tz="Asia/Shanghai")
    )
    px = np.array([10.0, 10.0, 10.0, 10.0, 10.5, 10.8, 11.0, 11.0])
    syms = {
        "A": {
            "code": "SH.600000",
            "dates": dates,
            "open": px.copy(),
            "close": px.copy(),
            "d2i": {d: i for i, d in enumerate(dates)},
            "small_by_bar": {5: [Signal(dates[5], 0, "3buy", 10.8)]},
            "big_dir_at": ["neutral"] * len(dates),
            "rules": MarketRules("A", commission=0.0, stamp_duty=0.0, t_plus=1, lot=100,
                                 limit_pct=0.10),
        }
    }

    result = portfolio_backtest(syms=syms, max_pos=1, require=("tech",), label="limit-up-buy")

    assert result["trades"] == []
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
