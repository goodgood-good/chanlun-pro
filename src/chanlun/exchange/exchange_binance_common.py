"""Shared public-market-data settings for Binance adapters."""

from __future__ import annotations

from typing import Any

import pandas as pd

from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)


# Binance documents this host specifically for unauthenticated Spot market data.
BINANCE_SPOT_PUBLIC_API_URL = "https://data-api.binance.vision/api/v3"

# The strict-structure runtime hashes this revision together with market, symbol,
# adjustment and price quantum.  Bump it whenever Binance OHLC normalization
# semantics change so cached structure snapshots cannot cross price bases.
BINANCE_OHLC_NORMALIZATION_REVISION = "kline-precision-round-half-up-v1"


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


def normalize_binance_kline_frame(
    frame: pd.DataFrame | None,
    *,
    market: str,
    code: str,
) -> pd.DataFrame | None:
    """Normalize Binance OHLC and attach the strict price-basis contract."""

    if market not in {"currency", "currency_spot"}:
        raise ValueError(f"unsupported Binance market for price basis: {market}")
    normalized = normalize_kline_precision(frame, market, code)
    if normalized is None:
        return None
    quantum = resolve_structure_price_quantum(market, code)
    if quantum is None:
        raise ValueError(
            f"Binance structure price quantum is unavailable: {market}:{code}"
        )
    metadata = build_provider_price_basis_metadata(
        provider="binance",
        market=market,
        code=code,
        adjustment="none",
        structure_price_quantum=quantum,
        normalization_revision=BINANCE_OHLC_NORMALIZATION_REVISION,
    )
    return attach_price_basis_metadata(normalized, metadata)


def configure_spot_public_market_data(exchange: Any) -> Any:
    """Restrict a CCXT Binance client to the official public Spot data host."""

    api_urls = exchange.urls.get("api")
    if not isinstance(api_urls, dict) or "public" not in api_urls:
        raise RuntimeError("ccxt Binance public API URL is unavailable")
    api_urls["public"] = BINANCE_SPOT_PUBLIC_API_URL
    return exchange
