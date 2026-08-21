from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from chanlun.exchange.kline_precision import resolve_structure_price_quantum
from chanlun.exchange.price_basis import (
    PriceBasisMismatchError,
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
    build_qmt_price_basis_metadata,
    merge_price_basis_metadata,
)


def _factors(*, dr: float = 1.02) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": 1736438400000.0,
                "interest": 0.02,
                "stockBonus": 0.0,
                "stockGift": 0.0,
                "allotNum": 0.0,
                "allotPrice": 0.0,
                "gugai": 0.0,
                "dr": dr,
            },
            {
                "time": 1750176000000.0,
                "interest": 0.056,
                "stockBonus": 0.0,
                "stockGift": 0.4,
                "allotNum": 0.0,
                "allotPrice": 0.0,
                "gugai": 0.0,
                "dr": 1.401423,
            },
        ]
    )


def test_structure_quantum_reuses_a_share_precision_rules() -> None:
    assert resolve_structure_price_quantum("a", "SH.600926") == Decimal("0.01")
    assert resolve_structure_price_quantum("a", "SH.510300") == Decimal("0.001")
    assert resolve_structure_price_quantum("a", "SZ.159919") == Decimal("0.001")
    assert resolve_structure_price_quantum("unknown", "X") is None


def test_qmt_basis_revision_is_order_independent_and_fact_sensitive() -> None:
    quantum = Decimal("0.01")
    factors = _factors()
    first = build_qmt_price_basis_metadata(
        code="SZ.301517",
        adjustment="front",
        structure_price_quantum=quantum,
        factors=factors,
    )
    reordered = build_qmt_price_basis_metadata(
        code="SZ.301517",
        adjustment="front",
        structure_price_quantum=quantum,
        factors=factors.iloc[::-1][list(reversed(factors.columns))],
    )
    changed = build_qmt_price_basis_metadata(
        code="SZ.301517",
        adjustment="front",
        structure_price_quantum=quantum,
        factors=_factors(dr=1.03),
    )
    back = build_qmt_price_basis_metadata(
        code="SZ.301517",
        adjustment="back",
        structure_price_quantum=quantum,
        factors=factors,
    )

    assert first.price_basis_revision == reordered.price_basis_revision
    assert first.price_basis_revision != changed.price_basis_revision
    assert first.price_basis_revision != back.price_basis_revision
    assert first.price_basis_revision == (
        "sha256:a57c35ffabd80b46ec85fb9e8da25aa9"
        "04bc554e007a75910bf44a13daef4ea7"
    )


def test_empty_qmt_factor_ledger_has_stable_revision() -> None:
    first = build_qmt_price_basis_metadata(
        code="SH.000001",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=None,
    )
    second = build_qmt_price_basis_metadata(
        code="SH.000001",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=pd.DataFrame(),
    )
    assert first == second


def test_provider_basis_revision_is_stable_and_method_specific() -> None:
    first = build_provider_price_basis_metadata(
        provider="longbridge",
        market="us",
        code="TSLA.US",
        adjustment="forward",
        structure_price_quantum=Decimal("0.001"),
    )
    same = build_provider_price_basis_metadata(
        provider="longbridge",
        market="us",
        code="TSLA.US",
        adjustment="forward",
        structure_price_quantum=Decimal("0.001"),
    )
    raw = build_provider_price_basis_metadata(
        provider="longbridge",
        market="us",
        code="TSLA.US",
        adjustment="none",
        structure_price_quantum=Decimal("0.001"),
    )
    normalized = build_provider_price_basis_metadata(
        provider="longbridge",
        market="us",
        code="TSLA.US",
        adjustment="forward",
        structure_price_quantum=Decimal("0.001"),
        normalization_revision="ohlc-envelope",
    )

    assert first == same
    assert first.price_basis_revision != raw.price_basis_revision
    assert first.price_basis_revision != normalized.price_basis_revision
    assert first.provider == "longbridge"
    assert first.adjustment == "forward"


