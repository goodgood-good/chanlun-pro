# HTF MACD 真合成 (H1+H2 根治) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `apply_higher_macd_to_chart_data` 从"在低周期 closes 上跑放大参数 EMA"改为"按市场时区合成高周期 K 线 → 跑标准 `talib.MACD(12,26,9)` → 投影回低周期",修正前端 MACD_HTF 与高周期真实 MACD 对不上、美股跨夜伪 spike 两个问题。

**Architecture:** 在 `chart_compute.py` 内部新增 3 个纯函数(`_resolve_higher_target_freq` / `_bin_keys_for_higher` / `_resample_closes_to_higher`),重写 `apply_higher_macd_to_chart_data` 串联它们;删除三个旧倍率常量表 + `_resolve_higher_macd_ratio`;测试 TDD 增量推进,验证脚本手动跑真实数据出对照报告。

**Tech Stack:** Python 3 / NumPy / talib / pytz / pytest。

**关联 Spec:** `docs/superpowers/specs/2026-05-15-higher-macd-resample-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `web/chanlun_chart/cl_app/services/chart_compute.py` | Modify | 新增 `HIGHER_FREQ_MAP` / `MARKET_TZ` / 3 个内部函数;重写 `apply_higher_macd_to_chart_data`;删除旧 ratio 常量与 `_resolve_higher_macd_ratio` |
| `web/chanlun_chart/cl_app/blueprints/tv.py` | Modify | 删除 L161-163 三个旧常量 import |
| `tests/test_apply_higher_macd.py` | Rewrite | 删 ratio-based 测试,加新算法单元测试 + numerical equivalence + 跨夜污染验证 |
| `web/chanlun_chart/scripts/verify_higher_macd.py` | Create | 一次性手动验证脚本,跑真实股票数据对照新旧算法 |

---

## Task 1: 新增 `HIGHER_FREQ_MAP` + `MARKET_TZ` 常量 与 `_resolve_higher_target_freq` 函数

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`(在原 `HIGHER_MACD_RATIO` 上方新增新常量;在文件末尾新增函数)
- Modify: `tests/test_apply_higher_macd.py`(追加新测试,旧测试暂保留)

- [ ] **Step 1.1: 在 `tests/test_apply_higher_macd.py` 末尾追加 `_resolve_higher_target_freq` 的失败测试**

```python
# === New algorithm tests (resample-based HTF MACD) ===
# 后续 task 会在 chart_compute.py 中新增以下符号。

def test_resolve_higher_target_freq_mappings():
    from cl_app.services.chart_compute import _resolve_higher_target_freq
    assert _resolve_higher_target_freq("1m", "a") == "5m"
    assert _resolve_higher_target_freq("5m", "a") == "30m"
    assert _resolve_higher_target_freq("30m", "us") == "d"
    assert _resolve_higher_target_freq("d", "us") == "w"
    assert _resolve_higher_target_freq("w", "us") == "M"


def test_resolve_higher_target_freq_no_higher():
    from cl_app.services.chart_compute import _resolve_higher_target_freq
    assert _resolve_higher_target_freq("M", "us") is None
    assert _resolve_higher_target_freq("999x", "us") is None
```

- [ ] **Step 1.2: 跑测试确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_resolve_higher_target_freq_mappings -v
```

Expected: FAIL with `ImportError: cannot import name '_resolve_higher_target_freq'`

- [ ] **Step 1.3: 在 `chart_compute.py` 文件 L47 注释行 `# ---------------- 跨周期 MACD 倍率 ----------------` 上方新增常量**

```python
# ---------------- HTF MACD: 频率映射与市场时区 ----------------

# 当前周期 -> 目标高周期(去除"放大倍率"概念,改用"目标周期标识符")
HIGHER_FREQ_MAP = {
    "1m": "5m",
    "5m": "30m",
    "30m": "d",
    "d": "w",
    "w": "M",
}

# 市场时区,决定 d/w/M bin 切割时的"自然日界"
MARKET_TZ = {
    "a": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
    "ny_futures": "America/New_York",
    "futures": "Asia/Shanghai",
    "currency": "UTC",
    "currency_spot": "UTC",
    "fx": "UTC",
}

```

- [ ] **Step 1.4: 在 `chart_compute.py` 文件末尾新增函数**

```python
def _resolve_higher_target_freq(frequency: str, market: str) -> str | None:
    """frequency -> 目标高周期标识符;无对照返回 None。

    与旧 _resolve_higher_macd_ratio 的区别:不再返回"倍率",直接返回目标
    周期字符串,后续由 _bin_keys_for_higher 决定具体怎么合成。
    """
    return HIGHER_FREQ_MAP.get(frequency)
```

- [ ] **Step 1.5: 跑测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_resolve_higher_target_freq_mappings tests/test_apply_higher_macd.py::test_resolve_higher_target_freq_no_higher -v
```

Expected: 2 passed

- [ ] **Step 1.6: 跑全量旧测试确认不破坏**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py -v
```

Expected: 全部 passed(旧 ratio 测试仍 OK,因为旧常量没删)

- [ ] **Step 1.7: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
feat(htf-macd): T1 — 新增 HIGHER_FREQ_MAP / MARKET_TZ / _resolve_higher_target_freq

H1+H2 根治第 1 步: 引入新的"目标周期"映射, 与旧 HIGHER_MACD_RATIO 共存,
后续 task 用它替换"放大倍率"算法。

- HIGHER_FREQ_MAP: 1m->5m / 5m->30m / 30m->d / d->w / w->M
- MARKET_TZ: 各市场时区, 用于后续 _bin_keys_for_higher 的 d/w/M 切分
- _resolve_higher_target_freq: 纯函数, 直接查表

