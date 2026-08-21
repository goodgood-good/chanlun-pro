# -*- coding: utf-8 -*-
"""QMT K 线只接受当前 ``dividend_type`` 契约。"""
from datetime import datetime, timedelta

import pytest
from tenacity import RetryError

from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import ExchangeQMT


def _mock_xt(monkeypatch, captured):
    class _FakeXt:
        @staticmethod
        def download_history_data(**k):
            captured["download_start_time"] = k.get("start_time")
            return None

        @staticmethod
        def get_market_data(**k):
            captured["dividend_type"] = k.get("dividend_type")
            captured["read_start_time"] = k.get("start_time")
            return {}  # 空 → klines 提前 return empty_df(不 retry)

    monkeypatch.setattr(exchange_qmt, "xtdata", _FakeXt)


def test_klines_forwards_dividend_type_back(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    ex.klines("SH.600519", "d", start_date="2020-01-01",
              args={"dividend_type": "back"})
    assert captured["dividend_type"] == "back"


def test_klines_default_is_front_ratio(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    ex.klines("SH.600519", "d", start_date="2020-01-01")
    assert captured["dividend_type"] == "front_ratio"


def test_unsupported_fq_key_is_rejected(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    with pytest.raises(RetryError):
        ex.klines(
            "SH.600519",
            "d",
            start_date="2020-01-01",
            args={"fq": "hfq"},
        )
    assert "dividend_type" not in captured


def test_realtime_incremental_download_keeps_full_local_read_window(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)

    ex.klines(
        "SH.600519",
        "5m",
        start_date="2020-01-01",
        args={"incremental_refresh_days": 14},
    )

    expected_starts = {
        (datetime.now() - timedelta(days=14) + timedelta(days=offset)).strftime(
            "%Y%m%d"
        )
        for offset in (-1, 0, 1)
    }
    assert captured["download_start_time"] in expected_starts
    assert captured["read_start_time"] == "20200101"
