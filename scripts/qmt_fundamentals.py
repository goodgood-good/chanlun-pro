"""scripts/qmt_fundamentals.py — 抓基本面+市值(QMT),供「三个独立系统」之①基本面、②比价。

缠论原文(line38524-38539):①基本面=行业地位(龙头)+质量(ROE/毛利)+成长(营收/净利增长);
②比价=市值与行业地位关系、低估(PB/PE)。本模块抓 PershareIndex(每股指标,**带 m_anntime 公告日
→ point-in-time 防 lookahead**) + 总股本(市值=总股本×价),缓存到 bt_data_fund/{code}.pkl。
运行: PYTHONPATH="src;web/chanlun_chart;." python scripts/qmt_fundamentals.py [沪深300]
"""
from __future__ import annotations

import glob
import os
import pickle
import sys
import time

OUT = "D:/chanlun_pro/bt_data_fund"


def to_qmt(code: str) -> str:        # 'SH.600519' → '600519.SH'
    mkt, num = code.split(".")
    return f"{num}.{mkt}"


def fetch(codes):
    from xtquant import xtdata
    os.makedirs(OUT, exist_ok=True)
    qcodes = [to_qmt(c) for c in codes]
    print(f"下载 {len(codes)} 只财务(PershareIndex)...")
    xtdata.download_financial_data(qcodes, ["PershareIndex"])
    print("下载完成,逐只抽取+缓存")
    ok = fail = 0
    t0 = time.time()
    for i, (code, qc) in enumerate(zip(codes, qcodes)):
        p = f"{OUT}/{code}.pkl"
        if os.path.exists(p):
            ok += 1
            continue
        try:
            fd = xtdata.get_financial_data([qc], ["PershareIndex"],
                                           "20230101", "20260601", report_type="report_time")
            pi = fd.get(qc, {}).get("PershareIndex")
            det = xtdata.get_instrument_detail(qc)
            reports = []
            if pi is not None and len(pi):
                for _, r in pi.iterrows():
                    ann = str(r.get("m_anntime") or "")
                    if not ann or ann == "nan":
                        continue
                    reports.append({
                        "anntime": ann,                          # 公告日 YYYYMMDD
                        "period": str(r.get("m_timetag") or ""),
                        "roe": _f(r.get("du_return_on_equity")),   # 单季ROE %
                        "gross": _f(r.get("sales_gross_profit")),  # 毛利率 %
                        "rev_inc": _f(r.get("inc_revenue_rate")),  # 营收同比增 %
                        "np_inc": _f(r.get("inc_net_profit_rate")),  # 净利同比增 %
                        "bps": _f(r.get("s_fa_bps")),              # 每股净资产
                        "eps": _f(r.get("s_fa_eps_basic")),        # 每股收益(单期)
                    })
            reports.sort(key=lambda x: x["anntime"])
            d = {"code": code, "reports": reports,
                 "total_share": _f(det.get("TotalVolume")) if det else None}
            pickle.dump(d, open(p, "wb"))
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  {code} 失败 {type(e).__name__} {e}")
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(codes)} ok={ok} fail={fail} {time.time()-t0:.0f}s")
    print(f"完成 ok={ok} fail={fail} {time.time()-t0:.0f}s → {OUT}")


def _f(v):
    try:
        f = float(v)
        return f if f == f else None      # NaN → None
    except (TypeError, ValueError):
        return None


def universe(sector):
    from xtquant import xtdata
    xtdata.download_sector_data()
    codes = xtdata.get_stock_list_in_sector(sector) or []
    return sorted(f"{c.split('.')[1]}.{c.split('.')[0]}" for c in codes)


def main():
    sector = sys.argv[1] if len(sys.argv) > 1 else None
    if sector:
        codes = universe(sector)
    else:   # 默认:已有 K线缓存的标的(bt_data)
        codes = sorted(os.path.basename(f)[:-4]
                       for f in glob.glob("D:/chanlun_pro/bt_data/*.pkl"))
    fetch(codes)


if __name__ == "__main__":
    main()
