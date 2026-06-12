# 多级别中枢叠加（混合方案）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 低周期图(1m/5m)叠加真实高周期(5m/30m)的 L1 线段中枢,让用户在低周期图看到缠论多级别结构。

**Architecture:** 后端 `apply_higher_zs_to_chart_data`(照 `apply_higher_macd` 范式)取高周期 K 线→新核心 `get_recursive_branch_levels`→取 L1 中枢→写 `chart_data['higher_zs']`;前端 `charts.js` 复用 `recursive_zss` 渲染机制叠加;时间靠绝对时间戳天然对齐。

**Tech Stack:** Python(flask/poetry/pytest)、缠论新核心、TradingView datafeed(TS+bundle.js)、charts.js。

参考 spec:`docs/chanlun_core_redesign_7_多周期中枢叠加_design.md`

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `src/chanlun/cl_utils.py` | core/web 共享工具 | 抽 `zs_to_chart_dict` 模块级 + `_zs_to_chart` 改调它;`query_cl_chart_config` 加配置 |
| `web/.../services/chart_compute.py` | web 多周期图表逻辑 | `higher_zs_periods` + `_higher_zs_for_period` + `apply_higher_zs_to_chart_data` + 透传 + 预热接入 |
| `web/.../blueprints/tv.py` | tv_history 请求路径 | 接入 `apply_higher_zs` |
| `web/.../static/datafeeds/udf/src/history-provider.ts` + `dist/bundle.js` | datafeed | `bars_result` 接收 `higher_zs` |
| `web/.../static/js/charts.js` | 前端渲染 | 注册容器 + 渲染 + 配色 + UI 开关 |
| `tests/test_higher_zs.py` | 测试 | 单元 + 集成 |

---

### Task 1: 抽 `zs_to_chart_dict` 为 cl_utils 模块级函数

**Files:**
- Modify: `src/chanlun/cl_utils.py`（`_zs_to_chart` 当前在 `cl_data_to_tv_chart` 内嵌,约 line 856-882）
- Test: `tests/core/test_zs_to_chart_dict.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_zs_to_chart_dict.py
from chanlun.cl_utils import zs_to_chart_dict


def test_zs_to_chart_dict_structure(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(600, multi_freq=False)
    zss = cd.get_bi_zss("zs_type_bz")
    assert zss, "合成数据应有笔中枢"
    d = zs_to_chart_dict(zss[0])
    assert isinstance(d["points"], list) and len(d["points"]) == 2
    assert all("time" in p and "price" in p for p in d["points"])
    assert d["linestyle"] in ("0", "1")
    assert "type" in d and "is_expanded" in d and "sub_count" in d


def test_zs_to_chart_dict_envelope_wider(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(600, multi_freq=False)
    zss = cd.get_bi_zss("zs_type_bz")
    z = zss[0]
    core = zs_to_chart_dict(z, use_envelope=False)
    env = zs_to_chart_dict(z, use_envelope=True)
    # 包络高点 >= 核心高点, 包络低点 <= 核心低点
    assert env["points"][0]["price"] >= core["points"][0]["price"]
    assert env["points"][1]["price"] <= core["points"][1]["price"]
```

- [ ] **Step 2: 运行验证失败**

Run: `poetry run pytest tests/core/test_zs_to_chart_dict.py -v`
Expected: FAIL（`ImportError: cannot import name 'zs_to_chart_dict'`）

- [ ] **Step 3: 抽模块级函数**

在 `src/chanlun/cl_utils.py` 模块级（`cl_data_to_tv_chart` 函数定义**之前**,与其他模块级 helper 一起）新增:

