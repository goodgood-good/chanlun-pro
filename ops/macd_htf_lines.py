# -*- coding: utf-8 -*-
"""诊断 MACD_HTF 的 DIF/DEA 线: 当前(随1m收盘价) vs 平滑插值 vs 阶梯。"""
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

cd = {"c": list(closes), "t": list(times)}
apply_higher_macd_to_chart_data(cd, "1m", "a", {})
cur_dif = np.array([np.nan if v is None else v for v in cd["higher_macd_dif"]])
cur_dea = np.array([np.nan if v is None else v for v in cd["higher_macd_dea"]])

# 桶划分
t = np.array(times, dtype=np.int64)
key = t // 300
bucket_idx = np.cumsum(np.concatenate(([True], key[1:] != key[:-1]))) - 1
bc = int(bucket_idx[-1]) + 1
last_pos = np.zeros(bc, dtype=np.int64)
last_pos[bucket_idx] = np.arange(n)
htf_closes = np.array(closes, dtype=float)[last_pos]
macd_b, dea_b, _ = talib.MACD(htf_closes, 12, 26, 9)

# A. 当前: 随 1m 收盘价(桶内锯齿)
# B. 平滑插值: 真5m点之间线性连
xp = last_pos.astype(float)
interp_dif = np.interp(np.arange(n), xp, np.where(np.isnan(macd_b), np.nan, macd_b))
interp_dea = np.interp(np.arange(n), xp, np.where(np.isnan(dea_b), np.nan, dea_b))
# C. 阶梯: 桶内保持真5m值
stair_dif = np.where(np.isnan(macd_b[bucket_idx]), np.nan, macd_b[bucket_idx])

lo, hi = 1230, 1230 + 60
x = np.arange(lo, hi)
fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
bl = [last_pos[b] for b in np.unique(bucket_idx[lo:hi]) if lo <= last_pos[b] < hi]

axes[0].plot(x, cur_dif[lo:hi], "-o", ms=3, color="#2962FF", label="DIF")
axes[0].plot(x, cur_dea[lo:hi], "-o", ms=3, color="#FF6D00", label="DEA")
axes[0].set_title("A. 当前实现: DIF/DEA 随 1m 收盘价 → 桶内锯齿抖动")

axes[1].plot(x, interp_dif[lo:hi], "-", lw=1.6, color="#2962FF", label="DIF")
axes[1].plot(x, interp_dea[lo:hi], "-", lw=1.6, color="#FF6D00", label="DEA")
axes[1].set_title("B. 平滑插值: 真5m点之间线性连接(平滑, 当前桶会随新bar微调)")

axes[2].step(x, stair_dif[lo:hi], where="post", lw=1.6, color="#2962FF", label="DIF")
axes[2].set_title("C. 阶梯: 桶内保持真5m值(每5根一平)")

for ax in axes:
    for b in bl:
        ax.axvline(b + 0.5, color="#ddd", lw=0.8)
    ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig("ops/macd_htf_lines.png", dpi=110)
print("saved: ops/macd_htf_lines.png")
