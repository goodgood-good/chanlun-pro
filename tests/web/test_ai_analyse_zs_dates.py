# -*- coding: utf-8 -*-
"""R13-#5: AIAnalyse.prompt() line192 用旧式 ZS.start.k/end.k 访问真实 ZS(核心重做后 start/end 是
LINE 无 .k)→ 任意有中枢的标的 100% AttributeError,AI分析功能(默认 /ai/analyse 端点,@login_required
即满足)对该标的整体失效(analyse() try/except 吞成误导性报错,且先于 API Key 校验)。与已修的
macd_infos F1-1(_zs_k_index_range)同根,ai_analyse 是漏改兄弟实例。修复=新增 _zs_dates(zs)(优先
zs.lines 取端点 FX 日期,空 lines 回退 FX 式 start/end),line192 改用它。
"""
from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun import fun
from chanlun.tools.ai_analyse import _zs_dates

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}


@pytest.fixture(scope="module")
def cd_600519():
    df = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")[
        ["date", "open", "high", "low", "close", "volume"]
    ].head(1500).reset_index(drop=True)
    cd = CL("SH.600519", "5m", dict(_CFG))
    cd.process_klines(df)
    return cd


def test_zs_dates_real_zs_no_crash(cd_600519):
    """真实 bi_zss 全量: 旧式 zs.start.k 100% AttributeError; _zs_dates 返回合法端点日期。"""
    zss = cd_600519.get_bi_zss()
    assert len(zss) >= 3, "fixture 前 1500 根应产生若干笔中枢"
    for zs in zss:
        with pytest.raises(AttributeError):
            _ = zs.start.k  # 根因存证: ZS.start 现为 LINE 无 .k
        sd, ed = _zs_dates(zs)
        assert sd is not None and ed is not None
        assert fun.datetime_to_str(sd) and fun.datetime_to_str(ed)


def test_prompt_no_longer_uses_zs_start_k():
    """锁死 wiring: line192 不再裸 zs.start.k.date/zs.end.k.date, 改用 _zs_dates。"""
    src = Path("src/chanlun/tools/ai_analyse.py").read_text(encoding="utf-8")
    assert "zs.start.k.date" not in src
    assert "zs.end.k.date" not in src
    assert "_zs_dates(" in src