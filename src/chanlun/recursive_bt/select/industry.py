"""chanlun.recursive_bt.select.industry — 基本面行业结构层:按行业龙头+成长性构建季度选股池。

策略:选行业→选企业(龙头70% + 成长性30%),每季度首个交易日重算,行业地位无变化则不换股。

工程化(point-in-time):
- 行业=GICS1 一级行业(QMT,11个)∩ 回测池(沪深300);行业映射缓存 D:/chanlun_pro/industry_map.pkl。
- 每季度首个交易日重算:龙头=行业内市值最大(close×总股本,总股本用当前值近似——送转致
  绝对值失真但行业内排名大体稳);成长=行业内营收/净利同比增速最高(只用已公告报告,
  公告次日生效,无lookahead)。
- 输出池调度 [(生效日, {code: weight})]:龙头组共70%、成长组共30%,组内等权。
运行: python -m chanlun.recursive_bt.select.industry  (构建并打印各季度池子供肉眼核验)
"""
from __future__ import annotations

import os
import pickle
from typing import Dict, List, Tuple

import pandas as pd

IND_MAP = "D:/chanlun_pro/industry_map.pkl"
FUND_DIR = "D:/chanlun_pro/bt_data_fund"


def build_industry_map(pool: List[str]) -> Dict[str, str]:
    """code('SH.600519') → GICS1 一级行业名(动态取 QMT 板块表,11个)。缓存;QMT 不可用时读缓存。"""
    if os.path.exists(IND_MAP):
        m = pickle.load(open(IND_MAP, "rb"))
        if set(pool) & set(m):
            return m
    from xtquant import xtdata
    xtdata.download_sector_data()
    gics1 = [s for s in (xtdata.get_sector_list() or []) if s.startswith("GICS1")]
    m: Dict[str, str] = {}
    for g in gics1:
        for c in (xtdata.get_stock_list_in_sector(g) or []):
            if "." not in c:
                continue
            num, mkt = c.split(".")
            if mkt not in ("SH", "SZ"):
                continue
            m[f"{mkt}.{num}"] = g
    m = {c: g for c, g in m.items() if c in set(pool)}
    pickle.dump(m, open(IND_MAP, "wb"))
    return m


def _load_fund(code: str):
    p = f"{FUND_DIR}/{code}.pkl"
    if not os.path.exists(p):
        return [], None
    d = pickle.load(open(p, "rb"))
    return d.get("reports", []), d.get("total_share")


def _pit_growth(reports, asof) -> float:
    """截至 asof(公告次日生效)的最新报告成长分=max(营收同比,净利同比)。无报告→-inf。"""
    best = None
    for r in reports:
        ann = r.get("anntime") or ""
        if not ann:
            continue
        if pd.Timestamp(ann, tz="Asia/Shanghai") + pd.Timedelta("1D") <= asof:
            best = r
        else:
            break
    if best is None:
        return float("-inf")
    g = max(best["rev_inc"] if best["rev_inc"] is not None else float("-inf"),
            best["np_inc"] if best["np_inc"] is not None else float("-inf"))
    return g


def build_pool_schedule(syms: dict, n_leader: int = 1, n_growth: int = 1,
                        w_leader: float = 0.70) -> List[Tuple[pd.Timestamp, Dict[str, float]]]:
    """季度池调度。syms=回测数据(dates/close);每季度首交易日:每行业取市值Top n_leader(龙头组)
    + 成长Top n_growth(成长组,剔除已选龙头)。返回 [(生效时刻, {code: weight})] 升序。"""
    ind = build_industry_map(list(syms))
    funds = {c: _load_fund(c) for c in syms}
    # 季度首交易日(用全池日期并集)
    all_dates = sorted(set().union(*[set(s["dates"]) for s in syms.values()]))
    qheads: List[pd.Timestamp] = []
    seen_q = set()
    for t in all_dates:
        q = (t.year, (t.month - 1) // 3)
        if q not in seen_q:
            seen_q.add(q)
            qheads.append(t)
    sched = []
    for qt in qheads:
        # point-in-time 市值与成长
        rows = []
        for c, s in syms.items():
            g = ind.get(c)
            if g is None:
                continue
            d2i = s["d2i"]
            if qt not in d2i:
                idx = None          # 该日停牌 → 用之前最近 bar
                for tt in reversed(s["dates"]):
                    if tt <= qt:
                        idx = d2i[tt]
                        break
                if idx is None:
                    continue
            else:
                idx = d2i[qt]
            px = float(s["close"][idx])
            reports, share = funds.get(c, ([], None))
            mcap = px * share if share else 0.0
            rows.append((c, g, mcap, _pit_growth(reports, qt)))
        pool: Dict[str, float] = {}
        by_g: Dict[str, list] = {}
        for r in rows:
            by_g.setdefault(r[1], []).append(r)
        leaders, growths = [], []
        for g, rs in by_g.items():
            rs_m = sorted(rs, key=lambda x: -x[2])
            lead = [r[0] for r in rs_m[:n_leader] if r[2] > 0]
            leaders += lead
            rs_g = sorted((r for r in rs if r[0] not in lead), key=lambda x: -x[3])
            growths += [r[0] for r in rs_g[:n_growth] if r[3] != float("-inf")]
        for c in leaders:
            pool[c] = w_leader / max(len(leaders), 1)
        for c in growths:
            pool[c] = (1 - w_leader) / max(len(growths), 1)
        if pool:
            sched.append((qt, pool))
    return sched


def main():
    from chanlun.recursive_bt.sim.portfolio import _load_bt_universe
    syms = _load_bt_universe()
    sched = build_pool_schedule(syms)
    ind = build_industry_map(list(syms))
    print(f"行业映射: {len(ind)} 只入 {len(set(ind.values()))} 行业; 季度池 {len(sched)} 期")
    for qt, pool in sched:
        ls = sorted(pool.items(), key=lambda kv: -kv[1])
        print(f"  {qt.date()} 池{len(pool)}只: " + " ".join(f"{c}({w:.0%})" for c, w in ls[:14]))


if __name__ == "__main__":
    main()
