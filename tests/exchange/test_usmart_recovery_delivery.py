"""Source recovery cooldown remains visible at the public adapter boundary."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange.errors import USmartAPIError
from chanlun.exchange.exchange_usmart import ExchangeUSmart


def test_unresolved_historical_minute_keeps_retry_after_without_publishing_bars(monkeypatch):
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    exchange=ExchangeUSmart('us',client=SimpleNamespace())
    source=Mock(return_value=(pd.DataFrame(),{'reason':'minute_contract_error','retry_after':600}))
    exchange._canonical_history=SimpleNamespace(window=source)
    with pytest.raises(USmartAPIError) as raised:
        exchange.klines('ORCL.US','5m',end_date='2026-10-09T16:00:00-04:00')
    assert raised.value.code=='usmart_minute_contract_error'
    assert raised.value.retry_after==600
    assert source.call_count==1
