# 中枢扩展实体化（中心定理二）设计

> 缠论核心重做 P8。新核心 `recursive_branch` 此前只做了「走势类型递归」(趋势型升级)，
> 漏了「中枢升级」(延伸 + 扩展) 这条单周期升级路径。本 spec 把定理二实体化，让单周期
> 直接产出高级别中枢 (L1=5min级别、L2=30min级别…)，**取代 P7 真实多周期叠加**。
> 复用新核心、不动旧 `recursive_calculator`、不改背驰、不改走势类型边界。

> **⚠ Task4 真实数据校准(2026-06-02,以此为准)**：原设计「借 3 个 zslx 走势类型算扩展区间」在真实数据
> 塌缩失效(走势类型太少、扩展中枢同属一个走势类型 → L1 区间重复+done恒False)。**已改为「扩展组子中枢
> 包络重合」**：核心区 [ZD,ZG]=[max(子中枢dd),min(子中枢gg)]、包络=并集、done=子中枢数≥3(每个子中枢=
> 最小次级别走势类型,line31774)。`_spanning_zslxs` 删除、`materialize_expansions(zss)` 去 zslxs 参。
> **延伸(单中枢≥9段)实体化暂缓**(line31774 段窗口分解属独立口径,待定)。下文「组件2/合并区间公式」按旧
> 「借走势类型」口径描述，已被本校准取代。

## 目标

让任意周期图上的高级别中枢，由**单周期递归**产出（缠论原文的正统做法）：

- **走势类型递归**(趋势型升级，已有，不动)：3 个方向交替的走势类型重叠 → 本级中枢。
- **中枢升级**(盘整型升级，新增)：
  - *扩展*：≥2 个相邻同级中枢，本体包络重叠 **且** 核心区分离 → 合并为高级别中枢（定理二）。
  - *延伸*：单中枢长到 ≥9 段（=3 组次级别走势重叠）→ 本身即高级别中枢。
- 两条路**并存、互补**：走势类型递归抓趋势型，中枢升级抓盘整型；用户口径「9 段延伸 ≡ 3 个次级别走势类型重叠」即同一结构的两个视角。

## 背景：补上 Phase 0 漏掉的升级路径

P7 的背景结论说「单周期升不上去 → 走真实多周期叠加」。复盘发现这个结论**只验证了走势类型递归一条路**：

1. 走势类型递归要求 3 个方向交替走势类型重叠才升级，符合原文 line8136「趋势中中枢绝对不重叠」，但真实市场很难凑出 → 升不上去（Phase 0 实测）。
2. **但定理二的「中枢扩展」是另一条独立升级路径**：相邻两中枢核心区分离、包络重叠即升级，**不需要方向交替**。spike(`scripts_local/spike_expand.py`) 实测：TSLA 线段中枢 8→2 扩展组、笔中枢 60→18 组；513100 线段中枢 3→1 组——**真实数据大量发生**。
3. 故单周期**能**升级，靠的是中枢扩展，不是走势类型递归。P7 真实多周期叠加是当时漏看这条路的变通；P8 补上后，单周期递归扩展取代 P7。

> 注：中途的 `cr_zdzg`(放宽 `classify_rel` 到 ZG/ZD) 违背原文已撤回——它把定理二判为「扩展」的情况误当趋势。本 spec 不走那条路，而是**新增**扩展判定、保留 `classify_rel` 趋势/非趋势语义不动。

## 原文口径（锚点，全部可回原文核对）

| 口径 | 原文 | 结论 |
|---|---|---|
| 定理二·三分类 | line 10029 | 后GG<前DD 或 后DD>前GG → 趋势；**后ZG<前ZD 且 后GG>=前DD，或 后ZD>前ZG 且 后DD<=前GG → 高级别中枢(扩展)**；核心区也重叠 → 延伸(同中枢) |
| 扩展中枢区间 | **line 31778 / 31774** | 「扩展后的中枢区间就是每 3 段中的最高最低点的**重合区域**」(10 线段→5分钟中枢实例) |
| 中枢区间公式 | line 20029 | 「MAX 的低点和 MIN 高点是对的，一般看前面三段」 |
| 完成度 | line 26870 / 26871 | 2 中枢重叠=**进行式**(需 3 买确认)；扩展后的划分和一般中枢无区别 |
| 9 段 | line 27278 / 31774 | 「至少要 9 段次级走势才能形成高一级中枢」；10 线段 (12 23 34)(45 56 67)(78 89 910) 三组重叠 |
| 扩展⊥转折 | line 16429 | 「中枢扩展与走势转折之间没什么必然联系」→ 扩展**不切**走势类型(现有 `zslx_branch` 口径对) |
| 影线重叠警告 | line 24704 / 22754 | 仅上下影线快速波动重合「觉得不妥」→ 用**本体**判重叠，不用瞬间影线 |

