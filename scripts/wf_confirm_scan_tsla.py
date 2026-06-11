# -*- coding: utf-8 -*-
"""TSLA 真·walk-forward + 信号确认层扫描(绝对无未来函数)。

第68轮(spec§79)结论:预算信号回测含未来函数(右边缘幻影),真wf下 TSLA 一年仅 +1.4%/DD19.1%。
本轮实验**确认层**:买/卖点信号首次出现后,须连续存活 N 根 bar 仍未被重绘掉才动作——
用确认滞后换幻影。N=0=见信号即动(上轮 WF 臂);N→∞=只剩最终稳定信号(≈预算口径)。

两阶段:
1) 逐根尾喂(一次,~35min):每根 bar 收盘重算 collect_branch_signals,记录每个信号
   (date,bs_type) 的生命周期 episode [first_seen, alive_until];中途消失再出现=新 episode
   (实盘语义:重新开始确认计数)。同时存 gate(wf_dir_series,无 lookahead)与 OHLC。
   阶段1产物落盘 pickle,后续可重 replay 不再重算。
2) 离线 replay:N_buy×N_sell 网格,episode 在 first_seen+N 根时仍存活→该 bar 收盘触发→
   下一根开盘成交;执行模型与 wf_backtest_tsla 一致(比例/门控down强平/T+0/费用)。

运行: PYTHONPATH=src python scripts/wf_confirm_scan_tsla.py [--replay-only]
"""
import json
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
from chanlun.recursive_bt.engine import CL_CFG, collect_branch_signals, buy_class
from chanlun.core.cl import CL

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wf_backtest_tsla import _load, _gate_by_bar, _run_exec, _pick_buy  # noqa: E402

WARMUP = 400
STAGE1_PKL = "D:/chanlun_pro/reports/wf_confirm_tsla_stage1.pkl"
OUT_JSON = "D:/chanlun_pro/reports/wf_confirm_tsla_scan.json"
N_BUYS = (0, 1, 2, 3, 4, 6, 8, 12)
N_SELLS = (0, 1, 2)

# --dir/--prefix/--tag 参数化(第二标的验证,如 QQQ):覆盖缓存目录/文件前缀/输出名
import wf_backtest_tsla as _wbt  # noqa: E402
for _i, _a in enumerate(list(sys.argv)):
    if _a == "--dir" and _i + 1 < len(sys.argv):
        _wbt.DIR = sys.argv[_i + 1]
    elif _a == "--prefix" and _i + 1 < len(sys.argv):
        _wbt.PREFIX = sys.argv[_i + 1]
    elif _a == "--tag" and _i + 1 < len(sys.argv):
        _t = sys.argv[_i + 1]
        STAGE1_PKL = f"D:/chanlun_pro/reports/wf_confirm_{_t}_stage1.pkl"
        OUT_JSON = f"D:/chanlun_pro/reports/wf_confirm_{_t}_scan.json"


