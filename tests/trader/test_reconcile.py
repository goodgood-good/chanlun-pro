"""M1: query_broker_position 三态 + reconcile_positions 仅告警不自动改仓。"""

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION


def _trader():
    tr = BackTestTrader("t", mode="trade", market="futures")
    tr.log = lambda *a, **k: None  # 静默日志
    return tr


def test_query_broker_position_ok_returns_data():
    tr = _trader()

    class _Ex:
        def positions(self, code):
            return {code: object()}

    tr.ex = _Ex()
    status, data = tr.query_broker_position("rb2405")
    assert status == "ok"
    assert data is not None


def test_query_broker_position_ok_empty_is_confirmed_flat():
    tr = _trader()

    class _Ex:
        def positions(self, code):
            return {}  # 确认无仓

    tr.ex = _Ex()
    status, data = tr.query_broker_position("rb2405")
    assert status == "ok"
    assert data == {}


def test_query_broker_position_exception_returns_fail():
    """查询抛异常 → ("fail", None), 调用方据此不清本地仓。"""
    tr = _trader()

    class _Ex:
        def positions(self, code):
            raise RuntimeError("网络超时")

    tr.ex = _Ex()
    status, data = tr.query_broker_position("rb2405")
    assert status == "fail"
    assert data is None


def test_reconcile_broker_has_local_none_alerts(monkeypatch):
    """券商有仓本地无 → 告警, 不自动改本地仓。"""
    tr = _trader()
    alerts = []
    monkeypatch.setattr(tr, "_safe_alert", lambda *a, **k: alerts.append(a))

    class _Ex:
        def positions(self, code):
            return {code: object()}  # 券商有仓

    tr.ex = _Ex()
    tr.positions = {}  # 本地无
    tr.reconcile_positions(["rb2405"])
    assert len(alerts) == 1
    assert tr.positions == {}  # 未被自动改


def test_reconcile_local_has_broker_none_alerts(monkeypatch):
    """本地有仓券商无 → 告警, 不自动平本地仓。"""
    tr = _trader()
    alerts = []
    monkeypatch.setattr(tr, "_safe_alert", lambda *a, **k: alerts.append(a))

    class _Ex:
        def positions(self, code):
            return {}  # 券商无仓

    tr.ex = _Ex()
    local = POSITION(code="rb2405", mmd="1buy", amount=1)
    tr.positions = {"rb2405:1buy": local}
    tr.reconcile_positions(["rb2405"])
    assert len(alerts) == 1
    # 本地仓保留 (保守: 不自动平)
    assert tr.positions["rb2405:1buy"].amount == 1


def test_reconcile_query_fail_skips_no_change(monkeypatch):
    """查询失败 → 跳过该 code, 绝不据失败改本地。"""
    tr = _trader()
    alerts = []
    monkeypatch.setattr(tr, "_safe_alert", lambda *a, **k: alerts.append(a))

    class _Ex:
        def positions(self, code):
            raise RuntimeError("超时")

    tr.ex = _Ex()
    local = POSITION(code="rb2405", mmd="1buy", amount=1)
    tr.positions = {"rb2405:1buy": local}
    tr.reconcile_positions(["rb2405"])
    assert alerts == []  # 查询失败不告警差异
    assert tr.positions["rb2405:1buy"].amount == 1


def test_reconcile_match_no_alert(monkeypatch):
    """两边都有仓 → 无差异告警。"""
    tr = _trader()
    alerts = []
    monkeypatch.setattr(tr, "_safe_alert", lambda *a, **k: alerts.append(a))

    class _Ex:
        def positions(self, code):
            return {code: object()}

    tr.ex = _Ex()
    tr.positions = {"rb2405:1buy": POSITION(code="rb2405", mmd="1buy", amount=1)}
    tr.reconcile_positions(["rb2405"])
    assert alerts == []
