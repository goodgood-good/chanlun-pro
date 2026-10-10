"""Explicit opt-in cache generations; never reinterpret legacy FLOAT rows.

The native binary64 generation is written only from provider reads. Its table
starts empty: no old price is copied, rounded, or certified by changing a type.
Default readers continue to see the original table and original history.
"""
import hashlib

KLINE_BINARY64_REVISION = "binary64_native_v1"


def precision_table_name(table_name, market, precision_revision):
    if precision_revision is None:
        return table_name
    if precision_revision != KLINE_BINARY64_REVISION:
        raise ValueError("unsupported kline precision revision")
    if market not in {"currency", "currency_spot"}:
        raise ValueError("native precision cache is only enabled for Binance markets")
    suffix = "_p64_v1"
    if len(table_name) + len(suffix) > 64:
        table_name = table_name[:48] + "_" + hashlib.sha256(table_name.encode()).hexdigest()[:8]
    return table_name + suffix