**合并区间公式（本 spec 的核心几何，纠正旧版 + 用户口径校正）**：

单元是**扩展跨越的 3 个次级别走势类型**（不是 2 个子中枢）——每个走势类型取其包络 GG/DD：

```
高级别中枢 核心区 [ZD,ZG] = [max(三个走势类型 DD), min(三个走势类型 GG)]   ← 重合(overlap)，画框用此
高级别中枢 包络   [DD,GG] = [min(三个走势类型 DD), max(三个走势类型 GG)]   ← 并集，震荡外沿
```

- 「3 个走势类型」对应原文 line27278「至少 9 段=3 组次级走势」、line31774 实例的 3 组；**2 个中枢(7 段)只是进行式，补满 3 个走势类型(9 段)才是完成的高级别中枢**。
- 此式正好 = `zs_branch.core_interval` 在 3 个走势类型单元上的结果——但成枢是否触发由「中枢层定理二」判（见组件 3「与走势类型递归的关系」），不是走势类型递归的核心区重叠判据。

> ⚠ **旧版 `recursive_calculator._merge_zss` 把核心区也用并集(`zd=min sub.zd, zg=max sub.zg`)——错了**。原文核心区是「重合」。这是 memory 记录「旧版过拟合、全图 3 层皆错」的一部分。包络用并集是对的。

## 级别映射与命名

L0 = 当前图周期的线段中枢；扩展/递归逐级抬升，沿 `charts.js` 的 `FREQ_CHAIN` 命名：

| 当前图 | L0 | L1 | L2 | L3 |
|---|---|---|---|---|
| 1m | 1min级别(线段中枢) | 5min级别 | 30min级别 | 日线级别 |
| 5m | 5min级别 | 30min级别 | 日线级别 | — |
| 30m | 30min级别 | 日线级别 | — | — |

> 笔中枢(`get_bi_zhongshu`) 仍是 L0 之下的观察级别，独立旁路、不参与升级，本 spec 不动。

## 架构

### 组件 1：定理二扩展判定（`zs_branch.py`，新增纯函数）

`classify_rel` 现在 "expand" = 本体包络重叠，**不区分**延伸(核心区也重叠) vs 扩展(核心区分离)。保留它不动（`zslx_branch` 靠它判趋势/非趋势），**新增**：

```python
def is_zs_expand(prev: ZS, cur: ZS) -> bool:
    """定理二·中枢扩展：本体包络重叠(闭区间) 且 核心区分离。

    用中枢自身 dd/gg(已 correct_exit 剥离开段，非瞬间影线，line24704) 与 zd/zg。
    """
    if None in (prev.zd, prev.zg, cur.zd, cur.zg):
        return False
    envelope_overlap = max(prev.dd, cur.dd) <= min(prev.gg, cur.gg)   # >=/<= 触及即算(§3.5)
    core_separated = (cur.zg < prev.zd) or (cur.zd > prev.zg)
    return envelope_overlap and core_separated
```

> 用中枢自身 `dd/gg` 而非 `body_envelope(lines[:3])`：done 中枢经 `correct_exit` 已剥离开段远摆，`dd/gg` 即本体包络；且合并中枢的 `lines` 是子中枢拼接，`lines[:3]` 不代表其包络——统一用 `dd/gg/zd/zg` 跨级一致。

### 组件 2：中枢扩展实体化（新模块 `zs_expand.py`）

**触发判定基于中枢、区间计算借走势类型**（用户口径）：

