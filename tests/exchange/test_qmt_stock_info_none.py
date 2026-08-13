# -*- coding: utf-8 -*-
"""R11-#4: ExchangeQMT.stock_info() 对退市/未识别代码不返回声明的 None,直接 TypeError 崩默认可达端点。

stock_info() 类型标注 Union[Dict, None], 但实现 stock_detail["InstrumentName"] 从不判空;
vendored SDK xtdata.get_instrument_detail 对未识别/退市代码确定性返回 None → None["InstrumentName"]
抛 TypeError。两条默认 UI 路径无 try/except 兜底会 500: /a/bkgn_codes(bkgn.py:72 裸调 stock_info,
codes 来自本地 JSON 快照, 任一成分股退市即整板块列表崩)与 /ai/analyse(ai_analyse.py:70)。
同文件兄弟 all_stocks():150 已有 `if not stock_detail: continue` 防御, stock_info 漏做。
修复=stock_info 加 `if not stock_detail: return None`, 兑现类型标注。
"""
from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import ExchangeQMT


def test_stock_info_returns_none_for_unknown_code(monkeypatch):
    """退市/未识别代码: get_instrument_detail 返回 None → stock_info 返回 None 而非抛错。"""
    ex = ExchangeQMT()
    monkeypatch.setattr(exchange_qmt.xtdata, "get_instrument_detail", lambda *a, **k: None)
    assert ex.stock_info("SH.600000") is None


def test_stock_info_empty_dict_also_none(monkeypatch):
    """空 dict 亦视为未识别(与 all_stocks 的 `if not stock_detail` 同口径)。"""
    ex = ExchangeQMT()
    monkeypatch.setattr(exchange_qmt.xtdata, "get_instrument_detail", lambda *a, **k: {})
    assert ex.stock_info("SH.600000") is None


def test_stock_info_valid_code_returns_dict(monkeypatch):
    """正常代码: 仍返回含 code/name/precision 的 dict(回归保护)。"""
    ex = ExchangeQMT()
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "get_instrument_detail",
        lambda *a, **k: {"InstrumentName": "浦发银行", "PriceTick": 0.01},
    )
    info = ex.stock_info("SH.600000")
    assert info is not None
    assert info["code"] == "SH.600000"
    assert info["name"] == "浦发银行"


def test_market_data_readiness_probe_requires_real_instrument(monkeypatch):
    """就绪探针必须取得有效标的信息，不能把空 RPC 响应误报为可用。"""

    ex = ExchangeQMT()
    monkeypatch.setattr(ex, "stock_info", lambda _code: None)

    try:
        ex.market_data_readiness_probe()
    except RuntimeError as exc:
        assert "行情 RPC" in str(exc)
    else:  # pragma: no cover - 明确保护失败分支
        raise AssertionError("空响应必须使 QMT 行情探针失败")


def test_market_data_readiness_probe_returns_read_only_evidence(monkeypatch):
    ex = ExchangeQMT()
    monkeypatch.setattr(
        ex,
        "stock_info",
        lambda code: {"code": code, "name": "浦发银行", "precision": 2},
    )

    result = ex.market_data_readiness_probe()

    assert result == {
        "schema": "chanlun-qmt-market-data-readiness",
        "ready": True,
        "probe_code": "SH.600000",
        "provider": "QMT_XTDATA",
        "real_account_access": False,
        "real_order_transport": False,
    }
