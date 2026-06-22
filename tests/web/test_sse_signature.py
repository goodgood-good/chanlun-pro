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