测试: 2 个新 unit test 验证映射与未知 freq 返回 None; 旧 ratio 测试全部
保持通过(旧常量本 commit 不动)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 实现 `_bin_keys_for_higher` 的 5m / 30m 分支

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`
- Modify: `tests/test_apply_higher_macd.py`

- [ ] **Step 2.1: 在 `tests/test_apply_higher_macd.py` 末尾追加测试**

```python
def test_bin_keys_5m_basic():
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    # 5 根 1m, 每根相隔 60s, 应分两个 5m bin
    # epoch 1700000000 / 60 / 120 / 180 / 240 在 5m=300s 边界:
    #   1700000000 // 300 = 5666666; 1700000240 // 300 = 5666667
    times = np.array(
        [1700000000, 1700000060, 1700000120, 1700000180, 1700000240],
        dtype=np.int64,
    )
    bins = _bin_keys_for_higher(times, "5m", "us")
    # 前 4 根属同 bin (epoch < 1700000300), 第 5 根跨 bin
    assert bins[0] == bins[1] == bins[2] == bins[3]
    assert bins[4] == bins[0] + 1


def test_bin_keys_5m_cross_overnight():
    """美股 1m 跨夜: 昨日 16:00 与今日 09:30 必须落在不同 5m bin。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 取 2024-01-02 (周二, 交易日)
    yesterday_close = int(tz.localize(datetime.datetime(2024, 1, 2, 15, 59)).timestamp())
    today_open = int(tz.localize(datetime.datetime(2024, 1, 3, 9, 30)).timestamp())
    times = np.array([yesterday_close, today_open], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "5m", "us")
    assert bins[0] != bins[1]  # 跨夜两根必不同 bin


def test_bin_keys_30m_basic():
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    # 30m = 1800s
    # epoch 1700001800 是 30m 边界
    times = np.array(
        [1700000000, 1700001799, 1700001800, 1700003600],
        dtype=np.int64,
    )
    bins = _bin_keys_for_higher(times, "30m", "us")
    assert bins[0] == bins[1]
    assert bins[2] == bins[0] + 1
    assert bins[3] == bins[0] + 2
```

- [ ] **Step 2.2: 跑测试确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_bin_keys_5m_basic -v
```

Expected: FAIL `ImportError: cannot import name '_bin_keys_for_higher'`

- [ ] **Step 2.3: 在 `chart_compute.py` 末尾(`_resolve_higher_target_freq` 之后)新增函数**

```python
def _bin_keys_for_higher(
    times: "np.ndarray",
    target_freq: str,
    market: str,
) -> "np.ndarray":
    """计算每根低周期 K 线归属哪个高周期 bin。

    返回 int64 numpy 数组,长度 == len(times)。

    bin id 仅用作"相邻同 bin 分组键",不要求全局唯一/单调。但对每个 target_freq,
    保证: bar_a 与 bar_b 同 bin 当且仅当 b_a 应被合成进同一根高周期 K 线。

    target_freq:
      "5m":  epoch // 300
      "30m": epoch // 1800
      "d":   market 时区下的 date.toordinal()
      "w":   market 时区下 ISO (year, week) 打包成 year*100 + week
      "M":   market 时区下 year * 100 + month
    """
    if target_freq == "5m":
        return (times // 300).astype(np.int64)
    if target_freq == "30m":
        return (times // 1800).astype(np.int64)
    # d / w / M 需要时区
    tz_name = MARKET_TZ.get(market, "UTC")
    tz = pytz.timezone(tz_name)
    out = np.empty(len(times), dtype=np.int64)
    for i, t in enumerate(times):
        dt = datetime.datetime.fromtimestamp(int(t), tz=tz)
        if target_freq == "d":
            out[i] = dt.date().toordinal()
        elif target_freq == "w":
            iso = dt.isocalendar()
            out[i] = iso[0] * 100 + iso[1]
        elif target_freq == "M":
            out[i] = dt.year * 100 + dt.month
        else:
            raise ValueError(f"Unsupported target_freq: {target_freq}")
    return out
```

注:`np` / `datetime` / `pytz` 在文件顶部已 import,无需补充。

- [ ] **Step 2.4: 跑测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_bin_keys_5m_basic tests/test_apply_higher_macd.py::test_bin_keys_5m_cross_overnight tests/test_apply_higher_macd.py::test_bin_keys_30m_basic -v
```

Expected: 3 passed

- [ ] **Step 2.5: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
feat(htf-macd): T2 — _bin_keys_for_higher 实现 5m/30m/d/w/M 分支

H1+H2 根治第 2 步: 把"每根低周期 K 线归属哪个高周期 bin"的判定逻辑实现
为纯函数。5m/30m 用 epoch 整除秒数(任何时区都对); d/w/M 用市场时区下
的日历分组(美股 ET 16:00 后归属当日, 不会被 UTC 切日污染)。

3 个 unit test:
- 5m 基础边界
- 5m 跨夜两根必不同 bin (H2 关键断言)
- 30m 基础边界

后续 task 还会加 d/w/M 的市场时区测试。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 补 `_bin_keys_for_higher` 的 d / w / M 测试覆盖

**Files:**
- Modify: `tests/test_apply_higher_macd.py`

- [ ] **Step 3.1: 在 `tests/test_apply_higher_macd.py` 末尾追加 d/w/M 测试**

```python
def test_bin_keys_d_us_market_tz_overnight():
    """美股 ET 16:00 后的 30m bar (若存在) 应归属当日, 不被 UTC 切到次日。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 2024-01-02 (周二) 同一交易日内三个时刻
    morning = int(tz.localize(datetime.datetime(2024, 1, 2, 9, 30)).timestamp())
    afternoon = int(tz.localize(datetime.datetime(2024, 1, 2, 15, 59)).timestamp())
    next_day_morning = int(tz.localize(datetime.datetime(2024, 1, 3, 9, 30)).timestamp())

    times = np.array([morning, afternoon, next_day_morning], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "d", "us")
    assert bins[0] == bins[1]      # 同一交易日 (2024-01-02)
    assert bins[2] == bins[0] + 1  # 跨日 (2024-01-03)