```python
def zs_to_chart_dict(zs, use_envelope: bool = False) -> dict:
    """把 ZS 中枢序列化为前端图表 dict(core/web 共享,多周期叠加复用)。

    - points: 默认中枢核心区 [ZD,ZG]; use_envelope=True 用 [DD,GG] 包络
      (递归 L≥1 高级中枢 / 扩展中枢 / 多周期叠加需表达「瞬间波动」)。
    - linestyle: done→"0"(实线) / 未完成→"1"(虚线)。
    - type: 中枢方向(up/down/zd); is_expanded/sub_count: 扩展中枢标记。
    """
    hi = zs.gg if use_envelope else zs.zg
    lo = zs.dd if use_envelope else zs.zd
    return {
        "points": [
            {
                "time": fun.datetime_to_int(zs.start.end.k.date) if zs.start else fun.datetime_to_int(zs.lines[0].start.k.date),
                "price": hi,
            },
            {
                "time": fun.datetime_to_int(zs.end.start.k.date) if zs.end else fun.datetime_to_int(zs.lines[-1].end.k.date),
                "price": lo,
            },
        ],
        "linestyle": "0" if zs.done else "1",
        "type": zs.type,
        "is_expanded": bool(getattr(zs, "expanded_with", [])),
        "sub_count": len(getattr(zs, "expanded_with", []) or []),
    }
```

- [ ] **Step 4: `cl_data_to_tv_chart` 内嵌 `_zs_to_chart` 改为 thin wrapper**

把内嵌的 `def _zs_to_chart(zs, use_envelope=False):` 整个函数体替换为:

```python
    def _zs_to_chart(zs, use_envelope: bool = False) -> dict:
        return zs_to_chart_dict(zs, use_envelope)
```

(保留内嵌名以最小化其余调用点改动。)

- [ ] **Step 5: 运行测试通过 + 回归**

Run: `poetry run pytest tests/core/test_zs_to_chart_dict.py tests/core/test_cl_data_to_tv_chart_zs.py -v`
Expected: PASS（新测试过 + 现有中枢图表测试不破）

- [ ] **Step 6: Commit**

```bash
git add src/chanlun/cl_utils.py tests/core/test_zs_to_chart_dict.py
git commit -m "refactor(cl_utils): 抽 zs_to_chart_dict 模块级供多周期叠加复用(P7-T1)"
```

---

### Task 2: `higher_zs_periods` 周期阶梯映射

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`（`HIGHER_FREQ_MAP` 定义在 line 82 附近）
- Test: `tests/test_higher_zs.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_higher_zs.py
from cl_app.services.chart_compute import higher_zs_periods


def test_higher_zs_periods_1m():
    assert higher_zs_periods("1m") == [("5m", "5min级别"), ("30m", "30min级别")]


def test_higher_zs_periods_5m():
    assert higher_zs_periods("5m") == [("30m", "30min级别")]


def test_higher_zs_periods_30m_empty():
    assert higher_zs_periods("30m") == []


def test_higher_zs_periods_d_empty():
    assert higher_zs_periods("d") == []
```

- [ ] **Step 2: 运行验证失败**

Run: `poetry run pytest tests/test_higher_zs.py -v`
Expected: FAIL（`ImportError: cannot import name 'higher_zs_periods'`）

- [ ] **Step 3: 实现**

在 `chart_compute.py` 中 `HIGHER_FREQ_MAP` 定义之后新增:

```python
# 多周期中枢叠加:叠加目标周期(MVP 各市场统一)+ 周期→级别展示名
HIGHER_ZS_TARGET = "30m"
_PERIOD_LEVEL_NAME = {
    "5m": "5min级别", "30m": "30min级别", "d": "日线级别", "w": "周线级别",
}


def _zs_level_name(period: str) -> str:
    return _PERIOD_LEVEL_NAME.get(period, f"{period}级别")


