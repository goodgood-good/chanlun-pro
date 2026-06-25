"""Task2: compute_signature 变化检测指纹。"""
from cl_app.services.sse_signature import compute_signature


def test_same_data_same_sig():
    a = {"t": [1, 2, 3], "bis": [{"points": [{"time": 1}, {"time": 2}]}]}
    assert compute_signature(a) == compute_signature(dict(a))


def test_new_bar_changes_sig():
    a = {"t": [1, 2, 3], "bis": []}
    b = {"t": [1, 2, 3, 4], "bis": []}
    assert compute_signature(a) != compute_signature(b)


def test_bi_endpoint_change_changes_sig():
    a = {"t": [1, 2], "bis": [{"points": [{"time": 1}, {"time": 2}]}]}
    b = {"t": [1, 2], "bis": [{"points": [{"time": 1}, {"time": 3}]}]}
    assert compute_signature(a) != compute_signature(b)


def test_new_mmd_changes_sig():
    a = {"t": [1, 2], "mmds": []}
    b = {"t": [1, 2], "mmds": [{"points": {"time": 2}}]}
    assert compute_signature(a) != compute_signature(b)


def test_empty_safe():
    assert isinstance(compute_signature({}), str)
    assert isinstance(compute_signature(None), str)


def test_last_close_change_changes_sig():
    # M2: 末根 close 变动(K线根数/形态计数都没变)也必须改变指纹,否则盘中"实时"在形态
    # 停滞期(末根上下抖动但无新分型/笔)会漏推,且与 prepend"末根OHLC全等才跳过"判据不一致。
    a = {"t": [1, 2, 3], "c": [10.0, 11.0, 12.0], "bis": []}
    b = {"t": [1, 2, 3], "c": [10.0, 11.0, 12.5], "bis": []}
    assert compute_signature(a) != compute_signature(b)


def test_last_ohlc_change_changes_sig():
    # M2: 末根 high/low/open 任一变动都应改变指纹。
    a = {"t": [1, 2], "o": [1, 2], "h": [3, 4], "l": [0, 1], "c": [2, 3]}
    b = {"t": [1, 2], "o": [1, 2], "h": [3, 5], "l": [0, 1], "c": [2, 3]}
    assert compute_signature(a) != compute_signature(b)


def test_no_ohlc_keys_still_stable():
    # 缺 o/h/l/c 键时(老数据/异常)仍稳定可比,不抛错。
    a = {"t": [1, 2, 3], "bis": []}
    assert compute_signature(a) == compute_signature(dict(a))
