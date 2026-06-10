"""scripts/qmt_fetch.py — 用 QMT 拉真实数据(前复权)+预计算缠论信号,缓存本地。

防 QMT 多天劣化崩溃 + 让选股回测可快速迭代:CL 重计算(慢)只在此做一次,缓存
{dates,open,close,small_by_bar,big_dir_at,limit_pct} 到 D:/chanlun_pro/bt_data/{code}.pkl。
可断点续传(已存在的跳过)。涨跌停按板块:科创688/创业300/301=20%,主板=10%。
运行: PYTHONPATH="src;web/chanlun_chart;." python scripts/qmt_fetch.py 沪深300 [limit]
"""
from __future__ import annotations

import os
import pickle
import sys
import time

sys.path.insert(0, "scripts")
from recursive_backtest import collect_branch_signals, CL_CFG, MTFStrategy  # noqa: E402
from chanlun.core.cl import CL  # noqa: E402

OUT = "D:/chanlun_pro/bt_data"


def universe(sector: str, limit=None):
    from xtquant import xtdata
    xtdata.download_sector_data()
    codes = xtdata.get_stock_list_in_sector(sector) or []
    out = []
    for c in codes:                       # '600000.SH' → 'SH.600000'
        num, mkt = c.split(".")
        out.append(f"{mkt}.{num}")
    out.sort()
    return out[:limit] if limit else out


def limit_pct(code: str) -> float:
    num = code.split(".")[1]
    if num.startswith("688") or num.startswith("300") or num.startswith("301"):
        return 0.20
    return 0.10


def build(code: str, ex) -> dict | None:
    df5 = ex.klines(code, "5m")
    df30 = ex.klines(code, "30m")
    if df5 is None or len(df5) < 200:
        return None
    cd5 = CL(code, "5m", dict(CL_CFG))
    cd5.process_klines(df5)
    small = collect_branch_signals(cd5, use_xd=False)
    big = []
    if df30 is not None and len(df30) >= 50:
        cd30 = CL(code, "30m", dict(CL_CFG))
        cd30.process_klines(df30)
        big = collect_branch_signals(cd30, use_xd=False)
    dates = list(df5["date"])
    strat = MTFStrategy(small, big, dates, "5m+30m", gate="not_down")
    return {
        "code": code, "dates": dates,
        "open": df5["open"].to_numpy(), "close": df5["close"].to_numpy(),
        "small_by_bar": strat.small_by_bar, "big_dir_at": strat.big_dir_at,
        "limit_pct": limit_pct(code),
        "n_small": len(small), "n_big": len(big),
    }


def main():
    sector = sys.argv[1] if len(sys.argv) > 1 else "上证50"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    os.makedirs(OUT, exist_ok=True)
    codes = universe(sector, limit)
    print(f"universe={sector} 共{len(codes)}只 → {OUT}")
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    ex = ExchangeQMT()
    ok = skip = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        p = f"{OUT}/{code}.pkl"
        if os.path.exists(p):
            skip += 1
            continue
        try:
            d = build(code, ex)
            if d:
                pickle.dump(d, open(p, "wb"))
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  {code} 失败: {type(e).__name__} {e}")
        if (i + 1) % 20 == 0:
            print(f"  进度 {i+1}/{len(codes)} ok={ok} skip={skip} fail={fail} "
                  f"用时{time.time()-t0:.0f}s")
    print(f"完成: ok={ok} skip={skip} fail={fail} 共{len(codes)} 用时{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
