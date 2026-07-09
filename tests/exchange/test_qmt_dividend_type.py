# -*- coding: utf-8 -*-
"""C3: reboot_sync_a_klines.py 传 args={"fq":"hfq"} 想要后复权, 但 exchange_qmt.klines
只消费 args["dividend_type"](缺省 front), "fq" 键零消费 → 实际拉前复权数据增量追加,
除权后库内新旧段复权基准永久错位。修复: 脚本改传 {"dividend_type":"back"}。

本测试钉死 exchange_qmt.klines 的消费契约(脚本修复所依赖): dividend_type 从 args 透传到
xtdata.get_market_data; 且旧 {"fq":"hfq"} 键确实不被消费(退化为 front)——正是 bug 根源。
"""
from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import ExchangeQMT


def _mock_xt(monkeypatch, captured):
    class _FakeXt:
        @staticmethod
        def download_history_data(**k):
            return None

        @staticmethod
        def get_market_data(**k):
            captured["dividend_type"] = k.get("dividend_type")
            return {}  # 空 → klines 提前 return empty_df(不 retry)

    monkeypatch.setattr(exchange_qmt, "xtdata", _FakeXt)


def test_klines_forwards_dividend_type_back(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    ex.klines("SH.600519", "d", start_date="2020-01-01",
              args={"dividend_type": "back"})
    assert captured["dividend_type"] == "back"


def test_klines_default_is_front(monkeypatch):
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    ex.klines("SH.600519", "d", start_date="2020-01-01")
    assert captured["dividend_type"] == "front"


def test_legacy_fq_key_not_consumed_is_front(monkeypatch):
    """旧脚本 {"fq":"hfq"} 不被消费 → 仍是 front(C3 bug 根源的运行时铁证)。"""
    ex = ExchangeQMT()
    captured = {}
    _mock_xt(monkeypatch, captured)
    ex.klines("SH.600519", "d", start_date="2020-01-01", args={"fq": "hfq"})
    assert captured["dividend_type"] == "front"