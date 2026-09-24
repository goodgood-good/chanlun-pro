"""Real-input regressions for the earliest first-case completion.

L065 226-244, L071 100-124 and L078 208-220 are the local-corpus premises; these market
boundaries are independently reviewed deductions, not author-labelled figures.
The earlier accepted effective-end convention from L079 remains in force.

The previous cases expected later local boundaries, because the ordinary
contact route was missing. Retain those exact market inputs, and check the
earlier qualifying break as well as the absence of the superseded boundary.
"""
from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.cl_utils.price_metadata import strict_snapshot_price_metadata, strict_cl_config
from script.check_segment_model import check_evidence


CASES = [
    ('SH.600279', '1m', (150, 154), (4.45, 4.31), '2026-09-17T09:52:00+08:00',
     (159, 165), (4.34, 4.40), (155, 157), (4.35, 4.41)),
    ('SH.600004', '5m', (235, 245), (8.02, 7.40), '2026-08-20T09:50:00+08:00',
     (235, 247), (7.46, 7.51), (246, 248), (7.47, 7.48)),
    ('SH.600033', '5m', (170, 180), (3.48, 3.70), '2026-09-17T09:55:00+08:00',
     (170, 182), (3.60, 3.67), (181, 183), (3.63, 3.62)),
]


def calculate(code, frequency, frame):
    meta = strict_snapshot_price_metadata(frame)
    cd = CL(code, frequency, strict_cl_config(
        structure_price_quantum=meta.structure_price_quantum,
        price_basis_revision=meta.price_basis_revision,
    ), market='a')
    cd.process_klines(frame, last_bar_closed=True)
    check_evidence(cd.xd_calculator, cd.get_contiguous_bis())
    return cd


@pytest.mark.parametrize('code,frequency,boundary,prices,when,superseded,reference,break_pens,break_ends', CASES)
def test_earlier_completion_is_visible_at_its_real_clock_and_keeps_its_proof(
    code, frequency, boundary, prices, when, superseded, reference, break_pens, break_ends,
):
    path = Path(__file__).resolve().parents[1] / 'fixtures/segment_completion'
    frame = pd.read_parquet(path / f"{code.replace('.', '_')}_{frequency}.parquet")
    clock = pd.Timestamp(when)

    def target(cd):
        return next((s for s in cd.get_xds()
                     if (s.start_line.index + 1, s.end_line.index + 1) == boundary), None)

    before = target(calculate(code, frequency, frame.loc[frame.date < clock].copy()))
    assert before is None or not before.done

    at = target(calculate(code, frequency, frame.loc[frame.date <= clock].copy()))
    assert at is not None and at.done
    assert (at.start.val, at.end.val) == prices
    assert at.locked_at == clock
    assert at.construction_evidence.rule == 'first-pen-continuation'
    cert = at.construction_evidence.first_pen_continuation
    assert cert.reference_context == 'record'
    assert (cert.reference.low, cert.reference.high) == reference
    assert (cert.first_pen_index + 1, cert.extension_index + 1) == break_pens
    pens = calculate(code, frequency, frame).get_contiguous_bis()
    first, extension = pens[cert.first_pen_index], pens[cert.extension_index]
    assert (first.end.val, extension.end.val) == break_ends
    # The first reversal enters the reference but does not cross its far edge.
    if at.type == 'up':
        assert reference[0] <= first.end.val <= reference[1]
        assert extension.end.val < first.end.val
    else:
        assert reference[0] <= first.end.val <= reference[1]
        assert extension.end.val > first.end.val

    later = target(calculate(code, frequency, frame))
    assert later is not None and later.done
    assert later.locked_at == clock
    assert later.construction_evidence == at.construction_evidence
    assert not any((s.start_line.index + 1, s.end_line.index + 1) == superseded
                   for s in calculate(code, frequency, frame).get_xds())
