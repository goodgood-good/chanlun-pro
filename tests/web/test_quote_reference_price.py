"""Price changes use the provider's exact basis, not a rounded percentage inversion."""
from types import SimpleNamespace

import pytest

from cl_app import create_app
from cl_app.blueprints import tv
from tests.web.test_ticks_none_rate import _tick


@pytest.fixture
def client():
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True, 'VALIDATE_WEB_SECURITY': False})
    yield app.test_client()
    app.extensions['shutdown_runtime_services']()


def response(client, monkeypatch, last, rate, reference):
    tick = _tick('SH.600000', last, rate)
    tick.reference_price = reference
    monkeypatch.setattr(tv, 'get_exchange', lambda _: SimpleNamespace(ticks=lambda _: {'SH.600000': tick}))
    return client.get('/tv/quotes?symbols=a:SH.600000').get_json()['d'][0]


def test_exact_reference_is_preserved_despite_rounded_percentage(client, monkeypatch):
    value = response(client, monkeypatch, 1033.33, 3.33, 1000)
    assert value['s'] == 'ok'
    assert value['v']['prev_close_price'] == 1000
    assert value['v']['ch'] == 33.33 and value['v']['chp'] == 3.33


def test_full_loss_retains_real_reference_and_change(client, monkeypatch):
    value = response(client, monkeypatch, 0, -100, 100)
    assert value['s'] == 'ok'
    assert value['v']['prev_close_price'] == 100 and value['v']['ch'] == -100


def test_full_loss_without_reference_does_not_fabricate_a_previous_price(client, monkeypatch):
    assert response(client, monkeypatch, 0, -100, None) == {'s': 'error', 'n': 'a:SH.600000', 'v': {}}


@pytest.mark.parametrize('reference', [float('nan'), float('inf'), 'bad', 0])
def test_explicit_invalid_reference_is_not_replaced_by_an_invented_value(client, monkeypatch, reference):
    assert response(client, monkeypatch, 110, 10, reference) == {'s': 'error', 'n': 'a:SH.600000', 'v': {}}


def test_rate_only_provider_keeps_its_existing_fallback(client, monkeypatch):
    value = response(client, monkeypatch, 110, 10, None)
    assert value['s'] == 'ok' and value['v']['prev_close_price'] == 100


def test_supplied_reference_and_small_change_keep_their_source_precision(client, monkeypatch):
    value = response(client, monkeypatch, 1.000009, 0.0, 1.000001)
    assert value['s'] == 'ok'
    assert value['v']['prev_close_price'] == 1.000001
    assert value['v']['ch'] == 0.000008


def test_unrepresentable_difference_does_not_put_infinity_into_json(client, monkeypatch):
    value = response(client, monkeypatch, 1e308, -200, -1e308)
    assert value == {'s': 'error', 'n': 'a:SH.600000', 'v': {}}
