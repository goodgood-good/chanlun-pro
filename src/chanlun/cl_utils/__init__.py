"""chanlun.cl_utils 包 facade。

原单文件 cl_utils.py(图表/web/配置工具)按内聚功能拆成 5 个子模块:
- chart_config: 缠论/画图配置查询(带进程级 RLock cache 单例)
- macd_infos:   线/笔/中枢 MACD 力度信息
- tv_chart:     缠论数据 → TV 图表坐标(含周期映射/趋势通道/角度/中枢序列化)
- data:         WEB 端批量计算缠论数据
- indicators:   笔停顿/上下穿/缺口/平均K线等基础指标

本 __init__ 再导出全部公开函数,使 ``from chanlun.cl_utils import X`` 对原有
30+ 调用方保持完全不变(facade 规范写法)。
"""

from chanlun.cl_utils.chart_config import (
    query_cl_chart_config,
    set_cl_chart_config,
    del_cl_chart_config,
)
from chanlun.cl_utils.macd_infos import (
    cal_klines_macd_infos,
    cal_line_macd_infos,
    cal_macd_bis_is_bc,
    cal_zs_macd_infos,
)
from chanlun.cl_utils.tv_chart import (
    kcharts_frequency_h_l_map,
    cl_qstd,
    prices_jiaodu,
    zs_to_chart_dict,
    cl_data_to_tv_chart,
)
from chanlun.cl_utils.data import (
    web_batch_get_cl_datas,
)
from chanlun.cl_utils.strict_chart_runtime import (
    StrictChartRuntimeResult,
    build_strict_chart_cd,
)
from chanlun.cl_utils.indicators import (
    bi_td,
    up_cross,
    down_cross,
    last_done_bi,
    bi_qk_num,
    klines_to_heikin_ashi_klines,
)
from chanlun.cl_utils.strict_chart import (
    active_center_projection_to_chart_dict,
    aware_datetime_to_epoch_seconds,
    build_strict_structure_snapshot,
    center_observation_to_chart_dict,
    strict_center_to_chart_dict,
    strict_divergence_to_chart_dict,
    strict_point_to_chart_dict,
    strict_trend_to_chart_dict,
)


__all__ = [
    # chart_config
    "query_cl_chart_config",
    "set_cl_chart_config",
    "del_cl_chart_config",
    # macd_infos
    "cal_klines_macd_infos",
    "cal_line_macd_infos",
    "cal_macd_bis_is_bc",
    "cal_zs_macd_infos",
    # tv_chart
    "kcharts_frequency_h_l_map",
    "cl_qstd",
    "prices_jiaodu",
    "zs_to_chart_dict",
    "cl_data_to_tv_chart",
    # data
    "web_batch_get_cl_datas",
    # strict chart runtime
    "StrictChartRuntimeResult",
    "build_strict_chart_cd",
    # indicators
    "bi_td",
    "up_cross",
    "down_cross",
    "last_done_bi",
    "bi_qk_num",
    "klines_to_heikin_ashi_klines",
    # strict chart evidence
    "active_center_projection_to_chart_dict",
    "aware_datetime_to_epoch_seconds",
    "build_strict_structure_snapshot",
    "center_observation_to_chart_dict",
    "strict_center_to_chart_dict",
    "strict_divergence_to_chart_dict",
    "strict_point_to_chart_dict",
    "strict_trend_to_chart_dict",
]


if __name__ == "__main__":
    cl_config = query_cl_chart_config("a", "SH.000001")
    print(cl_config)
