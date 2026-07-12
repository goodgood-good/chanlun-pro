from types import SimpleNamespace

from chanlun.backtesting.signal_to_trade import SignalToTrade


def _converter(start=None, end=None):
    converter = object.__new__(SignalToTrade)
    converter.trade_start_date = start
    converter.trade_end_date = end
    return converter


def test_trade_start_date_applies_without_trade_end_date():
    converter = _converter(start="2024-02-01", end=None)
    backtest = SimpleNamespace(
        start_datetime="2024-01-01",
        end_datetime="2024-12-31",
    )

    converter._apply_trade_date_overrides(backtest)

    assert backtest.start_datetime == "2024-02-01"
    assert backtest.end_datetime == "2024-12-31"


def test_trade_end_date_does_not_clear_unset_trade_start_date():
    converter = _converter(start=None, end="2024-11-30")
    backtest = SimpleNamespace(
        start_datetime="2024-01-01",
        end_datetime="2024-12-31",
    )

    converter._apply_trade_date_overrides(backtest)

    assert backtest.start_datetime == "2024-01-01"
    assert backtest.end_datetime == "2024-11-30"
