import math

from chanlun.exchange.kline_precision import _round_half_up, resolve_decimals


def test_a_share_stock_is_2_decimals():
    # 沪市股票 6 开头、深市 0/3 开头、北交所 4/8 开头 → 2 位
    assert resolve_decimals("a", "SH.600000") == 2
    assert resolve_decimals("a", "SZ.000001") == 2
    assert resolve_decimals("a", "SZ.300750") == 2
    assert resolve_decimals("a", "BJ.430047") == 2


def test_a_share_fund_is_3_decimals():
    # 代码数字首位 1/5 → 基金/ETF/可转债 → 3 位
    assert resolve_decimals("a", "SH.513100") == 3  # 跨境 ETF
    assert resolve_decimals("a", "SZ.159915") == 3  # 深市 ETF
    assert resolve_decimals("a", "SH.113050") == 3  # 沪市可转债
    assert resolve_decimals("a", "SZ.128040") == 3  # 深市可转债


def test_market_defaults():
    assert resolve_decimals("hk", "HK.00700") == 3
    assert resolve_decimals("us", "AAPL") == 3
    assert resolve_decimals("futures", "KQ.m@SHFE.rb") == 3
    assert resolve_decimals("ny_futures", "CL") == 3
    assert resolve_decimals("fx", "EURUSD") == 5
    assert resolve_decimals("currency", "BTC/USDT") == 8
    assert resolve_decimals("currency_spot", "ETH/USDT") == 8


def test_unknown_market_returns_none():
    assert resolve_decimals("mars", "X") is None


def test_a_share_no_digit_code_falls_back_to_stock():
    # 无数字的异常/内部代码默认按股票精度（2 位）处理
    assert resolve_decimals("a", "NODIGIT") == 2
    assert resolve_decimals("a", "") == 2


def test_round_half_up_rounds_5_upward():
    # 银行家舍入会逢偶取偶；ROUND_HALF_UP 一律进位
    assert _round_half_up(1.2345, 3) == 1.235
    assert _round_half_up(0.0625, 3) == 0.063
    assert _round_half_up(2.0005, 3) == 2.001  # 二进制实际值略大于 2.0005，任何舍入模式都进位


def test_round_half_up_truncates_extra_decimals():
    assert _round_half_up(12.3456789, 3) == 12.346
    assert _round_half_up(12.3454, 3) == 12.345


def test_round_half_up_kills_float_noise():
    # 远小于 1e-3、但高于 float64 ULP 的噪声，归一后必须 bit-exact 等于干净值
    assert _round_half_up(3.21 + 1e-9, 3) == 3.21
    assert _round_half_up(12.345 - 1e-9, 3) == 12.345


def test_round_half_up_passes_through_non_finite():
    assert math.isnan(_round_half_up(float("nan"), 3))
    assert _round_half_up(None, 3) is None
    assert _round_half_up(float("inf"), 3) == float("inf")
    assert _round_half_up(float("-inf"), 3) == float("-inf")
