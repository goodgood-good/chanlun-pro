"""The TQ chart adapter must never enter an account or execution path."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

try:
    import tqsdk  # noqa: F401
except ImportError:
    sdk = types.ModuleType("tqsdk")
    objects = types.ModuleType("tqsdk.objs")
    objects.Quote = type("Quote", (), {})
    sdk.TqApi = type("TqApi", (), {})
    sdk.TqAuth = type("TqAuth", (), {})
    sdk.objs = objects
    sys.modules["tqsdk"] = sdk
    sys.modules["tqsdk.objs"] = objects

import chanlun.exchange.exchange_tq as tq_module


@pytest.fixture
def adapter():
    cls = tq_module.ExchangeTq.__wrapped__
    # No background connection thread or real credentials in these checks.
    instance = cls.__new__(cls)
    instance.g_api = None
    return instance


def test_quote_connection_reuses_auth_and_reconnects_without_account_access(adapter, monkeypatch):
    first, second = Mock(), Mock()
    auth = object()
    sdk = SimpleNamespace(
        TqApi=Mock(side_effect=[first, second]),
        TqAuth=Mock(return_value=auth),
        TqKq=Mock(side_effect=AssertionError("simulation account must not be created")),
        TqAccount=Mock(side_effect=AssertionError("broker account must not be created")),
    )
    monkeypatch.setattr(tq_module, "tqsdk", sdk)
    monkeypatch.setattr(tq_module, "config", SimpleNamespace(TQ_USER="quote-user", TQ_PWD="test-only"))

    assert adapter.get_api() is first
    assert adapter.get_api() is first
    sdk.TqApi.assert_called_once_with(auth=auth)
    adapter.close_api()
    first.close.assert_called_once_with()
    assert adapter.get_api() is second
    assert sdk.TqApi.call_count == 2
    sdk.TqKq.assert_not_called()
    sdk.TqAccount.assert_not_called()
