# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import _round_floats, canonical_json


def test_round_floats_nested():
    obj = {"b": 1.123456789, "a": [2.987654321, {"c": 3.0000000004}]}
    assert _round_floats(obj, 8) == {"b": 1.12345679, "a": [2.98765432, {"c": 3.0}]}


def test_canonical_json_sorts_keys_and_rounds():
    s = canonical_json({"b": 1.0, "a": 2.123456789}, 8)
    assert s == '{\n  "a": 2.12345679,\n  "b": 1.0\n}'
