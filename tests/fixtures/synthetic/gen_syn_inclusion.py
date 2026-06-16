# -*- coding: utf-8 -*-
"""生成 A-CRIT-1 回归 fixture:强包含震荡合成 K 线(seed 固定、可复现)。

用途:tests/chan_core/test_incremental_equivalence.py::test_synth_strong_inclusion_every_bar
钉死 zs_calculator ``len<min`` 早退分支不残留陈旧中枢——这类「段数在
min_zs_lines 边界(3↔4)反复横跳」的瞬态分叉,真实行情 fixture 触发不到
(真实笔/段已确认前缀不消失),须用病态强包含数据逼出。

刻意不放进 tests/fixtures/ 顶层:那里被 test_golden_master 的 glob 全收集、
要求配 golden 终态;合成病态数据不属于「生产行为指纹」语义,故隔到 synthetic/
子目录(glob 非递归、不收集),只供对拍网逐根读取。

固化为 parquet(保 float64 精度、免 numpy default_rng 跨版本漂移)。重生:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/fixtures/synthetic/gen_syn_inclusion.py
"""
from pathlib import Path

import numpy as np
import pandas as pd


def synth_strong_inclusion(n: int = 160, seed: int = 0) -> pd.DataFrame:
    """每根尽量包住前一根(最大化缠论包含处理 churn)+ 周期性突破制造分型/笔,
    使笔/段数在 min 边界反复横跳,逼出 zs ``len<min`` 早退残留。"""
    rng = np.random.default_rng(seed)
    base = 100.0
    rows = []
    hi, lo = base + 1, base - 1
    for i in range(n):
        if i % 3 == 0:
            hi += rng.uniform(0.5, 2.0)
            lo -= rng.uniform(0.5, 2.0)
        else:
            mid = (hi + lo) / 2
            span = (hi - lo) * rng.uniform(0.2, 0.45)
            hi, lo = mid + span, mid - span
            if i % 7 == 0:
                hi += 3.0
            if i % 11 == 0:
                lo -= 3.0
        o = (hi + lo) / 2
        c = o + rng.uniform(-0.3, 0.3) * (hi - lo)
        c = min(max(c, lo), hi)
        rows.append((pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=5 * i),
                     o, hi, lo, c, 1000.0))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df.insert(0, "code", "SYN.incl")
    return df


def main() -> None:
    out = Path(__file__).resolve().parent / "SYN_strong_inclusion_5m.parquet"
    df = synth_strong_inclusion()
    df.to_parquet(out)
    print(f"saved {out}  shape={df.shape}")


if __name__ == "__main__":
    main()
