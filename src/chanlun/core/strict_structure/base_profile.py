"""Source-faithful base structure profile.

The profile fixes one production definition for K-line inclusion, fractals,
strokes, segments, and MACD.  Runtime price-basis fields are intentionally
added by the strict runtime factory rather than accepted as algorithm switches
here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Union

STRICT_BASE_PROFILE_ID = "chanlun-source-faithful-base"


_STRICT_BASE_CONFIG: Dict[str, Union[str, int, bool]] = {
    "strict_base_profile_id": STRICT_BASE_PROFILE_ID,
    # L062/L065: directional, chronological inclusion before three-K fractals.
    "kline_inclusion_rule": "directional-sequential",
    "fractal_rule": "three-cl-k-both-extremes",
    "stroke_rule": "strict-cl-k-distance",
    # L067/L071: feature-sequence segmentation with explicit gap handling.
    "segment_rule": "feature-sequence",
    "segment_gap_rule": "second-feature-sequence-fractal",
    # Physical centers use entry + middle-three core + maturity as one five-role
    # window. Maturity is either an initial leave or the first extension. A
    # completed leave may simultaneously be the next center's entry, and later
    # departures may use either direction. Recursive centers retain the original
    # three-trend core and obtain their shared entry from the preceding trend.
    "center_seed_rule": "shared-leave-entry-three-core-five-role",
    "center_lifecycle_rule": "bidirectional-shared-leave-first-return-event",
    "center_scan_rule": "post-third-point-first-mature-causal-owner",
    # A/C must use the same same-level width.  A valid three-unit incoming leg
    # is enter/reverse/re-enter with its first unit strictly outside the frozen
    # center interval; only then must C wait for leave/return/re-leave.
    "trend_divergence_rule": (
        "entry-width-matched-one-or-three-price-extreme-any-macd-decay"
    ),
    "decomposition_rule": "matched-leg-terminal-prefix-partition",
    # L044/L053: a lower-level reversal may cross multiple levels and produce a
    # higher-level second point even when that target level has no first point.
    # Every target must still be confirmed by the reverse third point of its
    # own direct sub-level's dynamic last center.
    "second_class_rule": (
        "parent-or-cross-level-small-large-direct-subcenter-third-retest"
    ),
    # The strength measure is fixed too; it is evidence, never a definition
    # switch for K/fractal/stroke/segment structure.
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
    # Formal evidence uses a prefix-stable partial higher-timeframe MACD.  For
    # upward legs only positive bars enter area; for downward legs only the
    # magnitude of negative bars enters area.  HTF unavailability fails closed
    # rather than switching the meaning of the rule to native MACD.
    "strict_macd_source": "causal_htf",
    "strict_macd_htf_policy": "level_plus_one",
    "strict_macd_area": "same_sign_magnitude",
    "strict_macd_decay_rule": "area-or-peak-or-dif",
}


def strict_base_config() -> dict:
    """Return a fresh copy of the fixed production base profile."""

    return dict(_STRICT_BASE_CONFIG)


def strict_base_config_revision() -> str:
    """Return a deterministic revision of the complete fixed base profile."""

    encoded = json.dumps(
        _STRICT_BASE_CONFIG,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