```python
def materialize_expansions(zss: List[ZS], zslxs: List[ZSLX]) -> List[ZS]:
    """检测中枢扩展(定理二)/延伸，借次级别走势类型实体化为高级别中枢，按时间序返回。

    触发(基于中枢)：
    - 扩展：相邻 is_zs_expand 为真的连续中枢成组(≥2)。
    - 延伸：单中枢 line_num>=9 (is_extension_candidate)。
    区间(借走势类型)：取该组跨越的连续次级别走势类型(来自 zslxs)，按组件 2 顶公式
        [ZD,ZG]=[max(走势类型 DD), min(走势类型 GG)]、[DD,GG]=[min(走势类型 DD), max(走势类型 GG)]。
    完成度：跨越走势类型 >=3 (=9段) → done=True(完成式)；<3 → forming(进行式)。
    非升级中枢(独立、不扩展、<9段) 不进结果。
    """

def _spanning_zslxs(group: List[ZS], zslxs: List[ZSLX]) -> List[ZSLX]:
    """扩展组跨越的次级别走势类型：按段索引范围，取覆盖 group 首末中枢的连续 zslxs。
    不足 3 个(进行式)按现有补满或标 forming——精确选取规则实现时定 + 出图审校。"""

def _build_expanded_zs(spanning: List[ZSLX]) -> ZS:
    """走势类型列表 → 高级别中枢。zd=max(w.zs_low)、zg=min(w.zs_high)（重合）；
    dd=min(w.zs_low)、gg=max(w.zs_high)（并集）。直接写 zd/zg/dd/gg 并置 _bounds_dirty=False，
    **不调 update_boundaries**(否则 gg/dd 重算成并集覆盖核心区)。expanded_with=对应子中枢链。"""
```

- 走势类型的 GG/DD：`zslx.zs_high/zs_low`(zslx_branch._finalize 已填 = 该走势类型中枢包络)即走势类型的 GG/DD；若出图审显示需用走势类型全段包络，再改取其首末段范围(留实现校)。
- `ZS.can_expand_with`(cl_interface line628) 现在只判包络重叠，**补核心区分离**改成 `is_zs_expand` 同口径（或直接复用 `is_zs_expand`）。

### 组件 3：递归扩展集成（`recursive_branch.py`）

走势类型递归主链**完全不动**（最低风险）。新增**扩展叠加**：在已建的层级树上，把每级中枢按 `materialize_expansions`(借本级走势类型算区间)递归抬升，并入同一层级索引：

```
trend 主链产出 done_zss[0..N] + 各级 zslxs[0..N]（现有，多数情况只到 L0）
扩展叠加（新增，递归）:
  combined[0] = done_zss[0]
  k = 0
  while True:
      E = materialize_expansions(combined[k], zslxs[k])   # 借本级走势类型算区间，E 属 level k+1
      next_trend = done_zss[k+1] if 存在 else []
      combined[k+1] = dedup_union(next_trend, E)   # 两路并入同一级(按子段索引范围重叠去重)
      if not combined[k+1] or k+1 触 _MAX_LEVELS: break
      k += 1
  每级 LevelResult.zss = combined[level]（trend ∪ expand）
```

- **去重**：trend 与 expand 对同一片都产中枢时，按构成子段索引范围重叠合并（trend 罕发，多数为空、直接取 expand）。
- `upgrade_idx` 保留：标注本级哪些中枢是升级产物(供审图/买卖点未来用)。
- 终止：某级无中枢 / 触 `_MAX_LEVELS`。

**与走势类型递归的关系**（用户口径：两条路并存、不冲突）：

| | 触发判据 | 成枢条件 | 区间 | 现状 |
|---|---|---|---|---|
| 走势类型递归(趋势型) | `zs_branch` 在走势类型单元上找中枢 | 3 走势类型**核心区**重叠(严) | `core_interval` | 已有、不动 |
| 中枢扩展(盘整型) | 中枢层**定理二** `is_zs_expand`(包络重叠+核心区分离，宽) | 跨越走势类型 ≥3(=9段)完成 | 借 3 走势类型 `[max DD,min GG]` | 本 spec 新增 |

二者区间公式同形，但**触发宽严不同**：递归要求走势类型核心区重叠（真实数据罕见 → Phase 0「升不上去」）；扩展只要中枢包络重叠+核心区分离即触发（spike 实测大量发生）。扩展独立触发、再借走势类型算区间，故能补上递归漏掉的盘整型升级。

### 组件 4：取代 P7（web 图表改线）

