"""A missing comparison price is unknown, not a flat market."""
from types import SimpleNamespace
from pathlib import Path
import ast
import math

import pytest
import pandas as pd

from chanlun.exchange.exchange import Tick
from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.exchange.exchange_usmart import ExchangeUSmart
from chanlun.exchange.quote_change import quote_change_rate, quote_price


def test_omitted_percentage_is_unknown():
    tick = Tick(code='example', last=10, buy1=10, sell1=10, high=10, low=10, open=10, volume=1)
    assert tick.rate is None


@pytest.mark.parametrize('reference', [0, None, float('nan'), float('inf'), 'unavailable'])
def test_usmart_keeps_price_when_comparison_price_is_unusable(reference):
    payload = {'list': [{'market': 'us', 'symbol': 'AAPL', 'latestPrice': 110, 'preClose': reference}]}
    exchange = ExchangeUSmart('us', client=SimpleNamespace(quote=lambda *_: payload))
    value = exchange.ticks(['AAPL.US'])['AAPL.US']
    assert value.last == 110 and value.rate is None


@pytest.mark.parametrize('reference', [0, None, float('nan'), float('inf'), 'unavailable'])
def test_longbridge_keeps_price_when_comparison_price_is_unusable(reference):
    exchange = object.__new__(ExchangeChangQiao.__wrapped__)
    quote = SimpleNamespace(symbol='AAPL.US', last_done=110, prev_close=reference,
                            high=111, low=109, open=110, volume=100)
    exchange._to_lb_symbol = lambda code: code
    exchange._from_lb_symbol = lambda code: code
    exchange._quote_ctx = lambda: SimpleNamespace(quote=lambda _: [quote])
    exchange._quote_call = lambda function, **_: function()
    value = exchange.ticks(['AAPL.US'])['AAPL.US']
    assert value.last == 110 and value.rate is None


@pytest.mark.parametrize('price,reference,expected', [(110, 100, 10.), (90, 100, -10.),
    (100, 100, 0.), (100, 0, None), (None, 100, None), ('bad', 100, None),
    (float('nan'), 100, None), (float('inf'), 100, None), (100, -100, None)])
def test_shared_change_preserves_known_values_and_rejects_unusable_comparisons(price, reference, expected):
    assert quote_change_rate(price, reference) == expected


def test_existing_unrounded_and_signed_reference_modes_are_preserved():
    assert quote_change_rate(101, 3, digits=None) == (101 - 3) / 3 * 100
    assert quote_change_rate(110, -100, positive_reference=False) == -210.


@pytest.mark.parametrize('provider', ['usmart', 'longbridge', 'futu', 'tq'])
def test_adapters_preserve_the_unrounded_reference_used_for_percentage(provider):
    code, last, reference = 'AAPL.US', 1033.33, 1000.0
    if provider == 'usmart':
        payload = {'list': [{'market': 'us', 'symbol': 'AAPL', 'latestPrice': last, 'preClose': reference}]}
        exchange = ExchangeUSmart('us', client=SimpleNamespace(quote=lambda *_: payload))
        quote = exchange.ticks([code])[code]
    elif provider == 'longbridge':
        from tests.exchange.test_invalid_quote_price import longbridge
        row = SimpleNamespace(symbol=code, last_done=last, prev_close=reference,
                              high=last, low=last, open=last, volume=1)
        quote = longbridge([row]).ticks([code])[code]
    elif provider == 'futu':
        frame = pd.DataFrame([{'code': code, 'last_price': last, 'prev_close_price': reference,
                               'high_price': last, 'low_price': last, 'open_price': last,
                               'bid_price': last, 'ask_price': last, 'volume': 1}])
        context = SimpleNamespace(get_market_snapshot=lambda _: (0, frame))
        method = _adapter_tick_method('exchange_futu.py', {'CTX': lambda: context, 'RET_OK': 0})
        quote = method(None, [code])[code]
    else:
        row = {'last_price': last, 'pre_settlement': reference, 'highest': last, 'lowest': last,
               'open': last, 'bid_price1': last, 'ask_price1': last, 'volume': 1}
        instance = SimpleNamespace(command_tasks=[], res_ticks={code: row})
        method = _adapter_tick_method('exchange_tq.py', {'math': math, 'time': SimpleNamespace(sleep=lambda _: None)})
        quote = method(instance, [code])[code]
    assert quote.last == last and quote.rate == 3.33 and quote.reference_price == reference


def _adapter_tick_method(filename, namespace):
    # Exercise the actual optional SDK adapter method without importing an SDK
    # or constructing any connection/account/subscription worker.
    path = Path(__file__).resolve().parents[2]/'src/chanlun/exchange'/filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'ticks')
    scope = {'Tick': Tick, 'Dict': dict, 'List': list, 'quote_change_rate': quote_change_rate,
             'quote_price': quote_price, **namespace}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
    return scope['ticks']


@pytest.mark.parametrize('reference,expected', [(None, None), (0, None), (float('nan'), None),
    (float('inf'), None), ('unavailable', None), (100, 10.), (110, 0.)])
@pytest.mark.parametrize('provider', ['futu', 'tq'])
def test_optional_adapter_quote_paths_preserve_unknown_comparison_prices(provider, reference, expected):
    code = 'fixture'
    if provider == 'futu':
        frame = pd.DataFrame([{'code': code, 'last_price': 110., 'prev_close_price': reference,
                               'high_price': 111., 'low_price': 109., 'open_price': 110.,
                               'bid_price': 110., 'ask_price': 110., 'volume': 100.}])
        context = SimpleNamespace(get_market_snapshot=lambda _: (0, frame))
        method = _adapter_tick_method('exchange_futu.py', {'CTX': lambda: context, 'RET_OK': 0})
        result = method(None, [code])
    else:
        quote = {'last_price': 110., 'pre_settlement': reference, 'highest': 111., 'lowest': 109.,
                 'open': 110., 'bid_price1': 110., 'ask_price1': 110., 'volume': 100.}
        instance = SimpleNamespace(command_tasks=[], res_ticks={code: quote})
        method = _adapter_tick_method('exchange_tq.py', {'math': math, 'time': SimpleNamespace(sleep=lambda _: None)})
        result = method(instance, [code])
        assert instance.command_tasks == ['tick:fixture']
    assert result[code].last == 110. and result[code].rate == expected
