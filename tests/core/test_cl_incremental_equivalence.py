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
import copy
import pathlib
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
        out.append((ki0, ki1, _r(zs.zd), _r(zs.zg), len(zs.lines), bool(zs.done), getattr(zs, "type", None)))
    return tuple(out)


def _round6(x):
    # float 用 round(6) 免 ~1e-16 包含处理噪声在 == 对拍下误报分裂(非 float 原样透传)
    return round(x, 6) if isinstance(x, float) else x


def _sig_bsp(cd, use_xd):
    out = []
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        af = getattr(p, "anchor_fx", None)
        ki = af.k.k_index if af is not None and af.k is not None else None
        # 终检盲区补: structural_stop_below/above + rebound_target 驱动 portfolio 结构止损/最小出场目标,
        # 须纳入 inc==batch 对拍(原只比 bs_type/k_index/level, 这些字段漂移抓不到)。
        out.append((
            str(p.bs_type), ki, p.level,
            _round6(getattr(p, "structural_stop_below", None)),
            _round6(getattr(p, "structural_stop_above", None)),
            _round6(getattr(p, "rebound_target", None)),
        ))
    return tuple(sorted(out, key=lambda x: (x[0], x[1] if x[1] is not None else -1, x[2] or 0)))


def _sig_zslx(zslxs):
    # 走势类型段(ZSLX): start/end k_index 位置锚(整数浮点免疫) + LINE 方向 + 盘整/趋势 + 含中枢数 + done
    out = []
    for z in zslxs:
        ki0 = z.start.k.k_index if z.start is not None else None
        ki1 = z.end.k.k_index if z.end is not None else None
        out.append((ki0, ki1, z.type, getattr(z, "zslx_type", None), len(z.zss or []), bool(z.is_done())))
    return tuple(out)


def _all_sigs(cd):
    return {
        "bis": _sig_lines(cd.get_bis()),
        "xds": _sig_lines(cd.get_xds()),
        "bi_zss": _sig_zss(cd.get_bi_zss()),
        "xd_zss": _sig_zss(cd.get_xd_zss()),
        "bsp_bi": _sig_bsp(cd, use_xd=False),
        "bsp_xd": _sig_bsp(cd, use_xd=True),
        "xd_zslx": _sig_zslx(cd.get_xd_zslx()),
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


def _gen_zss_klines(n, seed):
    """多频强震荡, 产生线段中枢(xd_zss>=1), 使 zslx.calculate 走 _wt_restart 而非空 zss 早退。"""
    rng = np.random.RandomState(seed)
    idx = np.arange(n)
    prices = 100 + 3 * np.sin(idx / 13.0) + 2 * np.sin(idx / 5.0) + 1 * np.sin(idx / 2.3) + rng.randn(n) * 0.2
    t0 = pd.Timestamp("2024-01-01 09:30:00")
    rows = []
    for i in range(n):
        p = float(prices[i])
        rows.append({"date": t0 + pd.Timedelta(minutes=i), "high": p + 0.25, "low": p - 0.25, "open": p, "close": p, "volume": 1000.0})
    return pd.DataFrame(rows)


@pytest.mark.parametrize("seed", [7, 29])
def test_cl_deepcopy_then_incremental_equals_batch(seed):
    """持久 CL 池 store_cl_to_pool 的 copy.deepcopy(cl) 复用回归网。

    预热 CL(线段中枢非空) -> deepcopy(模拟入池, 触发 __getstate__/__setstate__) ->
    池副本继续增量 process_klines。必须 (a) 不抛异常 (b) 逐前缀 inc==batch。

    修复前:ZslxCalculator.__setstate__ 漏恢复 _wt_state, _wt_restart 首行直接访问
    self._wt_state -> AttributeError: object has no attribute '_wt_state'。
    xd_zss 非空守卫确保真的走 _wt_restart(空 zss 会 early-return 重建属性而绕过 bug)。
    """
    n, warm_L = 480, 450
    df = _gen_zss_klines(n, seed)
    base = CL("TST", _FREQ, dict(_CFG))
    base.process_klines(df.iloc[:warm_L].reset_index(drop=True))
    assert len(base.get_xd_zss()) >= 1, "预热须产生线段中枢, 否则绕过 _wt_restart"
    pooled = copy.deepcopy(base)          # 模拟 store_cl_to_pool: copy.deepcopy(cl)
    for L in range(warm_L + 1, n + 1):
        sub = df.iloc[:L].reset_index(drop=True)
        pooled.process_klines(sub)        # 修复前首步即抛 _wt_state AttributeError
        inc_sigs = _all_sigs(pooled)
        fresh = CL("TST", _FREQ, dict(_CFG))
        fresh.process_klines(sub)
        bat_sigs = _all_sigs(fresh)
        for key in inc_sigs:
            assert inc_sigs[key] == bat_sigs[key], (
                f"deepcopy-inc != batch @ seed={seed} L={L} key={key}\n"
                f"  only_inc={set(inc_sigs[key]) - set(bat_sigs[key])}\n"
                f"  only_bat={set(bat_sigs[key]) - set(inc_sigs[key])}"
            )


_FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.skipif(not _FIX.exists(), reason="tests/fixtures 缺失(parquet 未恢复)")
@pytest.mark.parametrize("rel,code,freq,lo,hi", [
    ("SH.600519_5m.parquet", "SH.600519", "5m", 420, 500),
    ("SZ.002299_1m.parquet", "SZ.002299", "1m", 780, 880),
])
def test_cl_incremental_equals_batch_real_parquet(rel, code, freq, lo, hi):
    """真实 parquet 逐前缀 inc==batch(真实浮点边界 ~4e-16, 合成数据不覆盖; 补 D1-F1 盲区)。"""
    df = pd.read_parquet(_FIX / rel)
    df = df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    hi = min(hi, len(df))
    inc = CL(code, freq, dict(_CFG))
    for L in range(lo, hi + 1):
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)
        inc_sigs = _all_sigs(inc)
        fresh = CL(code, freq, dict(_CFG))
        fresh.process_klines(sub)
        bat_sigs = _all_sigs(fresh)
        for key in inc_sigs:
            assert inc_sigs[key] == bat_sigs[key], (
                f"real inc != batch @ {code} L={L} key={key} only_inc="
                f"{set(inc_sigs[key]) - set(bat_sigs[key])} only_bat="
                f"{set(bat_sigs[key]) - set(inc_sigs[key])}"
            )

