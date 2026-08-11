"""Canonical frame-to-evidence runtime for strict physical-timeframe signals."""

from __future__ import annotations

from datetime import datetime
from typing import cast

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_structure import (
    build_screening_evidence,
)


def screening_evidence_from_frame(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    market: str = "a",
) -> StrictEvidenceResult:
    """Build canonical recursive strict evidence from a closed-bar frame.

    Chart adapters, live screening, historical replay and stock selection must
    enter the structure engine here.  Every consumer receives the same
    physical-frequency recursive structure graph.
    """

    if not isinstance(code, str) or not code:
        raise ValueError("screening code is required")
    if not isinstance(frequency, str) or not frequency:
        raise ValueError("screening frequency is required")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("screening frame must contain closed bars")
    if "date" not in frame.columns:
        raise ValueError("screening frame requires date")
    closed_at = normalize_datetime(as_of, "as_of")
    latest = normalize_datetime(
        pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime(),
        "latest frame close",
    )
    if latest > closed_at:
        raise ValueError("screening frame contains bars after as_of")

    metadata = strict_snapshot_price_metadata(frame)
    config = strict_cl_config(
        structure_price_quantum=metadata.structure_price_quantum,
        price_basis_revision=metadata.price_basis_revision,
    )
    state = CL(code, frequency, config, market=market)
    state.process_klines(frame)
    return build_screening_evidence(
        state,
        source_closed_at=closed_at,
        structure_price_quantum=metadata.structure_price_quantum,
        price_basis_revision=metadata.price_basis_revision,
        strict_config_revision=cast(str, config["strict_config_revision"]),
    )


__all__ = ("screening_evidence_from_frame",)
