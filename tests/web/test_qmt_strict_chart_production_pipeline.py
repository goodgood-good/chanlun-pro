from __future__ import annotations

from decimal import Decimal

import pandas as pd

from chanlun.cl_utils import (
    bi_stroke_centers_to_chart_dicts,
    build_strict_chart_cd,
    cl_data_to_tv_chart,
    query_cl_chart_config,
    xd_segment_centers_to_chart_dicts,
)
from chanlun.core.cl import CL
from chanlun.decision_support.trading_system.runtime_config import (
    v3_recursive_cl_config,
)
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
    expected_strict_config = v3_recursive_cl_config(
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

    # Every visible line, center and point consumes the fixed V3 old-pen graph.
    # The mutable legacy chart instance may still exist for compatibility, but
    # it is not allowed to define production structure geometry.
    assert legacy_cd.get_config()["bi_mode"] == "strict"
    assert strict_runtime.cd.get_config()["bi_mode"] == "strict"
    assert strict_runtime.cd.get_config()["pen_definition"] == "ORIGINAL_OLD_PEN"
    assert payload["bi_zss"] == bi_stroke_centers_to_chart_dicts(
        strict_runtime.cd.get_bis()
    )
    assert payload["xd_zss"] == xd_segment_centers_to_chart_dicts(
        strict_runtime.cd.get_xds()
    )
    assert all(center["core_line_count"] == 3 for center in payload["xd_zss"])
    assert all(
        all(
            previous != current
            for previous, current in zip(
                center["core_directions"],
                center["core_directions"][1:],
            )
        )
        for center in payload["xd_zss"]
    )
    for center in payload["xd_zss"]:
        if center["completion_point_type"] == "3buy":
            assert center["leaving_segment"]["direction"] == "up"
            assert center["completion_return_segment"]["direction"] == "down"
        elif center["completion_point_type"] == "3sell":
            assert center["leaving_segment"]["direction"] == "down"
            assert center["completion_return_segment"]["direction"] == "up"
    assert payload["bi_zss"]
    assert all(center["tower"] == "bi" for center in payload["bi_zss"])
    assert all(center["tradable"] is False for center in payload["bi_zss"])
    # 该窗口的旧计算器会给出一个把非进入/离开段并入主体的伪中枢；五段角色
    # 校验允许正确结果为空。上下两类非空几何由 test_segment_five_role_centers 覆盖。
    assert strict["display_center_observations"] == strict[
        "stroke_center_observations"
    ]
    assert {
        center["source_kind"]
        for center in strict["stroke_center_observations"]
    } == {"stroke_observation"}