def stage1(limit_bars: int = 0) -> dict:
    """逐根尾喂,记录信号生命周期 episodes + gate + OHLC。"""
    df5, df30 = _load("5m"), _load("30m")
    if limit_bars:
        df5 = df5.iloc[:limit_bars].reset_index(drop=True)
        last5 = df5["date"].iloc[-1]
        df30 = df30[df30["date"] <= last5].reset_index(drop=True)
    n = len(df5)
    gate = _gate_by_bar(df5, df30)
    cd = CL("TSLA.US", "5m", dict(CL_CFG))
    cd.process_klines(df5.iloc[:WARMUP].reset_index(drop=True))
    # episodes: list of dict(key, side, bs_type, first_seen, alive_until)
    episodes: list[dict] = []
    open_ep: dict[tuple, int] = {}        # key -> index into episodes(当前活跃 episode)
    for i in range(WARMUP, n):
        cd.process_klines(df5.iloc[i:i + 1].reset_index(drop=True))
        cur_keys = set()
        for s in collect_branch_signals(cd, use_xd=False):
            k = (str(s.date), s.bs_type)
            cur_keys.add(k)
            ep_i = open_ep.get(k)
            if ep_i is not None and episodes[ep_i]["alive_until"] == i - 1:
                episodes[ep_i]["alive_until"] = i          # 连续存活
            else:
                open_ep[k] = len(episodes)                  # 新信号或复活→新 episode
                episodes.append({
                    "key": k, "side": "buy" if s.is_buy else "sell",
                    "bs_type": s.bs_type, "first_seen": i, "alive_until": i,
                })
        # 不在当前输出的活跃 episode 自然 freeze(alive_until 停在上一根)
        if i % 2000 == 0:
            print(f"  bar {i}/{n} episodes={len(episodes)}", flush=True)
    out = {
        "episodes": episodes, "gate": gate, "n": n, "warmup": WARMUP,
        "opens": df5["open"].to_numpy(), "closes": df5["close"].to_numpy(),
        "dates": [str(d) for d in df5["date"]],
        "span": f"{df5['date'].iloc[0].date()}~{df5['date'].iloc[-1].date()}",
    }
    Path(STAGE1_PKL).write_bytes(pickle.dumps(out))
    print(f"stage1 done: bars={n} episodes={len(episodes)} -> {STAGE1_PKL}", flush=True)
    return out


class _Sig:
    __slots__ = ("bs_type", "is_buy")

    def __init__(self, bs_type):
        self.bs_type = bs_type
        self.is_buy = True


def replay(st: dict, n_buy: int, n_sell: int) -> dict:
    """episode 在 first_seen+N 时仍存活 → 该 bar 触发;沿用 _run_exec 执行模型。"""
    buy_events: dict[int, str] = {}
    sell_bars: set[int] = set()
    n = st["n"]
    for ep in st["episodes"]:
        need = n_buy if ep["side"] == "buy" else n_sell
        c = ep["first_seen"] + need
        if c >= n or ep["alive_until"] < c:
            continue                                      # 确认前已被重绘掉/越界
        if ep["side"] == "buy":
            prev = buy_events.get(c)
            cand = ep["bs_type"]
            if prev is None or buy_class(cand) > buy_class(prev):   # 3first
                buy_events[c] = cand
        else:
            sell_bars.add(c)
    return _run_exec(buy_events, sell_bars, st["gate"], st["opens"], st["closes"], st["dates"])


def main() -> int:
    replay_only = "--replay-only" in sys.argv
    smoke = "--smoke" in sys.argv
    if replay_only and Path(STAGE1_PKL).exists():
        st = pickle.loads(Path(STAGE1_PKL).read_bytes())
    else:
        st = stage1(limit_bars=1500 if smoke else 0)
    bh = st["closes"][-1] / st["opens"][st["warmup"]] - 1
    print(f"\nTSLA 5m {st['span']} bars={st['n']} 裸持={bh:+.1%}  (N_buy×N_sell 确认层扫描)")
    rows = []
    for nb in N_BUYS:
        for ns in N_SELLS:
            r = replay(st, nb, ns)
            rows.append({"n_buy": nb, "n_sell": ns, "ret": round(r["ret"], 4),
                         "max_dd": round(r["max_dd"], 4), "trades": r["n"]})
    rows.sort(key=lambda x: -x["ret"])
    print(f"{'N_buy':>5} {'N_sell':>6} {'收益':>9} {'回撤':>7} {'笔数':>5}")
    for r in rows:
        print(f"{r['n_buy']:>5} {r['n_sell']:>6} {r['ret']:>+9.1%} {r['max_dd']:>7.1%} {r['trades']:>5}")
    Path(OUT_JSON).write_text(json.dumps({"buy_hold": round(bh, 4), "span": st["span"],
                                          "rows": rows}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"-> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
