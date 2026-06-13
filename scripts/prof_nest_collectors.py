"""性能剖析:nest_cascade 各 collector 在不同序列长度下的单次耗时 + 调用的全量装配次数,
定位 O(n^2) 主项。在 n=20k/40k 两点测,确认单次成本 ~O(n) 缩放。
"""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import (
    CL_CFG, collect_branch_signals, collect_nest_cascade_signals,
    collect_qs_beichi_candidates, collect_signals as collect_upgrade_signals,
)
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "TSLA.US", "1m")
print(f"TSLA total bars={len(df)}")


def timeit(fn, n=1):
    t = time.perf_counter()
    for _ in range(n):
        r = fn()
    return (time.perf_counter() - t) / n, r


for n in (20000, 40000):
    d = df.iloc[:n].reset_index(drop=True)
    cd = CL("TSLA.US", "1m", dict(CL_CFG))
    t0 = time.perf_counter()
    cd.process_klines(d)
    t_proc = time.perf_counter() - t0
    n_xds = len(list(cd.get_xds()))
    n_bis = len(list(cd.get_bis()))
    print(f"\n=== n={n}  process={t_proc:.2f}s  xds={n_xds} bis={n_bis} ===")
    t_lvl, _ = timeit(cd.get_recursive_branch_levels)
    t_bsp, _ = timeit(lambda: cd.get_branch_bspoints(use_xd=False))
    t_brs, _ = timeit(lambda: collect_branch_signals(cd, use_xd=False, annotate_nest=False))
    t_upg, _ = timeit(lambda: collect_upgrade_signals(cd))
    t_nest, _ = timeit(lambda: collect_nest_cascade_signals(cd))
    t_qs, _ = timeit(lambda: collect_qs_beichi_candidates(cd))
    print(f"  get_recursive_branch_levels : {t_lvl*1000:7.1f} ms")
    print(f"  get_branch_bspoints(bi)     : {t_bsp*1000:7.1f} ms")
    print(f"  collect_branch_signals(bi)  : {t_brs*1000:7.1f} ms")
    print(f"  collect_upgrade_signals     : {t_upg*1000:7.1f} ms")
    print(f"  collect_nest_cascade_signals: {t_nest*1000:7.1f} ms")
    print(f"  collect_qs_beichi_candidates: {t_qs*1000:7.1f} ms")
    one_recollect = t_upg + t_nest + t_qs
    print(f"  >> 单次 _collect_visible_signals(nest) ≈ {one_recollect*1000:.1f} ms")
    print(f"  >> 若每笔重算 × {n_bis} 笔 ≈ {one_recollect*n_bis/60:.1f} 分钟(仅此 n)")
