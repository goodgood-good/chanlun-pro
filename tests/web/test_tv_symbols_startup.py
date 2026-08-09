"""Cold-start regression coverage for TradingView symbol resolution."""

from flask import Flask
import pytest

from cl_app.blueprints import tv as tv_module
from cl_app.services import stock_list


@pytest.fixture(autouse=True)
def _isolated_symbol_cache():
    with stock_list._stock_cache_lock:
        previous = dict(stock_list.stock_cache)
        stock_list.stock_cache.clear()
    yield
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        stock_list.stock_cache.update(previous)


def _resolve(monkeypatch: pytest.MonkeyPatch, symbol: str):
    app = Flask(__name__)
    # Symbol resolution must not start the lazy market-metadata loader in this
    # focused test; the production fallback contains the same A-share set.
    monkeypatch.setattr(
        tv_module,
        "market_frequencys",
        {"a": ["1m", "5m", "30m", "d", "w", "m"]},
    )
    with app.test_request_context(f"/tv/symbols?symbol={symbol}"):
        return tv_module.tv_symbols.__wrapped__()


@pytest.mark.parametrize(
    ("code", "name", "symbol_type", "expected_pricescale"),
    [
        ("SZ.301268", "铭利达", "stock_cn", 100),
        # Disk-restored LKG rows intentionally omit precision; ETF precision
        # must still match the K-line normalization contract.
        ("SH.513100", "纳指ETF国泰", "etf_cn", 1000),
    ],
)
def test_tv_symbols_resolves_from_warm_cache_without_touching_exchange(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    name: str,
    symbol_type: str,
    expected_pricescale: int,
) -> None:
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [
            {"code": code, "name": name, "type": symbol_type}
        ]

    monkeypatch.setattr(
        tv_module,
        "get_exchange",
        lambda *_args: pytest.fail(
            "a disk-warmed symbol must not wait for the QMT native lock"
        ),
    )

    payload = _resolve(monkeypatch, f"a:{code}")

    assert payload["ticker"] == f"a:{code}"
    assert payload["description"] == name
    assert payload["pricescale"] == expected_pricescale
    assert payload["supported_resolutions"] == ["1", "5", "30", "1D", "1W", "1M"]


def test_tv_symbols_cache_miss_preserves_live_exchange_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class _Exchange:
        def stock_info(self, code):
            calls.append(code)
            return {"code": code, "name": "浦发银行", "precision": 100}

        def stock_owner_plate(self, _code):
            raise RuntimeError("unsupported")

    monkeypatch.setattr(tv_module, "get_exchange", lambda _market: _Exchange())

    payload = _resolve(monkeypatch, "a:SH.600000")

    assert calls == ["SH.600000"]
    assert payload["ticker"] == "a:SH.600000"
    assert payload["description"] == "浦发银行"


def test_cached_symbol_lookup_returns_a_defensive_copy() -> None:
    source = {"code": "SZ.301268", "name": "铭利达", "type": "stock_cn"}
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [source]

    cached = stock_list.get_cached_processed_stock("a", "SZ.301268")
    assert cached == source

    cached["name"] = "changed"
    assert stock_list.get_cached_processed_stock("a", "SZ.301268")["name"] == "铭利达"
