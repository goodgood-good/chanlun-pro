"""R78 提前量实测:NVDA 逼近真实趋势底背驰(L0 done[4]@05-05)时,沿 cut 逐步推进,
看「done 趋势是否已确立」+「live 离开段读法」,判断 _is_trend(live) 增强能否提前触发,
以及相对 done 背驰坐实的提前量。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import pandas as pd
from chanlun.core.cl import CL
from chanlun.core.beichi_calculator import is_qs, is_beichi
from chanlun.core.cl_interface import query_macd_ld
from chanlun.core.zs_branch import ZsBranchCalculator, classify_rel
from chanlun.recursive_bt.engine import CL_CFG
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
# 在 05-04 到 05-06 之间取若干 cut(趋势底背驰 done[4] 末端 @05-05 15:29)
lo, hi = pd.Timestamp("2026-05-04 17:00:00+00:00"), pd.Timestamp("2026-05-06 14:00:00+00:00")
window = df[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)
idxs = list(range(0, len(window), max(1, len(window) // 12)))
cuts = [window.loc[i, "date"] for i in idxs]

wzgx_global = None
for cut in cuts:
    d = df[df["date"] <= cut].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    wzgx = cd._recursive_wzgx()
    ld = lambda s, e: query_macd_ld(cd, s, e)
    res = ZsBranchCalculator(ld_provider=ld, frequency="1m", wzgx=wzgx, min_zs_lines=3).calculate(list(cd.get_xds()))
    done = res.done_zss
    # 已确立趋势? 末两 done 中枢 is_qs
    est = is_qs(done[-2], done[-1], wzgx, use_core_envelope=True) if len(done) >= 2 else None
    # live leave 读法
    leave_h = next((h for h in res.live if h.node1 == "leave"), None)
    info = "no-leave"
    if leave_h is not None:
        z = leave_h.zs
        a = z.start
        c = z.lines[-1] if z.lines else None
        at = getattr(a, "type", None); ct = getattr(c, "type", None)
        if a is not None and c is not None and at == ct:
            bc = is_beichi(a, c, ld, "1m")
            rel = classify_rel(done[-1], z) if done else None
            info = f"a={at} c={ct} is_beichi={bc} rel(prev,live)={rel}"
        else:
            info = f"turn a={at} c={ct}(转折/缺段)"
    print(f"{cut} done={len(done)} est_trend={est} | live_leave: {info}")
