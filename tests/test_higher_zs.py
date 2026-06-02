"""P7: 多周期中枢叠加 — higher_zs_periods 阶梯映射 + apply_higher_zs 计算/组织。"""
import numpy as np
import pandas as pd

from cl_app.services import chart_compute as CC
from cl_app.services.chart_compute import higher_zs_periods


def test_higher_zs_periods_1m():
    assert higher_zs_periods("1m") == [("5m", "5min级别"), ("30m", "30min级别")]


def test_higher_zs_periods_5m():
    assert higher_zs_periods("5m") == [("30m", "30min级别")]


def test_higher_zs_periods_30m_empty():
    assert higher_zs_periods("30m") == []


def test_higher_zs_periods_d_empty():
    assert higher_zs_periods("d") == []


def _synth_df(n, slope=0.0):
    t = np.arange(n, dtype=float)
    close = (100 + slope * t
             + 6 * np.sin(2 * np.pi * t / (n / 20.0))
             + 2 * np.sin(2 * np.pi * t / (n / 200.0)))
    rng = np.random.default_rng(7)
    close = close + rng.normal(0, 0.1, n)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01 09:30:00", periods=n, freq="1min"),
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1000.0,
    })


def test_apply_higher_zs_gated_off():
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {"chart_show_higher_zs": "0"}) is False
    assert "higher_zs" not in cd


def test_apply_higher_zs_high_period_empty():
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "30m", {}) is False
    assert "higher_zs" not in cd


def test_apply_higher_zs_organizes(monkeypatch):
    # monkeypatch 单周期取数, 验证组织逻辑(1m→两级, 字段结构)
    monkeypatch.setattr(CC, "_higher_zs_for_period",
                        lambda market, code, hf, cfg: [{"points": [], "type": "zd"}])
    cd = {"t": [1, 2, 3]}
    ok = CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {})
    assert ok is True
    assert [g["period"] for g in cd["higher_zs"]] == ["5m", "30m"]
    assert [g["level_name"] for g in cd["higher_zs"]] == ["5min级别", "30min级别"]
    assert all(isinstance(g["zss"], list) for g in cd["higher_zs"])


def test_higher_zs_for_period_real(monkeypatch):
    # monkeypatch ex.klines 返回合成趋势 df, 真实跑新核心取 L1 中枢
    class _Ex:
        def klines(self, code, freq, **kw):
            return _synth_df(5000, slope=0.01)
    monkeypatch.setattr(CC, "get_exchange", lambda m: _Ex())
    zss = CC._higher_zs_for_period("a", "X", "5m", {"zs_bi_type": ["zs_type_bz"]})
    assert isinstance(zss, list)  # 可能空(数据不足 L1), 但不报错且结构正确
    for z in zss:
        assert "points" in z and "linestyle" in z


def test_higher_zs_passthrough_slice_trim():
    hz = [{"period": "5m", "level_name": "5min级别", "zss": []}]
    cd = {"t": [100, 200, 300], "o": [1, 2, 3], "h": [1, 2, 3],
          "l": [1, 2, 3], "c": [1, 2, 3], "v": [1, 2, 3], "higher_zs": hz}
    sliced = CC.slice_chart_data_to_window(cd, 100, 300)
    assert sliced["higher_zs"] == hz          # 整体透传, 不按窗口裁切
    trimmed = CC.trim_future_bars(cd, 250)
    assert trimmed["higher_zs"] == hz
