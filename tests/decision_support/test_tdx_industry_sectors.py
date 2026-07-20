from __future__ import annotations

from chanlun.decision_support.tdx_industry_sectors import (
    build_tdx_industry_sector_catalog,
)


def test_catalog_maps_memberships_to_real_tdx_industry_indices() -> None:
    result = build_tdx_industry_sector_catalog(
        {
            "hy_codes": {
                "煤炭开采": ["600001", "600002"],
                "油气开采": ["600003"],
                "全国性银行": ["000001"],
                "地方性银行": ["600000", "601009"],
            }
        }
    )

    assert result["source"] == "tdx_880_industry_index"
    sectors = {item["kline_code"]: item for item in result["sectors"]}
    assert sectors["SH.880302"] == {
        "sector_id": "tdx-industry:SH.880302",
        "name": "煤炭开采",
        "kline_code": "SH.880302",
        "member_codes": ["600001", "600002"],
        "source_industries": ["煤炭开采"],
    }
    assert sectors["SH.880311"]["name"] == "石油开采"
    assert sectors["SH.880311"]["member_codes"] == ["600003"]
    assert sectors["SH.880471"]["member_codes"] == [
        "000001",
        "600000",
        "601009",
    ]
    assert sectors["SH.880471"]["source_industries"] == [
        "全国性银行",
        "地方性银行",
    ]


def test_catalog_never_emits_synthetic_ohlc_components() -> None:
    result = build_tdx_industry_sector_catalog(
        {"hy_codes": {"白色家电": ["000333"], "小家电": ["002032"]}}
    )

    assert result["unmapped_industries"] == []
    assert result["mapped_industry_count"] == 2
    assert result["sectors"] == [
        {
            "sector_id": "tdx-industry:SH.880387",
            "name": "家用电器",
            "kline_code": "SH.880387",
            "member_codes": ["000333", "002032"],
            "source_industries": ["小家电", "白色家电"],
        }
    ]
    assert all("component_codes" not in item for item in result["sectors"])
    assert all("weights" not in item for item in result["sectors"])
