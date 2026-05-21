# -*- coding: utf-8 -*-
"""紧凑对比: 旧 forward-fill(连续 5 根一样) vs 新桶内渐变。"""
import sys

sys.path.insert(0, "web/chanlun_chart")
sys.path.insert(0, "src")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import talib

from cl_app.services.chart_compute import apply_higher_macd_to_chart_data

df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet").reset_index(drop=True)
closes = df["close"].tolist()
times = (df["date"].astype("int64") // 10**9).tolist()
n = len(closes)

# 新行为
cd = {"c": list(closes), "t": list(times)}
apply_higher_macd_to_chart_data(cd, "1m", "a", {})
new_dif = np.array([np.nan if v is None else v for v in cd["higher_macd_dif"]])

# 旧行为(forward-fill 桶末值)
t = np.array(times, dtype=np.int64)
key = t // 300
bucket_idx = np.cumsum(np.concatenate(([True], key[1:] != key[:-1]))) - 1
bc = int(bucket_idx[-1]) + 1
last_pos = np.zeros(bc, dtype=np.int64)
last_pos[bucket_idx] = np.arange(n)
htf_closes = np.array(closes, dtype=float)[last_pos]
old_dif_b, _, _ = talib.MACD(htf_closes, 12, 26, 9)
old_dif = np.round(old_dif_b[bucket_idx], 6)

# 找一段价格有波动的窗口
lo = 1230
hi = lo + 45
x = np.arange(lo, hi)
fig, ax = plt.subplots(figsize=(14, 5))
ax.step(x, old_dif[lo:hi], where="post", color="#d32f2f", lw=1.6,
        label="OLD: forward-fill (连续 5 根一样)")
ax.plot(x, new_dif[lo:hi], "-o", ms=4, color="#2962FF",
        label="NEW: 桶内逐根渐变")
for b in np.unique(bucket_idx[lo:hi]):
    bl = last_pos[b]
    if lo <= bl < hi:
        ax.axvline(bl + 0.5, color="#ddd", lw=0.8)
ax.set_title("MACD_HTF DIF — 旧 staircase vs 新渐变 (灰线=5m桶边界)")
ax.legend()
plt.tight_layout()
plt.savefig("ops/macd_htf_stairs.png", dpi=120)
print("saved: ops/macd_htf_stairs.png")
