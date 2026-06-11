# -*- coding: utf-8 -*-
"""信号基线 dump/diff——动 zslx_branch 等已验证体系依赖前的安全网。

口径(同 2026-06-10 v33 全链收口审计): 10 只标的 5m
  - L0 交易信号: get_branch_bspoints(use_xd=True) (mmd,date,val)
  - kuozhan/tongjibie 各级: 中枢数量 + 买卖点(name,date,val) + 背驰数
用法:
  python scripts/signal_baseline_diff.py dump <out.pkl>   # 当前代码跑信号
  python scripts/signal_baseline_diff.py diff <a.pkl> <b.pkl>
"""
import pickle
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

CODES = [
    "SH.000001", "SZ.399001", "SH.510300", "SH.510500", "SZ.000001",
    "SH.600519", "SZ.300750", "SH.601318", "SZ.000858", "SH.600036",
]
START, END = "2025-12-01", "2026-06-11"
CFG = {
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}


def _sig_one(code: str) -> dict:
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    from chanlun.core.cl import CL

    df = ExchangeQMT().klines(code, "5m", start_date=START, end_date=END)
    cd = CL(code, "5m", dict(CFG))
    cd.process_klines(df)
    out = {"rows": len(df)}
    bsp = cd.get_branch_bspoints(use_xd=True)
    out["l0_bsp"] = sorted(
        (p.bs_type, str(p.anchor_fx.k.date), round(p.anchor_fx.val, 3)) for p in bsp)
    levels = cd.get_kuozhan_levels()
    for lv in levels:
        k = f"lv{lv['level']}"
        out[k + "_zss"] = [(round(z.zd, 2), round(z.zg, 2), bool(z.done)) for z in lv["zss"]]
        out[k + "_bsp"] = sorted(
            (p.bs_type, str(p.anchor_fx.k.date), round(p.anchor_fx.val, 3))
            for p in lv["bsp"])
        out[k + "_bcs"] = sorted((str(d), round(v, 3), kind) for d, v, kind in lv["bcs"])
    return out


def dump(out_path: str):
    res = {}
    for c in CODES:
        try:
            res[c] = _sig_one(c)
            print(f"{c}: rows={res[c]['rows']} l0_bsp={len(res[c]['l0_bsp'])}")
        except Exception as e:  # noqa: BLE001
            res[c] = {"error": repr(e)}
            print(f"{c}: ERROR {e!r}")
    Path(out_path).write_bytes(pickle.dumps(res))
    print(f"saved -> {out_path}")


def diff(a_path: str, b_path: str):
    a = pickle.loads(Path(a_path).read_bytes())
    b = pickle.loads(Path(b_path).read_bytes())
    total_changes = 0
    for c in sorted(set(a) | set(b)):
        da, db = a.get(c, {}), b.get(c, {})
        keys = sorted(set(da) | set(db))
        for k in keys:
            va, vb = da.get(k), db.get(k)
            if va == vb:
                continue
            total_changes += 1
            if isinstance(va, list) and isinstance(vb, list):
                sa, sb = set(map(tuple, va)) if va and isinstance(va[0], (list, tuple)) else set(va), \
                         set(map(tuple, vb)) if vb and isinstance(vb[0], (list, tuple)) else set(vb)
                gone, new = sa - sb, sb - sa
                print(f"{c}.{k}: {len(va)}->{len(vb)}  -{len(gone)} +{len(new)}")
                for g in sorted(gone)[:5]:
                    print(f"    - {g}")
                for n in sorted(new)[:5]:
                    print(f"    + {n}")
            else:
                print(f"{c}.{k}: {va} -> {vb}")
    print(f"\nchanged keys: {total_changes}")


if __name__ == "__main__":
    if sys.argv[1] == "dump":
        dump(sys.argv[2])
    elif sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
