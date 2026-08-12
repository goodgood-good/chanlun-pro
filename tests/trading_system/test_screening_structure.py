from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterPreview,
    CenterPreviewState,
    ConstituentUnit,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictStructureResult,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.decision_support.trading_system.provisional import (
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config
from chanlun.decision_support.trading_system.screening_structure import (
    build_screening_evidence,
)


NOW = datetime(2026, 7, 20, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
BASIS = "test-raw"
QUANTUM = Decimal("1")


def _unit(
    index: int,
    direction: str,
    start_tick: int,
    end_tick: int,
    *,
    locked: bool,
) -> ConstituentUnit:
    market_start = NOW - timedelta(minutes=7 - index)
    market_end = market_start + timedelta(minutes=1)
    return ConstituentUnit(
        unit_id=f"segment-{index}",
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=BASIS,
        direction=direction,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=market_start,
        market_end=market_end,
        confirmed_at=market_end if locked else None,
        available_at=market_end if locked else NOW,
        locked=locked,
        child_ids=(),
    )


def test_completed_preview_from_unfinished_segment_is_non_actionable() -> None:
    units = (
        _unit(1, "up", 8, 12, locked=True),
        _unit(2, "down", 12, 9, locked=True),
        _unit(3, "up", 9, 11, locked=True),
        _unit(4, "down", 11, 9, locked=True),
        _unit(5, "up", 9, 13, locked=False),
        _unit(6, "down", 13, 12, locked=False),
    )
    preview = CenterPreview(
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=BASIS,
        entry_unit_id=units[0].unit_id,
        unit_ids=tuple(unit.unit_id for unit in units[1:4]),
        state=CenterPreviewState.COMPLETED,
        zd_tick=9,
        zg_tick=11,
        available_at=NOW,
        completion_leave_unit_id=units[4].unit_id,
        completion_return_unit_id=units[5].unit_id,
        establishment_unit_id=units[4].unit_id,
        establishment_leave_unit_id=units[4].unit_id,
    )
    center_result = CenterLevelResult(
        structural_level=0,
        price_basis_revision=BASIS,
        centers=(),
        previews=(preview,),
        events=(),
        locked_unit_count=4,
        replay_from=0,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=units,
                center_result=center_result,
                trend_types=(),
                completed_trends=(),
            ),
        ),
    )
    revision = build_strict_evidence_revision(
        symbol="SZ.000001",
        source_frequency="5m",
        price_basis_revision=BASIS,
        strict_config_revision="strict-test-config",
        structure=structure,
        confirmed_points=(),
    )
    approaching = StrictSignalEngine(
        structure=structure,
        price_quantum=QUANTUM,
    ).approaching_points(NOW)
    evidence = StrictEvidenceResult(
        symbol="SZ.000001",
        source_frequency="5m",
        source_closed_at=NOW,
        price_basis_revision=BASIS,
        structure_price_quantum=QUANTUM,
        strict_config_revision="strict-test-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=CenterLevelResult(
            structural_level=0,
            price_basis_revision=None,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        ),
        confirmed_points=(),
        approaching_points=approaching,
    )

    [candidate] = extract_provisional_candidates(
        evidence,
        code="SZ.000001",
        source_frequency="5m",
        as_of=NOW,
    )

    assert candidate.point_type == "3buy"
    assert candidate.recursive_level == 0
    assert candidate.anchor_price == 12.0
    assert candidate.actionable is False
    assert "unfinished_segment_lock" in candidate.missing_conditions
    assert "unfinished_segment_participates" in candidate.evidence_codes


def test_empty_canonical_structure_has_no_unfinished_candidate() -> None:
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=BASIS,
        levels=(),
    )
    revision = build_strict_evidence_revision(
        symbol="qmt-gics3:test",
        source_frequency="30m",
        price_basis_revision=BASIS,
        strict_config_revision="strict-test-config",
        structure=structure,
        confirmed_points=(),
    )
    evidence = StrictEvidenceResult(
        symbol="qmt-gics3:test",
        source_frequency="30m",
        source_closed_at=NOW,
        price_basis_revision=BASIS,
        structure_price_quantum=QUANTUM,
        strict_config_revision="strict-test-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=CenterLevelResult(
            structural_level=0,
            price_basis_revision=None,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        ),
        confirmed_points=(),
        approaching_points=(),
    )

    assert extract_provisional_candidates(
        evidence,
        code="qmt-gics3:test",
        source_frequency="30m",
        as_of=NOW,
    ) == ()


def test_builder_returns_the_canonical_recursive_evidence_graph() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.date_range(
                "2026-07-20T14:59:00+08:00",
                periods=2,
                freq="min",
            ),
            "open": (10.0, 10.1),
            "high": (10.2, 10.3),
            "low": (9.9, 10.0),
            "close": (10.1, 10.2),
            "volume": (1000.0, 1200.0),
        }
    )
    config = strict_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision=BASIS,
    )
    cd = CL("SZ.000001", "5m", config, market="a")
    cd.process_klines(frame)

    evidence = build_screening_evidence(
        cd,
        source_closed_at=NOW,
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision=BASIS,
        strict_config_revision=str(config["strict_config_revision"]),
    )

    assert cd.config["stroke_rule"] == "strict-cl-k-distance"
    assert evidence == cd.get_strict_evidence()
    assert tuple(level.structural_level for level in evidence.structure.levels) == tuple(
        range(len(evidence.structure.levels))
    )
    assert all(
        unit.source_kind is SourceKind.SEGMENT
        for level in evidence.structure.levels[:1]
        for unit in level.units
    )
