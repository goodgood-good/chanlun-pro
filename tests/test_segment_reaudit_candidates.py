"""Known positive/negative hypotheses for the independent review scanner.

F1/F2 refer to local L067 lines 49-109; invalidation to L078 lines 214-220.
Prices are synthetic, not asserted to be a figure from the original text.
"""

import gzip
import json

from script import review_segment_gapped_candidates as scanner
from script.review_segment_gapped_candidates import turns, second_outcome
from script.explain_interior_segment_candidates import protected_suffix


def pens(values):
    return [{"start_tick": a, "end_tick": b} for a, b in zip(values, values[1:])]


def test_gapped_internal_hypothesis_can_complete_before_the_later_extreme():
    source = pens([0, 10, 6, 20, 15, 18, 10, 13, 12, 17, 8, 25])
    candidate = next(turns(source, 0, 10, True))
    assert (candidate["pivot"], candidate["first_witness"]) == (3, 5)
    suffix = protected_suffix(source, 3, 5, True)
    assert suffix["status"] == "independent_right"
    assert suffix["middle"] == [15, 20, [3]]
    assert suffix["right"] == [10, 18, [5]]
    outcome = second_outcome(source, 3, 10, True)
    assert outcome["status"] == "second_fractal_formed" and outcome["witness"] == 8


def test_new_original_extreme_without_second_fractal_cancels_hypothesis():
    source = pens([0, 10, 6, 20, 15, 18, 12, 17, 10, 13, 8, 25])
    assert next(turns(source, 0, 10, True))["pivot"] == 3
    outcome = second_outcome(source, 3, 10, True)
    assert outcome["status"] == "invalidated_before_second_fractal" and outcome["witness"] == 10


def test_full_standard_middle_cannot_keep_sources_before_its_own_boundary():
    source = pens([0, 10, 6, 13, 11, 22, 9, 21, 7, 15, 6, 30])
    candidate = next(turns(source, 0, 10, True))
    assert candidate["pivot"] == 5
    middle = candidate["first_sequence"][1]
    assert list(middle[2]) == [3, 5]
    suffix = protected_suffix(source, 5, candidate["first_witness"], True)
    assert suffix["middle"] == [9, 22, [5]]
    assert suffix["middle"] != [middle[0], middle[1], list(middle[2])]


def test_review_keeps_a_valid_earlier_ending_as_a_question(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "OUT", tmp_path)
    source = pens([0, 10, 6, 20, 15, 18, 10, 13, 12, 17, 8, 25])
    (tmp_path / "geometry").mkdir()
    record = {
        "components": [{"component": 0, "pens": source, "decisions": []}],
        "rows": [{"segment_id": "synthetic:S0001", "number": 1, "component": 0,
                  "start_pen": 0, "end_pen": 10, "direction": "up", "start_tick": 0}],
    }
    (tmp_path / "geometry/synthetic.json.gz").write_bytes(gzip.compress(json.dumps(record).encode()))
    result = scanner.review({"id": "synthetic", "code": "SYNTHETIC", "frequency": "1m"})
    assert result["counts"]["needs_reference_and_hypothesis_review"] == 1
    assert result["needs_review"][0]["second_outcome"]["witness"] == 8
