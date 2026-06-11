# -*- coding: utf-8 -*-
"""TSLA 信号 episode 生命周期分析:各买卖点类的右边缘幻影率。

读 wf_confirm_scan_tsla 的 stage1 pkl,按 bs_type 统计:
- episode 总数、存活时长(alive_until-first_seen)分位数
- 确认存活率 P(alive>=N):信号首见后 N 根仍在的比例(=确认层通过率)
- 速死率(存活<2根)=幻影占比
指导 per-class 确认参数(哪类买点右边缘最不稳定)。
运行: PYTHONPATH=src python scripts/analyze_episodes_tsla.py
"""
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
STAGE1_PKL = "D:/chanlun_pro/reports/wf_confirm_tsla_stage1.pkl"


def main() -> int:
    st = pickle.loads(Path(STAGE1_PKL).read_bytes())
    eps = st["episodes"]
    n = st["n"]
    print(f"TSLA {st['span']} episodes={len(eps)} bars={st['n']}")
    by_type = defaultdict(list)
    for ep in eps:
        # 右边缘截断的 episode(活到最后一根)不知真实寿命,单独计
        truncated = ep["alive_until"] >= n - 1
        by_type[ep["bs_type"]].append((ep["alive_until"] - ep["first_seen"], truncated))
    print(f"{'类型':>6} {'总数':>5} {'截断':>4} {'中位寿命':>7} {'速死<2':>6} "
          f"{'≥2存活':>6} {'≥4存活':>6} {'≥8存活':>6} {'≥12存活':>7}")
    for bt in sorted(by_type):
        rows = by_type[bt]
        lives = np.array([r[0] for r in rows])
        trunc = sum(1 for r in rows if r[1])
        full = np.array([r[0] for r in rows if not r[1]])   # 非截断=真实寿命
        med = np.median(full) if len(full) else float("nan")
        def surv(k):
            return (lives >= k).mean() * 100
        quick_die = (full < 2).mean() * 100 if len(full) else float("nan")
        print(f"{bt:>6} {len(rows):>5} {trunc:>4} {med:>7.0f} {quick_die:>5.0f}% "
              f"{surv(2):>5.0f}% {surv(4):>5.0f}% {surv(8):>5.0f}% {surv(12):>6.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
