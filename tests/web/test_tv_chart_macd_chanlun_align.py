"""R7-C1: kline_type=kline_chanlun 时 native MACD 与展示K线错位。get_klines() 返回合并缠论K线
(M根, 经包含处理 M<N), 而 else 分支 macd=cd.get_idx()['macd'] 恒 src 长度(N) → chart_data 里
t(M)与 macd_*(N)不等长, 前端按下标配对 macd[i]↔bar[i] 使 MACD 画在错误 bar 下、尾部错切。
修复: 展示K线与 src 不等长时在展示K线上重算 MACD 对齐(默认模式 klines==src 沿用 get_idx 不变)。
用强包含合成数据(cl_klines 显著少于 src)才能触发错位。"""

import numpy as np
import pandas as pd

from chanlun.core.cl import CL
from chanlun.cl_utils import cl_data_to_tv_chart


def _inclusive_klines(groups=40):
    """每组先放大再逐步收窄制造大量包含关系 → get_cl_klines() 显著少于 src。"""
    rows = []
    t = 1_600_000_000
    for g in range(groups):
        base = 50 + (g % 6) * 8
        for lo, hi in [
            (base - 3, base + 22), (base - 1, base + 20), (base + 2, base + 17),
            (base + 5, base + 14), (base + 7, base + 11),
            (base, base + 19), (base + 3, base + 16),
        ]:
            rows.append({
                "date": pd.Timestamp(t, unit="s", tz="UTC"),
                "open": (lo + hi) / 2.0, "high": float(hi), "low": float(lo),
                "close": (lo + hi) / 2.0, "volume": 100.0,
            })
            t += 60
    return pd.DataFrame(rows)


def _base_cfg():
    from chanlun.cl_utils import query_cl_chart_config
    return dict(query_cl_chart_config("a", "SYNC1"))


def test_kline_chanlun_macd_aligned_to_display_klines():
    cfg = _base_cfg()
    cfg["kline_type"] = "kline_chanlun"
    cd = CL("SYNCHAN", "1m", cfg, market="a")
    cd.process_klines(_inclusive_klines())
    # 前置: 强包含数据确使 get_klines(M) < get_src_klines(N), 否则测试无意义
    assert len(cd.get_klines()) < len(cd.get_src_klines())
    data = cl_data_to_tv_chart(cd, cfg)
    assert len(data["t"]) == len(cd.get_klines())
    # 核心: MACD 各序列与展示K线 t 等长(修复前 = src 长度 N != M → 错位)
    assert len(data["macd_dif"]) == len(data["t"])
    assert len(data["macd_dea"]) == len(data["t"])
    assert len(data["macd_hist"]) == len(data["t"])
    assert len(data["macd_area"]) == len(data["t"])


def test_kline_default_macd_unchanged_byte_identical():
    # 防呆: kline_default(klines==src)仍走 cd.get_idx(), MACD 与 get_idx 逐字节一致
    cfg = _base_cfg()
    cfg["kline_type"] = "kline_default"
    cd = CL("SYNDEF", "1m", cfg, market="a")
    cd.process_klines(_inclusive_klines())
    assert len(cd.get_klines()) == len(cd.get_src_klines())
    data = cl_data_to_tv_chart(cd, cfg)
    assert len(data["macd_dif"]) == len(data["t"])
    expect = np.round(cd.get_idx()["macd"]["dif"], 6).tolist()
    assert data["macd_dif"] == expect
