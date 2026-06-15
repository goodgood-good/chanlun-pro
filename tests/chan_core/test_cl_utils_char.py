# -*- coding: utf-8 -*-
"""cl_utils 特征化(characterization)测试。

cl_utils 是图表/web/配置工具,**不在 CL 计算路径**上,黄金主测不到它。
本测试在真实 fixture 上钉死一组**纯函数**的输出,作为拆分 cl_utils.py→cl_utils/ 包
(及未来任何 cl_utils 改动)的回归网。避开 DB 依赖函数(query_cl_chart_config 等)。
"""
import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import canonical_json  # noqa: E402
from chanlun.core.cl import CL  # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG  # noqa: E402
from chanlun import cl_utils as U  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "SH.600519_d.parquet"
GOLD = Path(__file__).resolve().parents[1] / "golden" / "cl_utils_char.json"


def _jsonable(o):
    """递归把非 JSON 安全类型转成可序列化:Timestamp/datetime→isoformat、np 标量→python。"""
    if isinstance(o, (pd.Timestamp, datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return [_jsonable(x) for x in o.tolist()]
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(x) for x in o]
    return o


def _line_id(line) -> dict:
    return {
        "index": line.index, "type": line.type, "high": line.high, "low": line.low,
        "start": line.start.k.date.isoformat(), "end": line.end.k.date.isoformat(),
    }


def build_char() -> dict:
    df = pd.read_parquet(FIX)
    cd = CL("SH.600519", "d", dict(CL_CFG))
    cd.process_klines(df)
    bis = cd.get_bis()
    ks = cd.get_klines()
    ha = U.klines_to_heikin_ashi_klines(df)
    return {
        "cal_line_macd_infos": U.cal_line_macd_infos(bis[0], cd).to_dict(),
        "cal_klines_macd_infos": U.cal_klines_macd_infos(
            ks[bis[0].start.k.k_index], ks[bis[0].end.k.k_index], cd
        ).to_dict(),
        "cal_macd_bis_is_bc": list(U.cal_macd_bis_is_bc(bis[:6], cd)),
        "cl_qstd": U.cl_qstd(cd, "bi", 5),
        "prices_jiaodu": U.prices_jiaodu([1.0, 2.0, 1.5, 3.0]),
        "up_cross": U.up_cross(np.array([1, 2, 3, 2, 4.0]), np.array([2, 2, 2, 2, 2.0])),
        "down_cross": U.down_cross(np.array([3, 2, 1, 2, 0.0]), np.array([2, 2, 2, 2, 2.0])),
        "bi_td": U.bi_td(bis[-1], cd),
        "last_done_bi": _line_id(U.last_done_bi(cd)),
        "bi_qk_num": list(U.bi_qk_num(cd, bis[-1])),
        "ha_shape": list(ha.shape),
        "ha_tail3": [
            {k: r[k] for k in ("open", "high", "low", "close")}
            for r in ha.tail(3).to_dict("records")
        ],
    }


def test_cl_utils_characterization():
    got = canonical_json(_jsonable(build_char()))
    assert got == GOLD.read_text(encoding="utf-8"), "cl_utils 纯函数输出相对 golden 漂移"
