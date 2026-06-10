"""scripts/wf_validate.py — 抽样验证:真·walk-forward(逐根增量重算,实时信号) vs 预算信号回测。

量化「确认滞后 + 幻影信号(右边缘 repaint)」对收益的影响:若两者接近,则预算信号回测=实盘可信;
若 walk-forward 明显更低,则预算回测乐观。5m-only 多头、忽略费用(对比差值,费用两边抵消)。
运行: PYTHONPATH="src;web/chanlun_chart;." python -m chanlun.recursive_bt.validate [trend]
trend 子命令: 周线笔方向事件确认滞后实证(截断重算)——定 trend 门控 delay 下界,防 lookahead。
"""
import glob
import os
import sys

from chanlun.recursive_bt.engine import CL_CFG, collect_branch_signals
from chanlun.core.cl import CL
from chanlun.exchange.exchange_qmt import ExchangeQMT

WIN = 3000   # 验证窗口(最近~3个月5m)


def ret_of(order_bars, opens, closes):
    """order_bars: 时间序 [(bar_idx,'buy'/'sell')];多头、下一bar开盘成交、末尾强平。"""
    cash, sh = 1.0, 0.0
    n = len(opens)
    for bi, act in order_bars:
        if bi + 1 >= n:
            continue
        px = opens[bi + 1]
        if act == "buy" and sh == 0:
            sh, cash = cash / px, 0.0
        elif act == "sell" and sh > 0:
            cash, sh = sh * px, 0.0
    if sh > 0:
        cash = sh * closes[-1]
    return cash - 1


def main():
    ex = ExchangeQMT()
    codes = sorted(os.path.basename(f)[:-4]
                   for f in glob.glob("D:/chanlun_pro/bt_data/*.pkl")
                   if "SH.000001" not in f)[:10]
    print(f"抽样 {len(codes)} 只,窗口最近 {WIN} 根5m,真·walk-forward vs 预算信号:")
    pre_sum = wf_sum = 0.0
    cnt = 0
    for code in codes:
        df = ex.klines(code, "5m")
        if df is None or len(df) < WIN + 500:
            continue
        n = len(df)
        w0 = n - WIN
        opens = df["open"].to_numpy()
        closes = df["close"].to_numpy()
        dates = list(df["date"])
        d2i = {d: i for i, d in enumerate(dates)}

        # 预算信号(全序列CL一次)→窗口内交易
        cdf = CL(code, "5m", dict(CL_CFG))
        cdf.process_klines(df)
        pre_bars = []
        for s in sorted(collect_branch_signals(cdf, use_xd=False), key=lambda x: x.date):
            i = d2i.get(s.date)
            if i is not None and i >= w0:
                pre_bars.append((i, "buy" if s.is_buy else "sell"))
        pre_ret = ret_of(pre_bars, opens, closes)

        # 真·walk-forward:逐根增量,信号首次出现时即动作(含幻影)
        cd = CL(code, "5m", dict(CL_CFG))
        cd.process_klines(df.iloc[:w0].reset_index(drop=True))
        ever = set()
        wf_bars = []
        for i in range(w0, n):
            cd.process_klines(df.iloc[: i + 1].reset_index(drop=True))
            for s in collect_branch_signals(cd, use_xd=False):
                k = (s.date, s.bs_type)
                if k in ever:
                    continue
                ever.add(k)
                wf_bars.append((i, "buy" if s.is_buy else "sell"))
        wf_ret = ret_of(wf_bars, opens, closes)

        pre_sum += pre_ret
        wf_sum += wf_ret
        cnt += 1
        print(f"  {code}: 预算={pre_ret:+6.1%}  walk-forward={wf_ret:+6.1%}  "
              f"差={wf_ret - pre_ret:+6.1%}  (预算{len(pre_bars)}动作 wf{len(wf_bars)}动作)")
    if cnt:
        print(f"\n均值: 预算={pre_sum/cnt:+.1%}  walk-forward={wf_sum/cnt:+.1%}  "
              f"差={ (wf_sum-pre_sum)/cnt:+.1%}")
        print("=> 差值小→预算回测≈实盘可信;walk-forward明显更低→预算乐观(幻影/滞后吃收益)。")


def trend_lag(n_codes: int = 8, tf: str = "w", start="2022-01-01", end="2024-12-31"):
    """周线笔方向事件(collect_dir_events)的**确认滞后**实证:逐根截断重算,
    统计每个事件(笔start,方向)首次稳定出现(其后不消失不变向)距 start 多少根 bar。
    trend 门控 delay 必须 ≥ 高分位滞后,否则回测用了「事后才确认的笔起点」=lookahead。"""
    import numpy as np
    import pandas as pd
    from chanlun.recursive_bt.engine import collect_dir_events
    ex = ExchangeQMT()
    codes = sorted(os.path.basename(f)[:-4]
                   for f in glob.glob("D:/chanlun_pro/bt_data_daily/*.pkl")
                   if "SH.000001" not in f)[:n_codes]
    lags = []
    for code in codes:
        df = ex.klines(code, tf, start_date=start)
        if df is None or len(df) < 60:
            continue
        df = df[df["date"] <= pd.Timestamp(end, tz="Asia/Shanghai")].reset_index(drop=True)
        n = len(df)
        cdf = CL(code, tf, dict(CL_CFG))
        cdf.process_klines(df)
        full = set(collect_dir_events(cdf))
        present_at = {}                    # 事件 -> 自哪个截断idx起持续存在
        for t in range(30, n + 1):
            cd = CL(code, tf, dict(CL_CFG))
            cd.process_klines(df.iloc[:t].reset_index(drop=True))
            evs = set(collect_dir_events(cd))
            for ev in evs:
                present_at.setdefault(ev, t)
            for ev in list(present_at):
                if ev not in evs:          # 中途消失(repaint)→重置,要求「稳定」存在
                    del present_at[ev]
        d2i = {d: i for i, d in enumerate(df["date"])}
        code_lags = []
        for ev, t in present_at.items():
            if ev not in full:
                continue
            si = d2i.get(ev[0])
            if si is None:
                continue
            code_lags.append((t - 1) - si)   # 截断含前t根→确认bar=t-1
        lags += code_lags
        if code_lags:
            print(f"  {code}: 事件{len(code_lags)}个 滞后bar 中位={np.median(code_lags):.0f} "
                  f"max={max(code_lags)}")
    a = np.array(sorted(lags))
    print(f"\n汇总 {len(a)} 事件: 滞后bar 中位={np.median(a):.0f} 均值={a.mean():.1f} "
          f"p90={np.percentile(a, 90):.0f} p95={np.percentile(a, 95):.0f} max={a.max()}")
    print(f"=> {tf} 门控 delay 应 ≥ p90~p95(周线1bar=7天)。低于此=lookahead 泄漏。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "trend":
        trend_lag()
    else:
        main()
