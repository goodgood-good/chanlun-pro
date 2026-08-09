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

from chanlun.core.types.config import Config


STRICT_BASE_PROFILE_ID = "chanlun-source-faithful-base-v10"


_STRICT_BASE_CONFIG: Dict[str, Union[str, int, bool]] = {
    "strict_base_profile_id": STRICT_BASE_PROFILE_ID,
    # L062/L065: directional, chronological inclusion before three-K fractals.
    "kline_type": Config.KLINE_TYPE_CHANLUN.value,
    "kline_qk": Config.KLINE_QK_NONE.value,
    "kline_inclusion_rule": "directional-sequential-v1",
    "fx_qy": Config.FX_QY_THREE.value,
    "fx_qj": Config.FX_QJ_CK.value,
    "fx_bh": Config.FX_BH_NO.value,
    "fractal_rule": "three-cl-k-both-extremes-v1",
    # L065: the only production stroke definition is the new-stroke rule.
    "bi_type": Config.BI_TYPE_NEW.value,
    "bi_mode": "new",
    "bi_bzh": Config.BI_BZH_YES.value,
    "bi_qj": Config.BI_QJ_DD.value,
    "bi_fx_cgd": Config.BI_FX_CHD_NO.value,
    "bi_rule": "new-stroke-source-distance-v1",
    # L067/L071: feature-sequence segmentation with explicit gap handling.
    "xd_qj": Config.XD_QJ_DD.value,
    "xd_bzh": Config.XD_BZH_YES.value,
    "xd_bi_pohuai": Config.XD_BI_POHUAI_NO.value,
    "xd_rule": "feature-sequence-v1",
    "xd_gap_rule": "second-feature-sequence-fractal-v1",
    # Physical centers use entry + middle-three core + maturity as one five-role
    # window. Maturity is either an initial leave or the first extension. A
    # completed leave may simultaneously be the next center's entry, and later
    # departures may use either direction. Recursive centers retain the original
    # three-trend core and obtain their shared entry from the preceding trend.
    "center_seed_rule": "shared-leave-entry-three-core-five-role-v7",
    "center_lifecycle_rule": "bidirectional-shared-leave-first-return-event-v6",
    "center_scan_rule": "post-third-point-first-mature-causal-owner-v1",
    # L037/L043: compare complete same-level departure legs (the terminal c
    # contains the last center's third point), and let confirmed divergence
    # close the fixed same-level decomposition at its causal endpoint.
    "trend_divergence_rule": "identified-complete-c-price-extreme-any-decay-v2",
    "decomposition_rule": "confirmed-divergence-partition-replay-v2",
    # L044/L053: a lower-level reversal may cross multiple levels and produce a
    # higher-level second point even when that target level has no first point.
    # Every target must still be confirmed by the reverse third point of its
    # own direct sub-level's dynamic last center.
    "second_class_rule": (
        "parent-or-cross-level-small-large-direct-subcenter-third-retest-v3"
    ),
    # The strength measure is fixed too; it is evidence, never a definition
    # switch for K/fractal/stroke/segment structure.
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
    "use_macd_ld": True,
    # Formal divergence boundaries must be prefix-stable.  Interpolated HTF
    # MACD rewrites the current bucket's historical source bars, so the strict
    # evidence path uses native source-frequency MACD.
    "macd_ld_use_htf": False,
}


def strict_base_config() -> dict:
    """Return a fresh copy so ``CL`` may add transitional compatibility keys."""

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
