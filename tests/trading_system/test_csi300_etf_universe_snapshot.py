from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "snapshot_csi300_etf_universe.py"
SPEC = importlib.util.spec_from_file_location("snapshot_csi300_etf_universe", TOOL)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


FIELDS = (
    "symbol",
    "secu_abbr",
    "chiname",
    "asset_type",
    "asset_sub_type",
    "secu_category",
    "listed_status",
    "trading_state",
    "source_type",
    "data_source",
    "market",
)


def _row(symbol: str, name: str, listed: str, state: str = "正常交易") -> tuple[str, ...]:
    return (
        symbol,
        symbol,
        name,
        "基金",
        "场内公募",
        "开放式基金",
        listed,
        state,
        "meta",
        "meta",
        "exchange",
    )


def test_universe_keeps_exact_broad_name_without_current_survivor_filter() -> None:
    rows = (
        _row(
            "510300.SH",
            "甲沪深300交易型开放式指数证券投资基金",
            "2012-05-28 00:00:00",
            "退市",
        ),
        _row(
            "510310.SH",
            "乙沪深300交易型开放式指数发起式证券投资基金",
            "2013-03-25 00:00:00",
        ),
    )

    selected = MODULE.select_broad_csi300_etfs(
        FIELDS,
        rows,
        listed_no_later_than=date(2018, 10, 9),
    )

    assert tuple(row["symbol"] for row in selected) == (
        "510300.SH",
        "510310.SH",
    )
    assert selected[0]["trading_state_at_query_time"] == "退市"


def test_universe_excludes_style_sector_enhanced_feeder_and_late_listing() -> None:
    rows = (
        _row(
            "512010.SH",
            "甲沪深300医药卫生交易型开放式指数证券投资基金",
            "2013-10-28 00:00:00",
        ),
        _row(
            "159238.SZ",
            "乙沪深300增强策略交易型开放式指数证券投资基金",
            "2018-01-01 00:00:00",
        ),
        _row(
            "160706.SZ",
            "丙沪深300交易型开放式指数证券投资基金联接基金(LOF)A类",
            "2012-08-21 00:00:00",
        ),
        _row(
            "159300.SZ",
            "丁沪深300交易型开放式指数证券投资基金",
            "2024-06-05 00:00:00",
        ),
    )

    assert MODULE.select_broad_csi300_etfs(
        FIELDS,
        rows,
        listed_no_later_than=date(2018, 10, 9),
    ) == ()
