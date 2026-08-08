from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import strict_base_config


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "strict_points_v10.json"


def strict_config():
    return {
        **strict_base_config(),
        "structure_price_quantum": "0.01",
        "price_basis_revision": "test-raw-v1",
        "skip_legacy_zslx": True,
        "skip_legacy_mmd": True,
    }


def load_frame(name, rows):
    return pd.read_parquet(FIXTURES / name)[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(rows).reset_index(drop=True)


def append_row(cd, row):
    cd.process_kline_values(
        row.date,
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
    )


def assert_point_prefix(name, code, frequency, start, end, *, require_points):
    frame = load_frame(name, end)
    incremental = CL(code, frequency, strict_config())
    incremental.process_klines(frame.head(start))
    frozen = {}

    for index in range(start, end):
        append_row(incremental, frame.iloc[index])
        evidence = incremental.get_strict_evidence()
        assert evidence.source_closed_at == frame.iloc[index].date
        current = {point.point_id: point for point in evidence.confirmed_points}
        assert len(current) == len(evidence.confirmed_points)
        for point_id, old in frozen.items():
            assert current[point_id] == old
        for point_id, point in current.items():
            assert point.available_at <= evidence.source_closed_at
            if point_id not in frozen:
                assert point.available_at == evidence.source_closed_at
        frozen = current

    batch = CL(code, frequency, strict_config())
    batch.process_klines(frame)
    assert batch.get_strict_evidence().confirmed_points == tuple(
        sorted(
            frozen.values(),
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )
    if require_points:
        assert frozen


def _golden_point(point):
    return {
        "point_id": point.point_id,
        "point_type": point.point_type,
        "level": point.structural_level,
        "center_id": point.center_id,
        "center_ordinal": point.center_ordinal,
        "anchor_at": point.anchor_at.isoformat(),
        "confirmed_at": point.confirmed_at.isoformat(),
        "available_at": point.available_at.isoformat(),
        "variant": point.variant.value,
        "strength_source": (
            None
            if point.divergence is None
            else point.divergence.strength_source
        ),
    }


def build_v10_golden_document():
    rows = 1100
    frame = load_frame("SZ.002299_1m.parquet", rows)
    cd = CL("SZ.002299", "1m", strict_config())
    cd.process_klines(frame)
    evidence = cd.get_strict_evidence()
    points = [_golden_point(point) for point in evidence.confirmed_points]
    assert points, "v10 golden fixture must remain non-vacuous"
    return {
        "code": "SZ.002299",
        "dataset": "tests/fixtures/SZ.002299_1m.parquet",
        "frequency": "1m",
        "points": points,
        "price_basis_revision": evidence.price_basis_revision,
        "rows": rows,
        "schema_version": "chanlun-strict-points-golden/v10",
        "source_closed_at": evidence.source_closed_at.isoformat(),
        "structure_price_quantum": str(evidence.structure_price_quantum),
    }


def test_maotai_real_point_ledger_is_prefix_stable():
    assert_point_prefix(
        "SH.600519_5m.parquet",
        "SH.600519",
        "5m",
        400,
        800,
        require_points=False,
    )


def test_zhongji_real_point_ledger_is_prefix_stable_and_non_vacuous():
    assert_point_prefix(
        "SZ.002299_1m.parquet",
        "SZ.002299",
        "1m",
        500,
        1100,
        require_points=True,
    )


def test_zhongji_real_points_match_audited_v10_golden():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    frame = load_frame("SZ.002299_1m.parquet", expected["rows"])
    cd = CL(expected["code"], expected["frequency"], strict_config())
    cd.process_klines(frame)
    evidence = cd.get_strict_evidence()

    assert expected["schema_version"] == "chanlun-strict-points-golden/v10"
    assert evidence.source_closed_at.isoformat() == expected["source_closed_at"]
    assert evidence.price_basis_revision == expected["price_basis_revision"]
    assert str(evidence.structure_price_quantum) == expected["structure_price_quantum"]
    assert [_golden_point(point) for point in evidence.confirmed_points] == expected[
        "points"
    ]
