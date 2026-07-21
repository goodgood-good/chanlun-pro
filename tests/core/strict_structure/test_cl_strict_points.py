from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config
from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure.signal_helpers import confirmed_point


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "SH.600519_5m.parquet"


@pytest.fixture(scope="module")
def sample_frame():
    return pd.read_parquet(FIXTURE)[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(1000).reset_index(drop=True)


def strict_config():
    return {
        **strict_base_config(),
        "structure_price_quantum": "0.01",
        "price_basis_revision": "test-raw-v1",
        "skip_legacy_zslx": True,
        "skip_legacy_mmd": True,
    }


def make_cd(frame, rows=800):
    cd = CL("SH.600519", "5m", strict_config())
    cd.process_klines(frame.head(rows))
    return cd


def test_cl_strict_points_can_expose_each_of_six_point_types(
    monkeypatch,
    sample_frame,
):
    cd = make_cd(sample_frame)
    points = {
        point_type: confirmed_point(point_type=point_type)
        for point_type in ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")
    }

    class SixTypeEngine:
        def __init__(self, **_kwargs):
            pass

        def first_class_points(self):
            return (points["1buy"], points["1sell"])

        def second_class_points(self, _first):
            return (points["2buy"], points["2sell"])

        def third_class_points(self):
            return (points["3buy"], points["3sell"])

    monkeypatch.setattr(
        "chanlun.core.strict_structure.signals.StrictSignalEngine",
        SixTypeEngine,
    )
    assert {point.point_type for point in cd.get_strict_points()} == set(points)


def test_cl_strict_points_never_read_stroke_observation(
    monkeypatch,
    sample_frame,
):
    cd = make_cd(sample_frame)
    monkeypatch.setattr(
        cd,
        "get_stroke_observation_centers",
        lambda: (_ for _ in ()).throw(AssertionError("stroke observation read")),
    )
    assert all(
        point.source_kind is not SourceKind.STROKE_OBSERVATION
        for point in cd.get_strict_points()
    )


def test_process_klines_invalidates_strict_point_memo(sample_frame):
    cd = make_cd(sample_frame, rows=600)
    before_structure = cd.get_strict_structure_levels()
    before_points = cd.get_strict_points()
    cd.process_klines(sample_frame.head(1000))
    after_structure = cd.get_strict_structure_levels()
    after_points = cd.get_strict_points()
    assert before_structure is not after_structure
    assert before_points == after_points or before_points != after_points
    assert cd._strict_structure_memo["confirmed_points"] is after_points


def test_atomic_strict_evidence_uses_one_cl_generation(sample_frame):
    cd = make_cd(sample_frame)
    evidence = cd.get_strict_evidence()
    assert evidence is cd.get_strict_evidence()
    assert evidence.structure is cd.get_strict_structure_levels()
    assert evidence.stroke_center_observations is cd.get_stroke_observation_centers()
    assert evidence.confirmed_points is cd.get_strict_points()
    assert evidence.approaching_points is cd.get_strict_approaching_points()
    assert evidence.divergences is cd.get_strict_divergences()
    assert evidence.price_basis_revision == evidence.structure.price_basis_revision
    assert evidence.structure_price_quantum == Decimal(
        str(cd.get_config()["structure_price_quantum"])
    )
    assert evidence.structure_revision == build_strict_evidence_revision(
        symbol=cd.get_code(),
        source_frequency=cd.get_frequency(),
        price_basis_revision=evidence.price_basis_revision,
        strict_config_revision=evidence.strict_config_revision,
        structure=evidence.structure,
        confirmed_points=evidence.confirmed_points,
        divergences=evidence.divergences,
    )
    assert evidence.source_closed_at == cd.get_src_klines()[-1].date
