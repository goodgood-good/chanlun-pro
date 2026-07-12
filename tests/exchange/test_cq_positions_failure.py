import pytest

from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.trading.backtest_trader import BackTestTrader


def _failing_exchange():
    exchange = object.__new__(ExchangeChangQiao.__wrapped__)
    exchange._trade_ctx = lambda: (_ for _ in ()).throw(
        ConnectionError("Longbridge disconnected")
    )
    return exchange


def test_positions_query_failure_raises_instead_of_confirming_flat():
    with pytest.raises(RuntimeError, match="Longbridge position query failed"):
        _failing_exchange().positions("AAPL.US")


def test_generic_reconciliation_sees_longbridge_query_failure_as_unknown():
    trader = object.__new__(BackTestTrader)
    trader.ex = _failing_exchange()
    trader.log = None

    status, positions = trader.query_broker_position("AAPL.US")

    assert status == "fail"
    assert positions is None
