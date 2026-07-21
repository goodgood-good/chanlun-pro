from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.base_profile import (
    STRICT_BASE_PROFILE_ID,
    strict_base_config_revision,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
    strict_cl_config,
    strict_runtime_config_revision,
    strict_snapshot_price_metadata,
)


def test_runtime_revision_tracks_quantum_and_price_basis_canonically() -> None:
    base = strict_runtime_config_revision(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )

    assert base == strict_runtime_config_revision(
        structure_price_quantum=Decimal("0.0100"),
        price_basis_revision="raw-v1",
    )
    assert base != strict_runtime_config_revision(
        structure_price_quantum=Decimal("0.001"),
        price_basis_revision="raw-v1",
    )
    assert base != strict_runtime_config_revision(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="corp-action-2026-07-20",
    )


def test_strict_cl_config_contains_only_fixed_base_and_runtime_identity() -> None:
    config = strict_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )

    assert STRICT_STRATEGY_ID == "chanlun_source_faithful_v2"
    assert config["strict_base_profile_id"] == STRICT_BASE_PROFILE_ID
    assert config["strict_base_profile_revision"] == strict_base_config_revision()
    assert config["structure_price_quantum"] == "0.01"
    assert config["price_basis_revision"] == "raw-v1"
    assert config["strict_config_revision"] == strict_runtime_config_revision(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )
    forbidden_prefixes = ("chart_show_", "zs_", "recursive_")
    assert not any(key.startswith(forbidden_prefixes) for key in config)


def test_snapshot_metadata_is_required_and_never_guessed() -> None:
    snapshot = SimpleNamespace(
        attrs={
            "structure_price_quantum": "0.001",
            "price_basis_revision": "qfq-known-at-2026-07-20",
        }
    )

    metadata = strict_snapshot_price_metadata(snapshot)

    assert metadata.structure_price_quantum == Decimal("0.001")
    assert metadata.price_basis_revision == "qfq-known-at-2026-07-20"
    with pytest.raises(ValueError, match="structure_price_quantum metadata"):
        strict_snapshot_price_metadata(SimpleNamespace(attrs={}))
    with pytest.raises(ValueError, match="price_basis_revision metadata"):
        strict_snapshot_price_metadata(
            SimpleNamespace(attrs={"structure_price_quantum": "0.01"})
        )


@pytest.mark.parametrize(
    ("quantum", "basis"),
    (
        (Decimal("0"), "raw-v1"),
        (Decimal("NaN"), "raw-v1"),
        (Decimal("0.01"), ""),
        (Decimal("0.01"), " raw-v1"),
    ),
)
def test_runtime_config_rejects_invalid_identity(quantum, basis) -> None:
    with pytest.raises(ValueError):
        strict_cl_config(
            structure_price_quantum=quantum,
            price_basis_revision=basis,
        )