- `chart_compute.apply_higher_zs_to_chart_data` **停止调用**（`tv.py` 移除调用 + 门控默认关）；`chart_data["higher_zs"]` 字段弃用。P7 计算代码**保留休眠**(不硬删，可逆)。
- 图表高级别中枢统一从 `recursive_levels` 的 **L1/L2/L3** 取（现有 `recursive_zss` 渲染只画 `level==0`，扩成画各级并按级别名分色/线宽递增）。
- `charts.js` 中枢组「5min级别 / 30min级别 / 日线级别」开关：从绑 `higher_zs_<period>` 改为绑 `recursive_levels[1/2/3]`（`zs_L1` / `zs_L2` …）。`_zsLevels` 计算改从 `recursive_levels` 的实际级别数 + `FREQ_CHAIN` 命名生成。
- 中枢区间用核心区 `[ZD,ZG]`（`zs_to_chart_dict(zs, use_envelope=False)`，沿用已修口径）。
- `SCHEMA_VERSION` bump（chart_data 含义变化、不进 config）。

## 配置项

复用现有 `chart_show_higher_zs`（语义从「P7 真实多周期叠加」变为「显示高级别(扩展)中枢」，默认 `"1"`）。`chart_use_branch_core` 等不变。

## 测试策略

- **单元** `tests/core/test_zs_expand.py`：
  - `is_zs_expand`：构造核心区分离+包络重叠→True；核心区也重叠(延伸)→False；包络分离(趋势)→False；闭区间触边→True。
  - `materialize_expansions`：相邻中枢扩展+跨越 3 走势类型→1 高级别，核心区=`[max(走势类型DD), min(走势类型GG)]` 重合、包络=`[min(走势类型DD), max(走势类型GG)]` 并集；跨越 ≥3 走势类型→done=True、<3→forming；单中枢≥9段→延伸升级；独立中枢→不进结果。
  - 区间数值断言（防回归用例锚定原文 line31778 重合口径，**显式断言核心区取走势类型 [max DD,min GG] 重合、不是并集**）。
- **集成** `tests/core/test_recursive_branch.py` 扩充：用真实 fixture(TSLA/513100/301004 已有 1m parquet/csv) 跑 `get_recursive_branch_levels`，断言扩展组数 ≈ spike 实测(TSLA 线段中枢 8→2、513100 3→1)，且 L1 中枢区间在合理范围(核心区窄于包络)。
- **回归**：全套 `poetry run pytest tests/ -q` 零回归(3 个 `test_exchange_lookback` 失败为 pre-existing QMT 配置，与本 spec 无关)；`ruff check`；charts.js / datafeed `node --check`。
- **真实出图验收**：A 股 1m 图人工审 L0(1min级别)+L1(5min级别)+L2(30min级别) 中枢位置、扩展框区间、级别命名；确认 P7 已下线无残留双框。⚠ 清 `chart_cache` 验证。

## 风险 / 边界

- **影线噪声误判扩展**(line24704)：用 `correct_exit` 后的本体 `dd/gg`、不用瞬间影线；闭区间触及口径可能对"擦边"敏感 → 真实出图复核，必要时加最小重叠阈值(留扩展，先按原文闭区间)。
- **trend×expand 去重**：trend 罕发，去重多数空转；规则按子段索引范围重叠，需用例覆盖「两路同片」边界。
- **进行式中枢右边缘**：2 中枢扩展未达 3 即 forming，沿用「pending 入树」口径入树、图表区分 done/forming(线型)。
- **级别命名近似**：L1 叫「5min级别」是缠论同构习惯口径，非严格 5 分钟 K 线结构；P7 下线后此名唯一来源即 P8，避免双源撞名。
- **多级深度**：`_MAX_LEVELS=50` 护栏；扩展逐级收缩，正常远不及。

## 范围

- **MVP**：定理二扩展 + 9 段延伸实体化、递归叠加进 `recursive_branch` 层级树、取代 P7、图表 L1/L2/L3 渲染 + 级别开关、真实出图验收。
- **后续(不在本 spec)**：高级别买卖点(3 买卖基于扩展中枢)、扩展最小重叠阈值调参、扩展中枢与背驰/区间套联动、P7 代码彻底删除。

## 不做（YAGNI）

- 不动旧链路(`recursive_calculator` / 旧中枢) 与笔中枢旁路。
- 不改 `classify_rel` 的趋势/非趋势语义、不改 `zslx_branch` 走势类型边界(line16429 扩展⊥转折)。
- 不改背驰、不动买卖点(保持笔级 18 个)。
- 不硬删 P7 代码(先休眠，验收稳定后再清)。
