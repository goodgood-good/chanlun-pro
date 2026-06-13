"""R78 真闭环冒烟:collect_qs_beichi_candidates 在 NVDA 趋势底背驰段进行时是否产 1buy_nest。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import pandas as pd
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import CL_CFG, collect_qs_beichi_candidates
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
cuts = ["2026-05-12 17:03:00+00:00", "2026-05-13 14:00:00+00:00",
        "2026-06-02 15:00:00+00:00", "2026-06-09 14:00:00+00:00"]
for cut in cuts:
    d = df[df["date"] <= pd.Timestamp(cut)].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    levels = cd.get_recursive_branch_levels()
    l0 = next((lv for lv in levels if lv.level == 0), None)
    nlive = len(getattr(l0, "live_qs_divergence", []) or []) if l0 else 0
    qs = collect_qs_beichi_candidates(cd)
    print(f"{cut} L0.live_qs={nlive} qs_intervene={len(qs)}")
    for s in qs[:5]:
        print(f"   1buy_nest @{s.date} px={s.price} stop<{s.structural_stop_below}")
