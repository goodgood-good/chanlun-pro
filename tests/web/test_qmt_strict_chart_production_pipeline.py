from __future__ import annotations

from decimal import Decimal

import pandas as pd

from chanlun.cl_utils import (
    build_strict_chart_cd,
    cl_data_to_tv_chart,
    query_cl_chart_config,
)
from chanlun.core.cl import CL
from chanlun.decision_support.trading_system.runtime_config import strict_cl_config
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_qmt_price_basis_metadata,
)


def test_qmt_frame_reaches_replace_payload_with_fixed_strict_config() -> None:
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet").tail(1500)
    frame = frame.reset_index(drop=True)
    metadata = build_qmt_price_basis_metadata(
        code="SH.600519",
        adjustment="front",
        structure_price_quantum=Decimal("0.01"),
        factors=pd.DataFrame(),
    )
    attach_price_basis_metadata(frame, metadata)

    legacy_config = query_cl_chart_config("a", "SH.600519")
    legacy_cd = CL("SH.600519", "5m", dict(legacy_config), market="a")
    legacy_cd.process_klines(frame)
    strict_runtime = build_strict_chart_cd(
        market="a",
        code="SH.600519",
        frequency="5m",
        frame=frame,
    )

    assert strict_runtime.error_code is None
    assert strict_runtime.cd is not None
    expected_strict_config = strict_cl_config(
        structure_price_quantum=Decimal("0.01"),
        price_basis_revision=metadata.price_basis_revision,
    )
    for key, value in expected_strict_config.items():
        assert strict_runtime.cd.get_config()[key] == value

    payload = cl_data_to_tv_chart(
        legacy_cd,
        legacy_config,
        strict_runtime=strict_runtime,
    )

    assert payload["strict_structure_mode"] == "replace"
    strict = payload["strict_structure"]
    assert strict["schema"] == "chanlun-chart-structure/v5"
    assert strict["symbol"] == "SH.600519"
    assert strict["source_frequency"] == "5m"
    assert strict["display_frequency"] == "5m"
    assert strict["price_basis_revision"] == metadata.price_basis_revision
    assert strict["structure_price_quantum"] == "0.01"
    assert strict["source_closed_at"] == int(frame.iloc[-1]["date"].timestamp())
    assert payload["c"][-1] == float(frame.iloc[-1]["close"])