def higher_zs_periods(frequency: str):
    """返回当前周期之上、沿阶梯到 HIGHER_ZS_TARGET(含)途经的 [(周期, 级别名)]。

    当前周期 >= 目标(从它沿 HIGHER_FREQ_MAP 走不到 target) → 返回 []。
    例: '1m'→[('5m','5min级别'),('30m','30min级别')]; '5m'→[('30m','30min级别')];
        '30m'/'d'→[]。
    """
    out = []
    cur = frequency
    seen = set()
    while cur in HIGHER_FREQ_MAP and cur not in seen:
        seen.add(cur)
        nxt = HIGHER_FREQ_MAP[cur]
        out.append((nxt, _zs_level_name(nxt)))
        if nxt == HIGHER_ZS_TARGET:
            return out
        cur = nxt
    return []
```

- [ ] **Step 4: 运行测试通过**

Run: `poetry run pytest tests/test_higher_zs.py -v`
Expected: PASS（4 个用例全过）

- [ ] **Step 5: Commit**

```bash
git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_higher_zs.py
git commit -m "feat(chart_compute): higher_zs_periods 周期阶梯映射(目标30m)(P7-T2)"
```

---

### Task 3: `apply_higher_zs_to_chart_data` 多周期中枢计算

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`
- Test: `tests/test_higher_zs.py`

- [ ] **Step 1: 写失败测试（门控 + 组织 + 集成）**

追加到 `tests/test_higher_zs.py`:

```python
import numpy as np
import pandas as pd
from cl_app.services import chart_compute as CC


def _synth_df(n, slope=0.0, freq="1min"):
    t = np.arange(n, dtype=float)
    close = 100 + slope * t + 6 * np.sin(2 * np.pi * t / (n / 20.0)) + 2 * np.sin(2 * np.pi * t / (n / 200.0))
    rng = np.random.default_rng(7)
    close = close + rng.normal(0, 0.1, n)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01 09:30:00", periods=n, freq=freq),
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1000.0,
    })


def test_apply_higher_zs_gated_off():
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {"chart_show_higher_zs": "0"}) is False
    assert "higher_zs" not in cd


def test_apply_higher_zs_high_period_empty():
    cd = {"t": [1, 2, 3]}
    assert CC.apply_higher_zs_to_chart_data(cd, "a", "X", "30m", {}) is False
    assert "higher_zs" not in cd


def test_apply_higher_zs_organizes(monkeypatch):
    # monkeypatch 单周期取数, 验证组织逻辑(1m→两级, 字段结构)
    monkeypatch.setattr(CC, "_higher_zs_for_period",
                        lambda market, code, hf, cfg: [{"points": [], "type": "zd"}])
    cd = {"t": [1, 2, 3]}
    ok = CC.apply_higher_zs_to_chart_data(cd, "a", "X", "1m", {})
    assert ok is True
    assert [g["period"] for g in cd["higher_zs"]] == ["5m", "30m"]
    assert [g["level_name"] for g in cd["higher_zs"]] == ["5min级别", "30min级别"]
    assert all(isinstance(g["zss"], list) for g in cd["higher_zs"])


def test_higher_zs_for_period_real(monkeypatch):
    # monkeypatch ex.klines 返回合成趋势 df, 真实跑新核心取 L1 中枢
    class _Ex:
        def klines(self, code, freq, **kw):
            return _synth_df(5000, slope=0.01)
    monkeypatch.setattr(CC, "get_exchange", lambda m: _Ex())
    zss = CC._higher_zs_for_period("a", "X", "5m", {"zs_bi_type": ["zs_type_bz"]})
    assert isinstance(zss, list)  # 可能空(数据不足L1), 但不报错且结构正确
    for z in zss:
        assert "points" in z and "linestyle" in z
```

- [ ] **Step 2: 运行验证失败**

Run: `poetry run pytest tests/test_higher_zs.py -k apply_higher_zs -v`
Expected: FAIL（`AttributeError: module ... has no attribute 'apply_higher_zs_to_chart_data'`）

- [ ] **Step 3: 实现**

在 `chart_compute.py` 顶部 import 区加 `from chanlun.cl_utils import web_batch_get_cl_datas, zs_to_chart_dict`（`web_batch_get_cl_datas` 已 import,补 `zs_to_chart_dict`）。在 `apply_higher_macd_to_chart_data` 附近新增:

```python
def _higher_zs_for_period(market: str, code: str, hf: str, cl_config: dict) -> list:
    """取 hf 周期 K 线→新核心→L1 线段中枢→zs_to_chart_dict 列表。

    异常/无数据/无 L1 → 返回 [](优雅降级,不阻断主图)。
    """
    try:
        ex = get_exchange(Market(market))
        klines = ex.klines(code, hf)
        if klines is None or len(klines) == 0:
            return []
        cd = web_batch_get_cl_datas(market, code, {hf: klines}, cl_config)[0]
        levels = cd.get_recursive_branch_levels() or []
        l1 = next((lv for lv in levels if lv.level == 1), None)
        if l1 is None:
            return []
        return [zs_to_chart_dict(zs, use_envelope=True) for zs in l1.zss]
    except Exception as e:
        LogUtil.error(f"[apply_higher_zs] period={hf} code={code} failed: {e}")
        return []


def apply_higher_zs_to_chart_data(
    chart_data: dict, market: str, code: str, frequency: str, cl_config: dict
) -> bool:
    """低周期图叠加更高真实周期的 L1 线段中枢,in-place 写 chart_data['higher_zs']。

    返回是否写入(高周期图/配置关→False)。单级取数失败该级为空,不阻断其他级。
    """
    if cl_config.get("chart_show_higher_zs", "1") != "1":
        return False
    periods = higher_zs_periods(frequency)
    if not periods:
        return False
    result = []
    for hf, level_name in periods:
        result.append({
            "period": hf,
            "level_name": level_name,
            "zss": _higher_zs_for_period(market, code, hf, cl_config),
        })
    chart_data["higher_zs"] = result
    return True
```

- [ ] **Step 4: 运行测试通过**

Run: `poetry run pytest tests/test_higher_zs.py -v`
Expected: PASS（门控/组织/集成全过）

- [ ] **Step 5: Commit**

```bash
git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_higher_zs.py
git commit -m "feat(chart_compute): apply_higher_zs 取高周期L1中枢写chart_data(P7-T3)"
```

---

### Task 4: chart_data 透传（合并/切窗/裁剪保留 higher_zs）

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`（`_merge_chart_data` line 253、`slice_chart_data_to_window` line 420、`trim_future_bars` line 458）
- Test: `tests/test_higher_zs.py`

- [ ] **Step 1: 写失败测试**

追加:

```python
def test_higher_zs_passthrough_slice_trim():
    hz = [{"period": "5m", "level_name": "5min级别", "zss": []}]
    cd = {"t": [100, 200, 300], "o": [1, 2, 3], "h": [1, 2, 3],
          "l": [1, 2, 3], "c": [1, 2, 3], "v": [1, 2, 3], "higher_zs": hz}
    sliced = CC.slice_chart_data_to_window(cd, 100, 300)
    assert sliced["higher_zs"] == hz          # 整体透传, 不按窗口裁切
    trimmed = CC.trim_future_bars(cd, 250)
    assert trimmed["higher_zs"] == hz
```

- [ ] **Step 2: 运行验证失败**

Run: `poetry run pytest tests/test_higher_zs.py -k passthrough -v`
Expected: FAIL（`KeyError: 'higher_zs'`,切窗结果不含该字段）

- [ ] **Step 3: 三处透传集合加 `higher_zs`**

`_merge_chart_data`（约 line 253）:
```python
    for key in ("recursive_levels", "interval_nest", "higher_zs"):
```
`slice_chart_data_to_window`（约 line 420）:
```python
    for field in ("recursive_levels", "interval_nest", "higher_zs"):
```
`trim_future_bars`（约 line 458）:
```python
    for field in ("recursive_levels", "interval_nest", "higher_zs"):
