"""Unknown latest prices are omitted instead of turning into zero-price quotes."""
from types import SimpleNamespace
import math

import pytest
import pandas as pd

from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.exchange.exchange_usmart import ExchangeUSmart
from chanlun.exchange.quote_change import quote_price
from tests.exchange.test_unknown_quote_change import _adapter_tick_method


@pytest.mark.parametrize('bad_price', [None, float('nan'), float('inf'), 'unavailable'])
def test_usmart_bad_latest_price_does_not_become_zero_or_affect_the_healthy_symbol(bad_price):
    payload = {'list': [
        {'market': 'us', 'symbol': 'BAD', 'latestPrice': bad_price, 'preClose': 100},
        {'market': 'us', 'symbol': 'GOOD', 'latestPrice': 110, 'preClose': 100},
    ]}
    exchange = ExchangeUSmart('us', client=SimpleNamespace(quote=lambda *_: payload))
    values = exchange.ticks(['BAD.US', 'GOOD.US'])
    assert list(values) == ['GOOD.US']
    assert values['GOOD.US'].last == 110 and values['GOOD.US'].rate == 10.


def longbridge(rows):
    exchange = object.__new__(ExchangeChangQiao.__wrapped__)
    exchange._to_lb_symbol = lambda code: code
    exchange._from_lb_symbol = lambda code: code
    exchange._quote_ctx = lambda: SimpleNamespace(quote=lambda _: rows)
    exchange._quote_call = lambda function, **_: function()
    return exchange


@pytest.mark.parametrize('bad_price', [None, float('nan'), float('inf'), 'unavailable'])
def test_longbridge_bad_latest_price_does_not_clear_the_batch(bad_price):
    rows = [SimpleNamespace(symbol=code, last_done=price, prev_close=100,
                            high=111, low=109, open=110, volume=100)
            for code, price in [('BAD.US', bad_price), ('GOOD.US', 110)]]
    values = longbridge(rows).ticks(['BAD.US', 'GOOD.US'])
    assert list(values) == ['GOOD.US']
    assert values['GOOD.US'].last == 110 and values['GOOD.US'].rate == 10.


def test_longbridge_other_malformed_fields_stay_local_to_one_symbol():
    bad = SimpleNamespace(symbol='BAD.US', last_done=100, prev_close=90, high='bad', low=99, open=100, volume=100)
    good = SimpleNamespace(symbol='GOOD.US', last_done=110, prev_close=100, high=111, low=109, open=110, volume=100)
    assert list(longbridge([bad, good]).ticks(['BAD.US', 'GOOD.US'])) == ['GOOD.US']


@pytest.mark.parametrize('price', [0, -1.5, '100.25'])
def test_explicit_finite_quote_values_are_not_mistaken_for_missing(price):
    assert quote_price(price) == float(price)


@pytest.mark.parametrize('bad_price', [None, float('nan'), float('inf'), 'unavailable'])
@pytest.mark.parametrize('provider', ['futu', 'tq'])
def test_optional_provider_bad_latest_price_does_not_poison_good_rows(provider, bad_price):
    if provider == 'futu':
        frame = pd.DataFrame([{'code': code, 'last_price': price, 'prev_close_price': 100,
                               'high_price': 111., 'low_price': 109., 'open_price': 110.,
                               'bid_price': 110., 'ask_price': 110., 'volume': 100.}
                              for code, price in [('bad', bad_price), ('good', 110.)]])
        context = SimpleNamespace(get_market_snapshot=lambda _: (0, frame))
        method = _adapter_tick_method('exchange_futu.py', {'CTX': lambda: context, 'RET_OK': 0})
        result = method(None, ['bad', 'good'])
    else:
        quotes = {code: {'last_price': price, 'pre_settlement': 100, 'highest': 111., 'lowest': 109.,
                         'open': 110., 'bid_price1': 110., 'ask_price1': 110., 'volume': 100.}
                  for code, price in [('bad', bad_price), ('good', 110.)]}
        instance = SimpleNamespace(command_tasks=[], res_ticks=quotes)
        method = _adapter_tick_method('exchange_tq.py', {'math': math, 'time': SimpleNamespace(sleep=lambda _: None)})
        result = method(instance, ['bad', 'good'])
    assert list(result) == ['good'] and result['good'].last == 110.


def test_tq_incomplete_snapshot_does_not_block_a_following_symbol():
    good = {'last_price': 110., 'pre_settlement': 100, 'highest': 111., 'lowest': 109.,
            'open': 110., 'bid_price1': 110., 'ask_price1': 110., 'volume': 100.}
    instance = SimpleNamespace(command_tasks=[], res_ticks={'bad': {}, 'good': good})
    method = _adapter_tick_method('exchange_tq.py', {'math': math, 'time': SimpleNamespace(sleep=lambda _: None)})
    assert list(method(instance, ['bad', 'good'])) == ['good']