def test_qmt_factor_contract_rejects_missing_or_non_finite_facts() -> None:
    missing = _factors().drop(columns=["dr"])
    with pytest.raises(ValueError, match="missing QMT factor fields"):
        build_qmt_price_basis_metadata(
            code="SH.600926",
            adjustment="front",
            structure_price_quantum=Decimal("0.01"),
            factors=missing,
        )

    invalid = _factors()
    invalid.loc[0, "dr"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        build_qmt_price_basis_metadata(
            code="SH.600926",
            adjustment="front",
            structure_price_quantum=Decimal("0.01"),
            factors=invalid,
        )


def test_merge_metadata_uses_new_basis_and_rejects_cross_basis() -> None:
    old = pd.DataFrame({"close": [10.0]})
    new = pd.DataFrame({"close": [11.0]})
    target = pd.DataFrame({"close": [10.0, 11.0]})
    original = build_qmt_price_basis_metadata(
        code="SH.600926",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=_factors(),
    )
    attach_price_basis_metadata(old, original)
    attach_price_basis_metadata(new, original)
    merge_price_basis_metadata(old, new, target)
    assert target.attrs["price_basis_revision"] == original.price_basis_revision

    revised = build_qmt_price_basis_metadata(
        code="SH.600926",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=_factors(dr=1.03),
    )
    attach_price_basis_metadata(new, revised)
    with pytest.raises(PriceBasisMismatchError):
        merge_price_basis_metadata(old, new, target)


def test_unknown_cached_basis_is_not_mixed_with_known_new_basis() -> None:
    old = pd.DataFrame({"close": [10.0]})
    new = pd.DataFrame({"close": [11.0]})
    target = pd.DataFrame({"close": [10.0, 11.0]})
    metadata = build_qmt_price_basis_metadata(
        code="SH.600926",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=_factors(),
    )
    attach_price_basis_metadata(new, metadata)

    with pytest.raises(PriceBasisMismatchError, match="unknown cached"):
        merge_price_basis_metadata(old, new, target)


def test_missing_new_metadata_clears_old_formal_identity() -> None:
    old = pd.DataFrame({"close": [10.0]})
    new = pd.DataFrame({"close": [11.0]})
    target = pd.DataFrame({"close": [10.0, 11.0]})
    metadata = build_qmt_price_basis_metadata(
        code="SH.600926",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=_factors(),
    )
    attach_price_basis_metadata(old, metadata)
    new.attrs["price_basis_error_code"] = "qmt_factor_read_failed"

    merge_price_basis_metadata(old, new, target)

    assert "price_basis_revision" not in target.attrs
    assert "structure_price_quantum" not in target.attrs
    assert target.attrs["price_basis_error_code"] == "qmt_factor_read_failed"


def test_merge_preserves_provider_ohlc_normalization_audit() -> None:
    cached = pd.DataFrame({"close": [10.0]})
    new = pd.DataFrame({"close": [11.0]})
    target = pd.DataFrame({"close": [10.0, 11.0]})
    metadata = build_provider_price_basis_metadata(
        provider="longbridge",
        market="us",
        code="AAPL.US",
        adjustment="forward",
        structure_price_quantum=Decimal("0.001"),
        normalization_revision="ohlc-envelope",
    )
    attach_price_basis_metadata(cached, metadata)
    attach_price_basis_metadata(new, metadata)
    new.attrs.update(
        ohlc_geometry_normalization="ohlc-envelope",
        ohlc_geometry_repair_count=1,
        ohlc_geometry_max_adjustment=0.049,
    )

    merge_price_basis_metadata(cached, new, target)

    assert target.attrs["ohlc_geometry_normalization"] == "ohlc-envelope"
    assert target.attrs["ohlc_geometry_repair_count"] == 1
    assert target.attrs["ohlc_geometry_max_adjustment"] == pytest.approx(0.049)
