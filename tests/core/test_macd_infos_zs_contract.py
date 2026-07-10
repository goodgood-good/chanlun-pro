# -*- coding: utf-8 -*-
"""R2-F1-1 + R2-F1-2: cl_utils.macd_infos 对现核心 ZS 契约漂移 + MACD_INFOS 假 dataclass。

F1-1: cal_zs_macd_infos 访问 zs.start.k/zs.end.k,但核心重做后 ZS.start/end 语义是
LINE(BI/XD,end 可 None),LINE 无 .k → 任意真实 ZS 输入 100% AttributeError。
消费者 trading/base.py:1000(实盘 Strategy)+ xuangu.py:207/369(选股,异常被任务层
逐 code 吞成 None → 选股静默 0 选出)。修复=优先 zs.lines 取 K 线范围(与 golden
_sig_zss 同口径),空 lines 回退 FX 式 start/end。

F1-2: MACD_INFOS 标注 @dataclass 但 8 字段全无类型注解 → fields()==0,
to_dict()/asdict() 恒 {},任意两实例 __eq__ 恒 True。修复=补注解。
"""
from dataclasses import fields

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.types import MACD_INFOS
from chanlun.cl_utils.macd_infos import cal_zs_macd_infos

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}


@pytest.fixture(scope="module")
def cd_600519():
    df = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(1500).reset_index(drop=True)
    cd = CL("SH.600519", "5m", dict(_CFG))
    cd.process_klines(df)
    return cd


def test_cal_zs_macd_infos_real_zs_no_crash(cd_600519):
    """真实 bi_zss 全量喂入: 旧码 13/13 AttributeError('BI' object has no attribute 'k')。"""
    zss = cd_600519.get_bi_zss()
    assert len(zss) >= 3, "fixture 前 1500 根应产生若干笔中枢"
    for zs in zss:
        infos = cal_zs_macd_infos(zs, cd_600519)   # 旧码此行 AttributeError
        assert isinstance(infos, MACD_INFOS)


def test_cal_zs_macd_infos_matches_lines_range(cd_600519):
    """返回值与按 zs.lines K 线范围独立计算的切片一致(非空转:last_dif 来自真实 dif)。"""
    zs = cd_600519.get_bi_zss()[-1]
    infos = cal_zs_macd_infos(zs, cd_600519)
    s = zs.lines[0].start.k.k_index
    e = zs.lines[-1].end.k.k_index
    dif = cd_600519.get_idx()["macd"]["dif"][s : e + 1]
    assert e > s, "中枢至少跨多根K线"
    assert infos.last_dif == dif[-1]


def test_macd_infos_is_real_dataclass():
    """旧码: fields=0 / to_dict={} / 值不同却相等。"""
    assert len(fields(MACD_INFOS)) == 8
    d = MACD_INFOS().to_dict()
    assert len(d) == 8 and d["dif_up_cross_num"] == 0
    a = MACD_INFOS()
    a.dif_up_cross_num = 99
    assert a != MACD_INFOS(), "值不同的两实例不得相等"