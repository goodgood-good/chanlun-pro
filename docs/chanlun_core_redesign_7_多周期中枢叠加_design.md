# 多级别中枢叠加（混合方案）设计

> 缠论核心重做 P7。低周期图叠加真实高周期的线段中枢，让用户看到多级别结构。
> 并存重做、复用新核心，不动旧链路。

## 目标

任何周期图上展示缠论的多级别中枢：
- **本周期**笔中枢(L0)、线段中枢(L1) —— 新核心 `get_recursive_branch_levels` 现成。
- **低周期图(1m/5m)** 额外叠加更高**真实周期**(5m/30m)的线段中枢。
- **高周期图(30m/d/w)** 用本周期原生中枢即可，不叠加。

## 背景：为什么是真实多周期，而非单周期递归升级

Phase 0 验证（`scripts_local/probe_phase0_*.py`、`spike_fix_*.py`）结论：

1. **单周期结构升级(recursive_branch)机制无 bug**。用"6 段涨跌交替、区间来回"的结构化数据实测，baseline 正确升出 L2 中枢。有真实高级别结构就升，符合原文。
2. **但符合原文 line8136"趋势中中枢之间必须绝对不存在重叠"(GG/DD 口径)**，1min 图升到 30min 级别(L3)需 ~10 万根 1m + 完整嵌套结构，实际市场给不出。
3. **中途的 cr_zdzg(放宽到 ZG/ZD 核心区)能升级但违背原文，已撤回**——它把原文判为"中枢扩展"的情况误当趋势。
4. **缠论走势同构**：真实 30m K 线的线段中枢 ≈ 1min 升 2 级的 L3 结果。取真实高周期数据只需几百根、秒出，是升级的高效等价实现。

故：低周期看高级别中枢，走真实多周期叠加，不靠单周期硬升。

## 级别映射 + 叠加规则

| 当前图 | 本周期原生(新核心) | 叠加(真实高周期线段中枢 L1) |
|---|---|---|
| 1m | 笔中枢(L0)、线段中枢=1min级别(L1) | 5min级别、30min级别 |
| 5m | 笔中枢、线段中枢=5min级别 | 30min级别 |
| 30m | 笔中枢、线段中枢=30min级别 | (原生) |
| d/w | 笔中枢、线段中枢 | (原生) |

**叠加规则**：沿周期阶梯从"当前周期的上一级"走到"叠加目标周期"，途经的每个周期都叠加。
- 阶梯：复用 `chart_compute.HIGHER_FREQ_MAP`（`1m→5m→30m→d→w→m→y`）。
- 叠加目标周期：A 股 `30m`（MVP 各市场统一；自定义阶梯/目标留扩展）。
- 当前周期 ≥ 目标 → 不叠加（高周期原生）。

## 架构

### 组件 1：周期阶梯映射（新纯函数）

**文件**：`web/chanlun_chart/cl_app/services/chart_compute.py`（与 `HIGHER_FREQ_MAP`、`apply_higher_macd_to_chart_data` 同处——属 web 图表的多周期逻辑层，避免 web→core 反向依赖）

```python
HIGHER_ZS_TARGET = "30m"   # 叠加目标周期(MVP 各市场统一)

def higher_zs_periods(frequency: str) -> List[Tuple[str, str]]:
    """返回当前周期之上、到 HIGHER_ZS_TARGET 为止、途经的 (周期, 级别名)。
    当前周期 >= 目标(在阶梯上不早于目标) → 返回 []。
    例: '1m' -> [('5m','5min级别'),('30m','30min级别')]; '5m' -> [('30m','30min级别')];
        '30m'/'d' -> []。
    """
```

实现：用同模块 `HIGHER_FREQ_MAP` 从 `frequency` 逐级 `next()`，收集直到（含）`HIGHER_ZS_TARGET`；起点本身 ≥ 目标（在阶梯上不早于目标，即从起点沿阶梯走不到目标）则返回 `[]`。级别名 = period 展示名 + "级别"（`5m→"5min级别"`、`30m→"30min级别"`）。

### 组件 2：多周期中枢计算（后端，照 `apply_higher_macd_to_chart_data` 范式）

**文件**：`web/chanlun_chart/cl_app/services/chart_compute.py`

```python
def apply_higher_zs_to_chart_data(
    chart_data: dict, market: str, code: str, frequency: str, cl_config: dict
) -> bool:
    """对低周期图,取每个更高真实周期的 K 线、跑新核心、取 L1 线段中枢,
    in-place 写入 chart_data['higher_zs']。返回是否写入。"""
```

逻辑：
1. `periods = higher_zs_periods(frequency)`；空则 return False（高周期图不叠加）。
2. 受配置门控：`if cl_config.get("chart_show_higher_zs","1") != "1": return False`。
3. 对每个 `(hf, level_name)`：
   - `klines = ex.klines(code, hf, end_date=now)`；空则跳过该级。
   - `cd_hf = web_batch_get_cl_datas(market, code, {hf: klines}, cl_config)[0]`。
   - `levels = cd_hf.get_recursive_branch_levels() or []`。
   - 取 `level==1`(线段中枢)的 `lv.zss`；空则该级为空列表(优雅降级)。
   - 转图表格式：把 `cl_data_to_tv_chart` 内嵌的 `_zs_to_chart` 抽成 `cl_utils` 模块级函数 `zs_to_chart_dict(zs, use_envelope=False)`(纯转换、core/web 共享)，原处改调它；`apply_higher_zs` import 复用并传 `use_envelope=True`。
   - append `{"period": hf, "level_name": level_name, "zss": [...]}`。
