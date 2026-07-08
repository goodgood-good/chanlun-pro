"""legacy BsPointCalculator (process_mmd 写 line.zs_type_mmds) 的 inc==batch 守护网。

补 core R1 盲区:golden 与 test_cl_incremental_equivalence 的 bsp 签名只取新核心
get_branch_bspoints,从不校验 legacy process_mmd 写的 zs_type_mmds(bi/xd 层买卖点)——
而后者被回测/策略/选股消费(bs_point_calculator.py 的 _bsp_restart_point / _restore_bsp_state
增量复原机制此前零 inc==batch 守护)。逐前缀对拍:同一 CL 增量摄入 == 每前缀新建全量。
"""
import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}
_FREQ = "1m"


def _gen_klines(n, seed):
    rng = np.random.RandomState(seed)
    t0 = pd.Timestamp("2024-01-01 09:30:00")
    rows = []
    price = 100.0
    ph, pl = price + 0.3, price - 0.3
    for i in range(n):
        price += rng.randn() * 0.6 + 0.4 * np.sin(i / 9.0)
        price = max(price, 5.0)
        hi, lo = price + 0.25, price - 0.25
        if i % 11 == 10:
            hi = max(hi, ph) + 0.15
            lo = min(lo, pl) - 0.15
        rows.append({"date": t0 + pd.Timedelta(minutes=i), "high": hi, "low": lo,
                     "open": price, "close": price, "volume": 1000.0})
        ph, pl = hi, lo
    return pd.DataFrame(rows)


def _sig_legacy_mmds(lines, zs_type_key):
    """legacy 买卖点签名: (line 起点 k_index, mmd.name, 中枢 real)。浮点免疫。"""
    out = []
    for ln in lines:
        mmds = getattr(ln, "zs_type_mmds", {}).get(zs_type_key, [])
        if not mmds:
            continue
        ki = ln.start.k.k_index if ln.start is not None and ln.start.k is not None else None
        for m in mmds:
            zs = getattr(m, "zs", None)
            real = bool(getattr(zs, "real", False))
            out.append((ki, str(getattr(m, "name", "")), real))
    return tuple(sorted(out, key=lambda x: (x[0] if x[0] is not None else -1, x[1], x[2])))


@pytest.mark.parametrize("seed", [3, 11, 29, 101])
def test_legacy_process_mmd_incremental_equals_batch(seed):
    n = 160
    df = _gen_klines(n, seed)
    inc = CL("TST", _FREQ, dict(_CFG))
    for L in range(45, n + 1):
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)
        fresh = CL("TST", _FREQ, dict(_CFG))
        fresh.process_klines(sub)
        for key, li, lb in (("bi", inc.get_bis(), fresh.get_bis()),
                            ("xd", inc.get_xds(), fresh.get_xds())):
            s_inc = _sig_legacy_mmds(li, key)
            s_bat = _sig_legacy_mmds(lb, key)
            assert s_inc == s_bat, (
                f"legacy {key}_mmds inc != batch @ seed={seed} L={L}\n"
                f"  only_inc={set(s_inc) - set(s_bat)}\n"
                f"  only_bat={set(s_bat) - set(s_inc)}"
            )

def test_legacy_mmd_incremental_real_data_nonempty():
    """R1-C10: 真实数据兜底组。合成 seeds 的 xd 层 4/4 恒空元组对比、bi 层 4 seeds
    合计仅 1 个 mmd——守护网近乎空转, 增量分叉抓不到。真实 002299_1m 前 1200 根
    bi/xd 两层 legacy mmd 均非空(反空转断言), 抽样前缀 inc==batch。"""
    import pathlib
    fix = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "SZ.002299_1m.parquet"
    if not fix.exists():
        pytest.skip("tests/fixtures parquet 缺失")
    df = pd.read_parquet(fix)[["date", "open", "high", "low", "close", "volume"]].head(1200).reset_index(drop=True)
    inc = CL("SZ.002299", _FREQ, dict(_CFG))
    prefixes = list(range(600, 1201, 7))
    if prefixes[-1] != 1200:
        prefixes.append(1200)
    for L in prefixes:
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)
        fresh = CL("SZ.002299", _FREQ, dict(_CFG))
        fresh.process_klines(sub)
        for key, li, lb in (("bi", inc.get_bis(), fresh.get_bis()),
                            ("xd", inc.get_xds(), fresh.get_xds())):
            s_inc = _sig_legacy_mmds(li, key)
            s_bat = _sig_legacy_mmds(lb, key)
            assert s_inc == s_bat, (
                f"legacy {key}_mmds inc != batch @ real L={L}\n"
                f"  only_inc={set(s_inc) - set(s_bat)}\n"
                f"  only_bat={set(s_bat) - set(s_inc)}"
            )
    # 反空转: 该组存在的意义就是两层都真的有 mmd 可对比(探针: bi=11, xd=2 @1200)
    assert _sig_legacy_mmds(inc.get_bis(), "bi"), "真实组 bi 层 legacy mmd 为空 = 守护网空转"
    assert _sig_legacy_mmds(inc.get_xds(), "xd"), "真实组 xd 层 legacy mmd 为空 = 守护网空转"
