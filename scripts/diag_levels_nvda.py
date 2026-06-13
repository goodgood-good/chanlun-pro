"""R78 深诊:在关键 cut 上全量 dump 各递归级别的 done/live 背驰结构,定位趋势底背驰真身。

输出每级:done_zss 数、最后一个 done 中枢的 done_divergence(kind/is_beichi/leave_dir)、
live 假设(node1/leave_dir/divergence)、live_qs_divergence 数。
目的:回答「NVDA 05-12 的趋势底背驰到底在哪一级、是 done 还是 live」。
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


def _dir(seg):
    return getattr(seg, "type", None)


def _div_str(dv):
    if dv is None:
        return "None"
    return f"is_beichi={dv.is_beichi} kind={dv.kind} leave_dir={_dir(dv.leave_seg)}"


df = load_chart_cache_klines("us", "NVDA.US", "1m")
cuts = ["2026-05-11 18:00:00+00:00", "2026-05-12 17:03:00+00:00",
        "2026-05-13 14:00:00+00:00"]
for cut in cuts:
    d = df[df["date"] <= pd.Timestamp(cut)].reset_index(drop=True)
    cd = CL("NVDA.US", "1m", dict(CL_CFG))
    cd.process_klines(d)
    levels = cd.get_recursive_branch_levels()
    print(f"\n===== cut={cut}  bars={len(d)}  levels={len(levels)} =====")
    for lv in levels:
        n_done = len(lv.zss)
        n_live = len(lv.live_zss)
        n_lq = len(getattr(lv, "live_qs_divergence", []) or [])
        # 最后一个 done 中枢的背驰
        last_dd = lv.done_divergence[-1] if lv.done_divergence else None
        print(f"  L{lv.level}: done_zss={n_done} live_zss={n_live} live_qs={n_lq}")
        print(f"        last_done_div: {_div_str(last_dd)}")
        # 末 2 个 done 中枢的背驰(看趋势是否已坐实)
        for i in range(max(0, n_done - 2), n_done):
            z = lv.zss[i]
            dd = lv.done_divergence[i] if i < len(lv.done_divergence) else None
            print(f"        done[{i}] zd={z.zd:.2f} zg={z.zg:.2f} dd={z.dd:.2f} gg={z.gg:.2f}  {_div_str(dd)}")
