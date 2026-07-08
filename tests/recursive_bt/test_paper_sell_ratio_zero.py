"""Round12 final-gate 观察项修: paper queue_events/fill_pending 的 `sell_ratio or 1.0` 把显式 0.0
(=不卖)吞成 1.0(满额卖), 使既有 `if ratio<=0: continue`(L386)/`if size<=0`(LOW-1 drop, L453)两个
本为跳过 0.0 卖而设的 guard 全失效 → queue/成交满额卖。与 MEDIUM-1 同根, 改保留显式 0.0(missing/None
才兜底 1.0)。当前 all_out 恒 1.0 dormant, 属一致性 + 让既有 guard 生效。"""

import types

from chanlun.recursive_bt.sim.paper import PaperBroker


def _held_broker(tmp_path):
    b = PaperBroker(str(tmp_path / "l.json"), market="a")
    b.positions = {
        "SZ.000001": {"shares": 100, "entry_px": 10.0, "entry_date": "2024-01-01 10:00:00", "bs": "3"}
    }
    b.pending = []
    return b


def test_queue_events_explicit_zero_sell_ratio_skipped(tmp_path):
    b = _held_broker(tmp_path)
    ev = types.SimpleNamespace(
        code="SZ.000001", side="sell", sell_ratio=0.0, bs_type="", reason="", identity="e1", signal_time=""
    )
    queued = b.queue_events([ev])
    # 修复前: 0.0 被 or 1.0 吞成 1.0 → queue 满额卖(queued=1); 修复后: 0.0 保留 → L386 guard 跳过
    assert queued == 0
    assert not any(o.get("act") == "sell" for o in b.pending)


def test_queue_events_missing_sell_ratio_defaults_full(tmp_path):
    # 回归: 缺失 sell_ratio(卖事件默认卖全部)仍兜底 1.0 → 正常 queue
    b = _held_broker(tmp_path)
    ev = types.SimpleNamespace(
        code="SZ.000001", side="sell", bs_type="", reason="", identity="e2", signal_time=""
    )
    queued = b.queue_events([ev])
    assert queued == 1
    sells = [o for o in b.pending if o.get("act") == "sell"]
    assert len(sells) == 1 and abs(sells[0]["sell_ratio"] - 1.0) < 1e-9