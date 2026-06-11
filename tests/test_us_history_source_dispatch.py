"""验证 ExchangeChangQiao.klines 按 config.US_HISTORY_KLINE_SOURCE 路由。"""



class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _CapturingExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, _fn, *args, **_kwargs):
        self.calls.append(args)
        return _ImmediateFuture([])


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


def test_allow_long_history_keeps_explicit_start_for_low_level(monkeypatch):
    """Long-history backtests should not be clipped by the default 1m lookback."""
    monkeypatch.setattr("chanlun.config.US_HISTORY_KLINE_SOURCE", "longbridge")

    import chanlun.exchange.exchange_cq as cq

    monkeypatch.setattr(cq, "as_completed", lambda futures: futures)
    ex = cq.ExchangeChangQiao()
    executor = _CapturingExecutor()
    monkeypatch.setattr(ex, "executor", executor)

    ex.klines(
        "TSLA.US",
        "1m",
        start_date="2025-12-20 00:00:00",
        end_date="2026-06-10 04:00:00",
        args={"allow_long_history": True},
    )

    starts = [call[4] for call in executor.calls]
    assert starts
    assert min(starts).strftime("%Y-%m-%d %H:%M:%S") == "2025-12-20 00:00:00"
