from __future__ import annotations

from decimal import Decimal

import pandas as pd

from chanlun.cl_utils import build_strict_chart_cd, cl_data_to_tv_chart
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_qmt_price_basis_metadata,
)


def test_qmt_frame_reaches_the_single_strict_chart_pipeline() -> None:
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet").tail(1500)
    frame = frame.reset_index(drop=True)
    metadata = build_qmt_price_basis_metadata(
        code="SH.600519",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=pd.DataFrame(),
    )
    attach_price_basis_metadata(frame, metadata)

    runtime = build_strict_chart_cd(
        market="a",
        code="SH.600519",
        frequency="5m",
        frame=frame,
    )

    assert runtime.error_code is None
    assert runtime.cd is not None
    expected_config = strict_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision=metadata.price_basis_revision,
    )
    assert runtime.cd.get_config() == expected_config

    payload = cl_data_to_tv_chart(
        frame,
        {
            "chart_show_fx": "1",
            "chart_show_bi": "1",
            "chart_show_xd": "1",
        },
        market="a",
        code="SH.600519",
        frequency="5m",
        strict_runtime=runtime,
    )

    assert payload is not None
    assert payload["strict_structure_mode"] == "replace"
    strict = payload["strict_structure"]
    assert strict["schema"] == "chanlun-chart-structure"
    assert strict["symbol"] == "SH.600519"
    assert strict["source_frequency"] == "5m"
    assert strict["display_frequency"] == "5m"
    assert strict["price_basis_revision"] == metadata.price_basis_revision
    assert strict["structure_price_quantum"] == "0.01"
    assert strict["source_closed_at"] == int(frame.iloc[-1]["date"].timestamp())
    assert strict["formal_direction"]["direction"] in {"up", "down", "neutral"}
    assert strict["formal_direction"]["reason_codes"]
    assert payload["c"][-1] == float(frame.iloc[-1]["close"])
    assert all(
        center["source_kind"] == "stroke_observation"
        and center["tradable"] is False
        and center["completion_phase"] == "NON_TRADABLE_OBSERVATION"
        for center in strict["stroke_center_observations"]
    )
