"""Round 3 监控/paper 修复补丁的单元测试。

覆盖 audit/_round3_fix_design_monitor.md 每条修复列出的单测:
- F-HIGH-1 drop_unclosed_last_bar 纯函数(naive/aware/日线/不足两根/间隔异常/进行中→裁/已收盘→留)
- F-HIGH-2 dedup identity 去 reason
- F-MED-4 ledger 截断容错读 + 原子往返
- F-MED-1 freshness 按 op 级别
- F-MED-5 refresh 失败计数熔断
- F-LOW-3 equity 裁剪后 total_return/max_dd 不变
- F-LOW-4 regime 跨日清理
- F-LOW-5 now_trading 兜底
- F-LOW-6 initial_codes selection 可淘汰

全部用 stub/mock 隔离真实 exchange 与缠论计算,只验证补丁逻辑本身。
"""
from __future__ import annotations

import datetime as _dt
import json

import pandas as pd
import pytest

from chanlun.recursive_bt.sim.paper import (
    INIT_CASH,
    PaperBroker,
    SymbolState,
    _freq_to_minutes,
    drop_unclosed_last_bar,
)
from chanlun.recursive_bt.monitor import live_monitor as lm
from chanlun.recursive_bt.monitor.live_monitor import (
    JsonDeduper,
    MonitorEvent,
    MonitorSymbolState,
    _REGIME_CACHE,
    _us_session_fallback,
    collect_monitor_events,
    current_visible_regime,
    fresh_monitor_events,
    market_is_open,
    rescan_selection_pool,
    unique_monitor_events,
)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _df_from_starts(starts) -> pd.DataFrame:
    """用一串 bar 起点构造最小 K 线 df(只需 date/open/close 列)。"""
    return pd.DataFrame(
        {
            "date": list(starts),
            "open": [1.0] * len(starts),
            "close": [1.0] * len(starts),
        }
    )


# ==========================================================================
# F-HIGH-1: drop_unclosed_last_bar
# ==========================================================================
class TestDropUnclosedLastBar:
    def test_freq_to_minutes(self):
        assert _freq_to_minutes("5m") == 5
        assert _freq_to_minutes("1m") == 1
        assert _freq_to_minutes("30m") == 30
        assert _freq_to_minutes("d") is None
        assert _freq_to_minutes("w") is None
        assert _freq_to_minutes("xm") is None  # 非法分钟数

    def test_in_progress_naive_trimmed(self):
        """A股口径(naive):末根仍在进行 → 裁掉。"""
        step = pd.Timedelta(minutes=5)
        now = pd.Timestamp.now()
        # 末根起点 = 当前周期起点(now 落在 [last, last+step) 内 → 未收盘)
        last = now.floor("5min")
        if last == now:  # 边界:正好整点,退一格保证 now 在区间内部
            last = last - step
        prev = last - step
        df = _df_from_starts([prev - step, prev, last])
        out = drop_unclosed_last_bar(df, "5m")
        assert len(out) == len(df) - 1
        assert out["date"].iloc[-1] == prev

    def test_in_progress_aware_trimmed(self):
        """美股/港股口径(aware, Asia/Shanghai):末根进行中 → 裁掉,且不抛 TypeError。"""
        tz = "Asia/Shanghai"
        step = pd.Timedelta(minutes=5)
        now = pd.Timestamp.now(tz=tz)
        last = now.floor("5min")
        if last == now:
            last = last - step
        prev = last - step
        df = _df_from_starts([prev - step, prev, last])
        out = drop_unclosed_last_bar(df, "5m")
        assert len(out) == len(df) - 1
        assert out["date"].iloc[-1] == prev

    def test_closed_last_bar_kept(self):
        """末根已收盘(now 远超末根收盘时刻)→ 原样保留。"""
        step = pd.Timedelta(minutes=5)
        now = pd.Timestamp.now()
        last = now.floor("5min") - pd.Timedelta(hours=2)  # 两小时前,早已收盘
        prev = last - step
        df = _df_from_starts([prev - step, prev, last])
        out = drop_unclosed_last_bar(df, "5m")
        assert len(out) == len(df)
        assert out["date"].iloc[-1] == last

    def test_daily_never_trimmed(self):
        """日线非分钟级 → helper no-op,绝不裁(即便末根是今天)。"""
        today = pd.Timestamp(_dt.date.today())
        df = _df_from_starts([today - pd.Timedelta(days=2),
                              today - pd.Timedelta(days=1), today])
        out = drop_unclosed_last_bar(df, "d")
        assert len(out) == len(df)

    def test_too_few_bars_returned_as_is(self):
        """不足两根 → 无法推断间隔,原样返回。"""
        now = pd.Timestamp.now().floor("5min")
        df1 = _df_from_starts([now])
        assert len(drop_unclosed_last_bar(df1, "5m")) == 1
        df0 = _df_from_starts([])
        assert len(drop_unclosed_last_bar(df0, "5m")) == 0

    def test_irregular_interval_not_trimmed(self):
        """末根与次末根间隔 != 名义周期(节假日跳空)→ 不裁,避免误删历史正常 bar。"""
        now = pd.Timestamp.now()
        last = now.floor("5min")
        if last == now:
            last = last - pd.Timedelta(minutes=5)
        # 次末根与末根间隔 = 35min(跨日跳空),非 5min
        prev = last - pd.Timedelta(minutes=35)
        df = _df_from_starts([prev - pd.Timedelta(minutes=5), prev, last])
        out = drop_unclosed_last_bar(df, "5m")
        assert len(out) == len(df)  # 间隔异常,即便末根"看起来"在进行中也不裁

    def test_none_df_returned_as_is(self):
        assert drop_unclosed_last_bar(None, "5m") is None


