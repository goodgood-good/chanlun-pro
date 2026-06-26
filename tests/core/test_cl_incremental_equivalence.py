"""inc==batch 对拍网恢复 — 审计 D4-CRIT-1。

背景:核心增量优化(bi 端点栈 / zs 续扫 identity-LCP / zslx plan_cache / bs_point 增量重启 /
HTF 增量)的注释都声称"等价性由 test_incremental_equivalence 守护",但该测试组在本活动分支
(feat/recursive-level-colors)随 tests/chan_core 被删、tests/core 这组又只在 master(且 master
无 perf-era 优化代码)→ 增量正确性在 HEAD 零运行守护(D4-CRIT-1)。

本测试用合成 K 线(随机游走 + 周期性强包含)逐前缀对拍:
  增量(同一 CL 实例反复 process_klines 切片递增,触发增量重启路径)
  vs 全量(每个前缀新建 CL 实例,走全量计算)
对 bis / xds / bi_zss / xd_zss / 背驰类买卖点 的结构签名,要求逐前缀 0 fork。

注:D4 finder 已用同法证当前 HEAD 实现 inc==batch 正确;本测试把该守护固化为运行时回归网。
合成数据非真实 parquet 浮点边界(~4e-16,fixture 已删),真实浮点对拍待恢复真 parquet 后补
(D4-CRIT-1 修复建议:`git checkout master -- tests/core/` 取回并适配 HEAD 优化版签名)。
"""
import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL

_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}
_FREQ = "1m"


def _gen_klines(n: int, seed: int) -> pd.DataFrame:
    """随机游走 + 每 11 根注入一次强包含(吞没前根 high/low),逼出包含处理 churn。"""
    rng = np.random.RandomState(seed)
    t0 = pd.Timestamp("2024-01-01 09:30:00")
    rows = []
    price = 100.0
    ph, pl = price + 0.3, price - 0.3
    for i in range(n):
        price += rng.randn() * 0.6 + 0.4 * np.sin(i / 9.0)
        price = max(price, 5.0)
        hi, lo = price + 0.25, price - 0.25
        if i % 11 == 10:  # 强包含:吞没前一根, 制造包含处理
            hi = max(hi, ph) + 0.15
            lo = min(lo, pl) - 0.15
        rows.append({
            "date": t0 + pd.Timedelta(minutes=i),
            "high": hi, "low": lo, "open": price, "close": price, "volume": 1000.0,
        })
        ph, pl = hi, lo
    return pd.DataFrame(rows)


def _sig_lines(lines):
    return tuple(
        (ln.start.k.k_index, ln.end.k.k_index, ln.type, bool(ln.is_done()))
        for ln in lines
    )


def _r(v):
    return round(float(v), 6) if v is not None else None


def _sig_zss(zss):
    # zs.start/zs.end 是 LINE(非 FX),用其 lines 的首末 k_index 作位置锚(整数, 浮点免疫)
    out = []
    for zs in zss:
        if zs.lines:
            ki0 = zs.lines[0].start.k.k_index
            ki1 = zs.lines[-1].end.k.k_index
        else:
            ki0 = ki1 = None
        out.append((ki0, ki1, _r(zs.zd), _r(zs.zg), len(zs.lines), bool(zs.done)))
    return tuple(out)


def _sig_bsp(cd, use_xd):
    out = []
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        af = getattr(p, "anchor_fx", None)
        ki = af.k.k_index if af is not None and af.k is not None else None
        out.append((str(p.bs_type), ki, p.level))
    return tuple(sorted(out, key=lambda x: (x[0], x[1] if x[1] is not None else -1, x[2] or 0)))


def _all_sigs(cd):
    return {
        "bis": _sig_lines(cd.get_bis()),
        "xds": _sig_lines(cd.get_xds()),
        "bi_zss": _sig_zss(cd.get_bi_zss()),
        "xd_zss": _sig_zss(cd.get_xd_zss()),
        "bsp_bi": _sig_bsp(cd, use_xd=False),
        "bsp_xd": _sig_bsp(cd, use_xd=True),
    }


@pytest.mark.parametrize("seed", [3, 11, 29, 101])
def test_cl_incremental_equals_batch_per_prefix(seed):
    """逐前缀:同一 CL 实例增量摄入 == 每前缀新建实例全量计算(结构签名严格相等)。"""
    n = 160
    df = _gen_klines(n, seed)
    inc = CL("TST", _FREQ, dict(_CFG))
    for L in range(45, n + 1):
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)              # 增量:同实例追加(触发增量重启路径)
        inc_sigs = _all_sigs(inc)
        fresh = CL("TST", _FREQ, dict(_CFG))
        fresh.process_klines(sub)            # 全量:新实例
        bat_sigs = _all_sigs(fresh)
        for key in inc_sigs:
            assert inc_sigs[key] == bat_sigs[key], (
                f"inc != batch @ seed={seed} L={L} key={key}\n"
                f"  only_inc={set(inc_sigs[key]) - set(bat_sigs[key])}\n"
                f"  only_bat={set(bat_sigs[key]) - set(inc_sigs[key])}"
            )
