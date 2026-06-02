"""P7-T5: query_cl_chart_config 默认含 chart_show_higher_zs(混合多级别叠加开关)。"""
from chanlun.cl_utils import query_cl_chart_config


def test_default_config_has_higher_zs():
    cfg = query_cl_chart_config("a", "SH.000001")
    assert cfg.get("chart_show_higher_zs") == "1"