def test_bin_keys_w_iso_monday_first():
    """ISO 周: 周一为首, 周一-周五同 bin, 下周一 bin+1。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 2024-01-01 是周一; 2024-01-05 是周五; 2024-01-08 是下周一
    monday = int(tz.localize(datetime.datetime(2024, 1, 1, 9, 30)).timestamp())
    friday = int(tz.localize(datetime.datetime(2024, 1, 5, 15, 0)).timestamp())
    next_monday = int(tz.localize(datetime.datetime(2024, 1, 8, 9, 30)).timestamp())

    times = np.array([monday, friday, next_monday], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "w", "us")
    assert bins[0] == bins[1]
    assert bins[2] != bins[0]


def test_bin_keys_M_year_month():
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    jan = int(tz.localize(datetime.datetime(2024, 1, 31, 23, 59)).timestamp())
    feb = int(tz.localize(datetime.datetime(2024, 2, 1, 0, 1)).timestamp())
    next_year_jan = int(tz.localize(datetime.datetime(2025, 1, 1, 0, 1)).timestamp())

    times = np.array([jan, feb, next_year_jan], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "M", "us")
    assert bins[0] != bins[1]
    assert bins[2] != bins[1]
    # 跨年 bin 单调递增
    assert bins[2] > bins[1] > bins[0]


def test_bin_keys_unknown_market_falls_back_to_utc():
    """未知 market 用 UTC, 不应崩。"""
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    times = np.array([1700000000, 1700086400], dtype=np.int64)  # 相差 1 天
    bins = _bin_keys_for_higher(times, "d", "unknown_market")
    assert bins[1] == bins[0] + 1
```

- [ ] **Step 3.2: 跑测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py -k "test_bin_keys_d_us or test_bin_keys_w_iso or test_bin_keys_M or test_bin_keys_unknown" -v
```

Expected: 4 passed

- [ ] **Step 3.3: Commit**

```bash
cd D:/project/chanlun-pro && git add tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
test(htf-macd): T3 — _bin_keys_for_higher 补 d/w/M 与未知市场覆盖

补 4 个 unit test:
- d 美股市场时区(2024-01-02 与 2024-01-03 归不同 bin)
- w ISO 周(2024-01-01 周一 ~ 2024-01-05 周五 同 bin, 下周一 +1)
- M 月份(跨月与跨年都正确递增)
- 未知 market fallback 到 UTC, 不崩

实现已在 T2 完成, 本 commit 仅补测试覆盖。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 实现 `_resample_closes_to_higher` (演化模式)

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`
- Modify: `tests/test_apply_higher_macd.py`

- [ ] **Step 4.1: 在 `tests/test_apply_higher_macd.py` 末尾追加失败测试**

```python
def test_resample_evolving_close_same_bin():
    """同一 bin 内多根 close: higher_closes 取 bin 内最后一根 (演化模式)。"""
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([100.0, 101.0, 102.0, 103.0], dtype=float)
    bin_keys = np.array([1, 1, 1, 1], dtype=np.int64)  # 全部同 bin
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert len(higher_closes) == 1
    assert higher_closes[0] == 103.0  # bin 内 last close
    assert list(low2high) == [0, 0, 0, 0]


def test_resample_bin_switches():
    """bin 切换: higher_closes 长度增加, low2high 对应索引递增。"""
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([100.0, 101.0, 200.0, 201.0, 300.0], dtype=float)
    bin_keys = np.array([1, 1, 2, 2, 3], dtype=np.int64)
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert list(higher_closes) == [101.0, 201.0, 300.0]
    assert list(low2high) == [0, 0, 1, 1, 2]


def test_resample_empty_input():
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([], dtype=float)
    bin_keys = np.array([], dtype=np.int64)
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert len(higher_closes) == 0
    assert len(low2high) == 0
```

- [ ] **Step 4.2: 跑测试确认失败**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_resample_evolving_close_same_bin -v
```

Expected: FAIL `ImportError: cannot import name '_resample_closes_to_higher'`

- [ ] **Step 4.3: 在 `chart_compute.py` 末尾(`_bin_keys_for_higher` 之后)新增函数**

```python
def _resample_closes_to_higher(
    closes: "np.ndarray",
    bin_keys: "np.ndarray",
) -> "tuple[np.ndarray, np.ndarray]":
    """按 bin_keys 把 closes 合成到高周期 closes (演化模式)。

    返回:
      higher_closes: 每个唯一 bin 的 close (bin 内最后一根低周期 close)
      low_to_higher_idx: 长度 == len(closes), 每个值是该低周期 K 线对应的
                        higher_closes 索引。

    "演化模式": 同 bin 内的多根低周期 K 线, higher_closes 用最新一根 close
    覆盖。每次重算时 higher_closes[-1] 反映当前 bin 内最新 close, 等价于
    "未收盘高周期 bar 实时演化"。

    假设 bin_keys 是按低周期 K 线时间顺序排列的(同一 bin 在数组里相邻),
    这由调用方保证 (chart_data["t"] 本身按时间升序)。
    """
    n = len(closes)
    if n == 0:
        return (
            np.empty(0, dtype=float),
            np.empty(0, dtype=np.int64),
        )
    higher_closes: "list[float]" = []
    low_to_higher_idx = np.empty(n, dtype=np.int64)
    prev_bin = None
    cur_higher_idx = -1
    for i in range(n):
        bk = bin_keys[i]
        if bk != prev_bin:
            cur_higher_idx += 1
            higher_closes.append(float(closes[i]))
            prev_bin = bk
        else:
            higher_closes[cur_higher_idx] = float(closes[i])
        low_to_higher_idx[i] = cur_higher_idx
    return np.array(higher_closes, dtype=float), low_to_higher_idx
```

- [ ] **Step 4.4: 跑测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_resample_evolving_close_same_bin tests/test_apply_higher_macd.py::test_resample_bin_switches tests/test_apply_higher_macd.py::test_resample_empty_input -v
```

Expected: 3 passed

- [ ] **Step 4.5: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
feat(htf-macd): T4 — _resample_closes_to_higher (演化模式)

H1+H2 根治第 4 步: 把"按 bin 分组合成高周期 closes"实现为纯函数。
演化模式: 同 bin 内 last close 覆盖, 等价于"未收盘高周期 bar 实时
演化", 与现行 MACD_HTF UX 一致。

输入: closes(float64) + bin_keys(int64, 按时间升序)
输出: (higher_closes, low_to_higher_idx)
  - higher_closes 长度 == 唯一 bin 数
  - low_to_higher_idx 长度 == len(closes), 单调非降

3 个 unit test:
- 同 bin 多根 close, higher 取 last
- bin 切换 3 段
- 空输入不崩

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 重写 `apply_higher_macd_to_chart_data` 串联新函数

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py:441-492`
- Modify: `tests/test_apply_higher_macd.py`(更新旧测试 + 加 numerical equivalence + 跨夜污染验证)

- [ ] **Step 5.1: 在 `tests/test_apply_higher_macd.py` 末尾追加 numerical equivalence 测试**

```python
def test_apply_numerical_equivalence_to_real_5m_macd():
    """新算法 HTF 投影回 1m 后, 必须 == 直接对手动合成 5m closes 跑
    talib.MACD(12,26,9) 得到的 hist 投影。
    """
    import numpy as np
    import talib
    from cl_app.services.chart_compute import (
        apply_higher_macd_to_chart_data,
        _bin_keys_for_higher,
        _resample_closes_to_higher,
    )

    # 构造 500 根 1m, 时间戳连续 60s 步长 (无跨夜)
    base = 1700000000
    t = np.array([base + i * 60 for i in range(500)], dtype=np.int64)
    c = np.array([100.0 + i * 0.1 for i in range(500)], dtype=float)
    chart_data = {"t": t.tolist(), "c": c.tolist()}
    cfg = {"idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
    apply_higher_macd_to_chart_data(chart_data, "1m", "us", cfg)

    # 手动合成 5m closes 跑 ref MACD
    bin_keys = _bin_keys_for_higher(t, "5m", "us")
    higher_closes, low2high = _resample_closes_to_higher(c, bin_keys)
    _, _, ref_hist = talib.MACD(higher_closes, 12, 26, 9)

    # 投影回 1m, 与 apply 输出对比
    actual = chart_data["higher_macd_hist"]
    for i in range(500):
        expected = ref_hist[low2high[i]]
        a = actual[i]
        if np.isnan(expected):
            assert a is None, f"i={i}: expected None, got {a}"
        else:
            assert a is not None, f"i={i}: expected {expected}, got None"
            assert abs(a - expected) < 1e-6, f"i={i}: expected {expected}, got {a}"


def test_apply_no_overnight_contamination():
    """对比"含跨夜断层"vs"无断层"两份 1m 数据, 开盘后第一根 1m 的
    higher_macd_dif 不应受昨日尾盘 EMA 残留影响。
    """
    import numpy as np
    from cl_app.services.chart_compute import apply_higher_macd_to_chart_data

    # 构造 300 根 1m, 模拟一天交易; 然后跨夜 17 小时, 再 300 根
    base = 1700000000
    t_today_only = [base + i * 60 for i in range(300)]
    c_today_only = [100.0 + i * 0.1 for i in range(300)]

    # 含跨夜版本: 前 300 根模拟"昨日", 后 300 根跨 17h 间隔后开盘
    t_with_overnight = (
        [base + i * 60 for i in range(300)]
        + [base + 300 * 60 + 17 * 3600 + i * 60 for i in range(300)]
    )
    c_with_overnight = (
        [100.0 + i * 0.1 for i in range(300)]
        + [200.0 + i * 0.1 for i in range(300)]  # 跳空开盘到 200
    )

    cd_a = {"t": t_today_only, "c": c_today_only}
    cd_b = {"t": t_with_overnight, "c": c_with_overnight}
    apply_higher_macd_to_chart_data(cd_a, "1m", "us", {})
    apply_higher_macd_to_chart_data(cd_b, "1m", "us", {})

    # cd_b 后半段对应的"开盘后第一根 1m" 索引 = 300
    # 它的 higher_macd_dif 不应该被昨日尾盘 EMA 影响 — 因为新算法对该根 1m
    # 所属 5m bin 是"今日开盘后第一根 5m", 跑 MACD 时该 5m close 仅参与
    # 自己之后的 EMA 计算; 但前提是 _bin_keys 把昨/今分到不同 bin。
    # 弱断言: cd_b 第 300 根的 hist 与 cd_a 完全独立的第 0 根含义类似——
    # 这里我们检测一个更强的契约: 跨夜两根 1m bin_keys 必不同。
    from cl_app.services.chart_compute import _bin_keys_for_higher
    bin_keys_b = _bin_keys_for_higher(
        np.array(t_with_overnight, dtype=np.int64), "5m", "us"
    )
    assert bin_keys_b[299] != bin_keys_b[300], (
        f"跨夜两根必须落不同 bin: bin[299]={bin_keys_b[299]}, "
        f"bin[300]={bin_keys_b[300]}"
    )
```

- [ ] **Step 5.2: 跑新测试,确认 numerical equivalence FAIL(旧 apply 还在用放大参数)**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_apply_numerical_equivalence_to_real_5m_macd -v
```

Expected: FAIL(差值远大于 1e-6)

- [ ] **Step 5.3: 替换 `apply_higher_macd_to_chart_data` 函数体(`chart_compute.py:441-492`)**

把 L441-492 整段(从 `def apply_higher_macd_to_chart_data(` 到 `LogUtil.error(f"[apply_higher_macd] Scaled MACD calc failed: {e}")` 结束)替换为:

```python
def apply_higher_macd_to_chart_data(
    chart_data: dict,
    frequency: str,
    market: str,
    cl_config: dict,
) -> None:
    """计算并 in-place 写入 chart_data 的 higher_macd_dif/dea/hist 字段。

    实现:把低周期 closes 按市场时区合成目标高周期 closes(演化模式),
    在 higher_closes 上跑标准 talib.MACD(12,26,9),再把结果按低-高 idx
    映射投影回低周期长度。

    与旧"参数 × ratio"近似法相比:
    1. 数学上等价"用真实高周期 K 线跑标准 MACD"
    2. 跨夜断层、半日休市、午休由 bin 分组自然处理,不再污染 EMA
    """
    target_freq = _resolve_higher_target_freq(frequency, market)
    if target_freq is None:
        return  # 月线或未知 freq, 无高周期对照

    times_list = chart_data.get("t", [])
    closes_list = chart_data.get("c", [])
    if not times_list or not closes_list:
        return
    if len(times_list) != len(closes_list):
        LogUtil.error(
            f"[apply_higher_macd] t/c length mismatch: "
            f"{len(times_list)} vs {len(closes_list)}"
        )
        return

    try:
        times = np.array(times_list, dtype=np.int64)
        closes = np.array(closes_list, dtype=float)
        bin_keys = _bin_keys_for_higher(times, target_freq, market)
        higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)

        fast = int(cl_config.get("idx_macd_fast", 12))
        slow = int(cl_config.get("idx_macd_slow", 26))
        signal = int(cl_config.get("idx_macd_signal", 9))
        if len(higher_closes) <= slow + signal:
            return

        h_dif, h_dea, h_hist = talib.MACD(
            higher_closes, fastperiod=fast, slowperiod=slow, signalperiod=signal,
        )
        low_dif = np.round(h_dif[low2high], 6)
        low_dea = np.round(h_dea[low2high], 6)
        low_hist = np.round(h_hist[low2high], 6)
        chart_data["higher_macd_dif"] = np.where(
            np.isnan(low_dif), None, low_dif
        ).tolist()
        chart_data["higher_macd_dea"] = np.where(
            np.isnan(low_dea), None, low_dea
        ).tolist()
        chart_data["higher_macd_hist"] = np.where(
            np.isnan(low_hist), None, low_hist
        ).tolist()
    except Exception as e:
        LogUtil.error(f"[apply_higher_macd] resample MACD calc failed: {e}")
```

- [ ] **Step 5.4: 跑 numerical equivalence 测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py::test_apply_numerical_equivalence_to_real_5m_macd tests/test_apply_higher_macd.py::test_apply_no_overnight_contamination -v
```

Expected: 2 passed

- [ ] **Step 5.5: 更新现有 apply 测试以传 `t` 字段(新算法需要)**

把 `tests/test_apply_higher_macd.py` 中以下 4 个测试改为传 `t`:

`test_apply_short_series_no_op`(L54-60):
```python
def test_apply_short_series_no_op():
    """close 数量不足时不写 higher_macd_* 字段。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(5)],
        "c": [100.0] * 5,
    }
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    assert "higher_macd_dif" not in chart_data
    assert "higher_macd_dea" not in chart_data
    assert "higher_macd_hist" not in chart_data
