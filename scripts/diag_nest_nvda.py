"""R78 诊断:NVDA nest_cascade 0 介入根因。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import pandas as pd
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import CL_CFG, collect_nest_cascade_signals, collect_branch_signals
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
for cut in ["2026-06-04 16:00:00+00:00", "2026-06-04 19:06:00+00:00", "2026-06-05 17:41:00+00:00"]:
    d = df[df["date"] <= pd.Timestamp(cut)].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    cands = cd.get_kuozhan_candidates()
    nest = collect_nest_cascade_signals(cd)
    l0buys = [s for s in collect_branch_signals(cd, use_xd=False, annotate_nest=False) if s.is_buy]
    print(cut, "cands=", len(cands), "nest=", len(nest), "l0buys=", len(l0buys))
    for c in cands[:4]:
        print("   cand zg=%.2f zd=%.2f leave_end=%s retest_end=%s" % (c.zg, c.zd, c.leave_end_date, c.retest_end_date))
