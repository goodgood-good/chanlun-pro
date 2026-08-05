"""Build the strict chart CL from authoritative price-basis metadata."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from chanlun.cl_utils.data import web_batch_get_cl_datas
from chanlun.core.types import ICL
from chanlun.decision_support.trading_system.runtime_config import (
    strict_snapshot_price_metadata,
    v3_recursive_cl_config,
)
from chanlun.tools.log_util import LogUtil


@dataclass(frozen=True, slots=True)
class StrictChartRuntimeResult:
    cd: ICL | None
    error_code: str | None
    error_message: str | None

    @classmethod
    def success(cls, cd: ICL) -> "StrictChartRuntimeResult":
        if cd is None:
            raise ValueError("strict chart CL is required")
        return cls(cd=cd, error_code=None, error_message=None)

    @classmethod
    def unavailable(
        cls,
        error_code: str,
        error_message: str,
    ) -> "StrictChartRuntimeResult":
        if not error_code:
            raise ValueError("strict chart error code is required")
        return cls(cd=None, error_code=error_code, error_message=error_message)


def _failure(
    *,
    market: str,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    error_code: str,
    exc: Exception,
) -> StrictChartRuntimeResult:
    provider = frame.attrs.get("price_basis_provider", "unknown")
    adjustment = frame.attrs.get("price_basis_adjustment", "unknown")
    message = f"{type(exc).__name__}: {exc}"
    LogUtil.warning(
        "[strict_chart_runtime] unavailable "
        f"market={market} code={code} frequency={frequency} "
        f"provider={provider} adjustment={adjustment} "
        f"error_code={error_code} error={message}"
    )
    return StrictChartRuntimeResult.unavailable(error_code, message)


def build_strict_chart_cd(
    *,
    market: str,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
) -> StrictChartRuntimeResult:
    try:
        metadata = strict_snapshot_price_metadata(frame)
    except Exception as exc:
        return _failure(
            market=market,
            code=code,
            frequency=frequency,
            frame=frame,
            error_code="strict_price_metadata_unavailable",
            exc=exc,
        )
    try:
        # The production chart is part of the V3 decision surface.  Using the
        # general research profile here used NEW_PEN while screening, replay
        # and notifications used ORIGINAL_OLD_PEN, so a visible center and its
        # point could come from different segment chains.
        config = v3_recursive_cl_config(
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
        )
        values = web_batch_get_cl_datas(
            market,
            code,
            {frequency: frame},
            config,
        )
        if len(values) != 1:
            raise ValueError(
                "strict chart CL factory must return exactly one object"
            )
        return StrictChartRuntimeResult.success(values[0])
    except Exception as exc:
        return _failure(
            market=market,
            code=code,
            frequency=frequency,
            frame=frame,
            error_code="strict_evidence_invalid",
            exc=exc,
        )


__all__ = ("StrictChartRuntimeResult", "build_strict_chart_cd")