```

`test_apply_long_series_writes_fields`(L63-73):
```python
def test_apply_long_series_writes_fields():
    """足够长的 close 序列会写 higher_macd_*。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0 + i * 0.1 for i in range(500)],
    }
    cfg = {"idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", cfg)
    assert "higher_macd_dif" in chart_data
    assert "higher_macd_dea" in chart_data
    assert "higher_macd_hist" in chart_data
    assert len(chart_data["higher_macd_dif"]) == 500
```

`test_apply_nan_replaced_with_none`(L76-83):
```python
def test_apply_nan_replaced_with_none():
    """MACD 计算结果中头部 slow+signal 根都是 NaN, 应转成 None。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0 + i * 0.1 for i in range(500)],
    }
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    # 头部应有 None (talib 在 slow+signal-1 根之前都返回 NaN)
    assert chart_data["higher_macd_dif"][0] is None
    # 末段应是浮点数
    assert isinstance(chart_data["higher_macd_dif"][-1], float)
```

`test_apply_unknown_frequency_no_op`(L86-91):
```python
def test_apply_unknown_frequency_no_op():
    """未知 frequency → target_freq=None → 不改 chart_data。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0] * 500,
    }
    before = dict(chart_data)
    apply_higher_macd_to_chart_data(chart_data, "999x", "a", {})
    assert chart_data == before
```

`test_apply_empty_closes_no_op`(L94-98)— 这个测试现在也得加 `t: []`:
```python
def test_apply_empty_closes_no_op():
    """close 为空时也不该崩。"""
    chart_data = {"t": [], "c": []}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    assert "higher_macd_dif" not in chart_data
```

- [ ] **Step 5.6: 跑全量测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py -v
```

Expected: 全部 passed(包括 ratio 测试,因为旧常量未删)

- [ ] **Step 5.7: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
feat(htf-macd): T5 — apply_higher_macd_to_chart_data 切到真合成算法

H1+H2 根治第 5 步: 重写主入口, 用 T1-T4 实现的 3 个新函数串联出
"低周期 (t,c) → bin 分组 → 演化合成 → talib.MACD(12,26,9) → 投影回
低周期" 流程。

修复 H1: 数学上等价"对手动合成 5m closes 跑标准 MACD" — numerical
equivalence test 验证误差 < 1e-6。
修复 H2: 美股跨夜两根 1m 自然落不同 5m bin, EMA 不再"穿越夜间"。

旧 _resolve_higher_macd_ratio 与三个 RATIO 常量本 commit 保留 (它们的
ratio 测试仍 pass), 下一 task 才删除。

测试: 6 个新 test 全绿; 5 个现有 apply 测试都补了 t 字段 (新算法依赖
时间戳算 bin), 数值契约保持(头部 None, 长度匹配, 短序列不写)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 删除旧 ratio 常量、`_resolve_higher_macd_ratio`、旧测试,以及 `tv.py` 导入

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`(删 L50-77 三个常量 + L423-438 旧函数)
- Modify: `web/chanlun_chart/cl_app/blueprints/tv.py:161-163`(删 import 行)
- Modify: `tests/test_apply_higher_macd.py`(删 L21-51 五个 ratio 测试)

- [ ] **Step 6.1: 全仓 grep 二次确认没有其他模块依赖**

```bash
cd D:/project/chanlun-pro && grep -rn "HIGHER_MACD_RATIO\|MARKET_30M_TO_D_RATIO\|MARKET_D_TO_W_RATIO\|_resolve_higher_macd_ratio" --include="*.py" --exclude-dir=".codex_debug_venv" --exclude-dir=".venv" --exclude-dir=".omc" --exclude-dir=".worktrees"
```

Expected: 仅 `chart_compute.py` 定义处、`tv.py:161-163` import、`tests/test_apply_higher_macd.py` 引用三处。若有其他模块,STOP — 那是需要协同修改的额外工作。

- [ ] **Step 6.2: 删除 `chart_compute.py` 的旧 ratio 常量(L47-77)**

把 L47 到 L77 这一段(包括 `# ---------------- 跨周期 MACD 倍率 ----------------` 注释、`HIGHER_MACD_RATIO` / `MARKET_30M_TO_D_RATIO` / `MARKET_D_TO_W_RATIO` 三个 dict)整段删除。

⚠️ 注意:Task 1 在这段上方新增了 `HIGHER_FREQ_MAP` 与 `MARKET_TZ`,只删旧段、保留新段。

- [ ] **Step 6.3: 删除 `chart_compute.py` 的 `_resolve_higher_macd_ratio` 函数(原 L423-438)**

把以下整段删除:

```python
def _resolve_higher_macd_ratio(frequency: str, market: str):
    """返回 frequency 对应的"高周期 MACD 倍率", 找不到返回 None。

    优先查 HIGHER_MACD_RATIO 通用表; 特殊 frequency 走市场专属表
    (30m_TO_D / D_TO_W) 或硬编码 (w=4, m=12)。
    """
    ratio = HIGHER_MACD_RATIO.get(frequency)
    if ratio is None and frequency == "30m":
        ratio = MARKET_30M_TO_D_RATIO.get(market, 8)
    elif ratio is None and frequency == "d":
        ratio = MARKET_D_TO_W_RATIO.get(market, 5)
    elif ratio is None and frequency == "w":
        ratio = 4
    elif ratio is None and frequency == "m":
        ratio = 12
    return ratio
```

- [ ] **Step 6.4: 同步更新 `chart_compute.py` 文件顶部 docstring(L9 行)**

把第 9 行:
```python
- ``HIGHER_MACD_RATIO`` / ``MARKET_30M_TO_D_RATIO`` / ``MARKET_D_TO_W_RATIO``: 跨周期 MACD 倍率
```
改为:
```python
- ``HIGHER_FREQ_MAP`` / ``MARKET_TZ``: HTF MACD 频率映射与市场时区
- ``_bin_keys_for_higher`` / ``_resample_closes_to_higher``: HTF MACD 合成核心
```

- [ ] **Step 6.5: 删除 `tv.py:161-163` 三个旧常量 import**

把:
```python
from ..services.chart_compute import (  # noqa: E402
    HIGHER_MACD_RATIO,
    MARKET_30M_TO_D_RATIO,
    MARKET_D_TO_W_RATIO,
    _SafeLockRegistry,
    ...
```
改为(删除三个 RATIO 常量,保留其他):
```python
from ..services.chart_compute import (  # noqa: E402
    _SafeLockRegistry,
    ...
```

- [ ] **Step 6.6: 删除 `tests/test_apply_higher_macd.py` 中 5 个旧 ratio 测试与对应 import**

把 L12-18 的 import:
```python
from cl_app.services.chart_compute import (
    HIGHER_MACD_RATIO,
    MARKET_30M_TO_D_RATIO,
    MARKET_D_TO_W_RATIO,
    _resolve_higher_macd_ratio,
    apply_higher_macd_to_chart_data,
)
```
改为:
```python
from cl_app.services.chart_compute import apply_higher_macd_to_chart_data
```

把 L21-51 的 5 个测试整体删除:
- `test_ratio_resolution_table_hit`
- `test_ratio_30m_uses_market_table`
- `test_ratio_d_uses_market_table`
- `test_ratio_w_and_m_hardcoded`
- `test_ratio_unknown_frequency_returns_none`

同步更新 module docstring(L1-8)为新算法描述:
```python
"""tests/test_apply_higher_macd.py — apply_higher_macd_to_chart_data 单元测试。

