from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (
    DirectRecursiveStructurePath,
)
from chanlun.decision_support.trading_system.v3_qmt_direct_recursive_path import (
    build_qmt_v3_direct_recursive_path,
)
from chanlun.decision_support.trading_system.v3_qmt_same_base_stream import (
    QmtSameBaseStreamFrames,
)
from chanlun.decision_support.trading_system.v3_selection import (
    TechnicalEntrySnapshot,
)
from tests.trading_system.helpers import POINT_AT


BASE = "sha256:" + "b" * 64
BASIS = "test-raw-v1"


def _frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "date": [POINT_AT + timedelta(minutes=120)],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            "volume": [1000.0],
        }
    )
    frame.attrs.update(
        source_base_stream_revision=BASE,
        derived_frequency="1m",
        structure_price_quantum="0.01",
        price_basis_revision=BASIS,
    )
    return frame


def _source() -> QmtSameBaseStreamFrames:
    one = _frame()
    derived = one.copy()
    return QmtSameBaseStreamFrames(
        symbol="SH.600000",
        observed_at=POINT_AT + timedelta(minutes=121),
        one_minute=one,
        five_minute=derived,
        thirty_minute=derived,
        daily=derived,
        source_base_stream_revision=BASE,
        price_basis_revision=BASIS,
        complete_sessions=(date(2026, 7, 20),),
        partial_session=None,
        session_issues=(),
        grade="FULL_SYSTEM_ELIGIBLE",
        blockers=(),
    )


def _entry(minutes_after: int) -> TechnicalEntrySnapshot:
    observed = POINT_AT + timedelta(minutes=minutes_after)
    return TechnicalEntrySnapshot(
        structure_snapshot_id="sha256:" + "c" * 64,
        observed_at=observed,
        price_basis_revision=BASIS,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=True,
        all_components_completed=True,
        l0_center_id="center-l2",
        l0_center_ordinal=1,
        l0_center_completed=True,
        l0_point_type="3buy",
        l0_point_id="point-l2",
        l0_point_confirmation_time=observed,
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=Decimal("10"),
        l0_zg=Decimal("9.8"),
        l2_locator="L2_FIRST_BUY",
        l2_point_id="point-l0",
        l2_confirmation_bar_high=Decimal("10"),
    )


def _patch_structure(monkeypatch, direct: DirectRecursiveStructurePath) -> None:
    evidence = SimpleNamespace(
        price_basis_revision=BASIS,
        structure_revision="structure-v1",
    )
    state = SimpleNamespace(
        process_klines=lambda _frame: None,
        get_strict_evidence=lambda: evidence,
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system."
        "v3_qmt_direct_recursive_path.strict_state",
        lambda *_args: state,
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system."
        "v3_qmt_direct_recursive_path."
        "build_v3_direct_recursive_structure_path",
        lambda **_kwargs: direct,
    )


def test_qmt_direct_path_is_primary_and_does_not_reuse_old_entry(
    monkeypatch,
) -> None:
    old = _entry(60)
    current = _entry(120)
    direct = DirectRecursiveStructurePath(
        symbol="SH.600000",
        structure_revision="structure-v1",
        structure_snapshot_id="sha256:" + "d" * 64,
        strategic_points=(),
        decisions=(),
        technical_entries=(old, current),
        rejection_counts=(),
        resolved_nine_segment_count=0,
        unresolved_nine_segment_count=0,
        relevant_expansion_count=0,
        grade="RESEARCH_ONLY",
    )
    _patch_structure(monkeypatch, direct)

    result = build_qmt_v3_direct_recursive_path(source=_source())

    assert result.historical_aligned_entry_count == 2
    assert result.current_technical_entries == (current,)
    assert result.aligned_entry_count == 1
    assert result.grade == "RESEARCH_ONLY"
    assert result.live_status == "LIVE_DISABLED"


def test_qmt_direct_path_propagates_unresolved_structure(monkeypatch) -> None:
    direct = DirectRecursiveStructurePath(
        symbol="SH.600000",
        structure_revision="structure-v1",
        structure_snapshot_id="sha256:" + "d" * 64,
        strategic_points=(),
        decisions=(),
        technical_entries=(),
        rejection_counts=(("LESS_THAN_THREE_RECURSIVE_LEVELS", 1),),
        resolved_nine_segment_count=0,
        unresolved_nine_segment_count=0,
        relevant_expansion_count=0,
        grade="UNRESOLVED",
    )
    _patch_structure(monkeypatch, direct)

    result = build_qmt_v3_direct_recursive_path(source=_source())

    assert result.grade == "UNRESOLVED"
    assert {item.code for item in result.blockers} == {
        "QMT_DIRECT_RECURSIVE_STRUCTURE_UNRESOLVED"
    }


def test_qmt_direct_path_rejects_crossed_base_stream() -> None:
    source = _source()
    source.one_minute.attrs["source_base_stream_revision"] = "sha256:" + "e" * 64

    with pytest.raises(ValueError, match="crossed base streams"):
        build_qmt_v3_direct_recursive_path(source=source)

