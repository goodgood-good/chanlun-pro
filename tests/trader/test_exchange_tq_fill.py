"""H4: 天勤 order() 返回实际成交量与加权均价 (非原始请求量)。

exchange_tq 依赖 tqsdk (本环境未装) 无法 import, 故这里以**与补丁完全一致的算法**
离线复现 order() 循环里的成交累计逻辑, 验证: 累计成交量 = sum(volume_orign-volume_left),
加权均价 = 成交额累计/成交量累计。补丁源码本体由 py_compile 守护。

(对应 src/chanlun/exchange/exchange_tq.py order() H4 段)
"""


class FakeTqOrder:
    def __init__(self, volume_orign, volume_left, trade_price, status="FINISHED", is_error=False, last_msg="", order_id="o1"):
        self.volume_orign = volume_orign
        self.volume_left = volume_left
        self.trade_price = trade_price
        self.status = status
        self.is_error = is_error
        self.last_msg = last_msg
        self.order_id = order_id


def _accumulate(order_seq, request_amount, fallback_price=100.0):
    """复现补丁中 order() 的成交累计循环 (单轮喂一个 order, 简化 wait/撤单 IO)。"""
    order = None
    filled_total = 0.0
    turnover_total = 0.0
    last_order_id = None
    idx = 0
    amount_left = request_amount

    while amount_left > 0 and idx < len(order_seq):
        order = order_seq[idx]
        idx += 1
        price = fallback_price
        last_order_id = order.order_id

        this_filled = max(0, int(order.volume_orign) - int(order.volume_left))
        if this_filled > 0:
            filled_total += this_filled
            fill_price = order.trade_price if order.trade_price else price
            turnover_total += this_filled * fill_price

        if order.status == "FINISHED":
            if order.is_error:
                if filled_total <= 0:
                    return False
                break
            if int(order.volume_left) > 0:
                amount_left = int(order.volume_left)
                continue
            break
        else:
            # 超时未完全成交 (复现里直接用 volume_left 续单)
            if order.is_error:
                if filled_total <= 0:
                    return False
                break
            amount_left = int(order.volume_left)

    if order is None or filled_total <= 0:
        return False
    avg_price = turnover_total / filled_total if filled_total > 0 else order.trade_price
    return {"id": last_order_id, "price": avg_price, "amount": filled_total}


def test_full_fill_single_order():
    """一次全成 2 手, trade_price=101 → amount=2, price=101。"""
    res = _accumulate([FakeTqOrder(2, 0, 101.0)], 2)
    assert res["amount"] == 2
    assert res["price"] == 101.0


def test_partial_then_complete_accumulates_volume():
    """第一轮成交 1 手剩 1 (FINISHED 但有剩余 → 续单), 第二轮成交剩余 1 手。"""
    seq = [
        FakeTqOrder(2, 1, 100.0, status="FINISHED"),  # 委托2成1剩1
        FakeTqOrder(1, 0, 102.0, status="FINISHED"),  # 委托1成1
    ]
    res = _accumulate(seq, 2)
    assert res["amount"] == 2
    # 加权均价 = (1*100 + 1*102)/2 = 101
    assert res["price"] == 101.0


def test_weighted_avg_price():
    """不同价多轮成交的加权均价正确。"""
    seq = [
        FakeTqOrder(3, 1, 100.0, status="FINISHED"),  # 成2手@100
        FakeTqOrder(1, 0, 110.0, status="FINISHED"),  # 成1手@110
    ]
    res = _accumulate(seq, 3)
    assert res["amount"] == 3
    # (2*100 + 1*110)/3 = 310/3
    assert abs(res["price"] - 310 / 3) < 1e-9


def test_error_with_no_fill_returns_false():
    """报错且零成交 → False。"""
    res = _accumulate([FakeTqOrder(2, 2, 0.0, status="FINISHED", is_error=True)], 2)
    assert res is False


def test_error_with_partial_fill_returns_filled_not_false():
    """已部分成交后报错: 不丢已成交, 按实际成交返回 (非 False)。"""
    seq = [FakeTqOrder(2, 1, 100.0, status="FINISHED", is_error=True)]
    res = _accumulate(seq, 2)
    assert res is not False
    assert res["amount"] == 1
    assert res["price"] == 100.0


def test_zero_fill_returns_false():
    """完全未成交 → False (比原来更严, 不再返回原始请求量)。"""
    seq = [FakeTqOrder(2, 2, 0.0, status="FINISHED")]
    res = _accumulate(seq, 2)
    assert res is False
