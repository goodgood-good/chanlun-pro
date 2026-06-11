# -*- coding: utf-8 -*-
"""诊断 1m 图 L2(30m tongjibie) 为空：L1 kuozhan 中枢序列的摆动腿与三段重合。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.exchange.exchange_qmt import ExchangeQMT
from chanlun.core.cl import CL as CoreCL
from chanlun.core.zs_upgrade import kuozhan_zhongshu, _swing_alternating_segs, _tongjibie_groups
from chanlun.core.zslx_branch import ZslxBranchCalculator

CFG = {
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}

df = ExchangeQMT().klines("SH.000001", "1m", start_date="2026-01-05", end_date="2026-06-11")
cd = CoreCL("SH.000001", "1m", dict(CFG))
cd.process_klines(df)
lv0 = cd.get_recursive_branch_levels()[0]
xds = list(cd.get_xds())
l1 = kuozhan_zhongshu(lv0.zss, xds)
print(f"L1 kuozhan 中枢 {len(l1)} 个:")
for i, z in enumerate(l1):
    print(f"  k{i}: core[{z.zd:.1f},{z.zg:.1f}] env[{z.dd:.1f},{z.gg:.1f}] "
          f"start={'None' if z.start is None else round(z.start.start.val,1)} "
          f"end={'None' if z.end is None else round(z.end.start.val,1)} done={z.done} "
          f"{z.lines[0].start.k.date.date()}~{z.lines[-1].end.k.date.date()}")
sw = ZslxBranchCalculator._swing_segments(l1)
print("摆动腿:", sw)
segs = _swing_alternating_segs(l1)
for i, s in enumerate(segs):
    sv = s.start.val if s.start is not None else None
    ev = s.end.val if s.end is not None else None
    print(f"  seg[{i}] {s.dir} [{s.zs_low:.1f},{s.zs_high:.1f}] {sv}->{ev} n={len(s.zss)}")
print("groups:", _tongjibie_groups(segs))
if len(segs) >= 3:
    tri = segs[0:3]
    print("前三段重合: zd=%.1f zg=%.1f" % (max(s.zs_low for s in tri), min(s.zs_high for s in tri)))
