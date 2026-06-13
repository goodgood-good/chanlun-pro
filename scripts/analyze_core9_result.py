"""core9 生产回测结果分析:产出收益/回撤/夏普/胜率 + 区间套(1buy_nest/3buy_nest)入场及其
单笔 P&L + 按标的拆分。core9 全基线跑完即 `python scripts/analyze_core9_result.py`。
"""
import json
import csv
from collections import defaultdict

SUM = "D:/chanlun_pro/reports/us_live_parity_backtest_summary.json"
TRD = "D:/chanlun_pro/reports/us_live_parity_backtest_trades.csv"
SIG = "D:/chanlun_pro/reports/us_live_parity_backtest_trades_signals.csv"

try:
    d = json.load(open(SUM, encoding="utf-8"))
    def g(k):
        return d.get(k)
    print("=== 组合汇总 ===")
    for k in ["total_return", "excess_return", "benchmark_return", "max_drawdown",
              "benchmark_drawdown", "sharpe", "win_rate", "num_trades"]:
        v = g(k)
        if v is not None:
            print(f"  {k}: {round(v*100,2) if isinstance(v,float) and abs(v)<10 else v}")
except Exception as e:
    print("summary 读失败:", e)

try:
    rows = list(csv.DictReader(open(TRD, encoding="utf-8-sig")))
    print(f"\n=== 交易 {len(rows)} 笔 ===")
    by_sym = defaultdict(lambda: [0, 0.0, 0])  # n, sum_ret, n_win
    reasons = defaultdict(int)
    for r in rows:
        code = r.get("code", "?")
        ret = float(r.get("ret", 0) or 0)
        by_sym[code][0] += 1
        by_sym[code][1] += ret
        by_sym[code][2] += 1 if ret > 0 else 0
        reasons[r.get("reason", "?")] += 1
    print("  按标的:")
    for code, (n, sret, nw) in sorted(by_sym.items()):
        print(f"    {code}: {n}笔 累计{round(sret*100,1)}% 胜{nw}/{n}")
    print("  退出原因分布:", dict(reasons))
except Exception as e:
    print("trades 读失败:", e)

try:
    srows = list(csv.DictReader(open(SIG, encoding="utf-8-sig")))
    nest = [s for s in srows if "nest" in str(s.get("bs_type", ""))]
    from collections import Counter
    print(f"\n=== 区间套信号 {len(nest)} 个 ===", dict(Counter(s.get("bs_type") for s in nest)))
    for s in nest[:15]:
        print(f"    {s.get('code')} {s.get('bs_type')} {s.get('anchor_time')} @{s.get('signal_price')}")
except Exception as e:
    print("signals 读失败:", e)
