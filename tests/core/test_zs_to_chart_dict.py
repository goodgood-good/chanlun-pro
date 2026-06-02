"""P7-T1: zs_to_chart_dict 模块级中枢序列化(core/web 共享,多周期叠加复用)。"""
from chanlun.cl_utils import zs_to_chart_dict


def test_zs_to_chart_dict_structure(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(800, multi_freq=True)
    zss = cd.get_bi_zss("zs_type_bz")
    assert zss, "合成数据应有笔中枢"
    d = zs_to_chart_dict(zss[0])
    assert isinstance(d["points"], list) and len(d["points"]) == 2
    assert all("time" in p and "price" in p for p in d["points"])
    assert d["linestyle"] in ("0", "1")
    assert "type" in d and "is_expanded" in d and "sub_count" in d


def test_zs_to_chart_dict_envelope_wider(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(800, multi_freq=True)
    zss = cd.get_bi_zss("zs_type_bz")
    z = zss[0]
    core = zs_to_chart_dict(z, use_envelope=False)
    env = zs_to_chart_dict(z, use_envelope=True)
    # 包络 [DD,GG] 不窄于核心 [ZD,ZG]:高点 >=、低点 <=
    assert env["points"][0]["price"] >= core["points"][0]["price"]
    assert env["points"][1]["price"] <= core["points"][1]["price"]
