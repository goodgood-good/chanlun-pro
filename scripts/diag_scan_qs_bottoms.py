"""R78 决策诊断:全量处理一次,扫描各级别历史上真实的「下跌趋势底背驰」位置。

对每级 done 中枢序列,找相邻对 is_qs==down(本体包络)的趋势,且后者 done_div
为 down 背驰(is_beichi+leave_dir==down)。这才是区间套真正要提前介入的买点。
若 NVDA 全程无此结构 → 区间套下跌买点在此数据天然缺席,R78 价值有限。
同时打印 up 顶背驰对照(看 sell 侧是否丰富),量化 buy/sell 不对称。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
from chanlun.core.cl import CL
from chanlun.core.beichi_calculator import is_qs
from chanlun.recursive_bt.engine import CL_CFG
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines


def scan(symbol, market):
    df = load_chart_cache_klines(market, symbol, "1m")
    cd = CL(symbol, "1m", dict(CL_CFG))
    cd.process_klines(df)
    wzgx = cd._recursive_wzgx()
    levels = cd.get_recursive_branch_levels()
    print(f"\n##### {symbol} bars={len(df)} levels={len(levels)} #####")
    for lv in levels:
        zss, divs = lv.zss, lv.done_divergence
        down_bei = up_bei = 0
        down_qs_bottoms = []
        for i in range(len(zss)):
            dv = divs[i] if i < len(divs) else None
            if dv is None or not dv.is_beichi:
                continue
            ld = getattr(dv.leave_seg, "type", None)
            if ld == "down":
                down_bei += 1
                # 是否趋势底背驰:与前一中枢 is_qs==down
                if i > 0:
                    qd = is_qs(zss[i - 1], zss[i], wzgx, use_core_envelope=True)
                    if qd == "down" and dv.kind == "qs":
                        end_dt = getattr(getattr(dv.leave_seg, "end", None), "k", None)
                        dt = getattr(end_dt, "date", "?")
                        down_qs_bottoms.append((i, dt, round(zss[i].dd, 2)))
            elif ld == "up":
                up_bei += 1
        print(f"  L{lv.level}: done_zss={len(zss)}  down背驰={down_bei} up背驰={up_bei}  趋势底背驰={len(down_qs_bottoms)}")
        for idx, dt, dd in down_qs_bottoms:
            print(f"        趋势底背驰 done[{idx}] @{dt} zs.dd={dd}")


for sym, mkt in [("NVDA.US", "us"), ("TSLA.US", "us"), ("QQQ.US", "us")]:
    try:
        scan(sym, mkt)
    except Exception as e:
        print(f"  {sym} skip: {e}")
