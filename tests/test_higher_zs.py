"""P7: 多周期中枢叠加 — higher_zs_periods 阶梯映射 + apply_higher_zs 计算/组织。"""
from cl_app.services.chart_compute import higher_zs_periods


def test_higher_zs_periods_1m():
    assert higher_zs_periods("1m") == [("5m", "5min级别"), ("30m", "30min级别")]


def test_higher_zs_periods_5m():
    assert higher_zs_periods("5m") == [("30m", "30min级别")]


def test_higher_zs_periods_30m_empty():
    assert higher_zs_periods("30m") == []


def test_higher_zs_periods_d_empty():
    assert higher_zs_periods("d") == []
