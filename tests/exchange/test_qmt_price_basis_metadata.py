from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tenacity import RetryError

from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import ExchangeQMT


def _market_data() -> dict[str, pd.DataFrame]:
    values = {
        "time": np.array([1736402400000, 1736402460000], dtype=np.int64),
        "open": np.array([10.0, 10.1]),
        "high": np.array([10.2, 10.3]),
        "low": np.array([9.9, 10.0]),
        "close": np.array([10.1, 10.2]),
        "volume": np.array([100.0, 120.0]),
    }
    return {name: pd.DataFrame([column]) for name, column in values.items()}


def _factor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "time": 1736438400000.0,
                "interest": 0.02,
                "stockBonus": 0.0,
                "stockGift": 0.0,
                "allotNum": 0.0,
                "allotPrice": 0.0,
                "gugai": 0.0,
                "dr": 1.02,
            }
        ]
    )


class _FakeXt:
    factor_calls = 0
    factor_error: Exception | None = None
    enable_hello = True

    @staticmethod
    def download_history_data(**_kwargs):
        return None

    @staticmethod
    def get_market_data(**_kwargs):
        return _market_data()

    @classmethod
    def get_divid_factors(cls, _code):
        cls.factor_calls += 1
        if cls.factor_error is not None:
            raise cls.factor_error
        return _factor_frame()


def test_qmt_front_ratio_adjusted_frame_has_formal_price_metadata(monkeypatch) -> None:
    _FakeXt.factor_calls = 0
    _FakeXt.factor_error = None
    monkeypatch.setattr(exchange_qmt, "xtdata", _FakeXt)

    frame = ExchangeQMT().klines("SH.600926", "1m", start_date="2025-01-09")

    assert len(frame) == 2
    assert frame.attrs["structure_price_quantum"] == "0.01"
    assert frame.attrs["price_basis_revision"].startswith("sha256:")
    assert frame.attrs["price_basis_provider"] == "qmt"
    assert frame.attrs["price_basis_adjustment"] == "front_ratio"
    assert _FakeXt.factor_calls == 1


def test_qmt_none_adjustment_does_not_read_factor_api(monkeypatch) -> None:
    _FakeXt.factor_calls = 0
    _FakeXt.factor_error = AssertionError("factor API must not run")
    monkeypatch.setattr(exchange_qmt, "xtdata", _FakeXt)

    frame = ExchangeQMT().klines(
        "SH.510300",
        "1m",
        start_date="2025-01-09",
        args={"dividend_type": "none"},
    )

    assert frame.attrs["structure_price_quantum"] == "0.001"
    assert frame.attrs["price_basis_adjustment"] == "none"
    assert _FakeXt.factor_calls == 0


def test_qmt_factor_failure_rejects_unattested_adjusted_bars(
    monkeypatch,
) -> None:
    _FakeXt.factor_calls = 0
    _FakeXt.factor_error = RuntimeError("native factor service unavailable")
    monkeypatch.setattr(exchange_qmt, "xtdata", _FakeXt)

    with pytest.raises(RetryError):
        ExchangeQMT().klines("SH.600926", "1m", start_date="2025-01-09")

    assert _FakeXt.factor_calls == 3
