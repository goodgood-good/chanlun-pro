"""P7(已停用) + P8: 多周期中枢叠加相关测试。

P8 取代 P7: 高级别中枢改由单周期递归扩展(recursive_levels)产出，
apply_higher_zs_to_chart_data 现在恒返回 False 且不写 chart_data['higher_zs']。
higher_zs_periods 阶梯映射逻辑保留，可供未来恢复 P7 使用。
"""
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


# ---- P8 停用验证: apply_higher_zs_to_chart_data 恒返回 False, 不写 higher_zs ----

def test_apply_higher_zs_p8_disabled_always_false():
    """P8 停用后：无论配置/频率如何，apply_higher_zs_to_chart_data 恒返回 False。"""
    for freq, cfg in [
        ("1m", {}),
        ("1m", {"chart_show_higher_zs": "1"}),
        ("1m", {"chart_show_higher_zs": "0"}),
        ("30m", {}),
        ("d", {}),
    ]:
        cd = {"t": [1, 2, 3]}
        result = CC.apply_higher_zs_to_chart_data(cd, "a", "X", freq, cfg)
        assert result is False, f"P8停用后应返回False，freq={freq} cfg={cfg}"
        assert "higher_zs" not in cd, f"P8停用后不应写higher_zs，freq={freq}"


def test_apply_higher_zs_gated_off():
    """兼容旧名：P8停用后配置关也返回 False（行为一致）。"""
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {"chart_show_higher_zs": "0"}) is False
    assert "higher_zs" not in cd


def test_apply_higher_zs_high_period_empty():
    """兼容旧名：P8停用后高周期无映射也返回 False（行为一致）。"""
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "30m", {}) is False
    assert "higher_zs" not in cd


# ---- P7 实现保留验证（dormant，不执行真实逻辑）----

def test_apply_higher_zs_dormant_does_not_populate(monkeypatch):
    """P7 dormant: 即使 monkeypatch _higher_zs_for_period，P8早返回前也不会调用。"""
    called = []
    monkeypatch.setattr(CC, "_higher_zs_for_period",
                        lambda market, code, hf, cfg: called.append(hf) or [])
    cd = {"t": [1, 2, 3]}
    ok = CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {})
    assert ok is False
    assert "higher_zs" not in cd
    assert called == [], "P8停用后不应调用 _higher_zs_for_period"


def test_higher_zs_for_period_real(monkeypatch):
    """_higher_zs_for_period 本身逻辑保留可用（P7 dormant，但实现未删）。"""
    class _Ex:
        def klines(self, code, freq, **kw):
            return _synth_df(5000, slope=0.01)
    monkeypatch.setattr(CC, "get_exchange", lambda m: _Ex())
    zss = CC._higher_zs_for_period("a", "X", "5m", {"zs_bi_type": ["zs_type_bz"]})
    assert isinstance(zss, list)  # 可能空(数据不足 L1), 但不报错且结构正确
    for z in zss:
        assert "points" in z and "linestyle" in z


def test_higher_zs_passthrough_slice_trim():
    """slice/trim 对已有 higher_zs 字段仍透传（历史缓存兼容）。"""
    hz = [{"period": "5m", "level_name": "5min级别", "zss": []}]
    cd = {"t": [100, 200, 300], "o": [1, 2, 3], "h": [1, 2, 3],
          "l": [1, 2, 3], "c": [1, 2, 3], "v": [1, 2, 3], "higher_zs": hz}
    sliced = CC.slice_chart_data_to_window(cd, 100, 300)
    assert sliced["higher_zs"] == hz          # 整体透传, 不按窗口裁切
    trimmed = CC.trim_future_bars(cd, 250)
    assert trimmed["higher_zs"] == hz
