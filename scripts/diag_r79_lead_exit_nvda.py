"""R79 领先退出诊断:NVDA 亏损单(structural_invalidation 退出)持有期内,
笔级向上段「不创新高」的领先卖点何时出现,相对实际结构失效退出能提前多少、少亏多少。

领先退出(A2.44):持多头时,次级别(笔级)向上段不创新高 → 该段是潜在顶背驰/盘背
→ 提前退出,不等结构失效(跌破中枢)。本诊断只用「完成的上笔 vs 前一上笔高点」
(walk-forward 可见,上笔完成才知高点),量化领先量。
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
# v11 nest_cascade 两笔亏损单(structural_invalidation)
trades = [
    ("2026-05-13 16:49:00+00:00", "2026-05-26 15:08:00+00:00", "-5.5%"),
    ("2026-06-02 15:47:00+00:00", "2026-06-05 14:48:00+00:00", "-6.7%"),
]
for entry, exit_, ret in trades:
    e0, e1 = pd.Timestamp(entry), pd.Timestamp(exit_)
    d = df[df["date"] <= e1].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    bis = list(cd.get_bis())
    entry_px = float(df[df["date"] == e0]["close"].iloc[0]) if (df["date"] == e0).any() else None
    print(f"\n=== 入场 {entry} @~{entry_px} 实际结构失效退出 {exit_} ({ret}) ===")
    # 持有期内完成的上笔,逐个比较高点(不创新高=领先卖触发)
    prev_up_high = None
    first_lead = None
    for b in bis:
        if b.start is None or b.start.k is None or b.end is None or b.end.k is None:
            continue
        b_end_dt = b.end.k.date
        if b_end_dt < e0 or b_end_dt > e1:
            continue
        if b.type == "up":
            h = b.high
            if prev_up_high is not None and h <= prev_up_high:
                if first_lead is None:
                    first_lead = (b_end_dt, h, prev_up_high)
            prev_up_high = max(prev_up_high, h) if prev_up_high is not None else h
    if first_lead:
        dt, h, ph = first_lead
        lead_px = h
        improve = (lead_px - entry_px) / entry_px * 100 if entry_px else None
        days = (e1 - dt).days
        print(f"  领先卖(首个不创新高上笔完成): {dt} 高点 {h:.2f} <= 前高 {ph:.2f}")
        print(f"  领先退出价~{lead_px:.2f}(vs 实际结构失效晚 {days} 天);若此处退出收益~{improve:+.1f}%")
    else:
        print("  持有期内无「上笔不创新高」领先信号(单边下跌无反抽上笔/数据边界)")