```

- [ ] **Step 4: 运行测试通过**

Run: `poetry run pytest tests/test_higher_zs.py -k passthrough -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/chanlun_chart/cl_app/services/chart_compute.py tests/test_higher_zs.py
git commit -m "feat(chart_compute): higher_zs 随 recursive_levels 整体透传(P7-T4)"
```

---

### Task 5: web 默认配置加 `chart_show_higher_zs`

**Files:**
- Modify: `src/chanlun/cl_utils.py`（`query_cl_chart_config` 的 `default_config`,约 line 461 `chart_show_recursive_levels` 附近）
- Test: `tests/core/test_cl_chart_config.py`（无则新建）

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_cl_chart_config.py
from chanlun.cl_utils import query_cl_chart_config


def test_default_config_has_higher_zs():
    cfg = query_cl_chart_config("a", "SH.000001")
    assert cfg.get("chart_show_higher_zs") == "1"
```

- [ ] **Step 2: 运行验证失败**

Run: `poetry run pytest tests/core/test_cl_chart_config.py -v`
Expected: FAIL（`assert None == "1"`）

- [ ] **Step 3: 加配置项**

在 `default_config` 中 `"chart_show_recursive_levels": "1",` 附近加:
```python
        "chart_show_higher_zs": "1",   # 低周期图叠加高周期线段中枢(混合多级别,默认开)
```

- [ ] **Step 4: 运行测试通过**

Run: `poetry run pytest tests/core/test_cl_chart_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/chanlun/cl_utils.py tests/core/test_cl_chart_config.py
git commit -m "feat(cl_utils): query_cl_chart_config 加 chart_show_higher_zs 默认开(P7-T5)"
```

---

### Task 6: 两条计算主路径接入 `apply_higher_zs`

**Files:**
- Modify: `web/chanlun_chart/cl_app/services/chart_compute.py`（`compute_and_cache_chart_data` line 326）
- Modify: `web/chanlun_chart/cl_app/blueprints/tv.py`（`tv_history` line 782）

> 无新单元测试(依赖 exchange/flask 上下文);正确性由 Task 3 的 apply_higher_zs 单测 + Task 9 真实出图验收覆盖。改动是在已验证的 helper 后加一行并列调用。

- [ ] **Step 1: 预热路径接入**

`chart_compute.py` line 326 `apply_higher_macd_to_chart_data(...)` **之后**加:
```python
    # P7: 多周期中枢叠加(低周期图叠加高周期 L1 线段中枢)
    apply_higher_zs_to_chart_data(cl_chart_data, market, code, frequency, cl_config)
```

- [ ] **Step 2: 请求路径接入**

`tv.py` line 782 `apply_higher_macd_to_chart_data(cl_chart_data, frequency, market, cl_config)` **之后**加:
```python
                # P7: 多周期中枢叠加
                apply_higher_zs_to_chart_data(cl_chart_data, market, code, frequency, cl_config)
```
并确认 `tv.py` 顶部从 `chart_compute` import 了 `apply_higher_zs_to_chart_data`（与 `apply_higher_macd_to_chart_data` 同处 import,补上）。

- [ ] **Step 3: 冒烟验证 import 与无语法错**

Run: `poetry run python -c "import sys; sys.path.insert(0,'web/chanlun_chart'); from cl_app.services.chart_compute import apply_higher_zs_to_chart_data; print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 4: Commit**

```bash
git add web/chanlun_chart/cl_app/services/chart_compute.py web/chanlun_chart/cl_app/blueprints/tv.py
git commit -m "feat(web): tv_history/预热两路径接入 apply_higher_zs(P7-T6)"
```

---

### Task 7: datafeed `bars_result` 接收 `higher_zs`

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts`（接口 + 3 处 bars_result 写入,line 56/120/466/614/658 附近）
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`（编译产物对应 3 处,line 311/411/451 附近）

> TS 不重新编译,手动同步 ts(源)与 bundle.js(浏览器实际加载),与现有 `recursive_levels` 同样维护方式。

- [ ] **Step 1: ts 接口加字段**

`history-provider.ts` 两个接口(`HistoryFullDataResponse`、`BarsResult` 一类,line 56/120 `recursive_levels?` 旁)各加:
```typescript
  higher_zs?: unknown[];
