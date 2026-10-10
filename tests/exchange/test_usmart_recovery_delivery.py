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


def test_minute_fault_exposes_symbol_period_and_utc_location_without_prices(monkeypatch):
    monkeypatch.setattr(config, 'US_HISTORY_KLINE_SOURCE', 'usmart')
    exchange = ExchangeUSmart('us', client=SimpleNamespace())
    coverage = {'reason': 'minute_contract_error', 'retry_after': 600,
                'diagnostics': {'invalid_minute_count': 1,
                                'invalid_minute_opens': [int(pd.Timestamp('2026-10-07T13:30:00Z').timestamp())]}}
    exchange._canonical_history = SimpleNamespace(window=Mock(return_value=(pd.DataFrame(), coverage)))
    with pytest.raises(USmartAPIError) as raised:
        exchange.klines('ORCL.US', '1m', end_date='2026-10-09T16:00:00-04:00')
    assert 'ORCL.US 1m' in str(raised.value)
    assert '无效分钟 1 条' in str(raised.value)
    assert '2026-10-07 13:30 UTC' in str(raised.value)
    assert '高周期' not in str(raised.value)
    assert raised.value.code == 'usmart_minute_contract_error'
    assert raised.value.retry_after == 600
