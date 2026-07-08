"""Round12 LOW-1(同 portfolio HIGH-1 根): paper broker sub-lot 部分卖取整为0股 → 原 carry 永久
滞留、该标退出被卡。当前生产 all_out 恒 sell_ratio=1.0 不触发, 属防未来分层退出接入的一致性修复。"""

import types

import pandas as pd

from chanlun.recursive_bt.sim.paper import PaperBroker


def _fake_state(**kw):
    s = types.SimpleNamespace()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_paper_sublot_partial_sell_drops_not_carries(tmp_path):
    ledger = str(tmp_path / "ledger.json")
    b = PaperBroker(ledger, market="a")
    b.positions = {
        "SZ.000001": {"shares": 100, "entry_px": 10.0, "entry_date": "2024-01-01 10:00:00", "bs": "3"}
    }
    # 分层退出事件的部分卖单(ratio 0.5), 100 股 A 股 lot=100 → 50 股取整为 0
    b.pending = [{"code": "SZ.000001", "act": "sell", "sell_ratio": 0.5, "reason": "partial"}]
    states = {
        "SZ.000001": _fake_state(
            last_open=10.0, prev_close=10.0, last_px=10.0, last5=pd.Timestamp("2024-01-02 10:00")
        )
    }
    b.fill_pending(states, now="2024-01-02 10:00:00")
    # 修复前: sub-lot 卖单永久 carry(pending 恒 1); 修复后: drop(pending 空), 卡死卖单不再滞留
    assert len(b.pending) == 0
    assert b.positions["SZ.000001"]["shares"] == 100  # 未成交(sub-lot), 但卡死卖单已清
    assert len(b.trades) == 0