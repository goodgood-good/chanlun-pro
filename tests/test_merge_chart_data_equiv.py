"""P-005 _merge_chart_data 优化前后等价测试。"""
import sys
sys.path.insert(0, "src")
sys.path.insert(0, "web/chanlun_chart")

from cl_app.services.chart_compute import _merge_chart_data


def test_merge_basic_overlap():
    existing = {
        "t": [1, 2, 3], "c": [10, 20, 30], "o": [1, 2, 3],
        "h": [1, 2, 3], "l": [1, 2, 3], "v": [1, 2, 3],
        "fxs": [], "bis": [], "xds": [], "bi_zss": [], "xd_zss": [],
        "bcs": [], "mmds": [],
    }
    new = {
        "t": [3, 4, 5], "c": [33, 40, 50], "o": [3, 4, 5],
        "h": [3, 4, 5], "l": [3, 4, 5], "v": [3, 4, 5],
        "fxs": [], "bis": [], "xds": [], "bi_zss": [], "xd_zss": [],
        "bcs": [], "mmds": [],
    }
    merged = _merge_chart_data(existing, new)
    assert merged["t"] == [1, 2, 3, 4, 5]
    assert merged["c"] == [10, 20, 33, 40, 50]


def test_merge_none_does_not_override():
    existing = {"t": [1, 2], "c": [10, 20],
                "o": [], "h": [], "l": [], "v": [],
                "fxs": [], "bis": [], "xds": [], "bi_zss": [],
                "xd_zss": [], "bcs": [], "mmds": []}
    new = {"t": [2, 3], "c": [None, 30],
           "o": [], "h": [], "l": [], "v": [],
           "fxs": [], "bis": [], "xds": [], "bi_zss": [],
           "xd_zss": [], "bcs": [], "mmds": []}
    merged = _merge_chart_data(existing, new)
    assert merged["c"] == [10, 20, 30]


def test_merge_empty_sides():
    assert _merge_chart_data({}, {"t": [1]}) == {"t": [1]}
    assert _merge_chart_data({"t": [1]}, {}) == {"t": [1]}
