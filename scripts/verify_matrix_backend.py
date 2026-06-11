# -*- coding: utf-8 -*-
"""三周期矩阵后端验证：1m(笔+L0+5m+30m)/5m(笔+L0+30m)/30m(笔+L0) 中枢/买卖点/背驰。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from chanlun.exchange.exchange_qmt import ExchangeQMT
from chanlun.core.cl import CL as CoreCL

CFG = {
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}


def main():
    ex = ExchangeQMT()
    plans = [("1m", "2026-04-20"), ("5m", "2025-12-01"), ("30m", "2025-06-03")]
    if len(sys.argv) > 2:                       # verify_matrix_backend.py 1m 2026-01-05
        plans = [(sys.argv[1], sys.argv[2])]
    for freq, sd in plans:
        df = ex.klines("SH.000001", freq, start_date=sd, end_date="2026-06-11")
        cd = CoreCL("SH.000001", freq, dict(CFG))
        cd.process_klines(df)
        levels = cd.get_kuozhan_levels()
        bis, xds = cd.get_bis(), cd.get_xds()
        lv0 = cd.get_recursive_branch_levels()[0]
        l0bsp = cd.get_branch_bspoints(use_xd=True)
        l0bcs = cd.get_branch_bcs(use_xd=True)
        print(f"== {freq} 图({len(df)}根) 笔={len(bis)} 线段={len(xds)} "
              f"L0中枢={len(lv0.zss)} L0买卖点={len(l0bsp)} L0背驰={len(l0bcs)} ==")
        for lv in levels:
            heads = [(round(z.zd, 1), round(z.zg, 1), z.done) for z in lv["zss"]][:4]
            print(f"   L{lv['level']}: 中枢={len(lv['zss'])} 买卖点={len(lv['bsp'])} "
                  f"背驰={len(lv['bcs'])} {heads}")
            for p in lv["bsp"][:6]:
                print(f"      bsp {p.bs_type} @ {p.anchor_fx.k.date} {p.anchor_fx.val}")


if __name__ == "__main__":
    main()
