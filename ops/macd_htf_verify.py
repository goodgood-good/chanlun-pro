# -*- coding: utf-8 -*-
"""验证改后的 apply_higher_macd_to_chart_data:
1. 桶末根严格等于真高周期 MACD;
2. 桶内逐根渐变(不再"连续 5 根一样")。
"""
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

chart_data = {"c": list(closes), "t": list(times)}
ok = apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
print(f"apply 返回: {ok},  bar 数: {n}")
fn_dif = np.array([np.nan if v is None else v for v in chart_data["higher_macd_dif"]])
fn_hist = np.array([np.nan if v is None else v for v in chart_data["higher_macd_hist"]])

# 桶划分: 5m
t = np.array(times, dtype=np.int64)
key = t // 300
boundaries = np.concatenate(([True], key[1:] != key[:-1]))
bucket_idx = np.cumsum(boundaries) - 1
bcount = int(bucket_idx[-1]) + 1
last_pos = np.zeros(bcount, dtype=np.int64)
last_pos[bucket_idx] = np.arange(n)
htf_closes = np.array(closes, dtype=float)[last_pos]
ref_dif, _, ref_hist = talib.MACD(htf_closes, 12, 26, 9)

# 1. 桶末根 vs 真 5m MACD
last_bars = last_pos  # 每桶最后一根 1m 的下标
mask = ~np.isnan(ref_dif)
d = fn_dif[last_bars][mask] - np.round(ref_dif[mask], 6)
print(f"桶末根 dif vs 真5m MACD   max|diff| = {np.nanmax(np.abs(d)):.2e}  (应≈0)")

# 2. 桶内是否仍有"连续 5 根一样"
seg_dup = 0
for b in range(bcount):
    members = np.where(bucket_idx == b)[0]
    vals = fn_dif[members]
    vals = vals[~np.isnan(vals)]
    if len(vals) >= 2 and len(set(np.round(vals, 6))) == 1:
        seg_dup += 1
print(f"5m 桶总数: {bcount},  桶内值全相同(staircase)的桶数: {seg_dup}  (应=0)")

# 画图: 放大一段看桶内渐变
lo, hi = 2000, 2120
x = np.arange(lo, hi)
fig, ax = plt.subplots(2, 1, figsize=(15, 7), sharex=True)
ax[0].plot(x, fn_dif[lo:hi], "-o", ms=3, color="#2962FF", label="higher_macd_dif (改后)")
for b in np.unique(bucket_idx[lo:hi]):
    bl = last_pos[b]
    if lo <= bl < hi:
        ax[0].axvline(bl, color="#ccc", lw=0.7)
        ax[0].plot(bl, np.round(ref_dif[b], 6), "x", color="red", ms=8)
ax[0].set_title("放大段: 蓝点=逐根输出, 灰线=5m桶边界, 红叉=真5m MACD(应落在桶末根)")
ax[0].legend(fontsize=8)
ax[1].bar(np.arange(n), fn_hist, width=1.0, color="#26a69a")
ax[1].axhline(0, color="#999", lw=0.5)
ax[1].axvspan(lo, hi, color="#ffe", alpha=0.5)
ax[1].set_title("全段 HIST")
plt.tight_layout()
plt.savefig("ops/macd_htf_verify.png", dpi=110)
print("saved: ops/macd_htf_verify.png")
