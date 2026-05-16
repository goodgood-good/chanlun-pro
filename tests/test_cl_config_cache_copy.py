"""_cl_config_cache 命中路径拷贝隔离测试 (P-007)。"""
from chanlun.cl_utils import (
    _cl_config_cache_get,
    _cl_config_cache_set,
    _cl_config_cache_invalidate,
)


def test_cache_hit_returns_isolated_copy():
    """命中返回的 dict 被修改不污染缓存里的原件。"""
    _cl_config_cache_invalidate()
    cfg = {"a": 1, "zs_bi_type": ["zs_type_bz"], "nested": {"x": 9}}
    _cl_config_cache_set("k1", cfg)

    got1 = _cl_config_cache_get("k1")
    got1["a"] = 999
    got1["zs_bi_type"].append("polluted")
    got1["nested"]["x"] = -1

    got2 = _cl_config_cache_get("k1")
    assert got2["a"] == 1
    assert got2["zs_bi_type"] == ["zs_type_bz"]
    assert got2["nested"]["x"] == 9


def test_cache_set_snapshots_input():
    """写入后修改原始 dict 不污染缓存。"""
    _cl_config_cache_invalidate()
    cfg = {"a": 1, "lst": [1, 2]}
    _cl_config_cache_set("k2", cfg)
    cfg["a"] = 888
    cfg["lst"].append(3)
    got = _cl_config_cache_get("k2")
    assert got["a"] == 1
    assert got["lst"] == [1, 2]
