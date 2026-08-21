"""Shared public-market-data settings for Binance adapters."""

from __future__ import annotations

from typing import Any


# Binance documents this host specifically for unauthenticated Spot market data.
BINANCE_SPOT_PUBLIC_API_URL = "https://data-api.binance.vision/api/v3"


# Project frequency -> CCXT/Binance native timeframe.  The three entries whose
# source timeframe differs are synthesized locally after downloading the bars.
BINANCE_KLINE_TIMEFRAMES = {
    "m": "1M",
    "w": "1w",
    "3d": "3d",
    "d": "1d",
    "12h": "12h",
    "8h": "8h",
    "6h": "6h",
    "4h": "4h",
    "3h": "1h",
    "120m": "2h",
    "60m": "1h",
    "30m": "30m",
    "15m": "15m",
    "10m": "5m",
    "5m": "5m",
    "3m": "3m",
    "2m": "1m",
    "1m": "1m",
}

BINANCE_SYNTHETIC_FREQUENCIES = frozenset({"2m", "10m", "3h"})

BINANCE_SUPPORTED_FREQUENCIES = {
    "m": "Month",
    "w": "Week",
    "3d": "3 Days",
    "d": "Day",
    "12h": "12H",
    "8h": "8H",
    "6h": "6H",
    "4h": "4H",
    "3h": "3H",
    "120m": "2H",
    "60m": "1H",
    "30m": "30m",
    "15m": "15m",
    "10m": "10m",
    "5m": "5m",
    "3m": "3m",
    "2m": "2m",
    "1m": "1m",
}


def configure_spot_public_market_data(exchange: Any) -> Any:
    """Restrict a CCXT Binance client to the official public Spot data host."""

    api_urls = exchange.urls.get("api")
    if not isinstance(api_urls, dict) or "public" not in api_urls:
        raise RuntimeError("ccxt Binance public API URL is unavailable")
    api_urls["public"] = BINANCE_SPOT_PUBLIC_API_URL
    return exchange
