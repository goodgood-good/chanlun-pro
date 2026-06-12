# -*- coding: utf-8 -*-
"""TSLA 缠论原生程式真·wf 对比:30m 笔方向跟随 / 38课同级别分解程式(30m、5m)。

与确认层体系互补的「原文正统」路线——全部逐根增量、无未来函数:
- dir30: wf_dir_series(30m) 当下笔方向,up 进 / down 出(neutral 持有不动);
- seg38_30m: 38课程式(L24751)工程版 wf_seg38_series(30m):向下段不破前低→买,
  向上段不创新高→卖(背驰提前卖以翻转确认近似);
- seg38_5m: 同程式跑 5m。
执行模型与确认层网格一致:事件=bar收盘确认 → 下一根 5m 开盘成交,全仓,T+0,费用 0.0001。
运行: PYTHONPATH=src python scripts/wf_chanlun_native_tsla.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import wf_dir_series, wf_seg38_series, US_STOCK

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wf_backtest_tsla import _load  # noqa: E402

COMMISSION = US_STOCK.commission
WARMUP = 400
EV_PKL = "D:/chanlun_pro/reports/wf_native_tsla_events.pkl"
CODE = "TSLA.US"

# --dir/--prefix/--tag 参数化(第二标的,如 QQQ)
import wf_backtest_tsla as _wbt  # noqa: E402
for _i, _a in enumerate(list(sys.argv)):
    if _a == "--dir" and _i + 1 < len(sys.argv):
        _wbt.DIR = sys.argv[_i + 1]
    elif _a == "--prefix" and _i + 1 < len(sys.argv):
        _wbt.PREFIX = sys.argv[_i + 1]
        CODE = sys.argv[_i + 1].replace("us_", "").replace("_US", "") + ".US"
    elif _a == "--tag" and _i + 1 < len(sys.argv):
        EV_PKL = f"D:/chanlun_pro/reports/wf_native_{sys.argv[_i + 1]}_events.pkl"


def exec_events(events, df5, label):
    """events: [(时刻, 'buy'|'sell')](bar收盘确认)→映射到 5m bar→下一根开盘成交。"""
    dates = list(df5["date"])
    opens = df5["open"].to_numpy()
    closes = df5["close"].to_numpy()
    d2i = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    # 事件时刻→该时刻或之后第一根 5m bar(30m 事件时刻=30m bar 收盘,对齐 5m 索引)
    acts = []
    sorted_d = dates
    import bisect
    for t, act in events:
        i = d2i.get(t)
        if i is None:
            i = bisect.bisect_left(sorted_d, t)
            if i >= n:
                continue
        acts.append((i, act))
    acts.sort()
    cash, sh, entry = 1.0, 0.0, 0.0
    peak, mdd = 1.0, 0.0
    trades, wins = 0, 0
    pend = None
    ai = 0
    for i in range(n):
        if pend is not None:
            px = opens[i]
            if pend == "buy" and sh == 0.0 and px > 0:
                sh = cash * 0.99 / (px * (1 + COMMISSION))
                cash -= sh * px * (1 + COMMISSION)
                entry = px
            elif pend == "sell" and sh > 0.0 and px > 0:
                cash += sh * px * (1 - COMMISSION)
                trades += 1
                wins += px > entry
                sh = 0.0
            pend = None
        while ai < len(acts) and acts[ai][0] == i:
            pend = acts[ai][1]
            ai += 1
        eq = cash + sh * closes[i]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    if sh > 0.0:
        cash += sh * closes[-1] * (1 - COMMISSION)
        trades += 1
        wins += closes[-1] > entry
    print(f"{label:>12}: 收益={cash - 1:+7.1%} 回撤={mdd:6.1%} 笔={trades:3d} "
          f"胜率={wins / trades * 100 if trades else 0:3.0f}%")
    return cash - 1, mdd


def exec_events_gated(events, df5, gate, label, gate_mode="not_down"):
    """同 exec_events 但叠 30m 门控:down 强平+禁买;up 模式仅 up 窗口可买。"""
    dates = list(df5["date"])
    opens = df5["open"].to_numpy()
    closes = df5["close"].to_numpy()
    d2i = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    import bisect
    acts = []
    for t, act in events:
        i = d2i.get(t)
        if i is None:
            i = bisect.bisect_left(dates, t)
            if i >= n:
                continue
        acts.append((i, act))
    acts.sort()
    cash, sh, entry = 1.0, 0.0, 0.0
    peak, mdd = 1.0, 0.0
    trades, wins = 0, 0
    pend = None
    ai = 0
    for i in range(n):
        if pend is not None:
            px = opens[i]
            if pend == "buy" and sh == 0.0 and px > 0:
                sh = cash * 0.99 / (px * (1 + COMMISSION))
                cash -= sh * px * (1 + COMMISSION)
                entry = px
            elif pend == "sell" and sh > 0.0 and px > 0:
                cash += sh * px * (1 - COMMISSION)
                trades += 1
                wins += px > entry
                sh = 0.0
            pend = None
        gate_down = gate[i] == "down"
        can_buy = (gate[i] == "up") if gate_mode == "up" else (not gate_down)
        if sh > 0.0 and gate_down:
            pend = "sell"
        while ai < len(acts) and acts[ai][0] == i:
            a = acts[ai][1]
            ai += 1
            if a == "sell" and sh > 0.0:
                pend = "sell"
            elif a == "buy" and sh == 0.0 and can_buy and pend is None:
                pend = "buy"
        eq = cash + sh * closes[i]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    if sh > 0.0:
        cash += sh * closes[-1] * (1 - COMMISSION)
        trades += 1
        wins += closes[-1] > entry
    print(f"{label:>22}: 收益={cash - 1:+7.1%} 回撤={mdd:6.1%} 笔={trades:3d} "
          f"胜率={wins / trades * 100 if trades else 0:3.0f}%")


def _map_events_to_bars(events, dates):
    d2i = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    acts = []
    import bisect
    for t, act in events:
        i = d2i.get(t)
        if i is None:
            i = bisect.bisect_left(dates, t)
            if i >= n:
                continue
        acts.append((i, act))
    acts.sort()
    return acts


def exec_events_sleeves(events, df5, label, core_fraction=0.0, active_fraction=1.0, verbose=True):
    """Replay independent capital sleeves: passive core, active 38-course sleeve, idle cash."""
    if core_fraction < 0 or active_fraction < 0 or core_fraction + active_fraction > 1.0000001:
        raise ValueError("core_fraction + active_fraction must be in [0, 1]")
    dates = list(df5["date"])
    opens = df5["open"].to_numpy()
    closes = df5["close"].to_numpy()
    acts = _map_events_to_bars(events, dates)

    idle_cash = 1.0 - core_fraction - active_fraction
    core_cash, core_sh = core_fraction, 0.0
    active_cash, active_sh, active_entry = active_fraction, 0.0, 0.0
    if core_cash > 0.0 and len(opens) > 0 and opens[0] > 0:
        core_sh = core_cash / (opens[0] * (1 + COMMISSION))
        core_cash = 0.0

    peak, mdd = 1.0, 0.0
    trades, wins = 0, 0
    pend = None
    ai = 0
    for i in range(len(dates)):
        if pend is not None:
            px = opens[i]
            if pend == "buy" and active_sh == 0.0 and active_cash > 0.0 and px > 0:
                active_sh = active_cash * 0.99 / (px * (1 + COMMISSION))
                active_cash -= active_sh * px * (1 + COMMISSION)
                active_entry = px
            elif pend == "sell" and active_sh > 0.0 and px > 0:
                active_cash += active_sh * px * (1 - COMMISSION)
                trades += 1
                wins += px > active_entry
                active_sh = 0.0
            pend = None
        while ai < len(acts) and acts[ai][0] == i:
            pend = acts[ai][1]
            ai += 1
        eq = idle_cash + core_cash + core_sh * closes[i] + active_cash + active_sh * closes[i]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)

    final_eq = idle_cash + core_cash + active_cash
    if core_sh > 0.0:
        final_eq += core_sh * closes[-1] * (1 - COMMISSION)
    if active_sh > 0.0:
        final_eq += active_sh * closes[-1] * (1 - COMMISSION)
        trades += 1
        wins += closes[-1] > active_entry
    result = {
        "label": label,
        "core_fraction": round(core_fraction, 4),
        "active_fraction": round(active_fraction, 4),
        "idle_fraction": round(idle_cash, 4),
        "ret": final_eq - 1.0,
        "max_dd": mdd,
        "trades": trades,
        "win_rate": (wins / trades) if trades else 0.0,
        "score_ret_minus_2dd": (final_eq - 1.0) - 2 * mdd,
    }
    if verbose:
        print(f"{label:>22}: core={core_fraction:4.0%} active={active_fraction:4.0%} "
              f"ret={result['ret']:+7.1%} dd={mdd:6.1%} trades={trades:3d} "
              f"win={result['win_rate'] * 100:3.0f}%")
    return result


def scan_sleeve_grid(events, df5):
    core_fracs = (0.0, 0.25, 0.33, 0.50, 0.67, 0.75, 1.0)
    active_fracs = (0.0, 0.25, 0.33, 0.50, 0.67, 0.75, 1.0)
    rows = []
    for core in core_fracs:
        for active in active_fracs:
            if core == 0.0 and active == 0.0:
                continue
            if core + active > 1.0000001:
                continue
            label = f"sleeve c{core:.2f} a{active:.2f}"
            rows.append(exec_events_sleeves(events, df5, label, core, active, verbose=False))
    rows.sort(key=lambda r: (r["score_ret_minus_2dd"], r["ret"]), reverse=True)
    return rows


def main() -> int:
    import pickle as _p
    from wf_backtest_tsla import _gate_by_bar
    df5, df30 = _load("5m"), _load("30m")
    bh = df5["close"].iloc[-1] / df5["open"].iloc[WARMUP] - 1
    print(f"TSLA {df5['date'].iloc[0].date()}~{df5['date'].iloc[-1].date()} 裸持={bh:+.1%}")
    ev_pkl = Path(EV_PKL)
    if ev_pkl.exists():
        cached = _p.loads(ev_pkl.read_bytes())
        dir_ev, ev30, ev5 = cached["dir30"], cached["seg38_30m"], cached["seg38_5m"]
    else:
        dir_ev = wf_dir_series(df30, CODE, "30m")
        ev30 = wf_seg38_series(df30, CODE, "30m")
        ev5 = wf_seg38_series(df5, CODE, "5m")
        ev_pkl.write_bytes(_p.dumps({"dir30": dir_ev, "seg38_30m": ev30, "seg38_5m": ev5}))

    # 1) 30m 笔方向跟随
    ev = [(t, "buy" if d == "up" else "sell") for t, d in dir_ev if d in ("up", "down")]
    exec_events(ev, df5, "dir30")
    # 2) 38课程式 30m / 5m(裸)
    exec_events(ev30, df5, "seg38_30m")
    exec_events(ev5, df5, "seg38_5m")
    # 3) 38课程式 5m + 30m 门控(原文:大级别定方向,小级别程式操作)
    gate = _gate_by_bar(df5, df30)
    exec_events_gated(ev5, df5, gate, "seg38_5m+gate(not_down)", "not_down")
    exec_events_gated(ev5, df5, gate, "seg38_5m+gate(up)", "up")
    exec_events_gated(ev30, df5, gate, "seg38_30m+gate(not_down)", "not_down")
    if "--sleeve-grid" in sys.argv:
        import json
        rows = scan_sleeve_grid(ev5, df5)
        out = Path("D:/chanlun_pro/reports/wf_native_tsla_sleeve_grid.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nsleeve grid top by ret-2*dd:")
        for row in rows[:10]:
            print(f"  core={row['core_fraction']:4.0%} active={row['active_fraction']:4.0%} "
                  f"idle={row['idle_fraction']:4.0%} ret={row['ret']:+7.1%} "
                  f"dd={row['max_dd']:6.1%} score={row['score_ret_minus_2dd']:+7.1%} "
                  f"trades={row['trades']:3d}")
        print(f"sleeve grid report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
