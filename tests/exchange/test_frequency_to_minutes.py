"""Market-data frequencies retain fractional minute precision."""

def test_freq_minutes_parses_seconds():
    # 秒级须解析为分数分钟(step 精确), 分钟级不变, 日/周级仍 None。
    from chanlun.exchange.kline_completion import frequency_to_minutes

    assert frequency_to_minutes("10s") == 10 / 60.0
    assert frequency_to_minutes("30s") == 0.5
    assert frequency_to_minutes("300s") == 5.0
    assert frequency_to_minutes("5m") == 5
    assert frequency_to_minutes("d") is None