def test_cl_incremental_equals_batch_real_xd_layer():
    """R1-C11: 合成 160 根 seeds 在 xd_zss/bsp_xd/xd_zslx 三个签名 key 恒空对比,
    线段层增量守护实际空转(此前仅 002299 单组前缀 101 + deepcopy 组独木)。
    真实 600519_5m 前 2500 根三 key 全非空(反空转断言, 探针: xd_zss=3/bsp_xd=1/
    xd_zslx=3), 抽样前缀 inc==batch 全签名严格相等。"""
    import pathlib
    fix = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "SH.600519_5m.parquet"
    if not fix.exists():
        pytest.skip("tests/fixtures parquet 缺失")
    df = pd.read_parquet(fix)[["date", "open", "high", "low", "close", "volume"]].head(2500).reset_index(drop=True)
    inc = CL("SH.600519", "5m", dict(_CFG))
    prefixes = list(range(1200, 2501, 13))
    if prefixes[-1] != 2500:
        prefixes.append(2500)
    for L in prefixes:
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)
        inc_sigs = _all_sigs(inc)
        fresh = CL("SH.600519", "5m", dict(_CFG))
        fresh.process_klines(sub)
        bat_sigs = _all_sigs(fresh)
        for key in inc_sigs:
            assert inc_sigs[key] == bat_sigs[key], (
                f"inc != batch @ real L={L} key={key}\n"
                f"  only_inc={set(inc_sigs[key]) - set(bat_sigs[key])}\n"
                f"  only_bat={set(bat_sigs[key]) - set(inc_sigs[key])}"
            )
    final = _all_sigs(inc)
    for key in ("xd_zss", "bsp_xd", "xd_zslx"):
        assert final[key], f"真实组 {key} 为空 = 线段层守护网空转"


def test_cl_incremental_us_htf_day_offset():
    """R12-1 硬化:美股 30m→d 日级分桶用 -5 时区偏移(market='us'),此前 inc==batch
    真实网只有 A股 5m/1m(不走日级 offset 分桶),HigherMACDCalculator 的 US 偏移
    _key_for/_bucket_keys 路径零对拍覆盖(critic 点名 coverage-by-luck 盲区)。

    用 QQQ.US_30m 真实 parquet 显式 market='us' 触发 -5 分桶,逐前缀增量 == 全量;
    并断言 htf 已激活 + us(-5) 与 a(+8) 的 htf 实际不同(反空转,证 offset 真生效)。"""
    fix = _FIX / "QQQ.US_30m.parquet"
    if not fix.exists():
        pytest.skip("tests/fixtures/QQQ.US_30m.parquet 缺失")
    df = pd.read_parquet(fix)[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    n = len(df)
    hi = min(n, 600)
    lo = max(60, hi - 80)
    inc = CL("QQQ.US", "30m", dict(_CFG), market="us")
    for L in range(lo, hi + 1):
        sub = df.iloc[:L].reset_index(drop=True)
        inc.process_klines(sub)
        inc_sigs = _all_sigs(inc)
        fresh = CL("QQQ.US", "30m", dict(_CFG), market="us")
        fresh.process_klines(sub)
        bat_sigs = _all_sigs(fresh)
        for key in inc_sigs:
            assert inc_sigs[key] == bat_sigs[key], (
                f"US htf inc != batch @ L={L} key={key}\n"
                f"  only_inc={set(inc_sigs[key]) - set(bat_sigs[key])}\n"
                f"  only_bat={set(bat_sigs[key]) - set(inc_sigs[key])}"
            )
    assert inc._htf_macd is not None, "末前缀 htf 未激活 = 未覆盖 30m→d offset 分桶路径"
    other = CL("QQQ.US", "30m", dict(_CFG), market="a")
    other.process_klines(df.iloc[:hi].reset_index(drop=True))
    du = np.asarray(inc._htf_macd["hist"])
    da = np.asarray(other._htf_macd["hist"])
    assert (np.abs(du - da) > 1e-9).any(), "us(-5)==a(+8) htf = offset 未生效, 测试空转"