# ==========================================================================
# F-HIGH-2: dedup identity 去 reason
# ==========================================================================
class TestIdentityDropsReason:
    def _evt(self, reason: str) -> MonitorEvent:
        return MonitorEvent(
            code="SH.600000",
            name="x",
            side="buy",
            kind="small_buy",
            bs_type="1buy",
            signal_time="2026-06-25 10:00:00",
            price=10.0,
            big_dir="up",
            reason=reason,
        )

    def test_identity_excludes_reason(self):
        a = self._evt("5m buy point + 30m not_down")
        b = self._evt("5m buy point + 30m not_down + regime_bear_x1.25")
        assert a.identity == b.identity
        # identity 仍含稳定字段
        assert a.identity == "small_buy|SH.600000|1buy|2026-06-25 10:00:00"

    def test_unique_monitor_events_dedups_by_identity(self):
        a = self._evt("reason A")
        b = self._evt("reason B + bs1_ratio_x1.40")
        uniq = unique_monitor_events([a, b])
        assert len(uniq) == 1

    def test_fresh_monitor_events_dedups_second(self, tmp_path):
        deduper = JsonDeduper(tmp_path / "state.json")
        a = self._evt("reason A")
        b = self._evt("reason B")
        # 第一个未见 → 返回;mark 后第二个(仅 reason 不同)应被去重
        first = fresh_monitor_events([a], deduper)
        assert len(first) == 1
        deduper.mark(first)
        second = fresh_monitor_events([b], deduper)
        assert second == []


