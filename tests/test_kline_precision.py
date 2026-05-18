from chanlun.exchange.kline_precision import resolve_decimals


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
