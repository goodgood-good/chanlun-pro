from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config
from chanlun.core.strict_structure.models import SourceKind


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "SH.600519_5m.parquet"


@pytest.fixture(scope="module")
def sample_frame():
    return (
        pd.read_parquet(FIXTURE)[
            ["date", "open", "high", "low", "close", "volume"]
        ]
        .head(1500)
        .reset_index(drop=True)
    )


def strict_config():
    return {
        **strict_base_config(),
        "structure_price_quantum": "0.01",
        "price_basis_revision": "test-raw-v1",
        "skip_legacy_zslx": True,
        "skip_legacy_mmd": True,
    }


def test_cl_exposes_strict_levels_during_transitional_read_only_phase(sample_frame):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(1000))
    legacy = cd.get_recursive_branch_levels()
    strict = cd.get_strict_structure_levels()

    assert strict.schema_version == "chanlun-structure/v3"
    assert strict.price_basis_revision == "test-raw-v1"
    assert legacy is cd.get_recursive_branch_levels()
    assert strict.levels
    assert all(
        item.source_kind is SourceKind.SEGMENT
        for item in strict.levels[0].units
    )


def test_stroke_observation_never_enters_strict_levels(sample_frame):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(1000))
    observed = cd.get_stroke_observation_centers()
    strict = cd.get_strict_structure_levels()

    assert all(
        center.source_kind is SourceKind.STROKE_OBSERVATION
        for center in observed.centers
    )
    assert all(
        center.source_kind is not SourceKind.STROKE_OBSERVATION
        for level in strict.levels
        for center in level.center_result.centers
    )


def test_cl_limits_recursive_depth_to_frequency_catalog(sample_frame, monkeypatch):
    from chanlun.core.strict_structure import recursive_engine

    original = recursive_engine.StrictRecursiveEngine
    captured = []

    class RecordingEngine(original):
        def __init__(self, max_levels=50):
            captured.append(max_levels)
            super().__init__(max_levels=max_levels)

    monkeypatch.setattr(recursive_engine, "StrictRecursiveEngine", RecordingEngine)
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(400))
    cd.get_strict_structure_levels()

    assert captured == [3]


def test_strict_memo_invalidates_but_lock_registry_survives_new_bars(sample_frame):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(600))
    first = cd.get_strict_structure_levels()
    assert first is cd.get_strict_structure_levels()
    registry = cd._strict_unit_registry

    cd.process_klines(sample_frame.head(1000))
    later = cd.get_strict_structure_levels()

    assert later is cd.get_strict_structure_levels()
    assert later is not first
    assert cd._strict_unit_registry is registry


def test_legacy_cl_construction_does_not_require_strict_metadata(sample_frame):
    cd = CL("SH.600519", "5m", strict_base_config())
    cd.process_klines(sample_frame.head(400))
    assert cd.get_xds() is not None

    with pytest.raises(ValueError, match="requires price_basis_revision"):
        cd.get_strict_structure_levels()


def test_strict_interface_rejects_invalid_quantum(sample_frame):
    config = strict_config()
    config["structure_price_quantum"] = "NaN"
    cd = CL("SH.600519", "5m", config)
    cd.process_klines(sample_frame.head(400))

    with pytest.raises(ValueError, match="positive finite price quantum"):
        cd.get_strict_structure_levels()


def test_price_basis_cannot_change_within_one_cl_lifecycle(sample_frame):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(600))
    cd.get_strict_structure_levels()
    cd.config["price_basis_revision"] = "post-action-v2"

    with pytest.raises(ValueError, match="price basis changed within CL lifecycle"):
        cd.get_strict_structure_levels()


def test_price_quantum_cannot_change_within_one_cl_lifecycle(sample_frame):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(600))
    cd.get_strict_structure_levels()
    cd.config["structure_price_quantum"] = "0.001"

    with pytest.raises(ValueError, match="price quantum changed within CL lifecycle"):
        cd.get_strict_structure_levels()


def test_strict_evidence_wraps_internal_contract_errors(sample_frame, monkeypatch):
    from chanlun.core.strict_structure.errors import StrictStructureContractError

    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(sample_frame.head(400))

    def invalid_structure():
        raise ValueError("unit directions must alternate")

    monkeypatch.setattr(cd, "get_strict_structure_levels", invalid_structure)

    with pytest.raises(
        StrictStructureContractError,
        match="unit directions must alternate",
    ):
        cd.get_strict_evidence()
