"""图表长周期默认历史范围的回归测试。"""

import pytest

from chanlun.exchange._lookback import DEFAULT_LOOKBACK_DAYS
from chanlun.exchange.exchange_cq import ExchangeChangQiao


def test_thirty_minute_and_daily_chart_lookbacks_are_extended() -> None:
    assert DEFAULT_LOOKBACK_DAYS["30m"] == 365 * 2
    assert DEFAULT_LOOKBACK_DAYS["d"] == 365 * 6


def test_longbridge_rejects_a_frequency_outside_the_shared_lookback_contract() -> None:
    exchange_type = ExchangeChangQiao.__wrapped__
    exchange = exchange_type.__new__(exchange_type)

    with pytest.raises(ValueError, match="Unknown frequency"):
        exchange.klines("KH.00700", "unsupported")
