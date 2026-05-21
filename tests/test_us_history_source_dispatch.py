"""验证 ExchangeChangQiao.klines 按 config.US_HISTORY_KLINE_SOURCE 路由。"""



def test_should_use_alpaca_returns_true_for_us_when_config_says_alpaca(monkeypatch):
    """config.US_HISTORY_KLINE_SOURCE=='alpaca' + US symbol → 应返回 True。"""
    monkeypatch.setattr("chanlun.config.US_HISTORY_KLINE_SOURCE", "alpaca")
    from chanlun.exchange.exchange_cq import ExchangeChangQiao
    ex = ExchangeChangQiao()
    assert ex._should_use_alpaca("QQQ.US") is True
    assert ex._should_use_alpaca("AAPL.US") is True


def test_should_use_alpaca_returns_false_for_us_when_config_says_longbridge(monkeypatch):
    """config.US_HISTORY_KLINE_SOURCE=='longbridge' → 应返回 False（不走 alpaca）。"""
    monkeypatch.setattr("chanlun.config.US_HISTORY_KLINE_SOURCE", "longbridge")
    from chanlun.exchange.exchange_cq import ExchangeChangQiao
    ex = ExchangeChangQiao()
    assert ex._should_use_alpaca("QQQ.US") is False


def test_should_use_alpaca_returns_false_for_hk_or_a_share(monkeypatch):
    """alpaca 仅美股；HK/A 股不应走 alpaca 即使 config 是 alpaca。"""
    monkeypatch.setattr("chanlun.config.US_HISTORY_KLINE_SOURCE", "alpaca")
    from chanlun.exchange.exchange_cq import ExchangeChangQiao
    ex = ExchangeChangQiao()
    assert ex._should_use_alpaca("700.HK") is False
    assert ex._should_use_alpaca("000001.SZ") is False
    assert ex._should_use_alpaca("") is False
    assert ex._should_use_alpaca(None) is False
