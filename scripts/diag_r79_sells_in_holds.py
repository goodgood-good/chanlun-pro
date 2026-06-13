"""R79 根因定位:三笔交易(1赢2亏)持有期内,笔级卖点(1sell/2sell/3sell)是否存在。
若亏损单持有期有笔级卖点却没被消费 → 消费侧修复(portfolio 门控/层级吞掉小级别卖);
若没有 → 生成侧(需领先退出新信号)。同时看赢单是否也有早期卖点(校准 over-trigger)。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
import pandas as pd
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import CL_CFG, collect_branch_signals
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines

df = load_chart_cache_klines("us", "NVDA.US", "1m")
trades = [
    ("2026-05-04 17:33:00+00:00", "2026-05-11 16:45:00+00:00", "赢 +11.95% (small_level_sell_point)"),
    ("2026-05-13 16:49:00+00:00", "2026-05-26 15:08:00+00:00", "亏 -5.5% (structural_invalidation)"),
    ("2026-06-02 15:47:00+00:00", "2026-06-05 14:48:00+00:00", "亏 -6.7% (structural_invalidation)"),
]
for entry, exit_, label in trades:
    e0, e1 = pd.Timestamp(entry), pd.Timestamp(exit_)
    d = df[df["date"] <= e1].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    sub = collect_branch_signals(cd, use_xd=False, annotate_nest=False)
    sells = [s for s in sub if not s.is_buy and e0 <= s.date <= e1]
    print(f"\n=== {label}\n    入场 {entry} 退出 {exit_} ===")
    print(f"  持有期笔级卖点数={len(sells)}")
    for s in sells[:12]:
        lv = int(getattr(s, "level", 0) or 0)
        print(f"    {s.date} {s.bs_type} L{lv} @{s.price:.2f}")
