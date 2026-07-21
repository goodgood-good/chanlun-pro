from __future__ import annotations

from decimal import Decimal

from chanlun.exchange.kline_precision import (
    resolve_tdx_industry_index_quantum,
)
from chanlun.exchange.price_basis import (
    build_tdx_industry_price_basis_metadata,
)


def test_tdx_880_price_basis_is_native_continuous_and_stable() -> None:
    quantum = resolve_tdx_industry_index_quantum("SH.880302")
    assert quantum == Decimal("0.01")
    first = build_tdx_industry_price_basis_metadata("SH.880302", quantum)
    second = build_tdx_industry_price_basis_metadata("SH.880302", quantum)
    assert first == second
    assert first.provider == "tdx-industry-index"
    assert first.adjustment == "none"
    assert first.price_basis_revision.startswith("sha256:")


def test_non_880_code_never_receives_tdx_industry_price_basis() -> None:
    assert resolve_tdx_industry_index_quantum("SH.600519") is None
    assert resolve_tdx_industry_index_quantum("SZ.880302") is None
