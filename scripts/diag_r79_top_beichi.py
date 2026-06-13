"""R79 结构化领先退出premise:L0 live 趋势顶背驰段(live_qs_divergence, leave UP, kind=qs)
是否在亏损单顶部出现、而赢单中途回调不出现。若是,卖出侧区间套(R78 镜像)是正解。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import pandas as pd
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import CL_CFG
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
cuts = [
    ("赢单中途回调(应无顶背驰)", "2026-05-07 15:00:00+00:00"),
    ("赢单中途回调(应无顶背驰)", "2026-05-08 15:00:00+00:00"),
    ("赢单顶/出场(可有)", "2026-05-11 14:30:00+00:00"),
    ("亏损单1顶(应有顶背驰)", "2026-05-14 14:00:00+00:00"),
    ("亏损单1顶(应有顶背驰)", "2026-05-14 18:00:00+00:00"),
    ("亏损单2顶(应有顶背驰)", "2026-06-02 16:30:00+00:00"),
    ("亏损单2顶(应有顶背驰)", "2026-06-02 18:00:00+00:00"),
]
for label, cut in cuts:
    d = df[df["date"] <= pd.Timestamp(cut)].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    levels = cd.get_recursive_branch_levels()
    l0 = next((lv for lv in levels if lv.level == 0), None)
    lq = getattr(l0, "live_qs_divergence", []) or [] if l0 else []
    ups = [(zs, dv) for zs, dv in lq if getattr(dv.leave_seg, "type", None) == "up"]
    downs = [(zs, dv) for zs, dv in lq if getattr(dv.leave_seg, "type", None) == "down"]
    tag = f"UP顶背驰={len(ups)} DOWN底背驰={len(downs)}"
    extra = ""
    if ups:
        zs, dv = ups[0]
        extra = f"  (顶背驰段 is_beichi={dv.is_beichi} zg={zs.zg:.2f} gg={zs.gg:.2f})"
    print(f"{cut} [{label}] {tag}{extra}")
