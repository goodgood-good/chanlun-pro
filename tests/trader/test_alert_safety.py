"""M2: _safe_alert 通道失败不抛、落地日志、不掩盖原异常。"""

import chanlun.trading.backtest_trader as mod
from chanlun.trading.backtest_trader import BackTestTrader


def _trader_with_log():
    tr = BackTestTrader("t", mode="trade", market="futures")
    logs = []
    tr.log = lambda m: logs.append(m)
    return tr, logs


def test_safe_alert_success_does_not_log_failure(monkeypatch):
    tr, logs = _trader_with_log()
    sent = []
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda c, t, m: sent.append((c, t, m)))
    tr._safe_alert("futures", "标题", "正文")
    assert len(sent) == 1
    # 正文被包成 list
    assert sent[0][2] == ["正文"]
    assert logs == []


def test_safe_alert_channel_failure_logged_not_raised(monkeypatch):
    """告警通道抛异常时 _safe_alert 不抛, 把失败落地日志。"""
    tr, logs = _trader_with_log()

    def _boom(*a, **k):
        raise RuntimeError("飞书挂了")

    monkeypatch.setattr(mod.utils, "send_fs_msg", _boom)
    # 不应抛出
    tr._safe_alert("futures", "标题", "正文")
    assert any("告警发送失败" in m for m in logs)


def test_safe_alert_original_exc_logged(monkeypatch):
    """传入原始异常 exc 时, 即使告警成功也落地原异常日志, 不被掩盖。"""
    tr, logs = _trader_with_log()
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *a, **k: None)
    err = ValueError("原始平仓异常")
    tr._safe_alert("futures", "平仓失败", "正文", exc=err)
    assert any("原始异常" in m and "原始平仓异常" in m for m in logs)


def test_safe_alert_list_msg_passthrough(monkeypatch):
    tr, logs = _trader_with_log()
    sent = []
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda c, t, m: sent.append(m))
    tr._safe_alert("futures", "标题", ["a", "b"])
    assert sent[0] == ["a", "b"]
