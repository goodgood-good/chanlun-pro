from typing import Dict, List

import pandas as pd

from chanlun.core.types import ICL


def web_batch_get_cl_datas(
    market: str, code: str, klines: Dict[str, pd.DataFrame], cl_config: dict = None
) -> List[ICL]:
    """
    WEB 端批量计算并获取缠论数据。

    不走 fdb.get_web_cl_data 的 pkl 缓存：pkl 增量路径在数据源切换 / 长期运行时
    会导致旧版 XD 残留累积（xds 异常膨胀），因此 web 路径统一走进程内 LRU 缓存
    (cl_object_cache)，cache miss 时每次全量 process_klines。
    fdb.get_web_cl_data 仍保留给 notebook/回测脚本（其末尾追加增量假设不同）。

    :param market: 市场
    :param code: 计算的标的
    :param klines: 每个周期对应一个 K 线 DataFrame
    :param cl_config: 缠论配置
    :return: 缠论数据对象列表，顺序与 klines.keys 一致
    """
    # 局部 import: cl_object_cache 在 web 层, 而 cl_utils 在 src 层 — 这里用局部
    # import 避免 src 层硬依赖 web 层 (调用栈本身就来自 web, import 不会循环)
    try:
        from cl_app.services.cl_object_cache import get_or_compute_cl
        _cache_available = True
    except ImportError:
        _cache_available = False

    if _cache_available:
        cls = []
        for f, k in klines.items():
            cd = get_or_compute_cl(market, code, f, cl_config, k)
            cls.append(cd)
        return cls

    # 兜底: 非 web 环境调用 (notebook / cli) 直接全量, 不接 cache
    from chanlun.core.cl import CL
    cls = []
    for f, k in klines.items():
        cd = CL(code, f, dict(cl_config) if cl_config else {}, market=market)
        cd.process_klines(k)
        cls.append(cd)
    return cls