```

- [ ] **Step 2: ts 三处 bars_result 写入加字段**

line 466、614、658 三处 `recursive_levels: (response as HistoryFullDataResponse).recursive_levels || [],` 旁各加:
```typescript
          higher_zs: (response as HistoryFullDataResponse).higher_zs || [],
```
(line 614 是 `obj_res.higher_zs = (response as HistoryFullDataResponse).higher_zs || [];`)

- [ ] **Step 3: bundle.js 三处同步**

`bundle.js` line 311、411、451 三处 `recursive_levels: response.recursive_levels || [],` 旁各加:
```javascript
                        higher_zs: response.higher_zs || [],
```
(line 411 是 `obj_res.higher_zs = response.higher_zs || [];`)

- [ ] **Step 4: 校验 bundle.js 语法**

Run: `node --check web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js && echo OK`
Expected: 输出 `OK`

- [ ] **Step 5: Commit**

```bash
git add web/chanlun_chart/cl_app/static/datafeeds/udf/src/history-provider.ts web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js
git commit -m "feat(datafeed): bars_result 接收 higher_zs 字段(P7-T7)"
```

---

### Task 8: charts.js 渲染 higher_zs + UI 开关

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`（`CL_SHOW_DEFAULT` line 9、`CHART_TYPES` line 116、配色常量 line 145 区、`drawChartElements` line 1608 区、UI 模板 line 987 区、toggle keys line 1074 区）

> 纯前端,无 pytest;靠 `node --check` + Task 9 出图验收。复用已存在的 `wrapZs`/`reconcile`/`createZhongshuShape`。

- [ ] **Step 1: 默认配置加开关**

`CL_SHOW_DEFAULT`（line 9 `zs_bi: true, zs_xd: true, zs_recursive: true,`）追加:
```javascript
    higher_zs: true,
```

- [ ] **Step 2: 注册容器**

`CHART_TYPES`（line 121 `"recursive_zss",` 旁）追加:
```javascript
        // 多周期中枢叠加(低周期图叠加的高周期线段中枢)
        "higher_zss",
```

- [ ] **Step 3: 配色常量**

`RECURSIVE_LEVEL_COLORS` 定义之后追加:
```javascript
// 多周期叠加中枢按"第几个高周期"配色(5min级别→[0]、30min级别→[1]…),冷色系区分递归中枢。
const HIGHER_ZS_COLORS = ["#5C6BC0", "#00897B", "#7E57C2", "#3949AB"];
```

- [ ] **Step 4: drawChartElements 渲染**

`drawChartElements` 中 `recursive_zss` 那段 reconcile（line 1604-1608）**之后**加:
```javascript
        // 多周期中枢叠加(higher_zs):后端按高周期给 [{period, level_name, zss}]。
        // 扁平化(附 _gi 组序)后单 reconcile,按高周期序选色;级别越高框可略粗。
        const higherZss = [];
        (barsResult.higher_zs || []).forEach((grp, gi) => {
            if (!grp || !Array.isArray(grp.zss)) return;
            grp.zss.forEach(zs => higherZss.push({ ...zs, _gi: gi }));
        });
        this.reconcile('higher_zss', (cfg.higher_zs !== false) ? higherZss : [], from, symbolKey, (item) => {
            const color = HIGHER_ZS_COLORS[(item._gi || 0) % HIGHER_ZS_COLORS.length];
            return safeCreate(wrapZs(color, 2)(item), 'higher_zs');
        }, false);
```

- [ ] **Step 5: UI 开关**

「中枢」组 UI（line 987 `递归中枢` 那个 `<label>` 之后）加:
```javascript
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('higher_zs')}" ${cfg.higher_zs !== false ? 'checked' : ''} style="margin-right:4px; vertical-align:middle;">高周期中枢</label>
```
toggle keys（line 1074 `'zs_recursive',` 旁）加:
```javascript
                    'higher_zs',
```

