# -*- coding: utf-8 -*-
"""TSLA 重绘/滞后偏差实测:预算信号回测 vs 真·walk-forward(尾喂增量,实盘可复制)。

与 chanlun.recursive_bt.validate.run() 同方法,但:
- 标的=TSLA(用户实际关注),数据取 1y 缓存 pkl(避免重复拉数据);
- WF 臂用**尾喂增量**(df.iloc[t-1:t],O(n)),已验与全量逐根重算等价(见 wf_dir_series);
- 两臂同一 ret_of:多头、下一bar开盘成交、窗口末强平、忽略费用(差值口径,费用两边抵消)。

预算=全序列 CL 一次算信号(=主回测口径),在窗口内按 anchor 日期执行;
WF=逐根增量,信号 (date,bs_type) 首次出现即动作(含右边缘幻影/会消失的信号)。
两者之差=预算回测因「确认滞后+幻影重绘」高估的部分。
运行: PYTHONPATH=src python scripts/wf_validate_tsla.py
"""
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import CL_CFG, collect_branch_signals
from chanlun.core.cl import CL

CACHE = "D:/chanlun_pro/chart_cache_us_tsla_1y/v33_us_TSLA_US_5m_recursivebt.pkl"
WARMUP = 400          # 暖机 bar(WF 臂从此处开始逐根)
WIN = 2600            # 验证窗口 bar(最近 ~4 个月 5m)


def _load_5m() -> pd.DataFrame:
    e = pickle.loads(Path(CACHE).read_bytes())
    d = e["data"]
    df = pd.DataFrame({
        "date": pd.to_datetime(d["t"], unit="s"),
        "open": d["o"], "high": d["h"], "low": d["l"], "close": d["c"],
        "volume": d.get("v", [0] * len(d["t"])),
    })
    return df


def ret_of(order_bars, opens, closes) -> float:
    """order_bars: 时间序 [(bar_idx,'buy'/'sell')];多头、下一bar开盘成交、末尾强平。"""
    cash, sh = 1.0, 0.0
    n = len(opens)
    for bi, act in order_bars:
        if bi + 1 >= n:
            continue
        px = opens[bi + 1]
        if act == "buy" and sh == 0.0:
            sh = cash / px
            cash = 0.0
        elif act == "sell" and sh > 0.0:
            cash = sh * px
            sh = 0.0
    if sh > 0.0:
        cash = sh * closes[-1]
    return cash - 1.0


def main() -> int:
    df = _load_5m()
    n = len(df)
    w0 = n - WIN
    if w0 < WARMUP:
        w0 = WARMUP
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    dates = list(df["date"])
    d2i = {d: i for i, d in enumerate(dates)}
    print(f"TSLA 5m bars={n} 窗口=[{w0},{n}) ({dates[w0].date()}~{dates[-1].date()})")

    # 预算臂:全序列 CL 一次,收集信号,窗口内按 anchor 日期执行
    cdf = CL("TSLA.US", "5m", dict(CL_CFG))
    cdf.process_klines(df)
    pre_bars = []
    for s in sorted(collect_branch_signals(cdf, use_xd=False), key=lambda x: x.date):
        i = d2i.get(s.date)
        if i is not None and i >= w0:
            pre_bars.append((i, "buy" if s.is_buy else "sell"))
    pre_ret = ret_of(pre_bars, opens, closes)

    # WF 臂:尾喂增量,信号首次出现即动作(含幻影)
    cd = CL("TSLA.US", "5m", dict(CL_CFG))
    cd.process_klines(df.iloc[:w0].reset_index(drop=True))
    ever = set()
    wf_bars = []
    for i in range(w0, n):
        cd.process_klines(df.iloc[i:i + 1].reset_index(drop=True))
        for s in collect_branch_signals(cd, use_xd=False):
            k = (s.date, s.bs_type)
            if k in ever:
                continue
            ever.add(k)
            wf_bars.append((i, "buy" if s.is_buy else "sell"))
    wf_ret = ret_of(wf_bars, opens, closes)

    print(f"预算(全序列信号)  = {pre_ret:+7.1%}  ({len(pre_bars)} 动作)")
    print(f"真walk-forward    = {wf_ret:+7.1%}  ({len(wf_bars)} 动作)")
    print(f"差(WF-预算)       = {wf_ret - pre_ret:+7.1%}")
    print("=> 差≈0→预算回测≈实盘可信;WF明显更低→预算因滞后/幻影乐观。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