# ==========================================================================
# F-MED-4: ledger 原子写 + 容错读
# ==========================================================================
class TestLedgerAtomicAndTolerantLoad:
    def test_truncated_ledger_starts_fresh(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text('{"cash": 100', encoding="utf-8")  # 截断的 JSON
        broker = PaperBroker(str(path))
        assert broker.cash == INIT_CASH
        assert broker.positions == {}
        assert (tmp_path / "ledger.json.corrupt").exists()

    def test_non_dict_ledger_starts_fresh(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")  # 合法 JSON 但非 dict
        broker = PaperBroker(str(path))
        assert broker.cash == INIT_CASH
        assert (tmp_path / "ledger.json.corrupt").exists()

    def test_missing_keys_tolerated(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text('{"positions": {}}', encoding="utf-8")  # 缺 cash 等
        broker = PaperBroker(str(path))
        assert broker.cash == INIT_CASH
        assert broker.positions == {}
        assert broker.pending == []
        assert broker.trades == []
        assert broker.equity_curve == []

    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "ledger.json"
        broker = PaperBroker(str(path))
        broker.cash = 888888.0
        broker.positions = {"SH.600000": {"shares": 100, "entry_px": 10.0,
                                          "entry_date": "2026-06-25", "bs": "1"}}
        broker.trades = [{"code": "SH.600000", "ret": 0.05}]
        broker.equity_curve = [{"time": "t", "equity": 1000000.0}]
        broker.save()
        # 落盘内容应是完整合法 JSON(原子写不留半截)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["cash"] == 888888.0
        assert "SH.600000" in data["positions"]
        # 再 load 回来一致
        broker2 = PaperBroker(str(path))
        assert broker2.cash == 888888.0
        assert broker2.positions["SH.600000"]["shares"] == 100
        assert broker2.trades[0]["ret"] == 0.05

    def test_atomic_write_no_tmp_left(self, tmp_path):
        path = tmp_path / "ledger.json"
        broker = PaperBroker(str(path))
        broker.save()
        assert not (tmp_path / "ledger.json.tmp").exists()  # tmp 已 replace 掉


# ==========================================================================
# F-MED-1: freshness 按 op 级别
# ==========================================================================
class TestFreshnessWindow:
    def test_monitor_1m_floor_60min(self):
        s = MonitorSymbolState("SH.600000", None, op_level="1m")
        assert s.signal_freshness == pd.Timedelta(minutes=60)

    def test_monitor_5m_200min(self):
        s = MonitorSymbolState("SH.600000", None, op_level="5m")
        assert s.signal_freshness == pd.Timedelta(minutes=200)

    def test_monitor_30m_scales(self):
        s = MonitorSymbolState("SH.600000", None, op_level="30m")
        assert s.signal_freshness == pd.Timedelta(minutes=1200)  # 30*40

    def test_paper_symbolstate_200min(self):
        sp = SymbolState("SH.600000", None)
        assert sp.signal_freshness == pd.Timedelta(minutes=200)


# ==========================================================================
# F-MED-5: refresh 失败计数熔断
# ==========================================================================
class _FlakyState:
    """refresh 前 n 次抛异常,之后成功返回空。"""

    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self._calls = 0
        self.consecutive_refresh_failures = 0
        # collect_monitor_events 成功路径会读这些属性
        self.last_px = 1.0
        self.last_open = 1.0

    def refresh(self):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RuntimeError("boom")
        return []

    # 成功路径会调用的若干方法(返回中性值,保证不产生事件)
    def big_dir(self):
        return "neutral"


class TestRefreshFailureCircuitBreaker:
    def test_alert_on_threshold_and_reset(self, caplog):
        state = _FlakyState(fail_times=3)
        states = {"SH.600000": state}
        # 前两轮失败:warning 但非 ALERT
        for _ in range(2):
            collect_monitor_events(states)
        assert state.consecutive_refresh_failures == 2
        # 第三轮失败:跨过阈值,记 ALERT
        import logging
        with caplog.at_level(logging.WARNING):
            collect_monitor_events(states)
        assert state.consecutive_refresh_failures == 3
        assert any("[ALERT]" in rec.getMessage() for rec in caplog.records)
        # 第四轮成功:清零
        collect_monitor_events(states)
        assert state.consecutive_refresh_failures == 0

    def test_healthy_state_no_alert(self, caplog):
        state = _FlakyState(fail_times=0)  # 从不失败
        import logging
        with caplog.at_level(logging.WARNING):
            collect_monitor_events({"SH.600000": state})
        assert state.consecutive_refresh_failures == 0
        assert not any("[ALERT]" in rec.getMessage() for rec in caplog.records)


# ==========================================================================
# F-LOW-3: equity_curve 裁剪后 total_return/max_dd 不变
# ==========================================================================
class TestEquityCurveTrim:
    def _make_curve(self, equities):
        return [{"time": f"t{i}", "equity": float(e), "cash": float(e),
                 "positions": 0, "pending": 0, "trades": 0}
                for i, e in enumerate(equities)]

    def test_trim_preserves_total_return_and_maxdd(self, tmp_path):
        broker = PaperBroker(str(tmp_path / "ledger.json"))
        # 构造一条已知曲线:起点 1e6,中途冲高到 2e6(峰值),回落到 1.5e6
        n = broker.EQUITY_CURVE_MAXLEN + 5000
        equities = []
        for i in range(n):
            if i == 1000:
                equities.append(2_000_000.0)  # 早期峰值,会被裁进折叠段
            elif i == n - 1:
                equities.append(1_500_000.0)  # 末值
            else:
                equities.append(1_000_000.0)
        # 先算未裁剪的基准
        broker_ref = PaperBroker(str(tmp_path / "ref.json"))
        broker_ref.equity_curve = self._make_curve(equities)
        ref_summary = broker_ref.performance_summary()

        # 逐条 append 触发滚动裁剪
        broker.equity_curve = []
        for snap in self._make_curve(equities):
            broker.equity_curve.append(snap)
            broker._trim_equity_curve()

        # 稳态长度有界(逐条 append 时每轮 anchor 折叠上一轮 anchor,稳定在
        # MAXLEN+1:1 个 anchor + MAXLEN 条明细)。核心目的=防无界增长,已达成。
        assert len(broker.equity_curve) == broker.EQUITY_CURVE_MAXLEN + 1
        # 头部是 anchor
        assert broker.equity_curve[0].get("anchor") is True
        trimmed_summary = broker.performance_summary()
        # total_return 与 max_drawdown 不因裁剪失真
        assert trimmed_summary["total_return"] == pytest.approx(
            ref_summary["total_return"], rel=1e-9
        )
        assert trimmed_summary["max_drawdown"] == pytest.approx(
            ref_summary["max_drawdown"], rel=1e-9
        )

    def test_no_trim_below_maxlen(self, tmp_path):
        broker = PaperBroker(str(tmp_path / "ledger.json"))
        broker.equity_curve = self._make_curve([1_000_000.0] * 10)
        broker._trim_equity_curve()
        assert len(broker.equity_curve) == 10
        assert not any(p.get("anchor") for p in broker.equity_curve)

    def test_early_peak_survives_repeated_trim(self, tmp_path):
        """逐轮滚动裁剪时,早期峰值(落在最早被挤出的段)必须经 anchor 链式传递
        保留,否则 max_drawdown 会随峰值丢失而失真归零。"""
        broker = PaperBroker(str(tmp_path / "ledger.json"))
        broker.equity_curve = []
        n = broker.EQUITY_CURVE_MAXLEN * 2  # 充分多轮,确保峰值被多次折叠
        for i in range(n):
            eq = 3_000_000.0 if i == 50 else 1_000_000.0  # 极早期一个高峰
            broker.equity_curve.append(
                {"time": f"t{i}", "equity": eq, "cash": eq,
                 "positions": 0, "pending": 0, "trades": 0}
            )
            broker._trim_equity_curve()
        # 末值 1e6,历史峰值 3e6 → max_dd 必须 = 1 - 1e6/3e6,而非归零
        summary = broker.performance_summary()
        assert summary["max_drawdown"] == pytest.approx(1.0 - 1_000_000.0 / 3_000_000.0)
        # anchor 应保住该峰值
        anchor_peaks = [p.get("peak_equity") for p in broker.equity_curve if p.get("anchor")]
        assert anchor_peaks and max(anchor_peaks) == pytest.approx(3_000_000.0)

    def test_oneshot_append_matches_design_spec(self, tmp_path):
        """设计 spec 场景:一次性 append 到 MAXLEN+1 后调一次 trim。
        此场景 drop_to=1 只折叠最早 1 条,设计 after 行为=峰值若不在第 0 条则保留。"""
        broker = PaperBroker(str(tmp_path / "ledger.json"))
        equities = [1_000_000.0] * (broker.EQUITY_CURVE_MAXLEN + 1)
        equities[5] = 2_000_000.0   # 峰值在保留段内
        equities[-1] = 1_500_000.0
        broker.equity_curve = self._make_curve(equities)
        broker._trim_equity_curve()
        # 折叠 1 条 + anchor → MAXLEN+1
        assert len(broker.equity_curve) == broker.EQUITY_CURVE_MAXLEN + 1
        assert broker.equity_curve[0].get("anchor") is True
        summary = broker.performance_summary()
        # 峰值 2e6 仍在保留段,max_dd=0.5
        assert summary["max_drawdown"] == pytest.approx(0.5)


# ==========================================================================
# F-LOW-4: regime 跨日清理
# ==========================================================================
class _RegimeEx:
    def __init__(self, closes):
        self._closes = closes

    def klines(self, code, freq):
        # 造足够长的日线,日期都在 today 之前
        dates = pd.date_range(end=pd.Timestamp.today().normalize()
                              - pd.Timedelta(days=1), periods=len(self._closes))
        return pd.DataFrame({"date": dates, "close": self._closes})


class TestRegimeCacheCrossDayCleanup:
    def setup_method(self):
        _REGIME_CACHE.clear()

    def teardown_method(self):
        _REGIME_CACHE.clear()

    def test_stale_day_key_cleared(self):
        ex = _RegimeEx([10.0] * 60)
        today = _dt.date(2026, 6, 25)
        # 注入昨日 key(D4-ARCH-1 新格式: (market, code, date, lookback))
        yest_key = ("a", "SH.600000", str(_dt.date(2026, 6, 24)), 20)
        _REGIME_CACHE[yest_key] = "bull"
        current_visible_regime(ex, "SH.600000", lookback_days=20, today=today, market="a")
        # 昨日 key 被清,今日 key 在
        assert yest_key not in _REGIME_CACHE
        assert ("a", "SH.600000", str(today), 20) in _REGIME_CACHE

    def test_cross_market_same_code_no_alias(self):
        """审计 D4-ARCH-1: 不同市场同 code 字符串不共享 regime 缓存条目(key 含 market)。"""
        _REGIME_CACHE.clear()
        today = _dt.date(2026, 6, 25)
        ex = _RegimeEx([10.0] * 60)
        current_visible_regime(ex, "600000", lookback_days=20, today=today, market="a")
        current_visible_regime(ex, "600000", lookback_days=20, today=today, market="hk")
        assert ("a", "600000", str(today), 20) in _REGIME_CACHE
        assert ("hk", "600000", str(today), 20) in _REGIME_CACHE  # 独立条目, 不被 "a" 覆盖

    def test_same_day_cache_hit(self):
        ex = _RegimeEx([10.0] * 60)
        today = _dt.date(2026, 6, 25)
        r1 = current_visible_regime(ex, "SH.600000", lookback_days=20, today=today)
        # 第二次调用:把 ex 换成会抛错的,若命中缓存则不调用 klines,仍返回 r1
        class _Boom:
            def klines(self, *a, **k):
                raise AssertionError("should not refetch on cache hit")
        r2 = current_visible_regime(_Boom(), "SH.600000", lookback_days=20, today=today)
        assert r1 == r2


# ==========================================================================
# F-LOW-5: now_trading 兜底
# ==========================================================================
class _NowTradingBoomEx:
    def now_trading(self):
        raise RuntimeError("ctx healing")


class _NowTradingOkEx:
    def __init__(self, value: bool):
        self._value = value

    def now_trading(self):
        return self._value


class TestNowTradingFallback:
    def test_fallback_open_during_us_session(self):
        # 北京时间 22:00(美股盘中近似)→ True
        now = _dt.datetime(2026, 6, 25, 22, 0)  # 周四
        assert market_is_open(_NowTradingBoomEx(), "us", now) is True

    def test_fallback_closed_during_us_off_hours(self):
        # 北京时间 14:00(美股休市)→ False
        now = _dt.datetime(2026, 6, 25, 14, 0)  # 周四
        assert market_is_open(_NowTradingBoomEx(), "us", now) is False

    def test_no_fallback_when_now_trading_ok(self):
        now = _dt.datetime(2026, 6, 25, 14, 0)
        # now_trading 正常返回 True/False 时直接透传(不走兜底)
        assert market_is_open(_NowTradingOkEx(True), "us", now) is True
        assert market_is_open(_NowTradingOkEx(False), "us", now) is False

    def test_us_session_fallback_helper(self):
        assert _us_session_fallback(_dt.datetime(2026, 6, 25, 22, 0)) is True   # 周四夜
        assert _us_session_fallback(_dt.datetime(2026, 6, 25, 3, 0)) is True    # 周四凌晨
        assert _us_session_fallback(_dt.datetime(2026, 6, 25, 14, 0)) is False  # 周四午后
        # 周日(weekday=6)整天休市
        assert _us_session_fallback(_dt.datetime(2026, 6, 28, 22, 0)) is False


# ==========================================================================
# D1b-HIGH-1: 下单与通知解耦的安全前提 — queue_events 幂等
# ==========================================================================
def test_queue_events_idempotent_guards_decouple(tmp_path):
    """D1b-HIGH-1: run_once 已把下单(queue_events)与通知(send_events)解耦——通知失败不再
    阻断下单。其安全前提是 queue_events 幂等:通知持续失败、下轮同一 fresh 事件重现时,
    不得重复入单。此测试钉死该幂等性(若被破坏则解耦会致重复下单)。"""
    from types import SimpleNamespace
    from chanlun.recursive_bt.sim.paper import PaperBroker

    b = PaperBroker(str(tmp_path / "ledger.json"), "a", 5)
    ev = SimpleNamespace(
        code="SH.600000", side="buy", buy_ratio=0.2, sell_ratio=1.0,
        bs_type="1buy", reason="x", identity="id1", signal_time="t1",
    )
    assert b.queue_events([ev]) == 1  # 首次入单
    assert b.queue_events([ev]) == 0  # 同 code 已在 pending → 幂等跳过
    assert sum(1 for o in b.pending if o["act"] == "buy") == 1


# ==========================================================================
# F-CRIT-2: runtime override 加风险方向不自动应用(降风险/中性自动生效)
# ==========================================================================
def test_runtime_override_risk_increasing_gate():
    from chanlun.recursive_bt.monitor.live_monitor import _is_risk_increasing_override as f

    assert f("max_pos", 50, 30) is True  # 放大持仓上限 = 加杠杆/集中度
    assert f("max_pos", 20, 30) is False  # 缩小 = 降风险, 自动生效
    assert f("max_pos", 30, 30) is False  # 不变
    assert f("max_pos", 50, None) is False  # 无当前值 → 不判加风险
    assert f("selection_max_codes", 100, 50) is True
    assert f("trend_3boost", True, False) is True  # 关→开 boost = 加风险
    assert f("trend_3boost", False, True) is False  # 开→关 = 降风险
    assert f("enable_selection_pool", True, False) is True
    assert f("op_level", "1m", "5m") is False  # 级别等非风险敞口键 → 允许
    assert f("regime_mode", "x", "y") is False
    assert f("max_pos", "abc", 30) is False  # 非法值不崩


# ==========================================================================
# F-LOW-6: initial_codes selection 可淘汰
# ==========================================================================
class _StubSelector:
    def __init__(self, codes):
        self._codes = codes

    def select(self):
        class _C:
            def __init__(self, code):
                self.code = code
        return [_C(c) for c in self._codes]


class _StubArgs:
    def __init__(self):
        self.ledger = ""
        self.op_level = "5m"
        self.big_level = "30m"
        self.mid_level = ""
        self.market = "a"


class TestRescanSelectionPoolEviction:
    def test_non_initial_non_holding_code_evicted(self, monkeypatch):
        # selector 这一轮只选出 A;B 是上轮选出的(非 initial、非持仓)→ 应被淘汰
        monkeypatch.setattr(lm, "load_ledger_positions", lambda _p: set())
        # merge_codes 透传(A 股口径),避免依赖真实实现细节
        monkeypatch.setattr(lm, "merge_codes",
                            lambda fresh, holdings, market: list(fresh))
        # 预置 states:A(本轮仍入选)、B(应淘汰)。用占位对象(淘汰路径只 pop,不调方法)
        states = {"SH.000A": object(), "SH.000B": object()}
        names = {"SH.000A": "A", "SH.000B": "B"}
        selector = _StubSelector(["SH.000A"])  # 本轮只选 A
        rescan_selection_pool(
            selector, states, names, ex=None, args=_StubArgs(),
            initial_codes=set(),  # selection 模式下 initial 为空(F-LOW-6)
        )
        assert "SH.000A" in states   # 仍入选,保留
        assert "SH.000B" not in states  # 非 initial、非持仓、不再入选 → 淘汰

    def test_initial_code_retained(self, monkeypatch):
        monkeypatch.setattr(lm, "load_ledger_positions", lambda _p: set())
        monkeypatch.setattr(lm, "merge_codes",
                            lambda fresh, holdings, market: list(fresh))
        states = {"SH.000A": object(), "SH.000B": object()}
        names = {"SH.000A": "A", "SH.000B": "B"}
        selector = _StubSelector([])  # 本轮谁都不选
        rescan_selection_pool(
            selector, states, names, ex=None, args=_StubArgs(),
            initial_codes={"SH.000B"},  # B 是永久保留(显式 --codes)
        )
        assert "SH.000B" in states   # initial 永久保留,即便不再入选
        assert "SH.000A" not in states  # 非 initial、不再入选 → 淘汰

    def test_holding_retained(self, monkeypatch):
        monkeypatch.setattr(lm, "load_ledger_positions", lambda _p: {"SH.000A"})
        monkeypatch.setattr(lm, "merge_codes",
                            lambda fresh, holdings, market: list(fresh) + list(holdings))
        states = {"SH.000A": object()}
        names = {"SH.000A": "A"}
        selector = _StubSelector([])  # 不选 A,但 A 是持仓
        rescan_selection_pool(
            selector, states, names, ex=None, args=_StubArgs(),
            initial_codes=set(),
        )
        assert "SH.000A" in states   # 持仓永久保留
