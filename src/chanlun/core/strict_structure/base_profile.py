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


STRICT_BASE_PROFILE_ID = "chanlun-source-faithful-base-v2"


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
    # L017/L020 and V3 §5.4: the first three consecutive lower-level
    # components establish a closed [ZD, ZG] center.  Departure and first
    # return are later lifecycle evidence, never extra seed roles.
    "center_seed_rule": "first-three-components-closed-overlap-v1",
    "center_lifecycle_rule": "departure-first-return-v1",
    # The strength measure is fixed too; it is evidence, never a definition
    # switch for K/fractal/stroke/segment structure.
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
    "use_macd_ld": True,
    "macd_ld_use_htf": True,
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
