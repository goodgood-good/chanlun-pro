"""chanlun.recursive_bt.fetch — 用 QMT 拉前复权 K线 + 预计算缠论信号,缓存本地。

CL 重计算(慢)只在此做一次,缓存 {dates,open,close,small_by_bar,big_dir_at,limit_pct} 到 out_dir/{code}.pkl。
可断点续传(已存在跳过)。涨跌停按板块:科创688/创业300/301=20%,主板=10%。
- 默认(近1年)5m小级别+30m大级别 → bt_data:   python -m chanlun.recursive_bt.fetch 沪深300
- 日线级(2022~2024,熊市验证)1d小+1w大 → bt_data_daily: python -m chanlun.recursive_bt.fetch daily
"""
from __future__ import annotations

import os
import pickle
import sys
import time

import pandas as pd

from chanlun.recursive_bt.engine import collect_branch_signals, CL_CFG, MTFStrategy
from chanlun.core.cl import CL

OUT = "D:/chanlun_pro/bt_data"
OUT_DAILY = "D:/chanlun_pro/bt_data_daily"
OUT_MTF3 = "D:/chanlun_pro/bt_data_mtf3"


def universe(sector: str, limit=None):
    from xtquant import xtdata
    xtdata.download_sector_data()
    codes = xtdata.get_stock_list_in_sector(sector) or []
    out = [f"{c.split('.')[1]}.{c.split('.')[0]}" for c in codes]   # '600000.SH'→'SH.600000'
    out.sort()
    return out[:limit] if limit else out


def limit_pct(code: str) -> float:
    num = code.split(".")[1]
    if num.startswith("688") or num.startswith("300") or num.startswith("301"):
        return 0.20
    return 0.10


def _slice(df, start, end):
    if df is None or len(df) == 0:
        return df
    if start:
        df = df[df["date"] >= pd.Timestamp(start, tz="Asia/Shanghai")]
    if end:
        df = df[df["date"] <= pd.Timestamp(end, tz="Asia/Shanghai") + pd.Timedelta("1D")]
    return df.reset_index(drop=True)


def _sig(code, ex, tf, start, end, min_bars=30):
    df = _slice(ex.klines(code, tf, start_date=start), start, end)
    if df is None or len(df) < min_bars:
        return df, []
    cd = CL(code, tf, dict(CL_CFG))
    cd.process_klines(df)
    return df, collect_branch_signals(cd, use_xd=False)


def build(code, ex, small_tf="5m", big_tf="30m", start=None, end=None,
          big_delay=None, min_small=200, mid_tf=None, mid_delay=None) -> dict | None:
    dfs, small = _sig(code, ex, small_tf, start, end, min_small)
    if dfs is None or len(dfs) < min_small:
        return None
    _, big = _sig(code, ex, big_tf, start, end)
    dates = list(dfs["date"])
    strat = MTFStrategy(small, big, dates, "5m+30m", gate="not_down", big_delay=big_delay)
    out = {
        "code": code, "dates": dates,
        "open": dfs["open"].to_numpy(), "close": dfs["close"].to_numpy(),
        "small_by_bar": strat.small_by_bar, "big_dir_at": strat.big_dir_at,
        "limit_pct": limit_pct(code), "n_small": len(small), "n_big": len(big),
    }
    if mid_tf:                       # 三级联立:中级别(如5m)方向门控(1m入场+5m中+30m大)
        _, mid = _sig(code, ex, mid_tf, start, end)
        sm = MTFStrategy(small, mid, dates, "5m+30m", gate="not_down", big_delay=mid_delay)
        out["mid_dir_at"] = sm.big_dir_at
        out["n_mid"] = len(mid)
    return out


def run(codes, out_dir, small_tf, big_tf, start, end, big_delay, min_small=200,
        mid_tf=None, mid_delay=None):
    os.makedirs(out_dir, exist_ok=True)
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    ex = ExchangeQMT()
    ok = skip = fail = 0
    t0 = time.time()
    lv = f"{small_tf}+{mid_tf}+{big_tf}" if mid_tf else f"{small_tf}+{big_tf}"
    print(f"取数 {len(codes)}只 {lv} {start}~{end} → {out_dir}")
    for i, code in enumerate(codes):
        p = f"{out_dir}/{code}.pkl"
        if os.path.exists(p):
            skip += 1
            continue
        try:
            d = build(code, ex, small_tf, big_tf, start, end, big_delay, min_small,
                      mid_tf=mid_tf, mid_delay=mid_delay)
            if d:
                pickle.dump(d, open(p, "wb"))
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  {code} 失败: {type(e).__name__} {e}")
        if (i + 1) % 30 == 0:
            print(f"  {i+1}/{len(codes)} ok={ok} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} skip={skip} fail={fail} 共{len(codes)} {time.time()-t0:.0f}s")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        # 熊市验证:沪深300 + 上证指数,日线小级别 + 周线大级别,2022-2024
        codes = universe("沪深300") + ["SH.000001"]
        run(codes, OUT_DAILY, "d", "w", "2022-01-01", "2024-12-31",
            big_delay=pd.Timedelta("7D"), min_small=120)
    elif len(sys.argv) > 1 and sys.argv[1] == "mtf3":
        # 30m+5m+1m 三级联立:1m入场(小)+5m中+30m大,近1年(分钟数据仅~1年)
        sector = sys.argv[2] if len(sys.argv) > 2 else "上证50"
        codes = universe(sector) + ["SH.000001"]
        run(codes, OUT_MTF3, "1m", "30m", None, None, big_delay=pd.Timedelta("30min"),
            min_small=500, mid_tf="5m", mid_delay=pd.Timedelta("5min"))
    else:
        sector = sys.argv[1] if len(sys.argv) > 1 else "上证50"
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run(universe(sector, limit), OUT, "5m", "30m", None, None,
            big_delay=pd.Timedelta("30min"))


if __name__ == "__main__":
    main()
