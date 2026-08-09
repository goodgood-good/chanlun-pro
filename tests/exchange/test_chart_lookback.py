"""图表长周期默认历史范围的回归测试。"""

from chanlun.exchange._lookback import DEFAULT_LOOKBACK_DAYS


def test_thirty_minute_and_daily_chart_lookbacks_are_extended() -> None:
    assert DEFAULT_LOOKBACK_DAYS["30m"] == 365 * 2
    assert DEFAULT_LOOKBACK_DAYS["d"] == 365 * 6
