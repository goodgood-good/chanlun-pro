# -*- coding: utf-8 -*-
"""signal_monitor done-gating 修复单测(审计 audit/_round5_signal_monitor.md)。

覆盖:
- S1: build_line_stable_key 把 done/forming 纳入身份(forming 与 done 是两个
      identity);make_identity 随之区分;getattr 兜底无 is_done 的对象。
- S4: monitoring_signal_code 拉 K 线喂 CL 前调用 drop_unclosed_last_bar 丢弃
      未收盘在制 bar(与交易层 live 口径统一)。

全部用 stub/monkeypatch 隔离真实 exchange 与缠论计算,只验证补丁逻辑本身。
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

from chanlun.signal_monitor import monitor as monitor_mod
from chanlun.signal_monitor.cl_signal import (
    build_line_stable_key,
    make_identity,
)
from chanlun.signal_monitor.evaluator import EvaluatorConfig


# --------------------------------------------------------------------------
# 辅助:最小 line stub(只需 start.k.date / type / is_done)
# --------------------------------------------------------------------------
class _K:
    def __init__(self, date):
        self.date = date


class _Start:
    def __init__(self, date):
        self.k = _K(date)


class _LineStub:
    """模拟 BI/XD:有 start 分型、type、is_done()。"""

    def __init__(self, date, line_type: str, done: bool):
        self.start = _Start(date)
        self.type = line_type
        self._done = done

    def is_done(self) -> bool:
        return self._done


class _NoIsDoneStub:
    """没有 is_done 方法的对象(防御兜底路径)。"""

    def __init__(self, date, line_type: str):
        self.start = _Start(date)
        self.type = line_type


_DATE = _dt.datetime(2026, 6, 25, 10, 0, 0)


# ==========================================================================
# S1: build_line_stable_key 含 done/forming
# ==========================================================================
class TestBuildLineStableKeyDoneGating:
    def test_done_and_forming_are_different_keys(self):
        forming = build_line_stable_key(_LineStub(_DATE, "down", done=False))
        done = build_line_stable_key(_LineStub(_DATE, "down", done=True))
        # 同起点同方向,仅完成状态不同 → 必须是两个不同 key
        assert forming != done
        assert forming.endswith("|forming")
        assert done.endswith("|done")

    def test_key_keeps_date_and_type_prefix(self):
        key = build_line_stable_key(_LineStub(_DATE, "up", done=True))
        # 形如 "2026-06-25 10:00:00|up|done"
        assert key == "2026-06-25 10:00:00|up|done"

    def test_forming_key_format(self):
        key = build_line_stable_key(_LineStub(_DATE, "down", done=False))
        assert key == "2026-06-25 10:00:00|down|forming"

    def test_missing_is_done_falls_back_to_forming(self):
        """对象没有 is_done() 时不崩溃,退化为 forming。"""
        key = build_line_stable_key(_NoIsDoneStub(_DATE, "up"))
        assert key.endswith("|forming")

    def test_identity_differs_between_forming_and_done(self):
        """identity 拼了 line_stable_key → forming/done 产出两个 identity。"""
        forming_key = build_line_stable_key(_LineStub(_DATE, "down", done=False))
        done_key = build_line_stable_key(_LineStub(_DATE, "down", done=True))
        id_forming = make_identity("SH.600519", "30m", "xd_beichi", "bullish", forming_key)
        id_done = make_identity("SH.600519", "30m", "xd_beichi", "bullish", done_key)
        assert id_forming != id_done
        assert id_forming.endswith("|forming")
        assert id_done.endswith("|done")


# ==========================================================================
# S4: monitoring_signal_code 拉 K 线前调 drop_unclosed_last_bar
# ==========================================================================
def _make_klines(starts) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": list(starts),
            "open": [1.0] * len(starts),
            "high": [1.0] * len(starts),
            "low": [1.0] * len(starts),
            "close": [1.0] * len(starts),
            "volume": [1.0] * len(starts),
        }
    )


class _StubExchange:
    """klines 返回固定 df(末根是「进行中」的在制 bar)。"""

    def __init__(self, df_by_freq):
        self._df_by_freq = df_by_freq
        self.calls = []

    def klines(self, code, freq):
        self.calls.append((code, freq))
        return self._df_by_freq[freq]


class TestS4DropUnclosedInPipeline:
    def _patch_common(self, monkeypatch, ex):
        # get_exchange / Market 走 stub,不触真实数据源
        monkeypatch.setattr(monitor_mod, "get_exchange", lambda _m: ex)
        monkeypatch.setattr(monitor_mod, "Market", lambda m: m)

    def test_drop_unclosed_called_per_freq_with_correct_args(self, monkeypatch):
        """每个周期都对该周期的 df + freq 调一次 drop_unclosed_last_bar。"""
        # 单周期梯队,避免多级别 mock 噪声
        df5 = _make_klines([_DATE])
        ex = _StubExchange({"5m": df5})
        self._patch_common(monkeypatch, ex)

        spy_calls = []

        def _spy_drop(df, freq):
            spy_calls.append((id(df), freq))
            return df

        monkeypatch.setattr(monitor_mod, "drop_unclosed_last_bar", _spy_drop)
        # web_batch_get_cl_datas 返回空 → operation_level 不在 cds → 提前 return []
        monkeypatch.setattr(monitor_mod, "web_batch_get_cl_datas",
                            lambda *a, **k: [])

        config = EvaluatorConfig(operation_level="5m", level_ladder=["5m"])
        out = monitor_mod.monitoring_signal_code(
            task_name="t", market="a", code="SH.600519", name="x",
            config=config, is_send_msg=False,
        )
        assert out == []
        # drop_unclosed_last_bar 对 5m 周期被调用一次,且 freq 实参正确
        assert ("5m" in [c[1] for c in spy_calls])
        assert (id(df5), "5m") in spy_calls

    def test_unclosed_bar_dropped_before_feeding_cl(self, monkeypatch):
        """端到端:末根在制 bar 应在喂 web_batch_get_cl_datas 前被真实裁掉。"""
        # 造一段 5m K 线,末根起点 = 当前 5m 周期起点(now 落在 [last,last+step) → 在制)
        step = pd.Timedelta(minutes=5)
        now = pd.Timestamp.now()
        last = now.floor("5min")
        if last == now:
            last = last - step
        prev = last - step
        df5 = _make_klines([prev - step, prev, last])
        ex = _StubExchange({"5m": df5})
        self._patch_common(monkeypatch, ex)

        captured = {}

        def _capture(market, code, klines_map, cl_config):
            # 记录喂进来的 df(经 drop_unclosed 处理后)
            captured["df"] = klines_map["5m"]
            return []   # 返回空,提前 return,不触真实缠论

        monkeypatch.setattr(monitor_mod, "web_batch_get_cl_datas", _capture)

        config = EvaluatorConfig(operation_level="5m", level_ladder=["5m"])
        monitor_mod.monitoring_signal_code(
            task_name="t", market="a", code="SH.600519", name="x",
            config=config, is_send_msg=False,
        )
        fed = captured["df"]
        # 末根在制 bar 已被裁:长度 -1,末根变成 prev
        assert len(fed) == len(df5) - 1
        assert fed["date"].iloc[-1] == prev

    def test_daily_freq_not_trimmed(self, monkeypatch):
        """日线非分钟级 → drop_unclosed_last_bar no-op,完整喂入(即便末根是今天)。"""
        today = pd.Timestamp(_dt.date.today())
        dfd = _make_klines([today - pd.Timedelta(days=2),
                            today - pd.Timedelta(days=1), today])
        ex = _StubExchange({"d": dfd})
        self._patch_common(monkeypatch, ex)

        captured = {}

        def _capture(market, code, klines_map, cl_config):
            captured["df"] = klines_map["d"]
            return []

        monkeypatch.setattr(monitor_mod, "web_batch_get_cl_datas", _capture)

        config = EvaluatorConfig(operation_level="d", level_ladder=["d"])
        monitor_mod.monitoring_signal_code(
            task_name="t", market="a", code="SH.600519", name="x",
            config=config, is_send_msg=False,
        )
        # 日线一根不裁
        assert len(captured["df"]) == len(dfd)
