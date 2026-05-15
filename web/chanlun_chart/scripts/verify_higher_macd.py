"""手动验证脚本: 对照新旧 HTF MACD 算法在真实股票数据上的输出差异。

用法:
    cd D:/project/chanlun-pro
    python web/chanlun_chart/scripts/verify_higher_macd.py [SYMBOL] [BARS]

默认 SYMBOL=us.TSLA, BARS=1950 (~5 个美股交易日 1min)。

不接入 CI, 仅用于线上验证 H1+H2 修复效果的对照报告。
"""
from __future__ import annotations

import sys
import os
import numpy as np
import talib

# 让脚本能 import 项目代码
_THIS_DIR = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "..", "..", "src"))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))

from cl_app.services.chart_compute import (  # noqa: E402
    apply_higher_macd_to_chart_data,
    _bin_keys_for_higher,
    _resample_closes_to_higher,
)
from chanlun.exchange import get_exchange  # noqa: E402
from chanlun.base import Market  # noqa: E402


def _legacy_scale_macd(closes: list, ratio: int) -> list:
    """脚本本地保留的"旧参数放大法", 仅用于对照。跑完即弃。"""
    arr = np.array(closes, dtype=float)
    fast = 12 * ratio
    slow = 26 * ratio
    signal = 9 * ratio
    if len(arr) <= slow + signal:
        return [None] * len(arr)
    _, _, h_hist = talib.MACD(
        arr, fastperiod=fast, slowperiod=slow, signalperiod=signal,
    )
    return [None if np.isnan(v) else float(v) for v in h_hist]


def _ref_5m_hist(times: list, closes: list, market: str) -> list:
    """参考实现: 直接对手动合成 5m closes 跑 talib.MACD, 投影回 1m。

    与新算法应 == (numerical equivalence 在 unit test 已保证),
    这里再跑一遍真实数据复核。
    """
    t = np.array(times, dtype=np.int64)
    c = np.array(closes, dtype=float)
    bin_keys = _bin_keys_for_higher(t, "5m", market)
    higher_closes, low2high = _resample_closes_to_higher(c, bin_keys)
    if len(higher_closes) <= 26 + 9:
        return [None] * len(times)
    _, _, ref_hist = talib.MACD(higher_closes, 12, 26, 9)
    out: list = []
    for i in range(len(times)):
        v = ref_hist[low2high[i]]
        out.append(None if np.isnan(v) else float(v))
    return out


def _diff_stats(a: list, b: list) -> dict:
    """两个 hist 数组的差异统计 (忽略 None 项)。"""
    diffs = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        diffs.append(abs(x - y))
    if not diffs:
        return {"count": 0, "mean": 0.0, "max": 0.0, "p95": 0.0}
    arr = np.array(diffs)
    return {
        "count": int(len(arr)),
        "mean": float(arr.mean()),
        "max": float(arr.max()),
        "p95": float(np.percentile(arr, 95)),
    }


def _find_session_open_indices(times: list, market: str = "us") -> list:
    """返回 times 中"每个新交易日开盘后第一根 1m"的 index 列表。"""
    t = np.array(times, dtype=np.int64)
    day_bins = _bin_keys_for_higher(t, "d", market)
    out: list = []
    for i in range(1, len(day_bins)):
        if day_bins[i] != day_bins[i - 1]:
            out.append(i)
    return out


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "us.TSLA"
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 1950

    # 简单的 market 识别 (us./us:/a./hk. 等前缀)
    if symbol.lower().startswith(("us.", "us:")):
        market = "us"
    elif symbol.lower().startswith(("hk.", "hk:")):
        market = "hk"
    else:
        market = "a"

    code = symbol.split(".", 1)[-1] if "." in symbol else symbol.split(":", 1)[-1]
    print(f"[INPUT] symbol={symbol} market={market} bars={bars} freq=1m")

    try:
        ex = get_exchange(Market(market))
    except Exception as e:
        print(f"[ERROR] get_exchange({market}) failed: {e}")
        return 1

    df = ex.klines(code, "1m")
    if df is None or len(df) == 0:
        print("[ERROR] no data, aborting")
        return 1
    df = df.tail(bars).reset_index(drop=True)

    times = [int(d.timestamp()) for d in df["date"]]
    closes = [float(v) for v in df["close"]]

    chart_data_new = {"t": list(times), "c": list(closes)}
    apply_higher_macd_to_chart_data(chart_data_new, "1m", market, {})
    new_hist = chart_data_new.get("higher_macd_hist") or [None] * len(times)

    # 旧算法对照: 1m HTF 旧 ratio = 5
    old_hist = _legacy_scale_macd(closes, ratio=5)
    ref_hist = _ref_5m_hist(times, closes, market)

    new_vs_ref = _diff_stats(new_hist, ref_hist)
    old_vs_ref = _diff_stats(old_hist, ref_hist)

    open_idxs = _find_session_open_indices(times, market)
    open_first_diffs_new = _diff_stats(
        [new_hist[i] for i in open_idxs if i < len(new_hist)],
        [ref_hist[i] for i in open_idxs if i < len(ref_hist)],
    )
    open_first_diffs_old = _diff_stats(
        [old_hist[i] for i in open_idxs if i < len(old_hist)],
        [ref_hist[i] for i in open_idxs if i < len(ref_hist)],
    )

    print()
    print("| Metric                          | NEW vs REF | OLD vs REF |")
    print("|---------------------------------|------------|------------|")
    print(f"| mean(|diff|) on hist            | {new_vs_ref['mean']:.6f}   | {old_vs_ref['mean']:.6f}   |")
    print(f"| max(|diff|)                     | {new_vs_ref['max']:.6f}   | {old_vs_ref['max']:.6f}   |")
    print(f"| p95(|diff|)                     | {new_vs_ref['p95']:.6f}   | {old_vs_ref['p95']:.6f}   |")
    print(f"| open first bar mean diff        | {open_first_diffs_new['mean']:.6f}   | {open_first_diffs_old['mean']:.6f}   |")
    print(f"| open first bar max diff         | {open_first_diffs_new['max']:.6f}   | {open_first_diffs_old['max']:.6f}   |")
    print()
    print(f"[INFO] open sessions detected: {len(open_idxs)}")
    print(f"[INFO] valid hist points compared: NEW={new_vs_ref['count']} OLD={old_vs_ref['count']}")
    print()
    accept = new_vs_ref["count"] > 0 and new_vs_ref["max"] < 1e-6
    print(
        ("[ACCEPT] " if accept else "[REJECT] ")
        + (
            "NEW vs REF 全局 max 差值 < 1e-6"
            if accept
            else "新算法与 REF 有显著差异 (max=%.6f), 检查实现" % new_vs_ref["max"]
        )
    )
    return 0 if accept else 2


if __name__ == "__main__":
    sys.exit(main())
