"""R78 诊断:L0 live hypotheses 的 node1 分布 + leave 读法 divergence,定位 live_qs=0 原因。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import collections
import pandas as pd
from chanlun.core.cl import CL
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator
from chanlun.recursive_bt.engine import CL_CFG
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
for cut in ["2026-05-13 14:00:00+00:00", "2026-06-02 15:00:00+00:00", "2026-06-09 14:00:00+00:00"]:
    d = df[df["date"] <= pd.Timestamp(cut)].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    ld = lambda s, e: query_macd_ld(cd, s, e)
    wzgx = cd._recursive_wzgx()
    res = ZsBranchCalculator(ld_provider=ld, frequency="1m", wzgx=wzgx, min_zs_lines=3).calculate(list(cd.get_xds()))
    nodes = collections.Counter(h.node1 for h in res.live)
    print(f"{cut} done_zss={len(res.done_zss)} live={len(res.live)} node1={dict(nodes)}")
    for h in res.live:
        if h.node1 == "leave":
            dv = h.divergence
            if dv is None:
                print("   leave: divergence=None")
            else:
                print(f"   leave: is_beichi={dv.is_beichi} kind={dv.kind} leave_dir={getattr(dv.leave_seg,'type',None)}")