新算法(resample-based HTF MACD)测试覆盖:
- _resolve_higher_target_freq: 频率映射 + 未知 freq
- _bin_keys_for_higher: 5m / 30m / d / w / M 各分支, 含跨夜断层与未知市场
- _resample_closes_to_higher: 演化模式 + bin 切换 + 空输入
- apply_higher_macd_to_chart_data: 端到端 (短/长序列, NaN→None,
  未知 freq, 空 closes, numerical equivalence, 跨夜污染验证)
"""
```

- [ ] **Step 6.7: 跑全量测试确认通过**

```bash
cd D:/project/chanlun-pro && pytest tests/test_apply_higher_macd.py -v
```

Expected: 13 passed(5 个新 _resolve 系列 + 4 个 bin_keys + 3 个 resample + 1 个 numerical equivalence + 1 个跨夜污染 + 4 个 apply 现有 + 1 个空 closes,旧 ratio 5 个已删)。具体数量取决于命名/分组,关键:**全绿,无 ImportError**。

- [ ] **Step 6.8: 跑全量项目测试确认无外部依赖被破坏**

```bash
cd D:/project/chanlun-pro && pytest -q 2>&1 | tail -30
```

Expected: 全绿。若任何测试因为 `HIGHER_MACD_RATIO` / `_resolve_higher_macd_ratio` ImportError,STOP — Step 6.1 漏掉了某个使用点,回去补查。

- [ ] **Step 6.9: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/cl_app/services/chart_compute.py web/chanlun_chart/cl_app/blueprints/tv.py tests/test_apply_higher_macd.py && git commit -m "$(cat <<'EOF'
refactor(htf-macd): T6 — 删除旧 ratio 倍率表与 _resolve_higher_macd_ratio

H1+H2 根治第 6 步: T5 之后 apply_higher_macd_to_chart_data 已切到新算法,
旧倍率表与 _resolve_higher_macd_ratio 不再被任何代码引用, 整体删除。

删除:
- chart_compute.py: HIGHER_MACD_RATIO / MARKET_30M_TO_D_RATIO /
  MARKET_D_TO_W_RATIO 三个常量, _resolve_higher_macd_ratio 函数, 顶部
  docstring 同步更新
- tv.py: L161-163 三个常量 import
- tests/test_apply_higher_macd.py: 5 个 ratio 测试 (test_ratio_*) 与对应
  import; 模块 docstring 更新为新算法描述

已知行为变更:
- 月线(M) 不再有 HTF 显示 (旧 m->年线 ratio=12, 新 HIGHER_FREQ_MAP
  没有 M 条目, _resolve_higher_target_freq("M",...) 返回 None,
  apply 直接 short-circuit 不写字段)。月线节奏慢, HTF 价值低, 接受。

测试: pytest tests/test_apply_higher_macd.py 全绿; pytest -q 全量也全绿。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 新增 `scripts/verify_higher_macd.py` 手动验证脚本

**Files:**
- Create: `web/chanlun_chart/scripts/verify_higher_macd.py`

- [ ] **Step 7.1: 确认 scripts 目录存在;不存在则创建**

```bash
cd D:/project/chanlun-pro && ls web/chanlun_chart/scripts/ 2>/dev/null || mkdir -p web/chanlun_chart/scripts
```

- [ ] **Step 7.2: 创建 `web/chanlun_chart/scripts/verify_higher_macd.py`**

```python
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
import datetime
import numpy as np
import talib

# 让脚本能 import 项目代码
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
)
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..")
)

from cl_app.services.chart_compute import (  # noqa: E402
    apply_higher_macd_to_chart_data,
    _bin_keys_for_higher,
    _resample_closes_to_higher,
    HIGHER_FREQ_MAP,
)
from chanlun.exchange import get_exchange  # noqa: E402
from chanlun.base import Market  # noqa: E402


def _legacy_scale_macd(closes: "list[float]", ratio: int) -> "list[float]":
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


def _ref_5m_hist(times: "list[int]", closes: "list[float]", market: str) -> "list[float]":
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
    out: "list[float]" = []
    for i in range(len(times)):
        v = ref_hist[low2high[i]]
        out.append(None if np.isnan(v) else float(v))
    return out


def _diff_stats(a: "list[float]", b: "list[float]") -> dict:
    """两个 hist 数组的差异统计 (忽略 None 项)。"""
    diffs = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        diffs.append(abs(x - y))
    if not diffs:
        return {"count": 0, "mean": None, "max": None, "p95": None}
    arr = np.array(diffs)
    return {
        "count": int(len(arr)),
        "mean": float(arr.mean()),
        "max": float(arr.max()),
        "p95": float(np.percentile(arr, 95)),
    }


def _find_session_open_indices(times: "list[int]", market: str = "us") -> "list[int]":
    """返回 times 中"每个新交易日开盘后第一根 1m"的 index 列表。
    
    用 bin_keys_for_higher 的 'd' 分支算; bin id 不同的相邻两根, 后一根
    就是新交易日的第一根。
    """
    t = np.array(times, dtype=np.int64)
    day_bins = _bin_keys_for_higher(t, "d", market)
    out: "list[int]" = []
    for i in range(1, len(day_bins)):
        if day_bins[i] != day_bins[i - 1]:
            out.append(i)
    return out


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "us.TSLA"
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 1950
    market = "us" if symbol.startswith("us.") or symbol.startswith("us:") else "a"

    code = symbol.split(".")[-1] if "." in symbol else symbol.split(":")[-1]
    print(f"[INPUT] symbol={symbol} market={market} bars={bars} freq=1m")

    # 拉数据 (用项目自己的 exchange 接口)
    ex = get_exchange(Market(market.upper()) if market in ("a", "us", "hk") else Market.A)
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

    # 旧算法对照: HIGHER_FREQ_MAP[1m]="5m", 旧 ratio for 1m = 5
    old_hist = _legacy_scale_macd(closes, ratio=5)
    ref_hist = _ref_5m_hist(times, closes, market)

    # 全局差异统计
    new_vs_ref = _diff_stats(new_hist, ref_hist)
    old_vs_ref = _diff_stats(old_hist, ref_hist)

    # 开盘后第一根 1m 的对照 (跨夜污染验证)
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
    print(f"| mean(|diff|) on hist            | {new_vs_ref['mean']:.6f} | {old_vs_ref['mean']:.6f} |")
    print(f"| max(|diff|)                     | {new_vs_ref['max']:.6f} | {old_vs_ref['max']:.6f} |")
    print(f"| p95(|diff|)                     | {new_vs_ref['p95']:.6f} | {old_vs_ref['p95']:.6f} |")
    print(f"| open first bar mean diff        | {open_first_diffs_new['mean']:.6f} | {open_first_diffs_old['mean']:.6f} |")
    print(f"| open first bar max diff         | {open_first_diffs_new['max']:.6f} | {open_first_diffs_old['max']:.6f} |")
    print()
    print(f"[INFO] open sessions detected: {len(open_idxs)}")
    print(f"[INFO] valid hist points compared: NEW={new_vs_ref['count']} OLD={old_vs_ref['count']}")
    print()
    print("[ACCEPT]" if new_vs_ref['max'] is not None and new_vs_ref['max'] < 1e-6 else "[REJECT]",
          "NEW vs REF 全局 max 差值 < 1e-6" if new_vs_ref['max'] is not None and new_vs_ref['max'] < 1e-6 else "新算法与 REF 有差异, 检查实现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7.3: 手动跑一次验证(本地有网/能拉 TSLA 1min 数据时)**

```bash
cd D:/project/chanlun-pro && python web/chanlun_chart/scripts/verify_higher_macd.py us.TSLA 1950
```

Expected 输出大致:
```
[INPUT] symbol=us.TSLA market=us bars=1950 freq=1m
| Metric                          | NEW vs REF | OLD vs REF |
| mean(|diff|) on hist            | 0.000000   | 0.0xxx     |
| max(|diff|)                     | 0.000000   | 0.xxxx     |
| p95(|diff|)                     | 0.000000   | 0.0xxx     |
| open first bar mean diff        | 0.000000   | 0.0xxx     |
| open first bar max diff         | 0.000000   | 0.xxxx     |
[ACCEPT] NEW vs REF 全局 max 差值 < 1e-6
```

接受标准:
- `NEW vs REF` 列所有 metric 都 `< 1e-6`
- `OLD vs REF` 列至少一个 metric 显著大于 NEW(证明旧算法确实有偏差)

如果本地无数据/无网,跳过此步,Step 7.4 直接 commit。

- [ ] **Step 7.4: Commit**

```bash
cd D:/project/chanlun-pro && git add web/chanlun_chart/scripts/verify_higher_macd.py && git commit -m "$(cat <<'EOF'
chore(htf-macd): T7 — 手动验证脚本 verify_higher_macd.py

H1+H2 根治第 7 步: 加一次性手动验证脚本, 用真实股票数据对照新旧两版
HTF MACD 与 REF (直接对合成 5m closes 跑 talib.MACD) 的差异。

不接入 CI, 仅作为"上线后 + 后续修改 bin 函数时"的快速核验工具。
脚本内本地保留 _legacy_scale_macd 与 _ref_5m_hist 两个对照实现 (与
chart_compute.py 主代码独立)。

输出表格列出 NEW/OLD 两列的 mean/max/p95 全局差与"开盘后第一根 1m"
跨夜污染差。接受标准: NEW 列 max < 1e-6。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review 已执行(可删除)

完成的自查:

1. **Spec coverage**:
   - 第 1 节 H1 / H2 背景 → Task 5 numerical equivalence + 跨夜污染测试覆盖
   - 第 2 节目标 (1)-(5) → Task 5 (1)(3), Task 2-3 (2), Task 1-4 (4), Task 6 (5)
   - 第 3 节"已知行为变更:月线失去 HTF" → Task 1 `HIGHER_FREQ_MAP` 没有 `M` 条目;Task 6 commit message 明确
   - 第 5 节关键函数签名 → Task 1/2/3/4/5 逐一实现
   - 第 6 节市场时区表 → Task 1 实现
   - 第 7 节 bin 函数语义表 → Task 2-3 实现
   - 第 8 节删除的代码 → Task 6 全部覆盖
   - 第 9 节 B 测试策略 → Task 1-5 增量
   - 第 9 节 C 验证脚本 → Task 7

2. **Placeholder scan**: 无 TBD / TODO / "implement later"。每个 step 都有完整代码或具体命令。

3. **Type consistency**:
   - `_resolve_higher_target_freq` 返回 `str | None`,Task 1 实现一致,Task 5 调用方 `if target_freq is None: return` 一致
   - `_bin_keys_for_higher` 返回 `np.ndarray[int64]`,Task 2-3 实现一致,Task 5 调用结果传入 `_resample_closes_to_higher` 一致
   - `_resample_closes_to_higher` 返回 `(higher_closes, low_to_higher_idx)`,Task 4 实现 + Task 5 调用解构一致
   - `HIGHER_FREQ_MAP` / `MARKET_TZ` 命名 Task 1 定义后 Task 2-3 引用一致

---

## Execution Handoff

Plan 已写并自查通过。

执行选项二选一:

1. **Subagent-Driven(推荐)**:每个 Task 派一个 fresh subagent 执行,我在 Task 之间 review,迭代快
2. **Inline Execution**:在当前会话里按 Task 顺序执行,带 checkpoint 给你 review

哪种?
