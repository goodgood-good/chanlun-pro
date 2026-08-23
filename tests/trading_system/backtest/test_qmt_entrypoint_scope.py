from __future__ import annotations

import pytest

from tools import backtest_qmt_fixed_year


def test_qmt_backtest_rejects_an_implicit_full_market_run() -> None:
    with pytest.raises(ValueError, match="bounded research scope required"):
        backtest_qmt_fixed_year.main([])


def test_qmt_backtest_rejects_conflicting_full_market_scope() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        backtest_qmt_fixed_year.main(["--full-market", "--limit", "48"])
