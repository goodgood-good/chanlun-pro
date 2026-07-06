"""collect_branch_signals 的 dingli2 事件接入(二期):独立 bs_type、白名单外零交易行为。"""
from chanlun.recursive_bt.engine.engine import BUYS, SELLS, collect_branch_signals


class _K:
    def __init__(self, idx):
        self.k_index = idx
        self.date = idx  # 可排序即可


class _FX:
    def __init__(self, idx, val):
        self.k = _K(idx)
        self.val = val


class _Pt:
    def __init__(self, bs_type, idx, val, level):
        self.bs_type = bs_type
        self.anchor_fx = _FX(idx, val)
        self.level = level
        self.divergence = None
        self.structural_stop_below = None
        self.structural_stop_above = None
        self.rebound_target = None


class _FakeCD:
    def get_branch_bspoints(self, use_xd=False):
        return [_Pt("3buy", 10, 1.0, 0)]

    def get_branch_dingli2_bspoints(self, use_xd=False):
        return [_Pt("3buy", 20, 2.0, 1), _Pt("3sell", 30, 3.0, 1)]


def test_dingli2_events_emitted_with_distinct_bs_type():
    sigs = collect_branch_signals(_FakeCD())
    types = [s.bs_type for s in sigs]
    assert "3buy_dingli2" in types and "3sell_dingli2" in types
    assert "3buy" in types                                  # 原生事件不受影响


def test_dingli2_not_in_default_whitelists():
    assert "3buy_dingli2" not in BUYS and "3sell_dingli2" not in SELLS


def test_collect_tolerates_cd_without_dingli2_getter():
    class _Old:
        def get_branch_bspoints(self, use_xd=False):
            return []
    assert collect_branch_signals(_Old()) == []