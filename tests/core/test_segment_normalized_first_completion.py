"""User-approved effective first-break boundary, 2026-09-21.

L079 supplies the same-side normalized-element figure. The user explicitly
selected the interpretation that continuation compares with that evolving
edge. These numeric regressions are not author-answered market examples.
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from chanlun.cl_utils.price_metadata import (
    strict_snapshot_price_metadata,
    strict_cl_config,
)
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("return_high", [11, 14])
def test_effective_end_break_does_not_wait_for_the_original_end(mirror, return_high):
    values = strokes([0, 10, 8, 14, 4, 9, 6, return_high, 5], mirror)
    calc = XdCalculator()
    calc.calculate(values[:-1])
    assert not any(s.done and s.end_line.index == 2 for s in calc.xds)
    calc.calculate(values)
    proof = next(p for p in calc.evidence if p.key[:2] == (0, 2))
    assert proof.witness_index == 7
    certificate = proof.first_break_evidence
    assert (certificate.first_pen_index, certificate.extension_index) == (3, 7)
    assert certificate.effective_first.source_indices == (3, 5)
    assert (certificate.effective_first.low, certificate.effective_first.high) == (
        (-14, -6) if mirror else (6, 14)
    )
    assert calc.xds[0].locked_at == values[7].locked_at
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_return_before_effective_completion_keeps_old_segment_open(mirror):
    values = strokes([0, 10, 8, 14, 4, 9, 6, 15, 5], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert not any(p.key[:2] == (0, 2) for p in calc.evidence)
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_later_origin_return_cannot_revoke_an_effective_completion(mirror):
    values = strokes([0, 10, 8, 14, 4, 9, 6, 11, 5, 15, 3, 12, 2], mirror)
    calc = XdCalculator()
    prior = {}
    proofs = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        check_evidence(calc, values[:stop])
        now = {
            (s.start_line.index, s.end_line.index, s.type): s.locked_at
            for s in calc.xds
            if s.done
        }
        evidence = {p.key: p for p in calc.evidence if p.key in now}
        assert all(
            now.get(k) == when and evidence.get(k) == proofs[k]
            for k, when in prior.items()
        )
        assert all(
            when == values[stop - 1].locked_at
            for k, when in now.items()
            if k not in prior
        )
        prior, proofs = now, evidence
    assert any(k[:2] == (0, 2) for k in prior)


@pytest.mark.parametrize("missing", [3, 4, 5, 6, 7])
def test_effective_interval_does_not_hide_unclosed_source_pens(missing):
    values = strokes([0, 10, 8, 14, 4, 9, 6, 11, 5])
    values[missing].locked_at = None
    calc = XdCalculator()
    calc.calculate(values)
    assert not any(s.done and s.end_line.index == 2 for s in calc.xds)


def test_checker_rebuilds_the_effective_interval_instead_of_trusting_it():
    values = strokes([0, 10, 8, 14, 4, 9, 6, 11, 5])
    calc = XdCalculator()
    calc.calculate(values)
    proof = calc.evidence[0]
    receipt = proof.first_break_evidence
    bad = replace(receipt, effective_first=replace(receipt.effective_first, low=7))
    calc.evidence = (replace(proof, first_break_evidence=bad),) + calc.evidence[1:]
    with pytest.raises(AssertionError, match="first_break_effective"):
        check_evidence(calc, values)


FORMATION_POINTS = [
    128240,
    124120,
    125140,
    123700,
    125090,
    124620,
    125400,
    121450,
    122000,
    121585,
    122030,
    121700,
    124960,
    123680,
    124400,
    123725,
    124360,
    123450,
    126080,
    123490,
    124950,
    124181,
    124699,
    123750,
    128780,
]


@pytest.mark.parametrize("mirror", [False, True])
def test_confirming_a_parent_cannot_split_its_normalized_continuation(mirror):
    values = strokes(FORMATION_POINTS, mirror)

    class RewindInsideFormation(XdCalculator):
        @staticmethod
        def _successor_formation_end(parent):
            return None

    malformed = RewindInsideFormation()
    malformed.calculate(values)
    with pytest.raises(AssertionError, match="successor_splits_predecessor_formation"):
        check_evidence(malformed, values)
    correct = XdCalculator()
    correct.calculate(values)
    assert [p.key[:2] for p in correct.evidence][:3] == [(0, 2), (3, 5), (6, 16)]
    check_evidence(correct, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_unfinished_successor_preview_retains_the_whole_formation_span(mirror):
    values = strokes(FORMATION_POINTS[:18], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    tail = calc.xds[-1]
    assert not tail.done and (tail.start_line.index, tail.end_line.index) == (6, 16)
    assert calc.tail_state.minimum_end_index == 16


def test_baba_user_partition_from_the_authenticated_market_window():
    path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/segment_normalized/BABA_US_5m.parquet"
    )
    frame = pd.read_parquet(path)
    meta = strict_snapshot_price_metadata(frame)
    cd = CL(
        "BABA.US",
        "5m",
        strict_cl_config(
            structure_price_quantum=meta.structure_price_quantum,
            price_basis_revision=meta.price_basis_revision,
        ),
        market="us",
    )
    cd.process_klines(frame, last_bar_closed=True)
    keys = {(s.start_line.index + 1, s.end_line.index + 1): s for s in cd.get_xds()}
    for key, prices in [
        ((182, 184), (123.7, 125.4)),
        ((185, 195), (125.4, 123.45)),
        ((196, 212), (123.45, 130.61)),
    ]:
        line = keys[key]
        assert line.done and (line.start.val, line.end.val) == prices
    first = keys[(182, 184)]
    assert first.construction_evidence.witness_index + 1 == 195
    receipt = first.construction_evidence.first_break_evidence
    assert receipt.effective_first.low == 123.725
    assert receipt.effective_first.source_indices == (184, 186, 188, 190, 192)
    # User-selected inclusion rule: B195 breaks the merged endpoint even
    # though it does not break B185's original 121.45 endpoint.
    pens = cd.get_contiguous_bis()
    assert pens[receipt.first_pen_index].end.val == 121.45
    assert pens[receipt.extension_index].end.val == 123.45
    assert (
        pens[receipt.first_pen_index].end.val
        < pens[receipt.extension_index].end.val
        < receipt.effective_first.low
    )
    assert first.locked_at == pd.Timestamp("2026-08-14T15:35:00-04:00")
    check_evidence(cd.xd_calculator, cd.get_contiguous_bis())
