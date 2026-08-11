from __future__ import annotations

from tools.build_replay_payload import (
    DEFAULT_CORPORATE_ACTIONS,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PRESCREENS,
)


def test_strict_replay_defaults_are_the_fixed_eight_csi300_etfs() -> None:
    symbols = tuple(
        path.stem.removeprefix("cached_symbol_prescreen_")
        for path in DEFAULT_PRESCREENS
    )
    assert symbols == (
        "159919",
        "159925",
        "510300",
        "510310",
        "510330",
        "510360",
        "510380",
        "510390",
    )


def test_strict_replay_defaults_use_expanded_actions_and_consolidated_cache() -> None:
    assert DEFAULT_CORPORATE_ACTIONS.name == "qmt_csi300_etf_corporate_actions.json"
    assert DEFAULT_MARKET_DATABASE.as_posix().endswith(
        "chanlun_csi300_broad_pool/financial_data_query_bars.sqlite3"
    )