4. `chart_data["higher_zs"] = result`。

**调用位置**：`compute_and_cache_chart_data`、`fetch_klines_and_compute_cl_data` 中 `cl_data_to_tv_chart` 之后、写 cache 之前（与 `apply_higher_macd_to_chart_data` 并列）。

**取数复用**：`get_exchange(Market(market)).klines` 已支持任意周期；高周期数据量小(30m 几百根)。可经 `cl_object_cache` 缓存 cd_hf 避免重复算（MVP 可不缓存，先正确）。

### 组件 3：时间对齐（天然成立）

高周期中枢的 `points[].time` = 高周期 K 线 date 的**绝对 unix 时间戳**(`fun.datetime_to_int`)。TradingView 在当前图按绝对时间戳定位形态 → 高周期中枢自动落到对应低周期 K 线位置，**无需额外对齐逻辑**。需保证时间戳单位与现有 shape 一致(秒级)。

### 组件 4：数据结构

`chart_data["higher_zs"]`：
```python
[
  {"period": "5m",  "level_name": "5min级别",  "zss": [ {points,linestyle,type,...}, ... ]},
  {"period": "30m", "level_name": "30min级别", "zss": [ ... ]},
]
```
`zss` 元素 = `_zs_to_chart(zs, use_envelope=True)` 输出（与 `recursive_levels` 的 zss 同构，前端可复用渲染）。

透传：`chart_compute._merge_chart_data` / `slice_chart_data_to_window` / `trim_future_bars` 把 `higher_zs` 加入"整体透传"集合（同 `recursive_levels`，全局视角不按窗口裁切）。datafeed `history-provider.ts`(+dist/bundle.js) 在 `bars_result` 各路径加 `higher_zs: response.higher_zs || []`。

### 组件 5：叠加渲染（前端，复用 `recursive_zss` 机制）

**文件**：`web/chanlun_chart/cl_app/static/js/charts.js`

- `CHART_TYPES` 注册 `higher_zss` 容器。
- `drawChartElements`：扁平化 `barsResult.higher_zs` 各 period 的 `zss`(附 `_period`/`_level_name`)，单 reconcile `higher_zss`，复用 `wrapZs`/`createZhongshuShape`。按 period 分色(独立调色板 `HIGHER_ZS_COLORS`，与 `RECURSIVE_LEVEL_COLORS` 区分)、线宽随级别递增。
- 复用窗口过滤/`makeKey`(points 唯一)。

### 组件 6：UI（动态级别开关）

- `cl_show_config` 默认加 `higher_zs: true`。
- 「中枢」组 UI 动态生成各高周期开关：根据当前 `higher_zs` 的 `level_name` 渲染 checkbox(如「5min级别」「30min级别」)，per-period 控制显隐(存 `cl_show_config.higher_zs_<period>`，默认 true)。
- toggle 绑定沿用现有 `keys.forEach` 机制(动态 key)。

### 组件 7：缓存

- `cache_key` 已含 `_stable_hash(cl_config)`；新增 `chart_show_higher_zs` 配置项纳入 web 默认 config → 切配置自动失效。
- 高周期 cd 缓存：MVP 不做，先保证正确；性能不足再经 `cl_object_cache` 按 `(market,code,hf,config_hash)` 缓存。

## 配置项（web 默认 `query_cl_chart_config`）

```python
"chart_show_higher_zs": "1",   # 低周期图叠加高周期线段中枢(混合方案,默认开)
```

## 测试策略

- **单元** `tests/core/test_higher_zs_periods.py`：`higher_zs_periods` 各周期映射(1m→[5m,30m]、5m→[30m]、30m→[]、d→[])。
- **集成** `tests/.../test_apply_higher_zs.py`：用 fixture `a_SZ_301004`(已有 1m/5m/30m/d 四周期)模拟 `ex.klines`，验证 1m chart_data 的 `higher_zs` 含 5m/30m 两级、各级 zss 结构正确(points/level_name)；30m 图 `higher_zs` 为空。
- **回归**：全套 pytest 不破(新字段不影响现有)；datafeed/charts.js 改动经 `node --check`。
- **真实出图验收**：A 股 1m/5m 图人工审多级中枢叠加位置与级别命名。

## 风险 / 边界

- **性能**：低周期图多取 2 次 K 线 + 算缠论。高周期数据量小，可接受；不足时加 cd 缓存。
- **数据不足降级**：高周期 K 线少 → L1 中枢可能空 → 该级 `zss=[]`，前端不画(正常)。
- **时间戳单位**：确保 `higher_zs` points time 与现有 shape 同为秒级(`fun.datetime_to_int`)。
- **`ex.klines` 失败**：单个高周期取数异常时跳过该级、不阻断主图(try/except 包裹)。
- **级别名近似**：单周期同构命名(5m 线段中枢叫"5min级别")是缠论习惯口径，非单周期升级的精确 L2；已与用户确认接受。

## 范围

- **MVP**：A 股 1m/5m 图叠加，目标 30m，取各高周期 L1 线段中枢，默认开，固定阶梯。
- **后续(不在本 spec)**：各市场自定义阶梯/目标(美股 15m/1h 等)、可选叠加 L0(高周期笔中枢)、高周期 cd 缓存、叠加层与 `recursive_levels` 的去重/统一。

## 不做（YAGNI）

- 不动旧链路(`recursive_calculator`/旧中枢)。
- 不改 `classify_rel`/`recursive_branch`(Phase 0 已证无 bug)。
- 不做单周期升级到 L3(实际数据给不出)。