- [ ] **Step 6: 校验语法**

Run: `node --check web/chanlun_chart/cl_app/static/js/charts.js && echo OK`
Expected: 输出 `OK`

- [ ] **Step 7: Commit**

```bash
git add web/chanlun_chart/cl_app/static/js/charts.js
git commit -m "feat(charts): 渲染 higher_zs 多周期叠加中枢+高周期中枢开关(P7-T8)"
```

---

### Task 9: 全回归 + 真实数据出图验收

**Files:** 无（验收）

- [ ] **Step 1: 全套 pytest**

Run: `poetry run pytest tests/ -q`
Expected: 全绿(新增 higher_zs 测试 + 现有全回归不破)

- [ ] **Step 2: ruff 净**

Run: `poetry run ruff check src/ tests/ web/chanlun_chart/cl_app/services/chart_compute.py`
Expected: `All checks passed!`

- [ ] **Step 3: 端到端 probe（本地,不入 git）**

写 `scripts_local/probe_higher_zs_chart.py`:用 `a_SZ_301004` 的 1m fixture + monkeypatch `ex.klines` 返回 5m/30m fixture,跑 `apply_higher_zs_to_chart_data`,打印 `chart_data['higher_zs']` 各级 period/level_name/中枢数。

Run: `PYTHONPATH=src poetry run python scripts_local/probe_higher_zs_chart.py`
Expected: 1m 图 higher_zs 含 `5m(5min级别)` 与 `30m(30min级别)` 两组,各组中枢数 ≥0、结构含 points。

- [ ] **Step 4: web 硬刷新人工审**

启动 web,打开 A 股 1m 图,硬刷新(Ctrl+Shift+R)。验收:
- 「中枢」组出现「高周期中枢」勾选框,默认勾选。
- 图上叠加 5min/30min 级别中枢(冷色框),位置随时间轴对齐。
- 切到 5m 图:叠加 30min 级别;切到 30m 图:无叠加(原生)。

- [ ] **Step 5: 最终 commit（如有 probe/文档微调）**

```bash
git add -A
git commit -m "test(P7): 多周期中枢叠加全回归+出图验收(P7-T9)"
```

---

## Self-Review

**Spec 覆盖:**
- ✅ 组件1 周期阶梯→Task 2;组件2 多周期计算→Task 3;组件3 时间对齐(绝对时间戳,无代码)→Task 3 zs_to_chart_dict 用 datetime_to_int;组件4 数据结构→Task 3;组件5 渲染→Task 8;组件6 UI→Task 8;组件7 缓存(config hash 自动失效)→Task 5 配置项纳入 cache_key;透传→Task 4;datafeed→Task 7;接入→Task 6;zs_to_chart_dict 抽取→Task 1。
- 测试:test_higher_zs_periods→Task 2;apply_higher_zs 集成→Task 3;全回归→Task 9。

**类型/命名一致性:**
- `zs_to_chart_dict(zs, use_envelope)` Task 1 定义,Task 3 调用,签名一致。
- `higher_zs_periods(frequency)→[(period, level_name)]` Task 2 定义,Task 3 消费,结构一致。
- `chart_data["higher_zs"] = [{period, level_name, zss}]` Task 3 产出,Task 4 透传、Task 7 datafeed、Task 8 渲染消费,字段名一致。
- `cfg.higher_zs` 开关 Task 8 默认/UI/渲染/toggle 一致。
- `chart_show_higher_zs` 配置 Task 5 默认、Task 3 门控一致。

**占位符扫描:** 无 TBD/TODO;每步含完整代码/命令/预期。

**边界:** MVP 单 `higher_zs` 总开关(per-period 动态开关留后续);高周期取数失败/无 L1 优雅降级为空;cache hit 不 lazy 补算(config hash 变→旧 cache 失效→miss 重算)。
