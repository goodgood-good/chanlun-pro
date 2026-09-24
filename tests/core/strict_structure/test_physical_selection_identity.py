"""Physical selection revisions must not overwrite another confirmation fact."""

import copy
from datetime import timedelta, timezone

from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from tests.core.strict_structure.test_unit_adapter import FakeLine
from tests.core.strict_structure.helpers import BASE
from chanlun.core.cl import CL
from tests.core.strict_structure.real_history import load_frame, strict_config


def _physical_stroke():
    line = FakeLine(0, "up", 100, 120, done=True)
    line.fractal_visible_at = line.end.k.date + timedelta(minutes=1)
    line.selected_at = line.fractal_visible_at
    return line


def _adapt(lines, registry):
    return adapt_lines(lines, 0, SourceKind.STROKE_OBSERVATION, "0.01",
                       BASE + timedelta(days=1), registry)


def test_reselected_physical_evidence_keeps_both_confirmation_records():
    registry, original = UnitLockRegistry("test-raw"), _physical_stroke()
    before = _adapt([original], registry)[0]
    changed = copy.deepcopy(original)
    changed.selected_at += timedelta(minutes=10)
    changed.locked_at += timedelta(minutes=10)
    after = _adapt([changed], registry)[0]
    assert before.unit_id != after.unit_id
    assert registry._confirmed_at[before.unit_id] == before.confirmed_at
    assert registry._confirmed_at[after.unit_id] == after.confirmed_at
    assert _adapt([original], registry)[0] == before


def test_evidence_revision_normalizes_equivalent_instants_and_price_ticks():
    registry, original = UnitLockRegistry("test-raw"), _physical_stroke()
    before = _adapt([original], registry)[0]
    translated = copy.deepcopy(original)
    tz = timezone(timedelta(hours=8))
    for field in ("selected_at", "fractal_visible_at", "locked_at", "formed_at"):
        setattr(translated, field, getattr(translated, field).astimezone(tz))
    translated.start.k.date = translated.start.k.date.astimezone(tz)
    translated.end.k.date = translated.end.k.date.astimezone(tz)
    translated.start.val = "100.000"
    translated.end.val = "120.000"
    assert _adapt([translated], registry)[0].unit_id == before.unit_id


def test_qqq_unchanged_confirmation_keeps_its_first_identity_after_a_later_choice():
    # The window's initial up-origin is invalidated before it forms a valid
    # boundary. Use the recovered 35 -> 271 proof once it is actually available.
    frame = load_frame("QQQ.US_30m.parquet", 338)
    live = CL("QQQ.US", "30m", strict_config(), market="us")
    snapshots = []
    for count in (337, 338):
        live.process_klines(frame.head(count))
        units = adapt_lines(live.get_xds(), 0, SourceKind.SEGMENT, "0.01",
                            frame.iloc[count - 1].date, live._strict_registry(),
                            constituent_lines=live.get_bis())
        snapshots.append(next(unit for line, unit in zip(live.get_xds(), units)
                              if (line.start.k.k_index, line.end.k.k_index) == (35, 271)))
    before, after = snapshots
    # Resolving a later candidate must not re-date an unchanged earlier proof.
    assert before.confirmed_at is not None
    assert before.confirmed_at == after.confirmed_at
    assert before.unit_id == after.unit_id
    registry = live._strict_registry()
    assert registry._confirmed_at[before.unit_id] == before.confirmed_at
    assert registry._confirmed_at[after.unit_id] == after.confirmed_at
    cold = CL("QQQ.US", "30m", strict_config(), market="us")
    cold.process_klines(frame)
    units = adapt_lines(cold.get_xds(), 0, SourceKind.SEGMENT, "0.01", frame.iloc[-1].date,
                        cold._strict_registry(), constituent_lines=cold.get_bis())
    assert after in units
