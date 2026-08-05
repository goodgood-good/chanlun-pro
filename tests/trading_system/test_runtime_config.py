from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.base_profile import (
    STRICT_BASE_PROFILE_ID,
    strict_base_config_revision,
)
from chanlun.decision_support.trading_system.runtime_config import (
    SCREENING_STRUCTURE_PROFILE_ID,
    STRICT_STRATEGY_ID,
    V3_RECURSIVE_STRUCTURE_PROFILE_ID,
    screening_cl_config,
    screening_runtime_config_revision,
    strict_cl_config,
    strict_runtime_config_revision,
    strict_snapshot_price_metadata,
    v3_recursive_base_config_revision,
    v3_recursive_cl_config,
    v3_recursive_runtime_config_revision,
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


def test_screening_config_is_old_pen_and_non_recursive_level_zero() -> None:
    config = screening_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )

    assert config["strict_base_profile_id"] == SCREENING_STRUCTURE_PROFILE_ID
    assert config["bi_type"] == "bi_type_old"
    assert config["bi_mode"] == "strict"
    assert config["screening_structure_scope"] == "physical-timeframe-level-zero"
    assert config["screening_center_source"] == "segment"
    assert config["screening_recursive_structure"] is False
    assert config["screening_unfinished_segment_participates"] is True
    assert config["strict_config_revision"] == screening_runtime_config_revision(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )


def test_v3_recursive_config_uses_original_old_pen_at_every_level() -> None:
    config = v3_recursive_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )

    assert config["strict_base_profile_id"] == V3_RECURSIVE_STRUCTURE_PROFILE_ID
    assert config["bi_type"] == "bi_type_old"
    assert config["bi_mode"] == "strict"
    assert config["pen_definition"] == "ORIGINAL_OLD_PEN"
    assert config["recursive_structure_scope"] == "same-source-direct-recursion"
    assert config["strict_base_profile_revision"] == (
        v3_recursive_base_config_revision()
    )
    assert config["strict_config_revision"] == (
        v3_recursive_runtime_config_revision(
            structure_price_quantum=Decimal("0.01"),
            price_basis_revision="raw-v1",
        )
    )
    assert config["strict_config_revision"] != screening_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision="raw-v1",
    )["strict_config_revision"]


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
