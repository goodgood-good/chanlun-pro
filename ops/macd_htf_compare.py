# -*- coding: utf-8 -*-
"""对比 MACD_HTF 的两种算法:参数缩放 vs 真高周期重采样。

- 方案A(现状):在 1m close 上用 fast=12*5, slow=26*5, signal=9*5 跑 MACD。
- 方案B(真高周期):把 1m 重采样成 5m OHLC,在 5m close 上跑 MACD(12,26,9),
  再 forward-fill 回 1m 时间轴。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import talib

RATIO = 5  # 1m -> 5m

df = pd.read_parquet("tests/fixtures/klines/a_SH_513100_1m.parquet")
df = df.reset_index(drop=True)
closes = df["close"].to_numpy(dtype=float)
n = len(closes)

# ---- 方案A:参数缩放 ----
a_dif, a_dea, a_hist = talib.MACD(
    closes, fastperiod=12 * RATIO, slowperiod=26 * RATIO, signalperiod=9 * RATIO
)

# ---- 方案B:真 5m 重采样 ----
g = df.copy()
g["bucket"] = np.arange(n) // RATIO  # 每 5 根 1m 合成 1 根 5m
agg = g.groupby("bucket").agg(
    open=("open", "first"),
    high=("high", "max"),
    low=("low", "min"),
    close=("close", "last"),
)
htf_close = agg["close"].to_numpy(dtype=float)
b_dif_htf, b_dea_htf, b_hist_htf = talib.MACD(
    htf_close, fastperiod=12, slowperiod=26, signalperiod=9
)
# forward-fill 回 1m 时间轴:第 i 根 1m 属于 bucket i//RATIO
bucket_idx = np.arange(n) // RATIO
b_dif = b_dif_htf[bucket_idx]
b_dea = b_dea_htf[bucket_idx]
b_hist = b_hist_htf[bucket_idx]

# ---- 统计差异 ----
mask = ~(np.isnan(a_hist) | np.isnan(b_hist))
diff = a_hist[mask] - b_hist[mask]
print(f"bar 数: {n}")
print(f"有效对比点: {mask.sum()}")
print(f"hist 差异  mean|d|={np.mean(np.abs(diff)):.6f}  max|d|={np.max(np.abs(diff)):.6f}")
b_range = np.nanmax(b_hist) - np.nanmin(b_hist)
print(f"真高周期 hist 量程: {b_range:.6f}  ->  平均相对误差 {np.mean(np.abs(diff))/b_range*100:.1f}%")
# 符号一致率(柱子红绿是否一致)
sign_agree = np.mean(np.sign(a_hist[mask]) == np.sign(b_hist[mask]))
print(f"hist 符号(红/绿)一致率: {sign_agree*100:.1f}%")

# ---- 画图 ----
x = np.arange(n)
fig, axes = plt.subplots(3, 1, figsize=(16, 11), sharex=True)

axes[0].plot(x, closes, color="#333", lw=0.6)
axes[0].set_title("513100  1m close")

axes[1].plot(x, a_dif, label="A scaled DIF", color="#2962FF", lw=0.9)
axes[1].plot(x, a_dea, label="A scaled DEA", color="#FF6D00", lw=0.9)
axes[1].plot(x, b_dif, label="B true-5m DIF", color="#2962FF", lw=1.4, ls="--")
axes[1].plot(x, b_dea, label="B true-5m DEA", color="#FF6D00", lw=1.4, ls="--")
axes[1].axhline(0, color="#999", lw=0.5)
axes[1].set_title("DIF / DEA  (solid=A scaled param,  dashed=B true 5m)")
axes[1].legend(loc="upper left", fontsize=8)

axes[2].bar(x - 0.2, a_hist, width=0.4, color="#ef5350", label="A scaled HIST")
axes[2].bar(x + 0.2, b_hist, width=0.4, color="#26a69a", label="B true-5m HIST")
axes[2].axhline(0, color="#999", lw=0.5)
axes[2].set_title("HIST  (red=A scaled param,  green=B true 5m)")
axes[2].legend(loc="upper left", fontsize=8)

plt.tight_layout()
out = "ops/macd_htf_compare.png"
plt.savefig(out, dpi=110)
print(f"saved: {out}")
