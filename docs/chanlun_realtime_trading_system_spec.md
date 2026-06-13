# 缠论实时交易系统规格

> 目标：用缠论原文口径构建 A 股与美股可回测、可监控、可迭代的多级别实时交易系统。
> 本规格约束当前实现与后续优化，避免回测、图表、实盘提醒各用一套解释。

## 1. 理论约束

1. 级别不是简单周期近似，而是递归层级标签：当前图周期线段中枢为 L0，向上依次抬升为 5min、30min 等级别中枢。
2. 30m 是操作级，必须采用同级别分解：5m 走势类型恰好 3 段重合形成 30m 中枢，允许盘整+盘整，不使用中枢延伸/扩展。
3. 30m 以下采用非同级别分解：线段形成 1m 中枢，1m 走势类型通过延伸/扩展/扩张形成 5m 中枢。
4. 中枢关系的优先级固定为：核心重叠先判延伸；核心分离但本体包络重叠判扩张；本体包络分离才判趋势或新中枢。
5. 趋势的标准结构为 `a+A+b+B+c`。趋势背驰比较 `c:b`，盘整背驰比较离开段与进入段。
6. 第一类买卖点来自趋势背驰；第二类买卖点来自次级别相应走势第一类买卖点；第三类买卖点来自离开中枢后的第一次回试不破 `ZG/ZD`。
7. 小级别信号必须服从高级别语境。1m 信号只有在 5m 与 30m 不向下时才进入实盘候选；背驰类买卖点后续再用区间套标注可操作性。

来源锚点：`docs/chanlun_core_redesign_0_中枢划分原文理论.md`、`docs/chanlun_core_redesign_5a_买卖点_design.md`、`docs/chanlun_core_redesign_5b_二类买卖点_design.md`、`docs/chanlun_core_redesign_6_区间套_design.md`、`docs/chanlun_core_redesign_8_中枢扩展_design.md`、`docs/chanlun_core_redesign_9_结合律与走势类型_原文理论.md`。

## 2. 图表显示口径

1. 1min K 线：显示笔、1m 中枢、5m 中枢、30m 中枢、各级买卖点、各级背驰。
2. 5min K 线：显示笔、5m 中枢、30m 中枢、各级买卖点、各级背驰。
3. 30min K 线：显示笔、30m 中枢、买卖点、背驰。
4. `recursive_levels` 是高级别中枢、买卖点、背驰的统一数据源；笔级与线段级信号分别走 `bi_mmds/xd_mmds` 与 `bi_bcs/xd_bcs`。
5. P7 真实高周期叠加不再作为主路径；高级别显示以单周期递归升级链为准，避免真实周期叠加与递归级别混用。

## 3. 三个独立选股系统

### 系统 A：技术结构系统

输入：1m/5m/30m 缠论结构、买卖点、背驰、30m 与 5m 方向。

规则：
1. A 股默认全市场扫描，候选来自最近技术买点。
2. 1m 实盘模式：1m 买点 + 30m 不向下；5m 不向下时按正常仓位，5m 向下时只允许 soft gate 折扣仓位。
3. 5m 稳健模式：5m 买点 + 30m 不向下。
4. 牛市或强趋势优先第三类买点；熊市或大盘弱时只保留高质量 1 买/2 买，且降低仓位。
5. 卖出为小级别卖点或 30m 转 down；组合当前保持长-only，先不启用美股做空。

代码入口：
`src/chanlun/recursive_bt/portfolio.py`、`src/chanlun/recursive_bt/live_backtest.py`、`src/chanlun/recursive_bt/live_monitor.py`。

### 系统 B：基本面质量系统

输入：按公告日可见的财报数据。

规则：
1. `fund_ok`：年化 ROE 高于阈值，收入或利润增速为正。
2. 行业池模式按结构层选股，行业龙头与成长权重分离，季度更新。
3. 只用于选股门控，不决定具体买卖时点。

代码入口：
`src/chanlun/recursive_bt/systems.py` 的 `attach_scores` 与 `main_v3`。

### 系统 C：比价/资金流系统

输入：相对大盘强弱、估值质量、海选趋势门槛。

规则：
1. `value_ok`：年化 ROE / PB 高于全市场中位，代表优质且相对便宜。
2. `rs_ok`：最近窗口个股收益强于大盘，作为资金流入代理。
3. `ma_ok`：前一完整日收盘在 70 日线上方，作为“能搞的”海选门槛。

代码入口：
`src/chanlun/recursive_bt/systems.py` 的 `main_v2`，`src/chanlun/recursive_bt/portfolio.py` 的 `attach_pool_filters`。

## 4. 策略组合

主策略：`CL-MTF-3`

1. 操作级别：1m。
2. 中级别门控：5m 不向下。
3. 大级别门控：30m 不向下。
4. 选股：A、B、C 三系统同时通过时为最高优先级；只通过 A 为技术基线。
5. 排序：日线 3 买窗口优先，其次第三类买点、第二类买点、第一类买点，最后按代码稳定排序。

稳健策略：`CL-5m-30m`

1. 操作级别：5m。
2. 大级别门控：30m 不向下。
3. 用于数据不足、1m 噪音过高、或全市场扫描压力过大时。

共振策略：`D3-30m-5m`

1. 日线 3 买窗口为候选池。
2. 30m 不向下。
3. 5m 买点触发入场。
4. 适合小资金和低频执行，也作为 `CL-MTF-3` 的排序增益。

## 5. 仓位与提醒

标准组合仓位为 `1 / max_pos`。

提醒口径：
1. `3buy`：最高为一个标准仓位。
2. `2buy`：0.75 个标准仓位。
3. `1buy`：0.5 个标准仓位。
4. 30m 向上或日线 3 买共振可提高建议比例，但不超过一个标准仓位。
5. 小级别卖点、30m 转 down 退出提醒均给 `建议卖出=100%`。

当前实时监控、组合回测与 paper broker 均按此口径使用目标仓位；历史挂单没有 `target_weight` 时仍按等权 slot 兼容成交。

## 6. 市场规则

A 股：
1. T+1，当日买入不可卖出。
2. 不做空。
3. 最小 100 股。
4. 主板 10% 涨跌停，创业板/科创板 20%，北交所 30%。
5. 卖出收印花税，涨停买不进、跌停卖不出按挂单顺延。

美股：
1. T+0。
2. 最小 1 股。
3. 无涨跌停。
4. 系统规则支持做空，但当前策略先保持长-only，等多市场回测确认后再开启空头版本。

## 7. 回测迭代协议

每一轮策略变更都必须输出：
1. 组合收益、等权基准收益、超额收益。
2. 最大回撤。
3. 夏普。
4. 胜率。
5. 交易次数。
6. A 股与美股分别跑，A 股优先全市场，必要时先按池子抽样。

基础命令：

```powershell
python -m chanlun.recursive_bt.live_backtest --market a --source bt_data --max-pos 10 --require tech --big-gate bsp
python -m chanlun.recursive_bt.live_backtest --market a --source chart_cache --op-level 1m --mid-level 5m --big-level 30m --max-pos 10
python -m chanlun.recursive_bt.live_backtest --market us --source chart_cache --op-level 1m --mid-level 5m --big-level 30m --max-pos 10
```

实时监控命令：

```powershell
python -m chanlun.recursive_bt.live_monitor --market a --op-level 1m --mid-level 5m --big-level 30m
python -m chanlun.recursive_bt.live_monitor --market us --data-source chart_cache --op-level 1m --mid-level 5m --big-level 30m
```

## 8. 当前实现状态

已具备：
1. 30m 同级别分解与 30m 以下非同级别升级链。
2. 图表递归层级中枢、买卖点、背驰数据源。
3. 三系统选股门控与 A 股全市场扫描入口。
4. A 股/美股市场规则与 live-parity 回测入口。
5. 实时监控支持 `1m+5m+30m` 或 `5m+30m`，通知包含建议买入/卖出比例。
6. 提醒、组合回测、paper broker 共享买入仓位比例口径。
7. `adaptive` bull/bear regime：30m 中性降仓、30m 向下归零、30m 向上时可选择放宽 5m 短暂向下的三买。
8. live-parity 回测与实时监控默认按有效池大小自动收缩 `max_pos`；显式 `--max-pos` 正数仍完全尊重。

## 9. 当前基线

2026-06-11 本机缓存小池 live-parity 基线：

| 市场 | 池 | 级别 | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | bt_data 20 只 | 5m+30m | +28.3% | -2.5% | +30.7% | 3.5% | 2.85 | 56% | 169 |
| A 股 | chart_cache 4 只 | 1m+5m+30m | +0.7% | -1.2% | +1.9% | 1.2% | 0.96 | 54% | 13 |
| 美股 | QQQ/TSLA | 1m+5m+30m | +3.5% | +15.1% | -11.6% | 0.4% | 6.85 | 82% | 56 |

初步结论：
1. A 股 5m+30m 技术基线当前收益/回撤比最好，适合作为全市场扩展基线。
2. 1m+5m+30m 对回撤控制很强，但在样本池较小时交易过少。
3. 美股强趋势阶段长-only 三级门控过于保守，下一轮应测试“30m up 时放宽 5m gate 或提高趋势持仓权重”。

## 10. 第二轮矩阵

2026-06-11 增加 `adaptive` regime、`bull_relaxed` mid gate、池规模匹配仓位后的矩阵：

| 市场 | 池 | 级别 | max_pos | regime | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | chart_cache 4 只 | 1m+5m+30m | 10 | off/strict | +0.7% | -1.2% | +1.9% | 1.2% | 0.94 | 54% | 13 |
| A 股 | chart_cache 4 只 | 1m+5m+30m | 4 | off/strict | +1.9% | -1.2% | +3.1% | 2.9% | 0.96 | 54% | 13 |
| 美股 | QQQ/TSLA | 1m+5m+30m | 10 | off/strict | +3.5% | +15.1% | -11.6% | 0.4% | 6.85 | 82% | 56 |
| 美股 | QQQ/TSLA | 1m+5m+30m | 2 | off/strict | +18.5% | +15.1% | +3.4% | 2.1% | 6.89 | 82% | 56 |
| A 股 | bt_data 20 只 | 5m+30m | 10 | off/strict | +28.3% | -2.5% | +30.7% | 3.5% | 2.85 | 56% | 169 |
| A 股 | bt_data 20 只 | 5m+30m | 10 | adaptive/strict | +26.1% | -2.5% | +28.6% | 2.9% | 2.84 | 56% | 169 |
| A 股 | bt_data 20 只 | 5m+30m | 5 | off/strict | +42.8% | -2.5% | +45.3% | 5.4% | 2.38 | 55% | 143 |
| A 股 | bt_data 20 只 | 5m+30m | 5 | adaptive/strict | +42.3% | -2.5% | +44.8% | 5.0% | 2.45 | 55% | 143 |

第二轮结论：
1. 小池必须匹配 `max_pos`。美股 QQQ/TSLA 用 `max_pos=2` 后，从跑输基准变成超额 +3.4%，且回撤仍远低于基准。
2. A 股 5m+30m 的当前收益候选是 `max_pos=5`；如果优先收益用 off/strict，如果优先收益/回撤平衡用 adaptive/strict。
3. 本轮样本里 `bull_relaxed` 没新增交易，说明被 strict 拦住的“30m up + 5m down + 3buy”很少；先保留参数，等待更大样本验证。
4. `1first` 与 `3first` 在 A 股 5m+30m 小池结果相同，说明同 bar 多买点并存较少；但代码已修复 `3first` 必须选择三买的类别一致性问题。

## 11. 第三轮默认参数校正

2026-06-11 将 `max_pos` 默认值改为随有效池自动收缩：`max_pos=min(10, universe_size)`，用户显式传正数时不改。

| 市场 | 池 | 级别 | requested_max_pos | 有效 max_pos | 收益 | 基准 | 超额 | 回撤 | 夏普 | 交易 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 美股 | QQQ/TSLA | 1m+5m+30m | null | 2 | +18.5% | +15.1% | +3.4% | 2.1% | 6.89 | 56 |
| A 股 | chart_cache 4 只 | 1m+5m+30m | null | 4 | +1.9% | -1.2% | +3.1% | 2.9% | 0.96 | 13 |
| A 股 | bt_data 20 只 | 5m+30m | null | 10 | +28.3% | -2.5% | +30.7% | 3.5% | 2.85 | 169 |

第三轮结论：
1. 美股小池默认结果现在就是上一轮 `max_pos=2` 的优选结果，不再因为 80% 现金闲置而跑输强趋势基准。
2. A 股 1m 小池默认收益从 +0.7% 提到 +1.9%，代价是回撤从 1.2% 提到 2.9%，仍显著低于基准回撤。
3. A 股 20 只池有效 `max_pos` 仍为 10，保持原有稳健基线。

下一轮优先修复：
1. 重建 A 股全市场 `bt_data` 技术信号缓存，把区间套 `operable/depth` 写入缓存后再跑全市场 `require=tech,nest`。
2. 对 A 股全市场与美股核心池分别跑更大参数矩阵，按“收益/回撤/夏普/交易数”自动选择下一轮候选。
3. 将全市场选股结果与默认 `max_pos` 联动，进一步区分“候选池大小”和“实际可开仓位数”。

## 12. 第四轮区间套门控

2026-06-11 已把区间套读数接入操作级买卖点信号：`Signal.nest_operable` 与 `Signal.nest_depth`。注意区间套标注必须与买点使用同一 L0 单位：笔买点用笔递归链重算区间套，线段买点用线段递归链重算区间套；不能拿线段区间套去标注笔买点。组合回测可通过 `require=tech,nest` 启用；实时监控可通过 `--require-nest` 或配置 `require_nest=true` 启用。

1. `1buy/2buy` 属于背驰定位型买点，启用硬 `nest` 时必须满足 `nest_operable=True`。
2. `3buy` 属于离开中枢后的回试确认，不强制要求背驰区间套，避免错杀趋势延续信号。
3. 监控通知中，一、二类买点通过区间套时会在 `reason` 中标记 `interval_nest(depth=N)`，同时仍给出建议买入比例。
4. `signal_schema.nest_key=3` 代表“按同一 L0 单位重算区间套”的缓存版本；低于该版本的旧 `bt_data` 不能用于 `nest` 结论。

本轮对照结果：

| 市场 | 池 | 级别 | source | require | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 美股 | QQQ/TSLA | 1m+5m+30m | chart_cache | tech,nest | +18.4% | +15.1% | +3.3% | 2.0% | 6.80 | 82% | 55 |
| A 股 | chart_cache 4 只 | 1m+5m+30m | chart_cache | tech,nest | +1.9% | -1.2% | +3.1% | 2.9% | 0.96 | 55% | 11 |
| A 股 | chart_cache 4 只 | 5m+30m | chart_cache | tech,nest | +2.9% | -0.0% | +3.0% | 1.8% | 1.28 | 75% | 4 |
| A 股 | bt_data 排序前 20 | 5m+30m | bt_data key3 | tech | -8.2% | -31.0% | +22.8% | 11.7% | -0.91 | 33% | 75 |
| A 股 | bt_data 排序前 20 | 5m+30m | bt_data key3 | tech,nest | -8.7% | -31.0% | +22.3% | 12.2% | -0.96 | 31% | 68 |

第四轮结论：

1. `chart_cache` 路径会实时计算区间套标注，US 小池表现为轻微降频、轻微降回撤，收益基本持平。
2. A 股 `chart_cache` 1m 小池交易数从 13 降到 11，收益/回撤基本不变，说明当前样本中被过滤的 1/2 类买点影响较小。
3. 修复同单位标注后，A 股排序前 20 样本中大多数一、二类买点只是 `depth=1`，真正 `operable=True/depth=2` 很少；硬 `nest` 门控会继续错杀机会，不适合作为默认交易规则。
4. A 股全市场回测不能再使用“代码排序前 N”作为样本池；该池当前集中在北交所标的，样本本身基准回撤过大。下一轮必须改为三系统选股/海选过滤形成 point-in-time 候选池，再比较 `tech` 与 `tech,nest`。
5. 已补北交所股票 30% 涨跌幅规则；后续全市场回测需继续区分主板、创业板/科创板、北交所、ST、新股首日等交易规则。

## 13. 第五轮 A 股 selector 池

2026-06-11 修复 `bt_data` 回测池选择：`live_backtest --source bt_data` 新增 `--bt-pool-mode selector|all|sorted`，默认 `selector`。其中：

1. `selector`：扫描 A 股缓存，用技术结构 + 基本面质量 + 比价估值三系统形成候选池，`--pool-size` 表示 selector 返回的最大候选数。
2. `all`：加载全部有效 `bt_data`，用于真正全市场动态扫描，但会更慢、更吃内存。
3. `sorted`：保留旧的代码排序前 N 逻辑，仅用于 legacy 对照，不再作为默认结论来源。
4. 报告摘要写入 `symbol_codes`，防止不同候选池之间误做同池比较。

当前 selector 参数：`pool_size=20`，`lookback_bars=240`，`buy_classes=3,2,1`，`require_three_systems=true`。当前入池代码：

`SH.601166, SH.600377, SH.688257, BJ.920111, SH.603379, SH.601939, SH.600060, SH.601838, SH.601997, SH.600188, SZ.000630, SH.688059, SH.600719, SH.603194, SH.600900, SH.600999, SH.601233, SZ.000681, SZ.001299, SZ.000333`

同池对照：

| 市场 | 池 | 级别 | require | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | 三系统 selector 20 只 | 5m+30m | tech | +75.9% | +70.0% | +5.9% | 5.4% | 4.49 | 53% | 301 |
| A 股 | 三系统 selector 20 只 | 5m+30m | tech,nest | +71.8% | +70.0% | +1.8% | 5.4% | 4.30 | 50% | 268 |
| A 股 | 三系统 selector 20 只 | 5m+30m | tech,nest_soft | +75.0% | +70.0% | +5.0% | 5.4% | 4.46 | 53% | 301 |

第五轮结论：

1. selector 池大幅修正了“排序前 N”样本污染问题，但当前结果仍是以 2026-06-10 附近的当前候选池回看过去一年，带有明显 as-of 当前候选池偏差；不能当作严格 walk-forward 全市场收益。
2. 在同一 selector 池内，硬 `nest` 过滤主要砍掉一类、二类买点：`tech` 交易分布为 1 类 30 笔、2 类 6 笔、3 类 255 笔；`tech,nest` 基本只剩 3 类 258 笔。
3. 硬 `nest` 让交易数从 301 降到 268，收益从 +75.9% 降到 +71.8%，最大回撤没有改善，因此不作为默认。
4. `nest_soft` 不砍一/二类买点，只按区间套深度降低一/二类仓位：收益 +75.0%，回撤 5.4%，交易数仍为 301。当前比硬门控更合理，可作为下一轮候选。
5. 下一步需要实现严格 walk-forward selector：每个交易日/每 N 根 5m bar 只使用当时可见的技术、基本面、比价数据生成候选池，再进入组合回测；这才是 A 股全市场三系统收益/回撤结论的有效证据。

## 14. 第六轮 walk-forward 三系统门控

2026-06-11 已把 A 股 `bt_data` 回测池扩展为 `--bt-pool-mode walk_forward`。该模式不使用“当前选出的股票池回看过去”，而是加载当时可见的技术、基本面、比价数据，在组合回测每根 bar 上通过 `tech + fund + value` 三系统门控。当前为了控制运行时间先跑 `--selection-scan-limit 200`，样本代码以北交所为主，因此本轮只作为机制验证和参数方向验证，不能外推为全 A 最终结论。

同一 scan200 样本对照：

| 市场 | 池 | 级别 | require | max_pos | regime | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | walk-forward scan200 | 5m+30m | tech,fund,value | 10 | off | +15.8% | -19.4% | +35.2% | 11.3% | 0.88 | 45% | 310 |
| A 股 | walk-forward scan200 | 5m+30m | tech,fund,value,nest_soft | 10 | off | +15.6% | -19.4% | +35.0% | 11.4% | 0.88 | 45% | 310 |
| A 股 | walk-forward scan200 | 5m+30m | tech,fund,value | 10 | adaptive | +15.7% | -19.4% | +35.1% | 11.3% | 0.89 | 45% | 310 |
| A 股 | walk-forward scan200 | 5m+30m | tech,fund,value | 5 | off | +33.1% | -19.4% | +52.5% | 14.5% | 1.12 | 47% | 215 |
| A 股 | walk-forward scan200 | 5m+30m | tech,fund,value | 5 | adaptive | +34.4% | -19.4% | +53.8% | 14.4% | 1.17 | 47% | 215 |

第六轮结论：

1. 严格 walk-forward 三系统门控已经比当前 selector 池更接近实盘：买入时只看当时可见的 `tech/fund/value`，不会把 2026-06-10 附近的强势池倒灌到过去一年。
2. 在 BJ-heavy 的困难样本中，`tech/fund/value` 仍相对等权基准取得 +35.2% 到 +53.8% 超额，说明“三系统先过滤、缠论买点再执行”的方向有效。
3. `nest_soft` 在本样本中没有改善收益/回撤，且略降收益；硬 `nest` 已在第五轮证明会错杀一/二类买点，因此当前默认仍应是 `tech,fund,value`，区间套只作为一/二类买点仓位调节或通知解释。
4. `max_pos=5` 明显提升收益和夏普，但回撤从约 11.3% 增至约 14.4%；若目标是“收益最高且回撤可接受”，当前候选为 `max_pos=5 + adaptive + tech/fund/value`。若目标是低回撤，则保留 `max_pos=10 + adaptive`。
5. 下一轮必须去掉 sorted scan 偏差：增加主板/创业板/科创板/北交所分层抽样或直接跑全 A walk-forward，再按市场板块分别比较 `max_pos=5/10`、`adaptive/off`、`nest_soft/off`。

## 15. 第七轮分层样本与板块过滤

2026-06-11 已新增 `--selection-sample-mode stratified|sorted` 与 `--selection-board-filter all|shsz|main,gem,star,bj`。`walk_forward` 在设置 `--selection-scan-limit` 时默认使用 `stratified`，按主板、创业板、科创板、北交所轮转取样，避免 `sorted` 模式被北交所代码前缀占满。旧结果可用 `--selection-sample-mode sorted` 复现。

本地 `bt_data_all_a` 当前可加载 3746 只，其中主板 2817、科创板 608、北交所 321，暂未包含创业板缓存。因此全板块分层 200 样本为主板 67、科创板 67、北交所 66；`shsz` 200 样本为主板 100、科创板 100。

同一分层样本对照：

| 市场 | 池 | 级别 | require | max_pos | regime | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | stratified all 200 | 5m+30m | tech,fund,value | 5 | adaptive | +80.8% | +23.4% | +57.4% | 8.5% | 2.97 | 47% | 190 |
| A 股 | stratified all 200 | 5m+30m | tech,fund,value | 5 | off | +80.8% | +23.4% | +57.4% | 8.5% | 2.92 | 47% | 190 |
| A 股 | stratified all 200 | 5m+30m | tech,nest_soft,fund,value | 5 | adaptive | +78.8% | +23.4% | +55.4% | 7.9% | 2.96 | 47% | 190 |
| A 股 | stratified all 200 | 5m+30m | tech,fund,value | 10 | adaptive | +61.2% | +23.4% | +37.8% | 6.9% | 3.42 | 48% | 345 |
| A 股 | stratified shsz 200 | 5m+30m | tech,fund,value | 5 | adaptive | +104.2% | +39.0% | +65.2% | 9.4% | 3.65 | 53% | 215 |
| A 股 | stratified shsz 200 | 5m+30m | tech,fund,value | 10 | adaptive | +82.2% | +39.0% | +43.1% | 6.8% | 4.07 | 53% | 394 |

第七轮结论：

1. 分层样本显著修正了 BJ-heavy scan 的样本偏差；在更均衡的 all 200 中，`max_pos=5 + tech/fund/value` 收益 +80.8%，回撤 8.5%，比上一轮 BJ-heavy 样本更能说明三系统门控有效。
2. `shsz` 样本收益和夏普都更好，说明当前系统在主板/科创样本上的适配度强于北交所；北交所应继续保留 30% 涨跌幅规则，但在最终实盘中可作为独立子池控制仓位。
3. `max_pos=5` 是高收益候选，`max_pos=10` 是低回撤高夏普候选。当前建议默认实盘通知给出两套仓位：进攻版按 5 槽，单只基础仓位 20%；稳健版按 10 槽，单只基础仓位 10%。
4. `adaptive` 在 all 200 的收益/回撤变化很小，但夏普略优；保留为默认，因为它在熊市和 30m 向下时可以自然降仓，符合缠论“大级别优先”的风险控制。
5. `nest_soft` 在分层 all 200 中将回撤从 8.5% 降到 7.9%，代价是收益从 +80.8% 降到 +78.8%。因此它适合作为稳健版仓位调节，不适合作为进攻版默认。
6. 下一轮优先补齐创业板缓存，并跑更大的 `stratified all` 与 `shsz` 样本；若资源允许，再跑不设 `selection-scan-limit` 的全 A walk-forward。

## 16. 第八轮全 A walk-forward

2026-06-11 已完成不设 `selection-scan-limit` 的全 A walk-forward。该轮加载本地全部有效 `bt_data_all_a`：3746 只，其中主板 2817、科创板 608、北交所 321，当前缓存仍缺创业板。全 A 结果优先级高于前面 200/600 小样本。

全量样本对照：

| 市场 | 池 | 级别 | require | max_pos | regime | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | 全 A 3746 | 5m+30m | tech,fund,value | 10 | off | +100.3% | +26.9% | +73.3% | 9.0% | 3.50 | 51% | 457 |
| A 股 | 全 A 3746 | 5m+30m | tech,fund,value | 10 | adaptive | +96.3% | +26.9% | +69.4% | 9.0% | 3.46 | 51% | 457 |
| A 股 | 全 A 3746 | 5m+30m | tech,nest_soft,fund,value | 10 | adaptive | +94.6% | +26.9% | +67.7% | 8.6% | 3.44 | 51% | 457 |
| A 股 | 全 A 3746 | 5m+30m | tech,fund,value | 5 | adaptive | +86.1% | +26.9% | +59.2% | 14.4% | 2.44 | 47% | 224 |
| A 股 | 主板+科创 3425 | 5m+30m | tech,fund,value | 10 | adaptive | +71.1% | +30.8% | +40.4% | 9.9% | 2.76 | 48% | 469 |
| A 股 | 北交所 321 | 5m+30m | tech,fund,value | 10 | adaptive | +50.2% | -19.8% | +70.0% | 14.4% | 1.87 | 46% | 463 |

第八轮结论：

1. 全 A 证据推翻第七轮小样本里的“进攻版 `max_pos=5`”判断。全量样本中 `max_pos=5` 收益更低、回撤更高，因此不作为默认。
2. 当前 A 股默认候选更新为：`bt_pool_mode=walk_forward`、`selection_board_filter=all`、`require=tech,fund,value`、`max_pos=10`、`regime_mode=off`。这组全 A 收益 +100.3%，最大回撤 9.0%，夏普 3.50。
3. `adaptive` 在该年度样本里没有降低最大回撤，反而略降收益；保留为熊市预案和风控开关，但不作为当前默认。
4. `nest_soft` 在全 A 中把回撤从 9.0% 降到 8.6%，收益从 +96.3% 降到 +94.6%。因此稳健版可启用 `nest_soft`，但进攻/默认版不启用。
5. 北交所子池基准为 -19.8%，策略仍为 +50.2%，说明北交所不是无效池，但回撤 14.4%、夏普 1.87，风险显著高于全 A 默认。实盘可保留北交所信号，但单独限额或使用 `nest_soft/adaptive` 降仓。
6. 主板+科创全量样本不如全 A，说明跨板块比价阈值和北交所超跌修复共同贡献了收益；不应简单排除北交所，而应以子池风控处理。
7. 报告摘要已新增 `board_counts` 字段，后续全量报告可以直接证明样本板块结构，不再依赖前 200 个 `symbol_codes`。

## 17. 第八轮默认实盘参数快照（历史子集）

截至第八轮，全 A walk-forward 证据权重大于局部小样本。第十二轮已确认本节的 3746 只样本缺创业板，因此本节只保留为历史子集证据，当前默认参数以第十二轮为准。

| 市场 | 数据池 | 级别联立 | 选股/门控 | 仓位槽 | 默认 regime | 区间套 | 证据 |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| A 股 | 全 A `bt_data` 3746 | 30m+5m | `tech,fund,value` | 10 | off | 默认 off；稳健版 soft | 全 A +100.3%，回撤 9.0% |
| A 股稳健版 | 全 A `bt_data` 3746 | 30m+5m | `tech,fund,value` | 10 | adaptive | `nest_soft` | 全 A +94.6%，回撤 8.6% |
| 美股 | 在线核心 9 只 | 30m+5m+1m | `tech,trend3_boost,nest_soft` | 9 | off | soft | +14.9%，回撤 1.6% |

仓位解释：

1. A 股默认 `max_pos=10`，基础单槽 10%。三买为 1.0 槽，二买为 0.75 槽，一买为 0.5 槽；大级别向上或日线共振只允许把建议仓位提升到最多 1 槽。
2. 美股核心池当前为 `SPY/QQQ/AAPL/MSFT/NVDA/AMZN/META/GOOGL/TSLA`，`max_pos=9`，基础单槽约 11.1%。这是在线核心池逻辑；若只用 QQQ/TSLA 缓存小池，仍按自动 2 槽解释。
3. `nest_soft` 只影响一类、二类买点：真区间套可满额执行，浅层区间套降到 0.75，无区间套降到 0.5；三买不受区间套限制。
4. A 股北交所信号继续保留，但在通知层应标注 `A_BJ` 30% 涨跌幅规则，并可在资金执行层限制北交所总风险暴露。
5. 当前实证不支持硬 `nest`，也不支持把 `max_pos=5` 作为全 A 默认。它们只保留为研究开关。

美股小池最新对照：

| 市场 | 池 | 级别 | require | max_pos | regime | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 美股 | QQQ/TSLA | 1m+5m+30m | tech | 2 | off | +18.5% | +15.1% | +3.4% | 2.1% | 6.89 | 82% | 56 |
| 美股 | QQQ/TSLA | 1m+5m+30m | tech,nest_soft | 2 | off | +18.2% | +15.1% | +3.1% | 2.0% | 6.83 | 82% | 56 |
| 美股 | QQQ/TSLA | 1m+5m+30m | tech | 2 | adaptive | +18.5% | +15.1% | +3.4% | 2.1% | 6.89 | 82% | 56 |

## 18. 第九轮牛熊分段与牛段软化试验

2026-06-11 报告摘要新增 `market_regime_segments`：用等权基准的日线状态分段，规则为：

1. `bull`：20 日基准涨幅 >= 5%，且基准回撤 > -5%。
2. `bear`：20 日基准跌幅 <= -5%，或基准回撤 <= -10%。
3. `range`：其余状态。

该分段只用于评估和下一轮迭代，不改变原始交易信号；交易仍按当时可见的 30m/5m 买卖点和三系统门控执行。

全 A 默认分段：

| 策略 | 全年收益 | 全年回撤 | 分段 | 天数 | 策略收益 | 基准收益 | 超额 | 分段回撤 | 夏普 | 交易 |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `max_pos=10, off, tech/fund/value` | +100.3% | 9.0% | bull | 78 | +28.1% | +32.9% | -4.9% | 5.8% | 5.05 | 129 |
| `max_pos=10, off, tech/fund/value` | +100.3% | 9.0% | range | 150 | +56.1% | +4.2% | +51.9% | 3.8% | 3.85 | 289 |
| `max_pos=10, off, tech/fund/value` | +100.3% | 9.0% | bear | 16 | +0.2% | -7.9% | +8.1% | 5.9% | 0.27 | 39 |
| `max_pos=20, off, tech/fund/value` | +85.5% | 7.6% | bull | 78 | +26.9% | +32.9% | -6.0% | 3.3% | 5.96 | 266 |
| `max_pos=20, off, tech/fund/value` | +85.5% | 7.6% | range | 150 | +43.8% | +4.2% | +39.6% | 5.6% | 3.38 | 627 |
| `max_pos=20, off, tech/fund/value` | +85.5% | 7.6% | bear | 16 | +1.6% | -7.9% | +9.5% | 5.7% | 0.94 | 79 |

牛段诊断：

| 策略 | 全年收益 | 全年回撤 | bull 收益 | range 收益 | bear 收益 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `tech/fund/value` | +100.3% | 9.0% | +28.1% | +56.1% | +0.2% | 默认，震荡段贡献最大，熊段防守有效 |
| `tech/fund` | +82.6% | 8.3% | +47.2% | +31.6% | -5.8% | 牛段强，但破坏熊段和震荡段 |
| `tech/fund/value + value_bull_relaxed` | +77.3% | 8.0% | +33.6% | +27.1% | +4.5% | 牛段略改善，但全年收益大幅下降 |

第九轮结论：

1. 当前全 A 默认策略的主收益源是 `range`，不是追逐牛段指数弹性；这符合“三系统过滤 + 缠论买点执行”偏向高胜率、低回撤的定位。
2. 牛段跑输基准不是简单由 `value` 门控错误造成。完全去掉 `value` 能提高 bull 段收益，但会让 bear 段亏损并显著降低全年收益；`value_bull_relaxed` 也没有通过全年验证。
3. `max_pos=20` 是低回撤版本：全年收益 +85.5%，回撤 7.6%，夏普 3.71。它适合稳健账户，但不是收益最高版本。
4. 默认仍保持 `max_pos=10 + off + tech/fund/value`；稳健版可选 `max_pos=20` 或 `nest_soft`，两者都属于降波动而非增收益。
5. 下一轮若要改善牛段，不应粗暴放松价值系统，而应研究“牛段三买趋势延续仓位提升”或“30m 上涨中枢扩张后的三买加仓”，让进攻性来自缠论结构，而不是放弃三系统过滤。

## 19. 第十轮三买趋势延续仓位

2026-06-11 按第九轮结论实现 `trend3_boost` 研究开关：只在三买、30m 方向为 `up` 时提高建议仓位，一/二类买点不变，三系统 `tech/fund/value` 不放松。其缠论含义是：牛市或大级别向上时，第三类买点往往是离开中枢后的回试确认，趋势延续属性强于一/二类背驰定位，因此可以在结构确认后给更高仓位。当前实现为三买目标仓位从 1.0 槽提高到 1.25 槽。

全 A 对照：

| 市场 | 池 | 级别 | require | max_pos | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 股 | 全 A 3746 | 5m+30m | tech,fund,value | 10 | +100.3% | +26.9% | +73.3% | 9.0% | 3.50 | 51% | 457 |
| A 股 | 全 A 3746 | 5m+30m | tech,trend3_boost,fund,value | 10 | +105.8% | +26.9% | +78.9% | 9.7% | 3.45 | 50% | 461 |
| A 股 | 全 A 3746 | 5m+30m | tech,trend3_boost,nest_soft,fund,value | 10 | +113.0% | +26.9% | +86.1% | 9.2% | 3.62 | 51% | 465 |
| A 股 | 全 A 3746 | 5m+30m | tech,fund,value | 20 | +85.5% | +26.9% | +58.6% | 7.6% | 3.71 | 50% | 972 |
| A 股 | 全 A 3746 | 5m+30m | tech,trend3_boost,nest_soft,fund,value | 20 | +84.7% | +26.9% | +57.8% | 8.7% | 3.47 | 50% | 962 |

默认与新候选分段：

| 策略 | 全年收益 | 全年回撤 | 分段 | 策略收益 | 基准收益 | 超额 | 分段回撤 | 夏普 | 交易 |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 默认 `tech/fund/value` | +100.3% | 9.0% | bull | +28.1% | +32.9% | -4.9% | 5.8% | 5.05 | 129 |
| 默认 `tech/fund/value` | +100.3% | 9.0% | range | +56.1% | +4.2% | +51.9% | 3.8% | 3.85 | 289 |
| 默认 `tech/fund/value` | +100.3% | 9.0% | bear | +0.2% | -7.9% | +8.1% | 5.9% | 0.27 | 39 |
| 新候选 `trend3_boost+nest_soft` | +113.0% | 9.2% | bull | +28.5% | +32.9% | -4.4% | 5.4% | 4.91 | 130 |
| 新候选 `trend3_boost+nest_soft` | +113.0% | 9.2% | range | +61.8% | +4.2% | +57.7% | 3.8% | 3.94 | 294 |
| 新候选 `trend3_boost+nest_soft` | +113.0% | 9.2% | bear | +2.4% | -7.9% | +10.3% | 4.5% | 1.20 | 41 |

美股小池对照：

| 市场 | 池 | 级别 | require | max_pos | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 美股 | QQQ/TSLA | 1m+5m+30m | tech | 2 | +18.5% | +15.1% | +3.4% | 2.1% | 6.89 | 82% | 56 |
| 美股 | QQQ/TSLA | 1m+5m+30m | tech,trend3_boost,nest_soft | 2 | +19.7% | +15.1% | +4.6% | 2.3% | 6.89 | 82% | 56 |

第十轮结论：

1. `trend3_boost` 单独启用能提高收益，但回撤也升到 9.7%，夏普略降，不适合作为唯一改动。
2. `trend3_boost + nest_soft` 形成互补：三买趋势确认加仓，一/二类背驰买点按区间套深度降仓。全 A 收益从 +100.3% 提升到 +113.0%，回撤只从 9.0% 升到 9.2%，夏普从 3.50 提升到 3.62。
3. `max_pos=20` 不适合叠加该开关；稳健低回撤版仍用 `max_pos=20 + tech/fund/value`，不加 `trend3_boost`。
4. 在缺创业板的 3746 子集上，收益候选曾更新为：`max_pos=10 + off + tech,trend3_boost,nest_soft,fund,value`。第十二轮补齐创业板后，该候选不再作为 A 股全市场默认；当前默认以 `max_pos=30 + off + tech,fund,value` 为准。
5. 美股核心 9 只在线池已在第十一轮确认 `trend3_boost,nest_soft` 方向有效；缓存小池 QQQ/TSLA 仅作为历史对照，不再作为美股默认结论。

## 20. 图表展示契约

2026-06-11 已对 `cl_data_to_tv_chart` 增加图表契约测试，确认当前展示层按 branch core 路径输出多级别结构。前端不再依赖旧的 `higher_zs` 真实多周期叠加，而是统一消费 `recursive_levels`：

1. 1m K 线：展示笔 `bis`、笔买卖点 `bi_mmds`、笔背驰 `bi_bcs`；本周期线段/1m 中枢在 `recursive_levels[L0]`；5m 中枢/买卖点/背驰在 `recursive_levels[L1]`；30m 中枢/买卖点/背驰在 `recursive_levels[L2]`。
2. 5m K 线：展示笔 `bis`、5m 本级结构在 `recursive_levels[L0]`；30m 中枢/买卖点/背驰在 `recursive_levels[L1]`。
3. 30m K 线：展示笔 `bis`、30m 本级结构在 `recursive_levels[L0]`；30m 不再向上升级，符合“30m 作为操作级别封顶”的规则。
4. `recursive_levels[].zss` 画对应级别中枢，`recursive_levels[].mmds` 画对应级别一二三类买卖点，`recursive_levels[].bcs` 画对应级别背驰；前端 `charts.js` 用 `zs_L1/zs_L2`、`mmd_L1/mmd_L2`、`bc_L1/bc_L2` 分级 toggle。
5. 频率标签由 `_kuozhan_freq_label` 固化：1m 的 L1/L2 分别标为 `5m`/`30m`，5m 的 L1 标为 `30m`。即使某个样本在某级别没有实际买卖点/背驰，也必须保留该级容器，避免前端菜单和数据结构漂移。
6. 前端菜单动态生成 `mmd_L*/bc_L*` 后，必须把这些 key 注册进统一 change handler；否则用户能看到 5m/30m 买卖点、背驰开关，但切换不会保存配置或触发重绘。

新增测试：

| 测试 | 覆盖 |
| --- | --- |
| `test_cl_data_to_tv_chart_multitimeframe_overlay_contract` | 真实 301004 fixture 验证 1m/5m/30m 容器、笔、买卖点、背驰字段 |
| `test_cl_data_to_tv_chart_serializes_l1_l2_mmds_and_bcs` | 注入 L1/L2 买卖点和背驰，验证 5m/30m 标签进入 `recursive_levels[].mmds/bcs` |
| `test_recursive_signal_menu_toggles_are_registered` | 静态验证 `mmd_L*/bc_L*` 高级别买卖点/背驰菜单开关已注册重绘监听 |

当前图表路径与理论约束一致：30m 是同级别分解封顶；30m 以下使用非同级别扩展链生成高级别中枢，但在显示时仍按实际频率标签展示。

## 21. 第十一轮美股核心池在线验证

2026-06-11 使用长桥在线历史 K 线扩展美股样本池，从 QQQ/TSLA 小池推进到 9 只高流动性核心池：

`SPY.US, QQQ.US, AAPL.US, MSFT.US, NVDA.US, AMZN.US, META.US, GOOGL.US, TSLA.US`

回测窗口为 2026-03-02 22:30:00+08:00 至 2026-06-10 23:59:00+08:00，级别为 `1m+5m+30m`，自动仓位槽为 `max_pos=9`。

| 市场 | 池 | 级别 | require | max_pos | 收益 | 基准 | 超额 | 回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 美股 | 在线核心 9 只 | 1m+5m+30m | tech | 9 | +12.2% | +9.1% | +3.0% | 1.3% | 11.3% | 7.99 | 71% | 244 |
| 美股 | 在线核心 9 只 | 1m+5m+30m | tech,trend3_boost,nest_soft | 9 | +14.9% | +9.1% | +5.7% | 1.6% | 11.3% | 8.15 | 71% | 244 |

分段结果：

| 策略 | 分段 | 策略收益 | 基准收益 | 超额 | 回撤 | 交易 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| tech | bull | +10.0% | +17.6% | -7.6% | 0.9% | 155 |
| tech | range | +2.2% | -3.1% | +5.3% | 0.7% | 86 |
| tech | bear | -0.3% | -5.8% | +5.5% | 0.3% | 3 |
| trend3_boost+nest_soft | bull | +12.5% | +17.6% | -5.1% | 1.0% | 155 |
| trend3_boost+nest_soft | range | +2.4% | -3.1% | +5.5% | 0.9% | 86 |
| trend3_boost+nest_soft | bear | -0.4% | -5.8% | +5.4% | 0.4% | 3 |

交易类别分布：三买 219 笔、一买 21 笔、二买 4 笔。美股收益提升主要来自三买趋势延续仓位，而不是放大背驰定位型买点；该判断与缺创业板的第十轮 A 股子集一致，但第十二轮真全 A 已把 A 股默认修正为不启用 `trend3_boost/nest_soft`。

实盘配置同步：

1. `RECURSIVE_MONITOR_CONFIG["us"]["codes"]` 固化上述 9 只核心池。
2. 美股实时监控默认 `max_pos=9`，`trend_3boost=true`，`nest_mode="soft"`。
3. 长桥 2026-06 月度 history kline 配额已使用 9 个 symbol，`exhausted=false`；后续扩展到更多美股池前必须继续检查配额文件。

## 22. 第十二轮补齐创业板后的真全 A 验证

2026-06-11 审计发现第八至第十轮所谓“全 A 3746/3748”缓存没有创业板，因此只能作为缺创业板子集证据，不能再作为最终全市场默认。随后补齐创业板缓存并重跑不设 `selection_scan_limit` 的 walk-forward。

数据覆盖修正：

1. QMT `universe_all_a` 返回 5550 只：北交所 343、主板 3201、科创板 608、创业板 1398。
2. 技术缓存 `D:/chanlun_pro/bt_data_all_a` 补齐后 5145 个文件：北交所 322、主板 2818、创业板 1397、科创板 608；创业板仅 `SZ.301669` 因样本不足未形成有效技术缓存。
3. 基本面缓存 `D:/chanlun_pro/bt_data_fund_all_a` 补齐后 5145 个文件：北交所 322、创业板 1398、主板 2817、科创板 608。
4. 组合回测实际可加载 5143 只：北交所 321、创业板 1397、主板 2817、科创板 608；`fund_ok` 为 3122/5143，价值阈值中位数约 4.52，`value_ok` 为 3016/5143。

真全 A 对照均使用 `5m+30m`、`bt_pool_mode=walk_forward`、`selection_board_filter=all`、`regime_mode=off`：

| require | max_pos | 收益 | 基准 | 超额 | 回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tech,fund,value` | 10 | +82.2% | +30.0% | +52.2% | 8.7% | 13.9% | 2.82 | 48% | 460 |
| `tech,trend3_boost,nest_soft,fund,value` | 10 | +90.3% | +30.0% | +60.4% | 9.0% | 13.9% | 2.91 | 49% | 457 |
| `tech,fund,value` | 20 | +118.4% | +30.0% | +88.5% | 7.9% | 13.9% | 4.37 | 49% | 968 |
| `tech,trend3_boost,nest_soft,fund,value` | 20 | +105.6% | +30.0% | +75.7% | 8.1% | 13.9% | 3.79 | 50% | 951 |
| `tech,fund,value` | 25 | +122.4% | +30.0% | +92.5% | 7.4% | 13.9% | 4.73 | 49% | 1218 |
| `tech,fund,value` | 30 | +128.1% | +30.0% | +98.2% | 7.0% | 13.9% | 5.10 | 50% | 1451 |
| `tech,fund,value` | 40 | +110.3% | +30.0% | +80.4% | 7.3% | 13.9% | 4.87 | 49% | 1937 |
| `tech,fund,value` | 50 | +109.7% | +30.0% | +79.8% | 6.7% | 13.9% | 5.05 | 50% | 2409 |

`max_pos=30 + tech/fund/value` 分段：

| 分段 | 策略收益 | 基准收益 | 超额 | 分段回撤 | 夏普 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bull | +51.0% | +34.4% | +16.7% | 2.6% | 9.31 | 446 |
| range | +51.5% | +1.9% | +49.6% | 4.3% | 4.29 | 866 |
| bear | -0.3% | -4.7% | +4.4% | 4.2% | 0.00 | 139 |

交易结构诊断：

1. `max_pos=30` 共 1451 笔，三买 1324 笔、一买 80 笔、二买 17 笔、未分类 30 笔。收益主要来自第三类买点，但补齐创业板后不再需要额外 `trend3_boost` 才能取得最优组合收益。
2. 按板块看，主板 1095 笔、创业板 149 笔、科创板 93 笔、北交所 114 笔；平均单笔收益约为主板 +1.86%、创业板 +2.99%、科创板 +3.55%、北交所 +0.30%。创业板和科创板显著贡献收益，旧 3746 子集低估了全 A 机会集。
3. `trend3_boost + nest_soft` 在 10 槽子集里仍能提高收益，但在真全 A 的 20 槽以上组合中降低收益或提高回撤，不再作为 A 股默认。

当前配置结论：

1. A 股收益默认的全市场回测证据为：`max_pos=30`、`regime_mode=off`、`require=tech,fund,value`、`nest_mode=off`、`trend_3boost=false`。基础单槽为 3.33%；三买默认 3.33%，二买约 2.50% 到 3.00%，一买约 1.67% 到 2.67%，具体由 30m 方向和日线共振决定。
2. A 股实盘通知执行层为：`op_level=1m`、`mid_level=5m`、`big_level=30m`。全市场 selector 仍用 5m+30m 三系统过滤选出候选池，但在 1m 模式下 selector 候选只负责入池，不直接发送买点；最终买点由 1m 买点 + 30m not_down 触发，5m not_down 正常仓位，5m down 仅在 `mid_gate=soft` 下折扣仓位通过。
3. A 股低回撤候选：`max_pos=50 + off + tech/fund/value`，收益 +109.7%，回撤 6.7%，基础单槽为 2.00%。适合低波动账户，但收益低于 30 槽默认。
4. A 股小资金或集中组合不再使用旧 `max_pos=10 + trend3_boost+nest_soft` 作为默认；如果必须限制到 10 槽，可把它作为研究开关，而不是全市场主配置。
5. 美股仍保留第十一轮在线核心 9 只结论：`max_pos=9 + op_level=1m + mid_level=5m + big_level=30m + trend_3boost=true + nest_mode=soft`。A 股与美股的趋势加仓开关分化，是由各自真实回测结果决定的，不再强行统一。
6. 实盘通知必须继续输出买入比例和卖出比例；A 股买点通知按 30 槽比例解释，美股买点通知按 9 槽比例解释，卖点仍按当前持仓中对应标的的可卖比例处理。

## 23. 第十三轮 A 股 1m+5m+30m 小池验证

2026-06-11 将 `live_backtest --source bt_data` 放宽为两类缓存：普通 `5m+30m` 缓存，或显式 `1m+5m+30m` 且含 `mid_dir_at` 的三级联立缓存。这样 `D:/chanlun_pro/bt_data_mtf3` 可以进入统一回测入口；若普通缓存伪装成 `1m+5m+30m` 会直接报错，避免把 5m 信号误当 1m 信号。

当前本地 `bt_data_mtf3` 为 50 只可交易标的加上上证指数，操作级别约 58,684 根 1m bar。该样本偏向上证 50/大盘高流动性股票，只用于验证 1m 执行层和中级别门控，不替代第十二轮 5143 只真全 A 默认。

`1m+5m+30m` 严格三级联立结果：

| require | max_pos | 收益 | 基准 | 超额 | 回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `tech` | 5 | +116.4% | +17.9% | +98.4% | 9.4% | 13.2% | 3.77 | 52% | 775 |
| `tech` | 10 | +70.6% | +17.9% | +52.7% | 4.4% | 13.2% | 4.12 | 50% | 1202 |
| `tech` | 20 | +37.0% | +17.9% | +19.1% | 2.1% | 13.2% | 4.61 | 50% | 1354 |
| `tech` | 30 | +21.8% | +17.9% | +3.9% | 1.4% | 13.2% | 4.48 | 50% | 1362 |
| `tech,trend3_boost` | 5 | +149.7% | +17.9% | +131.8% | 10.0% | 13.2% | 3.98 | 52% | 775 |
| `tech,trend3_boost` | 10 | +88.1% | +17.9% | +70.1% | 4.8% | 13.2% | 4.21 | 50% | 1200 |
| `tech,trend3_boost` | 20 | +45.7% | +17.9% | +27.8% | 2.7% | 13.2% | 4.52 | 50% | 1362 |

中级别门控诊断，固定 `max_pos=10 + tech`：

| 口径 | 收益 | 基准 | 超额 | 回撤 | 夏普 | 胜率 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1m 买点 + 5m not_down + 30m not_down | +70.6% | +17.9% | +52.7% | 4.4% | 4.12 | 50% | 1202 |
| 1m 买点 + 30m not_down，去掉 5m 门控 | +120.9% | +17.9% | +102.9% | 6.5% | 5.04 | 53% | 1727 |

本轮结论：

1. `1m+5m+30m` 实盘执行链条已经可由统一回测入口验证，且严格三级联立在小池中显著跑赢基准，回撤低于基准。
2. 小池里 `trend3_boost` 对 1m 执行层有效：`max_pos=10` 收益从 +70.6% 提升到 +88.1%，回撤从 4.4% 升到 4.8%。但第十二轮真全 A 5m 回测不支持 A 股默认启用该开关，因此它暂时只作为 1m 小池研究候选。
3. 严格 5m 门控明显降回撤，但也错过较多 1m 机会；去掉 5m 门控收益更高、回撤也更高。本轮不因此取消高低级别联立，而是把“5m soft gate/仓位折扣”列为下一轮优化方向；第十四轮已实现并验证。
4. 1m 小池与真全 A 的最优槽位不同：小池收益优先为 5 槽，稳健为 10-20 槽；真全 A 5m 证据收益默认仍为 30 槽。实盘 A 股当前继续用全市场 30 槽资金框架，1m 信号只作为更精细的入场触发，不把 50 只小池结论直接覆盖到全 A。

## 24. 第十四轮 5m soft gate

2026-06-11 根据第十三轮诊断实现 `mid_gate=soft`：当 1m 出现买点、30m 不向下，但 5m 当前为 down 时，不再直接丢弃信号，而是允许通过并把建议买入比例打 5 折。其缠论含义是：30m 是操作级别同级别分解封顶，5m 是入场前的中级别约束；5m 下行代表小级别回抽仍未结束，不能满仓确认，但若 30m 没有破坏，1m 买点可作为试探仓，而不是完全忽略。

实现约束：

1. `mid_gate=strict`：5m down 直接过滤，保持第十三轮稳健基线。
2. `mid_gate=soft`：5m down 允许买点通过，但买入比例乘以 0.5；通知 reason 标注 `5m soft_down_discount`。
3. `mid_gate=bull_relaxed`：保留原研究开关，只在 `regime_mode=adaptive`、30m up、三买时放松。
4. 30m down 仍然禁止新买入；soft gate 只作用在中级别 5m，不改变大级别风控。

`bt_data_mtf3` 50 只小池对照：

| 口径 | max_pos | require | 收益 | 基准 | 超额 | 回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| strict | 5 | `tech` | +116.4% | +17.9% | +98.4% | 9.4% | 13.2% | 3.77 | 52% | 775 |
| soft | 5 | `tech` | +70.5% | +17.9% | +52.6% | 7.0% | 13.2% | 3.17 | 53% | 864 |
| strict | 10 | `tech` | +70.6% | +17.9% | +52.7% | 4.4% | 13.2% | 4.12 | 50% | 1202 |
| soft | 10 | `tech` | +79.1% | +17.9% | +61.2% | 4.8% | 13.2% | 4.69 | 53% | 1713 |
| no 5m gate | 10 | `tech` | +120.9% | +17.9% | +102.9% | 6.5% | 13.2% | 5.04 | 53% | 1727 |
| strict | 20 | `tech` | +37.0% | +17.9% | +19.1% | 2.1% | 13.2% | 4.61 | 50% | 1354 |
| soft | 20 | `tech` | +50.7% | +17.9% | +32.8% | 2.2% | 13.2% | 5.40 | 53% | 2539 |
| strict | 30 | `tech` | +21.8% | +17.9% | +3.9% | 1.4% | 13.2% | 4.48 | 50% | 1362 |
| soft | 30 | `tech` | +30.7% | +17.9% | +12.8% | 1.4% | 13.2% | 5.35 | 53% | 2580 |
| soft | 10 | `tech,trend3_boost` | +102.4% | +17.9% | +84.5% | 5.6% | 13.2% | 4.63 | 53% | 1718 |
| soft | 20 | `tech,trend3_boost` | +66.0% | +17.9% | +48.0% | 2.7% | 13.2% | 5.42 | 53% | 2563 |
| soft | 30 | `tech,trend3_boost` | +41.5% | +17.9% | +23.5% | 1.8% | 13.2% | 5.54 | 53% | 2628 |

第十四轮结论：

1. `mid_gate=soft` 在当前 A 股实盘默认的 30 槽框架上优于 strict：收益从 +21.8% 提升到 +30.7%，回撤仍为 1.4%，夏普从 4.48 提升到 5.35。
2. 20 槽稳健框架也明显受益：收益从 +37.0% 提升到 +50.7%，回撤仅从 2.1% 到 2.2%。
3. 5 槽集中进攻版不适合 soft：收益从 +116.4% 降到 +70.5%，说明集中仓位需要更高确认度。
4. `trend3_boost` 在 1m 小池继续有效，但第十二轮真全 A 5m 证据仍不支持把它设为 A 股默认；当前只把 `mid_gate=soft` 同步到 A 股实盘配置，不同步 `trend_3boost`。
5. A 股实时默认更新为：`max_pos=30 + op_level=1m + mid_level=5m + big_level=30m + mid_gate=soft + trend_3boost=false + nest_mode=off`。买点通知在 5m down 时必须显示折扣后的买入比例。

## 25. 第十五轮美股核心池 soft gate 在线验证

2026-06-11 沿用第十一轮同一批美股核心 9 只与同一在线窗口，验证 `mid_gate=soft` 是否也适用于美股实时系统。窗口为 2026-03-02 22:30:00 至 2026-06-10 23:59:00，本地时区口径；级别为 `1m+5m+30m`，核心池仍为：
`SPY.US, QQQ.US, AAPL.US, MSFT.US, NVDA.US, AMZN.US, META.US, GOOGL.US, TSLA.US`。

| 口径 | max_pos | 收益 | 基准 | 超额 | 回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| strict `tech` | 9 | +12.2% | +9.1% | +3.0% | 1.3% | 11.3% | 7.99 | 71% | 244 |
| soft `tech` | 9 | +16.2% | +9.1% | +7.1% | 1.3% | 11.3% | 9.10 | 70% | 450 |
| strict `tech,trend3_boost,nest_soft` | 9 | +14.9% | +9.1% | +5.7% | 1.6% | 11.3% | 8.15 | 71% | 244 |
| soft `tech,trend3_boost,nest_soft` | 9 | +19.6% | +9.1% | +10.4% | 1.5% | 11.3% | 9.31 | 70% | 450 |

soft `trend3_boost+nested` 交易结构：三买 359 笔、一买 67 笔、二买 23 笔、收尾 1 笔。和 strict 版相比，soft gate 没改变买点定义，而是把原先因 5m down 被过滤的 1m 买点以折扣仓位纳入，因此交易数从 244 增至 450，但最大回撤仍维持在约 1.5%。

第十五轮结论：

1. 美股核心池明确支持 `mid_gate=soft`：收益从 +14.9% 提升到 +19.6%，回撤从 1.6% 小降到 1.5%，夏普从 8.15 提升到 9.31。
2. 美股仍保留 `trend_3boost=true + nest_mode=soft`，因为该组合在第十一轮和第十五轮均优于纯 `tech`。
3. 美股实时默认更新为：`max_pos=9 + op_level=1m + mid_level=5m + big_level=30m + mid_gate=soft + trend_3boost=true + nest_mode=soft`。
4. A 股与美股现在共享“30m 硬风控、5m soft 仓位调节、1m 触发”的执行骨架；差异只保留在趋势三买加仓和区间套调仓开关上，按各自市场回测证据决定。

## 26. 第十六轮仿实盘撮合规则对齐

2026-06-11 审计发现旧 `paper.py` 纸面账户仍是 A 股 5m/30m 时代的撮合假设：固定 `MAX_POS=10`、固定 100 股、固定 A 股佣金/印花税/T+1，并且涨跌停只区分主板与创业/科创。实时通知和回测已经升级到 A/US 多市场后，纸面账户必须至少在撮合规则上与市场规则一致。

本轮修正：

1. `PaperBroker` 新增 `market` 与 `max_pos` 参数；默认仍兼容 A 股旧入口。
2. 买卖撮合统一通过 `market_rules_for_code(market, code)` 获取规则。
3. A 股主板/创业板/科创板/北交所分别使用 10%/20%/20%/30% 涨跌停，100 股交易单位，T+1，卖出印花税。
4. 美股使用 T+0、1 股单位、无涨跌停、无印花税；同日买入后若出现卖点可在纸面账户中卖出。
5. 买入目标金额使用通知层同源的 `target_weight`；若没有给出比例，才退回 `1 / max_pos`。
6. 涨停买入仍按“错过该信号”处理，不顺延陈旧买单；跌停卖出和 A 股 T+1 卖出继续顺延。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_paper_broker_uses_us_t0_and_lot_one_rules` | 美股不按 100 股取整，且同日可卖出 |
| `test_paper_broker_respects_a_share_t1_and_bj_limit` | 北交所 30% 涨停买不进，A 股同日卖出被 T+1 顺延，A 股仍按 100 股成交 |
| `test_market_runtime_uses_board_specific_a_share_limits` | A 股主板/创业板/北交所涨跌停规则由统一市场规则函数返回 |

当前状态：回测、通知、paper broker 三者已经在“买入比例、卖出比例、交易单位、T+1/T+0、涨跌停/无涨跌停”上使用同一套市场规则。`paper.py` 的旧循环入口仍主要服务 A 股缓存烟测；真正的 A/US 实时扫描以 `live_monitor/app_monitor` 为准。

## 27. 第十七轮实盘通知到账户执行闭环

2026-06-11 继续审计发现，仅让实时通知文本显示买入比例、卖出比例还不够；纸面账户必须直接消费 `live_monitor/app_monitor` 产出的事件，才能验证“信号 -> 通知 -> 挂单 -> 撮合 -> 仓位变化”的完整链路。否则后续收益统计仍可能与通知层的仓位建议脱节。

本轮修正：
1. `PaperBroker.queue_events(events)` 新增 MonitorEvent 风格事件入口：`side=buy` 时把 `buy_ratio` 写入挂单 `target_weight`，`side=sell/exit` 时把 `sell_ratio` 写入卖出挂单。
2. 买入事件继续去重：已持仓或已有买入挂单的标的不重复排队；`buy_ratio<=0` 的事件直接忽略。
3. 卖出事件仅对已持仓标的排队；`sell_ratio` 被限制在 0 到 1 之间，防止通知层异常值放大执行。
4. `fill_pending` 支持部分卖出：例如 `sell_ratio=0.25` 只卖出当前持仓的 25%，剩余持仓继续保留；A 股仍按 100 股交易单位取整，美股按 1 股单位处理。
5. `live_monitor` 与 `app_monitor` 均接入纸面执行开关：只对 deduper 判定为“新鲜且已发送”的事件排队，先撮合上一轮 pending，再写入本轮新挂单并保存 ledger。
6. A 股和美股实时配置默认 `paper_enabled=true`，分别写入 `D:/chanlun_pro/paper/ledger.json` 与 `D:/chanlun_pro/paper_us/ledger.json`；这仍是本地仿实盘账本，不触碰真实券商下单接口。CLI 可用 `--paper-enabled/--no-paper` 显式覆盖。
7. `1m` 默认回看窗口恢复为 365 天；注释、测试和多级递归图表/实盘系统的历史窗口需求重新一致。90 天窗口容易让 1m 图表的 L1/L2/L3 递归层级被历史长度饿死，不能作为当前默认。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_paper_broker_queues_monitor_events_with_ratios` | 实时买入/卖出事件的 `buy_ratio`、`sell_ratio` 能进入 paper broker 挂单 |
| `test_paper_broker_queue_events_respects_max_pos` | 挂单队列不超过 `max_pos` 可用买入槽位 |
| `test_paper_broker_partial_sell_uses_sell_ratio` | 纸面账户按 `sell_ratio` 部分卖出，并保留剩余持仓 |
| `test_dynamic_monitor_paper_executes_fresh_events_to_ledger` | web scheduler 动态监控器第一轮排队、第二轮按开盘价撮合成持仓 |
| `test_live_monitor_cli_accepts_paper_switches` | CLI 支持 `--paper-enabled/--no-paper` 开关 |
| `test_exchange_lookback.py` | `1m` 公共回看窗口保持 365 天，所有 exchange 共享同一锚点 |

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `773 passed, 1 skipped`。当前闭环状态为：图表层展示多级中枢/买卖点/背驰，实时通知层给出买入比例和卖出比例，实时入口把新鲜事件写入 paper broker，paper broker 再按同一套 A/US 市场规则执行这些比例。

## 28. 第十八轮仿实盘绩效曲线

2026-06-11 继续审计发现，上一轮虽然已经把实时事件写入 paper broker，但 ledger 仍只记录现金、持仓、挂单和逐笔交易。这样可以复盘交易明细，却不能稳定回答“当前仿实盘收益、最大回撤、胜率是多少”，也不能为后续自动优化提供连续目标函数。因此本轮把权益曲线和摘要指标纳入 paper broker 的持久化账本。

本轮修正：
1. `PaperBroker` 新增 `equity_curve` 字段，随 ledger 一起加载和保存。
2. 新增 `record_snapshot(states, now)`：按当前 `last_px` 标记持仓市值，记录 `time/equity/cash/positions/pending/trades`。
3. 新增 `performance_summary()`：输出 `start_equity/latest_equity/total_return/max_drawdown/positions/pending/trades/win_rate`。
4. `live_monitor` 和 `app_monitor` 在每轮 paper 执行后都记录权益快照；CLI 输出追加 `paper_equity/paper_return/paper_dd`，web scheduler 返回值追加 `paper_equity/paper_return/paper_max_drawdown`。
5. 旧 `paper.step` 循环也记录快照并打印收益、回撤，避免新旧仿实盘入口指标口径分裂。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_paper_broker_records_equity_curve_and_summary` | paper ledger 能保存权益曲线、收益、最大回撤，并在重载后保留 |
| `test_dynamic_monitor_paper_executes_fresh_events_to_ledger` | 动态实时监控器写入挂单、撮合成持仓，同时生成权益曲线和 summary |

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `774 passed, 1 skipped`。当前仿实盘闭环从“信号和执行”推进到“信号、执行、权益曲线、回撤摘要”，后续可以直接以 paper ledger 的 `summary.total_return` 与 `summary.max_drawdown` 作为持续优化目标。

## 29. 第十九轮策略候选目录与统一评分

2026-06-11 继续推进“根据回测结果和仿实盘结果持续优化”的工程闭环。此前 A/US 默认策略、低回撤候选、进攻候选主要写在文档和配置里，缺少一个可被程序读取的候选目录，也缺少统一的收益-回撤评分函数。本轮新增 `strategy_optimizer.py`，把当前证据支持的候选策略固化为代码。

三套 A 股独立选股系统现在由 `a_selection_systems()` 明确定义：
1. `fundamental`：点时可见的质量/成长确认，主要约束 ROE、营收/利润增速。
2. `comparison`：比价/相对低估确认，当前用 `ROE/PB` 风格的全市场中位比较。
3. `technical`：缠论买点确认，使用 1/2/3 类买点、30m 不向下、5m soft gate 与买入比例。

当前嵌入候选：

| 市场 | 候选 ID | 角色 | max_pos | 级别 | 关键开关 | 嵌入证据 |
| --- | --- | --- | ---: | --- | --- | --- |
| A 股 | `a_full_market_balanced` | 当前默认 | 30 | 1m+5m+30m | `tech,fund,value`, `mid_gate=soft` | 真全 A +128.1%，回撤 7.0%，夏普 5.10 |
| A 股 | `a_full_market_low_dd` | 低回撤候选 | 50 | 1m+5m+30m | `tech,fund,value`, `mid_gate=soft` | 真全 A +109.7%，回撤 6.7%，夏普 5.05 |
| A 股 | `a_concentrated_trend3_research` | 研究开关 | 10 | 1m+5m+30m | `trend_3boost=true`, `nest_mode=soft` | 真全 A 10 槽 +90.3%，回撤 9.0% |
| 美股 | `us_core9_default` | 当前默认 | 9 | 1m+5m+30m | `mid_gate=soft`, `trend_3boost=true`, `nest_mode=soft` | 核心 9 +19.6%，回撤 1.5%，夏普 9.31 |
| 美股 | `us_core9_tech_soft` | 技术基线 | 9 | 1m+5m+30m | `mid_gate=soft` | 核心 9 +16.2%，回撤 1.3%，夏普 9.10 |
| 美股 | `us_core9_strict_control` | strict 对照 | 9 | 1m+5m+30m | `mid_gate=strict`, `trend_3boost=true`, `nest_mode=soft` | 核心 9 +14.9%，回撤 1.6%，夏普 8.15 |

统一评分函数 `score_summary()` 使用同一套口径消费两类输入：
1. `live_backtest` 输出的 summary：读取 `total/max_dd/sharpe/trade_count`。
2. paper ledger 输出的 summary：读取 `total_return/max_drawdown/trades/win_rate`。

当前默认权重为：收益加分、最大回撤按 2 倍惩罚、夏普小权重加分、交易数低于 10 笔轻微惩罚。它的定位不是取代人工研究，而是让每轮 backtest/paper 结果能进入同一张排名表，避免只凭单一收益或单一回撤做选择。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_a_selection_systems_define_three_independent_confirmations` | A 股三套独立选股系统已程序化定义 |
| `test_strategy_candidates_codify_current_market_defaults` | 当前 A/US 默认候选在嵌入证据下排名第一 |
| `test_score_summary_penalizes_drawdown_for_same_return` | 同收益下评分会惩罚更高回撤 |
| `test_summary_from_paper_ledger_feeds_optimizer` | paper ledger summary 可以直接进入统一评分器 |
| `test_build_candidate_report_is_serializable_and_ranked` | 候选报告可序列化并保持排序 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_backtest_live_parity.py tests/test_recursive_app_monitor.py tests/test_recursive_live_monitor.py` 通过，结果为 `65 passed`。后续每次回测或实时 paper ledger 更新后，可以把 summary 喂给 `rank_summary_records()`，按统一目标选择“收益/回撤”更优的候选。

## 30. 第二十轮自动策略优化报告

2026-06-11 在第十九轮候选目录和评分函数之上，新增自动报告生成能力。`python -m chanlun.recursive_bt.strategy_optimizer` 会读取：
1. A/US 默认 paper ledger：`default_ledger_path(market)`。
2. A/US 默认 live parity backtest summary：`default_backtest_report_paths(market)[0]`。
3. `D:/chanlun_pro/reports/*_summary.json` 中可识别市场的历史回测摘要。

报告输出：
1. `D:/chanlun_pro/reports/strategy_optimization_report.json`：机器可读，用于后续自动策略选择。
2. `D:/chanlun_pro/reports/strategy_optimization_report.md`：人可读，包含嵌入候选排名、runtime summary 排名、缺失数据源。

本轮实现：
1. `RuntimeSummarySource`：统一描述 paper ledger 与 backtest summary 的来源。
2. `score_runtime_sources()`：读取、去重、评分、排序，并把缺失/空摘要记录到 `missing_sources`，不中断报告生成。
3. `discover_backtest_summary_sources()`：扫描报告目录，按 summary 内容或文件名推断 A/US 市场。
4. `build_optimization_report()` / `write_optimization_report()`：合并嵌入候选和 runtime 摘要，生成推荐项。
5. `render_optimization_markdown()`：输出 Markdown 排名表，方便人工巡检。

真实生成结果摘要：
1. 嵌入候选数：6。
2. runtime summary 数：112。
3. 缺失源：2（当前为本地 paper ledger 缺失或尚未有足够实时轮次时的正常状态）。
4. 当前推荐：
   - A 股嵌入候选：`a_full_market_balanced`；runtime 最优：`a_live_parity_backtest`。
   - 美股嵌入候选：`us_core9_default`；runtime 最优：`us_online_core9_1m5m30m_trend3_nest_soft_midsoft`。

runtime 排名前五：

| Rank | 市场 | Source | Score | Return | Max DD | Trades |
| ---: | --- | --- | ---: | ---: | ---: | ---: |
| 1 | A | `a_live_parity_backtest` | 1.4779 | +157.0% | 7.0% | 512 |
| 2 | A | `live_parity_backtest` | 1.4779 | +157.0% | 7.0% | 512 |
| 3 | A | `walk_forward_a_mtf3_51_1m5m30m_max5_trend3` | 1.3368 | +149.7% | 10.0% | 775 |
| 4 | A | `walk_forward_a_5m30m_all5145_max30_off_segments` | 1.1924 | +128.1% | 7.0% | 1451 |
| 5 | A | `walk_forward_a_5m30m_shsz600_max10_adaptive` | 1.1439 | +128.2% | 9.0% | 462 |

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_score_runtime_sources_handles_paper_and_backtest_summaries` | paper ledger、backtest summary、缺失源可统一处理，且同一路径不会重复计分 |
| `test_discover_and_write_optimization_report` | 临时报告目录可发现 summary，并写出 JSON/Markdown |
| `test_build_optimization_report_merges_candidates_and_runtime` | 嵌入候选与 runtime summary 能合并成同一推荐报告 |

本轮后，系统已经具备“回测/仿实盘结果 -> 自动评分 -> 策略候选排名报告”的闭环入口。下一步应让定时任务或实时监控每轮结束后自动刷新该报告，并在候选排名发生变化时输出策略切换建议。

## 31. 第二十一轮实时监控自动刷新优化报告

2026-06-11 继续把第 30 轮的自动报告生成能力接入实时监控闭环：每轮实时扫描在完成信号去重、通知、paper broker 撮合、权益快照和 ledger 保存后，会刷新策略优化报告。这样仿实盘账本或回测 summary 一旦更新，就会进入同一套评分函数，避免“运行中有新结果，但优化报告仍停留在旧证据”的断层。

配置入口统一放在 `RECURSIVE_MONITOR_CONFIG["common"]`：
1. `optimization_report_enabled=true`：默认开启实时监控后的报告刷新。
2. `optimization_report_json="D:/chanlun_pro/reports/strategy_optimization_report.json"`。
3. `optimization_report_markdown="D:/chanlun_pro/reports/strategy_optimization_report.md"`。
4. `optimization_report_dir="D:/chanlun_pro/reports"`：用于扫描历史 `*_summary.json`。
5. `optimization_report_include_discovered=true`：默认纳入报告目录中自动发现的历史回测摘要。

实现约束：
1. `live_monitor.refresh_optimization_report()` 是实时入口的统一适配层，内部调用 `strategy_optimizer.write_optimization_report()`，并返回 JSON/Markdown 路径、嵌入候选数、runtime summary 数、缺失源数和推荐项。
2. `live_monitor.run_once()` 在 paper broker 记录权益快照之后刷新报告；CLI 状态行追加 `opt_runtime` 与 `opt_missing`，方便无人值守日志巡检。
3. `DynamicRecursiveMonitor.run_once()` 同步接入刷新结果，web scheduler 返回值新增 `optimization_report` 字段。
4. CLI 支持 `--optimization-report-enabled`、`--no-optimization-report`、`--optimization-report-json`、`--optimization-report-markdown`、`--optimization-report-dir`、`--no-optimization-report-discover`；命令行覆盖配置，配置再覆盖代码默认值。
5. 报告刷新只消费本地 paper ledger 与回测摘要，不触碰真实券商交易接口，也不改变买卖点定义；它只负责把当前证据重新排序。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器每轮结束后能写出 JSON/Markdown 优化报告，并返回推荐结果 |
| `test_dynamic_monitor_config_reads_project_config` | common 配置中的优化报告路径、目录和发现开关能被动态监控器读取 |
| `test_live_monitor_cli_accepts_optimization_report_switches` | CLI 能显式启停优化报告刷新并覆盖输出路径/扫描目录 |
| `test_default_recursive_monitor_config_tracks_latest_live_candidates` | 默认实时配置保持 A/US 最新候选口径，并默认开启 paper 与优化报告闭环 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_app_monitor.py tests/test_recursive_live_monitor.py tests/test_backtest_live_parity.py` 通过，结果为 `70 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `785 passed, 1 skipped`。

当前闭环状态更新为：图表层展示 1m/5m/30m 多级中枢、买卖点和背驰；实时通知层输出买入比例和卖出比例；paper broker 按 A/US 市场规则执行这些比例并记录权益曲线；策略优化器在每轮实时监控后刷新候选排名。下一步的优化重点从“能生成报告”转为“根据报告变化生成明确的策略切换/降级建议”，尤其要继续比较 A 股 30 槽默认、50 槽低回撤候选、美股 core9 趋势三买区间套候选在持续仿实盘中的收益和回撤稳定性。

## 32. 第二十二轮策略动作建议

2026-06-11 在自动优化报告基础上新增 `action_suggestions`，把“候选排名”进一步转化为可执行的策略动作。该层不改变缠论信号定义，也不自动改写配置；它只根据嵌入候选证据、runtime summary 和当前监控配置输出建议，供后续无人值守任务或人工巡检使用。

动作口径：
1. `keep_candidate`：当前实时配置能匹配最高分嵌入候选，继续运行。
2. `switch_candidate`：当前实时配置未匹配最高分嵌入候选，建议切换到报告中的 `target_candidate`。
3. `degrade_candidate`：当前 paper ledger 的最大回撤明显超过候选证据阈值，且存在更低回撤嵌入候选，建议降级到低回撤候选。
4. `review_runtime_gap`：当前 paper ledger 分数显著落后最优 runtime summary，但未触发回撤降级，建议人工或自动任务复查数据窗口、市场状态和候选差异。

实现约束：
1. `build_action_suggestions(report, current_candidate_ids=...)` 负责从报告生成动作建议，输出 `market/action/current_candidate/target_candidate/best_runtime_summary/reason/monitor_config`。
2. `match_candidate_from_monitor_config(config, market)` 负责把实时监控配置匹配到嵌入候选；例如当前美股 `max_pos=9 + 1m/5m/30m + mid_gate=soft + nest_mode=soft + trend_3boost=true` 会匹配 `us_core9_default`。
3. `write_optimization_report()` 与 `build_optimization_report()` 写出的 JSON/Markdown 默认包含 `action_suggestions`。
4. `live_monitor.refresh_optimization_report()` 接收 `current_config`，实时刷新时把当前运行配置写入建议层；CLI 日志追加 `opt_action`。
5. `DynamicRecursiveMonitor.run_once()` 把 web scheduler 的当前配置传给报告刷新入口，返回值继续带 `optimization_report`，其中包含动作建议。

当前实际报告含义：
1. A 股默认配置匹配 `a_full_market_balanced` 时，动作应为 `keep_candidate`；若仿实盘回撤持续恶化并超过阈值，低回撤降级目标为 `a_full_market_low_dd`。
2. 美股默认配置匹配 `us_core9_default` 时，动作应为 `keep_candidate`；若 soft 趋势三买组合失效，低回撤候选为 `us_core9_tech_soft`。
3. 自定义实时配置若不能匹配嵌入候选，会收到 `switch_candidate`，目标为当前市场最高分嵌入候选。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_action_suggestions_switch_unmatched_current_candidate` | 自定义/未匹配配置会建议切换到最高分嵌入候选 |
| `test_action_suggestions_degrade_when_paper_drawdown_worsens` | paper ledger 回撤恶化时会降级到低回撤候选 |
| `test_match_candidate_from_monitor_config_identifies_defaults` | 当前 A/US 默认实时配置能匹配对应嵌入候选 |
| `test_refresh_optimization_report_returns_action_suggestions` | 命令行实时报告刷新会返回动作建议 |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器刷新出的报告包含动作建议 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `44 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `789 passed, 1 skipped`。当前真实报告 `D:/chanlun_pro/reports/strategy_optimization_report.json` 已包含 `action_suggestions`：A 股 `keep_candidate -> a_full_market_balanced`，美股 `keep_candidate -> us_core9_default`。

## 33. 第二十三轮策略决策文件

2026-06-11 在 `action_suggestions` 之上新增独立的机器可读决策产物：`D:/chanlun_pro/reports/strategy_decision.json`。完整优化报告仍保留候选排名、runtime 排名和缺失源；决策文件只保留无人值守任务真正需要读取的部分：每个市场当前动作、目标候选、风险状态、原因和可执行 `monitor_config`。

决策文件字段：
1. `version`：决策文件 schema 版本，当前为 `1`。
2. `market`：报告范围，通常为 `all`。
3. `decisions[]`：每个市场一条决策。
4. `decisions[].action`：继承第 32 轮动作口径。
5. `decisions[].risk_state`：`ok`、`switch_ready`、`risk_reduce`、`review` 或 `unknown`。
6. `decisions[].ready_to_apply`：仅 `switch_candidate` 与 `degrade_candidate` 为 `true`，表示配置层可以按 `monitor_config` 执行切换或降级。
7. `decisions[].requires_review`：仅 `review_runtime_gap` 为 `true`，表示不应直接切换，应复查 paper ledger 与 runtime 窗口差异。
8. `decisions[].monitor_config`：目标候选对应的实时监控配置片段。

实现约束：
1. `build_decision_artifact(report)` 从完整报告提取紧凑决策文件，不重复评分逻辑。
2. `write_optimization_report(..., output_decision=...)` 可同时写 JSON 报告、Markdown 报告和决策文件。
3. `python -m chanlun.recursive_bt.strategy_optimizer` 默认额外写出 `D:/chanlun_pro/reports/strategy_decision.json`，并在 stdout 打印 `decision=...`。
4. `RECURSIVE_MONITOR_CONFIG["common"]["optimization_decision_json"]` 定义实时监控默认决策路径。
5. `live_monitor` CLI 支持 `--optimization-decision-json`；`refresh_optimization_report()` 返回 `decision_json` 与 `decisions`。
6. `DynamicRecursiveMonitor` 同步读取并传递 `optimization_decision_json`，web scheduler 每轮刷新报告时也会刷新决策文件。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_discover_and_write_optimization_report` | 优化器能同时写出 JSON/Markdown/decision 三个文件 |
| `test_build_decision_artifact_marks_switches_ready_to_apply` | `switch_candidate` 会在决策文件中标记为可执行切换 |
| `test_refresh_optimization_report_returns_action_suggestions` | 命令行实时刷新会写出决策文件并返回 `decisions` |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器每轮刷新会写出决策文件 |
| `test_dynamic_monitor_config_reads_project_config` | 配置中的 `optimization_decision_json` 能被动态监控器读取 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `45 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `790 passed, 1 skipped`。当前真实决策文件包含 2 条决策：A 股 `keep_candidate -> a_full_market_balanced`，美股 `keep_candidate -> us_core9_default`，两者 `risk_state=ok`、`ready_to_apply=false`。

## 34. 第二十四轮策略决策审计状态

2026-06-11 在 `strategy_decision.json` 之后新增 `D:/chanlun_pro/reports/strategy_decision_state.json`。决策文件表达“这一轮建议什么”，状态文件表达“这个建议已经连续出现多少次，是否达到自动应用门槛”。这样后续即便出现 `switch_candidate` 或 `degrade_candidate`，也不会因为单轮 paper ledger 噪声立即改变实时策略。

状态文件字段：
1. `confirmation_threshold`：默认 `3`，表示同一市场、同一动作、同一目标候选需要连续出现 3 次。
2. `market_states[].decision_key`：`market|action|target_candidate`，用于判断是否延续同一决策。
3. `market_states[].confirmations`：连续确认次数。
4. `market_states[].status`：`stable`、`confirming`、`apply_allowed`、`review_required` 或 `observing`。
5. `market_states[].apply_allowed`：只有 `ready_to_apply=true` 且确认次数达到门槛时才为 `true`。
6. `apply_allowed_count`：当前允许自动应用的市场数。
7. `review_required_count`：当前需要复查 runtime 差异的市场数。

实现约束：
1. `build_decision_state(decision_artifact, previous_state, confirmation_threshold=3)` 负责纯函数计算状态。
2. `update_decision_state_file(path, decision_artifact, confirmation_threshold=3)` 负责读取旧状态、更新确认次数并持久化。
3. `write_optimization_report(..., output_decision_state=...)` 默认可同时写完整报告、决策文件和决策状态文件。
4. `python -m chanlun.recursive_bt.strategy_optimizer` 默认写出 `strategy_decision_state.json` 并打印 `decision_state=...`。
5. `RECURSIVE_MONITOR_CONFIG["common"]` 新增 `optimization_decision_state_json` 与 `decision_confirmation_threshold`。
6. `live_monitor` CLI 支持 `--optimization-decision-state-json` 与 `--decision-confirmation-threshold`，状态行追加 `opt_apply`。
7. `DynamicRecursiveMonitor` 每轮刷新优化报告时同步维护决策状态。

当前真实状态：
1. A 股：`stable/confirmations=1 -> a_full_market_balanced`。
2. 美股：`stable/confirmations=1 -> us_core9_default`。
3. `apply_allowed_count=0`，`review_required_count=0`。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_decision_state_requires_repeated_ready_decisions` | `switch_candidate` 需要连续达到确认门槛才变成 `apply_allowed` |
| `test_discover_and_write_optimization_report` | 优化器可同时写出 decision state |
| `test_refresh_optimization_report_returns_action_suggestions` | 命令行实时刷新会写出并返回决策状态 |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器刷新会写出决策状态 |
| `test_dynamic_monitor_config_reads_project_config` | 状态文件路径和确认门槛能从配置读取 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `46 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `791 passed, 1 skipped`。当前真实状态文件 `D:/chanlun_pro/reports/strategy_decision_state.json` 显示 A/US 均为 `stable`，确认次数均为 1，尚无可应用切换。

## 35. 第二十五轮运行时策略覆盖文件

2026-06-11 在决策审计状态之后新增 `D:/chanlun_pro/reports/strategy_runtime_overrides.json`。它是自动切换链路的最后一层安全闸：只有 `strategy_decision_state.json` 中 `apply_allowed=true` 的市场，才会进入覆盖文件；普通 `stable`、`confirming`、`review_required` 都不会改变下一轮实时监控参数。

覆盖文件字段：
1. `version`：schema 版本，当前为 `1`。
2. `updated_at`：来源状态文件的更新时间。
3. `source`：当前为 `strategy_decision_state`。
4. `overrides[]`：允许应用的市场覆盖项。
5. `overrides[].monitor_config`：目标候选的策略参数，只允许覆盖策略相关白名单字段。
6. `override_count`：当前允许覆盖的市场数。

运行时覆盖白名单：
`max_pos`、`op_level`、`mid_level`、`big_level`、`mid_gate`、`regime_mode`、`nest_mode`、`trend_3boost`、`sell_scope`、`enable_selection_pool`、`selection_require_three_systems`、`selection_max_codes`。

实现约束：
1. `build_runtime_overrides(decision_state)` 只提取 `apply_allowed=true` 的市场。
2. `write_runtime_overrides_file(path, decision_state)` 写出覆盖文件。
3. `runtime_override_for_market(path, market)` 为实时监控读取单市场覆盖配置。
4. `write_optimization_report(..., output_runtime_overrides=...)` 默认同时写出覆盖文件。
5. `python -m chanlun.recursive_bt.strategy_optimizer` 默认写出 `strategy_runtime_overrides.json` 并打印 `runtime_overrides=...`。
6. `RECURSIVE_MONITOR_CONFIG["common"]` 新增 `optimization_runtime_overrides_json` 与 `runtime_overrides_enabled=true`。
7. `live_monitor` CLI 支持 `--optimization-runtime-overrides-json`、`--runtime-overrides-enabled`、`--no-runtime-overrides`。
8. `live_monitor` 与 `DynamicRecursiveMonitor` 在解析市场配置时会读取覆盖文件；若覆盖文件为空或禁用，则保持原配置。

当前真实覆盖文件：
1. `override_count=0`。
2. A/US 当前都没有运行时覆盖。
3. 因此 A 股继续使用 `a_full_market_balanced` 配置，美股继续使用 `us_core9_default` 配置。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_runtime_overrides_only_include_apply_allowed_states` | 只有 `apply_allowed=true` 的状态会进入覆盖文件 |
| `test_apply_runtime_overrides_only_uses_confirmed_monitor_config` | 实时监控只应用确认过的覆盖配置，并忽略非白名单字段 |
| `test_discover_and_write_optimization_report` | 优化器可同时写出 runtime overrides |
| `test_refresh_optimization_report_returns_action_suggestions` | 命令行实时刷新会写出 runtime overrides |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器刷新会写出 runtime overrides |
| `test_dynamic_monitor_config_reads_project_config` | 覆盖文件路径和开关可从配置读取 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `48 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `793 passed, 1 skipped`。当前真实覆盖文件 `D:/chanlun_pro/reports/strategy_runtime_overrides.json` 的 `override_count=0`，说明尚无达到确认门槛的切换/降级，实时系统继续按当前 A/US 默认候选运行。

## 36. 第二十六轮运行时覆盖审计与通知

2026-06-11 在运行时覆盖文件之后新增覆盖应用审计日志与通知摘要。目标是让每一次真正被应用的策略覆盖都有可追溯记录，并在实时监控启动或 web scheduler 首轮运行时发出通知；同时仍保持第 35 轮安全闸：只有覆盖文件中存在 `apply_allowed=true` 的市场才会触发审计和通知。

审计日志：
1. 默认路径：`D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl`。
2. 格式：一行一个 JSON 事件。
3. 事件名：`runtime_override_applied`。
4. 去重键：`market|decision_key`。
5. 记录字段：`time`、`market`、`action`、`risk_state`、`target_candidate`、`decision_key`、`confirmations`、`confirmation_threshold`、`reason`、`override_path`、`applied_config`。
6. 如果同一个 `market|decision_key` 已经记录过，不重复写日志，也不重复发送通知。

通知摘要：
1. `runtime_override_notice_lines(event)` 生成“策略覆盖已应用”通知内容。
2. `send_runtime_override_notice(notifier, title, event)` 发送通知；无事件时返回 `False`。
3. CLI 实时监控在 notifier 初始化后发送一次新覆盖通知。
4. `DynamicRecursiveMonitor` 在第一次 `run_once()` 时发送一次新覆盖通知，并通过 `runtime_override_notice_sent` 返回是否发送。

实现约束：
1. `_apply_runtime_overrides()` 读取覆盖文件后，只应用策略白名单字段。
2. 覆盖应用后调用 `record_runtime_override_application()` 写入审计日志。
3. 审计去重由 `_audit_log_has_event()` 保证，避免每轮扫描重复通知。
4. `RECURSIVE_MONITOR_CONFIG["common"]["runtime_override_audit_jsonl"]` 定义审计日志路径。
5. 当前真实覆盖文件 `override_count=0`，因此真实审计日志尚未生成，这是正确状态。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_apply_runtime_overrides_only_uses_confirmed_monitor_config` | 覆盖应用会写审计日志，重复应用同一 decision_key 不重复记录 |
| `test_runtime_override_notice_formats_and_sends` | 覆盖通知内容可生成并发送 |
| `test_dynamic_monitor_sends_runtime_override_notice_once` | web 动态监控器只在首次运行发送一次覆盖通知 |
| `test_default_recursive_monitor_config_tracks_latest_live_candidates` | 默认配置含覆盖审计日志路径 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py tests/test_strategy_optimizer.py` 通过，结果为 `50 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `795 passed, 1 skipped`。当前真实覆盖文件为空，因此 `D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl` 尚未生成；这表示没有任何策略覆盖被实际应用或通知。

## 37. 第二十七轮策略阶段归因报告

2026-06-11 在覆盖审计之后新增策略阶段归因报告：`D:/chanlun_pro/reports/strategy_attribution_report.json` 与 `D:/chanlun_pro/reports/strategy_attribution_report.md`。它把 paper ledger 的 `equity_curve` 与 `strategy_runtime_override_audit.jsonl` 中的覆盖事件关联起来，按“默认阶段/策略覆盖阶段”切分收益、回撤和交易增量，用于回答“某次策略切换前后到底改善了收益还是恶化了回撤”。

归因报告字段：
1. `markets[].segments[]`：单市场策略阶段。
2. `segments[].target_candidate`：该阶段使用的候选策略。
3. `segments[].action`：`baseline`、`switch_candidate`、`degrade_candidate` 等。
4. `segments[].start_time/end_time`：该阶段覆盖的权益曲线时间范围。
5. `segments[].total_return/max_drawdown`：阶段内收益和最大回撤。
6. `segments[].trade_count_delta`：阶段内交易增量。
7. `missing_sources[]`：明确记录缺失或空账本，不静默失败。

实现约束：
1. `load_runtime_override_audit_events(path, market=...)` 读取并过滤覆盖审计事件。
2. `build_strategy_attribution_report()` 读取 A/US paper ledger 与覆盖审计，生成归因结构。
3. `write_strategy_attribution_report()` 写出 JSON/Markdown。
4. `render_strategy_attribution_markdown()` 输出人可读阶段表。
5. `python -m chanlun.recursive_bt.strategy_optimizer` 默认写出归因 JSON/Markdown。
6. `live_monitor.refresh_optimization_report()` 每轮刷新优化报告后同步刷新归因报告。
7. `DynamicRecursiveMonitor` 同步传递归因报告路径，web scheduler 每轮也会刷新归因报告。

当前真实归因状态：
1. A 股 ledger 存在但没有 `equity_curve`，归因报告标记 `empty_equity_curve`。
2. 美股 ledger 缺失，归因报告标记 `missing`。
3. 当前 `segments=0`，`missing_sources=2`。
4. 这说明下一步仿实盘循环需要先积累有效 paper equity snapshots，之后归因报告才能评估策略阶段优劣。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_strategy_attribution_segments_equity_by_override_events` | 权益曲线能按覆盖事件切成 baseline/切换后两个阶段 |
| `test_strategy_attribution_reports_missing_or_empty_ledgers` | 缺失或空 ledger 会进入 `missing_sources` |
| `test_refresh_optimization_report_returns_action_suggestions` | CLI 实时刷新会写出归因报告并返回缺失统计 |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器刷新会写出归因报告 |
| `test_dynamic_monitor_config_reads_project_config` | 归因报告路径可从配置读取 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `52 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `797 passed, 1 skipped`。当前真实归因报告已生成：`markets=2`、`segments=0`、`missing_sources=2`，缺失原因为 A 股 `empty_equity_curve`、美股 `missing`。

## 38. 第二十八轮 paper ledger 基线权益快照

2026-06-11 在策略阶段归因报告之后，补上 paper ledger 的基线权益快照初始化。这个改动解决的不是收益问题，而是归因口径问题：当 A/US 账本刚启动、还没有任何权益曲线时，系统以前只能把账本标记为 `missing` 或 `empty_equity_curve`，导致归因报告无法进入持续观察状态。现在归因刷新会先为缺失或空权益曲线的账本建立一个零交易、零收益的权益锚点。

基线快照约束：
1. 不伪造买卖点，不伪造成交，不改变现金、持仓、挂单或交易记录。
2. 只在 `equity_curve` 为空时创建一次，已有权益曲线时不重复追加。
3. 快照记录 `time/equity/cash/positions/pending/trades`，并标记 `baseline=true`、`reason=ledger_baseline`。
4. 若账本已有持仓但没有行情价格，基线权益按持仓 `entry_px` 估值，后续真实扫描会用最新价格覆盖新增快照。
5. 归因报告会把该快照视为 `baseline` 阶段，收益和回撤均为 0，直到后续 paper/live 扫描积累新的权益点。
6. 只有 `ledger_baseline` 的账本不参与优化器 runtime 排名，避免把“尚未运行出真实权益点”误判为“策略表现落后”。
7. 只有现金快照、无持仓且无成交的账本同样不参与 runtime 排名，避免空仓心跳快照被误当成策略业绩。

实现约束：
1. `PaperBroker.ensure_baseline_snapshot(now)` 负责账本内的一次性权益锚点。
2. `ensure_paper_ledger_baseline(path, market)` 负责从优化器侧初始化缺失或空账本，并保存 ledger。
3. `build_strategy_attribution_report(..., ensure_baseline_ledgers=True)` 与 `write_strategy_attribution_report(..., ensure_baseline_ledgers=True)` 支持在归因前先修复账本基线。
4. `python -m chanlun.recursive_bt.strategy_optimizer` 默认会初始化 A/US 归因账本；如需纯只读检查，可加 `--no-attribution-baseline`。
5. `live_monitor.refresh_optimization_report()` 在实时闭环刷新归因报告时默认启用基线修复，测试可通过 `attribution_ledger_paths` 传入临时账本以隔离真实仿真账本。
6. `summary_from_paper_ledger()` 会过滤 baseline-only 与 no-activity 账本，`score_runtime_sources()` 分别将其标记为 `baseline_only` 或 `no_activity` 缺失源，等待真实持仓或成交后再作为 runtime evidence 参与策略优化。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_paper_broker_ensures_one_baseline_snapshot` | PaperBroker 只创建一次基线权益快照，保存重载后不丢失 |
| `test_strategy_attribution_can_initialize_baseline_ledger` | 归因层可把缺失 ledger 初始化为 baseline segment，并清空缺失源 |
| `test_refresh_optimization_report_returns_action_suggestions` | 实时刷新可对临时 A/US 账本创建两个归因基线段 |
| `test_baseline_only_paper_ledger_is_not_runtime_evidence` | 只有基线锚点的 paper ledger 不参与 runtime 打分，避免误触发策略 review |
| `test_no_activity_paper_ledger_is_not_runtime_evidence` | 空仓、无成交的现金快照不参与 runtime 打分，避免空仓心跳误导优化 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `86 passed`；针对失败回归点的复测 `PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions tests/test_recursive_app_monitor.py::test_dynamic_monitor_refreshes_optimization_report` 通过，结果为 `22 passed`。

真实生成结果：`D:/chanlun_pro/reports/strategy_attribution_report.json` 已从 `segments=0/missing_sources=2` 变为 `markets=2`、`segments=2`、`missing_sources=0`；A/US 各 1 个 baseline segment，收益和回撤均为 0，交易数均为 0。A 股账本 `D:/chanlun_pro/paper/ledger.json` 与美股账本 `D:/chanlun_pro/paper_us/ledger.json` 都只新增了 `ledger_baseline` 权益锚点，未伪造成交。这一步仍不代表策略盈利，只代表 paper 归因闭环具备从零开始记录阶段表现的锚点。

离线缓存单轮验证：使用 `live_monitor --data-source chart_cache --once --force --dry-run --paper-enabled --optimization-report-enabled` 分别扫描 US `QQQ.US,TSLA.US` 与 A 股 `SH.513100,SZ.002920,SZ.301004`。两边当前均无新买卖点、无挂单、无成交；A/US ledger 均从 1 个 baseline 快照增加到 2 个快照，归因报告 `segments=2/missing_sources=0/snapshots=2+2`，优化报告把两个 paper ledger 标为 `no_activity`，决策仍为 `keep_candidate/ok`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `801 passed, 1 skipped`。真实优化报告中 A/US 决策仍为 `keep_candidate/ok`，baseline-only 或 no-activity paper ledger 仅作为缺失源等待真实持仓/成交，不参与策略切换或降级判断。

## 39. 第二十九轮 chart cache 小池 live-parity 回放

2026-06-11 在账本基线与 no-activity 过滤稳定后，使用本地 `chart_cache` 对当前可用小池重新跑一轮 live-parity 回测，用于验证实时 1m/5m/30m 联立逻辑在无实时行情时仍能持续产生可评分证据。

执行命令：
1. US：`python -m chanlun.recursive_bt.live_backtest --market us --source chart_cache --codes QQQ.US,TSLA.US --op-level 1m --mid-level 5m --big-level 30m --mid-gate soft --max-pos 9 --require tech,nest`
2. A 股：`python -m chanlun.recursive_bt.live_backtest --market a --source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --op-level 1m --mid-level 5m --big-level 30m --mid-gate soft --max-pos 30 --require tech,nest`

回放结果：

| 市场 | 标的 | 区间 | 收益 | 基准 | 超额 | 最大回撤 | Sharpe | 胜率 | 交易 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| US | QQQ/TSLA | 2026-03-03~2026-06-01 | +4.2% | +15.1% | -10.9% | 0.4% | 7.11 | 81% | 68 |
| A 股 | SH.513100/SZ.002920/SZ.301004 | 2026-02-25~2026-06-04 | +0.3% | -7.3% | +7.6% | 0.4% | 1.72 | 52% | 33 |

牛熊段含义：
1. US 小池在 bull 段收益 +3.0%，但明显跑输基准；bear 段收益 +0.07%，基准 -1.9%，防守有效。
2. A 股小池在 range 段小幅正收益，bear 段仅 -0.03%，基准 -9.0%，说明 30m 大级别约束与 5m 中级别 soft gate 对回撤控制仍有价值。
3. 两个小池回放都没有推翻当前默认候选：A 股默认仍是全市场三系统 `a_full_market_balanced`，US 默认仍是 core9 `us_core9_default`。小池结果只是最新 runtime 证据，不能替代全市场/核心池的长期嵌入证据。

刷新后的真实优化状态：
1. `D:/chanlun_pro/reports/a_live_parity_backtest_summary.json` 与 `D:/chanlun_pro/reports/us_live_parity_backtest_summary.json` 已更新。
2. `strategy_optimization_report.json` 已纳入这两份 summary，runtime summary 数仍为 112。
3. A/US 决策保持 `keep_candidate/ok`，paper ledger 缺失源保持 `no_activity`，等待真实持仓或成交后再参与 runtime evidence。

## 40. 第三十轮 runtime observation 只读诊断层

2026-06-11 在小池 live-parity 回放后，新增 `runtime_observations` 诊断层。它只做观察，不改变 `action_suggestions`、`strategy_decision.json` 或 runtime override。这样当最新 live-parity 回测明显落后嵌入候选证据、或者相对基准出现显著负超额时，报告可以提示需要继续观察，但不会因为小样本或短窗口直接切换实盘策略。

诊断规则：
1. 只检查默认 live-parity 源：`a_live_parity_backtest` 与 `us_live_parity_backtest`。
2. 交易数低于 20 的样本不提示，避免过小样本误报。
3. 当 runtime 收益低于嵌入候选收益 10 个百分点以上，输出 `live_parity_runtime_lag/watch`。
4. 当 runtime summary 的 `excess <= -5%`，同样输出 watch。
5. watch 只进入 `strategy_optimization_report.json/md`，不会进入自动切换门槛。

当前真实观察项：
1. A 股：`a_live_parity_backtest` 小池收益 +0.3%，显著低于全市场嵌入候选 +128.1%，因此 watch；但小池仍有 +7.6% 超额，不能据此否定全市场默认策略。
2. US：`us_live_parity_backtest` 小池收益 +4.2%，低于 core9 嵌入候选 +19.6%，且超额 -10.9%，因此 watch；后续应重点比较 core9 全池与 QQQ/TSLA 小池的市场覆盖差异。
3. A/US 最终决策仍为 `keep_candidate/ok`，因为 watch 不是切换信号。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_runtime_observations_flag_live_parity_lag_without_switching` | live-parity 跑输会生成 watch 观察项，但 action 仍保持 keep |
| `test_strategy_optimizer.py` 全组 | `runtime_observations` 不破坏优化报告、决策文件、归因报告生成 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `87 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `802 passed, 1 skipped`。真实优化报告当前有 2 条 `runtime_observations`，分别对应 A/US 最新 live-parity 回测 watch；A/US 决策仍为 `keep_candidate/ok`。

## 41. 第三十一轮一二三类买点绩效归因

2026-06-11 新增 `strategy_bs_point_attribution_report.json/md`，用于持续回答“当前一二三类买卖点是否有问题、买入比例是否应该调整”。该报告读取最新 live-parity trades CSV，按入场 `bs_type` 的 1/2/3 类分组，统计每类买点的交易数、胜率、平均收益、中位数收益、复合收益、序列最大回撤、最好/最差单笔、平均持仓小时和退出原因。

实现约束：
1. `build_bs_point_attribution_report()` 读取 A/US 默认 trades 文件，缺失时进入 `missing_sources`，不静默失败。
2. `write_bs_point_attribution_report()` 写出 JSON/Markdown。
3. `python -m chanlun.recursive_bt.strategy_optimizer` 默认同步生成买点归因报告。
4. `live_monitor.refresh_optimization_report()`、CLI 与 `DynamicRecursiveMonitor` 每轮优化刷新时同步刷新买点归因报告。
5. 报告只给 `ratio_guidance`，暂不自动改实时买入比例；后续若要自动应用，需要像策略切换一样经过确认门槛，避免短样本噪声直接改仓位。

比例建议规则：
1. 样本数低于 10 或类别未知：`watch / 1.00`。
2. 平均收益为负且胜率低于 45%：`reduce / 0.75`。
3. 平均收益大于 0.5%、胜率不低于 58%、类别序列回撤不超过 10%：`allow_boost / 1.10`。
4. 其他情况：`keep / 1.00`。

当前真实结果：

| 市场 | 买点类 | 交易 | 胜率 | 均值收益 | 复合收益 | 序列回撤 | 平均持仓 | 建议 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 3 | 32 | 53.1% | +0.32% | +9.71% | 12.17% | 50.4h | keep 1.00 |
| A 股 | unknown | 1 | 0.0% | -0.09% | -0.09% | 0.09% | 148.7h | watch 1.00 |
| US | 3 | 68 | 80.9% | +0.61% | +50.55% | 2.21% | 15.5h | allow_boost 1.10 |

策略含义：
1. 当前最新 A/US 小池交易主要来自 3 类买点，说明“3 买优先”在最近缓存样本中确实是主要成交来源；但 A 股 3 买序列回撤达到 12.17%，未通过加权门槛，继续保持默认比例。
2. US 3 买在 QQQ/TSLA 小池中胜率和均值收益都较强，且回撤受控，因此报告允许后续进入“加权观察”；这不是自动加仓命令，只是比例调优候选。
3. A 股 `unknown` 来自收尾强平类记录，样本薄且不是明确一二三类买点，不参与比例调优。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_bs_point_attribution_summarizes_trade_classes_and_guidance` | 能按买点类别统计绩效，并给出 reduce/allow_boost/watch 比例建议 |
| `test_refresh_optimization_report_returns_action_suggestions` | 实时刷新会写出买点归因报告，并在 trades 缺失时返回缺失数 |
| `test_dynamic_monitor_refreshes_optimization_report` | web 动态监控器刷新会写出买点归因 JSON/Markdown |
| `test_dynamic_monitor_config_reads_project_config` | 买点归因报告路径可从配置读取 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `88 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `803 passed, 1 skipped`。真实报告已生成：`strategy_bs_point_attribution_report.json/md`，当前 A 股 3 类买点 `keep 1.00`，US 3 类买点 `allow_boost 1.10`，但尚未自动应用比例调整。

## 42. 第三十二轮买点比例建议确认与运行时覆盖

2026-06-11 在一二三类买点绩效归因之后，新增买点比例建议的确认状态和运行时覆盖文件。目标是让 `allow_boost` / `reduce` 不再停留在报告里，但也不能一出现就影响实盘比例：必须像策略切换一样，连续确认达到门槛后才写入覆盖文件，由实时监控在下一轮读取并应用。

新增文件：
1. `strategy_bs_point_ratio_state.json`：记录每个 `market|bs_class` 的比例建议、确认次数、状态和样本统计。
2. `strategy_bs_point_ratio_overrides.json`：只包含已达到确认门槛的倍率，例如 `{"3": 1.1}`。

确认规则：
1. 状态键：`market|bs_class`。
2. 决策键：`market|bs_class|action|ratio_multiplier`。
3. 默认确认门槛：`3`。
4. 只有 `allow_boost` / `reduce` 且倍率不等于 1.0 时，才属于 `ready_to_apply`。
5. `keep` / `watch` 永远不进入覆盖文件。

实时应用规则：
1. `live_monitor._apply_runtime_overrides()` 会独立读取 `strategy_bs_point_ratio_overrides.json`，即使没有策略覆盖也能读取买点倍率。
2. `collect_monitor_events()` 在基础 `recommended_buy_ratio()`、中级别 soft 折扣、区间套折扣之后，再应用已确认倍率。
3. `DynamicRecursiveMonitor._selection_events()` 对 A 股三系统 selector 信号也应用同一倍率。
4. 通知 reason 中会追加 `bs3_ratio_x1.10` 之类的痕迹，方便审计比例为何变化。

当前真实状态：
1. A 股 3 类买点：`keep`，确认 1 次，不进入覆盖。
2. A 股 unknown：`watch`，确认 1 次，不进入覆盖。
3. US 3 类买点：`allow_boost`，确认 1/3，状态 `confirming`，尚未进入覆盖。
4. `strategy_bs_point_ratio_overrides.json` 当前 `override_count=0`，因此真实实时系统尚未自动改变买入比例。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_bs_point_ratio_state_requires_confirmation_before_override` | 买点比例建议需要连续确认达到门槛后才生成覆盖 |
| `test_collect_monitor_events_applies_confirmed_bs_point_ratio_multiplier` | 普通实时买点会应用已确认倍率并写入 reason |
| `test_a_monitor_applies_bs_point_ratio_multiplier_to_selector_event` | A 股三系统 selector 买点也会应用已确认倍率 |
| `test_apply_runtime_overrides_only_uses_confirmed_monitor_config` | 没有策略覆盖时也能独立读取买点倍率覆盖 |
| `test_refresh_optimization_report_returns_action_suggestions` | 实时刷新会写出比例状态与覆盖文件 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `60 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `806 passed, 1 skipped`。真实状态文件显示 US 3 类买点 `allow_boost` 仍为 `confirming=1/3`，`strategy_bs_point_ratio_overrides.json` 的 `override_count=0`，所以实盘比例尚未自动提高。

## 43. 第三十三轮卖点类别归因与卖出比例统一

2026-06-11 在买点比例确认之后，补齐卖点侧的可审计闭环。此前通知层虽然会显示 `sell_ratio`，但组合回测和 paper ledger 只记录退出原因，没有稳定记录退出时对应的是 1卖、2卖、3卖还是大级别转空，导致无法持续回答“当前一二三类卖点是否有问题”。本轮新增统一的卖点比例入口和退出类别字段，默认仍保持保守全退，不在样本不足时做部分卖出。

实现约束：
1. `recommended_sell_ratio(bs_type, big_dir)` 成为卖点比例统一入口；当前规则为 1卖、2卖、3卖以及 30m 大级别转空均卖出 100%。
2. `collect_monitor_events()` 的小级别卖点和大级别退出都通过 `recommended_sell_ratio()` 生成通知里的建议卖出比例。
3. `PTrade` 新增 `exit_bs_type` 与 `sell_ratio`，组合回测在小级别卖点退出时记录真实 1/2/3 卖类型，在大级别转空时记录 `big_level_down`，收尾强平记录 `final_close`。
4. `PaperBroker.queue_events()` 和 `fill_pending()` 会把实时卖点事件的 `bs_type` 写入成交记录，paper ledger 中同时保留入场 `bs_type` 与退出 `exit_bs_type`。
5. `strategy_bs_point_attribution_report.json/md` 升级到 `version=2`，保留原有买点 `groups/ratio_guidance`，并新增卖点 `sell_groups/sell_ratio_guidance`。
6. 卖点归因目前只给 `keep_full_exit / 1.00`、`close_only / 1.00`、`watch / 1.00`，不自动生成部分卖出覆盖；后续若要降低某类卖点卖出比例，需要先补“卖出后未来收益/回撤”的反事实比较，而不是仅看该笔交易收益。

本轮真实归因结果：

| 市场 | 退出类别 | 交易 | 胜率 | 均值收益 | 序列回撤 | 卖出建议 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| A 股 | 1卖 | 8 | 100.0% | +3.03% | 0.00% | keep_full_exit 1.00 |
| A 股 | 2卖 | 4 | 75.0% | -0.18% | 4.07% | keep_full_exit 1.00 |
| A 股 | 3卖 | 19 | 31.6% | -0.64% | 11.97% | keep_full_exit 1.00 |
| A 股 | big_down | 1 | 0.0% | -1.11% | 1.11% | keep_full_exit 1.00 |
| A 股 | final | 1 | 0.0% | -0.09% | 0.09% | close_only 1.00 |
| US | 1卖 | 26 | 100.0% | +1.05% | 0.00% | keep_full_exit 1.00 |
| US | 2卖 | 6 | 100.0% | +0.26% | 0.00% | keep_full_exit 1.00 |
| US | 3卖 | 34 | 64.7% | +0.37% | 2.56% | keep_full_exit 1.00 |
| US | big_down | 2 | 50.0% | -0.09% | 1.30% | keep_full_exit 1.00 |

策略含义：
1. A 股小池最新样本中，3卖退出最多但表现最弱，说明 A 股当前 1m/5m/30m 联立系统的 3卖可能偏滞后或受弱市震荡影响，需要后续重点比较“3卖全退”与“3卖减仓后等买点回补”的机会成本。
2. US 样本中 1卖、2卖、3卖退出均为正均值，且回撤较低，当前没有证据支持降低卖出比例。
3. 仅凭已完成交易的收益不能证明“某卖点应该少卖”，因为缺少卖出后继续持有的反事实路径；因此系统先记录退出类别，卖出比例保持全退，后续优化应新增 exit-after-return 统计再考虑部分卖出。
4. 买点比例状态同步刷新后，A 股 3买为 `keep`，US 3买为 `allow_boost` 且确认数 `2/3`，`strategy_bs_point_ratio_overrides.json` 仍为 `override_count=0`，所以本轮没有自动改变实盘买入比例。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_recommended_buy_ratio_caps_at_one_slot` | 同时覆盖 `recommended_sell_ratio()` 的默认全退规则 |
| `test_pick_buy_class_respects_priority` | 买点优先级不变，并验证卖点归因优先选择 1卖/2卖/3卖中的更强退出类别 |
| `test_paper_broker_partial_sell_uses_sell_ratio` | paper 部分卖出成交会记录入场 `bs_type`、退出 `exit_bs_type` 和 `sell_ratio` |
| `test_portfolio_backtest_records_exit_sell_point_class` | 组合回测能记录 3买入场、2卖退出的完整交易归因 |
| `test_bs_point_attribution_summarizes_trade_classes_and_guidance` | 买卖点归因报告 v2 同时输出买点和卖点分组及比例建议 |
| `test_dynamic_monitor_refreshes_optimization_report` | 动态监控刷新时接受买卖点归因报告 v2 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_recursive_live_monitor.py tests/test_strategy_optimizer.py` 通过，结果为 `79 passed`。
最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `807 passed, 1 skipped`。真实报告已刷新：`D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json/md` 为 `version=2`，A/US 决策仍为 `keep_candidate/ok`，卖出比例通知继续为 100% 全退。

## 44. 第三十四轮卖点反事实收益与 US 3买比例覆盖生效

2026-06-11 在卖点类别归因之后，新增“卖出后继续持有”的反事实统计。仅看一笔交易在卖出时的收益，无法判断该卖点是否卖早；必须观察卖出后若继续持有 5/20/60 根主时钟 bar 的收益变化，以及 20 根内最大有利/最大不利波动。该统计进入 trades CSV 与买卖点归因报告，但仍不直接改变卖出比例。

新增字段：
1. `post_exit_bars`：卖出后可观察的主时钟 bar 数。
2. `post_exit_ret_5`、`post_exit_ret_20`、`post_exit_ret_60`：卖出后继续持有到对应 bar 的收益。
3. `post_exit_mfe_20`：卖出后 20 根内最大继续持有收益，用来识别是否卖早。
4. `post_exit_mae_20`：卖出后 20 根内最大不利继续持有收益，用来识别卖点是否有效保护回撤。

卖点报告规则：
1. `sell_groups` 现在包含 `avg_post_exit_ret_5/20/60`、`avg_post_exit_mfe_20`、`avg_post_exit_mae_20`。
2. Markdown 的 Sell Points 表新增 `Post 20`、`MFE 20`、`MAE 20`。
3. 若某 1/2/3 卖点样本数足够，且 `avg_post_exit_ret_20 > 0.5%`、`avg_post_exit_mfe_20 > 1.0%`，报告只标记 `review_scale_out`，提示需要做部分卖出回测；不自动降低实时卖出比例。
4. 当前真实样本未触发 `review_scale_out`，所以卖点通知继续保持 100% 全退。

本轮真实反事实结果：

| 市场 | 退出类别 | 交易 | 交易均值 | Post 20 | MFE 20 | MAE 20 | 建议 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 1卖 | 8 | +3.03% | -0.35% | +0.16% | -0.55% | keep_full_exit |
| A 股 | 2卖 | 4 | -0.18% | +0.34% | +0.72% | -0.26% | keep_full_exit |
| A 股 | 3卖 | 19 | -0.64% | -0.46% | +0.00% | -0.65% | keep_full_exit |
| A 股 | big_down | 1 | -1.11% | +0.74% | +0.93% | -0.58% | keep_full_exit |
| US | 1卖 | 26 | +1.05% | +0.10% | +0.17% | -0.19% | keep_full_exit |
| US | 2卖 | 6 | +0.26% | -0.39% | -0.05% | -0.49% | keep_full_exit |
| US | 3卖 | 34 | +0.37% | -0.12% | +0.06% | -0.34% | keep_full_exit |
| US | big_down | 2 | -0.09% | +0.22% | +0.34% | -0.28% | keep_full_exit |

策略含义：
1. A 股 3卖虽然交易均值较弱，但卖后 20 根平均继续下跌 -0.46%，说明在当前小池里 3卖全退并不是明显卖早；更像是退出已较晚，下一轮应研究是否要提前到 1卖/2卖或加强 5m 中枢破坏过滤。
2. US 3卖卖后 20 根平均 -0.12%，也不支持部分卖出。
3. A 股 2卖和 big_down 卖后有小幅正漂移，但样本只有 4 和 1，低于确认阈值，不参与卖出比例调整。
4. 本轮连续确认后，US 3买 `allow_boost` 达到 `3/3`，`strategy_bs_point_ratio_overrides.json` 变为 `override_count=1`，内容为 `{"market":"us","bs_point_ratio_multipliers":{"3":1.1}}`。下一轮 US 实时通知会在基础买入比例、5m soft 折扣和区间嵌套折扣之后，对 3买再乘 1.10，并在 reason 中标记 `bs3_ratio_x1.10`。
5. A 股 3买仍为 `keep`，不生成比例覆盖；A/US 策略候选仍为 `keep_candidate/ok`，没有发生策略切换。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_portfolio_backtest_records_exit_sell_point_class` | 组合回测成交记录包含卖后反事实字段 |
| `test_bs_point_attribution_summarizes_trade_classes_and_guidance` | 卖点归因能汇总 Post 20/MFE/MAE，并在卖后明显继续上涨时只给 `review_scale_out` |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_recursive_live_monitor.py tests/test_strategy_optimizer.py tests/test_recursive_app_monitor.py` 通过，结果为 `92 passed`。

最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `807 passed, 1 skipped`。真实报告已刷新，`strategy_bs_point_attribution_report.md` 的 Sell Points 表包含 `Post 20`、`MFE 20`、`MAE 20`，且当前没有任何卖点类别触发 `review_scale_out`。

实时 dry-run 验证：使用 `live_monitor --data-source chart_cache --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup` 分别扫描 US `QQQ.US,TSLA.US` 与 A 股 `SH.513100,SZ.002920,SZ.301004`。两边当前均无新买卖点、无挂单、无成交，paper 权益均为 1,000,000；A/US paper ledger 各 3 个快照。比例覆盖文件保持 `override_count=1`，仅 US 3买 `bs_point_ratio_multipliers={"3":1.1}` 生效。

## 45. 第三十五轮确认买点倍率进入 live-parity 回测

2026-06-11 在 US 3买 `allow_boost` 达到确认门槛后，将 `strategy_bs_point_ratio_overrides.json` 接入 `live_backtest` 与 `portfolio_backtest`。此前实时通知已经会读取确认倍率，但离线 live-parity 回测仍只使用 `recommended_buy_ratio()` 的基础比例，导致“实盘执行口径”和“最新回测评估口径”不完全一致。本轮补齐这条链路。

实现约束：
1. `portfolio_backtest(..., bs_point_ratio_multipliers=...)` 支持按买点类别乘以确认倍率，倍率限制在 `0.0~2.0`。
2. `PTrade` 新增 `buy_ratio`，记录实际成交时使用的目标仓位比例，便于后续排查某笔交易为何加权。
3. `live_backtest` 默认读取 `D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json`，并通过 `--no-bs-point-ratio-overrides` 保留无覆盖基准回测。
4. 回测 summary 新增 `bs_point_ratio_overrides_enabled`、`bs_point_ratio_overrides_json`、`bs_point_ratio_multipliers`，明确本次回测是否采用确认倍率。
5. 当前只有 US 3买确认倍率 `{"3": 1.1}`，A 股没有倍率覆盖，因此 A 股回测结果保持原样。

US 小池同窗对照：

| 口径 | 3买倍率 | 收益 | 基准 | 超额 | 最大回撤 | 胜率 | 交易 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 无覆盖基准 | 1.00 | +4.17% | +15.08% | -10.91% | 0.43% | 80.9% | 68 |
| 实盘一致覆盖 | 1.10 | +4.60% | +15.08% | -10.49% | 0.47% | 80.9% | 68 |

策略含义：
1. 已确认的 US 3买加权在 QQQ/TSLA 小池中提高约 `+0.43pp` 收益，回撤增加约 `+0.04pp`，收益改善大于回撤代价。
2. 交易数和胜率不变，说明本轮改变的是仓位大小，不是信号选择。
3. 覆盖后仍明显跑输该小池等权基准，所以 `runtime_observations` 继续保持 US `live_parity_runtime_lag/watch`，但动作仍为 `keep_candidate/ok`，不触发策略切换。
4. A 股无确认倍率覆盖，继续使用全市场三系统默认策略。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 能从覆盖文件读取 US 3买倍率并传给组合回测 |
| `test_portfolio_backtest_applies_bs_point_ratio_multiplier_to_buy_weight` | 组合回测实际成交记录中的 `buy_ratio` 会反映 3买倍率 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `94 passed`。JSON 解析验证通过：`strategy_optimization_report.json`、US 默认 summary、US 无覆盖 summary 均可被 Python `json.loads()` 正常读取。
最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `809 passed, 1 skipped`。

## 46. 第三十六轮买点倍率覆盖影响报告

2026-06-11 在确认倍率进入 live-parity 回测后，新增 `strategy_bs_point_ratio_impact_report.json/md`，用于持续审计“已确认买点倍率是否真的改善收益/回撤”。这一步把手工对比 US 覆盖前后 summary 的过程固化为优化器产物，避免后续只知道覆盖文件生效，却不知道覆盖是否仍值得保留。

实现约束：
1. `build_bs_point_ratio_impact_report()` 读取每个市场的默认 live-parity summary，并在存在 `bs_point_ratio_multipliers` 时读取对应的无覆盖 baseline summary。
2. baseline 文件默认命名为 `*_no_bs_override_summary.json`，例如 US 的 `us_live_parity_backtest_no_bs_override_summary.json`。
3. 无活跃倍率的市场不要求 baseline，`baseline_required=false`，基准值等于当前值，delta 为 0，动作记为 `no_active_override`。
4. 有活跃倍率且收益改善、回撤增加不超过 0.5pp 时，动作记为 `keep_override`。
5. 若覆盖降低收益且没有降低回撤，动作将记为 `review_disable`，后续可进入确认门槛再移除倍率。
6. `python -m chanlun.recursive_bt.strategy_optimizer` 默认写出 JSON 和 Markdown，并在终端输出 `bs_point_ratio_impact=...`。

当前真实影响报告：

| 市场 | 倍率 | 基准收益 | 当前收益 | 收益差 | 基准回撤 | 当前回撤 | 回撤差 | 动作 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | `{}` | +0.26% | +0.26% | +0.00% | 0.39% | 0.39% | 0.00% | no_active_override |
| US | `{"3":1.1}` | +4.17% | +4.60% | +0.43% | 0.43% | 0.47% | +0.04% | keep_override |

策略含义：
1. US 3买 1.10 倍率在当前 QQQ/TSLA 小池窗口内是正贡献，收益增加约 `+0.43pp`，回撤仅增加约 `+0.04pp`，因此覆盖保持。
2. A 股当前没有确认倍率覆盖，不需要 baseline 对照，后续若 A 股某类买点进入 `apply_allowed`，同一报告会自动开始比较覆盖前后差异。
3. 该影响报告只评估“已确认倍率是否值得继续保留”，不替代买点归因报告；倍率的产生仍来自 `strategy_bs_point_attribution_report` 和 `strategy_bs_point_ratio_state` 的确认门槛。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_bs_point_ratio_impact_report_compares_override_to_baseline` | 有确认倍率时能比较当前 summary 与无覆盖 baseline，并给出 `keep_override` |
| `test_bs_point_ratio_impact_report_handles_no_active_override` | 无确认倍率时不要求 baseline，delta 归零并给出 `no_active_override` |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py tests/test_backtest_live_parity.py tests/test_recursive_live_monitor.py tests/test_recursive_app_monitor.py` 通过，结果为 `96 passed`。
最终验证：`PYTHONPATH=src python -m pytest` 全量通过，结果为 `811 passed, 1 skipped`。

## 47. 第三十七轮小级别卖点退出策略候选对照

2026-06-11 在买点倍率影响报告之后，新增 `--sell-classes` 回测参数与 `strategy_sell_policy_impact_report.json/md`，用于持续回答“1/2/3 类卖点是否都应该作为小级别全退出信号”。默认实盘与默认 live-parity 回测仍保持 `1,2,3` 全卖点退出；新候选只用于对照研究，不直接改变实时系统。

实现约束：1. `portfolio_backtest(..., sell_classes=...)` 只过滤小级别卖点；30m 大级别转空的硬退出仍然始终生效。2. `live_backtest --sell-classes 1,2` 可以生成忽略小级别 3 卖的候选 summary/trades，默认仍是 `1,2,3`。3. summary 新增 `sell_classes` 字段，优化器可识别本次回测采用的退出类别。4. `strategy_sell_policy_impact_report` 比较默认全卖点退出与 `sell12` 候选的收益、回撤、超额、交易次数，并输出 `keep_default`、`watch_drawdown`、`review_sell_policy` 等动作。5. 候选只有在提高收益且最大回撤增加不超过 0.5pp 时才会进入 `review_sell_policy`，否则先保留默认全卖点退出。

当前真实候选对照：

| 市场 | 默认卖点 | 候选卖点 | 默认收益 | 候选收益 | 收益差 | 默认回撤 | 候选回撤 | 回撤差 | 默认交易 | 候选交易 | 动作 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 1/2/3 卖 | 1/2 卖 | +0.26% | -0.50% | -0.75% | 0.39% | 0.77% | +0.38% | 33 | 17 | keep_default |
| US | 1/2/3 卖 | 1/2 卖 | +4.60% | +5.13% | +0.54% | 0.47% | 1.28% | +0.81% | 68 | 38 | watch_drawdown |

策略含义：1. A 股小池忽略 3 卖会减少交易并使收益转负，说明当前样本下 3 卖不能被简单忽略，默认全卖点退出继续保留。2. US 忽略 3 卖能提高约 0.54pp 收益，但最大回撤增加约 0.81pp，超过系统当前的回撤容忍阈值，因此不直接采纳，只纳入观察。3. 该结果与卖后反事实统计一致：当前样本没有足够证据支持降低 3 卖退出力度。4. 后续如要继续研究，应增加候选：3 卖半仓退出、3 卖后等待 5m 二卖确认、30m 非下跌时延迟 3 卖退出，并全部通过同一报告层比较收益与回撤。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_portfolio_backtest_can_ignore_3sell_for_sell_policy_candidate` | 组合回测可以在候选模式中忽略小级别 3 卖，直到 1/2 卖或大级别风控触发 |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 同时传递买点倍率和默认 `sell_classes={1,2,3}` |
| `test_sell_policy_impact_report_watches_higher_return_with_worse_drawdown` | 候选收益更高但回撤恶化过大时只给 `watch_drawdown` |
| `test_sell_policy_impact_report_reviews_controlled_improvement` | 候选收益提高且回撤受控时才给 `review_sell_policy` |

局部验证：`PYTHONPATH=src python -m pytest tests/test_strategy_optimizer.py::test_sell_policy_impact_report_watches_higher_return_with_worse_drawdown tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement tests/test_strategy_optimizer.py::test_bs_point_ratio_impact_report_compares_override_to_baseline tests/test_backtest_live_parity.py::test_portfolio_backtest_can_ignore_3sell_for_sell_policy_candidate tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` 通过，结果为 `5 passed`。最终验证：`PYTHONPATH=src python -m pytest -q` 全量通过，结果为 `814 passed, 1 skipped`。真实报告已生成：`D:/chanlun_pro/reports/strategy_sell_policy_impact_report.json/md`。

## 48. 第三十八轮 3 卖半仓退出候选与回测撮合修复

2026-06-11 在 `sell12` 候选之后，继续验证“3 卖是否可以先半仓退出、等待后续 1/2 卖或 30m 风控确认”的思路。本轮先审计执行链路，发现 paper broker 已能按 `sell_ratio` 部分卖出，但组合回测只记录 `sell_ratio`，实际仍整仓卖出。若不先修复，所有部分卖出候选都会是伪回测。因此本轮先补齐回测撮合，再跑 `sell3half` 候选。

实现约束：1. `portfolio_backtest` 新增 `sell_ratio_overrides`，例如 `{"3":0.5}` 仅作用于小级别卖点。2. 30m 大级别转空不读取该覆盖，仍按 100% 强制退出。3. 小级别部分卖出时，回测只卖出对应股数，剩余仓位保留，后续 1/2 卖、再次卖点、30m 转空或收尾强平继续处理。4. `PTrade` 新增 `shares` 字段，记录每次部分成交股数。5. `live_backtest --sell-ratio-overrides 3:0.5` 可生成候选 summary/trades，summary 同步记录 `sell_ratio_overrides`。6. `strategy_sell3half_impact_report.json/md` 默认由优化器生成，候选标签为 `sell3half`。

当前真实候选对照：

| 市场 | 默认卖点 | 候选规则 | 默认收益 | 候选收益 | 收益差 | 默认回撤 | 候选回撤 | 回撤差 | 默认交易 | 候选交易 | 动作 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 1/2/3 卖全退 | 3 卖 50%，1/2 卖全退 | +0.26% | -0.08% | -0.34% | 0.39% | 0.48% | +0.09% | 33 | 19 | keep_default |
| US | 1/2/3 卖全退 | 3 卖 50%，1/2 卖全退 | +4.60% | +3.79% | -0.81% | 0.47% | 0.70% | +0.23% | 68 | 100 | keep_default |

策略含义：1. A 股 3 卖半仓减少了亏损幅度，相比 `sell12` 更稳，但仍低于默认全退，并且回撤略升，因此不采纳。2. US 3 卖半仓显著增加交易次数，收益下降约 0.81pp，回撤上升约 0.23pp，说明当前 QQQ/TSLA 小池里 3 卖全退比半仓更优。3. 这与卖后反事实统计方向一致：当前样本没有显示 3 卖后继续持有剩余仓位能补偿风险。4. 后续可继续研究的候选不是简单半仓，而是“3 卖半仓后必须出现 5m 三买才回补”或“3 卖只在 30m 上行时半仓，否则全退”。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_portfolio_backtest_can_half_exit_on_3sell_candidate` | 组合回测能在 3 卖只卖半仓，并在后续 1 卖卖出剩余仓位 |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 会把 `sell_ratio_overrides` 传到底层组合回测 |
| `test_sell_policy_impact_report_reviews_controlled_improvement` | 卖点策略影响报告会记录候选卖出比例覆盖 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py::test_portfolio_backtest_can_half_exit_on_3sell_candidate tests/test_backtest_live_parity.py::test_portfolio_backtest_can_ignore_3sell_for_sell_policy_candidate tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement tests/test_strategy_optimizer.py::test_sell_policy_impact_report_watches_higher_return_with_worse_drawdown -q` 通过，结果为 `5 passed`。最终验证：`PYTHONPATH=src python -m pytest -q` 全量通过，结果为 `815 passed, 1 skipped`。真实报告已生成：`D:/chanlun_pro/reports/strategy_sell3half_impact_report.json/md`。

## 49. 第三十九轮 30m 向上约束下的 3 卖半仓候选

2026-06-11 在无条件 `sell3half` 被否定后，继续验证更贴近高低级别联立的候选：只有当 30m 大级别笔方向仍为 `up` 时，小级别 3 卖才半仓；若 30m 非上行或转空，则不使用半仓覆盖，继续全退。这个候选意图对应缠论里“大级别走势未破坏时，小级别卖点可作为震荡短差处理；大级别走坏时不能恋战”的实盘解释。

实现约束：1. `portfolio_backtest` 新增 `sell_ratio_override_scope`，支持 `all/up/not_down`。2. `up` 作用域只在当前 30m 方向为 `up` 的小级别卖点上应用 `sell_ratio_overrides`。3. 30m 大级别转空仍然绕过覆盖并 100% 退出。4. `live_backtest --sell-ratio-override-scope up` 可生成候选 summary/trades，summary 记录 `sell_ratio_override_scope`。5. `strategy_sell3half_up_impact_report.json/md` 由优化器默认生成，候选标签为 `sell3half_up`。

当前真实候选对照：

| 市场 | 默认卖点 | 候选规则 | 默认收益 | 候选收益 | 收益差 | 默认回撤 | 候选回撤 | 回撤差 | 默认交易 | 候选交易 | 动作 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 1/2/3 卖全退 | 30m up 时 3 卖 50% | +0.26% | -0.08% | -0.34% | 0.39% | 0.48% | +0.09% | 33 | 19 | keep_default |
| US | 1/2/3 卖全退 | 30m up 时 3 卖 50% | +4.60% | +3.79% | -0.81% | 0.47% | 0.70% | +0.23% | 68 | 100 | keep_default |

策略含义：1. `sell3half_up` 与无条件 `sell3half` 的结果完全一致，说明当前 A/US 小池中触发半仓的 3 卖基本都发生在 30m 上行状态；加入 30m up 约束没有过滤掉噪声。2. A/US 两边都出现“收益下降、回撤上升”，因此该候选不采纳。3. 这进一步说明问题不在于“3 卖半仓是否应该加一个大级别上行条件”，而在于当前样本中 3 卖本身更适合全退。4. 下一步更有价值的候选是“3 卖全退后等待 5m/1m 三买回补”或“3 卖半仓后只有出现 5m 三买才回补”，也就是把优化点从卖出比例转向回补条件。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_portfolio_backtest_limits_3sell_half_exit_to_big_level_up` | scope=`up` 时，30m up 的 3 卖半仓，30m neutral 的 3 卖仍全退 |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 会把 `sell_ratio_override_scope` 传到底层组合回测 |
| `test_sell_policy_impact_report_reviews_controlled_improvement` | 卖点影响报告会记录候选卖出比例作用域 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py::test_portfolio_backtest_limits_3sell_half_exit_to_big_level_up tests/test_backtest_live_parity.py::test_portfolio_backtest_can_half_exit_on_3sell_candidate tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement -q` 通过，结果为 `4 passed`。最终验证：`PYTHONPATH=src python -m pytest -q` 全量通过，结果为 `816 passed, 1 skipped`。真实报告已生成：`D:/chanlun_pro/reports/strategy_sell3half_up_impact_report.json/md`。

## 50. 第四十轮 3 卖后仅 3 买回补候选

2026-06-11 在 3 卖半仓和 30m up 半仓均被否定后，继续把优化点从“卖出比例”转向“卖后回补条件”。本轮候选为 `sell3_rebuy3`：小级别 3 卖仍然全退，但同一标的后续只有出现 3 买才允许重新开仓，1 买/2 买不作为该标的的回补触发。这对应缠论实盘中“3 卖后不急于用低级别反抽抄回，等结构重新走出第三类买点再参与”的保守解释。

实现约束：1. `portfolio_backtest` 新增 `after_3sell_reentry_buy_classes`，默认空，不影响当前实盘默认路径。2. 仅当一笔持仓因为小级别 3 卖被完全卖出后，才给该标的挂上回补买点类别限制。3. 限制在买单真实成交后解除；若停牌、涨停或挂单未成交，限制继续保留。4. 大级别 30m 转空退出不触发该回补限制，因为大级别风控后的回补应由大级别重新不空和买点共同决定。5. `live_backtest --after-3sell-reentry-buy-classes 3` 可生成候选 summary/trades，summary 记录 `after_3sell_reentry_buy_classes`。6. `strategy_sell3_rebuy3_impact_report.json/md` 由优化器默认生成，候选标签为 `sell3_rebuy3`。

当前真实候选对照：

| 市场 | 默认规则 | 候选规则 | 默认收益 | 候选收益 | 收益差 | 默认回撤 | 候选回撤 | 回撤差 | 默认交易 | 候选交易 | 动作 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 3 卖后可按任意买点再入 | 3 卖后仅 3 买回补 | +0.26% | +0.26% | +0.00% | 0.39% | 0.39% | +0.00% | 33 | 33 | watch |
| US | 3 卖后可按任意买点再入 | 3 卖后仅 3 买回补 | +4.60% | +4.60% | +0.00% | 0.47% | 0.47% | +0.00% | 68 | 68 | watch |

策略含义：1. `sell3_rebuy3` 在当前 A/US 小池中完全没有改变收益、回撤和交易数，说明当前样本里“3 卖后被 1/2 买过早接回”不是主要问题。2. 由于没有正向收益差，也没有降低回撤，不能采纳为默认策略。3. 这也说明当前默认的 3 买优先排序已经足以覆盖该小池里的大部分回补路径。4. 下一步应研究更强的回补确认：例如“3 卖全退后必须出现 5m 三买或 5m 中枢上移确认才回补”，而不是仅限制 1m 买点类别。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_portfolio_backtest_can_require_3buy_reentry_after_3sell` | 组合回测会阻止 3 卖后的 1 买回补，等待后续 3 买才重新开仓 |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 会把 `after_3sell_reentry_buy_classes` 传到底层组合回测 |
| `test_sell_policy_impact_report_reviews_controlled_improvement` | 卖点影响报告会记录候选回补买点类别 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py::test_portfolio_backtest_can_require_3buy_reentry_after_3sell tests/test_backtest_live_parity.py::test_portfolio_backtest_limits_3sell_half_exit_to_big_level_up tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement -q` 通过，结果为 `4 passed`。最终验证：`PYTHONPATH=src python -m pytest -q` 全量通过，结果为 `817 passed, 1 skipped`。真实报告已生成：`D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.json/md`。

## 51. 第四十一轮 3 卖后等待 5m 三买确认回补

2026-06-11 在“3 卖后仅 1m 三买回补”无影响后，继续增强回补确认级别。本轮候选为 `sell3_rebuy_mid3`：小级别 3 卖仍然全退，但同一标的后续必须先出现 5m 级别三买确认，之后才允许 1m 买点重新开仓。这个候选更贴近高低级别联立：1m 负责执行，5m 负责确认回补结构是否重新站回多方。

实现约束：1. `build_symbol_from_klines()` 在存在 `mid_level=5m` 时新增 `mid_by_bar`，把 5m 买卖点按“信号时间 + 5m 延迟”映射到 1m 主时钟，避免偷看未完成 5m bar。2. QMT 缓存构建 `fetch.build()` 也写入 `mid_by_bar`，后续 A 股全市场 MTF3 缓存重建后可使用同一候选。3. `portfolio_backtest` 新增 `after_3sell_reentry_mid_buy_classes`，仅在小级别 3 卖全退后生效；等待指定 5m 买点出现后才解除 5m 确认锁。4. 买单真实成交后清除回补限制；未成交则继续等待。5. `live_backtest --after-3sell-reentry-mid-buy-classes 3` 可生成候选 summary/trades。6. `strategy_sell3_rebuy_mid3_impact_report.json/md` 由优化器默认生成，候选标签为 `sell3_rebuy_mid3`。

当前真实候选对照：

| 市场 | 默认规则 | 候选规则 | 默认收益 | 候选收益 | 收益差 | 默认回撤 | 候选回撤 | 回撤差 | 默认交易 | 候选交易 | 胜率变化 | 动作 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A 股 | 3 卖后可按 1m 买点再入 | 3 卖后先等 5m 三买 | +0.26% | +0.28% | +0.02% | 0.39% | 0.36% | -0.03% | 33 | 13 | 51.5%→61.5% | watch |
| US | 3 卖后可按 1m 买点再入 | 3 卖后先等 5m 三买 | +4.60% | +2.23% | -2.36% | 0.47% | 0.55% | +0.08% | 68 | 39 | 80.9%→76.9% | keep_default |

策略含义：1. A 股小池中，5m 三买回补确认使交易数从 33 降到 13，胜率提高、回撤略降、收益略升，但收益改善只有约 0.02pp，仍不足以作为默认切换依据。2. US 小池中，该确认过于严格，错过了大量有效回补，收益下降约 2.36pp，回撤还略升，明确不采纳。3. 这个结果说明“提高回补级别确认”对 A 股可能有防噪声价值，但不能直接用于 US；市场差异必须保留。4. 下一步若继续研究 A 股，可把候选细化为“仅 A 股启用 5m 三买回补确认，并要求连续多窗口确认”，而不是全市场统一切换。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_mid_signals_map_to_first_main_bar_after_confirmation_delay` | 5m 信号会在确认延迟后的第一个 1m 主时钟 bar 映射出来 |
| `test_portfolio_backtest_can_require_mid_3buy_reentry_after_3sell` | 组合回测会等待 5m 三买确认，再允许 1m 买点回补 |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | `live_backtest` 会把 `after_3sell_reentry_mid_buy_classes` 传到底层组合回测 |
| `test_sell_policy_impact_report_reviews_controlled_improvement` | 卖点影响报告会记录候选 5m 回补买点类别 |

局部验证：`PYTHONPATH=src python -m pytest tests/test_backtest_live_parity.py::test_mid_signals_map_to_first_main_bar_after_confirmation_delay tests/test_backtest_live_parity.py::test_portfolio_backtest_can_require_mid_3buy_reentry_after_3sell tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement -q` 通过，结果为 `4 passed`。最终验证：`PYTHONPATH=src python -m pytest -q` 全量通过，结果为 `819 passed, 1 skipped`。真实报告已生成：`D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.json/md`。

## 52. 第四十二轮 A股5m/30m大样本 3卖后3买回补候选

2026-06-11 在 `sell3_rebuy_mid3` 小样本结果之后，继续扩大 A 股验证范围。由于当前本地 `chart_cache` 只有 4 个 A 股/指数代码，不能支撑全市场判断；而 `bt_data_all_a` 有 5145 个 A 股缓存，但结构是 5m+30m，不包含 1m+5m+30m 的 `mid_by_bar`。因此本轮不把它当成 1m 实盘默认策略证据，而是单独建立 A 股 5m 执行/30m 大级别的风控回补候选报告。

本轮候选规则为 `a_5m_sell3_rebuy3`：在 A 股 5m 执行级别上，5m 3卖仍然全退；同一标的后续必须重新出现 5m 3买，才允许再次开仓。它对应缠论里“卖出后等待走势重新形成第三类买点，再参与新一段”的保守解释。这个解释只用于 5m+30m 大样本研究，不直接覆盖 1m+5m+30m 实盘联立默认路径。

实现约束：
1. `strategy_optimizer` 新增 `strategy_a_5m_sell3_rebuy3_impact_report.json/md` 默认输出。
2. 该报告复用 `build_sell_policy_impact_report()`，但固定比较 A 股大样本 5m/30m 两份 summary：`a_bt_all_a_5m30m_default_summary.json` 与 `a_bt_all_a_5m30m_sell3_rebuy3_summary.json`。
3. 新增 CLI 参数 `--output-a-5m-sell3-rebuy3-impact-json`、`--output-a-5m-sell3-rebuy3-impact-markdown`、`--a-5m-sell3-rebuy3-default-summary`、`--a-5m-sell3-rebuy3-candidate-summary`，方便后续替换为更大或不同时间窗口样本。
4. 当 `strategy_optimizer --market us` 只跑美股报告时，不额外写 A 股专项报告；默认全市场优化或 `--market a` 会写出该报告。

本轮真实大样本对照：

| 市场/样本 | 默认规则 | 候选规则 | 收益 | 基准 | 超额 | 最大回撤 | 基准回撤 | 夏普 | 胜率 | 交易 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A股300只分层样本 | 5m 3卖后可按任意后续买点再入 | 无 | +66.07% | +30.99% | +35.08% | 4.36% | 13.91% | 4.68 | 52.13% | 846 |
| A股300只分层样本 | 无 | 5m 3卖后等5m 3买再入 | +66.34% | +30.99% | +35.35% | 4.06% | 13.91% | 4.73 | 51.54% | 778 |

影响报告结论：

| 候选 | 收益差 | 回撤差 | 交易差 | 动作 |
| --- | ---: | ---: | ---: | --- |
| `a_5m_sell3_rebuy3` | +0.27pp | -0.30pp | -68 | `review_sell_policy` |

策略含义：
1. 在 A 股 5m+30m 大样本中，3卖后等待同级 5m 3买回补同时略微提高收益、降低回撤并减少交易，说明该规则有“过滤卖后弱反抽”的防守价值。
2. 该证据不能直接推导到 1m 实盘默认，因为 1m+5m+30m 小样本里的 `sell3_rebuy_mid3` 仅改善约 0.02pp，仍为 `watch`；US 小样本甚至明显变差，继续 `keep_default`。
3. 当前更合理的处理是：A 股 5m/30m 研究路径将 `a_5m_sell3_rebuy3` 标记为 `review_sell_policy`，但 A 股 1m 实盘联立路径暂不切换；后续需要重建更大的 1m+5m+30m A 股 MTF3 缓存后再验证。
4. 对应缠论原文中的级别关系，本轮不是改变“30m 同级别分解、30m以下非同级别联立”的主系统，而是确认：在只使用 5m/30m 的 A 股大样本研究里，5m 3买可作为 5m 3卖后的同执行级别回补确认。

验证：
1. A股默认大样本：`PYTHONPATH=src python -m chanlun.recursive_bt.live_backtest --market a --source bt_data --bt-data D:/chanlun_pro/bt_data_all_a --bt-pool-mode walk_forward --selection-scan-limit 300 --selection-sample-mode stratified --selection-board-filter shsz --max-pos 30 --require tech,fund,value --sell-classes 1,2,3`，结果 `+66.1%`、最大回撤 `4.4%`、交易 `846`。
2. A股候选大样本：同参数增加 `--after-3sell-reentry-buy-classes 3`，结果 `+66.3%`、最大回撤 `4.1%`、交易 `778`。
3. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `strategy_a_5m_sell3_rebuy3_impact_report.json/md`。
4. 局部测试：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_sell_policy_impact_report_reviews_controlled_improvement tests/test_strategy_optimizer.py::test_sell_policy_impact_report_watches_higher_return_with_worse_drawdown -q` 通过，结果 `2 passed`；`python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py` 通过。
5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `819 passed, 1 skipped`。

## 53. 第四十三轮 1m+5m+30m MTF3缓存覆盖审计

2026-06-11 在 A 股 5m/30m 大样本候选进入 `review_sell_policy` 后，继续补齐证据质量边界。当前系统同时存在两类证据：`chart_cache` 可以构建 1m+5m+30m 联立，但代码数量很少；`bt_data_all_a` 覆盖 A 股全市场，但当前是 5m+30m 结构，缺少 `mid_by_bar`，不能直接验证“1m 执行、5m 回补确认、30m 大级别”的全市场效果。为避免后续自动优化把二者混为一谈，本轮新增 MTF3 缓存覆盖审计报告。

实现约束：
1. 新增 `build_mtf3_cache_coverage_report()`，分别统计 `chart_cache` 中 1m、5m、30m 缓存代码数，以及三频率都齐全的完整 MTF3 代码数。
2. A 股 `bt_data` 额外做抽样字段审计：含 `small_by_bar`+`big_dir_at` 视为 5m/30m 可用；含 `mid_dir_at`+`mid_by_bar` 才视为 1m+5m+30m MTF3 可用。
3. 新增 `strategy_mtf3_cache_coverage_report.json/md`，并接入 `python -m chanlun.recursive_bt.strategy_optimizer` 默认输出。
4. 新增 CLI 参数：`--output-mtf3-cache-coverage-json`、`--output-mtf3-cache-coverage-markdown`、`--mtf3-cache-chart-cache-dir`、`--mtf3-cache-bt-data-dir`、`--mtf3-cache-bt-sample-size`。
5. `chart_cache_status` 使用分层状态：`ready_research_pool`、`limited_pool`、`small_sample_only`、`missing`；A 股 `bt_data.status` 使用 `mtf3_ready`、`mixed_mtf3`、`5m30m_only`、`unusable`、`missing`。

当前真实覆盖结果：

| 市场 | 1m代码 | 5m代码 | 30m代码 | 完整MTF3 | chart状态 | bt文件 | bt抽样 | bt 5m/30m可用 | bt MTF3可用 | bt状态 |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |
| A股 | 4 | 4 | 4 | 4 | `small_sample_only` | 5145 | 300 | 300 | 0 | `5m30m_only` |
| US | 2 | 2 | 2 | 2 | `small_sample_only` | 0 | 0 | 0 | 0 | `-` |

策略含义：
1. A 股 1m+5m+30m 联立策略当前只能用 4 个 `chart_cache` 代码做小样本验证，因此 `sell3_rebuy_mid3` 只能保留 `watch`，不能提升为全市场实盘默认。
2. A 股 `bt_data_all_a` 已经足够支撑 5m+30m 大样本研究，本轮抽样 300 个文件全部具备 5m/30m 所需字段，但没有一个样本具备 `mid_by_bar`，所以不能用于验证 1m 执行路径的 5m 回补确认。
3. 美股当前 `chart_cache` 也只有 2 个完整 MTF3 代码，US 小样本结论必须继续以 `watch/keep_default` 的保守门槛处理；不能因为 QQQ/TSLA 的局部表现就泛化到核心9。
4. 后续若要把 A 股 1m+5m+30m 策略升级为全市场级证据，下一步不是继续调参，而是重建或扩充 A 股 MTF3 缓存，使 `chart_cache_complete_mtf3_count` 或 `bt_data.sample_mtf3_ready_count` 达到研究池级别。

验证：
1. 新增单测 `test_mtf3_cache_coverage_report_separates_chart_and_bt_data`，用临时 `chart_cache` 与 `bt_data` 验证报告能区分完整三频率缓存、5m/30m-only 缓存和混合 MTF3 缓存。
2. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `1 passed`。
3. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `strategy_mtf3_cache_coverage_report.json/md`。
4. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `820 passed, 1 skipped`。

## 54. 第四十四轮 A股MTF3缓存扩容入口与报告建议动作

2026-06-11 在 MTF3 覆盖审计确认 A 股 1m+5m+30m 样本不足后，继续把“覆盖缺口”转化为可执行任务。本轮不直接运行大规模 QMT 取数，而是补齐取数入口和报告建议，让后续扩容、回测、再评估能够按同一命令链执行。

实现约束：
1. `fetch.py` 新增输出目录常量 `OUT_MTF3_ALL_A = "D:/chanlun_pro/bt_data_mtf3_all_a"`。
2. `python -m chanlun.recursive_bt.fetch mtf3_all_a [limit] [board_filter]` 现在可以批量生成 A 股 1m+5m+30m 缓存，并写入 `mid_dir_at`、`mid_by_bar`、`n_mid`。
3. `mtf3_all_a` 复用已有 `limit` 与板块过滤：例如 `300 shsz` 表示先构建沪深非北交所 300 只样本；不传 `limit` 时可面向全 A，但耗时和数据量会显著增加。
4. `strategy_mtf3_cache_coverage_report` 新增 `recommended_next_actions`：当 A 股完整 MTF3 样本不足且 `bt_data` 仍为 5m/30m-only 时，报告会输出“构建 MTF3 样本缓存”和“用新缓存回测 5m 三买回补候选”两条命令。
5. US 覆盖不足时，报告只提示需要扩展核心9的 MTF3 `chart_cache`，不生成 QMT 取数命令。

当前报告建议动作：

| 市场 | 动作 | 命令/说明 |
| --- | --- | --- |
| A股 | `build_a_mtf3_research_cache` | `PYTHONPATH=src python -m chanlun.recursive_bt.fetch mtf3_all_a 300 shsz` |
| A股 | `backtest_a_mtf3_reentry_candidate` | `PYTHONPATH=src python -m chanlun.recursive_bt.live_backtest --market a --source bt_data --bt-data D:/chanlun_pro/bt_data_mtf3_all_a --bt-pool-mode walk_forward --selection-scan-limit 300 --selection-sample-mode stratified --selection-board-filter shsz --op-level 1m --mid-level 5m --big-level 30m --mid-gate soft --max-pos 30 --require tech,fund,value --after-3sell-reentry-mid-buy-classes 3` |
| US | `expand_us_core_mtf3_cache` | 扩展至少 SPY、QQQ、AAPL、MSFT、NVDA、AMZN、META、GOOGL、TSLA 的 1m/5m/30m 缓存 |

策略含义：
1. 这一步把“继续调参数”之前必须完成的数据前置条件固定下来：没有足够 MTF3 缓存时，A 股 1m+5m+30m 回补规则只能维持观察。
2. 先用 `mtf3_all_a 300 shsz` 建研究池，而不是直接全 A，是为了验证链路、控制 QMT 拉取压力，并让报告能快速从 `small_sample_only` 进入 `ready_research_pool`。
3. 当 `bt_data_mtf3_all_a` 建成后，可用覆盖报告的 `--mtf3-cache-bt-data-dir D:/chanlun_pro/bt_data_mtf3_all_a` 参数重新审计；若抽样全部具备 `mid_by_bar`，再运行候选回测并更新 `strategy_sell3_rebuy_mid3_impact_report`。

验证：
1. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `1 passed`。
2. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/fetch.py tests/test_strategy_optimizer.py` 通过。
3. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已重新生成带 Recommended Next Actions 的 `strategy_mtf3_cache_coverage_report.json/md`。
4. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `820 passed, 1 skipped`。

## 55. 第四十五轮 策略采纳门槛报告

2026-06-11 在 MTF3 覆盖报告和 A 股 MTF3 扩容入口之后，继续把“样本覆盖不足不能采纳”的原则固化到优化器产物中。本轮新增 `strategy_adoption_gate_report.json/md`：它不替代收益/回撤报告，而是把候选报告的动作与缓存覆盖质量合并，给出候选是否具备进入采纳复核的证据条件。

实现约束：
1. 新增 `build_strategy_adoption_gate_report()`，输入 MTF3 覆盖报告和候选影响报告，输出每个候选的 `evidence_scope`、`candidate_action`、`evidence_ready`、`gate_action` 和原因。
2. 采纳门槛默认值：A 股 1m+5m+30m MTF3 至少 `90` 个样本；US MTF3 至少 `9` 个核心样本；A 股 5m/30m 大样本至少 `90` 个可用样本。
3. `sell3_rebuy_mid3` 或带 `candidate_after_3sell_reentry_mid_buy_classes` 的候选被识别为 `mtf3_1m5m30m` 证据域，需要通过 MTF3 覆盖门槛。
4. `a_5m_sell3_rebuy3` 被识别为 `a_5m30m_large_sample` 证据域，只要求 A 股 5m/30m 大样本覆盖。
5. 新增 `write_strategy_adoption_gate_report()` 与 `render_strategy_adoption_gate_markdown()`，并接入 `python -m chanlun.recursive_bt.strategy_optimizer` 默认输出。
6. 新增 CLI 参数 `--output-strategy-adoption-gate-json` 和 `--output-strategy-adoption-gate-markdown`。

当前真实 gate 结果：

| 市场 | 候选 | 证据域 | 候选动作 | 证据就绪 | Gate动作 | 原因 |
| --- | --- | --- | --- | --- | --- | --- |
| A股 | `sell3_rebuy_mid3` | `mtf3_1m5m30m` | `watch` | 否 | `watch_evidence_limited` | A MTF3 sample `4/90` |
| US | `sell3_rebuy_mid3` | `mtf3_1m5m30m` | `keep_default` | 否 | `keep_default` | US MTF3 sample `2/9`，且候选跑输 |
| A股 | `a_5m_sell3_rebuy3` | `a_5m30m_large_sample` | `review_sell_policy` | 是 | `review_allowed` | A 5m/30m sample `300/90` |

策略含义：
1. `review_sell_policy` 不再等同于“可以实盘默认切换”；只有同时通过证据覆盖门槛，才会进入 `review_allowed`。
2. A 股 1m+5m+30m 的 5m 三买回补候选继续保持观察，因为候选效果只是 `watch`，且 MTF3 样本只有 4 个完整代码。
3. A 股 5m/30m 的 3卖后3买回补候选是当前唯一进入 `review_allowed` 的卖后回补规则，但它属于 5m/30m 研究路径，不改变 1m 实盘默认。
4. US 的同类 MTF3 回补候选仍为 `keep_default`，即便未来扩展核心9样本，也需要先看候选能否从跑输转为正贡献。

验证：
1. 新增单测 `test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready`，验证 MTF3 覆盖不足时正向候选会被证据门槛拦住，而 A 股 5m/30m 大样本候选可进入 `review_allowed`。
2. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `2 passed`。
3. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py tests/test_strategy_optimizer.py` 通过。
4. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `strategy_adoption_gate_report.json/md`。
5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `821 passed, 1 skipped`。

## 56. 第四十六轮 实时监控读取策略采纳门槛

2026-06-11 在新增 `strategy_adoption_gate_report` 后，继续把该报告暴露给实时监控层。此前 gate 只存在于离线报告中，实盘 dry-run 状态行只能看到优化器主报告的 `opt_action` 和 `opt_apply`，无法直接知道当前市场里有多少候选是“证据已允许复核”、多少候选仍是“样本覆盖受限”。本轮让 `live_monitor` 在扫描状态中读取 gate 计数。

实现约束：
1. `live_monitor` 新增 `--strategy-adoption-gate-json` 参数，默认读取 `D:/chanlun_pro/reports/strategy_adoption_gate_report.json`，也支持从 `RECURSIVE_MONITOR_CONFIG` 的 `strategy_adoption_gate_json` 覆盖。
2. 新增 `strategy_adoption_gate_status(path, market)`，按市场统计 `review_allowed`、`watch_evidence_limited`、`blocked`、`keep_default` 和 `total`。
3. 当 `--optimization-report-enabled` 启用时，扫描状态行新增 `opt_gate_allowed`、`opt_gate_limited`、`opt_gate_blocked`。
4. 该功能只读 gate 报告，不改变买卖点、仓位比例、paper broker 或 runtime overrides；它的作用是让实盘运行时知道候选策略仍处在研究/受限/可复核的哪一类。

当前真实 dry-run 结果：

```text
[2026-06-11 10:33:25] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=125 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0
```

策略含义：
1. A 股实时扫描现在能直接看到：当前有 1 个候选通过采纳门槛进入复核，1 个候选仍受证据覆盖限制。
2. `opt_gate_allowed=1` 对应 A 股 5m/30m 的 `a_5m_sell3_rebuy3`；`opt_gate_limited=1` 对应 1m+5m+30m 的 `sell3_rebuy_mid3`。
3. 这避免了实盘观察中只看 `opt_action=keep_candidate` 而忽略研究候选状态的情况，也避免把 `review_allowed` 误当成自动切换默认策略。

验证：
1. 新增单测 `test_strategy_adoption_gate_status_counts_market_gates`，验证 live monitor 能按市场统计 gate 状态。
2. CLI 单测 `test_live_monitor_cli_accepts_optimization_report_switches` 增加 `--strategy-adoption-gate-json` 参数断言。
3. 局部验证：`PYTHONPATH=src pytest tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_strategy_adoption_gate_status_counts_market_gates -q` 通过，结果 `2 passed`。
4. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/live_monitor.py tests/test_recursive_live_monitor.py` 通过。
5. 实盘 dry-run 验证：`PYTHONPATH=src python -m chanlun.recursive_bt.live_monitor --market a --data-source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup` 通过，状态行包含 `opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0`。
6. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `822 passed, 1 skipped`。

## 57. 第四十七轮 实时监控刷新同步生成采纳门槛

2026-06-11 在 `live_monitor` 能读取 `strategy_adoption_gate_report` 后，继续修复一个实盘链路缺口：上一轮状态行读到的 gate 仍依赖外部先运行 `strategy_optimizer`，如果文件陈旧，实盘扫描看到的 `opt_gate_*` 可能不是本轮数据。本轮把 MTF3 覆盖审计、关键回补候选影响报告和 adoption gate 全部接入 `refresh_optimization_report()`，使每次启用 `--optimization-report-enabled` 的扫描都能刷新这些产物。

实现约束：
1. `refresh_optimization_report()` 新增输出参数：`output_mtf3_cache_coverage_json/markdown`、`output_sell3_rebuy_mid3_impact_json/markdown`、`output_a_5m_sell3_rebuy3_impact_json/markdown`、`output_strategy_adoption_gate_json/markdown`。
2. 刷新顺序为：主优化报告 -> 策略归因 -> 买卖点归因/倍率 -> MTF3 覆盖报告 -> `sell3_rebuy_mid3` 影响报告 -> `a_5m_sell3_rebuy3` 影响报告 -> adoption gate。
3. `run_once()` 在刷新优化报告时传入上述默认路径，因此状态行读取到的 `opt_gate_allowed/limited/blocked` 来自本轮刷新后的 gate 文件。
4. CLI 新增/补齐参数：`--mtf3-cache-coverage-json`、`--mtf3-cache-coverage-markdown`、`--sell3-rebuy-mid3-impact-json`、`--sell3-rebuy-mid3-impact-markdown`、`--a-5m-sell3-rebuy3-impact-json`、`--a-5m-sell3-rebuy3-impact-markdown`、`--strategy-adoption-gate-markdown`。
5. 各路径同样支持通过 `RECURSIVE_MONITOR_CONFIG` 覆盖。

当前真实 dry-run 结果：

```text
[2026-06-11 10:38:16] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=125 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0
```

策略含义：
1. gate 状态不再只是离线分析结果，而是实盘扫描过程中的同步诊断结果。
2. 当后续 A 股 MTF3 缓存扩容后，只要启用优化刷新，`opt_gate_limited` 会自动随覆盖报告变化而减少；无需手动先运行独立优化器。
3. 这仍然不自动切换策略，只把“是否允许进入采纳复核”作为实时可观察状态暴露出来。

验证：
1. CLI 单测 `test_live_monitor_cli_accepts_optimization_report_switches` 覆盖新增输出参数。
2. `test_refresh_optimization_report_returns_action_suggestions` 现在会断言 MTF3 覆盖报告、`sell3_rebuy_mid3` 影响报告、A 股 5m/30m 影响报告和 adoption gate 均被 `refresh_optimization_report()` 写出。
3. 局部验证：`PYTHONPATH=src pytest tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `2 passed`。
4. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/live_monitor.py tests/test_recursive_live_monitor.py` 通过。
5. 实盘 dry-run 验证：`PYTHONPATH=src python -m chanlun.recursive_bt.live_monitor --market a --data-source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup` 通过，状态行包含 `opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0`。
6. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `822 passed, 1 skipped`。

## 58. 第四十八轮 MTF3缓存构建 Manifest

2026-06-11 在 `mtf3_all_a` 入口和实时 gate 刷新链路完成后，继续补齐 A 股 MTF3 扩容的可审计性。此前 `fetch.run()` 只把 ok/skip/fail 打印到终端，大规模构建中如果中断，后续系统只能通过目录里的 pkl 文件和覆盖报告推断状态，无法知道本轮请求了多少代码、哪些跳过、哪些失败、哪些成功文件包含 `mid_by_bar`。本轮新增构建 manifest。

实现约束：
1. `fetch.run()` 每次执行都会在输出目录写 `_build_manifest.json`，并在开始、进度节点和结束时刷新。
2. Manifest 字段包括：`label`、`started_at`、`completed_at`、`out_dir`、`levels`、`min_small`、`metadata`、`requested_count`、`counts`、`entries`。
3. 每个 entry 记录 `code`、`status`、`path`、`time`；成功项额外记录 `n_small`、`n_mid`、`n_big`、`has_mid_by_bar`；失败项记录 `reason` 或异常类型/错误信息。
4. `fetch.run()` 新增可测试参数 `exchange`，默认仍创建 `ExchangeQMT()`；测试可以传入 fake exchange，避免触发真实 QMT。
5. `mtf3` 与 `mtf3_all_a` 入口写入明确 `manifest_label` 和 `metadata`，例如 `mtf3_all_a` 会记录 `limit` 与 `board_filter`。
6. `strategy_mtf3_cache_coverage_report` 现在会读取目标 `bt_data` 目录的 `_build_manifest.json`，在 JSON 的 `bt_data.build_manifest` 和 Markdown 的 `Build Manifests` 段展示构建摘要。

Manifest 示例结构：

```json
{
  "label": "mtf3_all_a",
  "levels": {"small_tf": "1m", "mid_tf": "5m", "big_tf": "30m"},
  "requested_count": 300,
  "counts": {"ok": 287, "skip": 5, "fail": 8},
  "entries": [
    {"code": "SH.600000", "status": "ok", "has_mid_by_bar": true}
  ]
}
```

策略含义：
1. A 股 MTF3 扩容从“一条长命令”升级为可审计数据构建流程，后续即使构建中断，也能从 manifest 判断已完成和失败标的。
2. 覆盖报告读取 manifest 后，可以把“字段抽样状态”和“本轮构建状态”并列展示，避免只看 pkl 数量误判 MTF3 样本质量。
3. 这为下一步真正执行 `mtf3_all_a 300 shsz` 和后续 1m+5m+30m 候选回测提供了可追踪证据链。

当前真实状态：
1. 默认 `D:/chanlun_pro/bt_data_all_a` 仍无 `_build_manifest.json`，覆盖报告的 `bt_data.build_manifest` 为空。
2. 覆盖报告仍显示 A 股 `chart_cache_complete_mtf3_count=4`、`bt_data.status=5m30m_only`，下一步仍是构建 `D:/chanlun_pro/bt_data_mtf3_all_a`。

验证：
1. 新增单测 `test_fetch_run_writes_build_manifest`，验证 fake 构建会写出 `_build_manifest.json`，并记录 ok/fail、levels、`has_mid_by_bar`。
2. `test_mtf3_cache_coverage_report_separates_chart_and_bt_data` 增加 manifest 读取断言，确认覆盖报告能显示 `Build Manifests`。
3. 局部验证：`PYTHONPATH=src pytest tests/test_backtest_live_parity.py::test_fetch_run_writes_build_manifest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `2 passed`。
4. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/fetch.py src/chanlun/recursive_bt/strategy_optimizer.py tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py` 通过。
5. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已重新生成覆盖报告，当前无 manifest 时报告稳定。
6. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `823 passed, 1 skipped`。

## 59. 第四十九轮 Manifest完成度摘要与重试建议

2026-06-11 在 MTF3 构建 manifest 落地后，继续增强覆盖报告对 manifest 的理解。上一轮只显示构建的 `ok/skip/fail` 汇总，但还不能直接看出：本轮请求的代码是否全部产生 entry、成功文件中有多少真正包含 `mid_by_bar`、失败代码样本是什么、manifest 是完成态还是中断态。本轮把这些字段纳入 `strategy_mtf3_cache_coverage_report`。

实现约束：
1. `_load_bt_data_build_manifest()` 新增摘要字段：`manifest_status`、`entry_count`、`missing_entries`、`ok_has_mid_by_bar`、`failed_codes_sample`、`skipped_codes_sample`。
2. `manifest_status` 分为 `completed`、`completed_with_gaps`、`incomplete`：无 `completed_at` 视为中断；有失败或 entry 少于 requested 视为有缺口。
3. Markdown 的 `Build Manifests` 行现在显示 `status`、`entries`、`mid_ok`、`missing`，用于快速判断 MTF3 构建质量。
4. 若 A 股 manifest 存在失败、缺失 entry 或中断状态，覆盖报告新增 `retry_or_continue_a_mtf3_cache` 推荐动作。
5. `retry_or_continue_a_mtf3_cache` 会根据 manifest 的 `label` 和 `metadata` 尽量还原原始构建命令，例如 `mtf3_all_a 300 shsz`。

策略含义：
1. A 股 MTF3 扩容后，系统不再只看目录里有多少 pkl，而是能判断“成功构建且包含 `mid_by_bar` 的样本数”。
2. 如果大样本构建中断，覆盖报告会优先提示继续/重试构建；由于 `fetch.run()` 已会跳过已有 pkl，重跑同一命令会自然补齐失败或未完成标的。
3. 这为后续把 `bt_data_mtf3_all_a` 接入 `strategy_adoption_gate_report` 提供更强证据：只有 `ok_has_mid_by_bar` 和抽样字段都达标时，1m+5m+30m 候选才有资格进入采纳复核。

当前真实状态：
1. 默认 `D:/chanlun_pro/bt_data_all_a` 仍无 `_build_manifest.json`，覆盖报告的 `bt_data.build_manifest` 为空。
2. 覆盖报告仍稳定显示 A 股 `chart_cache_complete_mtf3_count=4`、`bt_data.status=5m30m_only`，推荐动作仍是先构建 `mtf3_all_a 300 shsz`。

验证：
1. `test_mtf3_cache_coverage_report_separates_chart_and_bt_data` 增加 manifest entries，验证 `ok_has_mid_by_bar=1`、`manifest_status=completed_with_gaps`、`failed_codes_sample=["SZ.300001"]`，并验证推荐动作包含 `retry_or_continue_a_mtf3_cache`。
2. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready -q` 通过，结果 `2 passed`。
3. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py tests/test_strategy_optimizer.py` 通过。
4. `PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已重新生成真实覆盖报告，当前无 manifest 时报告稳定。
5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `823 passed, 1 skipped`。

## 60. 第五十轮 A股5m/30m与MTF3双目录覆盖审计

2026-06-11 在实时监控刷新 adoption gate 后，继续修复一个证据目录混用问题：A 股 `a_5m_sell3_rebuy3` 是 5m/30m 候选，需要用已有的大样本 `D:/chanlun_pro/bt_data_all_a` 作为门槛证据；而 `sell3_rebuy_mid3` 是 1m+5m+30m 候选，需要用待扩容的 `D:/chanlun_pro/bt_data_mtf3_all_a` 作为 MTF3 证据。如果只给覆盖报告传一个 `bt_data_dir`，当它指向空的 MTF3 目录时，已经有 300 个 5m/30m 样本支持的 A 股候选会被误判为 `blocked_evidence`。

实现约束：
1. `strategy_mtf3_cache_coverage_report` 将 A 股离线证据拆成 `bt_data` 与 `mtf3_bt_data` 两层。
2. `bt_data` 默认指向 `D:/chanlun_pro/bt_data_all_a`，用于审计 5m/30m 候选证据，继续统计 `sample_5m30m_ready_count`。
3. `mtf3_bt_data` 默认指向 `D:/chanlun_pro/bt_data_mtf3_all_a`，用于审计 1m+5m+30m 候选证据，统计 `sample_mtf3_ready_count` 与构建 manifest。
4. Adoption gate 对 `mtf3_1m5m30m` 候选使用 `chart_cache`、`bt_data`、`mtf3_bt_data` 三者中的最大 MTF3 覆盖数；对 5m/30m 候选仍使用 `bt_data.sample_5m30m_ready_count`。
5. `live_monitor` 新增 `--mtf3-cache-mtf3-bt-data-dir`，同时保留 `--mtf3-cache-bt-data-dir`；启用 `--optimization-report-enabled` 时会同步刷新双目录覆盖报告和采纳门槛。
6. 覆盖报告的 Markdown 表格新增 `MTF3 BT Files`、`MTF3 BT Sample`、`MTF3 BT Ready`、`MTF3 BT Status`，`Build Manifests` 同时展示两个目录的构建摘要。

当前真实 dry-run 结果：

```text
[2026-06-11 10:55:55] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=125 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0
```

当前真实覆盖状态：
1. A 股 `chart_cache_complete_mtf3_count=4`，仍不足以支撑 1m+5m+30m 候选采纳。
2. `bt_data` 指向 `D:/chanlun_pro/bt_data_all_a`，共有 5145 个文件，抽样 300 个均满足 5m/30m，状态为 `5m30m_only`。
3. `mtf3_bt_data` 指向 `D:/chanlun_pro/bt_data_mtf3_all_a`，当前文件数为 0，状态为 `missing`，仍需要继续执行 `mtf3_all_a 300 shsz` 或更大样本构建。
4. Adoption gate 当前恢复为：A 股 `sell3_rebuy_mid3` 是 `watch_evidence_limited`，美股 `sell3_rebuy_mid3` 是 `keep_default`，A 股 `a_5m_sell3_rebuy3` 是 `review_allowed`。

策略含义：
1. A 股 5m/30m 卖三后只等三买回补候选，不再被空的 MTF3 目录误伤，可以继续进入复核队列。
2. 1m+5m+30m 的 `sell3_rebuy_mid3` 仍被覆盖证据限制，符合“30m 同级别分解、30m 以下非同级别分解”的联立策略要求。
3. 实盘状态行的 `opt_gate_allowed/limited/blocked` 现在同时反映 5m/30m 大样本证据和 MTF3 扩容缺口，便于后续自动观察候选策略是否达到采纳门槛。

验证：
1. `test_mtf3_cache_coverage_report_separates_chart_and_bt_data` 验证覆盖报告能同时展示 `bt_data` 与 `mtf3_bt_data`。
2. `test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready` 验证 MTF3 候选只有在独立 MTF3 目录覆盖达标后才进入复核。
3. `test_live_monitor_cli_accepts_optimization_report_switches` 验证实时监控 CLI 接受 `--mtf3-cache-mtf3-bt-data-dir`。
4. `test_refresh_optimization_report_returns_action_suggestions` 验证实时优化刷新会写出双目录覆盖报告和 adoption gate。
5. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `4 passed`。
6. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/live_monitor.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py` 通过。
7. 实盘 dry-run 验证：`PYTHONPATH=src python -m chanlun.recursive_bt.live_monitor --market a --data-source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup --mtf3-cache-bt-data-dir D:/chanlun_pro/bt_data_all_a --mtf3-cache-mtf3-bt-data-dir D:/chanlun_pro/bt_data_mtf3_all_a` 通过，状态行包含 `opt_gate_allowed=1 opt_gate_limited=1 opt_gate_blocked=0`。
8. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `823 passed, 1 skipped`。

## 61. 第五十一轮 A股MTF3分层扩容与卖三回补矩阵

2026-06-11 在双目录覆盖审计完成后，继续推进 A 股 1m+5m+30m 真实证据池。检查发现 `mtf3_all_a 300 shsz` 原先直接取排序前 300 个代码，实际会全部落在沪市主板，不能代表“全 A 分层样本”。本轮先修正取样，再执行真实 QMT 构建和候选回测矩阵。

实现约束：
1. `fetch.py` 新增 `sample_codes_by_board()`，`mtf3_all_a` 默认采用 `stratified` 板块轮询取样；显式传 `sorted` 时仍可保留旧排序行为。
2. `mtf3_all_a` 的 manifest metadata 新增 `sample_mode`，`strategy_optimizer` 的 manifest 重试命令会保留非默认取样模式。
3. `strategy_optimizer` 直接运行时，`--mtf3-cache-mtf3-bt-data-dir` 默认改为 `D:/chanlun_pro/bt_data_mtf3_all_a`，与 `live_monitor` 保持一致。
4. A 股 `sell3_rebuy_mid3` 与 `sell3_rebuy3` 的 impact 报告默认读取 MTF3 宽样本 summary，而不是旧的少量 `chart_cache` summary。
5. `sell3_rebuy3` 被加入 adoption gate；当 summary 声明 `op_level=1m, mid_level=5m, big_level=30m` 时，gate 归入 `mtf3_1m5m30m` 证据域。
6. `live_monitor` 新增 `--sell3-rebuy3-impact-json/markdown`，实时优化刷新会同步写出 `strategy_sell3_rebuy3_impact_report` 并把它纳入 adoption gate。
7. `sell_policy_impact` 新增防守观察规则：若候选收益小幅让渡但回撤显著下降，归为 `watch_defensive`，不自动采纳，也不误判为失败候选。

真实数据构建：

```text
PYTHONPATH=src python -m chanlun.recursive_bt.fetch mtf3_all_a 300 shsz
```

构建结果：
1. Manifest：`requested=301, ok=301, skip=0, fail=0, completed=2026-06-11T11:23:07, elapsed=1144.259s`。
2. 分层样本：主板 100、创业板 100、科创板 100，另加 `SH.000001`。
3. `has_mid_by_bar=301`，全部满足当前 1m+5m+30m 回测和门禁要求。
4. 覆盖报告显示 A 股 `MTF3 BT Files=301, MTF3 BT Sample=300, MTF3 BT Ready=300, MTF3 BT Status=mtf3_ready`。
5. A 股 MTF3 推荐动作从 `build_a_mtf3_research_cache` 清除；当前覆盖报告只剩美股 core-9 MTF3 扩容建议。

A 股 1m+5m+30m 分层样本回测矩阵：

| 策略 | 收益 | 回撤 | 交易数 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 默认卖点全退 | 66.96% | 3.41% | 3504 | 收益优先基准 |
| 卖三后等 5m 三买回补 | 37.42% | 1.74% | 1552 | 过度滞后，`keep_default` |
| 卖三后等 5m 二/三买回补 | 38.10% | 1.80% | 1578 | 仍过度滞后，暂不纳入 |
| 卖三后等 1m 三买回补 | 65.75% | 2.34% | 2984 | 收益接近默认、回撤明显下降，`watch_defensive` |
| 卖三半仓退出 | 5.50% | 2.30% | 306 | 收益损失过大，废弃 |
| 30m 上行时卖三半仓退出 | 9.80% | 2.30% | 505 | 收益损失过大，废弃 |

策略含义：
1. 对当前 A 股分层 MTF3 样本，`5m` 回补确认不是越严格越好；等待 5m 二/三买会错过大量 1m 层面的重新介入机会。
2. 纯 `1m` 三买回补更贴近“30m 同级别分解、30m 以下非同级别分解”的实盘执行关系：30m 控大方向，5m 做中级别联立背景，1m 负责卖三后的可操作回补。
3. 默认策略仍是收益最高的基准；`sell3_rebuy3` 不能直接替代默认，但可作为高波动、熊市或账户回撤敏感模式的防守候选继续观察。
4. 半仓卖三退出在当前实现下显著压低交易机会和收益，暂不作为主策略候选。

当前真实 gate 状态：

```text
| a | sell3_rebuy3 | mtf3_1m5m30m | watch_defensive | yes | watch |
| a | sell3_rebuy_mid3 | mtf3_1m5m30m | keep_default | yes | keep_default |
| a | a_5m_sell3_rebuy3 | a_5m30m_large_sample | review_sell_policy | yes | review_allowed |
```

实时 dry-run 验证：

```text
[2026-06-11 11:47:47] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=131 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=0 opt_gate_blocked=0
```

验证：
1. 局部验证：`PYTHONPATH=src pytest tests/test_backtest_live_parity.py::test_fetch_board_filter_selects_requested_a_share_boards tests/test_backtest_live_parity.py::test_fetch_board_sample_stratifies_a_share_mtf3_pool tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `3 passed`。
2. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_sell_policy_impact_report_watches_defensive_tradeoff tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `4 passed`。
3. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/fetch.py src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/live_monitor.py tests/test_backtest_live_parity.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py` 通过。
4. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `825 passed, 1 skipped`。

## 62. 第五十二轮 30m上行卖三回补防守候选

2026-06-11 在 A 股 MTF3 分层样本中，`sell3_rebuy3` 全时版本能把回撤从 3.41% 降到 2.34%，但收益从 66.96% 小幅降到 65.75%。本轮继续拆解这个防守收益来自哪里：如果只在 30m 非上行时启用卖三后三买回补，几乎退化成默认；如果只在 30m 上行时启用，结果接近全时防守版本。这说明当前样本里的有效防守主要不是“弱势不追”，而是“30m 上行中 1m 卖三噪声后，不立刻用任意 1m 买点回补，至少等 1m 三买确认”。

实现约束：
1. `portfolio_backtest()` 新增 `after_3sell_reentry_scope`，默认 `all` 保持旧行为。
2. `after_3sell_reentry_scope=up` 表示只有小级别 3 卖发生时，30m 大级别方向为 `up`，才启用后续回补买点类别限制。
3. `after_3sell_reentry_scope=not_up/neutral/down/not_down` 可用于后续继续测试不同 30m 环境；本轮主要验证 `up` 与 `not_up`。
4. 卖点挂单会记录卖三发生时的 `exit_big_dir`，避免 T+1、停牌或延迟成交后用错误的后验 30m 方向。
5. `live_backtest` 新增 `--after-3sell-reentry-scope`，并把该字段写入 summary。
6. `strategy_optimizer` 新增 `sell3_rebuy3_up` 候选，默认读取 `a_bt_mtf3_1m5m30m_sell3_rebuy3_up_summary.json`，并纳入 adoption gate。
7. `live_monitor` 新增 `--sell3-rebuy3-up-impact-json/markdown`，实时优化刷新会同步写出 `strategy_sell3_rebuy3_up_impact_report`。
8. `strategy_sell_policy_impact_report` 的 Markdown 表格新增 `Candidate Reentry Scope`，避免把卖出比例 scope 与卖三回补 scope 混淆。

A 股 1m+5m+30m 分层样本新增矩阵：

| 策略 | 收益 | 回撤 | 交易数 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 默认卖点全退 | 66.96% | 3.41% | 3504 | 收益基准 |
| 卖三后三买回补，全时 | 65.75% | 2.34% | 2984 | 防守观察 |
| 卖三后三买回补，仅 30m 非上行 | 66.81% | 3.37% | 3473 | 几乎等同默认 |
| 卖三后三买回补，仅 30m 上行 | 65.76% | 2.36% | 3017 | 更合理的防守观察候选 |
| 默认 + `regime_mode=adaptive` | 53.17% | 2.42% | 3406 | 收益牺牲过大 |
| 30m 上行回补候选 + `regime_mode=adaptive` | 53.27% | 1.99% | 2936 | 低回撤但收益牺牲过大 |

策略含义：
1. 当前 A 股 MTF3 样本中，`regime_mode=adaptive` 可以降回撤，但收益牺牲太大，暂不作为默认。
2. `sell3_rebuy3_up` 比全时 `sell3_rebuy3` 更符合缠论高低级别联立：30m 上行保留多头背景，1m 卖三后要求 1m 三买再回补，用于过滤上行中枢震荡里的小级别噪声。
3. `sell3_rebuy3_up` 当前仍是 `watch_defensive`，不是自动采纳；后续需要在不同年份、熊市段、以及美股 core-9 MTF3 扩容后继续验证。
4. `not_up` 版本没有明显降回撤，说明仅用“非上行才防守”不是当前样本的主要矛盾。

当前真实 gate 状态：

```text
| a | sell3_rebuy3 | mtf3_1m5m30m | watch_defensive | yes | watch |
| a | sell3_rebuy3_up | mtf3_1m5m30m | watch_defensive | yes | watch |
| a | sell3_rebuy_mid3 | mtf3_1m5m30m | keep_default | yes | keep_default |
| a | a_5m_sell3_rebuy3 | a_5m30m_large_sample | review_sell_policy | yes | review_allowed |
```

实时 dry-run 验证：

```text
[2026-06-11 12:07:18] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=135 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=0 opt_gate_blocked=0
```

验证：
1. 局部验证：`PYTHONPATH=src pytest tests/test_backtest_live_parity.py::test_portfolio_backtest_can_require_3buy_reentry_after_3sell tests/test_backtest_live_parity.py::test_portfolio_backtest_limits_3sell_reentry_lock_by_big_direction_scope tests/test_backtest_live_parity.py::test_live_backtest_passes_confirmed_bs_point_ratio_multipliers -q` 通过，结果 `3 passed`。
2. 局部验证：`PYTHONPATH=src pytest tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions tests/test_backtest_live_parity.py::test_portfolio_backtest_limits_3sell_reentry_lock_by_big_direction_scope tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready -q` 通过，结果 `4 passed`。
3. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/portfolio.py src/chanlun/recursive_bt/live_backtest.py src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/live_monitor.py tests/test_backtest_live_parity.py tests/test_recursive_live_monitor.py` 通过。
4. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `826 passed, 1 skipped`。

## 63. 第五十三轮 US core-9 MTF3扩容与卖三回补复核

2026-06-11 在 A 股 MTF3 样本和 30m 上行防守候选完成后，继续修复美股证据不足问题。此前 US 的 1m+5m+30m chart_cache 只有 2/9 个核心标的完整，导致 adoption gate 对 US MTF3 候选只能给出 evidence limited。直接无界拉取 Longbridge 1m 历史会超时，因此本轮新增有界 US core-9 MTF3 缓存构建器，并用 2026-04-15 至 2026-06-11 的真实 Longbridge 数据完成 SPY、QQQ、AAPL、MSFT、NVDA、AMZN、META、GOOGL、TSLA 的 1m/5m/30m 扩容。

实现约束：1. 新增 `src/chanlun/recursive_bt/us_mtf3_cache.py`，默认构建 US core-9 的 `1m`、`5m`、`30m` chart_cache，写入 `v33_us_*_recursivebt.pkl`，并生成 `_us_mtf3_build_manifest.json`。2. 构建器写入最小回测可读 payload：`t/o/h/l/c/v`、`min_time`、`max_time`、`validated_at`、`cache_source`，供 `live_backtest --source chart_cache --op-level 1m --mid-level 5m --big-level 30m` 现场生成 `mid_by_bar`。3. `strategy_optimizer` 新增 US MTF3 默认 summary 路径，`sell3_rebuy3` 和 `sell3_rebuy_mid3` 不再回落到旧的 US live_parity 小样本。4. `live_monitor.refresh_optimization_report()` 同步传入 US MTF3 summary 路径，实时 dry-run 刷新 gate 时使用同一套证据。5. `strategy_mtf3_cache_coverage_report` 新增 US chart_cache manifest 摘要，展示 `requested`、`entries`、`ok`、`fail`、`missing`、`completed_at` 与频率组合。6. 当 US core-9 覆盖不足或 manifest 有缺口时，覆盖报告会给出有界构建命令 `python -m chanlun.recursive_bt.us_mtf3_cache --start-date ... --end-date ...`。

真实缓存构建结果：
```text
PYTHONPATH=src python -m chanlun.recursive_bt.us_mtf3_cache --start-date "2026-04-15 00:00:00" --end-date "2026-06-11 00:00:00"
completed: requested=27, ok=27, skip=0, fail=0
manifest=D:/chanlun_pro/chart_cache/_us_mtf3_build_manifest.json
```

当前真实覆盖状态：
```text
| Market | 1m | 5m | 30m | Complete | Chart Status |
| us | 9 | 9 | 9 | 9 | small_sample_only |
| us chart_cache manifest | levels=1m+5m+30m | status=completed | requested=27 | entries=27 | ok=27 | fail=0 |
```

US core-9 1m+5m+30m 回测矩阵：

| 策略 | 收益 | 回撤 | 交易数 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 默认卖点全退 | 12.95% | 1.42% | 343 | 当前 US 默认基准 |
| 卖三后等 1m 三买回补 | 12.35% | 1.54% | 286 | 收益和回撤均弱于默认，`keep_default` |
| 仅 30m 上行时卖三后等 1m 三买回补 | 12.43% | 1.54% | 305 | 仍未优于默认，暂不纳入 gate |
| 卖三后等 5m 三买回补 | 6.68% | 0.95% | 137 | 回撤下降但收益牺牲过大，`keep_default` |

策略含义：1. US core-9 已经达到 MTF3 adoption gate 的最小证据要求 `9/9`，后续 US 候选不再因为样本不足被挡住。2. 与 A 股不同，US 当前样本中 `sell3_rebuy3` 没有形成防守收益：回撤从 1.42% 升到 1.54%，收益从 12.95% 降到 12.35%，因此不能采用。3. `sell3_rebuy_mid3` 虽然把回撤压到 0.95%，但收益几乎腰斩，说明在 T+0、无涨跌停、核心科技权重流动性强的美股环境里，卖三后等待 5m 三买过于滞后。4. 美股默认策略继续保持“30m/5m 背景联立、1m 执行”的快速回补能力；防守型卖三回补规则仅作为后续更长年份和熊市片段的研究项，不进入当前实盘默认。5. 这也强化了分市场规则：A 股受 T+1、涨跌停和全市场分层选股影响，防守候选需要单独观察；US core-9 当前更适合保持默认卖点全退与快速再介入机制。

当前真实 gate 状态：
```text
| us | sell3_rebuy3 | mtf3_1m5m30m | keep_default | yes | keep_default |
| us | sell3_rebuy_mid3 | mtf3_1m5m30m | keep_default | yes | keep_default |
```

实时 dry-run 验证：
```text
[2026-06-11 12:38:27] scan=9 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=139 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=0 opt_gate_limited=0 opt_gate_blocked=0
```

验证：1. 局部验证：`PYTHONPATH=src pytest tests/test_recursive_live_monitor.py::test_us_mtf3_chart_cache_builder_writes_backtest_ready_files tests/test_recursive_live_monitor.py::test_load_chart_cache_syms_builds_us_backtest_symbol -q` 通过，结果 `2 passed`。2. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_sell_policy_impact_report_watches_defensive_tradeoff tests/test_strategy_optimizer.py::test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `3 passed`。3. 覆盖报告测试：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_mtf3_cache_coverage_report_separates_chart_and_bt_data -q` 通过，结果 `1 passed`。4. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/us_mtf3_cache.py src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/live_monitor.py tests/test_recursive_live_monitor.py tests/test_strategy_optimizer.py` 通过。5. 真实报告刷新：`PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 US MTF3 覆盖、影响和 gate 报告。6. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `827 passed, 1 skipped`。

## 64. 第五十四轮 牛市/震荡/熊市 Regime 压力报告

2026-06-11 在 A/US MTF3 证据补齐后，继续补足“必须考虑牛市和熊市等各种情况”的系统证据。此前 `live_backtest` summary 已经包含 `market_regime_segments`，但这些字段只埋在单个 summary 里，优化器和实时刷新链路没有把默认策略与候选策略按 bull/range/bear 做横向对照。本轮新增 `strategy_market_regime_stress_report.json/md`，把 regime 结果提升为正式优化产物。

实现约束：1. `strategy_optimizer` 新增 `build_market_regime_stress_report()`、`write_market_regime_stress_report()` 和 `render_market_regime_stress_markdown()`。2. 默认纳入 A 股 MTF3 的 `default`、`sell3_rebuy3`、`sell3_rebuy3_up`、`sell3_rebuy_mid3`，以及 US core-9 MTF3 的 `default`、`sell3_rebuy3`、`sell3_rebuy_mid3`。3. 每行输出 regime 天数、策略收益、基准超额、最大回撤、Sharpe、交易数，以及相对默认的收益差、超额差、回撤差。4. 对候选动作做保守分类：`baseline`、`improves_regime`、`defensive_improvement`、`defensive_tradeoff`、`underperforms`、`watch`、`evidence_limited`。5. `min_regime_days` 默认 10 天，低于该阈值的 regime 不给结论；当前 US bear 因 0 天样本被标记为 `evidence_limited`。6. 优化器 CLI 新增 `--output-market-regime-stress-json`、`--output-market-regime-stress-markdown`、`--market-regime-min-days`，默认写入 `D:/chanlun_pro/reports/strategy_market_regime_stress_report.*`。7. `live_monitor.refresh_optimization_report()` 同步刷新该报告，CLI 新增对应路径参数，确保实盘 dry-run 与离线优化器一致。

当前真实 regime 结果摘要：

| 市场 | Regime | 最优策略 | 结论 |
| --- | --- | --- | --- |
| A 股 | bull | `default` | 牛市中默认收益最高；卖三回补候选会少赚约 1.75-1.84 个百分点，不替代默认 |
| A 股 | range | `sell3_rebuy3_up` | 震荡中收益小幅高于默认且回撤下降约 0.40 个百分点，属于防守改善 |
| A 股 | bear | `sell3_rebuy3` | 熊市样本 19 天，收益略高于默认且回撤从 1.21% 降到 0.61%，属于防守改善 |
| US | bull | `default` | US 牛市样本 12 天，默认仍最优 |
| US | range | `default` | US 震荡样本 29 天，默认仍最优 |
| US | bear | 无 | 当前 US core-9 窗口无 bear 样本，不能给熊市结论 |

关键数据：A 股默认在 bull/range/bear 分别为 `29.21% / 25.95% / 2.58%`；`sell3_rebuy3_up` 在 range 为 `26.71%`、回撤 `1.77%`，相对默认多 `0.77%` 且回撤少 `0.40%`；`sell3_rebuy3` 在 bear 为 `2.69%`、回撤 `0.61%`，相对默认多 `0.11%` 且回撤少 `0.60%`。US 默认在 bull/range 为 `2.71% / 9.82%`，两个卖三回补候选都没有超过默认；`sell3_rebuy_mid3` 在 US bull 虽然降低回撤，但收益少 `1.78%`，只能视为防守取舍，不是当前默认。

策略含义：1. A 股 `sell3_rebuy3_up` 的定位进一步清晰：它不是牛市收益增强策略，而是震荡/弱市中降低小级别卖三噪声回补风险的防守候选。2. A 股默认策略仍应作为牛市和总体收益基准，不能因为全样本回撤下降就直接切换防守候选。3. US core-9 当前更适合维持默认快速回补；卖三后等待 1m/5m 三买在 bull/range 都没有优势。4. 后续若要真正做“牛熊自适应”，需要先把 regime 报告接入稳定确认和 runtime overrides，而不是基于单轮 regime 最优直接切策略。5. US bear 样本缺失是下一步实证缺口，应继续扩展更长历史或引入可覆盖 2022/2023 回撤窗口的数据源。

实时 dry-run 验证：
```text
[2026-06-11 12:54:45] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=139 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=0 opt_gate_blocked=0
```

验证：1. 局部验证：`PYTHONPATH=src pytest tests/test_strategy_optimizer.py::test_market_regime_stress_report_compares_candidates_by_regime tests/test_recursive_live_monitor.py::test_live_monitor_cli_accepts_optimization_report_switches tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `3 passed`。2. 语法验证：`PYTHONPATH=src python -m py_compile src/chanlun/recursive_bt/strategy_optimizer.py src/chanlun/recursive_bt/live_monitor.py tests/test_strategy_optimizer.py tests/test_recursive_live_monitor.py` 通过。3. 真实报告刷新：`PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `strategy_market_regime_stress_report.json/md`。4. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `828 passed, 1 skipped`。

## 65. 第五十五轮 US 2026Q1 弱市压力窗口

2026-06-11 在 regime 压力报告显示 US core-9 当前窗口 bear 样本为 0 天后，继续补 US 弱市实证。先探测 2022 年 9-10 月窗口，Longbridge 对 QQQ.US 的 1m/5m/30m 均返回 0 行，说明当前权限/接口无法直接获得这么远的分钟级历史。随后探测 2026-02-01 至 2026-04-15，QQQ.US 可返回 1m=19261、5m=3853、30m=643 行，因此本轮构建一个独立的近端弱市窗口，不污染当前实时 chart_cache。

构建命令：
```text
PYTHONPATH=src python -m chanlun.recursive_bt.us_mtf3_cache --out-dir D:/chanlun_pro/chart_cache_us_2026q1_bear --start-date "2026-02-01 00:00:00" --end-date "2026-04-15 00:00:00" --version v34 --cache-tag recursivebt_2026q1_bear
```

构建结果：`requested=27, ok=27, skip=0, fail=0`，覆盖 SPY、QQQ、AAPL、MSFT、NVDA、AMZN、META、GOOGL、TSLA 的 1m/5m/30m。Manifest 路径为 `D:/chanlun_pro/chart_cache_us_2026q1_bear/_us_mtf3_build_manifest.json`。

实现约束：1. 该窗口使用独立目录 `D:/chanlun_pro/chart_cache_us_2026q1_bear`，避免覆盖实时用的 `D:/chanlun_pro/chart_cache`。2. `strategy_optimizer` 新增 US 2026Q1 summary 常量，并在默认 CLI 中额外写出 `strategy_market_regime_stress_us_2026q1_report.json/md`。3. 新增 CLI 参数 `--output-us-2026q1-regime-stress-json`、`--output-us-2026q1-regime-stress-markdown`、`--us-2026q1-mtf3-default-summary`、`--us-2026q1-mtf3-sell3-rebuy3-summary`、`--us-2026q1-mtf3-sell3-rebuy-mid3-summary`。4. 该压力窗口只作为研究证据，不进入实时默认缓存和 runtime overrides。

US 2026Q1 1m+5m+30m 回测矩阵：

| 策略 | 收益 | 基准 | 超额 | 回撤 | 交易数 | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 默认卖点全退 | 4.50% | -2.31% | 6.81% | 1.37% | 239 | 弱市默认基准 |
| 卖三后等 1m 三买回补 | 3.84% | -2.31% | 6.15% | 1.62% | 197 | 收益和回撤均弱于默认，继续不用 |
| 卖三后等 5m 三买回补 | 2.67% | -2.31% | 4.97% | 0.98% | 77 | 回撤低但收益牺牲明显，防守取舍 |

US 2026Q1 regime 分段：

| Regime | 天数 | 默认收益 | 基准收益 | 默认回撤 | 候选结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| bull | 0 | 0.00% | 0.00% | 0.00% | `evidence_limited` |
| range | 38 | 4.32% | 0.72% | 0.93% | 默认最优 |
| bear | 12 | 0.30% | -3.71% | 0.09% | 默认与 `sell3_rebuy3` 几乎持平，均显著跑赢基准 |

策略含义：1. US 在近端弱市窗口里，默认策略已经能通过 30m/5m/1m 联立和全退卖点控制回撤，benchmark 回撤 15.5% 时策略最大回撤约 1.4%。2. `sell3_rebuy3` 没有带来额外防守价值，range 段少赚 0.66%，整体回撤还更高。3. `sell3_rebuy_mid3` 是典型低回撤/低收益取舍，适合记录但不能作为默认。4. 当前 US 弱市证据支持“默认保持、候选观察”的结论，与上一轮 core9 当前窗口一致。5. 真正覆盖 2022 大熊市仍需要更长分钟历史数据源；当前 Longbridge 探测结果显示该权限下 2022 分钟数据不可用。

真实压力报告：
```text
D:/chanlun_pro/reports/strategy_market_regime_stress_us_2026q1_report.md
```

验证：1. 数据探测：2022-09-01 至 2022-10-31 QQQ.US 的 1m/5m/30m 返回 0 行；2026-02-01 至 2026-04-15 QQQ.US 返回 1m=19261、5m=3853、30m=643 行。2. 缓存构建：US 2026Q1 独立 chart_cache manifest 为 `ok=27, fail=0`。3. 回测：默认、`sell3_rebuy3`、`sell3_rebuy_mid3` 三份 summary/trades 已写入 `D:/chanlun_pro/reports/us_core9_mtf3_2026q1_*`。4. 真实报告刷新：`PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `strategy_market_regime_stress_us_2026q1_report.json/md`。5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `828 passed, 1 skipped`。

## 66. 第五十六轮 分行情策略建议报告

2026-06-11 在 bull/range/bear 压力报告和 US 2026Q1 弱市窗口之后，继续把“分行情最优”推进到“可审计但不自动切换”的策略建议层。此前 `strategy_market_regime_stress_report` 能告诉我们某个行情段里哪组候选跑得更好，但单一窗口的最优不应直接写入实盘策略。本轮新增 `strategy_regime_policy_report.json/md`，把多窗口证据合并成 `keep_default`、`watch_regime_candidate`、`review_regime_candidate`、`evidence_limited` 四类建议。

实现约束：1. 默认需要至少 2 个独立证据源支持同一个非默认候选，才会从 `watch_regime_candidate` 升级为 `review_regime_candidate`。2. 只有 `improves_regime` 和 `defensive_improvement` 会被视为正向候选证据；`watch`、`defensive_tradeoff`、`underperforms` 不允许推动策略切换。3. 离线优化器会同时纳入主压力报告和 US 2026Q1 弱市窗口；实时监控刷新也会在内存中带入 US 2026Q1 研究窗口，避免覆盖掉弱市证据。4. 该报告只输出建议，不改写 runtime overrides，不改变买卖点比例。

当前真实 policy 结论：
| 市场 | 行情 | 建议 | 策略 | 支持 |
| --- | --- | --- | --- | ---: |
| A 股 | bull | `keep_default` | `default` | 1/2 |
| A 股 | range | `watch_regime_candidate` | `sell3_rebuy3_up` | 1/2 |
| A 股 | bear | `watch_regime_candidate` | `sell3_rebuy3` | 1/2 |
| US | bull | `keep_default` | `default` | 1/2 |
| US | range | `keep_default` | `default` | 2/2 |
| US | bear | `keep_default` | `default` | 1/2 |

策略含义：1. A 股震荡段的 `sell3_rebuy3_up` 和熊段的 `sell3_rebuy3` 都有防守改善证据，但都只有 1 个窗口支持，因此只能观察，不能自动切换。2. A 股牛段仍保留默认策略，避免防守规则吞掉主要收益弹性。3. US 当前窗口和 2026Q1 弱市窗口都支持默认策略，尤其 range 段已有 2 个来源同时支持默认。4. 后续如果要启用分行情 runtime override，必须先让同一候选在至少两个独立窗口中持续正向，再进入人工或自动复核。

验证：1. 新增 `build_regime_strategy_policy_report()`、`write_regime_strategy_policy_report()`、`render_regime_strategy_policy_markdown()`，并接入 `strategy_optimizer` CLI。2. `live_monitor.refresh_optimization_report()` 同步刷新 policy 报告，CLI/配置新增 `--regime-policy-json`、`--regime-policy-markdown`、`--regime-policy-min-supporting-sources`。3. 真实刷新：`PYTHONPATH=src python -m chanlun.recursive_bt.strategy_optimizer --no-attribution-baseline` 已生成 `D:/chanlun_pro/reports/strategy_regime_policy_report.json/md`。4. 实盘 dry-run：`PYTHONPATH=src python -m chanlun.recursive_bt.live_monitor --market a --data-source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup` 通过，状态行为 `opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=0 opt_gate_blocked=0`。5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `829 passed, 1 skipped`。

## 67. 第五十七轮 实时状态暴露分行情 policy

2026-06-11 在 `strategy_regime_policy_report` 写出后，继续补实盘可观测性。上一轮 policy 已经能告诉我们 A 股震荡/熊市候选只是观察、US 继续默认，但实时 dry-run 状态行仍只展示 adoption gate，不展示 bull/range/bear policy 结果。这样无人值守扫描时只能知道“候选证据门槛”，不能直接知道“分行情策略建议”。

实现约束：1. 新增 `regime_policy_status(path, market)`，只读 `strategy_regime_policy_report.json`，按市场统计 `review_regime_candidate`、`watch_regime_candidate`、`keep_default`、`evidence_limited`。2. `run_once()` 在优化报告刷新后，把当前市场 policy 计数追加到状态行：`opt_regime_review`、`opt_regime_watch`、`opt_regime_default`、`opt_regime_limited`。3. 该状态只读报告，不应用策略覆盖，不改变买卖点比例，不改变 paper broker 撮合。4. 与 adoption gate 分离：gate 解决候选证据覆盖是否足够，regime policy 解决牛市/震荡/熊市下默认或候选应处于何种观察状态。

真实 A 股 dry-run 状态：
```text
[2026-06-11 13:19:46] scan=3 holdings=0 events=0 sent=0 paper_queued=0 pending=0 paper_equity=1000000 paper_return=0.00% paper_dd=0.00% opt_runtime=142 opt_missing=2 opt_action=keep_candidate opt_apply=0 opt_gate_allowed=1 opt_gate_limited=0 opt_gate_blocked=0 opt_regime_review=0 opt_regime_watch=2 opt_regime_default=1 opt_regime_limited=0
```

策略含义：1. A 股当前状态行能直接看到 2 个分行情观察候选，对应 range 的 `sell3_rebuy3_up` 和 bear 的 `sell3_rebuy3`；bull 仍是默认。2. `opt_regime_review=0` 表示没有任何分行情候选达到多证据源复核门槛，因此 runtime overrides 仍不应启用分行情自动切换。3. 这让实盘巡检不需要打开 Markdown，也能知道牛熊震荡策略优化是否开始接近可复核状态。

验证：1. 新增单测 `test_regime_policy_status_counts_market_policies`，覆盖四类 policy action 和跨市场过滤。2. `test_refresh_optimization_report_returns_action_suggestions` 增加 policy 文件内容断言，确认刷新链路实际写出 A 股观察候选。3. 局部验证：`PYTHONPATH=src pytest tests/test_recursive_live_monitor.py::test_regime_policy_status_counts_market_policies tests/test_recursive_live_monitor.py::test_refresh_optimization_report_returns_action_suggestions -q` 通过，结果 `2 passed`。4. 实盘 dry-run 已显示 `opt_regime_watch=2 opt_regime_default=1`。5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `830 passed, 1 skipped`。

## 68. 第五十八轮 买卖点 x 行情阶段归因

2026-06-11 在分行情 policy 可见后，继续检查“缠论一二三类买卖点是否在牛市、震荡、熊市里各自有效”。此前 `strategy_bs_point_attribution_report` 只按买卖点类别聚合，无法回答同一个 3 买在 bull/range/bear 下是否失效。本轮让 `live_backtest` 的 `market_regime_segments` 输出 `daily_regimes`，再新增 `strategy_bs_point_regime_attribution_report.json/md`，按交易入场日把每笔交易映射到 bull/range/bear。

实现约束：1. `live_backtest._market_regime_segments()` 保留每日 regime 标签、20 日基准收益、基准回撤和日收益，summary 不再只保存聚合段。2. `strategy_optimizer` 新增 `build_bs_point_regime_attribution_report()`、`write_bs_point_regime_attribution_report()`、`render_bs_point_regime_attribution_markdown()`。3. 报告按市场、regime、买点类别统计交易数、胜率、均值收益、中位收益、逐笔回撤，并按卖点类别统计卖出后 20 根收益、MFE/MAE。4. 优化器 CLI、`live_monitor.refresh_optimization_report()`、`DynamicRecursiveMonitor`、默认配置均接入该报告。5. 旧 summary 若没有 `daily_regimes` 会被标记为 `missing_daily_regimes`，避免用弱证据做分行情判断。

真实样本刷新：1. A 股默认 MTF3 重新跑 `D:/chanlun_pro/bt_data_mtf3_all_a` 300 分层样本，结果仍为收益 +67.0%、回撤 3.4%、交易 3504，并写出 244 个 `daily_regimes`。2. US core9 默认按候选口径 `require=tech,nest_soft,trend3_boost` 刷新，收益 +13.2%、回撤 1.6%、交易 343，并写出 41 个 `daily_regimes`。3. 真实报告路径：`D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md`，当前无 missing source。

当前买点读数：
| 市场 | Regime | 1 买 | 2 买 | 3 买 |
| --- | --- | --- | --- | --- |
| A 股 | bull | 217 笔，胜率 56.2%，均值 +0.28% | 18 笔，胜率 66.7%，均值 +1.51% | 974 笔，胜率 52.9%，均值 +1.03% |
| A 股 | range | 508 笔，胜率 51.6%，均值 +0.32% | 25 笔，胜率 56.0%，均值 +0.94% | 1559 笔，胜率 51.3%，均值 +0.72% |
| A 股 | bear | 69 笔，胜率 52.2%，均值 +0.26% | 1 笔，样本太薄 | 133 笔，胜率 64.7%，均值 +1.45% |
| US | bull | 15 笔，胜率 93.3%，均值 +0.09% | 5 笔，样本太薄 | 86 笔，胜率 70.9%，均值 +0.46% |
| US | range | 39 笔，胜率 94.9%，均值 +0.27% | 14 笔，胜率 57.1%，均值 +0.35% | 184 笔，胜率 66.3%，均值 +0.43% |

策略含义：1. A 股 3 买不是只在牛市有效，range 与 bear 中仍有正均值，尤其 bear 段 133 笔、胜率 64.7%、均值 +1.45%，支持继续把 3 买作为主执行买点。2. A 股 1 买在三种行情里均值都偏小但为正，适合低比例参与，不宜放大。3. A 股 2 买在 bull/range 表现好但样本少，bear 只有 1 笔，不能单独升权。4. US 1 买胜率高但均值低，3 买在 bull/range 样本最厚且正期望，继续支持 US 默认的趋势三买加权逻辑。5. 卖点归因显示 A 股 1 类卖点后 20 根平均漂移多为负或接近零，继续支持全退；A 股 range/bull 的 2 类卖点后 MFE 较高但 post20 不稳定，仍只记录，不直接做部分止盈。

验证：1. 新增单测 `test_live_backtest_market_regime_segments_classify_bull_and_bear` 对 `daily_regimes` 字段断言。2. 新增单测 `test_bs_point_regime_attribution_joins_daily_regimes`，验证交易入场日能正确映射 bull/range/bear/unknown，并生成买卖点分组。3. `test_refresh_optimization_report_returns_action_suggestions`、`test_dynamic_monitor_refreshes_optimization_report` 已覆盖实时刷新链路写出新报告。4. 真实 dry-run：`PYTHONPATH=src python -m chanlun.recursive_bt.live_monitor --market a --data-source chart_cache --codes SH.513100,SZ.002920,SZ.301004 --once --force --dry-run --paper-enabled --optimization-report-enabled --no-warmup` 通过，状态行保持正常。5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `831 passed, 1 skipped`。

## 69. 第五十九轮 买点 x 行情比例 policy

2026-06-11 在买卖点 x 行情归因之后，继续把事实归因转成可审计 policy。上一轮报告显示 A 股和 US 多个买点/行情组合都有正期望，但仅有正期望并不等于可以立刻放大买入比例；必须先通过专门的比例冲击回测，再允许 runtime override。本轮新增 `strategy_bs_point_regime_policy_report.json/md`，把每个市场、regime、买点类别转成“保持当前比例、观察正向边际、复核加仓/减仓、证据不足”的策略状态。

实现约束：1. `build_bs_point_regime_policy_report()` 读取 `strategy_bs_point_regime_attribution_report`，不重新解释原始交易。2. `min_trades` 默认 30，低于门槛的组合一律 `evidence_limited`。3. 只有底层 guidance 明确给出 `allow_regime_boost` 或 `reduce_in_regime` 时，才进入 `review_regime_ratio_boost/reduce`；这也只是复核，不自动覆盖实盘比例。4. 正收益且胜率不低的组合进入 `watch_positive_regime_edge`，表示值得继续跟踪，但仍保持 `candidate_ratio_multiplier=1.00`。5. 优化器、实时 CLI、`DynamicRecursiveMonitor` 和默认配置均接入该 policy 报告。

当前真实 policy 结论：
| 市场 | policy | 数量 | 含义 |
| --- | --- | ---: | --- |
| A 股 | `watch_positive_regime_edge` | 6 | 1/3 买在 bull/range/bear 多数组合正向，但不改比例 |
| A 股 | `evidence_limited` | 3 | 2 买样本不足，尤其 bear 只有 1 笔 |
| US | `watch_positive_regime_edge` | 3 | 3 买 bull/range 与 1 买 range 正向，但不改比例 |
| US | `evidence_limited` | 3 | US bull 的 1/2 买、range 的 2 买样本不足 |

策略含义：1. 当前没有任何买点/行情组合进入 `review_regime_ratio_boost` 或 `review_regime_ratio_reduce`，因此不应生成实盘买点比例 override。2. A 股 3 买虽然在三类行情里表现正向，但逐笔回撤仍高，必须先做“按行情放大/缩小 3 买比例”的 impact backtest。3. US 3 买在 bull/range 继续作为正向观察，但当前默认已经有趋势三买加权逻辑，不额外叠加 regime 加仓。4. 2 买在 A/US 多数行情里样本不足，继续维持现有低比例/不升权结论。

验证：1. 新增单测 `test_bs_point_regime_policy_keeps_ratio_changes_review_only`，覆盖复核加仓、正向观察和证据不足三类 policy。2. `test_live_monitor_cli_accepts_optimization_report_switches`、`test_refresh_optimization_report_returns_action_suggestions`、`test_dynamic_monitor_refreshes_optimization_report` 均覆盖新路径参数和刷新链路。3. 真实优化器刷新已生成 `D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json/md`。4. 真实 A 股 dry-run 通过，状态行保持 `opt_action=keep_candidate opt_apply=0`。5. 最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `832 passed, 1 skipped`。

## 70. 第六十轮 买点 x 行情比例 impact 回测

2026-06-11 按第五十九轮结论执行"按行情放大/缩小买点比例"的真实 impact 回测。`portfolio_backtest()` 新增 `regime_bs_ratio_multipliers`（如 `{"bear": {"3": 1.25}}`）与 `regime_lookback_days`，`live_backtest` 新增 `--regime-bs-ratio-multipliers-json`（内联 JSON 或文件路径）并把乘数写入 summary。

实现约束（无前视是本轮的硬要求）：
1. 等权基准曲线在主循环前一次性预计算（`_bench_curve`，报告层复用同一条曲线）。
2. `_regime_by_date_lookup` 按 live_backtest 归因同一套规则（20 日基准涨跌 ±5%、回撤 -5%/-10%）生成日线 bull/range/bear，但整体向后 shift 一个交易日：bar 当日查询到的是**截至前一交易日收盘**的判定。实盘当日盘中看不到当日收盘，不 shift 就是前视；这也意味着回测口径与实时监控可执行口径一致。
3. 乘数只作用于买入比例（复用 `_apply_buy_ratio_multiplier` 的 0-2 倍 clamp 与 0-1 截断），不改变买卖点定义、卖出比例和大级别风控。
4. 单测 `test_portfolio_backtest_applies_regime_bs_ratio_multipliers_point_in_time` 专门验证点时性：基准当日跌破 bear 阈值时，当日信号仍按前一日 range 处理，次日信号才吃到 bear 乘数。

A 股 MTF3 300 分层样本（同窗同参，仅乘数不同）：

| 候选 | 乘数 | 收益 | Δ收益 | 回撤 | Δ回撤 | 夏普 | 交易 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 默认 | 无 | 66.96% | - | 3.41% | - | 6.31 | 3504 |
| bear3_boost | `{"bear":{"3":1.25}}` | 68.77% | +1.81pp | 3.45% | +0.04pp | 6.38 | 3504 |
| weak1_reduce | `{"bull":{"1":0.5},"range":{"1":0.5}}` | 66.52% | -0.44pp | 3.15% | -0.26pp | 6.48 | 3472 |
| combo（两者叠加） | bear3×1.25 + 弱市1买×0.5 | 68.13% | +1.17pp | 3.20% | -0.21pp | 6.54 | 3472 |

分段验证乘数机制精确：bear3_boost 只改 bear 段（+2.58%→+3.51%，bear 段回撤 +0.03pp），bull/range 几乎不动；weak1_reduce 的 range 段回撤 2.17%→1.87%。1 买平均 buy_ratio 精确减半（US 样本 0.0346→0.0173），证明乘数生效路径正确。

美股两窗口对照：

| 窗口 | 候选 | 收益 | Δ收益 | 回撤 | Δ回撤 | 结论 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| core9 当前窗口 | weak1_reduce | 14.26% | +1.07pp | 1.68% | +0.12pp | watch_positive_tradeoff |
| core9 2026Q1 弱市 | weak1_reduce | 4.29% | -0.21pp | 1.37% | +0.00pp | keep_default |

US 两窗口方向相反（当前窗口的收益提升主要来自 1 买占资减半后现金池变厚、高价值 3 买不再被现金不足缩量；2026Q1 没有这个效应），因此 US 维持 keep_default，不引入行情比例乘数。

全 A 5m/30m 5143 只第二独立证据窗口（同窗同参，max_pos=30, off, tech/fund/value）：

| 候选 | 收益 | Δ收益 | 回撤 | Δ回撤 | 夏普 | bear 段收益 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 默认 | 128.12% | - | 6.99% | - | 5.10 | -0.32%（17 天） |
| bear3_boost | 131.93% | +3.81pp | 7.08% | +0.09pp | 5.17 | **+0.94%（转正）** |

当前真实 verdict（`strategy_regime_ratio_impact_report`，missing=0）：

```text
a  bear3_boost            -> review_regime_ratio   pos=2/2
a  bear3boost_weak1reduce -> watch_regime_ratio    pos=1/1
a  weak1_reduce           -> watch_defensive       pos=0/1
us weak1_reduce           -> keep_default          pos=0/2
```

第六十轮结论：
1. A 股 `bear3_boost` 与第五十八轮归因定向一致（bear 段 3 买 133 笔、胜率 64.7%、均值 +1.45%）：熊段 3 买是大级别下跌背景中新中枢生成的确认，提高其仓位符合缠论"第三类买点的爆发力"口径，且只有一个参数、单一方向，不属于多参数过拟合。
2. **`bear3_boost` 在 MTF3 300（1m+5m+30m）与全 A 5143（5m+30m）两个独立窗口均为正向（+1.81pp/+3.81pp，回撤增幅均 ≤0.1pp），是第一个达到 `review_regime_ratio` 复核门槛的行情比例候选**。全 A 窗口 bear 段从微亏 -0.32% 转正 +0.94%。
3. A 股 `combo`（bear 3 买 ×1.25 + bull/range 1 买 ×0.5）在 MTF3 窗口收益、回撤、夏普三指标同时占优（+1.17pp/-0.21pp/6.31→6.54），但只有单窗口证据，verdict 为 `watch_regime_ratio`；下一轮应跑全 A combo 作为第二窗口。
4. 新增 `strategy_regime_ratio_impact_report.json/md`：每行=同窗同参对照，verdict 聚合要求正向窗口≥2 才输出 `review_regime_ratio`，单窗口正向只能 `watch_regime_ratio`，多窗口方向冲突一律回到 `keep_default`；该报告不写 runtime overrides。实时监控 `refresh_optimization_report()` 已同步刷新该报告（`--regime-ratio-impact-json/markdown`）。
5. `review_regime_ratio` 仍是研究级证据：接入实盘买入比例前必须解决口径差异——回测 regime 用组合等权基准，实时监控全市场扫描没有固定等权池，需改用上证指数/标普日线作为 regime 代理。**代理一致性实测（MTF3 300 样本 243 个交易日，同一分类规则）：整体一致率 74.5%，但分歧结构化且方向安全——上证指数判 bear 的 11 天全部是基准 bear（真子集，零误报），基准 bear 18 天中指数漏判 7 天（约 39% 加仓机会损失但不引入错误加仓）；bull 分歧大（基准 bull 86 天指数只认 32 天，等权强于加权所致）但 `combo` 的 bull/range 1 买乘数相同，互换无影响。结论：指数代理对 `bear3_boost` 与 `combo` 都是保守安全方向，实盘接入轮可直接采用，但应先在回测里支持「外部 regime 源」并跑指数口径对照确认收益让渡幅度。**
6. 实时监控已常驻运行（live_monitor a/us 两进程，2026-06-11 起，Start-Process 独立派生，日志 `D:/chanlun_pro/logs/live_monitor_{a,us}.{out,err}.log`），paper ledger 将随交易时段自然积累仿实盘证据。
7. 工程教训：超过 10 分钟的回测必须用 `Start-Process` 派生独立进程（会话工具的 600s 超时上限曾在三系统门控阶段杀掉全 A 回测）。

## 71. 第六十一轮 combo 第二窗口与指数口径对照

2026-06-11 继续完成第六十轮结论 3 的待办：跑全 A 5143 只 5m/30m 的 `combo` 第二证据窗口；同时按结论 5 实现「外部 regime 源」并用上证指数口径做对照。

工程插曲（全部已写入跨会话记忆）：全 A 回测真正的内存瓶颈是 Windows commit limit（44.2GB=31.7 物理+pagefile），不是物理内存——`logioptionsplus_agent`（罗技外设代理）持续泄漏 commit 达 7.25GB 是主要挤占者；连续三次 MemoryError 后杀掉该代理并暂停盘外 US 监控释放 8.7GB，第四次成功。教训固化：跑全 A 前先按 `PrivateMemorySize64` 查 commit 大户，绝不并行第二个回测。

全 A combo 第二窗口（同窗同参，仅乘数不同）：

| 候选 | 收益 | Δ收益 | 回撤 | Δ回撤 | 夏普 | bear 段 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 默认 | 128.12% | - | 6.99% | - | 5.10 | -0.32% |
| bear3_boost | 131.93% | +3.81pp | 7.08% | +0.09pp | 5.17 | +0.94% |
| **combo** | 131.79% | +3.67pp | **6.98%（-0.01pp）** | 5.19 | +1.06% |

当前真实 verdict（刷新后，missing=0）：

```text
a  bear3_boost            -> review_regime_ratio   pos=2/2
a  bear3boost_weak1reduce -> review_regime_ratio   pos=2/2
a  weak1_reduce           -> watch_defensive       pos=0/1
us weak1_reduce           -> keep_default          pos=0/2
```

第六十一轮结论：
1. **`combo`（bear 3 买 ×1.25 + bull/range 1 买 ×0.5）在两个独立窗口均正向并升级 `review_regime_ratio`，且是两窗口下的总最优**：MTF3 窗口收益/回撤/夏普三指标全面占优；全 A 窗口与 `bear3_boost` 收益几乎相同（131.79% vs 131.93%）但回撤更低（6.98% vs 7.08%）、夏普更高（5.19 vs 5.17）。
2. `portfolio_backtest` 新增 `regime_source_sym`、`live_backtest` 新增 `--regime-source-code`：regime 判定可改用外部标的（上证指数）收盘，实盘监控可复制同一口径；该标的不参与交易。
3. 指数口径对照回测（MTF3 300 + bear3_boost + `--regime-source-code SH.000001`）：

| 口径 | 收益 | 回撤 | 夏普 | bear 段收益 |
| --- | ---: | ---: | ---: | ---: |
| 默认（无乘数） | 66.96% | 3.41% | 6.31 | +2.58% |
| bear3_boost，等权基准源 | 68.77% | 3.45% | 6.38 | +3.51% |
| bear3_boost，上证指数源 | 68.44% | **3.41%** | 6.38 | +3.29% |

4. **指数口径是实盘接入的正确选择**：保留乘数收益增益的 86%（+1.48pp / +1.81pp），回撤与默认完全持平（等权口径 +0.04pp），夏普相同；与第六十轮代理分析的预测一致（指数 bear=等权 bear 真子集→少加仓但零误报）。上证指数日线在实时监控中可直接获得，不存在"实盘无固定等权池"的口径缺口。
5. 下一轮（第六十二轮）方向：把 regime 乘数以指数口径接入实时监控买入比例链路，但必须走既有采纳纪律——adoption gate 证据域 + 决策确认状态（连续确认门槛）+ runtime overrides 白名单，不允许直接改默认配置。

## 72. 第六十二轮 实时监控接入指数口径 regime 乘数

2026-06-11 把第六十/六十一轮的 `review_regime_ratio` 候选能力接进实时监控执行链，但**默认关闭**：A/US 默认配置均不带乘数，启用只能通过显式 CLI、配置或未来的 runtime overrides 确认链。

实现约束：
1. `market_runtime` 新增共享纯函数：`classify_visible_regime(daily_closes, lookback_days)`（与回测 `_regime_by_date_lookup` 同规则）与 `parse_regime_ratio_multipliers(raw)`（dict/内联 JSON/文件路径三态解析）；`live_backtest._load_regime_bs_ratio_multipliers` 重构为薄包装，消除口径漂移。
2. `live_monitor.current_visible_regime(ex, code, lookback_days)`：拉指数日线，**丢弃当日未完成 bar**（盘中看不到当日收盘，等价回测的 shift 1 日），失败或数据不足返回 range（=不调比例）；同一 (code, 日期) 按日缓存，QMT 一天只取一次。
3. `collect_monitor_events` 新增 `regime_ratio_multipliers/current_regime`：在确认比例乘数之后应用，通知 reason 追加 `regime_bear_x1.25` 风格标注；`run_once` 新增 `exchange` 参数串联主循环的 ex。
4. CLI：`--regime-ratio-multipliers-json`、`--regime-source-code`（默认取 `INDEX_BY_MARKET`，A 股=SH.000001）、`--regime-lookback-days`；fallback 链 CLI→市场配置→默认。
5. `RUNTIME_OVERRIDE_KEYS` 白名单纳入 `regime_ratio_multipliers/regime_source_code/regime_lookback_days`：未来 `strategy_decision_state` 连续确认通过后，自动覆盖链可携带乘数配置，仍受白名单与审计约束。

新增验证：

| 测试 | 证明内容 |
| --- | --- |
| `test_classify_visible_regime_rules` | bull/range/bear 分类与数据不足兜底 |
| `test_collect_monitor_events_applies_regime_ratio_multiplier` | bear 时 3 买 ×1.25 且 reason 标注；range 时不变 |
| `test_current_visible_regime_drops_today_and_caches` | 当日未完成日线被丢弃、按日缓存、取数失败回 range |
| `test_live_monitor_cli_accepts_regime_ratio_switches` | CLI 三参数透传 |

真实 dry-run：`--regime-ratio-multipliers-json regime_mults_combo.json` 链路跑通（chart_cache 源无日线时安全降级 range）。最终验证：`PYTHONPATH=src pytest -q` 全量通过，结果 `840 passed, 1 skipped`。

当前状态：实时链路已具备能力但保持默认关闭，符合"单轮证据不切默认"的纪律。启用路径有三条：人工 CLI 显式启用、市场配置写入、或等 regime ratio impact 的 `review_regime_ratio` verdict 进入决策确认链后由 runtime overrides 自动携带。

实时可观测性：`regime_ratio_review_status()` 读取 impact 报告 verdicts，状态行追加 `opt_rratio_review/opt_rratio_watch`。当前真实 A 股 dry-run 状态行：

```text
... opt_regime_watch=2 opt_regime_default=1 opt_regime_limited=0 opt_rratio_review=2 opt_rratio_watch=1
```

即 A 股有 2 个达到复核门槛的行情比例候选（`bear3_boost`、`combo`）与 1 个防守观察候选。

verdict→decision 自动确认链**暂缓**：当前两个 review 候选的证据窗口都来自同一段历史（2025-06~2026-06），decision_state 的"连续确认"语义对同一批 summary 的重复刷新没有信息增量，做成自动应用反而是假安全。升级条件：出现真正独立的新证据窗口（下一季度真实 paper ledger 数据、美股 bear 窗口、或新的时间段回测）支持同一候选后，再把 verdict 接入 `action_suggestions/decision_state` 生成带乘数的 runtime override。

## 73. 总体目标验收矩阵（2026-06-11 复核）

对总目标逐项复核"要求 → 实现 → 当前验证证据"。所有列出的测试均在本日重新执行通过。

| # | 目标要求 | 实现 | 本日验证证据 |
| --- | --- | --- | --- |
| 1 | 30m 同级别分解、30m 以下非同级别分解 | `core/zs_upgrade`：5m 走势类型恰好三段重合（整段高低点，原文 20 课）成 30m 中枢、不延伸；线段→1m 中枢→延伸/扩展/扩张（原文 10018 简化公式 `[max(DD),min(GG)]`）升级链 | `test_zs_upgrade.py + test_zslx_branch.py` 45 passed |
| 2 | 1m 图展示笔+1m/5m/30m 中枢/买卖点/背驰；5m 图笔+5m/30m；30m 图笔+30m | `cl_data_to_tv_chart` 的 `recursive_levels` 契约（§20） | `test_cl_data_to_tv_chart_zs.py` 8 passed；`multitimeframe_overlay_contract` 用真实 SZ.301004 fixture 断言 1m levels⊇{0,1,2}、5m⊇{0,1}、30m={0} 封顶，每级 `zss`、L1+ 必有 `mmds/bcs`，且 `bis/bi_mmds/xd_mmds/bi_bcs/xd_bcs` 全在 |
| 3 | 深入理解原文正文/回复/图表 | 原文理论文档（`chanlun_core_redesign_0/9`）+ 代码行号锚（行 2835 中枢区间公式、行 5010-5066 趋势背驰 A 段、行 2891/3565 趋势 GD、20 课三段重合、38515-38544 三层架构、23172 三买程式、13507 三级结构、第 8/9 课选股） | `test_audit_repro_yuanwen.py` 把原文行号级规则编码为断言，12 passed（曾为 RED 确证 bug，修复后 GREEN） |
| 4 | 中枢/走势类型/扩展/扩张/背驰/一二三类买卖点/区间套全覆盖 | branch core 八模块（中枢/走势类型/背驰/递归/一二三类买卖点/区间套） | 核心模块 134 passed（`zs/zslx/beichi/bs1/bs2/bs3/interval_nest/recursive`） |
| 5 | 三个独立选股系统 | `a_selection_systems()`：①基本面质量/成长 ②比价低估(ROE 年化/PB) ③缠论买点执行（原文 38542 独立性；伪乘法已按第 9 课行 430 证伪） | `test_a_selection_systems_define_three_independent_confirmations` passed |
| 6 | 大小级别买卖点结合的实盘策略 | 30m 硬风控 + 5m soft gate + 1m 触发；A 股全 A walk_forward `tech,fund,value` 30 槽（+128.1%/DD7.0%/夏普5.10）；US core9（+19.6%/DD1.5%/夏普9.31） | §22-25 真全 A/core9 证据；`test_backtest_live_parity.py` 46 passed |
| 7 | 持续仿实盘实时交易 | live_monitor a/us 常驻进程 + paper broker 权益曲线 | 进程存活验证 + ledger 盘中快照（§70.6） |
| 8 | 不同市场交易规则 | 主板 10%/创业科创 20%/北交所 30% 涨跌停、T+1、100 股、印花税 vs US T+0、1 股、无涨跌停 | `test_paper_broker_uses_us_t0_and_lot_one_rules` 等 passed |
| 9 | 根据回测迭代修复缠论系统 | 62 轮迭代（本文档全程），F1 中枢公式/F2 背驰 A 段/F3 趋势 GD 等原文修正 | 全量 841 passed, 1 skipped |
| 10 | 牛市/熊市各种情况 | regime 分段压力报告（§64-66）+ 第 60-62 轮行情比例乘数（bear 3 买 ×1.25 + 弱市 1 买 ×0.5，2/2 窗口 review，指数口径实盘可复制） | §70-72 |
| 11 | A 股全市场多系统选股 | walk_forward 全 A 5143 只三系统门控 + selector 动态入池 | §16/§22 |
| 12 | 通知带买入/卖出比例 | `recommended_buy_ratio/sell_ratio`（3 买 1.0 / 2 买 0.75 / 1 买 0.5 槽 × 大级别/共振/区间套/行情修正），钉钉通知，paper 按比例撮合 | `test_collect_monitor_events_*` + `test_paper_broker_queues_monitor_events_with_ratios` passed；钉钉 hook 命令可发现 |
| 13 | 回撤最低、收益最高 | 统一评分（回撤 2 倍惩罚）+ 候选排名 + adoption gate + 低回撤候选（50 槽 DD6.7%）与收益默认（30 槽 +128.1%）双轨 | §29-35/§70-72 |

结论：总目标的全部构成要素均有实现与可重跑的测试证据；系统当前处于"持续仿实盘积累 + 候选复核等待独立窗口"的稳态优化阶段。

## 74. 第六十三轮 涨跌停判定实盘一致性修复

2026-06-11 应用户要求对 A 股回测的涨跌停撮合做实盘一致性审计，发现并修复一个全链路真实 bug。

**Bug**：四处实现（`portfolio._limit_locked`、`engine.Simulator._limit_locked`、`paper.SymbolState`、`live_monitor.MonitorSymbolState`）全部用**前一根 bar 的收盘价**作为涨跌停判定基准。日线回测恰好正确（前一根=昨日），但分钟级回测里单根 1m/5m bar 相对前根涨不到 10%，判定恒为 False——**涨停板上回测照常买入、实时纸面账户同样漏判**，与实盘"封板买不进"不一致。3 买突破追涨停是高频场景，系统性虚增分钟级收益。涨跌停撮合层的既有测试都是注入 `prev_close` 测的（broker 层正确），bug 藏在 prev_close 的**填充层**。

**修复**（commit 4586b9aa）：
1. `engine.prev_day_close_series(dates, closes)`：逐 bar 向量化预计算**前一交易日收盘**（分钟 bar=昨日最后一根收盘、日线 bar=前一根收盘、首日 NaN=不判）。
2. `engine.latest_prev_day_close(df)`：实时 K 线窗口取昨日尾根收盘（窗口内无昨日数据返回 0=不判）。
3. 四处统一接入：判定=「挂单 bar 开盘价 / 昨日收盘 - 1」对照板块限幅（主板 10%/创业科创 20%/北交所 30%，0.995 容差吸收前复权舍入）；涨停买单按"错过信号"丢弃、跌停卖单与停牌单顺延、T+1 不变。
4. 新增测试钉死"盘中逐步封板"场景：挂单 bar 相对前根仅 +1.9% 但相对昨收 +10%，必须锁死（旧实现放行）；日线行为回归不变。全量 `843 passed, 1 skipped`。

**修复后基线重跑（A 股 MTF3 300 分层样本，1m+5m+30m，同窗同参）**：

| 口径 | 默认 | bear3_boost | combo |
| --- | --- | --- | --- |
| 修复前（涨停可买，作废） | +66.96% / DD 3.41% / 夏普 6.31 | +68.77% / DD 3.45% | +68.13% / DD 3.20% |
| **修复后（实盘可成交）** | **+66.11% / DD 3.41% / 夏普 6.24** | **+67.91% / DD 3.45%（+1.80pp）** | **+67.32% / DD 3.20% / 夏普 6.47（+1.21pp/-0.21pp）** |

关键结论：
1. 默认基线下修 0.85pp/年——这正是旧口径在涨停板上虚增的、实盘拿不到的部分；回撤与交易数基本不变，说明体系收益不依赖涨停追买。
2. **两个 review 候选的优势在真实可成交约束下完整保留**（bear3_boost +1.80pp vs 修复前 +1.81pp；combo +1.21pp/-0.21pp vs +1.17pp/-0.21pp），候选结论不被修复推翻。
3. 实时端已同步：监控/纸面账户重启加载新口径,涨停日的买入通知与撮合从此与回测一致。
4. 已知未建模项（占比小，列为后续）：ST 股 ±5%、新股上市初期无涨跌停、盘中开板回封的时点级可成交窗口（当前按挂单 bar 开盘判定，保守口径）。

**修复后全口径重跑定稿（7 组串行,含全 A 5143 只与指数口径对照）**：

| 窗口 | 默认(修复后) | bear3_boost | combo | weak1_reduce |
| --- | --- | --- | --- | --- |
| MTF3 300 (1m+5m+30m) | +66.11% / DD 3.41% / 夏普 6.24 | +67.91% (+1.80pp) / DD 3.45% | **+67.32% (+1.21pp) / DD 3.20% / 夏普 6.47** | +65.78% (-0.33pp) / DD 3.15% |
| 全 A 5143 (5m+30m) | +130.01% / DD 6.52% / 夏普 5.16 | +130.39% (+0.38pp) / DD 6.64% | +130.37% (+0.36pp) / DD 6.53%(持平) | - |
| MTF3 指数口径 bear3 | - | +67.61% (+1.50pp) / DD 3.41%(零增) | - | - |

修复后最终 verdict（零口径混杂，missing=0）：

```text
a  bear3_boost            -> watch_regime_ratio    pos=1/2 (全A回撤+0.11pp超容差降级)
a  bear3boost_weak1reduce -> review_regime_ratio   pos=2/2 (唯一保持复核门槛)
a  weak1_reduce           -> watch_defensive       pos=0/1
us weak1_reduce           -> keep_default          pos=0/2
```

第六十三轮最终结论：
1. **全 A 5m/30m 默认基线在真实涨停约束下反而更好**（+128.12%→+130.01%，回撤 6.99%→6.52%）：涨停追买的票被锁死后，slot 资金转投同期其他买点标的，组合净效应为正——体系收益不依赖涨停追买，反而被它拖累。
2. **修复暴露了 regime 乘数候选的真实价值**：修复前 combo 全 A 增益 +3.67pp，修复后只剩 +0.36pp——约九成来自实盘买不进的涨停板成交（bear 段加仓的恰是易涨停的超跌反弹票）。这正是"回测和实盘结果一致"审计的核心价值：如果没修就采纳该候选，实盘会拿不到回测声称的收益。
3. 修复后 `combo` 仍是唯一 2/2 窗口正向的 review 候选，但全 A 窗口增益微弱（+0.36pp），主要价值在 MTF3 窗口（+1.21pp 且回撤改善 -0.21pp）。`bear3_boost` 单独使用降级为 watch。
4. 指数口径在修复后依然成立：+1.50pp、回撤零增加，保留等权口径增益的 83%，实盘接入口径不变。
5. 第71节中基于修复前口径的"combo 全 A +3.67pp"等数字全部作废，以本节为准；第60-62 节的修复前数字保留为历史对照，不再用于决策。
6. 实时端已同步新口径并重启（监控/纸面账户的涨停判定从此与回测、与实盘一致）。

## 75. 第六十四轮 修复后口径乘数幅度敏感性

2026-06-11 在涨停修复后口径下验证 combo 参数(bear 3 买 ×1.25 / 弱市 1 买 ×0.5)是否处于平滑邻域(防过拟合:孤峰参数不可用)。MTF3 300 分层样本,同窗同参:

| 配置 | 收益 | Δ收益 | 回撤 | Δ回撤 | 夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认(无乘数) | 66.11% | - | 3.41% | - | 6.24 |
| bear3×1.15 + w1×0.5 | 66.54% | +0.43pp | 3.18% | -0.23pp | 6.44 |
| **bear3×1.25 + w1×0.5 (combo)** | 67.32% | +1.21pp | 3.20% | -0.21pp | 6.47 |
| bear3×1.40 + w1×0.5 | 68.51% | +2.39pp | 3.22% | -0.19pp | 6.52 |
| bear3×1.25 + w1×0.25 | 64.68% | -1.43pp | 3.04% | -0.37pp | 6.28 |
| bear3×1.25 + w1×0.75 | 67.15% | +1.04pp | 3.32% | -0.09pp | 6.36 |

第六十四轮结论:
1. **combo 参数稳健**:两个维度邻域均平滑——bear3 维度(1.15→1.25→1.40)收益单调上升、回撤几乎不动、夏普同步升,1.25 不是孤峰;w1 维度 0.5 是缓峰(0.75 仍正向,0.25 砍太狠丢 1.43pp 收益,证伪"防守越狠越好")。
2. bear3 维度的单调性提示熊段 3 买是结构性 alpha(与第五十八轮归因一致),`×1.40` 在 MTF3 窗口三指标全面更优,已按两窗口纪律送全 A 第二窗口验证(结果见下补充)。
3. 注意 bear 段仅 19 个交易日,放大幅度=放大对该段的依赖;若全 A 窗口不支持 ×1.40,维持 ×1.25 并等待新熊段样本。

全 A 5143 只第二窗口验证(修复后口径,同窗同参):

| 配置 | 收益 | Δ收益 | 回撤 | Δ回撤 | 夏普 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认 | 130.01% | - | 6.52% | - | 5.16 |
| combo(bear3×1.25) | 130.37% | +0.36pp | 6.53% | +0.01pp | 5.17 |
| **combo_b140(bear3×1.40)** | **131.53%** | **+1.51pp** | **6.58%** | **+0.06pp** | **5.18** |

当前真实 verdict(刷新后,missing=0):

```text
a  bear3_boost            -> watch_regime_ratio    pos=1/2
a  bear3boost_weak1reduce -> review_regime_ratio   pos=2/2
a  combo_bear3x140        -> review_regime_ratio   pos=2/2  ← 新最优候选
a  weak1_reduce           -> watch_defensive       pos=0/1
us weak1_reduce           -> keep_default          pos=0/2
```

补充结论:
4. **combo_b140(bear3 买 ×1.40 + 弱市 1 买 ×0.5)两窗口均 review 且全面优于 ×1.25 版**(MTF3 +2.39pp vs +1.21pp,全 A +1.51pp vs +0.36pp,回撤增幅均≤0.06pp),成为当前最优 review 候选;窗口表已加入 `combo_bear3x140`。
5. 仍不自动采纳:两窗口共享同一 19 天 bear 段,×1.40 对该段依赖更重;采纳决策维持等独立新证据窗口(新熊段/下季 paper)确认的纪律。实盘若手动启用,建议从保守的 ×1.25 起步。


## 缠论核心专项轮（2026-06-11）：原文全集深读（配图+回复版）+ 同级别分解段语义修复（fix/zhongshu-l0）

本节为缠论核心专项（不占 recursive_bt 轮次编号，第65轮=ST涨跌停建模见对应commit）：响应 goal「结合原文全部内容+图表深入研究中枢/走势类型/扩展/扩张/同级别分解/一二三类买卖点，30m=同级别分解、30m以下=非同级别分解，三周期图表矩阵」。

### 1. 资料工程（新资产）

- 解析 `docs/缠中说禅缠论108课教你炒股票配图+回复版本（共3697页）.docx`（131MB）→
  `D:/BaiduNetdiskDownload/_chanlun_work/extracted/`：122 个课程分组（含各课独立回复篇
  「回复（一）/（二）」），**1061 张配图全部索引**（image_index.json：图→课程+上下文段落），
  与 chanlun.txt 行号映射（chanlun_txt_lesson_lines.json）。仅课 4 两版本皆缺（文档自身缺陷）。
- 5 个主题深读笔记（正文+回复+逐张读图，每条带行号判据）：`docs/yuanwen_study/topic1..5`
  （中枢/走势类型/级别 39 判据；延伸-扩展-扩张辨析+3买；同级别/非同级别分解 20 判据；
  一二三类买卖点完备体系；背驰+区间套）。审计工作笔记 `audit_worknotes.md`。

### 2. 核心修复：同级别分解段语义（commit de487779，schema v34）

**两类实测病（各错一半，同根=段方向标签对 V/Λ 型 expand 链固有歧义）**：
- SH.000001 5m：高位横盘+暴跌收尾的盘整段继承摆动腿方向标 up → 与前「上涨」相邻同向被
  _jiehe_segments 误并成净下跌的 up 段（3914→3795）→ 30m tongjibie 三段重合建立在失真段上。
- SH.600519 5m：V 型链（1428→1322→1565）净位移若用**离开段终点**（1565）则 down 腿翻成 up
  → 丢失向下段 → 30m 中枢消失。

**修复（判据：39课L25179 Ai 严格交替按涨跌；L25128 段起点=前段结束点；18课L8131 a1=b1
共享端点；42课L26239 含 N 个不延伸中枢的趋势在同级别分解中仍是一段）**：
1. `zslx_branch._finalize`：盘整段 _type = 净位移·**转折点口径**（进入段起点→**离开段起点**；
   离开段跨越转折属下一段），swing_dir 降为 fallback。
2. `zs_upgrade.tongjibie_zhongshu_ex`：交替段改 `_swing_alternating_segs`（本体摆动腿直出，
   与 zslx 标签解耦；39课结合运算合并后「还是 5 分钟级别的走势类型」=合并不升级，无级别错配）。
   段端点=转折点，span=腿内中枢 gg/dd ∪ 两端转折值。`_jiehe_segments` 保留为未接线原语。

**信号基线 diff（10 标的 5m，scripts/signal_baseline_diff.py）**：L0 买卖点 64 个**零变动**；
30m 仅 600519 区间收紧 [1322,1510]→[1322,1431]（清除离开段污染）、510300 假窄条 [4.71,4.78]
消失（z5 本体 [4.789,5.041] 与前震荡区 ≤4.778 真实不重叠，窄条恰由污染值拼出）；000001
[3871,4142] 不变。tests/core 358→362 passed（新增 net-displacement 单元 + 000001/600519
fixture 回归）。fixture：tests/fixtures/klines/a_SH_{000001,600519}_5m.parquet（QMT 拉取，本地保留）。

### 3. 架构与三周期矩阵验证（commit 7d2d1b1e）

- `cl._UPGRADE_CHAIN` 与 goal 一致：1m→[5m kuozhan 非同级别, 30m tongjibie 同级别]、
  5m→[30m tongjibie]、30m 无链（封顶操作级，L24735）。
- 端到端 PASS（scripts/verify_matrix_chartdata.py，web 同路径 cl_data_to_tv_chart）：
  1m 图=笔1665+L0(1m)29中枢+L1(5m)12中枢/10买卖点/3背驰+L2(30m)层就绪；
  5m 图=笔421+L0 10中枢+L1(30m)1中枢（正在形成 [3871,4142]）；30m 图=笔129+L0 2中枢。
  1m 图 L2=0 个为窗口现实（12 个 L1 中枢只走出 down/up 两腿，第三段未出；30m 中枢=月级结构）。
- 前端 CHART_TYPES 注册回归 9 passed；chart_cache schema v33→**v34** 强制旧缓存失效；
  web 已带 PYTHONPATH 用 conda env python 重启（9900 端口正常）。

### 4. 审计裁决（详见 docs/yuanwen_study/audit_worknotes.md）

- **V2（1m 图与 5m 图的 30m 中枢口径不同）**：合法多义性（43课L26543-26551 两种分解读法
  皆合法 + 36课L24180 按当下组合的图形意义操作），各图内部自洽即可，保持现状。
- **V3（kuozhan_level_signals 用单根线段当 L1(5m) 的离开/回试段）**：与 30m 修复前同质的
  级别错配（程度低一档），记录为后续精修项（可仿 tongjibie_level_signals 段粒度化）。

### 5. 风险与遗留

- 本轮修复不影响 recursive_bt 实盘信号链（L0 买卖点零变动；live_monitor 两进程未动）。
- `_jiehe_segments` C3.3 精确条件版（「A2 升破 a 高点且 A3 不跌回」）未实现，当前摆动腿
  近似已满足交替+趋势整段；若将来精修买卖程式（38课L24751 先买后卖韵律）可在原语上做。

## 76. 第六十五轮 主板 ST 股 ±5% 涨跌停建模

2026-06-11 补齐第六十三轮(§74)列出的撮合缺口:主板 ST/*ST 股涨跌幅 ±5%(创业板/科创板 ST 仍 20%、北交所无 ST 制度)。

实现(commit ca5ae3a8):
1. `engine.A_ST` 规则(limit_pct=0.05);`fetch.py st_list` 子命令用 QMT `InstrumentName` 构建 `_st_list.json`,真实名单 **172 只主板 ST**(构建端即过滤板块)。
2. `market_rules_for_code(market, code, name="")`:显式名称含 ST 优先,其次查名单;`load_cached` 按名单覆盖 `limit_pct`,无需重建缓存。
3. 名单按当前名称近似(戴帽/摘帽时点未追溯),一年回测窗口内偏差有限,已注明。
4. 测试:主板/创业/科创/北交所 ST 分板规则、load_cached 名单覆盖、名单构建过滤,全量 `850 passed, 1 skipped`。

全 A 5143 只重跑影响评估:**零差异**(+130.01%/DD 6.52%/夏普 5.16/1460 笔,与 ST 规则前完全相同)。验证两点:
1. 规则确实生效(真实环境抽查 ST 码 `limit_pct=0.05`),零差异是真实结果——组合 1460 笔中仅 13 笔 ST(0.9% 暴露),且全部为盘中温和成交(入场时日内涨幅未达 ±5% 触发区)。
2. **三系统质量门控(fund/value)天然规避 ST 暴露**:基本面恶化的 ST 股几乎过不了质量与比价确认,这是体系性的风险规避而非偶然。ST 规则的价值在防御侧:纯 `tech` 模式、自选池含 ST、或持仓中标的被戴帽时,卖出端 ±5% 跌停顺延口径从此正确。

A 股回测-实盘一致性清单至此:T+1、板块涨跌停(10/20/30%)、主板 ST ±5%、涨停昨收口径(§74)、跌停卖出顺延、停牌冻结+顺延、印花税、100 股整手、滑点敏感性——全部建模并有测试钉死。剩余已知近似:新股上市初期无涨跌停(样本占比极小)、盘中开板回封的时点级窗口(保守口径)、ST 戴帽时点未追溯。

### 6. 第二轮补充（goal 重触发，2026-06-11晚）：39课条件版证伪 + web 真实口径终验

- **「1m 图 30m 级别空」为验证窗口假象**：web 经 `QMT_LOOKBACK_OVERRIDE_DAYS`（exchange_qmt.py，1m/5m=365天，注释明言为 30m 同级别中枢而设）实拉一年 1m；一年口径下 1m 图 L2(30m)=1 个已完成中枢 [3859,3905]，三级齐（L0=71中枢/88买卖点/17背驰、L1(5m)=29/23/4、L2(30m)=1中枢）。上轮 5 个月窗口的 0 个为自设窗口偏短。
- **「5m 图 30m 买卖点/背驰=0」=当下市场状态**：组(0,2) 后第 4 交替段未走出 → 无离开/回抽段 → 无信号（L24736 第二段走出后才能分解）。等市场走出下一段自然激活。
- **39课 L25179 条件版结合运算（紧致中枢方向）已证伪**：推演 000001 紧致中枢 [3955,4099] 的三段含「隐式连接段」（4099→3955 无中枢纯次级下跌）——违反 17 课铁律（中枢=3个连续次级别**走势类型**重叠，隐式段不够格，只能结合运算并入相邻段→必然回到摆动腿结果）。39课状态机定位=操作程式（滚动买卖），不替代 17/38 课中枢定义。**摆动腿版（v34）=17课铁律下的正确实现，紧致版与 cr_zdzg 同列禁止再试的错路**。

### 7. 第三轮（浏览器端最后一公里，2026-06-11夜）：前端跨窗渲染真凶修复

goal 第三次重触发后用 Playwright 走完「图上真的能看到」最后一公里，抓到前端真凶：
- **后端数据三级齐全**（/tv/history 实测：1m 图 recursive_levels L0=72中枢/L1=29+23买卖点+4背驰/L2=1中枢）但浏览器容器 recursive_zss 恒 0。
- **真凶=charts.js reconcile 窗口过滤**：recursive_zss 用 includeOverlaps=false（headTime>=from），高级别中枢框跨度数周~数月、左沿恒在可视窗之前 → 永不渲染。当初改 false 是为修「TV 把未加载角点 snap 到边缘致框塌/错位」。
- **修复（commit 2ae7fe06）**：includeOverlaps=true + 渲染前把窗外左沿 clamp 到已加载窗口左缘——框显示右段、角点不 snap；滚动恢复真实左沿。
- **浏览器终验**：1m 图 recursive_zss=1✓、30m 图=1✓、5m 图默认窗 0（中枢整体在窗口左侧之外=正确行为）/150 天窗 **9 个全显**✓；截图存 D:/chanlun_pro/browser_verify/。
- 教训追加「数据有但页面不显示→先查前端」条目：本例数据在 HTTP 响应里都有，断点在 reconcile 的**可视窗过滤策略**（非容器注册）——排查顺序：HTTP 响应字段 → cl_show_config toggle → reconcile 过滤（headTime/tailTime vs from）。

## 77. 第六十六轮 kuozhan 级买卖点段粒度化（V3 审计修复，schema v35）

2026-06-11 修复缠论核心专项轮 §4 审计裁决记录在案的 V3 精修项：`kuozhan_level_signals` 用单根线段（`xds[b0+1]/[b0+2]`）当 L1(5m) kuozhan 中枢的离开/回试段——与 30m 修复前（v31 已修）同质的级别错配，程度低一档（线段 vs 1m 走势类型差一级；30m 当时差两级）。判据 topic2 C2.10：对 5m 中枢，3 买回试=「次级别走势类型」=1m 走势类型，非单根线段。

实现（`kuozhan_level_signals_ex` 替换旧版，cl.py 接线，旧单线段口径删除）：
1. 次级别段 = `_swing_alternating_segs(lower_zss)`（下级中枢摆动腿，与 tongjibie/v34 同口径同实现）；升级中枢本体由 `expanded_with`（延伸=[z]/扩张=[a,b]）定位段范围。
2. 本体端点恰为腿端点 → 进入/离开/回试 = 相邻整腿；本体在腿中间 → 进入/离开 = 腿内剩余子段（`_leg_sub_seg`，端点=转折点口径，方向=腿向），回试 = 下一整腿。
3. 信号口径与旧版同构（enter/leave 同向 is_beichi → is_qs 出一类；leave+retest 不破核心出三类），仅判定单位从单根线段升格为次级别走势段。
4. 测试：6 个新单测替换旧 4 个（整腿对齐 3buy/3sell、回试破核心不出、右边缘无信号、**腿中子段 3sell**（旧口径不可表达的结构）、腿中子段背驰 qs→1buy）；tests/core 364 passed。

信号基线 diff（10 标的，scripts/signal_baseline_diff.py 增 freq 参数）：
1. **5m 周期：零变动**（升级链=tongjibie 不走 kuozhan，安全网确认）。
2. **1m 周期：L0 买卖点、L1 中枢序列、L2(30m) 全部零变动**；仅 L1(5m) bsp/bcs 变化——**lv1 买卖点 88→37（-58%）、背驰 26→22**。方向：旧单线段口径信号过密（单根 1m 线段当回试窗口，几乎每个微小回调都判"回试不破"），段粒度后收敛到摆动腿转折点锚定；与 30m 的 v31 修复对称（30m 是恒空→有，5m 是过密→正常，错配的两种表现）。

recursive_bt 影响评估（MTF3 300 分层样本，同窗同参重跑）：
| 口径 | 收益 | 回撤 | 夏普 | 交易 |
| --- | ---: | ---: | ---: | ---: |
| 修复前（§74 基线） | +66.11% | 3.41% | 6.24 | 3504 |
| **修复后（V3 fix）** | **+66.1%** | **3.4%** | **6.24** | **3503** |

**实质等价（仅差 1 笔）**：MTF3 策略的 mid/big 门控走独立周期 K 线（`mid_dir_at`/30m 自身），对 1m 周期 L1 kuozhan 买卖点依赖极小——V3 修复对实盘链路零扰动，改善集中在 1m 图 L1(5m) 信号质量与未来基于该级别信号的策略。全量 `852 passed, 1 skipped`。

其他：chart_cache schema v34→**v35**（1m 图 recursive_levels L1 bsp/bcs 内容变化，强制旧缓存失效）；audit_worknotes V3 条目改已修；观察记录：相邻 L1 中枢共享同一离开/回试腿时产生同锚重复信号（tongjibie 同构现象，下游 bar 级幂等，暂不去重）。发现并重启 US live_monitor（20:00:47 后静默退出，err.log 无报错，原因待察——重启时间见下）；live_monitor a/us 与 web 均以新代码重启。

## 78. 第六十七轮 TSLA 单标专项：一年回测 + 弱市 3 买过滤证伪 + regime 乘数 0.0 bug 修复

2026-06-11 应用户「小资金不分散、只买 TSLA」需求，跑 TSLA 单标满仓（max_pos=1，US 实盘默认 require=tech,nest_soft,trend3_boost，1m+5m+30m soft 门控）。长桥（ExchangeChangQiao）可拉满一年 1m（97,340 根/55s），构建专用缓存 `D:/chanlun_pro/chart_cache_us_tsla_1y`（1m/5m/30m，version v33）。

**一年回测（2025-06-11~2026-06-10）**：

| | 策略 | 裸持 TSLA |
| --- | ---: | ---: |
| 收益 | **+97.4%** | +15.5% |
| 最大回撤 | **7.7%** | 32.3% |
| 夏普 | 4.12 | — |
| 胜率 / 交易 | 74% / 149 笔 | — |

裸持 TSLA 全年仅 +15.5%（剧烈震荡、回撤 32%），系统靠买卖点把同票做到 +97.4% 且回撤压到 1/4——收益来自波动中反复进出，非趋势。买点贡献：3 买 114 笔/+81.7pp（主力）、1 买 24 笔/100% 胜率/+10.0pp、2 买 11 笔/+3.1pp。分季：2025Q4 最佳(+35.8pp)，2026Q1 弱市仅 8 笔/+1.6pp（系统下跌市自动收缩，与 §65 Q1 2 月窗口 +0.7% 同向）。

**弱市 3 买过滤假设（证伪）**：基于 §65 Q1 窗口「弱市 3 买衰减」提出「30m 走熊时只接 1 买、3 买减半/跳过」。对照（`--regime-bs-ratio-multipliers-json`，bear 判定=20 日跌幅≤-5% 或回撤≤-10%，单标用 TSLA 自身曲线）：

| 配置 | 收益 | 回撤 | 夏普 | 3买笔数 | Δ收益 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 基线（不过滤） | +97.4% | 7.7% | 4.12 | 114 | — |
| bear 减半 3买 | +73.8% | 7.9% | 3.98 | 114（半仓） | -23.5pp |
| bear 跳过 3买 | +55.0% | 9.8% | 3.37 | 83（-31笔） | -42.4pp |

**结论：过滤在每个维度都更差（收益↓、回撤↑、夏普↓），假设被全年数据决定性证伪**。根因：20 日 regime 分类器把 TSLA 全年 251 天里 **127 天标成 bear**（TSLA 高波动→频繁触发回撤阈值），而基线在 bear 区间反而赚 +16.7%——bear 区间 33 笔 3 买是 **73% 胜率/+27.7pp** 的好交易（买的是会反弹的回撤，非 Q1 式单边下跌）。§65 的「弱市 3 买衰减」是 2 个月单边下跌窗口的小样本特例，不能外推到「凡 bear 即砍 3 买」。**教训：粗粒度 20 日 regime 过滤对高 beta 标的太钝，会误杀大量可反弹回撤的买点；后续若做弱市防御应用更细的级别判据（30m 中枢破位/趋势背驰确认）而非日线动量阈值**。

**附带修复真实 bug（commit 见下）**：`portfolio._apply_buy_ratio_multiplier` 的 `multipliers.get(cls, 1.0) or 1.0` 把显式 `0.0` 当 falsy→还原成 `1.0`，使 `{"bear": {"3": 0.0}}` 静默失效（首轮 skip3 跑出与基线完全相同的假结果才暴露）。修为 `raw = get(cls); 1.0 if raw is None else float(raw)`，仅缺失键/None 兜底。RED 测试 `test_apply_buy_ratio_multiplier_honors_explicit_zero` 先证伪后修复；全量 853 passed。不改任何已采纳配置（A 股/US 候选乘数均 0.25~1.40，从未用 0.0）。报告：`D:/chanlun_pro/reports/us_tsla_only_1y{,_skip3,_halve3}_{summary.json,trades.csv}`。

## 79. 第六十八轮 ★未来函数审计：预算信号回测 vs 真·walk-forward（重大方法论发现）

2026-06-11 用户质疑「缠论买卖点有滞后性，回测是否含未来函数」。**逐行核对确认：现有所有 recursive_bt 回测的入场买卖点是「全序列 CL 一次算好」（`collect_branch_signals` on full series），按 anchor 分型日期触发——含未来函数（缠论右边缘分型/笔会重绘，预算口径用的是事后才稳定下来的信号集）**。仅 30m 方向门控是真·walk-forward（`wf_dir_series` 逐根增量）。已做对的：无「当根 lookahead」（信号当根收盘确认、下一根开盘成交，`portfolio.py:571`）。

**量化偏差（同一执行模型，仅信号时点不同）**：

第一层——简化验证（无门控、见信号即动作=幻影上界，`validate.py` + `scripts/wf_validate_tsla.py`）：

| 标的/窗口 | 预算 | 真 walk-forward | 差 | 交易数 |
| --- | ---: | ---: | ---: | --- |
| TSLA 5m / 1.5 月 | +14.7% | +7.0% | -7.6pp | 23→**202（9×）** |
| A股 10 只 5m / 3 月 | +0.9% | -1.0% | -1.9pp | ~12→~50（4-8×） |

第二层——**完整策略真·walk-forward**（`scripts/wf_backtest_tsla.py`：5m 操作级 + 30m 门控 + 买入比例，max_pos=1，T+0，信号首次出现即动作含幻影，绝对无未来函数）TSLA 一年：

| 口径 | 收益 | 回撤 | 交易 |
| --- | ---: | ---: | ---: |
| 裸持 TSLA | +20.8% | — | — |
| 预算（含未来函数） | **+31.9%** | 7.1% | 42 |
| **真 walk-forward（无未来函数）** | **+1.4%** | **19.1%** | 60 |
| 差 | **-30.5pp** | +12pp | +18 |

**结论（重大，固化）**：
1. **未来函数把 TSLA 回测从「+31.9%/低回撤」灌水成假优秀；真实盘口径仅 +1.4%、回撤暴增到 19.1%（接近裸持），且跑输裸持（+20.8%）**。§78 的 +97.4% 是 1m MTF3 含同种未来函数的产物，没做 1m 逐根 walk-forward（一年 97k×3 级算力不现实），但 1m 幻影只会更多、缩水只会更大——**+97.4% 不可信，不能作为实盘预期**。
2. **根因=右边缘幻影**：实时不断冒出买卖点、照做，但很多后来重绘消失（简化验证交易数 9×、完整版 1.4×因门控滤掉部分）。预算回测只「看见」最终稳定信号→显得又准又少。回撤的恶化比收益更致命（风控在 hindsight 下成立、实时失效）。
3. **TSLA 偏差远大于 A 股**（-30pp vs -1.9pp/季）：高 beta 标的右边缘噪声极大，缠论分型在剧烈波动里反复重绘。**单标集中高波动票 + 缠论分钟级信号 = 未来函数放大器**。
4. 已做对的部分仍有效：30m 门控（wf）、无当根 lookahead、T+1/涨跌停/费用建模。问题专属于**入场买卖点的信号时点**。

**修复方向（下一轮）**：把回测信号链从「全序列预算」改为「逐根 walk-forward / 信号首次稳定出现才触发」（接受确认滞后换无未来函数），或对买卖点加「持稳 N 根才动作」的确认层（降幻影、换滞后）。在此之前，**所有历史轮次的绝对收益数字（§22 起全部）须视为含未来函数的乐观上界，仅相对/同口径对照可用，绝对值不可外推实盘**。`live_monitor` 实时链路本身是逐根的（无此问题），paper ledger 是最终验收。新增脚本 `scripts/wf_validate_tsla.py`、`scripts/wf_backtest_tsla.py`。

## 第六十八轮 监控扫描增量化与报告节流(首晚美股实战驱动)

2026-06-11 美股 21:30 开盘后的首晚实战验证暴露真实时效缺陷并当晚修复(commit 0a0a14d6 + 44faaf09)。

**发现**:监控/纸面闭环功能健康(逐轮扫描、快照落账、状态行完整),但扫描周期 ~14 分钟,远超 1m 操作级——9 标的每轮全量拉 365 天 1m K 线(~10 万根/标的),CL 喂入虽是增量,拉取窗口却是全量;1m 买点信号会延误十几分钟,实盘意义大打折扣。

**修复**:
1. `MonitorSymbolState._fetch_klines`:首轮全量 warmup;有锚点后只拉锚点回退 5 天的尾部窗口(覆盖节假日/停牌缓冲),op/mid/big/daily 四级别统一;不支持 `start_date` 的数据源 TypeError 安全回退全量;`min_bars` 健全性检查只对首轮生效;昨收判定不受影响(5 天尾窗必含前一交易日尾根)。
2. 优化报告刷新时间节流 `--optimization-report-min-interval`(默认 600s):全套报告每轮重写耗时 ~30-50s 而新鲜度只需分钟级;首轮与 `--once` 模式总是刷新。

**实测效果**:扫描周期 14 分钟 → **~105 秒**(增量拉取,8 倍),报告节流后预期再降至 ~60-70s,接近 1m bar 对齐节奏。测试新增:增量尾窗调用断言、不支持 start_date 的回退、节流行为;全量 `856 passed, 1 skipped`。

**教训**:回测一致性(§74-76)之外,实盘还有第二类一致性——**时效一致性**:操作级别是 1m,扫描-通知链路的端到端延迟必须与 bar 周期同量级,否则"信号正确但晚到"等效于另一种回测-实盘偏差。首晚实战观察(事件=0)符合预期:US core9 在 1m+5m+30m 联立+30m 风控下本来低频,需多个交易日积累 paper 证据。

## 80. 第六十九轮 TSLA 真·实盘体系（goal 重设 TSLA 专项）——确认层扫描 + 幻影结构 + 区间套式确认

goal 重设为 TSLA 专项（原文深读/30m 同级别架构/三周期矩阵均已有，增量=无未来函数地基上构建 TSLA 实盘体系）。前置：TSLA 三周期矩阵数据级 PASS（`scripts/verify_matrix_chartdata_tsla.py`：1m 图 L0=119 中枢/L1(5m)=52 中枢+23 买卖点+10 背驰/L2(30m)=2 中枢；5m 图 L0+L1(30m)；30m 图 L0——与 A 股矩阵同构）。

**确认层扫描**（`scripts/wf_confirm_scan_tsla.py` 两阶段：①5m 逐根尾喂一次(~35min)记录全部信号生命周期 episode[first_seen, alive_until]，中途消失再现=新 episode；②离线 replay 任意确认参数不再重算）。TSLA 一年（裸持 +20.8%）：

| 配置（真 wf） | 收益 | 回撤 | 笔 |
| --- | ---: | ---: | ---: |
| N=0 见信号即动（§79 基线） | +1.4% | 19.1% | 60 |
| N_buy=12（盲等 60 分钟） | +8.1% | 10.0% | 50 |
| **N_buy=12 + 3 卖不卖（卖类={1,2}+门控兜底）** | **+9.6%** | **10.2%** | 50 |

大网格（`wf_replay_grid_tsla.py`，224 配置：N×买类×卖类×门控）：N>12 衰减（16/24/36 更差）、买类维度无增量（1/2 买 episode 太少）、门控 up-only 零差异、卖类={1,2} 略优。**所有配置评分（收益-2×回撤）仍为负——确认层有效但有天花板**。

**幻影结构**（`analyze_episodes_tsla.py`，448 episodes）：三类买卖点占 96%（3buy 196+3sell 234），**60% 活不过 12 根 5m**——预算回测虚高的物理基础；速死(<2 根)仅 ~10%，大量 4-12 根中寿命幻影——解释 N=1~4 反而最差（躲过速死、被中寿命幻影骗进且担滞后）。1sell 8 个全部存活到数据尾（最终稳定信号）。

**用户提出区间套级联确认（理论正确，原文判据全支持）**：5m 中枢由 1m 走势类型构成→1m 走势类型由 1m 中枢构成→1m 中枢由线段构成——本级别信号的确认应=**次级别结构完成**，而非本级别 bar 数盲等。原文：61 课 L33017「观察内部结构…逐次下去…在当下精确地定位转折点」、27 课 L17055 区间套定理、29 课 L20110「5 分钟背驰段考察 1 分钟以下级别精确定位」、43 课 L26551 跨级别背驰点充当本级别分解分界。**N=12 盲等是反原文的（纯时间不看结构）**；级联=系统 `_UPGRADE_CHAIN` 递归构造的天然能力（1m 图 L1(5m) 信号的重绘步长=1m bar）。

实验中：方案 B=5m 信号+1m 局部结构确认 vs 盲等 K 根 1m 对照（`wf_nest_confirm_tsla.py`，结果见 §84）；方案 A=1m 递归级别 L1(5m) 信号直接 wf（`wf_recursive_levels_1m_tsla.py`，结果见 §85）；缠论原生程式对比（`wf_chanlun_native_tsla.py`：30m 笔方向跟随/38 课程式 30m/5m）与 QQQ 全年同框架重算（防过拟合第二标的）并行跑。

### §80 结果补全（2026-06-12 定稿，方案 B 因 1m 全年 O(n²) 计算过重未完待补，用户指示不再等待）

**QQQ 交叉验证（确认层，QQQ 裸持 +32.5%）**：最优 N_buy=3（+18.3%/DD3.2%），N=12（TSLA 最优）在 QQQ 是最差区（+7.3%）——**「盲等 N 根」最优值两标的完全相反，固定时间确认=标的特异过拟合参数，证伪其通用性**。这从反面支持区间套结构确认方向（自适应、无固定参数）。

**缠论原生程式对比（wf_chanlun_native_tsla.py，事件流 pkl 缓存可秒级 replay）**：

| 真·wf 策略 | TSLA（裸持+20.8%） | QQQ（裸持+32.5%） |
| --- | ---: | ---: |
| 30m 笔方向跟随 | -59.6%/DD60.2%/160笔 | -18.9%/DD20.4% |
| 38课程式@30m | -14.9%/DD38.0% | **+11.7%/DD12.3%** |
| **38课程式@5m** | **+25.1%/DD23.4%/253笔/47%胜率** | +5.3%/DD12.1% |
| 程式@5m+30m门控(not_down/up) | +3.9%/+10.7%（DD14.8%） | +0.6%/+0.7%（DD5.8%） |

**第六十九轮定稿结论**：
1. **38课程式@5m 是 TSLA 唯一跑赢裸持的真·wf 策略**（+25.1% vs +20.8%，回撤 23.4% vs ~32%），且零拟合参数（「向下段不破前低买/向上段不创新高卖」两条规则）——TSLA 实盘收益线推荐。稳健线=确认层 N=12+卖类{1,2}（+9.6%/DD10.2%）。
2. **30m 门控对程式是净伤害（双标的一致）**：程式 alpha 来自「向下段不破前低→买」的左侧低吸，天然落在 30m down/neutral 时刻，门控恰好拦掉最好的逆势买点（与§78 bear 区间 3 买是好交易同构）。**对高波动单标，任何大级别方向过滤都在误杀均值回归买点；压回撤的正道=仓位管理（31课）或更准出场（程式补背驰先卖分支），非入场过滤**。
3. **没有任何固定配置跨标的同时跑赢裸持**：TSLA 最优=5m 程式、QQQ 最优=30m 程式；确认层 N 同样相反。规律符合原文（操作级别按标的波动节奏选择，31课）——**「最通用」的诚实答案=方法论层（无未来函数验证框架+按标的选操作级别+程式操作+区间套结构确认方向），而非任何参数组合**。
4. 方向跟随双标的证伪（高波动绞肉机）。
5. 新脚本：wf_confirm_scan_tsla（两阶段 episode 框架，--dir/--prefix/--tag 参数化）、wf_replay_grid_tsla（离线网格）、analyze_episodes_tsla（幻影结构）、wf_chanlun_native_tsla（原生程式+门控变体+事件缓存）、wf_nest_confirm_tsla（1m 局部结构确认，§84 已补测）、verify_matrix_chartdata_tsla（TSLA 三周期矩阵 PASS）。
6. 待办（下轮）：①方案 B 结果落盘后补记；②38课程式补「背驰先卖」分支（TSLA 回撤最对症改善）；③方案 A（1m 递归级别信号 wf）需先优化 wf_dir_series 的 O(n²) 复制（get_bis 每根全列表复制）；④程式+仓位管理（31课）压回撤实验。

## 81. 第七十轮 统一回测入口改为真 walk-forward 信号链

2026-06-12 继续响应用户「绝对不允许未来信号」要求，把 §79 的方法论审计落到统一 `live_backtest` 入口，不再只停留在专项脚本。

**实现**：
1. `live_backtest.build_symbol_from_klines` 新增 `signal_mode`，默认 `walk_forward`。`batch` 仍保留为显式对照模式，含义是「全序列算完后回填」，不能作为实盘结论。
2. `walk_forward` 模式每根主图 K 线只向 `CL` 喂入当时已经收盘且可见的 K 线；操作级信号 0 延迟，高一级/中级别信号按完整周期收盘延迟后才进入主时钟。
3. warmup 期只建立结构，不发交易信号；warmup 后只发「当前可见快照中新出现」的买卖点。右边缘信号消失后再出现，视为新的实盘可见 episode。
4. `--source bt_data --signal-mode walk_forward` 直接拒绝，因为 `bt_data` 是预计算信号缓存，无法证明无未来。`bt_data` 老缓存仅能走 `batch`/历史兼容。
5. CLI 默认 `--signal-mode walk_forward`，summary 写入 `signal_mode` 与 `signal_warmup_bars`，防止未来报告口径混淆。

**TSLA 可复现实验入口**：新增 `scripts/tsla_live_walk_forward_replay.py`，只读取 chart cache 原始 K 线，切出「预热窗口 + 交易窗口」后调用统一入口。默认 45 天交易窗口、30 天预热、5m+30m、max_pos=1；`--compare-batch` 可同窗跑旧 batch 污染对照。

本地复现（`D:/chanlun_pro/chart_cache`，TSLA.US，5m+30m）：

| 窗口 | 口径 | 收益 | 裸持 | 回撤 | 交易 |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-04-27~2026-06-10 | walk_forward | +7.9% | +3.7% | 5.5% | 3 |
| 2026-06-05~2026-06-10 | walk_forward 短窗烟测 | +0.0% | -3.9% | 0.0% | 0 |
| 2026-06-05~2026-06-10 | batch 对照 | +0.0% | -3.9% | 0.0% | 0 |

报告文件：
- `D:/chanlun_pro/reports/tsla_5m_30m_walk_forward_report.json`
- `D:/chanlun_pro/reports/tsla_5m_30m_walk_forward_summary.json`
- `D:/chanlun_pro/reports/tsla_5m_30m_walk_forward_trades.csv`

**新增测试与验证**：
1. `test_build_symbol_walk_forward_uses_visible_new_signals`：操作级信号只在 warmup 后首次可见时发出。
2. `test_build_symbol_walk_forward_delays_higher_level_to_main_clock`：5m 高一级信号必须等 5m bar 收完并映射到主图时钟后才可见。
3. `test_live_backtest_rejects_walk_forward_signal_mode_on_bt_data`：预计算缓存不能冒充无未来回测。
4. `test_live_backtest_cli_defaults_to_walk_forward_signal_mode`：CLI 默认锁为实盘口径。
5. `test_original_level_ladder_contract_uses_30m_same_level_decomposition`：1m→5m 用非同级别 `kuozhan`，5m/1m 升 30m 用同级别 `tongjibie`，30m 不再升级。

验证结果：`tests/test_backtest_live_parity.py tests/core/test_cl_incremental_equivalence.py tests/core/test_bs_point_incremental.py` 为 `73 passed`；`tests/core/test_cl_data_to_tv_chart_zs.py tests/core/test_zs_upgrade.py tests/core/test_zslx_fixture_regression.py` 为 `42 passed, 1 skipped`；`scripts/tsla_live_walk_forward_replay.py` 语法检查、help、短窗对照、默认 TSLA 近期窗口均通过。

**当前结论更新**：统一回测入口从本轮起可直接产生无未来信号结果；所有旧 `batch` 历史收益仍只作为乐观上界或同口径对照。TSLA 下一步不再纠结“有没有未来函数”这一地基问题，而应继续优化：1m 递归级别 wf 性能、38 课程式背驰先卖分支、以及 31 课仓位管理来压 TSLA 回撤。

## 82. 第七十一轮 原文资金分层实验：多重赋格只能改变风险档位，不是免费 alpha

原文依据来自 §3/§5 笔记：小级别进入后可换挡到大级别，并「部分保持小级别操作」；大资金可做 N 重层次操作，每层对应独立资金/筹码与节奏（`topic3_tongjibie_feitongjibie.md` L99-L103）。背驰卖出也按操作级别决定仓位：≤5m 可全清，大级别可先出部分；1m 短差资金不应追高，机动资金可只占 1/4-1/3（`topic5_beichi_qujiantao.md` L179/L251/L342）。本轮把这套思想落成可复现实验，而不是人工解释收益曲线。

**实现**：`scripts/wf_chanlun_native_tsla.py --sleeve-grid` 新增三账本 replay：
1. `core_fraction`：被动核心仓，开局买入并持有到期；
2. `active_fraction`：5m 38 课程式活动仓，仍按事件收盘确认、下一根 5m 开盘成交；
3. `idle_fraction`：现金，完全不参与交易。

三层资金互不借用，不允许杠杆；输出写入 `D:/chanlun_pro/reports/wf_native_tsla_sleeve_grid.json`。评分临时采用 `收益 - 2×回撤`，目的是观察“低回撤优先”的风险档位。

| TSLA 真 wf 资金组合 | 收益 | 回撤 | 笔 | 备注 |
| --- | ---: | ---: | ---: | --- |
| active 100% | +25.1% | 23.4% | 253 | 最高收益，仍是唯一明确跑赢裸持的原生程式线 |
| active 75% + cash 25% | +18.8% | 18.7% | 253 | 接近线性降风险，收益低于裸持 |
| active 33% + cash 67% | +8.3% | 9.3% | 253 | 低回撤档 |
| active 25% + cash 75% | +6.3% | 7.2% | 253 | `收益-2×回撤` 网格最优，但收益明显牺牲 |
| core 25% + cash 75% | +5.2% | 11.0% | 0 | 被动 beta 回撤已经高于 25% 活动仓 |
| core 25% + active 25% + cash 50% | +11.5% | 16.2% | 253 | 加核心仓提高收益，但回撤上升更快 |
| core 50% + active 50% | +23.0% | 27.4% | 253 | 收益接近 active 100%，回撤更差 |
| core 100% | +21.0% | 32.2% | 0 | 全期裸持口径；脚本主输出 warmup 裸持为 +20.8% |

**结论**：
1. 31 课式资金分层在 TSLA 上有效地提供“风险档位”：25%-33% 活动仓能把回撤压到 7%-9%，但不是高收益系统。
2. 被动核心仓不是 TSLA 的低回撤答案；它把单标的 beta 回撤带回来，和活动仓组合后多数情况下 `收益/回撤` 变差。
3. 若目标是“跑赢裸持”，当前仍只能选择 5m 38 课程式 active 100%；若目标是“低回撤实盘观察”，应选择 active 25%-33% 小仓运行。
4. 因此，下一步不能靠简单降仓位解决“回撤低、收益高”的矛盾；真正需要补的是原文中更靠前的出场机制：区间套背驰定位、C 段力度衰竭先卖、以及小转大失败后的二/三卖退出。

## 83. 第七十二轮 背驰/卖点提前退出实验：低级别卖点不能无上下文压到持仓

本轮验证 §82 的下一步假设：既然 5m 38 课程式的主要问题是回撤，是否可以把原文中的「有卖点就卖」「背驰点先卖」直接接到持仓退出上？

**实现**：新增 `scripts/wf_native_branch_exit_tsla.py`。入口仍用 `wf_seg38_series(5m)` 的 38 课程式买入/卖出事件；额外退出事件来自 `wf_confirm_scan_tsla.py` 的 stage1 episode 缓存。stage1 是逐根 5m 增量计算 `collect_branch_signals` 得到的「首次可见」信号生命周期，因此 replay 只使用：
1. `first_seen`：该卖点在实盘当根收盘首次可见；
2. `alive_until`：若要求 `n_sell=1`，必须下一根仍存在才触发；
3. 触发后仍按下一根 5m 开盘成交。

也就是说，这不是全量预计算卖点回填，而是严格复用前轮无未来 episode 证据。测试组合包括：只接 1/2 类卖点、接全部卖点、只接 3 卖，并用 TSLA/QQQ 双标的验证。

| 策略 | TSLA 收益/回撤 | QQQ 收益/回撤 | 结论 |
| --- | ---: | ---: | --- |
| 5m 38 课程式原版 | +25.1% / 23.4% | +5.3% / 12.1% | 基线 |
| + 1/2 卖，n=0 | +24.1% / 23.5% | +3.4% / 12.1% | TSLA 略降，QQQ 明显降 |
| + 1/2 卖，n=1 | +25.1% / 23.3% | +3.1% / 12.1% | TSLA 仅微小改善，QQQ 变差 |
| + 3 卖，n=0 | -2.8% / 27.7% | +5.5% / 10.9% | 标的特异；TSLA 灾难，QQQ 小幅好 |
| + 全部卖点，n=0 | -3.6% / 28.0% | +3.3% / 10.9% | 低级别卖点过度干预 |
| + 全部卖点，n=1 | -3.6% / 30.3% | +0.1% / 11.3% | 双标的均差 |

报告文件：
- `D:/chanlun_pro/reports/wf_native_tsla_branch_exit.json`
- `D:/chanlun_pro/reports/wf_native_qqq_branch_exit.json`

**结论更新**：
1. 「级联更早信号」方向仍正确，但不能把低级别任意卖点无条件压到一个 5m 38 课程式持仓上。这样会把不同节奏的分解混在一起，正是原文多重赋格反复提醒要避免的事：每层资金与节奏独立。
2. 1/2 类卖点作为提前退出，在 TSLA 上只给出 `23.4%→23.3%` 的微弱回撤改善，在 QQQ 上反而伤害收益；证据不足以采纳为通用规则。
3. 3 卖退出在 QQQ 有一点帮助，但在 TSLA 直接破坏 alpha，说明它需要结合「所锚定中枢是否属于本持仓操作级别」「是否处于最后中枢离开/回试链条」「30m 同级别环境」过滤，而不是当作普通止盈止损。
4. 下一步应从“卖点事件本身”升级到“卖点所属结构上下文”：只在 38 课程式持仓进入后的同一 5m 上行段/最后中枢 C 段中，识别力度衰竭和区间套背驰；否则低级别卖点只是另一层资金的独立操作信号。

## 84. 第七十三轮 5m 信号的 1m 级联确认补测：结构确认有效但尚非通用最优

本轮补齐 §80 留下的「方案 B」：5m 信号首次可见后，不再盲等本级别 N 根，而是下钻到 1m 观察次级别局部转折。旧 `wf_nest_confirm_tsla.py` 有两个问题：一是原计划用完整 1m CL 笔方向，全年现算仍过重；二是旧脚本里关键 `if dir1[i] == "up"` 被编码/注释破坏，实际退化成「下一根 1m 就买」。本轮已重写为干净 ASCII 脚本，并明确标注当前口径为 `confirm_source=local_raw_1m_fx_stack`。

**实现口径**：
1. 5m 信号仍来自 `wf_confirm_scan_tsla.py` 的 stage1 episode：逐根 5m 增量重算、记录 `first_seen/alive_until`，无未来信号。
2. 1m 确认不再跑完整 CL 全链路，而用 raw 1m 局部分型端点栈作为快速代理：当前 1m 方向为 up，或从非 up 翻成 up，即视为次级别局部转折确认。该代理只使用当前及以前 1m K 线；分型需右侧 1 根确认，因此仍有真实滞后。
3. 只在 5m 买点 episode 首见后的 `max_wait=90` 根 1m 窗口里查询方向；窗口外不重建结构。TSLA 全年只需查询 9450 个 1m bar，QQQ 为 12693 个。
4. 对照组为同一 1m 执行轴的盲等 `0/5/15/30/60` 根；买入确认时 5m episode 必须仍存活，卖点首见即动，30m gate down 强平。

| 确认方式 | TSLA 收益/回撤 | QQQ 收益/回撤 | 说明 |
| --- | ---: | ---: | --- |
| wait_0 | +9.6% / 15.5% | +15.5% / 4.3% | 见信号即动；TSLA 收益最佳但回撤高 |
| wait_15 | -8.6% / 22.0% | +17.3% / 3.5% | 固定时间参数跨标的冲突明显 |
| wait_60 | +7.2% / 10.7% | +7.3% / 4.4% | TSLA 低回撤较好，QQQ 衰减 |
| struct_already_up | +5.8% / 13.4% | +12.7% / 4.1% | 结构代理；跨标的中等稳定 |
| struct_flip_up | +1.7% / 14.3% | +12.9% / 4.0% | 更严格，TSLA 过度过滤 |

报告文件：
- `D:/chanlun_pro/reports/wf_nest_confirm_tsla.json`
- `D:/chanlun_pro/reports/wf_nest_confirm_qqq.json`

**结论**：
1. 用户提出的级联确认方向是对的：固定盲等 N 根依然表现为标的特异参数，TSLA/QQQ 的最优等待时间完全不同；结构确认虽然不是最优，但比多数固定等待更少出现灾难性错配。
2. 当前 `local_raw_1m_fx_stack` 只是「次级别结构代理」，还不是完整原文口径的 1m 走势类型→1m 中枢→线段链。它能说明“结构确认值得继续”，不能作为最终通用交易规则。
3. 下一步真正应该做的是方案 A：直接在 1m 图的递归升级层读取 L1(5m)/L2(30m) 买卖点与背驰的 walk-forward episode。这样 5m 信号天然由 1m 结构逐级生成，而不是先有 5m 信号再用代理过滤。
4. 性能教训：完整 1m CL 全链路逐根现算不能放在 replay 内临时跑，必须做成可缓存的增量 episode 生成器；否则会把研究阻塞在计算成本上。

## 85. 第七十四轮 方案 A：1m 递归 L1/L2 信号的真 walk-forward 回放

本轮把用户提出的“5m 中枢由 1m 走势类型构成、1m 走势类型由 1m 中枢/线段构成”的正统级联口径落到可运行脚本：新增 `scripts/wf_recursive_levels_1m_tsla.py`。

**实现口径**：
1. 只读取原始 1m K 线缓存，不读取任何预计算买卖点缓存。
2. 用单个 `CL(code, "1m")` 逐根喂入；每根 1m 收盘后调用 `collect_signals(cd)`，信号源即 `cd.get_kuozhan_levels()`。
3. L1=由 1m 递归出的 5m 级别，使用 `_UPGRADE_CHAIN["1m"][0] = ("5m", "kuozhan")`；L2=30m 级别，使用 `_UPGRADE_CHAIN["1m"][1] = ("30m", "tongjibie")`。
4. 只在当前快照中新出现的 L1/L2 买卖点触发；同一快照内重复锚点去重；右边缘信号消失后再次出现，视为新的实盘 episode。
5. warmup 期只更新 CL 状态与已知 L2 背景，不允许交易；交易信号在 1m 收盘可见，下一根 1m 开盘成交。

**TSLA 15 天窗口实测**（2026-05-26 16:00 ~ 2026-06-10 16:00，预热从 2026-04-27 13:30 开始，12240 根 1m，扫描 344s）：

| 方案 A 变体 | 收益 | 回撤 | 笔 | 信号事实 |
| --- | ---: | ---: | ---: | --- |
| L1 only | -1.0% | 3.6% | 1 | 交易窗仅 1 个 L1 3buy、5 个 L1 3sell |
| L1 + L2 not_down | -1.0% | 3.6% | 1 | L2 无新方向事件，等同 L1 only |
| L1 + L2 up | 0.0% | 0.0% | 0 | L2 未给 up 背景，严格过滤为空仓 |
| 裸持 | -9.9% | 未单列 | 0 | 同窗下跌环境，空仓过滤本身有价值但不是收益系统 |

报告文件：`D:/chanlun_pro/reports/wf_recursive_levels_tsla_15d_1m.json`。短窗烟测 `tsla_2d`、`tsla_5d` 均无递归 L1/L2 新交易信号，说明递归高一级信号天然稀疏。

**结论更新**：
1. 级联分析能解决“未来函数/先知回填”的问题，因为信号只在递归结构当下可见时触发；但它不能自动解决“滞后导致收益差”的问题。严格 L1/L2 递归信号比独立 5m 信号更干净，也更稀疏、更晚确认。
2. 用户的方向仍然正确：5m 买卖点应由 1m 内部结构确定，而不是固定等待 N 根。但交易体系不能只拿 L1/L2 高级别买卖点裸跑；它更适合作为结构背景、仓位换挡、以及过滤低级别程式的上下文。
3. TSLA 当前最可用主线仍是 `5m 38课程式`（§80/§82，收益高但回撤大）+ `结构确认/资金分层`（§82/§84，降低回撤）；方案 A 暂不替代主线，只证明了正统级联信号的无未来口径。
4. 性能上，1m 递归全链路仍不能作为全年网格优化内循环。下一步工程目标应是把 `wf_recursive_levels_1m_tsla.py` 的事件流做成可缓存 episode，并只增量刷新最新交易日；研究目标是把 L1/L2 信号接入 38 课程式的上下文过滤，而不是单独开仓。

## 86. 第七十五轮 递归 L1 作为 5m 课程式上下文：局部改善，机械退出仍伤害

本轮把 §85 的方案 A 事件流做成可复用缓存，并做第一次和 5m 38课程式的混合实盘回放。

**工程实现**：
1. `scripts/wf_recursive_levels_1m_tsla.py` 新增 `--event-cache/--force-rescan`。缓存绑定 `code/freq/首尾时间/交易起点/1m bars/signal_warmup_bars`，只有完全匹配才复用。
2. 用 `D:/chanlun_pro/reports/wf_recursive_levels_tsla_15d_1m.json` 初始化并验证缓存 `D:/chanlun_pro/reports/wf_recursive_levels_tsla_15d_1m_events.pkl`；同一窗口从 344 秒重扫降到约 2 秒缓存回放，交易结果不变。
3. 新增 `scripts/wf_native_recursive_context_tsla.py`：统一 1m 执行轴，把 5m 38课程式事件映射到 1m，递归 L1 事件按其 `visible_time` 映射到 1m；所有决策都在 bar 收盘后，下一根 1m 开盘成交。

**TSLA 同窗压力测试**（2026-05-26 16:00 ~ 2026-06-10 16:00，裸持 -9.9%，5m 课程式事件 107 个，递归 L1 事件 13 个/交易窗 6 个）：

| 混合方式 | 收益 | 回撤 | 笔 | 说明 |
| --- | ---: | ---: | ---: | --- |
| 5m 38课程式基线 | -3.1% | 8.0% | 10 | 下跌窗口里反复低吸，仍优于裸持但回撤大 |
| 基线 + L1 sell 机械退出 | -4.2% | 8.0% | 11 | 又一次证明“低级别卖点直接压持仓”伤害收益 |
| L1 上下文 not_down 过滤买入 + L1 sell 退出 | +1.3% | 1.8% | 2 | 阻断 13 次 down 背景买入，1 次递归 L1 sell 退出 |
| L1 上下文 up 才买 | 0.0% | 0.0% | 0 | 过严，直接空仓 |

报告文件：`D:/chanlun_pro/reports/wf_native_recursive_context_tsla_15d.json`。

**QQQ 同窗复验**（同样 2026-05-26 16:00 ~ 2026-06-10 16:00，裸持 -3.4%，递归扫描 12241 根 1m/339s，缓存回放约 2s）：

| 混合方式 | 收益 | 回撤 | 笔 | 说明 |
| --- | ---: | ---: | ---: | --- |
| 递归 L1 only | +1.3% | 2.1% | 2 | 交易窗 2 个 L1 buy、1 个 L1 sell，L2 仍无新方向 |
| 5m 38课程式基线 | -2.8% | 4.7% | 11 | 同窗下跌，课程式也受伤 |
| 基线 + L1 sell 机械退出 | -2.9% | 4.8% | 11 | 和 TSLA 一致，机械退出无增益 |
| L1 上下文 not_down 过滤买入 + L1 sell 退出 | +0.1% | 2.3% | 9 | 阻断 4 次 down 背景买入，回撤约减半 |
| L1 上下文 up 才买 | +0.1% | 2.3% | 9 | 本窗与 not_down 相同，仍需更长窗口区分 |

报告文件：
- `D:/chanlun_pro/reports/wf_recursive_levels_qqq_15d_1m.json`
- `D:/chanlun_pro/reports/wf_recursive_levels_qqq_15d_1m_events.pkl`
- `D:/chanlun_pro/reports/wf_native_recursive_context_qqq_15d.json`

**结论更新**：
1. 递归 L1 不适合当“有卖点就卖”的机械退出信号；这和 §83 的 branch sell 结论一致。
2. 递归 L1 作为状态上下文更有希望：`not_down` 不是追求 L1 已经 up，而是避免在 L1 明确 down 后继续执行 5m 课程式低吸。TSLA 把回撤从 8.0% 压到 1.8%、收益从 -3.1% 改为 +1.3%；QQQ 把回撤从 4.7% 压到 2.3%、收益从 -2.8% 改为 +0.1%。
3. 双标的 15 天压力窗支持“递归结构作上下文过滤”这一方向，但仍不能声明通用最优；样本窗口偏短，且两标的都处在下跌环境，可能偏向过滤型规则。
4. 这条路径更原文一致：每层资金独立，低级别递归结构用于“是否允许本层继续操作/是否降档”，而不是替代本层完整买卖程式。
5. 下一步应把 `seg38_l1_not_down` 做成年份级事件缓存回放；如果 TSLA+QQQ 全年仍降低回撤，再接入仓位分层（active 25/33/100%）做收益-回撤曲线。

## 87. 第七十六轮 年份级递归事件的分段缓存生成器

§86 之后的主要瓶颈不是交易逻辑，而是完整 1m 递归 L1/L2 的逐根扫描成本：15 天约 339~344 秒，直接全年单次跑不可维护。本轮新增 `scripts/wf_recursive_chunked_report.py`，把年份级事件生成拆成可恢复分段。

**实现口径**：
1. 每个 chunk 独立取 `chunk_start - warmup_days` 到 `chunk_end` 的 1m 原始 K 线，仍调用 `CL(1m).get_kuozhan_levels()` 逐根生成「当前可见」L1/L2 事件。
2. 只合并 `visible_time` 落在该 chunk 真实交易区间内的事件；warmup 事件只用于建立结构，不进入最终事件流。
3. 每个 chunk 写独立 pickle 缓存，key 绑定 `code/首尾时间/trade_start_idx/signal_warmup_bars`；重跑只做合并，不重扫。
4. 合并后的 report 含完整 `events`，可直接喂给 `wf_native_recursive_context_tsla.py`。同时 `wf_recursive_levels_1m_tsla.py` 也改为写完整 `events`，混合脚本若遇到旧 report 会从 `scan.event_cache` 兜底读取。

**烟测证据**：
1. TSLA 5 天窗口（2026-06-05 16:00 ~ 2026-06-10 16:00），按 3 天分 2 段、10 天预热：首次扫描约 40 秒，无递归事件；二次运行两段均 cache hit，约 2 秒完成，结果与此前 5 天单段无事件一致。
2. TSLA 2 天有事件窗口（2026-06-08 16:00 ~ 2026-06-10 16:00），30 天预热：首次扫描 129 秒，修正合并后保留 2 个交易窗口递归事件；二次运行 cache hit，约 2 秒完成。
3. 用 2 天 chunked report 跑混合回放：基线 5m 课程式 `-2.5%/DD7.8%/1笔`；机械 L1 sell 退出 `-3.7%/DD7.8%/1笔`，继续支持“低级别卖点不能机械压持仓”的结论。

**修复记录**：
烟测发现最后一个 chunk 的初版合并条件会把 warmup 内旧事件也纳入 `events all`，虽然交易层因 `in_trade_window=False` 没有使用它们，但报告计数会污染。已修正为：最后 chunk 也必须满足 `chunk_start <= visible_time <= chunk_end`，重跑后 `events all=2/trade=2`。

**下一步**：
年份级验证现在可以分段恢复，不再需要一次性长进程。后续应按 TSLA、QQQ 各自跑 `--window-days 365 --chunk-days 15 --warmup-days 30`，生成全年递归事件 report 后，再统一跑 `seg38_l1_not_down` 和资金分层组合。

## 88. 第七十七轮 年份级断点续跑控制与 partial 防误用

本轮把 §87 的分段生成器从“可缓存”推进到“可长期断点续跑”：

**实现**：
1. `scripts/wf_recursive_chunked_report.py` 新增 `--chunk-start-index/--chunk-end-index/--merge-only`。可以只扫描某一个或某一段 chunk；已有缓存自动合并，未缓存 chunk 跳过并记录为 `missing_chunks`。
2. 输出收敛：未选择、未命中的 chunk 不再刷屏；只打印缓存命中或实际扫描的 chunk。
3. `cache_misses` 改为实际扫描段数，避免把尚未选择的缺失 chunk 误算为本次 miss。
4. `scripts/wf_native_recursive_context_tsla.py` 新增原生 5m 课程式事件缓存读取：默认复用 `D:/chanlun_pro/reports/wf_native_{tag}_events.pkl` 的 `seg38_5m`，避免每次混合回放都现算全年 5m 程式。
5. 混合回放默认拒绝 `scan.is_partial=true` 的递归 report；只有显式 `--allow-partial-recursive` 才允许调试 partial 覆盖，并在 stdout/report 标出 `recursive_is_partial` 与缺失 chunk。防止把不完整递归上下文误当全年策略结论。

**TSLA 年份级 partial 进度**：
采用 `--window-days 335 --warmup-days 30 --chunk-days 5 --signal-warmup-bars 1200`。原因：当前 1m 缓存从 2025-06-10 16:00 开始，先用 30 天作为结构 warmup，交易窗从 2025-07-10 16:00 开始到 2026-06-10 16:00。

已完成并缓存：

| chunk | 交易区间 | 扫描耗时 | 合并事件 |
| --- | --- | ---: | ---: |
| 1 | 2025-07-10 16:00 ~ 2025-07-15 16:00 | 158s | 1 |
| 2 | 2025-07-15 16:00 ~ 2025-07-20 16:00 | 159s | 3 |

当前 partial report：
- `D:/chanlun_pro/reports/wf_recursive_levels_tsla_year_partial_chunked_1m.json`
- chunk 缓存目录：`D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_partial/`
- 已缓存 2/67 段，合并事件 4 个，`missing_chunks=3..67`，`is_partial=true`。

**调试回放**：
用 `--allow-partial-recursive` 跑了 partial 混合回放，原生 5m 课程式事件缓存命中（`wf_native_tsla_events.pkl`，893 个 5m 事件），递归事件仅 4 个。输出文件 `D:/chanlun_pro/reports/wf_native_recursive_context_tsla_year_partial_debug.json` 明确标记 `recursive_is_partial=true`。该结果只验证工程链路，不能作为交易结论；因为缺失 chunk 会让递归上下文在全年后续区间悬空。

**下一步运行方式**：
继续执行：
`python scripts/wf_recursive_chunked_report.py --dir D:/chanlun_pro/chart_cache_us_tsla_1y --prefix us_TSLA_US --tag tsla_year_partial --window-days 335 --warmup-days 30 --chunk-days 5 --signal-warmup-bars 1200 --chunk-start-index N --chunk-end-index N`

当所有 67 段缓存完成后，用 `--merge-only` 合并；只有 `missing_chunks=[]` 时，才允许把 report 作为全年递归上下文输入 `wf_native_recursive_context_tsla.py` 做正式策略结论。

## 89. 第七十八轮 年份级正式路线切换为 15 天 chunk，并开始 TSLA 全年缓存

§88 的 5 天 chunk 路线证明了断点续跑机制，但效率不合适：TSLA 每个 5 天 chunk 约 158~159 秒，全年 67 段重复 warmup 太多。结合 §85 的 15 天窗口实测（约 344 秒），正式年份级路线改为 15 天 chunk：全年 23 段，重复 warmup 成本明显更低。

**新增工具**：
`scripts/wf_recursive_chunked_report.py --status-only`。它按当前参数验证 chunk 缓存的元数据是否匹配，而不是只看文件名；输出 `wf_recursive_status_{tag}_chunked.json`，包含 cached/missing/selected_missing。

**TSLA 15 天年份方案**：
参数：

```powershell
$env:PYTHONPATH='src;web/chanlun_chart'
python scripts\wf_recursive_chunked_report.py `
  --dir D:/chanlun_pro/chart_cache_us_tsla_1y `
  --prefix us_TSLA_US `
  --tag tsla_year_15d `
  --window-days 335 `
  --warmup-days 30 `
  --chunk-days 15 `
  --signal-warmup-bars 1200 `
  --chunk-start-index N `
  --chunk-end-index N
```

当前进度（更新至第九十九轮）：

| chunk | 交易区间 | 扫描耗时 | 合并事件 |
| --- | --- | ---: | ---: |
| 1 | 2025-07-10 16:00 ~ 2025-07-25 16:00 | 344s | 8 |
| 2 | 2025-07-25 16:00 ~ 2025-08-09 16:00 | 358s | 2 |
| 3 | 2025-08-09 16:00 ~ 2025-08-24 16:00 | 326s | 5 |
| 4 | 2025-08-24 16:00 ~ 2025-09-08 16:00 | 303s | 3 |
| 5 | 2025-09-08 16:00 ~ 2025-09-23 16:00 | 308s | 4 |
| 6 | 2025-09-23 16:00 ~ 2025-10-08 16:00 | 359s | 4 |
| 7 | 2025-10-08 16:00 ~ 2025-10-23 16:00 | 402s | 0 |
| 8 | 2025-10-23 16:00 ~ 2025-11-07 16:00 | 371s | 0 |
| 9 | 2025-11-07 16:00 ~ 2025-11-22 16:00 | 342s | 0 |
| 10 | 2025-11-22 16:00 ~ 2025-12-07 16:00 | 303s | 0 |
| 11 | 2025-12-07 16:00 ~ 2025-12-22 16:00 | 279s | 2 |
| 12 | 2025-12-22 16:00 ~ 2026-01-06 16:00 | 263s | 8 |
| 13 | 2026-01-06 16:00 ~ 2026-01-21 16:00 | 268s | 7 |
| 14 | 2026-01-21 16:00 ~ 2026-02-05 16:00 | 310s | 4 |
| 15 | 2026-02-05 16:00 ~ 2026-02-20 16:00 | 327s | 11 |
| 16 | 2026-02-20 16:00 ~ 2026-03-07 16:00 | 331s | 3 |
| 17 | 2026-03-07 16:00 ~ 2026-03-22 16:00 | 308s | 0 |
| 18 | 2026-03-22 16:00 ~ 2026-04-06 16:00 | 312s | 3 |
| 19 | 2026-04-06 16:00 ~ 2026-04-21 16:00 | 306s | 0 |
| 20 | 2026-04-21 16:00 ~ 2026-05-06 16:00 | 312s | 4 |
| 21 | 2026-05-06 16:00 ~ 2026-05-21 16:00 | 356s | 2 |
| 22 | 2026-05-21 16:00 ~ 2026-06-05 16:00 | 352s | 6 |
| 23 | 2026-06-05 16:00 ~ 2026-06-10 16:00 | 175s | 3 |

当前文件：
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_001_202507101600_202507251600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_002_202507251600_202508091600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_003_202508091600_202508241600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_004_202508241600_202509081600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_005_202509081600_202509231600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_006_202509231600_202510081600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_007_202510081600_202510231600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_008_202510231600_202511071600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_009_202511071600_202511221600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_010_202511221600_202512071600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_011_202512071600_202512221600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_012_202512221600_202601061600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_013_202601061600_202601211600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_014_202601211600_202602051600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_015_202602051600_202602201600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_016_202602201600_202603071600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_017_202603071600_202603221600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_018_202603221600_202604061600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_019_202604061600_202604211600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_020_202604211600_202605061600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_021_202605061600_202605211600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_022_202605211600_202606051600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_chunks_tsla_year_15d/chunk_023_202606051600_202606101600.pkl`
- `D:/chanlun_pro/reports/wf_recursive_levels_tsla_year_15d_chunked_1m.json`
- `D:/chanlun_pro/reports/wf_recursive_status_tsla_year_15d_chunked.json`

状态：`cached=23/23`，最终合并事件 79 个，`missing_chunks=[]`，`is_partial=false`。`--merge-only --chunk-start-index 1 --chunk-end-index 23` 已验证所有缓存复用，年度递归上下文 report 可以作为正式策略输入。

年度递归 L1/L2 自身回放结果：

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 |
| --- | ---: | ---: | ---: | ---: |
| L1-only | +3.8% | 18.4% | 9 | 56% |
| L1/L2-not-down | +3.8% | 18.4% | 9 | 56% |
| L1/L2-up | 0.0% | 0.0% | 0 | 0% |

结论：递归 L1 事件本身能把交易频率压得很低，但单独作为开平仓系统收益不足；L2 在本段年度窗口没有提供有效放大信号。它更适合作为低级别实时上下文和过滤器，而不是直接替代 5m 操作级别程序。

**TSLA 年度正式混合回放**：

输入：
- 递归 report：`D:/chanlun_pro/reports/wf_recursive_levels_tsla_year_15d_chunked_1m.json`，`recursive_is_partial=false`。
- 原生 5m 课程 38 程式事件缓存：`D:/chanlun_pro/reports/wf_native_tsla_events.pkl`，命中 893 个 5m 事件。
- 递归 L1 可见事件：79 个，全部在交易窗口内。
- 执行轴：统一 1m；5m 缓存时间戳为 K 线起始时间，脚本先平移到该 5m 窗口的最后一根 1m bar 收盘可见，再由下一根 1m 开盘成交；递归事件按 `visible_time` 可见，下一根 1m 开盘成交。
- 对照：同窗口买入持有 `+27.1%`。

输出文件：`D:/chanlun_pro/reports/wf_native_recursive_context_tsla_year_15d.json`。

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 | L1 卖出 | 过滤买入 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| seg38_base | +39.7% | 23.4% | 225 | 48% | 0 | 0 |
| seg38_plus_l1_sell | +37.6% | 22.6% | 235 | 48% | 19 | 0 |
| seg38_l1_up | +21.3% | 7.8% | 39 | 49% | 2 | 326 |
| seg38_l1_not_down | +12.8% | 15.0% | 74 | 49% | 4 | 271 |

当前可用结论：
1. 2026-06-12 修正了 5m 起始时间戳投到 1m 主时钟时的可见时间：旧口径会把 5m 事件最多提前 4 分钟，新口径为严格无未来口径。
2. `seg38_base` 在严格口径下收益高于裸持，但 225 笔、23.4% 回撤，仍是高换手高波动活动仓，不适合作为无过滤默认满仓系统。
3. 只加 L1 卖出收益略降，说明低级别卖点不能无条件叠加；它更适合作为上下文退出条件，而不是单独增强器。
4. `seg38_l1_up` 是当前更稳的实盘候选：只在 1m 递归 L1 上下文为 `up` 时接受 5m 买点，把交易压到 39 笔，回撤降到 7.8%，收益仍有 +21.3%。
5. 该候选低于裸持与 `seg38_base` 的收益，但收益/回撤更平衡。下一步应继续用 QQQ 和更多 US core 标的验证，再决定是否把 `seg38_l1_up` 作为默认活动仓。

## 90. 第一百轮 TSLA 年度 `seg38_l1_up` 仓位分层

本轮把 §89 的年度候选 `seg38_l1_up` 接入 31 课资金分层思想：大级别核心仓、5m 操作活动仓、空闲现金分离。信号仍是同一套严格实盘式事件流：

1. 递归 L1 信号按 `visible_time` 可见，下一根 1m 开盘成交；
2. 原生 5m 38 课程式事件按 5m 收盘可见，下一根 1m 开盘成交；
3. 核心仓只在交易窗口起点 `2025-07-10 16:00:00` 的 1m 开盘买入，不参与后续择时；
4. 活动仓只执行 `seg38_l1_up` 的买卖，空闲资金保持现金。

实现：`scripts/wf_native_recursive_context_tsla.py` 新增 `--sleeve-grid` 与 `--sleeve-variant`。输出文件：`D:/chanlun_pro/reports/wf_native_recursive_context_tsla_year_15d_sleeves.json`。

验证约束：年度递归 report 内本次只有 79 个 L1(5m) 可见事件，L2(30m) 事件数为 0。因此 30m 同级别分解在这个窗口没有形成可操作买卖点，不能人为制造 30m 过滤信号；当前只把 30m 作为“无事件不放大”的事实记录。后续需要延长样本或改进 L2 提取后，再把 30m 同级别状态接入活动仓上限。

关键仓位结果：

| 仓位 | 收益 | 最大回撤 | 交易数 | 胜率 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| active 25% + cash 75% | +5.3% | 2.2% | 39 | 49% | 低回撤观察档 |
| active 33% + cash 67% | +7.0% | 2.9% | 39 | 49% | 低回撤观察档 |
| active 50% + cash 50% | +10.7% | 4.2% | 39 | 49% | 中性活动仓 |
| active 75% + cash 25% | +16.0% | 6.1% | 39 | 49% | 收益/回撤较稳 |
| active 100% | +21.3% | 7.8% | 39 | 49% | 当前活动仓候选 |
| core 25% + active 75% | +22.8% | 11.4% | 39 | 49% | 收益略高但回撤抬升 |
| core 33% + active 67% | +23.2% | 14.3% | 39 | 49% | 接近裸持收益，回撤仍低于裸持 |
| core 50% + active 50% | +24.2% | 19.8% | 39 | 49% | 回撤偏大 |
| core 75% + active 25% | +25.7% | 26.6% | 39 | 49% | 主要收益来自裸持核心仓 |
| core 100% | +27.1% | 32.3% | 0 | 0% | 裸持对照 |

当前仓位结论：

1. 如果以 `收益 - 2×回撤` 排名，`active 100%` 当前最优，收益 +21.3%、回撤 7.8%，明显优于旧时间口径下的仓位结论。
2. 纯活动仓从 25% 到 100% 是一条平滑风险曲线；其中 75% 活动仓 `+16.0%/DD6.1%`，适合作为更保守的实盘观察档。
3. 加核心仓能提高收益但回撤抬升更快；`core 33% + active 67%` 接近裸持收益，但回撤已到 14.3%，不再是低回撤体系。
4. 当前 TSLA 单标的候选为 `active 75%~100%`。下一轮必须继续补两件事：一是更精确的退出机制（区间套背驰、小转大失败、二/三卖），二是选股/标的过滤（把同一套严格无未来流程跑到 QQQ 与更多 US core 标的）。

## 91. 第一百零一轮 QQQ 年度严格无未来验证结论

为避免 `seg38_l1_up` 只是在 TSLA 单标的上调出的偶然结果，本轮把完全相同的实盘式链路迁移到 QQQ，并且只接受完整年度递归 report：

1. 数据：`D:/chanlun_pro/chart_cache_us_qqq_1y`，1m/5m/30m 均覆盖 `2025-06-10 16:00:00 ~ 2026-06-10 16:00:00`。
2. 参数与 TSLA 年度正式路线一致：交易窗 `2025-07-10 16:00:00 ~ 2026-06-10 16:00:00`，`warmup-days=30`，`chunk-days=15`，`signal-warmup-bars=1200`。
3. 递归工具：`scripts/wf_recursive_chunked_report.py --tag qqq_year_15d --merge-only --chunk-start-index 1 --chunk-end-index 23`。
4. 混合工具：`scripts/wf_native_recursive_context_tsla.py`，输入年度递归 report 与 QQQ 原生 5m 事件缓存。

递归 report 校验：

- 输出：`D:/chanlun_pro/reports/wf_recursive_levels_qqq_year_15d_chunked_1m.json`。
- 状态：`cached=23/23`，`missing_chunks=[]`，`is_partial=false`。
- 合并事件：91 个，全部在交易窗口内；L1 买信号 31 个，L1 卖信号 58 个，L2/30m 方向变化 0 个。
- 无未来规则：每个 chunk 带前置 warmup 逐根扫描原始 1m K 线，只合并 `visible_time` 落在 chunk 内的事件；成交统一在下一根 1m 开盘。

年度递归 L1/L2 自身回放：

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 |
| --- | ---: | ---: | ---: | ---: |
| L1-only | +5.5% | 8.7% | 9 | 56% |
| L1/L2-not-down | +5.5% | 8.7% | 9 | 56% |
| L1/L2-up | 0.0% | 0.0% | 0 | 0% |

正式混合回放输入：

- 输出：`D:/chanlun_pro/reports/wf_native_recursive_context_qqq_year_15d.json`。
- sleeve 输出：`D:/chanlun_pro/reports/wf_native_recursive_context_qqq_year_15d_sleeves.json`。
- 原生 5m 事件缓存：`D:/chanlun_pro/reports/wf_native_qqq_events.pkl`，命中 851 个 5m 事件。
- 执行轴：统一 1m；5m 缓存时间戳为 K 线起始时间，先平移到该 5m 窗口最后一根 1m bar 收盘可见，再由下一根 1m 开盘成交。
- 对照：同窗口买入持有 `+26.8%`。

混合策略结果：

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 | L1 卖出 | 过滤买入 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| seg38_base | +4.3% | 12.2% | 217 | 46% | 0 | 0 |
| seg38_l1_up | +1.6% | 6.0% | 93 | 54% | 5 | 249 |
| seg38_l1_not_down | +1.6% | 6.3% | 110 | 49% | 7 | 208 |
| seg38_plus_l1_sell | -2.2% | 14.7% | 239 | 46% | 37 | 0 |

关键仓位结果：

| 仓位 | 收益 | 最大回撤 | 交易数 | 胜率 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| active 25% + cash 75% | +0.4% | 1.6% | 93 | 54% | 只做活动仓收益不足 |
| active 50% + cash 50% | +0.8% | 3.1% | 93 | 54% | 回撤低但无明显收益 |
| active 75% + cash 25% | +1.2% | 4.6% | 93 | 54% | 不适合作为 QQQ 主仓 |
| active 100% | +1.6% | 6.0% | 93 | 54% | 低回撤但明显跑输裸持 |
| core 50% + cash 50% | +13.4% | 6.7% | 0 | 0% | QQQ 年度收益主要来自趋势核心仓 |
| core 75% + cash 25% | +20.1% | 9.7% | 0 | 0% | 高于活动仓，低于满核心仓 |
| core 75% + active 25% | +20.5% | 10.8% | 93 | 54% | 活动仓略增收益但抬升回撤 |
| core 100% | +26.8% | 12.6% | 0 | 0% | QQQ 本窗口最强对照 |

QQQ 验证结论：

1. `seg38_l1_up` 在 QQQ 上依然能降低回撤：`seg38_base` 为 `+4.3%/DD12.2%`，`seg38_l1_up` 为 `+1.6%/DD6.0%`；但收益被压得过低，不能作为指数类标的的默认满仓系统。
2. 只加 L1 卖出在 QQQ 上变差到 `-2.2%/DD14.7%`，再次说明低级别卖点不能机械叠加到 5m 程式上；必须结合更大级别走势段、背驰位置、二/三卖结构和仓位层级。
3. QQQ 这种年度趋势强、波动相对低的标的，主要收益来自核心仓，而不是 5m 活动仓频繁交易。当前应把 5m 活动仓定位成降低净暴露和微调仓位的工具，而不是替代趋势持仓。
4. 对 TSLA 这类高波动标的，`seg38_l1_up` 可以把 TSLA 年度回撤压到 7.8%；对 QQQ 则只保留风控价值。选股/选标的因此不能只看是否出现缠论买点，还要先过滤“是否值得用活动仓交易”：波动、趋势斜率、5m 中枢扩张/扩展后的空间、与 30m 同级别方向是否共振。
5. 原文中的级联分析不能消灭买卖点确认滞后；它的实盘作用是：用大级别中枢、背驰段和走势类型先限定风险区间，再用小级别一/二/三类买卖点给出可执行触发。回测中必须把每个级别的 `visible_time` 作为唯一可交易时间，严禁把后验确认点提前到结构起点。

图表实现核对：

1. `CL._UPGRADE_CHAIN` 已明确 `1m -> 5m(kuozhan) -> 30m(tongjibie)`、`5m -> 30m(tongjibie)`，`30m` 不再升级。
2. `tests/core/test_cl_data_to_tv_chart_zs.py::test_cl_data_to_tv_chart_multitimeframe_overlay_contract` 覆盖：1m 图含 `L0/5m/30m`，5m 图含 `L0/30m`，30m 图只含 `L0`；同时要求 `bis`、`bi_mmds/xd_mmds`、`bi_bcs/xd_bcs` 和 `recursive_levels.zss/zslx_lines` 存在。
3. `test_cl_data_to_tv_chart_serializes_l1_l2_mmds_and_bcs` 覆盖 L1/L2 买卖点与背驰随 `recursive_levels` 输出。下一步仍需用浏览器对真实 TSLA/QQQ 1m、5m、30m 图做截图验收，确认前端实际渲染不重叠、不缺层。

## 92. 第一百零二轮 TSLA 2026Q1 熊市严格无未来压力测试

为检验 TSLA 年度上涨/震荡样本中得到的活动仓候选是否能穿越下跌窗口，本轮只用 TSLA 2026Q1 熊市缓存做严格实盘式压力测试：

1. 数据：`D:/chanlun_pro/chart_cache_us_2026q1_bear`，TSLA 1m/5m/30m 覆盖 `2026-02-02 14:30:00 ~ 2026-04-14 16:00:00`。
2. 交易窗：`2026-02-17 14:30:00 ~ 2026-04-14 16:00:00`；此前 15 天只作为结构 warmup。
3. 分片：`chunk-days=10`，`signal-warmup-bars=1200`，6 个 chunk 全部逐根 1m 扫描。
4. 兼容修正：严格回放脚本的缓存查找已支持 `recursivebt_2026q1_bear.pkl` 这类后缀文件；年度无后缀缓存仍优先按原路径命中。

递归 report 校验：

- 输出：`D:/chanlun_pro/reports/wf_recursive_levels_tsla_2026q1_bear_strict_10d_chunked_1m.json`。
- 状态：`chunks=6`，`missing_chunks=[]`，`is_partial=false`。
- 合并事件：5 个，全部在交易窗口内；L1 买信号 4 个，L1 卖信号 1 个，L2/30m 事件 0 个。
- 对照：同窗口买入持有 `-11.3%`。

年度递归 L1/L2 自身压力结果：

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 |
| --- | ---: | ---: | ---: | ---: |
| L1-only | -10.9% | 15.8% | 1 | 0% |
| L1/L2-not-down | -10.9% | 15.8% | 1 | 0% |
| L1/L2-up | 0.0% | 0.0% | 0 | 0% |

正式混合回放：

- 输出：`D:/chanlun_pro/reports/wf_native_recursive_context_tsla_2026q1_bear_strict_10d.json`。
- 原生 5m 事件：161 个；本轮重新生成，非缓存命中。
- 执行轴：统一 1m；5m 事件按 5m 窗口最后一根 1m bar 收盘可见，下一根 1m 开盘成交。

混合策略结果：

| 方案 | 收益 | 最大回撤 | 交易数 | 胜率 | L1 卖出 | 过滤买入 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| seg38_base | +0.6% | 8.1% | 37 | 46% | 0 | 0 |
| seg38_plus_l1_sell | +0.6% | 8.1% | 37 | 46% | 0 | 0 |
| seg38_l1_not_down | -5.6% | 8.1% | 32 | 41% | 0 | 8 |
| seg38_l1_up | -6.4% | 7.9% | 31 | 39% | 0 | 12 |

仓位压力结果：

| 仓位 | 收益 | 最大回撤 | 交易数 | 胜率 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| active 25% + cash 75% | -1.6% | 2.0% | 31 | 39% | 熊市观察档仍亏损 |
| active 50% + cash 50% | -3.2% | 4.0% | 31 | 39% | 仓位越高亏损线性放大 |
| active 100% | -6.4% | 7.9% | 31 | 39% | 不适合熊市主仓 |
| core 25% + cash 75% | -2.8% | 4.9% | 0 | 0% | 核心仓也应降低 |
| core 50% + cash 50% | -5.7% | 9.9% | 0 | 0% | 接近裸持的一半损失 |
| core 100% | -11.3% | 19.5% | 0 | 0% | 裸持对照 |

熊市压力结论：

1. TSLA 熊市窗口里，5m 原生 `seg38_base` 反而是相对最稳的活动策略：`+0.6%/DD8.1%`，明显好于裸持 `-11.3%/DD19.5%`。
2. `seg38_l1_up` 在熊市中不再是好过滤器：它减少了 12 次买入，但留下的 31 笔交易质量更差，最终 `-6.4%/DD7.9%`。这说明低级别 L1 上下文为 `up` 只能证明小级别反弹，不等于大级别风险解除。
3. 本轮 L2/30m 没有形成可操作事件，因此不能把 30m 方向硬编码为利多或利空。30m 同级别分解在无事件时应表现为“不放大仓位”，而不是制造信号。
4. 结合 TSLA 年度、QQQ 年度、TSLA 熊市三组严格样本，当前通用仓位框架应改成条件式：
   - 30m 同级别方向明确向上，且 5m 中枢扩展/扩张后仍有空间：允许 5m 活动仓提高到 75%~100%；
   - 指数类或低波动强趋势标的：优先核心仓，5m 活动仓只做小比例微调；
   - 30m 无向上事件、1m/L1 只是小级别反弹、或市场处于熊市段：活动仓降到 0%~25%，等待 5m 二/三买与背驰区间套确认后再恢复。
5. 这更符合原文“买点不是预测最低点，而是走势分解后可操作点”的思想：大级别先决定能不能做、做多大；小级别只决定什么时候做，不能替代大级别仓位约束。

## 93. 第一百零三轮 TSLA 多级别图表真实数据审计

本轮补做 TSLA 真实缓存的图表展示审计，目标不是再证明交易收益，而是确认用户要求的 1m/5m/30m 多层结构确实能进入展示数据：

1. 当前 9900 Web 服务已在运行，但 `/tv/history` 直接访问会重定向到 `/login`；本轮未读取或绕过用户密码，只用 Flask test client 注入登录态做后端数据契约审计。
2. 默认 `D:/chanlun_pro/chart_cache` 中 TSLA 5m 只覆盖 `2026-03-16 13:30:00 ~ 2026-04-14 16:00:00`，窗口太短，因此不能拿它判断“5m 图是否能显示 30m 中枢”；这是缓存完整性问题，不是 recursive_levels 逻辑失败。
3. 年度严格缓存 `D:/chanlun_pro/chart_cache_us_tsla_1y` 覆盖完整年度，已用新增脚本 `scripts/render_chanlun_visual_audit.py` 生成独立审计页：`D:/chanlun_pro/reports/chanlun_visual_audit_tsla.html`。
4. Codex 内置浏览器安全策略拒绝打开 `file://` 审计页；本轮没有绕过该策略改用其他浏览器面。验收改为 HTML/SVG 结构统计与后端真实数据计数。

默认 Web 缓存的 `/tv/history` 数据契约结果：

| 图表 | K线覆盖 | bars | 本级/递归结构 |
| --- | --- | ---: | --- |
| 1m | `2025-06-12 13:30:00 ~ 2026-04-14 16:00:00` | 81111 | 笔中枢 694；L0 中枢 119、走势线 41；L1 中枢 52、买卖点 23、背驰 10；L2 中枢 2 |
| 5m | `2026-03-16 13:30:00 ~ 2026-04-14 16:00:00` | 1591 | L0 中枢 6；L1 为 0，原因是默认 5m 缓存窗口太短 |
| 30m | `2025-06-12 13:30:00 ~ 2026-04-14 16:00:00` | 2704 | L0 中枢 3 |

年度严格缓存的独立审计页结果：

| 图表 | 审计窗口 | bars | 笔 | 笔中枢 | 递归层级合计 | 本级买卖点 | 本级背驰 |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: |
| 1m | `2025-09-01 13:30:00 ~ 2025-12-22 21:00:00` | 30625 | 2018 | 228 | L0 中枢 119；L1 中枢 52、买卖点 23、背驰 10；L2 中枢 2 | 335 | 71 |
| 5m | `2025-06-10 13:30:00 ~ 2026-04-15 21:00:00` | 16476 | 1083 | 132 | L0 中枢 19；L1/30m 中枢 1 | 158 | 13 |
| 30m | `2026-02-15 13:30:00 ~ 2026-05-20 21:00:00` | 858 | 70 | 10 | L0/30m 中枢 3 | 11 | 0 |

HTML 输出统计：3 个 SVG 面板、48401 个 `rect`、597 个 `circle`、3171 个 `polyline`。因此当前证据链为：

1. 后端 `cl_data_to_tv_chart` 会输出 `bis`、`bi_zss`、`bi_mmds/xd_mmds`、`bi_bcs/xd_bcs` 与 `recursive_levels[L0/L1/L2]`。
2. 前端 `charts.js` 已按 `recursive_levels` 扁平化绘制各级中枢，并从 `recursive_levels[].mmds/bcs` 绘制 L1/L2 买卖点与背驰。
3. 年度 TSLA 真实缓存下，1m 图具备 1m/5m/30m 三层结构，5m 图具备 5m/30m 两层结构，30m 图具备 30m 本级结构。
4. 下一次如要做截图级终验，应先让 Web 服务加载年度完整缓存，或给审计页提供合规的本地 HTTP 静态路由；不能再用默认短 5m 缓存误判前端缺层。

## 94. 第一百零四轮 组合回测入口的严格窗口切片与 US core9 烟测

本轮继续推进“绝对不允许未来信号”的组合回测入口。直接用 `live_backtest.py` 跑 US core9、`signal_mode=walk_forward`、`signal_warmup_bars=1200` 的完整 `2026-04-14 ~ 2026-06-10` 窗口时，9 标的与 3 标的版本分别超过 10 分钟、6 分钟仍未完成；单 TSLA 全窗口也超过 5 分钟。这不是未来函数问题，而是逐根重算工程性能不适合作为日常严格验收入口。

已修复：

1. `src/chanlun/recursive_bt/live_backtest.py` 新增 `_slice_df_for_signal_window`。
2. `chart_cache` 来源且指定 `--start/--end` 时，原始 K 线只保留：
   - `end` 之前的数据，`end` 是硬上界，未来 K 线不进入扫描；
   - `start` 之前每个频率各自的 `signal_warmup_bars` 行，作为不可交易 warmup。
3. 不指定 `--start/--end` 时保持旧行为，仍可做全缓存回放。
4. `tests/test_backtest_live_parity.py` 增加两条回归：一条验证切片不含未来行，一条验证 `load_chart_cache_syms` 在建信号前完成窗口切片。

严格短窗烟测：

命令共同口径：

- `source=chart_cache`
- `op_level=1m`
- `mid_level=5m`
- `big_level=30m`
- `signal_mode=walk_forward`
- `signal_warmup_bars=1200`
- `require=tech,nest_soft,trend3_boost`
- `mid_gate=soft`
- `big_gate=bsp`
- 交易窗 `2026-06-08 13:30:00+00:00 ~ 2026-06-10 16:00:00+00:00`

| 样本 | 输出 | 收益 | 买持 | 超额 | 最大回撤 | 基准回撤 | 交易数 | 胜率 | 耗时 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TSLA | `D:/chanlun_pro/reports/us_tsla_mtf3_wf1200_window_smoke_summary.json` | +4.2% | -2.5% | +6.8% | 1.5% | 8.3% | 6 | 83% | 17.8s |
| US core9 | `D:/chanlun_pro/reports/us_core9_mtf3_wf1200_window_smoke_summary.json` | -0.2% | -2.6% | +2.5% | 0.9% | 4.1% | 38 | 45% | 144.6s |

本轮结论：

1. 组合入口现在可以在指定窗口内用严格实盘方式快速产出可审计报告，报告显式写入 `signal_mode=walk_forward` 与 `signal_warmup_bars=1200`。
2. 这两条烟测只能证明执行链路与短窗风控有效，不能当成全年收益结论；全年/多行情结论仍应以 §89~§92 的 TSLA 年度、QQQ 年度、TSLA 熊市严格分片结果为主。
3. 下一步工程方向应是把 `live_backtest` 的逐根信号扫描做成事件缓存，类似 `wf_recursive_levels_1m_tsla.py` 的 event-cache；这样 US core9 全窗口严格回放才可作为常规回归，而不是一次性长跑。

## 95. 第一百零五轮 严格 walk-forward 信号事件缓存

本轮把 §94 的工程缺口落地到组合回测入口：`src/chanlun/recursive_bt/live_backtest.py` 已新增严格 walk-forward 信号事件缓存。

缓存设计：

1. 默认目录：`D:/chanlun_pro/reports/live_backtest_signal_cache`；CLI 参数 `--signal-cache-dir` 可覆盖，传空字符串可禁用。
2. 仅缓存严格 `walk_forward` 扫描后的 `by_bar` 事件流，不缓存组合权益曲线，不改变撮合逻辑。
3. 缓存 key 包含：
   - `code`、信号周期；
   - K 线行数、首尾时间、OHLCV 内容 hash；
   - 主执行轴行数、首尾时间、时间序列 hash；
   - `available_delay`、`signal_warmup_bars`、`annotate_nest`；
   - `CL_CFG` hash 与缓存 schema 版本。
4. meta 不完全一致直接 miss；因此修正 K 线、换窗口、换 warmup、换嵌套判据、换配置都不会误命中旧事件。
5. summary 新增 `signal_cache_dir` 与 `signal_cache_stats`，可审计本次是命中缓存还是重新扫描。

真实 TSLA 短窗缓存验证：

| 运行 | 输出 | 耗时 | cache stats | 收益 | 回撤 | 交易 |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| 首次写入 | `D:/chanlun_pro/reports/us_tsla_mtf3_wf1200_cache_miss_summary.json` | 16.5s | `hits=0, misses=3, writes=3, entries=30` | +4.2% | 1.5% | 6 |
| 全命中 | `D:/chanlun_pro/reports/us_tsla_mtf3_wf1200_cache_hit_summary.json` | 0.8s | `hits=3, misses=0, writes=0, entries=30` | +4.2% | 1.5% | 6 |

真实 US core9 短窗缓存验证：

| 运行 | 输出 | 耗时 | cache stats | 收益 | 买持 | 回撤 | 交易 |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |
| 写入剩余标的 | `D:/chanlun_pro/reports/us_core9_mtf3_wf1200_cache_warm_summary.json` | 123.7s | `hits=3, misses=24, writes=24, entries=264` | -0.2% | -2.6% | 0.9% | 38 |
| 全命中 | `D:/chanlun_pro/reports/us_core9_mtf3_wf1200_cache_hit_summary.json` | 1.6s | `hits=27, misses=0, writes=0, entries=264` | -0.2% | -2.6% | 0.9% | 38 |

本轮结论：

1. 事件缓存不改变结果：TSLA 与 core9 的收益、回撤、交易数在 miss/write 与 full-hit 两轮完全一致。
2. 组合严格回测已经从“每次逐根重算”变成“第一次生成可审计事件缓存，之后秒级回归”。
3. 这为后续扩展到完整 core9 全窗口、更多标的与更多行情段提供了工程基础；仍需逐步生成完整窗口缓存并纳入常规回归。

## 96. 第一百零六轮 首次扫描去重与更长弱势窗口验证

本轮继续优化严格 walk-forward 首次扫描。观察到首次扫描的主要开销不是撮合，而是每个主时钟 tick 都可能重复读取整棵买卖点树；因此在 `live_backtest.py` 中增加结构签名跳过：

1. 每根 K 线仍逐根进入 `CL.process_klines`，无未来语义不变。
2. 仅当 CL 的 `_last_mmd_sig` 发生变化时，才重新执行 `collect_branch_signals`。
3. 结构签名未变时，沿用上一轮 active signal set，不产生新事件。
4. 对没有 `_last_mmd_sig` 的测试假对象保持回退：每次仍收集，避免隐藏兼容问题。

回归保护：

- `test_walk_forward_skips_signal_collection_when_structure_signature_unchanged` 覆盖结构签名稳定时只收集一次。
- 原高周期 delay、事件缓存命中、窗口切片测试继续通过。

真实短窗性能对照：

| 样本 | 窗口 | 首次扫描 | 全命中 | cache stats | 说明 |
| --- | --- | ---: | ---: | --- | --- |
| TSLA | `2026-06-08 ~ 2026-06-10` | 13.5s | 0.7s | miss `0/3/3`，hit `3/0/0` | 首次较 §95 的 16.5s 小幅下降 |
| TSLA | `2026-06-01 ~ 2026-06-10` | 37.9s | 0.8s | miss `0/3/3`，hit `3/0/0` | 更长弱势窗口 |
| TSLA/QQQ/NVDA | `2026-06-01 ~ 2026-06-10` | 77.0s | 1.0s | miss `3/6/6`，hit `9/0/0` | 三标的组合验证 |

弱势窗口结果：

| 样本 | 收益 | 买持 | 超额 | 最大回撤 | 基准回撤 | 交易数 | 胜率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| TSLA | 0.0% | -9.6% | +9.6% | 0.0% | 11.7% | 0 | 0% |
| TSLA/QQQ/NVDA | -1.5% | -6.6% | +5.2% | 3.1% | 9.8% | 21 | 38% |

本轮结论：

1. 签名跳过能减少重复信号收集，但首次扫描仍主要受 `CL.process_klines` 内部高周期 MACD 与结构递归计算影响；不能为了速度关闭高周期 MACD，因为那会改变背驰/买卖点语义。
2. 事件缓存继续是严格回归的核心工程机制：同输入 hit 后 TSLA 与三标的组合均为 1 秒内复验。
3. 6 月 1 日到 6 月 10 日弱势窗口里，严格系统对 TSLA 选择空仓，避免了 -9.6% 买持下跌；三标的组合仍小亏，但显著低于买持回撤，说明当前体系更像风险控制优先，而不是任何行情都追求满仓收益。
4. 下一步应继续扩大缓存生成范围：先按窗口分批生成 core9 全窗口事件缓存，再把“空仓/低仓位何时恢复”的规则和 30m 同级别方向、5m 二/三买、背驰区间套结合。

## 97. 第一百零七轮 TSLA 完整 chart_cache 窗口分片严格回放

连续状态扫描 TSLA `2026-04-14 ~ 2026-06-10` 全窗口时，进程运行 8 分钟仍未产出结果，确认单标的全窗口仍存在 O(n^2) 级工程瓶颈。本轮新增显式分片扫描参数：

- CLI：`--signal-scan-chunk-bars N`
- 默认：`0`，保持旧的连续扫描语义；
- 启用：按主执行轴每 N 根 bar 分片，每片向前取 `signal_warmup_bars` 个信号周期 K 线作为不可交易 warmup；
- 合并：只合并落在本片交易区间内的可见新事件；
- 约束：仍逐根推进每片内 CL，不使用片尾之后的数据；这与 §89~§92 的分片 walk-forward 口径一致，但不是无限历史连续状态。

回归保护：

- `test_walk_forward_chunked_scan_matches_continuous_scan` 用确定性假信号验证简单场景下分片事件与连续事件一致。
- `test_walk_forward_skips_signal_collection_when_structure_signature_unchanged` 继续保护结构签名跳过。
- 核心回归：`70 passed, 1 skipped`。

TSLA 完整 chart_cache 窗口结果：

| 运行 | 输出 | 耗时 | cache stats | 收益 | 买持 | 超额 | 最大回撤 | 基准回撤 | 交易数 | 胜率 |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 分片首次 | `D:/chanlun_pro/reports/us_tsla_mtf3_wf1200_full_20260414_0610_chunk3000_miss_summary.json` | 212.1s | `hits=0, misses=18, writes=18, entries=391` | +11.7% | +5.7% | +6.0% | 6.1% | 15.5% | 37 | 62% |
| 全命中 | `D:/chanlun_pro/reports/us_tsla_mtf3_wf1200_full_20260414_0610_chunk3000_hit_summary.json` | 1.5s | `hits=18, misses=0, writes=0, entries=391` | +11.7% | +5.7% | +6.0% | 6.1% | 15.5% | 37 | 62% |

本轮结论：

1. TSLA 完整 `D:/chanlun_pro/chart_cache` 窗口已经有严格、可复验的分片 walk-forward 报告；这不是先算全局买卖点再回测。
2. 分片首次扫描仍要 212 秒，但它产出 18 个可审计事件缓存；之后同输入 1.5 秒复验，收益/回撤/交易数完全一致。
3. 这条结果与弱势窗口一致：系统用仓位和信号过滤压低回撤，TSLA 两个月窗口跑赢买持且回撤显著下降。
4. 下一步可以用同一 `--signal-scan-chunk-bars 3000` 逐步生成 QQQ、NVDA、再到 core9 全窗口缓存；core9 不应再用连续扫描硬跑。

## 98. 第一百零八轮 QQQ/NVDA 完整 chart_cache 窗口横向复验

本轮按 §97 的同一严格口径继续生成 QQQ 与 NVDA 全窗口分片 walk-forward 报告：

- 数据源：`D:/chanlun_pro/chart_cache`
- 窗口：`2026-04-14 16:00:00+00:00 ~ 2026-06-10 16:00:00+00:00`
- 层级：`1m` 执行，`5m` 中级，`30m` 大级别
- 信号：`signal_mode=walk_forward`，`signal_warmup_bars=1200`
- 分片：`signal_scan_chunk_bars=3000`
- 过滤：`require=tech,nest_soft,trend3_boost`，`mid_gate=soft`，`big_gate=bsp`

横向结果：

| 标的 | 运行 | 输出 | cache stats | 收益 | 买持 | 超额 | 最大回撤 | 基准回撤 | 交易数 | 胜率 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| QQQ | 分片首次 | `D:/chanlun_pro/reports/us_qqq_mtf3_wf1200_full_20260414_0610_chunk3000_miss_summary.json` | `hits=0, misses=18, writes=18, entries=356` | +2.1% | +12.2% | -10.1% | 4.1% | 8.2% | 49 | 53% |
| QQQ | 全命中 | `D:/chanlun_pro/reports/us_qqq_mtf3_wf1200_full_20260414_0610_chunk3000_hit_summary.json` | `hits=18, misses=0, writes=0, entries=356` | +2.1% | +12.2% | -10.1% | 4.1% | 8.2% | 49 | 53% |
| NVDA | 分片首次 | `D:/chanlun_pro/reports/us_nvda_mtf3_wf1200_full_20260414_0610_chunk3000_miss_summary.json` | `hits=0, misses=18, writes=18, entries=383` | -2.1% | +5.1% | -7.1% | 10.5% | 15.5% | 53 | 49% |
| NVDA | 全命中 | `D:/chanlun_pro/reports/us_nvda_mtf3_wf1200_full_20260414_0610_chunk3000_hit_summary.json` | `hits=18, misses=0, writes=0, entries=383` | -2.1% | +5.1% | -7.1% | 10.5% | 15.5% | 53 | 49% |

本轮结论：

1. QQQ 与 NVDA 的首次扫描和全命中复验完全一致，说明事件缓存只复用逐根可见的信号流，不引入未来信号。
2. 当前 `tech + nest_soft + trend3_boost` 活动交易体系对 TSLA 两个月窗口有效，但在 QQQ/NVDA 强趋势段会明显牺牲收益；它更像低回撤活动仓，而不是满仓趋势持有仓。
3. 对指数或趋势很顺的标的，最终体系不能简单把该活动策略作为唯一仓位来源，应拆成：
   - 30m 同级别方向确认后的核心仓；
   - 5m/1m 级联买卖点的活动仓；
   - 背驰、三卖、30m 中枢破坏后的降仓/清仓规则。
4. 因此后续“最通用、低回撤、高收益”版本应在不破坏无未来约束的前提下，引入趋势核心仓与活动仓分层，而不是继续只优化单一买卖点过滤器。

## 第六十九轮 A股监控全市场选股落地(每日重选+候选窗扩宽)

2026-06-12 A股开盘实战验证发现:CLI live_monitor 的池子固定在自选 4 只——selector 只在进程启动时选一次,且 `selection_lookback_bars=3`(缓存最后 15 分钟)在盘后启动时永远选不出候选。"A股全市场标的多系统选股"在实时监控层未真正落地。

修复(commit 196df42a):
1. **候选窗扩到昨日整天**:`selection_lookback_bars` 3→48(48根5m)。设计语义校正为"日级选股+分钟级择时":selector 在静态 bt_data 缓存(每日盘后构建)上选出昨日出现 1/2/3 类买点且过三系统(质量/比价/技术)的候选,监控盯这批候选的 1m 实时买点。
2. **每日重选**:`rescan_selection_pool()` 每天 09:00 后开盘前自动重选——新候选创建 state 并 warmup(现有信号只登记不触发),非持仓/非初始自选且不在新结果的旧候选淘汰,防止池子无界增长。
3. **warmup 限窗**:`WARMUP_DAYS_BY_FREQ`(1m=30天/5m=120天,30m/日线保留全量)——监控信号=笔级买点+笔方向,无需图表递归级别的 365 天 1m 全量;90 只池 warmup 从 ~2 小时压到分钟级。

当日实战验证(2026-06-12 开盘后):
- Warmup **56 symbols**(昨日买点候选+自选,三系统过滤后),scan=56 持续扫描;
- 扫描周期 **~38-63 秒**——56 只池(14 倍扩张)在限窗+增量拉取下仍保持 1m 级时效;
- err 日志干净,paper/优化链路状态行完整(opt_rratio_review=2)。

至此 goal 的"A股全市场标的多系统选股"在实时监控端真正闭环:全市场 5143 只 → 三系统日级过滤 → ~56 只候选池 → 1m 买点+5m/30m 联立门控 → 比例通知+paper 撮合。测试 `871 passed, 1 skipped`。
## 99. 第一百零九轮 原文三段口径、递归升级信号与买卖点滞后实证

本轮从参数调优转向原文一致性修复，新增并验证以下回测口径：

1. `--signal-source branch|upgrade`
   - `branch` 沿用当前 `get_branch_bspoints()`；
   - `upgrade` 在严格 walk-forward 中调用 `get_kuozhan_levels()`，收集 1m->5m/30m 级联后的 L1/L2 买卖点。
2. `--signal-unit bi|xd`
   - 用于 `branch` 信号源，区分笔级观察信号与线段级正式信号。
3. `--recursive-l0-min-zs-lines 3|4`
   - `3` 对齐原文“至少三个连续次级别走势类型重叠构成中枢”的结构定义；
   - `4` 保留旧工程确认口径。
4. `--signal-warmup-bars -1`
   - 表示保留全部历史作为预热；
   - 正数表示有限根数预热；有限预热会截断高级别结构，报告必须记录该值。

关键工程修复：

- `RecursiveBranchCalculator(l0_min_zs_lines=...)` 支持 L0 三段/四段口径；
- `CL.get_recursive_branch_levels()`、`get_branch_bspoints()`、`get_branch_bcs()` 读取 `recursive_l0_min_zs_lines`；
- `live_backtest` 的缓存元数据区分 `signal_source`、`signal_unit`、`recursive_l0_min_zs_lines`；
- 交易主时钟可只覆盖 `start/end` 窗口，信号计算仍保留窗口前历史；
- `upgrade` 信号源用线段结构签名跳过重复收集，避免每根 1m 都重算升级层级。

TSLA 结构诊断：

| 口径 | 历史窗口 | L1 信号 | L2 信号 | 说明 |
| --- | --- | ---: | ---: | --- |
| L0=4 | 2026-04-14~2026-06-10 | 3 | 0 | 旧口径压缩 5m/30m 结构 |
| L0=3 | 2026-04-14~2026-06-10 | 6 | 0 | 原文口径恢复更多 5m 信号，并出现 1 个 30m 中枢 |
| L0=3 | 全历史至 2026-06-10 | 23 | 2 | 历史越完整，高级别结构越完整，但计算成本更高 |

买卖点滞后实证：

| 字段 | 时间/值 |
| --- | --- |
| 静态 anchor | `2026-06-08 18:33:00+00:00` |
| 信号 | L1 `3buy`，price `412.94` |
| walk-forward visible | `2026-06-09 17:15:00+00:00` |
| next-bar fill | `2026-06-09 17:16:00+00:00`，entry `390.12` |

这说明静态图上的买点锚在 6月8日，但实盘直到 6月9日 17:15 才能确认；严格回测在下一根 1m 开盘成交，没有提前使用未来信号。

TSLA 短窗严格回测：

| 参数 | 值 |
| --- | --- |
| source | `chart_cache` |
| signal_mode | `walk_forward` |
| signal_source | `upgrade` |
| recursive_l0_min_zs_lines | `3` |
| signal_warmup_bars | `6000` |
| 窗口 | `2026-06-08 13:30:00+00:00 ~ 2026-06-10 20:00:00+00:00` |
| 输出 | `D:/chanlun_pro/reports/us_tsla_mtf3_wf6000_window_upgrade_l0min3_summary.json` |

结果：

| 收益 | 买持 | 超额 | 最大回撤 | 基准回撤 | 交易数 | 胜率 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| -2.18% | -3.72% | +1.54% | 4.25% | 8.92% | 1 | 0% |

结论：

1. 当前严格回测已明确区分 `anchor_time`、`visible_time`、`fill_time`，不会把静态锚点当作可交易时间。
2. 用户提出的“用大小级别级联降低买卖点滞后”方向成立；`upgrade` 链能真实体现 1m->5m 的级联确认。
3. 30m L2 买卖点仍稀疏，下一步应继续审计 `tongjibie_zhongshu_ex`、走势类型完成度、30m 中枢扩展/扩张与背驰触发，而不是回到笔级信号调参。

## 100. 第一百一十轮 信号审计 CSV 与起点方向状态修复

本轮补强严格回测的可审计性：

1. 新增 `--output-signals`，默认随交易明细生成 `_signals.csv`；
2. 每条信号输出：
   - `code`
   - `stream`
   - `level`
   - `bs_type`
   - `anchor_time`
   - `visible_time`
   - `next_fill_time`
   - `anchor_bar`
   - `visible_bar`
   - `next_fill_bar`
   - `anchor_to_visible_bars`
   - `signal_price`
   - `next_fill_open`
3. `summary.json` 增加 `signal_event_count`。

同时修复一个实盘语义问题：

- walk-forward 事件流只应输出“回测开始后新出现的信号”；
- 但 5m/30m 方向门控必须继承“回测开始时已经可见的最近高等级信号状态”；
- 因此扫描器现在返回 `initial_state`，用于初始化 `mid_dir_at` / `big_dir_at`；
- 旧信号不会重新发为交易事件，只用于方向状态。

回归覆盖：

- `test_walk_forward_preserves_initial_direction_without_reemitting_old_signal`
- `test_build_symbol_walk_forward_uses_visible_new_signals`
- `test_build_symbol_walk_forward_can_use_recursive_upgrade_signals`

TSLA 审计输出：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf6000_window_upgrade_l0min3_signal_audit_v2_summary.json` | 严格回测汇总 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf6000_window_upgrade_l0min3_signal_audit_v2_trades.csv` | 交易明细 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf6000_window_upgrade_l0min3_signal_audit_v2_signals.csv` | 信号审计明细 |

信号审计明细中，TSLA L1 `3buy`：

| anchor_time | visible_time | next_fill_time | anchor_to_visible_bars | signal_price | next_fill_open |
| --- | --- | --- | ---: | ---: | ---: |
| `2026-06-08 18:33:00+00:00` | `2026-06-09 17:15:00+00:00` | `2026-06-09 17:16:00+00:00` | 312 | 412.94 | 390.12 |

30m L2 诊断：

- 全历史至 2026-06-10、L0=3 时，`1m->5m` L1 中枢为 66 个；
- `tongjibie_zhongshu_ex` 将其组合为 16 个同级别交替段；
- 形成 4 个 L2/30m 中枢组：`(0,2)`, `(5,7)`, `(8,10)`, `(13,15)`；
- 前 3 个已完成，最后一个未完成；
- L2 买卖点只有 2 个，分别为历史 `3buy` 与 `3sell`，当前 2026-06 窗口尚无新的 L2 回抽确认。

结论：30m L2 稀疏不只是回测撮合问题，而是同级别分解在当前 TSLA 历史里确实需要很长结构积累。下一步要继续审计“同级别分解的段划分与完成度”是否过严，尤其是 `_swing_alternating_segs` 和 `_tongjibie_groups` 对 30m 中枢的分组方式。

## 101. 第一百一十一轮 大级别底仓与次级别短差回补

本轮继续沿用第 99-100 轮建立的无未来信号链，不改信号生成，只补组合仓位状态机。

原文口径：

- 大级别买点介入后，次级别卖点是活动仓短差，不应默认清掉大级别底仓；
- 次级别买点出现后，应在实盘可见后下一根 bar 回补活动仓；
- 大级别转 down 或较大级别卖点，才处理核心仓。

实现：

- `portfolio_backtest` 的 `trend_core_hold_ratio` 现在不仅记录 `core_shares`，还记录 `activity_target_shares`。
- 小级别卖点成交后，若仍保留核心仓，会进入 `activity_reentry=wait_buy`。
- 后续同标的当前可见买点触发 `activity_refill` 挂单，按下一根 bar 开盘补回活动仓缺口；该挂单不占新开仓 slot。
- 30m `down` 仍全平，不保护核心仓。

验证：

| 项目 | 结果 |
| --- | --- |
| 新测试 | `test_portfolio_backtest_trend_core_refills_activity_on_later_buy_point` |
| 回归 | `179 passed, 1 skipped` |
| TSLA 复跑 | `signal_mode=walk_forward`, `signal_source=upgrade`, `signal_warmup_bars=-1`, `trend_core_hold_ratio=0.5` |
| 输出 | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_core_refill_verify_summary.json` |
| 结果 | 收益 `0.00%`，买持 `-3.72%`，回撤 `0.00%`，交易 `0`，信号事件 `4`，缓存命中 `hits=1` |

信号审计仍显示 TSLA L1 `3buy` 的 `anchor_time=2026-06-08 18:33:00+00:00`、`visible_time=2026-06-09 17:15:00+00:00`、`next_fill_time=2026-06-09 17:16:00+00:00`、`anchor_to_visible_bars=312`。因此本轮仓位增强没有把锚点提前当作可交易时间。

结论：仓位层已从“底仓保护”推进到“卖活动仓、等买点回补”的闭环；但它仍只是两层状态。后续要完全贴近原文，应继续拆出 30m 核心仓、5m 波段仓、1m 短差仓，并让每层只接受自己级别的买卖点和背驰确认。

## 102. 第一百一十二轮 30m 同级别信号稀疏诊断

本轮没有放宽 30m 同级别分解，而是补强审计，确认 TSLA 严格窗口中 L2 信号稀疏的原因。

审计产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit_v2.json` | TSLA L1/L2 同级别候选、入选、三买三卖诊断 |
| `scripts/audit_tongjibie_tsla.py` | 生成脚本，读取 raw chart-cache，不作为交易信号源 |

关键计数：

| 层级 | 中枢 | 买卖点 | 背驰 |
| --- | ---: | ---: | ---: |
| L1 / 5m | 66 | 23 | 13 |
| L2 / 30m | 4 | 2 | 0 |

同级别候选诊断：

- 所有连续三段重叠候选为 7 组；
- 非重叠同级别分解入选 4 组：`(0,2)`, `(5,7)`, `(8,10)`, `(13,15)`；
- 被排除的 `(1,3)`, `(2,4)`, `(7,9)` 均是与已入选前缀重叠的审计候选，不能同时交易，否则破坏同级别唯一分解。

L2 三买三卖诊断：

| group | 诊断 |
| --- | --- |
| `(0,2)` | 末段向下，但后续上段回试终点 `321.55` 高于 `ZD=314.75`，不构成 `3sell` |
| `(5,7)` | 后续下段回试终点 `420.49 >= ZG=340.55`，构成 L2 `3buy` |
| `(8,10)` | 后续上段回试终点 `403.09 <= ZD=422.12`，构成 L2 `3sell` |
| `(13,15)` | 右边缘尚无后续回试段，不能提前给 L2 `3buy` |

结论：

1. L2 稀疏不是审计脚本与正式 `get_kuozhan_levels()` 不一致，正式链路计数同样是 L1 `66/23/13`、L2 `4/2/0`。
2. 不能为了增加 TSLA 交易，把重叠三段候选也当成可交易 30m 中枢；这会违反 39 课“当下同级别唯一分解”的操作纪律。
3. 当前 TSLA 的 30m 结构更适合作为核心仓/风控背景；实际执行仍应由 5m/1m 活动仓在可见买卖点上完成。

## 103. 第一百一十三轮 核心仓卖点级别路由

本轮修复组合撮合层的一个分层语义风险：持有核心仓时，卖点必须按来源级别路由。L1/5m 卖点只能处理活动仓；L2/30m 卖点必须能处理核心仓。

实现：

- `portfolio_backtest` 新增 `core_signal_level`；
- 卖点 `level >= core_signal_level` 时，退出原因为 `big_level_sell_point`，卖出比例锁为 `1.0`；
- 小级别卖点仍保留 `small_level_sell_point`，并继续尊重小级别 `sell_ratio_overrides`；
- `live_backtest` 在 `signal_source=upgrade` 时自动推断核心级别：`1m->30m` 为 L2，`5m->30m` 为 L1；
- 回测 summary 增加 `core_signal_level` 字段。

验证：

| 项目 | 结果 |
| --- | --- |
| 新测试 | `test_portfolio_backtest_core_signal_level_exits_trend_core` |
| 回归 | `133 passed` |
| TSLA 严格复跑 | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_core_level_verify_summary.json` |
| 复跑口径 | `signal_mode=walk_forward`, `signal_source=upgrade`, `signal_warmup_bars=-1`, `core_signal_level=2` |
| 结果 | 交易 `0`，信号事件 `4`，缓存命中 `hits=1` |

结论：核心仓现在不再只靠 `big_dir_at` 间接退出；30m/L2 卖点本身也能直接触发核心仓全平。这是把“30m 核心仓、5m 活动仓、1m 触发”的三层状态机继续落地的一步。

## 104. 第一百一十四轮 30m 下行中的低级别活动仓

本轮继续把原文的大小级别联动落实到组合执行层：30m `down` 不再只能产生“全局禁止买入”这一种含义。默认仍禁止开仓；但当策略显式开启活动仓参数时，低于核心级别的 5m/1m 买点可以用缩小仓位参与短差，核心仓仍不建立。

实现：

- `portfolio_backtest` 新增 `big_down_activity_buy_ratio_multiplier`，默认 `0.0`；
- `live_backtest` 新增 CLI：`--big-down-activity-buy-ratio-multiplier`；
- summary 新增 `big_down_activity_buy_ratio_multiplier` 字段；
- walk-forward 信号事件按 `anchor_time + level + bs_type` 全 replay 去重，同一信号消失后再出现不再二次触发；
- 只有 `signal.level < core_signal_level` 的买点才可作为 30m down 背景中的活动仓买点；
- 活动仓成交后 `core_shares=0`，并带 `big_down_activity` 标记；
- 该标记只豁免“开仓背景就是 30m down”导致的机械平仓，不豁免小级别卖点、核心级别卖点，也不豁免后续重新转 down 的大级别风控。

验证：

| 项目 | 结果 |
| --- | --- |
| 新测试 | `test_portfolio_backtest_blocks_big_down_activity_by_default` |
| 新测试 | `test_portfolio_backtest_allows_lower_level_activity_in_big_down_when_enabled` |
| 新测试 | `test_portfolio_backtest_preserves_buy_ratio_on_final_close` |
| 新测试 | `test_walk_forward_dedupes_reappearing_signal_identity` |
| 回归 | `75 passed` (`tests/test_backtest_live_parity.py`) |
| 相关回归 | `184 passed, 1 skipped` |
| TSLA 严格复跑 | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_bigdown_activity025_summary.json` |
| 复跑口径 | `signal_mode=walk_forward`, `signal_source=upgrade`, `signal_warmup_bars=-1`, `core_signal_level=2`, `big_down_activity_buy_ratio_multiplier=0.25` |
| 结果 | 收益 `-0.60%`，买持 `-3.72%`，超额 `+3.12%`，回撤 `1.18%`，交易 `1`，信号事件 `2` |

成交审计：

| 字段 | 值 |
| --- | --- |
| 信号 | L1 `3buy` |
| `anchor_time` | `2026-06-08 18:33:00+00:00` |
| `visible_time` | `2026-06-09 17:15:00+00:00` |
| `next_fill_time` | `2026-06-09 17:16:00+00:00` |
| `entry_date` | `2026-06-09 17:16:00+00:00` |
| `entry_px` | `390.12` |
| `buy_ratio` | `0.275` |

结论：该口径是对“级联分析解决滞后”的保守实现：高一级别不确认核心反转时，只允许低级别活动仓；交易仍发生在 `visible_time` 后的下一根 bar，绝不使用静态锚点提前成交。

## 105. 第一百一十五轮 核心仓入场级别约束

本轮修正一个更细的仓位分层语义：核心仓不能只由 30m 方向决定。若 `core_signal_level=2` 表示 30m/L2 是核心级别，则 L1/5m 买点即使发生在 30m `up` 背景中，也只能建立活动仓，不能生成 `core_shares`。

实现：

- 买单携带触发信号的 `level`；
- 持仓记录 `entry_level`、`entry_layer`、`activity_shares`；
- `PTrade` 导出 `entry_level`、`exit_level`、`entry_layer`、`exit_layer`、`core_shares_before`、`activity_shares_before`；
- 当 `core_signal_level > 0` 时，只有 `entry_level >= core_signal_level` 才允许使用 `trend_core_hold_ratio` 拆出核心仓；
- 小级别卖点记录为 `exit_layer=activity`，核心级别卖点记录为 `exit_layer=core_all`，窗口末尾强平记录为 `exit_layer=all`。

验证：

| 项目 | 结果 |
| --- | --- |
| 调整测试 | `test_portfolio_backtest_core_signal_level_exits_trend_core` 使用 L2 `3buy` 建核心仓 |
| 新测试 | `test_portfolio_backtest_low_level_buy_does_not_create_core_when_core_level_set` |
| 单文件回归 | `76 passed` |
| 相关回归 | `185 passed, 1 skipped` |
| TSLA 严格复跑 | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_layer_audit_summary.json` |

TSLA 交易审计：

| 字段 | 值 |
| --- | --- |
| `entry_date` | `2026-06-09 17:16:00+00:00` |
| `entry_level` | `1` |
| `entry_layer` | `activity` |
| `core_shares_before` | `0.0` |
| `activity_shares_before` | `697.792417005608` |
| `buy_ratio` | `0.275` |
| `total_return` | `-0.60%` |
| `buy_hold` | `-3.72%` |
| `max_drawdown` | `1.18%` |

结论：当前资金层已能证明这笔 TSLA 交易是 L1 活动仓，而不是被 30m 背景错误提升成核心仓。后续应继续把 `activity` 拆成 5m 波段层和 1m 短差层。

## 106. 第一百一十六轮 5m Swing 与 1m Scalp 分层

本轮把上一轮的 `activity` 桶拆成两个操作层：

- `swing_signal_level`：5m 波段层；`signal_source=upgrade` 时 `1m->5m` 自动映射为 L1；
- `scalp`：低于 `swing_signal_level` 的 1m 短差层。

实现：

- `portfolio_backtest` 新增 `swing_signal_level`；
- `live_backtest` 新增 CLI：`--swing-signal-level`，默认 `0` 表示自动推断；
- summary 新增 `swing_signal_level`；
- 持仓记录新增 `swing_shares`、`scalp_shares`；
- `PTrade` 导出新增 `swing_shares_before`、`scalp_shares_before`；
- L0 卖点只卖 `scalp`；L1 卖点卖 `swing + scalp`；L2 卖点或 30m down 才处理核心仓。

验证：

| 项目 | 结果 |
| --- | --- |
| 新测试 | `test_portfolio_backtest_scalp_sell_does_not_sell_swing_layer` |
| 更新测试 | `test_portfolio_backtest_core_signal_level_exits_trend_core` 验证 `core_swing -> swing -> core_all` |
| 单文件回归 | `77 passed` |
| 相关回归 | `186 passed, 1 skipped` |
| TSLA 严格复跑 | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_swing_scalp_summary.json` |

TSLA 交易审计：

| 字段 | 值 |
| --- | --- |
| `core_signal_level` | `2` |
| `swing_signal_level` | `1` |
| `entry_level` | `1` |
| `entry_layer` | `swing` |
| `core_shares_before` | `0.0` |
| `swing_shares_before` | `697.792417005608` |
| `scalp_shares_before` | `0.0` |
| `entry_date` | `2026-06-09 17:16:00+00:00` |

收益审计不变：组合收益 `-0.60%`，买持 `-3.72%`，最大回撤 `1.18%`，交易 `1`，信号事件 `2`。信号时间仍为 L1 `3buy` 在 `2026-06-09 17:15:00+00:00` 可见，下一根 `17:16` 成交。

结论：当前资金执行层已形成 30m core、5m swing、1m scalp 的可审计雏形；下一步需要让图表和回测报告也按这三层分别展示持仓、买卖点和背驰。

## 107. 第一百一十七轮 可视化层级审计报告

本轮把严格回放的信号/交易 CSV 叠加到 TSLA 多级别结构图中，形成可直接复核的 HTML 报告。

实现：

- `scripts/render_chanlun_visual_audit.py` 新增 `--summary`、`--trades`、`--signals`；
- 1m 面板叠加：
  - `visible_time` 竖线；
  - `next_fill_time` 成交点；
  - `entry_layer`/`exit_layer` 交易标记；
- 页面顶部新增 Strict Replay Metrics、Layer Trades、Signal Visibility Audit 三个审计表；
- 面板统计新增 Overlay Signals 与 Overlay Trades。

产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_swing_scalp.html` | TSLA 1m/5m/30m 多级别结构 + 实盘回放层级 overlay |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_swing_scalp_summary.json` | 严格回放 summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_swing_scalp_trades.csv` | 分层交易明细 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_swing_scalp_signals.csv` | 信号可见时间审计 |

浏览器验证：

| 项目 | 结果 |
| --- | --- |
| 页面标题 | `Chanlun Visual Audit - TSLA.US` |
| SVG 面板数 | `3` |
| panel 数 | `3` |
| 1m overlay signals | `2` |
| 1m overlay trades | `1` |
| 文本包含 | `swing`、`2026-06-09 17:15:00+00:00`、`2026-06-09 17:16:00+00:00` |

结论：现在图表报告能同时证明三件事：结构图包含 1m/5m/30m 中枢、买卖点、背驰；交易属于 L1 swing 层；成交发生在信号可见后的下一根 bar，而不是静态锚点。

## 108. 第一百一十八轮 分层归因与策略问题定位

本轮新增 layer attribution 报告，把严格回放交易按资金层和操作级别拆开统计。它的用途不是追求单次回测最好看，而是在收益或回撤不达标时定位问题属于哪一层。

新增能力：

- `build_layer_attribution_report(summary_path, trade_path, min_trades=...)`；
- `write_layer_attribution_report(..., output_markdown=...)`；
- `render_layer_attribution_markdown(report)`；
- 分组维度：`entry_layer`、`entry_level`、`exit_layer`；
- 指标：交易数、样本状态、胜率、平均收益、复利收益、最大回撤、平均持仓小时；
- 建议：`watch`、`reduce_layer_risk`、`keep_or_boost`、`keep_watch`。

产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_swing_scalp_layer_attribution.json` | 机器可读分层归因 |
| `D:/chanlun_pro/reports/tsla_swing_scalp_layer_attribution.md` | 人工复核报告 |

当前 TSLA 严格窗口结论：

| 字段 | 值 |
| --- | --- |
| `trade_count` | `1` |
| `signal_event_count` | `2` |
| `core_signal_level` | `2` |
| `swing_signal_level` | `1` |
| `entry_layer` | `swing` |
| `entry_level` | `1` |
| `avg_return` | `-2.19%` |
| `sample_state` | `thin` |
| `layer_guidance` | `watch` |

验证：

| 项目 | 结果 |
| --- | --- |
| 新增测试 | `test_layer_attribution_summarizes_layers_levels_and_guidance` |
| 优化器测试 | `36 passed` |
| 相关回归 | `222 passed, 1 skipped` |

策略含义：

- 这笔亏损只能说明当前窗口内 L1 swing 样本太少且单笔亏损，不能据此后验取消 5m swing 层；
- 后续优化必须先扩大严格无未来样本，再按 core/swing/scalp 逐层比较；
- 若长期归因显示 L1 swing 负期望，优先降低该层 `buy_ratio` 或提高 1m 级联确认要求，而不是放宽 30m 同级别分解；
- 若 L2/30m 核心仓长期样本稀缺，应审计走势类型完成规则和三买三卖确认滞后，不应交易重叠候选来制造信号。

## 109. 第一百一十九轮 级联确认滞后审计

本轮新增级联确认滞后报告，专门把买卖点的三个时间拆开：

- `anchor_time`：最终结构图上的买卖点锚点；
- `visible_time`：实盘逐根重算后首次可见的时间；
- `next_fill_time`：信号可见后下一根 1m bar 的成交时间。

新增脚本：

| 文件 | 说明 |
| --- | --- |
| `scripts/audit_cascade_confirmation_tsla.py` | 从严格回放 signals CSV 和原始 1m chart-cache 生成锚点/可见点/成交点审计 |

产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.json` | 结构化审计 |
| `D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md` | Markdown 审计 |

TSLA 结果：

> 历史口径说明：本表来自 v6 连续 signal registry 修正前的短窗级联审计，只保留用于说明“锚点不可提前成交”的滞后问题；旧 L1 `3sell` 在 v6 中被识别为窗口前已首次出现的历史信号，不能作为最终实盘回测退出信号。

| 信号 | 锚点 | 可见 | 成交 | 滞后 |
| --- | --- | --- | --- | ---: |
| L1 `3sell` | `2026-05-29 16:24:00+00:00` | `2026-06-08 19:53:00+00:00` | `2026-06-08 19:54:00+00:00` | `2549` 根 1m |
| L1 `3buy` | `2026-06-08 18:33:00+00:00` | `2026-06-09 17:15:00+00:00` | `2026-06-09 17:16:00+00:00` | `312` 根 1m |

截断复算证据：

| 信号 | anchor_time | visible 前一根 | visible_time |
| --- | --- | --- | --- |
| L1 `3sell` | 不存在 | 不存在 | 存在 |
| L1 `3buy` | 不存在 | 不存在 | 存在 |

结构解释：

- L1 `3buy` 是三类买点：向上离开中枢后，第一次低级别回试段结束且不破 `ZG`；
- 在 `anchor_time=2026-06-08 18:33` 时，最终图上的回试锚点已经可以被事后看见，但当时线段/下级走势并未完成确认，因此严格实盘不能交易；
- 到 `visible_time=2026-06-09 17:15` 时，XD 数从 `877` 增至 `878`，L1 买卖点数从 `22` 增至 `23`，该信号才进入可交易事件流；
- 组合层在 `2026-06-09 17:16` 下一根开盘成交，属于 L1 `swing` 仓。

策略约束：

- 以后所有回测、可视化和归因报告必须保留 `anchor_time`、`visible_time`、`next_fill_time`；
- 级联分析只能让低级别“自身已经可见”的买卖点管理小仓位，不能提前交易高级别锚点；
- 30m/L2 信号继续控制核心仓；5m/L1 信号控制 swing；1m/L0 信号控制 scalp；
- 若要进一步降低滞后，应优化低级别确认和仓位响应，而不是让高一级信号提前可见。

## 110. 第一百二十轮 L0/1m Scalp 信号接入严格 Upgrade 回放

本轮把 1m/L0 线段级买卖点正式接入 `signal_source=upgrade` 的严格 walk-forward 信号流，使 `scalp` 不再只是组合层的空桶。

新增参数：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--include-l0-upgrade-signals` | `False` | 在 `upgrade` 信号源中并入 L0 线段级买卖点 |
| `include_l0_upgrade_signals` | `False` | Python API 参数，同 CLI |

实现约束：

- L1/L2 仍来自 `get_kuozhan_levels()`；
- L0 来自 `get_branch_bspoints(use_xd=True)`，只取 `level == 0`；
- 仍由 `_walk_forward_signals_by_main_bar()` 逐根收集，不能使用最终全序列锚点；
- 信号缓存 meta 包含 `include_l0_upgrade_signals`；
- summary 输出该字段，便于区分历史报告；
- L0 买点进入 `entry_layer=scalp`，L0 卖点只卖 `scalp`。

新增测试：

| 测试 | 覆盖 |
| --- | --- |
| `test_build_symbol_walk_forward_can_include_l0_upgrade_scalp_signals` | 开启参数后 L0/L1/L2 同入 `small_by_bar`，L1 仍单独进入 `mid_by_bar`，L2 仍控制 `big_dir_at` |
| `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` | 同时验证 `include_l0_upgrade_signals` 从 `run_backtest` 传入加载层 |

TSLA 严格复跑：

| 字段 | 值 |
| --- | --- |
| summary | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_summary.json` |
| trades | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_trades.csv` |
| signals | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_signals.csv` |
| visual | `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_include_l0.html` |
| layer attribution | `D:/chanlun_pro/reports/tsla_include_l0_layer_attribution.md` |
| `include_l0_upgrade_signals` | `true` |
| `signal_event_count` | `29` |
| `trade_count` | `2` |
| `total_return` | `-0.52%` |
| `buy_hold` | `-3.72%` |
| `excess` | `+3.20%` |
| `max_drawdown` | `0.80%` |

交易结果：

| 入场 | 出场 | 层级 | 收益 | 退出原因 |
| --- | --- | --- | ---: | --- |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp` | `+1.42%` | L1 `3sell` |
| `2026-06-09 14:20` | `2026-06-09 17:16` | `scalp` | `-4.25%` | L0 `3sell` |

浏览器验证：

| 项目 | 结果 |
| --- | --- |
| 页面标题 | `Chanlun Visual Audit - TSLA.US` |
| SVG 面板数 | `3` |
| panel 数 | `3` |
| 页面包含 | `L0`、`scalp`、`2026-06-08 15:07:00+00:00`、`2026-06-09 17:16:00+00:00` |

注意事项：

`2026-06-09 17:15` 同时可见 L1 `3buy` 与 L0 `3sell`。当前撮合在下一根 `17:16` 先按 L0 卖点退出 scalp，没有同 bar 转为 L1 swing。下一轮应专门审计并实现“低级别卖点与高级别买点同时出现”的级别优先策略，例如先结清 scalp，再按 L1 买点是否仍满足仓位/方向条件建立 swing。

## 111. 第一百二十一轮 同 bar L0 卖点与 L1 买点的级别滚动

本轮修正组合层撮合顺序中的一个原文一致性问题：已有 L0/scalp 仓位时，如果同一可见 bar 同时出现 L0 卖点和更高级别 L1 买点，旧逻辑会因为 pending sell 阻止同标的开仓扫描，导致下一 bar 只卖不买。严格无未来性没有问题，但级别联立执行不完整。

实现：

| 变更 | 说明 |
| --- | --- |
| `_build_open_buy_candidate()` | 抽出普通开仓候选构造，滚动开仓与普通开仓共用同一套门控和仓位比例 |
| 同 bar 滚动规则 | 非核心、非 big-down 强制卖出，且卖点会全平当前可卖层时，若同 bar 有更高级别买点，追加下一 bar 买单 |
| 级别约束 | 仅允许 `buy_level > exit_level`，避免同级别买卖点互相覆盖 |
| 成交顺序 | pending 中 sell 在 buy 前，下一 bar 先平旧层，再建更高级别仓位 |

新增测试：

| 测试 | 覆盖 |
| --- | --- |
| `test_portfolio_backtest_same_bar_scalp_sell_rolls_into_higher_level_buy` | L0 scalp 在同 bar L0 三卖 + L1 三买后，下一 bar 先出 scalp，再进 L1 swing |

验证：

| 项目 | 结果 |
| --- | --- |
| `pytest tests/test_backtest_live_parity.py -q` | `79 passed` |
| HTML 标题 | `Chanlun Visual Audit - TSLA.US` |
| SVG / panel | `3` / `3` |
| 页面包含 | `L0`、`L1`、`scalp`、`swing`、`2026-06-09 17:16:00+00:00` |
| 浏览器 console | `0` warnings/errors |

TSLA 严格复跑：

| 字段 | 值 |
| --- | --- |
| summary | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_summary.json` |
| trades | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_trades.csv` |
| signals | `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_signals.csv` |
| visual | `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_include_l0.html` |
| layer attribution | `D:/chanlun_pro/reports/tsla_include_l0_layer_attribution.md` |
| `signal_event_count` | `29` |
| `trade_count` | `3` |
| `total_return` | `-1.11%` |
| `buy_hold` | `-3.72%` |
| `excess` | `+2.61%` |
| `max_drawdown` | `1.30%` |

最新交易：

| 入场 | 出场 | 层级 | 收益 | 说明 |
| --- | --- | --- | ---: | --- |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp -> swing exit` | `+1.42%` | L0 二买短差由 L1 三卖退出 |
| `2026-06-09 14:20` | `2026-06-09 17:16` | `scalp -> scalp exit` | `-4.25%` | L0 三卖退出短差 |
| `2026-06-09 17:16` | `2026-06-10 19:59` | `swing -> all` | `-2.19%` | 同一可见 bar 的 L1 三买转入 swing，窗口末强平 |

分层归因更新：

| Entry Layer | Trades | Win Rate | Avg Ret | Max DD | Guidance |
| --- | ---: | ---: | ---: | ---: | --- |
| `scalp` | `2` | `50.0%` | `-1.41%` | `4.25%` | `keep_watch` |
| `swing` | `1` | `0.0%` | `-2.19%` | `2.19%` | `reduce_layer_risk` |

结论：

- 当前问题定位为组合撮合层的级别优先规则缺口，不是未来信号问题；
- 修正后仍保持 `anchor_time -> visible_time -> next_fill_time` 三分离；
- L1 三买没有按 `2026-06-08 18:33` 锚点提前交易，而是在 `2026-06-09 17:15` 首次可见后，于 `17:16` 成交；
- 如果后续收益或回撤仍不达标，应继续审计 L1 swing 负期望是否来自走势类型确认、仓位比例或退出规则，而不是回退到静态全量信号回测。

## 112. 第一百二十二轮 30m 同级别分解审计报告

本轮把“30m 同级别分解，30m 以下非同级别分解”从代码注释升级为可复跑报告。核心问题是：不能只把递归 L2 命名为 30m，而实际仍按扩展/扩张一路升级；30m 层必须按原文同级别分解，用低一级走势类型的三段重合形成中枢。

代码合同：

| Base | Upgrade Chain |
| --- | --- |
| `1m` | `5m: kuozhan` → `30m: tongjibie` |
| `5m` | `30m: tongjibie` |
| `30m` | 无升级链，展示本级 30m 结构 |

新增报告：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.json` | 机器可读候选/入选/信号审计 |
| `D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.md` | 人工复核 Markdown |

TSLA 1m → 5m → 30m 结构审计：

| 指标 | 值 |
| --- | ---: |
| 原始 1m bars | `96949` |
| L1/5m `kuozhan` 中枢 | `66` |
| 30m 同级别交替段 | `16` |
| 三段重合候选 | `7` |
| 入选非重叠组 | `4` |
| L2/30m `tongjibie` 中枢 | `4` |
| L2/30m 信号 | `2` |

入选 30m groups：

| Group | Dirs | ZD | ZG |
| --- | --- | ---: | ---: |
| `0-2` | `DUD` | `314.750` | `330.110` |
| `5-7` | `UDU` | `330.400` | `340.550` |
| `8-10` | `DUD` | `422.120` | `451.460` |
| `13-15` | `UDU` | `406.390` | `412.430` |

候选筛选：

| Candidate | 结果 |
| --- | --- |
| `0-2` | selected |
| `1-3` | overlaps selected prefix |
| `2-4` | overlaps selected prefix |
| `5-7` | selected |
| `7-9` | overlaps selected prefix |
| `8-10` | selected |
| `13-15` | selected |

这证明 30m 采用的是前缀唯一的同级别分解，而不是把所有事后重叠候选同时拿来交易。L2/30m 只产生两条结构信号：`2025-11-24 17:40` 的 `3buy` 和 `2026-04-17 18:48` 的 `3sell`；当前 2026-06-08 至 2026-06-10 窗口内交易主要由 L0/L1 管理，30m 层提供背景和核心仓约束。

验证项：

| 测试/命令 | 结果 |
| --- | --- |
| `test_original_level_ladder_contract_uses_30m_same_level_decomposition` | 锁定升级链 |
| `test_tongjibie_6_segments_two_zs_not_extended` | 锁定 30m 同级别不延伸 |
| `python scripts/audit_tongjibie_tsla.py ...` | `l1=66 segs=16 candidates=7 chosen=4 l2=4 signals=2` |

结论：结构路径已经符合用户要求的级别分工。若收益/回撤仍不达标，后续应继续从 30m 方向状态、L1/L0 级联确认、仓位比例与退出规则定位问题；不能用“提前交易高级别锚点”或“30m 继续扩展化”来优化曲线。

## 113. 第一百二十三轮 三买结构失效过滤

本轮修复“可见但不可执行”的滞后信号问题。严格 walk-forward 已经保证不使用未来信号，但高级别三买可能在最终结构上锚得很早，等到真正可见并准备下一根成交时，价格已经跌破三买对应中枢的 `ZG`。这种情况下信号仍可作为图表历史结构存在，但不能作为实盘买点成交。

新增字段：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `zs_zd` | `BuySellPoint.zs.zd` | 关联中枢下沿 |
| `zs_zg` | `BuySellPoint.zs.zg` | 关联中枢上沿 |
| `structural_stop_below` | `3buy -> ZG` | 买入成交价跌破则取消 |
| `structural_stop_above` | `3sell -> ZD` | 卖出/做空侧结构边界 |

实现：

- `Signal` dataclass 携带结构边界；
- `collect_signals()`、`collect_branch_signals()` 从买卖点对象提取 `ZD/ZG`；
- signals CSV 导出结构字段；
- signal cache version 升级为 `v3`，本轮 TSLA 回放重新扫描，`hits=0, misses=1`；
- `portfolio_backtest()` 在候选生成阶段检查当前可见收盘价；
- pending buy 在下一根开盘成交前再次检查开盘价，若已跌破三买 `ZG` 则取消。

新增测试：

| 测试 | 覆盖 |
| --- | --- |
| `test_portfolio_backtest_cancels_buy_when_fill_breaks_structural_boundary` | 三买信号可见后，下一根开盘低于 `structural_stop_below`，不产生交易 |

TSLA 关键取消案例：

| Level | Signal | Anchor | Visible | Fill | Open | ZG | 结果 |
| ---: | --- | --- | --- | --- | ---: | ---: | --- |
| `1` | `3buy` | `2026-06-08 18:33` | `2026-06-09 17:15` | `2026-06-09 17:16` | `390.12` | `405.63` | cancel |

严格回放结果：

| 指标 | 值 |
| --- | ---: |
| `signal_event_count` | `29` |
| `trade_count` | `2` |
| `total_return` | `-0.52%` |
| `buy_hold` | `-3.72%` |
| `excess` | `+3.20%` |
| `max_drawdown` | `0.80%` |

交易结果：

| 入场 | 出场 | 层级 | 收益 | 退出 |
| --- | --- | --- | ---: | --- |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp` | `+1.42%` | L1 `3sell` |
| `2026-06-09 14:20` | `2026-06-09 17:16` | `scalp` | `-4.25%` | L0 `3sell` |

新增审计报告：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_trade_invalidation_audit.json` | 机器可读结构失效审计 |
| `D:/chanlun_pro/reports/tsla_trade_invalidation_audit.md` | 人工复核结构失效报告 |

审计发现：

- L1 swing 亏损被取消，因为成交开盘已经低于三买 `ZG`；
- 现有两笔 L0/scalp 交易入场后仍发生结构边界跌破，下一步应研究“持仓后结构失效退出”，而不是继续调高买点仓位；
- 这一步没有提前使用锚点，也没有删除信号；信号仍在 CSV 和图表中，交易层只是不执行已失效买点。

## 114. 第一百二十四轮 持仓后结构失效退出

本轮把上一轮发现的“持仓后结构边界跌破”纳入组合撮合。第三类买点的原文结构条件不是一次性标签，而是持仓期间的有效性边界：三买关联中枢的 `ZG` 被跌破后，该买点失效，不能继续等待后续卖点才退出。

实现：

| 变更 | 说明 |
| --- | --- |
| `build_symbol_from_klines()` | 向组合层传入 `high/low` |
| position state | 保存 `structural_stop_below/above` |
| refill | 合并结构边界，买入侧采用更严格边界 |
| exit scan | 每根 bar 收盘后检查结构失效，下一根开盘退出 |
| reason | `structural_invalidation` |
| exit type | `structural_stop_below` / `structural_stop_above` |

新增测试：

| 测试 | 覆盖 |
| --- | --- |
| `test_portfolio_backtest_exits_next_bar_after_structural_invalidation` | 三买开仓后，当根低点跌破 `ZG`，下一 bar 开盘退出 |

TSLA 最新严格回放：

| 字段 | 值 |
| --- | ---: |
| `signal_event_count` | `29` |
| `trade_count` | `2` |
| `total_return` | `-0.04%` |
| `buy_hold` | `-3.72%` |
| `excess` | `+3.67%` |
| `max_drawdown` | `0.15%` |
| `sharpe` | `-4.31` |

交易：

| 入场 | 出场 | 层级 | 收益 | 退出原因 |
| --- | --- | --- | ---: | --- |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp` | `+1.42%` | L1 `3sell` |
| `2026-06-09 14:20` | `2026-06-09 14:47` | `scalp` | `-0.78%` | `structural_invalidation` |

结构失效审计：

| Trade | Signal | Boundary | First Break | Exit | Ret |
| ---: | --- | ---: | --- | --- | ---: |
| `0` | L0 `2buy` | `388.590` | none | L1 `3sell` | `+1.42%` |
| `1` | L0 `3buy` | `404.405` | `2026-06-09 14:46` | `2026-06-09 14:47` | `-0.78%` |

报告已刷新：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_summary.json` | 最新严格 summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_trades.csv` | 最新交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_signals.csv` | 带结构边界的信号审计 |
| `D:/chanlun_pro/reports/tsla_trade_invalidation_audit.md` | 结构失效审计 |
| `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_include_l0.html` | 图表报告 |
| `D:/chanlun_pro/reports/tsla_include_l0_layer_attribution.md` | 分层归因 |

浏览器验证：

| 项目 | 结果 |
| --- | --- |
| 标题 | `Chanlun Visual Audit - TSLA.US` |
| SVG / panel | `3` / `3` |
| 页面包含 | `structural_invalidation`、`2026-06-09 14:47:00+00:00`、`2026-06-09 17:15:00+00:00`、`scalp` |
| console | `0` warnings/errors |

结论：级联分析现在不仅用于“低级别先入场、高级别确认后升级”，也用于“买点结构失效后及时退出”。这更接近实盘缠论：所有成交仍在可见信息之后，但持仓不再无条件等待后续卖点。

## 115. 第一百二十五轮 二买/二卖结构边界正式进入信号

本轮处理 L0 `2buy` 失效边界的原文一致性。二买不是“二买锚点不破”，而是“一买后反弹，再回调不破一买前低”。因此实盘信号必须把一买前低作为 `2buy.structural_stop_below` 导出；二卖对称导出一卖前高。

实现：

| 位置 | 变更 |
| --- | --- |
| `BuySellPoint` | 新增 `structural_stop_below/above` |
| `BsBranchCalculator.second_class()` | 单级别 `2buy/2sell` 携带一类点极值 |
| `Bs2BranchCalculator.calculate()` | 跨级别二买/二卖携带本级别一类点极值 |
| `_structural_signal_fields()` | 显式结构边界优先，之后才回退到锚点或 `ZG/ZD` |
| `live_backtest.py` | signal cache version `v4`，强制重新逐根扫描 |
| `audit_trade_invalidation_tsla.py` | 审计优先使用 signals CSV 的 `structural_stop_*` |

TSLA 复核：

| Signal | Visible | Fill | Boundary | First Break | Exit | Ret |
| --- | --- | --- | ---: | --- | --- | ---: |
| L0 `2buy` | `2026-06-08 15:06` | `2026-06-08 15:07` | `388.590` | none | L1 `3sell` | `+1.42%` |
| L0 `3buy` | `2026-06-09 14:19` | `2026-06-09 14:20` | `404.405` | `2026-06-09 14:46` | `2026-06-09 14:47` | `-0.78%` |

严格 replay：

| 指标 | 值 |
| --- | ---: |
| `signal_cache_stats` | `hits=0, misses=1, writes=1, entries=29` |
| `signal_event_count` | `29` |
| `trade_count` | `2` |
| `total_return` | `-0.04%` |
| `buy_hold` | `-3.72%` |
| `excess` | `+3.67%` |
| `max_drawdown` | `0.15%` |

验证：

| 项目 | 结果 |
| --- | --- |
| target tests | `108 passed` |
| visual HTML | 3 个 SVG 面板，包含 1m/5m/30m 与 `structural_invalidation` |
| console | 仅 `favicon.ico` 404，无页面脚本错误 |

结论：级联分析解决滞后问题的关键不是提前拿未来高级别锚点交易，而是把每一级买卖点的成立条件变成实盘状态字段。二买/二卖边界正式进入信号后，TSLA 第一笔二买不再被错误判定为跌破 `403.410`，而是按原文用一买前低 `388.590` 审计。

## 116. 第一百二十六轮 长窗口 no-future 与完整历史状态区分

本轮增加回测报告的口径自证字段，解决一个容易误判的问题：`walk_forward` 只能证明“不使用未来 bar”，但如果 `signal_warmup_bars` 是正数，缠论状态仍是有限历史初始化，不等于完整历史状态。对于缠论这种中枢/走势类型会受长期结构影响的系统，最终实盘口径必须能区分这两者。

执行口径同步修正：`signal_warmup_bars=-1` 表示完整既往历史状态，此时执行器会强制使用连续扫描，不再按 `signal_scan_chunk_bars` 分块重放。原因是完整历史模式本来就应模拟一个实盘 `CL` 对象从既往历史一路更新到当前；分块从历史起点反复重放虽不引入未来信号，但计算成本高，也不如连续状态贴近实盘。若命令仍传入分块参数，summary 会记录 `requested_signal_scan_chunk_bars`，但实际 `signal_scan_chunk_bars=0`、`chunked_signal_scan=false`。

当前严格回放的主要工程瓶颈是 `CL.process_klines` 中高一级 MACD 与递归结构存在全量重算路径。这个瓶颈只能通过增量 MACD/结构缓存解决，不能通过预先全量计算所有买卖点再映射回历史交易来规避；后者会重新变成未来函数口径。本轮已完成 v5/v6 性能修复：高一级 MACD 增量化、笔/线段无变化跳过、upgrade 回放跳过不消费的 legacy zslx 路径、单根 K 线 fast path，并把信号缓存版本提升到 `v6`。

实测记录更新：TSLA 长窗完整历史连续 replay（`2026-04-14 16:00` 到 `2026-06-10 16:00`、`signal_warmup_bars=-1`）此前运行到约 `2131s` CPU 仍未写出 summary/trades/signals；v5 修复后已落盘 policy 版报告：`D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_fullhistory_incremental_v5_policy_upgrade_l0min3_include_l0_summary.json`。结果为收益 `+15.50%`、买持 `+5.65%`、最大回撤 `3.09%`、交易 `11`、信号 `172`、胜率 `45.45%`、结构失效退出 `3`。

同时补一个更细的实盘口径区分：`signal_warmup_bars=-1` 让 CL 结构状态使用全部前置 K 线，但如果回测 `start` 晚于原始缓存第一根，信号首次可见去重表也必须从原始数据第一根连续滚动，否则历史上出现又消失的右边缘信号可能在交易窗口内二次触发。summary 新增 `signal_seen_registry`、`signal_seen_registry_complete`、`stale_reappearing_signal_risk`。v6 通过 `emit_start_idx` 实现“交易起点前只登记 seen_keys，不输出交易事件”。

新增 summary 字段：

| 字段 | 含义 |
| --- | --- |
| `no_future_policy.strict_no_future` | 是否按 walk-forward 决策 |
| `anchor_time_tradeable` | 永远为 `false`，锚点只作结构归属 |
| `decision_time` | `visible bar close` |
| `execution_time` | `next bar open` |
| `history_state` | `full_prior_history` 或 `bounded_warmup` |
| `history_state_complete` | `signal_warmup_bars < 0` 才为 `true` |
| `chunked_signal_scan` | 是否分块扫描 |
| `signal_seen_registry` | 信号首次可见去重表的初始化口径 |
| `signal_seen_registry_complete` | 是否从原始数据第一根连续滚动信号去重表 |
| `stale_reappearing_signal_risk` | 历史右边缘信号消失后在窗口内重现的风险 |
| `warning` | bounded warmup 时提示它可能不同于完整历史 replay |

TSLA 长窗 bounded-warmup 结果：

| 指标 | 值 |
| --- | ---: |
| 窗口 | `2026-04-14 16:00` 到 `2026-06-10 16:00` |
| `signal_warmup_bars` | `1200` |
| `signal_scan_chunk_bars` | `3000` |
| `history_state_complete` | `false` |
| 信号 | `127` |
| 交易 | `13` |
| 收益 | `+17.30%` |
| 买持 | `+5.65%` |
| 超额 | `+11.65%` |
| 最大回撤 | `6.16%` |
| 结构失效退出 | `2` |

TSLA 长窗 full-prior-history policy 版结果：

| 指标 | 值 |
| --- | ---: |
| 窗口 | `2026-04-14 16:00` 到 `2026-06-10 16:00` |
| `signal_warmup_bars` | `-1` |
| `history_state_complete` | `true` |
| `signal_seen_registry_complete` | `false` |
| 信号 | `172` |
| 交易 | `11` |
| 收益 | `+15.50%` |
| 买持 | `+5.65%` |
| 超额 | `+9.85%` |
| 最大回撤 | `3.09%` |
| 结构失效退出 | `3` |

TSLA 短窗真正连续 registry v6 结果：

| 指标 | 值 |
| --- | ---: |
| 窗口 | `2026-06-08 13:30` 到 `2026-06-10 16:00` |
| registry 扫描起点 | `2025-06-10 16:00` |
| `emit_start_idx` | `96409` |
| `signal_seen_registry_complete` | `true` |
| `stale_reappearing_signal_risk` | `false` |
| 信号 | `28` |
| 交易 | `1` |
| 收益 | `-1.43%` |
| 买持 | `-2.54%` |
| 最大回撤 | `2.68%` |
| 结构失效退出 | `1` |
| cache-hit 复跑 | 约 `5s` |

重叠区差异：

| 口径 | `2026-06-08` 交易处理 |
| --- | --- |
| 完整历史短窗 `signal_warmup_bars=-1` | L0 `2buy` 在 `15:07` 入场，`19:54` 被 L1 `3sell` 退出，但该 L1 `3sell` 在长窗中已于 `2026-05-29 17:25` 首次可见，短窗存在 stale reappearing 风险 |
| 长窗 bounded warmup `1200/chunk=3000` | 同一 L0 `2buy` 持有到 `2026-06-09 16:18`，跌破 `388.590` 后结构失效退出 |
| 长窗 full-prior-history policy v5 | 同一 L0 `2buy` 也持有到 `2026-06-09 16:18`，按 `structural_stop_below=388.590` 结构失效退出 |
| 短窗 continuous-registry v6 | 同一 L0 `2buy` 持有到 `2026-06-09 16:18`，按 `structural_stop_below=388.590` 结构失效退出；旧 L1 `3sell` 不再二次触发 |

结论：长窗 bounded-warmup 可作为无未来压力测试，不能作为最终实盘证明。v5 full-prior-history 已能完成长窗严格回放；v6 进一步解决短窗 stale reappearing 问题，凡是 `signal_seen_registry_complete=true` 的报告才可称为真正连续 registry 的实盘口径。后续扩大样本时必须保留 v6 registry cache，不能回到 trade-window-start 初始化。

## 第七十轮 实时信号新鲜判定修复(全天零事件根因,实战闭环三类一致性补全)

2026-06-12 收盘后诊断 A 股 56 只池全天零事件:重建候选池状态发现当日有 **12 只标的出现「今日 1m 买点 + 30m up + 5m up」**——全部应触发事件却一个未发,US 首晚零事件同因。这不是低频,是链路 bug。

**根因**(commit 7e30ca32):实时新鲜判定 `sig.date == self.last_op` 要求信号确认 bar 恰好等于最新 bar(零滞后)。但买卖点的 `anchor_fx.k.date` 是确认 bar,**首次可见时刻滞后确认 bar 若干根**(分型/笔需要右侧结构,既有实证:中位 9 bar / p90=20 bar)——collect_branch_signals 首次输出该信号时 last_op 已前进数根,等式永不成立,信号被加入 seen 后静默吞掉。回测不受影响(small_by_bar 全量计算按 date 索引);纯实时层缺陷。

**修复**:判定改为「本轮新出现 + 确认 bar 在新鲜窗口内」(窗口=30 个 op bar:1m→30 分钟,5m→150 分钟;超窗的新信号视为深度回溯修正不发)。监控与 paper 旧循环同修;warmup 语义不变(首轮全量登记不触发);滞后到达按当前价撮合,真实滑点原样体现(右边缘幻影已有 -1.6%/季审计预算)。新增三个测试钉死(滞后 8 分钟首见必须发出/滞后 120 分钟丢弃/已见不重复),全量 `893 passed, 1 skipped`。

**实战闭环的三类一致性至此补全**:
1. 撮合一致性(§74-76):涨跌停昨收口径、ST、T+1、停牌;
2. 时效一致性(§第六十八轮):扫描-通知延迟与操作级 bar 周期同量级;
3. **信号可达性一致性(本轮)**:回测里按 date 索引可见的信号,实时端必须在其首次确认时真正发出——"回测有信号、实盘永远等不到"是最隐蔽的一类偏差,且只有拿真实盘面做端到端对照(诊断脚本重建当日候选状态)才能发现。

## 第七十一轮 原文三段中枢默认口径与 v7 registry replay

本轮把 L0 中枢默认口径从 legacy 4 段确认门控切到原文三段本体：三段次级别走势类型重叠定义中枢，下一段只用于实盘确认 `visible_time`。实现上，`DEFAULT_RECURSIVE_L0_MIN_ZS_LINES=3`，`CL_CFG.recursive_l0_min_zs_lines=3`，`_cl_config()` 显式写入该值，信号缓存升级到 `v7` 且 cache meta 永远包含 `recursive_l0_min_zs_lines`。

新增证据资产：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/chanlun_original_index.json` | 全量原文证据索引：正文 `20728` 段、图片 `1061` 张、图表锚点 `1100` 段、回复类锚点 `2399` |
| `D:/chanlun_pro/reports/chanlun_original_logic_matrix.md` | 原文逻辑覆盖矩阵，当前 gap count = `0` |
| `tests/core/test_zs_branch.py::test_calculator_min3_confirms_three_segment_center_on_next_leave` | 三段中枢本体 + 下一段确认可见的行为锁定 |

TSLA v7 严格 replay：

| 指标 | 值 |
| --- | ---: |
| 窗口 | `2026-06-08 13:30` → `2026-06-10 16:00` |
| `recursive_l0_min_zs_lines` | `3` |
| `signal_warmup_bars` | `-1` |
| `signal_seen_registry_complete` | `true` |
| `stale_reappearing_signal_risk` | `false` |
| 信号事件 | `28` |
| L1 `3sell` | `0` |
| 交易 | `1` |
| 总收益 | `-1.43%` |
| 买持 | `-2.54%` |
| 最大回撤 | `2.68%` |
| 基准回撤 | `8.34%` |
| cache-hit | `hits=1, misses=0, writes=0` |

v7 产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_hit_summary.json` | cache-hit summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_hit_trades.csv` | cache-hit trades |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_hit_signals.csv` | cache-hit signals |
| `D:/chanlun_pro/reports/tsla_wffull_window_v7_registry_trade_invalidation_audit.md` | 结构失效审计 |
| `D:/chanlun_pro/reports/tsla_wffull_window_v7_registry_layer_attribution.md` | 分层归因 |
| `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_v7_registry.html` | v7 多级别图表审计 HTML |
| `D:/chanlun_pro/browser_verify/v7_visual_full.png` | 浏览器全页截图 |
| `D:/chanlun_pro/browser_verify/v7_visual_1m_panel.png` | 1m 面板截图 |
| `D:/chanlun_pro/browser_verify/v7_visual_5m_panel.png` | 5m 面板截图 |
| `D:/chanlun_pro/browser_verify/v7_visual_30m_panel.png` | 30m 面板截图 |

图表审计契约已用浏览器复核：1m 面板显示笔、1m/5m/30m 中枢、买卖点、背驰与 28 个 strict replay overlay 信号；5m 面板显示笔、5m/30m 中枢、买卖点、背驰；30m 面板显示笔、30m 中枢、买卖点、背驰。DOM 检查显示三个 SVG 分别为 `1362x458`，图元数为 `2170/18690/1833`，像素检查显示 1m/5m/30m 面板非白像素占比约 `9.61%/16.08%/13.49%`，不是空图。

结论：v6 解决了旧信号重现，v7 进一步把原文三段中枢本体设为默认实盘口径，并补齐 1m/5m/30m 展示证据。后续优化收益/回撤必须基于 v7；不能再以 legacy 4 段默认、批量预计算买卖点、或 trade-window-start 初始化 registry 的结果作为最终证据。

## 第七十二轮 原文交易体系矩阵与 v7 级联审计刷新

本轮新增 `scripts/audit_original_trading_system_matrix.py`，把 goal 拆成可执行证据矩阵，而不是只看收益曲线。输出：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/chanlun_original_trading_system_matrix.json` | 机器可读矩阵 |
| `D:/chanlun_pro/reports/chanlun_original_trading_system_matrix.md` | 人工复核矩阵 |

当前矩阵更新为：`6 pass / 1 partial / 1 gap`。

`pass` 项：原文全文/回复/图表索引、30m 同级别与 30m 以下级联、v7 无未来逐根回放、级联滞后控制、结构边界失效退出、多层仓位。

`partial` 项：三系统选股。A 股 selector 已有基本面+比价+技术面；但 TSLA/core-9 US 当前仍是 technical-only，不能说已经完成“完全基于原文的选股体系”。

多层仓位本轮从 `partial` 升级为 `pass`：新增 `--sell-ratio-policy original_layered`。旧 `all_out` 仍保留为基线；新 policy 在大级别 down 或核心级别卖点时全退，在大级别 up 时让小级别二/三卖只减活动层的一部分，避免把 1m/5m 的短差节奏错误地提升为 30m 核心仓清仓。

`gap` 项：v7 TSLA 短窗只有 `1` 笔交易，是无未来正确性基线，不足以证明通用、低回撤、高收益体系。

同时刷新 `scripts/audit_cascade_confirmation_tsla.py`：默认信号源改为 v7 registry hit signals，默认只审计 `L1+`。新的 `D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md` 只剩 L1 `3buy`，anchor `2026-06-08 18:33`，visible `2026-06-09 17:15`，next fill `2026-06-09 17:16`。旧报告里的 L1 `3sell` 是 stale reappearing，不再属于当前实盘证据链。

TSLA v7 分层卖出复跑产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_summary.json` | `sell_ratio_policy=original_layered` summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_trades.csv` | 分层卖出复跑交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_signals.csv` | 分层卖出复跑信号 |

该复跑 cache hit：`hits=1/misses=0/writes=0`，无未来字段仍为 `strict_no_future=true`、`signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false`。收益仍 `-1.43%`、买持 `-2.54%`、最大回撤 `2.68%`、交易 `1`。唯一退出是结构失效，按原文必须全退，因此分层 policy 不改变这笔交易。

本轮继续新增两个审计产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_selection_source_audit.md` | US/core9 三系统选股数据源审计 |
| `D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md` | 现有 summary 的无未来证明强度分级 |

US/core9 数据源审计：`SPY/QQQ/AAPL/MSFT/NVDA/AMZN/META/GOOGL/TSLA` 的 1m/5m/30m 技术 K 线缓存齐，technical=`pass`；但本地 US 基本面/行业地位/估值缓存覆盖 `0/9`，fundamental=`gap`，comparison=`gap`。因此 US/TSLA 当前不能宣称已完成原文“三个独立系统”选股，只能作为技术执行链和风控链证据。

稳健性证据审计：本地 `strict_original_registry` 报告 `5` 个，但最佳严格报告仅 `1` 笔交易；`bounded_warmup_walk_forward` `35` 个，`legacy_or_unknown` `50` 个，`strict_but_registry_incomplete` `6` 个。旧 core9 高交易数报告不再作为最终证明，只保留为研究参考。

工程尝试：`TSLA/QQQ/NVDA` 三标的、`2026-06-01~2026-06-10`、v7 full-history continuous registry + `original_layered` 首跑超过 `6` 分钟仍未落盘，已停止进程。下一轮若继续扩大回测，不应再围绕“是否有未来函数”反复证明；应把精力放在两个未完成体系层：US/TSLA point-in-time 基本面/比价数据源，以及可恢复的 v7 event-cache 预热机制，随后在更长 v7/core9 样本上验证 `original_layered` 对收益/回撤的影响。

## 第七十三轮 v7 严格信号缓存预热机制

本轮新增 `scripts/prewarm_live_backtest_signal_cache.py`，只解决一个工程问题：把严格 live-parity 信号扫描按标的隔离，并在每个标的完成后立即写 manifest。它不改变买卖点规则、不放松 `signal_warmup_bars=-1`、不启用未来信号，也不把 full-history continuous registry 切成伪 chunk。

预热脚本默认口径：

| 参数 | 值 |
| --- | --- |
| `signal_mode` | `walk_forward` |
| `signal_source` | `upgrade` |
| `include_l0_upgrade_signals` | `true` |
| `recursive_l0_min_zs_lines` | `3` |
| `signal_warmup_bars` | `-1` |
| `op/mid/big` | `1m/5m/30m` |
| `signal_cache_dir` | `D:/chanlun_pro/reports/live_backtest_signal_cache_v7_l0min3_registry` |

TSLA v7 已用该脚本做一次 cache-hit 预热验证：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_tsla_v7_registry_manifest.json` | 逐标的预热 manifest |

验证结果：`ok=1/1`，`registry_complete=1`，`events=28`，`cache(h/m/w)=1/0/0`，运行约 `0.746s`。这说明脚本可复用现有严格 v7 信号缓存；后续扩大到 core3/core9 时，可以逐标的续跑，失败后从已完成 manifest 继续，而不是一次性组合回测超时后全部丢失。

这仍不改变稳健性结论：当前 strict original-registry 最佳报告还是 `1` 笔交易，不能证明“通用低回撤高收益”。预热机制只是把严格样本扩容从“不可恢复的大任务”改成“可恢复的小任务”。

## 第七十四轮 full-history registry checkpoint

实际扩大 `TSLA/QQQ/NVDA` 到 `2026-06-01` 到 `2026-06-10` 时，暴露了更深一层的工程瓶颈：即使已经按标的预热，单个 TSLA 的 full-history first-seen registry 也要从 `2025-06-12 13:30` 一路扫描到交易窗口。未带 checkpoint 的后台预热在 TSLA `70000/96710` 处停止，错误日志为空，但没有 final cache/manifest；这说明瓶颈在单标的严格扫描内部，不在组合回测层。

本轮在 `src/chanlun/recursive_bt/live_backtest.py` 增加 opt-in checkpoint：

| 环境变量/CLI | 说明 |
| --- | --- |
| `CHANLUN_WF_CHECKPOINT_EVERY` / `--checkpoint-every` | 每 N 根主时钟 K 线写一次严格扫描 checkpoint |
| `CHANLUN_WF_CHECKPOINT_DIR` / `--checkpoint-dir` | checkpoint 输出目录；不填则放在 signal cache 旁边 |

checkpoint 保存的是完整 `CL` 对象、`row_pos`、`active_keys`、`seen_keys`、`ready`、`last_collect_sig`、`by_bar`、`initial_state` 与 `next_main_idx`。因此恢复后仍是同一个连续实盘扫描，不是 bounded warmup，也不是把历史拆块重算后拼接。

真实 TSLA 长窗验证：

| 文件/目录 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_checkpoints_v7_l0min3_registry_core3_202606/6ff7c746920fc72419678b3f50fec8ca.checkpoint.pkl` | 已写 checkpoint |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_prewarm.out.log` | 首次真实 checkpoint 到 `5000/96710` |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_resume.out.log` | 从 `5000` 恢复并推进到 `10000/96710` |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_advance.out.log` | 再次恢复并推进到 `15000/96710` |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_advance2.out.log` | 再次恢复并推进到 `25000/96710` |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_continue.out.log` | 再次恢复并推进到 `55000/96710` |
| `D:/chanlun_pro/reports/prewarm_logs/tsla_202606_v7_checkpoint_continue2.out.log` | 再次恢复并推进到 `60000/96710` |

后续续跑已完成 TSLA `2026-06-01` 到 `2026-06-10` 的 full-history strict signal cache：

| 文件 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_v7_l0min3_registry_core3_202606/6ff7c746920fc72419678b3f50fec8ca.pkl` | TSLA 长窗 strict signal cache |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_tsla_202606_v7_registry_manifest.json` | `ok=1/1`、`registry_complete=1`、`events=36`、`checkpoint_resumes=1`、`checkpoint_writes=7` |

cache-hit 复跑产物：

| 文件 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_summary.json` | `hits=1/misses=0/writes=0`、`signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false` |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_signals.csv` | 36 个严格可见信号 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_trades.csv` | 0 笔交易 |

TSLA 长窗严格结果：组合收益 `0.00%`，买持 `-9.64%`，最大回撤 `0.00%`，基准回撤 `11.68%`，交易 `0`。这不是收益优化证据，而是说明在当前原文规则、级别级联、仓位门槛和风控约束下，这段更长 TSLA 样本没有触发可执行买入。

测试新增覆盖：checkpoint 后崩溃再恢复，结果必须与一次性完整扫描一致；并且专门覆盖 `emit_start_idx` 之前的历史注册分支也会写 checkpoint。当前这一步仍只是让严格扩容变得可恢复，不是完成 core3/core9 稳健性证明。

### 第七十轮修复验证:实时闭环首批真实事件(2026-06-12 美股开盘)

修复后监控于开盘 5 分钟产生首个事件,全链路真实运转:

| 时刻 | 事件 |
| --- | --- |
| 21:35:23 | 首事件 events=1 sent=1(钉钉)paper_queued=1 |
| 21:36:23 | 挂单按开盘价撮合:QQQ.US 3buy @715.68,211.35股(≈15.1%仓位,trend3_boost 口径) |
| 21:41:23 | 1m 卖点(2sell)事件 → 挂卖单 |
| 21:42:22 | 平仓 @713.87,首笔完整交易落账 ret=-0.25%,sell_ratio=1.0 |

验证点全部通过:信号→比例通知→paper 挂单→开盘价撮合→持仓→卖点→全退→权益曲线(equity 999587,-0.04%,DD 0.05%)。修复前同链路全天零事件——第70轮的信号可达性修复是实时系统从"看起来在跑"到"真正在交易"的分水岭。paper ledger 自此开始积累可用于 strategy_optimizer 评分的真实 runtime 证据(A 股侧明日开盘同步生效)。

## 第七十五轮 core3 strict registry 组合复核

在 TSLA 长窗 checkpoint 跑通后，本轮继续把 `QQQ.US` 与 `NVDA.US` 按同一口径预热到同一个 strict signal cache 目录：

| 标的 | manifest | 结果 |
| --- | --- | --- |
| TSLA.US | `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_tsla_202606_v7_registry_manifest.json` | `registry_complete=1`、`events=36` |
| QQQ.US | `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_qqq_202606_v7_registry_manifest.json` | `registry_complete=1`、`events=31` |
| NVDA.US | `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_nvda_202606_v7_registry_manifest.json` | `registry_complete=1`、`events=40` |

组合 cache-hit 复跑：

| 文件 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v7_registry_layered_summary.json` | `hits=3/misses=0/writes=0`、`signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false` |
| `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v7_registry_layered_signals.csv` | 107 个严格可见信号 |
| `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v7_registry_layered_trades.csv` | 4 笔交易 |

严格组合结果：组合收益 `-4.00%`，等权基准 `-6.64%`，超额 `+2.64%`，最大回撤 `4.54%`，基准回撤 `9.79%`，交易 `4`。结果说明当前规则确实降低了回撤并跑赢这段下跌基准，但绝对收益仍为负，且交易数远低于稳健性下限。

最大亏损来自 `NVDA.US`：L1 `3buy` 于 `2026-06-02 15:46` 可见，下一根 `2026-06-02 15:47` 成交，随后在 `2026-06-09 16:11` 因 `structural_invalidation/structural_stop_below` 全退，单笔约 `-10.60%`。这不是未来函数问题，而是当前缠论逻辑仍需继续审查的信号质量/结构止损问题。

`D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md` 已刷新：`strict_original_registry` 报告数为 `7`，最佳严格报告交易数为 `4`，`robust_claim_supported=false`。因此后续如果仍无法达到低回撤高收益目标，优先排查当前缠论逻辑是否完全符合原文，尤其是 5m/1m 走势类型完成、L1/L0 买点质量、结构失效边界和 30m 背景下活动仓准入；不能回退到批量全局预计算买卖点。

## 第七十五轮 独立重审：双敌对审计、wzgx 口径统一、v7→v8 断层勘定与测试密闭化

本轮起点是用户指令：**「不能以之前的循环为准。只可以作为参考。」**因此本轮不延续上一轮待办，而是把之前所有自评结论降级为待验证主张，派出两个互相独立的敌对式审计（无未来函数 / 原文一致性），以代码与原文为唯一权威重新核对，再据审计结果修复。

### 一、无未来函数敌对审计（8 项全 PASS）

对 `live_backtest.py`/`engine.py`/`portfolio.py`/`kline_data_processor.py`/`exchange.py` 与两个 parity 测试做逐项核查，未发现 high/medium 级泄漏：

| 项 | 裁决 | 关键证据 |
| --- | --- | --- |
| 决策只用 ≤t K线 | PASS | 单一前向 CL 逐根喂入；`kline_data_processor` 禁止回插早于 last_date 的 bar |
| t+1 开盘成交 | PASS | `engine.py`/`portfolio.py` 挂单次 bar 开盘价撮合 |
| 多周期不提前泄漏 | PASS | 5m/30m 独立存储 + `label=left, closed=right` + 满周期 `available_delay`，30m bar 到真实收盘才可见 |
| 缓存 key 完备 | PASS+1 SUSPECT | key 含 df 全签名/main_dates/emit_start_idx/l0_min_zs_lines 等；SUSPECT=哈希全局 `CL_CFG` 而非实际生效配置 |
| first-seen registry | PASS | emit_start_idx 前只登记不发事件 |
| checkpoint 恢复==连续 | PASS | 逐事件断言（`test_selection_and_robustness_audits.py`） |
| 结构失效边界 | PASS | 边界值在信号首次可见时已知，当根 low/high 判定、次 bar 退出 |
| 大级别方向状态 | PASS | `big_dir_at[j]` 仅含 ≤j 可见信息 |

4 条 LOW 级事项中，「缓存 key 哈希全局 CL_CFG」已用守卫测试加固：`test_cl_config_extensions_are_covered_by_signal_cache_meta` 钉死 `_cl_config` 的扩展键必须显式入 `_signal_cache_meta`，违者测试失败。

### 二、原文一致性敌对审计（自评 gap=0 不成立）

结论：代码骨架与原文高度吻合（中枢公式、背驰比较段 c-vs-b、30m 同级别/<30m 非同级别架构、升级三情况、区间套嵌套性均对得上 file:line），但非 gap=0。判定：1 必偏离 + 3 偏离 + 1 误报 + 5 存疑。

| # | 项 | 处置 |
| --- | --- | --- |
| A | **趋势判定口径潜伏分裂**：cl.py 5 处新核心 getter、engine.py、zs_branch 构造签名的 fallback 写死 ZGD，而 process_klines/beichi_qs 内部 fallback=GD。活动构造的 CL 因 `__init__` 合并默认配置不受影响（CL_CFG 恒带 `zs_wzgx=GD`），但旧 pickle 反序列化 CL 的缺键 config 会在**同一对象内**口径分裂（图表 vs 买卖点对趋势/盘整结论矛盾） | **本轮已修**：唯一解析点 `CL._recursive_wzgx()`（fallback=GD=定理二），10 处统一；engine.py/zs_branch 同步；F7 回归测试 `test_f7_wzgx_fallback_unified_to_gd_everywhere` 钉死。规范路径行为零变化 |
| B | 2/3 买重合无双标签（C4.12 重合=最强信号，bs2 与 bs3 独立产出不交叉标记） | 待下轮：BuySellPoint 增加重合标记 |
| C | macd_htf 用高一档 K 线周期 MACD 判背驰——原文无此依据（级别≠周期），系工程近似 | **本轮已标注**：模块 docstring 改为【工程近似，非原文判据】并注明 `macd_ld_use_htf=False` 回退开关；htf vs 原生 A/B 量化留待后轮 |
| D | 区间套用时间区间包含近似空间嵌套，operable 标注未闭环到买卖点介入决策（C2.16） | 待后轮设计（交易层已有独立级联确认线） |
| E | 「三买 `>=ZG` 应为严格 `>`」 | **复核为误报**：原文 L10031「其低点不跌破 ZG」，不跌破=不低于=`>=`，代码与原文字面一致 |

存疑 5 项照录待量化：扩张触及级别门槛(C2.7)、tongjibie 段=摆动腿 vs 结合运算(C3.3)、三买未显式校验离开段冲出、背驰缺「黄白线先回 0 轴」前提门槛(C5.10)、9 段升级优先于扩张的无据约定。

### 三、v7→v8 断层勘定（本轮最重要的工程发现）

NTFS 时间戳串出完整事件链：上一会话(/clear 前)的最后一批编辑经 session-restore **延迟落盘**——大部分文件 22:05:47 落盘，`strategy_optimizer.py` 22:33:05、`live_backtest.py` 22:48:31 才落盘。`live_backtest.py` 的延迟写入携带 `_SIGNAL_CACHE_VERSION v7→v8` 与真实语义变更（实测 QQQ bar2589：旧=同一离开/回试对**三个历史叠加中枢各发一个 L1 3buy**（结构止损远在价格 -9% 之下），新=只对最近中枢发**一个** 3buy+紧结构止损——更合原文三买「离开后第一次回试」本义）。

后果与处置：
- 今晚 22:48 前启动的扫描进程全部用旧模块 → core3 三标的 v7 条目=旧语义，**全部作废**；
- 第一次 core3 组合回测在新代码下正确 miss 全部 v7 缓存、开始无 checkpoint 裸扫 TSLA 全年（≈16 分钟 955s CPU 后被停止）；
- 已改用带 checkpoint 的预热脚本重建 v8 缓存（QQQ/NVDA 分钟级、TSLA 约 1-1.5h）；
- 3 个 strategy_optimizer/monitor 测试的「瞬态失败」双根因：①22:33 的 strategy_optimizer.py 延迟 restore 恰落在两次运行之间；②测试经默认路径读真实 paper ledger，而首批真实 paper 交易今晚落账。已加 autouse fixture 把 `default_runtime_summary_sources` 密闭为空（3 个测试文件），并与全量 903 passed 复验。

教训固化：**uncommitted 状态跨会话是隐形断层源**——版本号 bump 必须与语义变更同 commit 落盘；本轮起把累计 ~5.7k 行未提交工作全部 commit。

### 四、core3 v8 严格样本（预热完成后回填）

TSLA registry 起点 2025-06-12（一年），QQQ/NVDA 起点 2026-04-14（受原始缓存历史限制，约两个月）——三者都对各自全部可得历史连续滚动（registry_complete=true），但证据强度分级披露。

2026-06-13 00:26 回填（第76轮执行）。预热恢复过程本身验证了 checkpoint 战略：上一会话的预热进程与自愈循环在 00:13-00:15 间相继无声死亡（与 US live_monitor 同时段，疑似系统性资源事件，根因另查），TSLA checkpoint 停在 `next_main_idx=95000/96710`。恢复前先验证两个易变成分均与 checkpoint meta 一致——`_stable_hash(CL_CFG)=1e783c1a...` 与 TSLA 1m df 按 `end=2026-06-10 16:00` 截断签名 `94c1bff0...`（chart_cache 已追加 6/11 新 bar，但 end 截断使签名不变）——然后以自愈循环 v2（`prewarm_logs/core3_v8_rebuild_loop2.ps1`：统一 v8 目录、UTF-8 日志、checkpoint 间隔 5000→1000、`Start-Process` detached 不随会话死）重启：

| 标的 | 结果 | 用时 |
| --- | --- | ---: |
| QQQ.US | registry_complete、events=25 | 34.8s |
| NVDA.US | registry_complete、events=37 | 30.0s |
| TSLA.US | **从 checkpoint 95000 恢复**，registry_complete、events=36 | **62.4s** |

manifest `ok=3/3`、总 events=98。从 checkpoint 恢复使原本 1-1.5h 的 TSLA 全年重扫只剩 62 秒——`checkpoint-every=1000` 后任何中断最多损失 1000 根的重扫。

core3 v8 严格组合（`us_core3_mtf3_20260601_0610_v8_registry_layered_*`，cache `hits=3/misses=0`，无未来口径四项全真）：

| 指标 | v8 | v7（作废口径，对照） |
| --- | ---: | ---: |
| 组合收益 | **-4.04%** | -4.00% |
| 等权基准 | -6.64% | -6.64% |
| 超额 | +2.60% | +2.64% |
| 最大回撤 | 4.35% | 4.54% |
| 基准回撤 | 9.79% | 9.79% |
| 信号事件 | 98 | 107 |
| 交易 | 3 | 4 |

v8 与 v7 的交易差异恰好只有一笔：v7 的第 4 笔 QQQ `2026-06-09 17:40` L1 `3buy`（+0.15% final_close）在 v8 下不再产生——正是「最近中枢单 3buy」语义消掉的历史叠加中枢重复信号，方向与第75轮 QQQ bar2589 案例一致。前 3 笔逐字段相同。结论：v8 语义变更对该窗口的组合影响微小且方向正确（少一笔低质量重复 3buy）。

NVDA `2026-06-02 15:47` 入场、`2026-06-09 16:11` 结构失效全退的 **-10.60%** 单笔在 v8 下原样存在：入场为最近中枢合法 3buy，结构止损边界距入场过远，持有期内无更早的失效信号。该笔是下一步「区间套 operable 闭环到介入决策」（第75轮 gap D）与结构止损质量审查的核心案例。

## 第七十六轮 v8 证据链统一、级联滞后实证与区间套介入闭环立项

本轮承接用户重申的总目标（真实盘口径、绝对无未来信号、级联分析降滞后、严格原文一致、解决严格回测耗时），起点是上一轮断在半途的 core3 v8 预热。

### 一、工程恢复与无声死亡根因

预热恢复过程见第75轮第四节回填。本轮新增的根因证据：Windows 事件日志在 `2026-06-13 00:04:32` 记录 `LiveKernelEvent P1=141`（GPU TDR 挂起）×2 与一条历史 `BlueScreen P1=116`（VIDEO_TDR_FAILURE）的延迟上报；python 预热进程死于 ~00:13（checkpoint 最后写入）、US live_monitor 心跳停于 00:15:23，属同一波 GPU 驱动级系统不稳定，**不是回测代码缺陷**。对策维持「自愈循环 + checkpoint-every=1000 + detached 启动」三件套；US live_monitor 已于 00:39 重启恢复心跳（A 股监控进程未受影响）。

附带修复：上一轮自愈循环 v1 的 PowerShell 5.1 `>>` 重定向产生 UTF-16 宽字符日志且循环进程随终端会话死亡；v2（`prewarm_logs/core3_v8_rebuild_loop2.ps1`）改用 `cmd /c` 重定向 + `PYTHONIOENCODING=utf-8` + `Start-Process` detached。

### 二、严格回测耗时的现状结论

用户点名的「真实无未来回测耗时太长」按 v8 体系拆解为三段：
1. **首次全历史扫描**（TSLA 一年 96710 根约 1-1.5h）：一次性成本，不可用「批量预计算买卖点」规避（那会回到未来函数口径，§126 已论证）；
2. **中断恢复**：checkpoint 间隔降到 1000 根后，本轮实测从 95000/96710 恢复仅 **62.4s** 完成收尾——任何中断最多重扫 1000 根；
3. **复跑**：v8 缓存命中后组合回测秒级（core3 组合含撮合全程 <1 分钟）。
结论：耗时瓶颈已从「每次实验 1.5h」变为「每标的一次性预热 + 之后秒级迭代」。后续扩标的（core9/全A）按 manifest 逐标的断点续跑。

### 三、证据链 v8 统一

按第75轮「v7 条目作废」裁决完成产物层面的清场：
1. 11 份 pre-v8 口径 summary 及其 trades/signals 伙伴文件移入 `D:/chanlun_pro/reports/superseded_pre_v8/`（含 core3/tsla v7 layered、wffull_window v5/v6/v7 系列）；
2. `audit_robustness_evidence.py` 重跑：strict_original_registry 报告 **9→2**（全 v8），`best_trades=3`，`robust_claim_supported=false`（如实）；
3. `audit_cascade_confirmation_tsla.py` 默认信号源切到 `us_tsla_mtf3_20260601_0610_v8_registry_layered_signals.csv`；
4. `audit_original_trading_system_matrix.py` 全部 v7 证据路径与判定切到 v8；CASCADE 行的过时断言「无 3sell」（第72轮 stale 语境）改为对每个事件 snapshot proof 的无未来一致性断言（`anchor_time` 时 `matched_signal_present=false` 且 `visible_time` 时 `=true`）；
5. 矩阵结果：**6 pass / 1 partial / 1 gap**（partial=US 选股 technical-only；gap=严格样本交易数 3 笔低于稳健下限）；
6. 测试 `test_original_trading_system_matrix.py` fixture 同步 v8；全量回归 `904 passed, 3 skipped`。

### 四、级联确认滞后的 v8 实证（区间套闭环的动机数据）

cascade 审计（TSLA 06-01→06-10，L1+ 共 4 事件，全部带 anchor 不在/visible 在的快照证明）：

| 事件 | anchor | visible | 滞后(1m bar) |
| --- | --- | --- | ---: |
| L1 3sell | 06-01 17:43 | 06-02 14:03 | **170** |
| L1 1sell | 06-01 17:43 | 06-02 14:03 | **170** |
| L1 3sell | 06-04 17:32 | 06-05 19:41 | **519** |
| L1 3buy | 06-08 18:33 | 06-09 17:15 | **312** |

L0 滞后中位 9 bar，而 **L1 滞后 170-519 bar（数小时到隔日）**——大级别买卖点的确认滞后比小级别严重两个数量级，这是区间套（大级别候选+次级别确认介入）能兑现的最大空间，也直接解释 §109 的「滞后实证」在递归级别上的放大效应。

TSLA 窗口内 36 事件 0 成交的归因同时落地：10 个买点全为 L0 级（深跌段左侧 1buy/2buy），按门控（30m/5m 向下时 scalp 不准入）被正确拦截；25 个 3sell 正确标识下跌结构。空仓使 TSLA 严格口径躲过基准 -9.6%。

NVDA -10.60% 案例的结构化审计（`nvda_core3_v8_trade_invalidation_audit.md`，注意其中 QQQ 行为工具按 NVDA 价格误算，仅 NVDA 两行有效）：入场后 **MFE 仅 +0.06%**、MAE -10.70%，7 天阴跌至结构边界 201.488 才退出——「L1 3buy 可见即入场、无次级别回试确认、止损边界过远」三重问题的完整standing案例。

### 五、区间套介入闭环设计研究（gap D 立项定稿）

完整报告：`D:/chanlun_pro/reports/qujiantao_intervention_design_research.md`（412 行，五节，含 2026-06-13 实跑试算）。要点：

**原文判据（带 chanlun.txt 行号）**：
1. 候选机制的原文依据=C5.41（61课 行33015-33017）「没实际走出来……都可以先假设是进入背驰段；一旦力度大于前者，断定背驰段不成立」——**大级别候选先假设成立、可被证伪，无须等大级别自身完成确认**，这是解除 L1 滞后 170-519 bar 的理论钥匙；
2. 介入点=候选背驰段**内部结构**中次级别背驰点/一类买点的当下出现（C5.38 程序定理 行17064-17068、C2.16「3买回试完成以再次级别 3 段呈现为准，精确买点参考该次级别第一类买点」L10399-10401）；
3. 现状「严格时间包含+同向」近似（`beichi_nest.py:48`）只取了时间投影，**丢了结构归属（次级别背驰须属于 c 内部针对最近中枢的离开段）与创新高/新低逐重收缩（C5.42）两个维度**——这就是 gap D 的实质；
4. 实战级差以一档为宜（C5.46 行18062「30 分钟的背驰段用 5 分钟找买点」），不必一推到底。

**现状根因（file:line 实锤）**：v8 生产主链（upgrade 源）根本没接 nest——`_collect_visible_signals` 的 upgrade 分支不传 annotate_nest（live_backtest.py:741-750），98 事件 CSV 的 `nest_operable` 全空实测；且 operable 的标注时点 ≥ 大级别信号可见时点（嵌套森林只收已固化背驰段），**结构上不可能提前介入**——filter/soft 只能在既定时点丢信号/打折仓位。

**闭环设计**：每级只跟踪最近中枢候选（对齐 v8 语义）的状态机：`CANDIDATE(离开腿冲出)→ENTRY_EMITTED(回试腿内笔级买点首次可见)→CONFIRMED(原生 L1 3buy 闭合)/INVALIDATED(回试破 ZG→nest_invalid 强退)`。新 `signal_source="nest_cascade"`（meta 含 source → 缓存键天然隔离，**无需 bump v9**）；新事件 `3buy_nest/1buy_nest`（buy_class 兼容现有 ratio 通道）与 `nest_invalid_*`（强退优先级=结构失效）；次级别确认本身用 branch 流的 wf 首次可见语义（无任何未来引用）。NVDA 案例预期：入场 225.6→约 211-216，同一失效边界下止损距离 10.7%→4.7-6.7%，MFE +0.06%→+4.6-7.0%，最坏亏损 -10.6%→约 -5~-7%（**减亏近半但不是免亏——区间套修复的是风险收益比的分母**）。

**试算修正设计方向（估计器 A，已实跑）**：用现有 v8 CSV 事后配对——「任意 L0 买点」做确认反而**均值 -1.97% 价格劣化**（3buy 回试向下走，早期 L0 买点更贵：TSLA/NVDA 提前 200+ bar 贵 5%）；「严格 L0 1buy」仅 1/11 匹配（upgrade 流无笔级事件，系统性低估）。**结论：提前≠改善；确认池必须是笔级（branch 流）一/二买（回试结束类，C2.16 精确口径），不可放宽**。正式度量=估计器 B（提前 10 交易日窗口、upgrade vs nest_cascade 双流配对，指标含 no_subconfirm 诚实空集率与 false_positive 假阳性成本）。

**关键风险**：①假阳性成本未知（候选破 ZG 失效单笔 ≈4-7%，频率未知）；②破 ZG 失效口径对插针敏感（建议收盘价口径+两个反例 fixture）；③同 anchor 的 3buy+1buy 并发候选需去重；④nest_cascade 每 bar 全量 collect 性能约翻倍；⑤**门控交互：nest_entry 是左侧介入，第69轮已证 30m 门控对左侧低吸净伤害——建议默认绕过 mid 门控、保留 big_dir!=down 红线，留参数给回测裁决**；⑥A 股扩展前必须过涨停锁死验证（第63轮纪律）。

**推进顺序（下轮执行）**：最小闭环（仅 L1 3buy 候选+笔级一/二买确认+破 ZG 失效）→ 测试钉法 1-3（受控构造/快照无未来断言/缓存回归 byte-identical）→ 估计器 B 跑 NVDA/TSLA/QQQ 提前窗口 → NVDA 验收 fixture → 再扩 1buy 与卖向。全程不动 v8 既有缓存与 nest_mode 行为；结果只进 review 候选，不自动采纳。

### 六、v8 多级别图表证据刷新

`render_chanlun_visual_audit.py` 默认源切到 v8 layered 报告，产出 `chanlun_visual_audit_tsla_v8_registry.html`（2.0MB）：3 个 SVG 面板（1m=笔+1m/5m/30m 中枢/买卖点/背驰、5m=笔+5m/30m、30m=笔+30m）+ Strict Replay Metrics/Layer Trades/Signal Visibility Audit 三个数据节，图元 716 polyline/10794 rect/174 circle 非空图——用户三周期展示口径在 v8 下的可视证据。

### 七、本轮工程教训

1. **后台子代理产出会丢**：两次研究代理（OMC architect 与 general-purpose）的最终回复均被替换为一句空话且 transcript 0 字节（疑似 OMC stop-hook 干扰）——**对策=要求代理边研究边把报告写入指定文件**（第三次成功，49 次工具调用产出 412 行报告零丢失）；
2. `.gitignore` 的 `*.md` 全局忽略曾使主日志与全部设计文档零版本保护（76 轮记录只存在于工作目录，今晚恰逢 GPU 级系统不稳定）——已加 `!docs/**/*.md` 例外并首次入库 33 份文档。

## 第七十七轮 原文全文深读与全量偏离矩阵：收益低/回撤高的原文级诊断

用户指令：「当前的交易系统没有预期的收益的原因还是因为没有完全基于缠论原文导致的……必须全面阅读理解原文，而不是简单读某几个章节。」本轮以 11 个并行深读代理覆盖 **107 课全文+全部回复**（chanlun.txt 54587 行，无遗漏：A1 理念选股/A2 操作资金管理/B 中阴转折/C1 MACD背驰/C2 背驰区间套/D 分型笔线段/E1 走势必完美买卖点/E2 同级别分解程式/F 图解表里动力学/H 杂谈挖掘），外加代码全量编目（G：247 规则点，73 无据）。产出 **859 条判据（82 条冲突标记）**，全部落盘 `docs/yuanwen_study/v2/`（每代理边读边写，防丢产出纪律）。

### 一、总裁决：用户假设成立

第75轮「代码骨架与原文高度吻合」仍然成立——但骨架≠体系。结构感知层（分型/笔/线段/中枢/级别架构）与原文吻合度高（D 分片 89/119 确认）；**决策层与原文系统性相悖**：入场（结构钉死后追入 vs 原文候选不等确认+回跌中介入+区间套次级别1买下手）、退出（已病杀跌 vs 原文上涨中出货+宁早勿迟+破位等反抽）、门控（方向一票否决 vs 原文仅主跌段过滤+背驰段放行）、仓位（3buy 优先固定 ratio vs 原文 1/2买原理级保证+状态机+成本归0）。四环节偏离共同指向同一种病：**把原文的「当下结构操作体系」做成了「滞后信号过滤体系」**——收益低（错过左侧+追高）与回撤高（已病退出+杀跌）皆其症状。完整矩阵=`docs/yuanwen_study/v2/_MATRIX_全量偏离矩阵.md`（七大主轴+修复路线图 R78-R84）。

### 二、最高价值判据（每条带 chanlun.txt 行号）

1. **区间套介入是标准做法非可选优化**（H.56 行13922-32：「没必要等5分钟回抽走势完成，只要在5分钟的第一类买点介入就可以」；行13011「第三类买点就可次次级别的第一类买点下手」）——6b 设计获原文四组授权（另 C2.10/B.21/E2.37）。
2. **NVDA 坏单的原文判词链**：fill 比 anchor 贵 7%=「追涨」非「买点买」（A2.27 行25745）；MFE+0.06% 第一个次级别向上段不创新高按原文当时就该走（A2.44 行28532-37）；7 天满仓扛=中阴期非法选项（B.83 行44363-65）；边界最低区杀跌=「不能杀跌」明令的反面（C2.36 行26775-80）+「已病才动」最低档（B.101 行46011-13）。
3. **门控的正确形态**：全书唯一方向过滤授权=主跌段（E2.74 行25609-13）；高级别改变必先从低级别开始（E2.107 行49711）——「等 30m 转向再放行低级别买点」因果上恒滞后；衰竭（背驰段）即放行（A2.28 行25746）；按量分级不按方向禁止（E2.3 行15889）。
4. **macd_htf 最终裁决=分级映射**：判背驰用信号所属级别的本级别周期图（C1.15/31/43 行4063-66/6002-10/14685 + B.75 行33472 整合）；统一 htf+1 与统一原生都不对。背驰另需：趋势前提（C1.19 行4913「没有趋势没有背驰」）、盘整柱剔除（C1.34 行6377-93）、黄白线回0轴+高度双不如（F.44/81 行31384-85/32861-63）、同趋势对唯一性（E1.22 行8377）、**1/2类点只存在于中阴窗口**（F.137 行49571-73）。
5. **操作模式架构**：原文主模式=39课程式（E2.60 行25178-83 可编程精确版，含原文自证 walk-forward；E2.40 行24718 一招鲜；C2.36「机械化程序粗糙但坚持必有超级回报」）——第69轮 TSLA 程式唯一跑赢的实证与全文深读在此会师；现系统把程式当实验旁支、把工程自创的「信号扫描+门控」当主线，应反转。
6. **结构底层定案**：原文最终笔口径=新笔（D.8 行33383-87），系统硬编码老笔（bi_calculator.py:38 strict/cl.py:67）——切换需全基线 A/B；线段破坏官方「直接取整」容差（C2.45 行33276-77）vs 全精度浮点（4e-16 噪声教训同源）；有效跌破=「次级别下去+次级别反拉不能重新上来，和百分比无关」（C2.7 行15309）。

### 三、本轮速修与暴露的真缺陷

1. **F8 修复**：`recursive_l0_min_zs_lines` 缺键 fallback 4 处（cl.py:463/502/529+engine.py:373）与生产默认 3 分裂（wzgx 同构）——统一为唯一解析点 `CL._recursive_l0_min_zs_lines()`（fallback=3），F8 测试源码级钉死（禁止散装 fallback=4 读取）。CL_CFG 恒带 3，缓存 key 不受影响。
2. **F8 暴露 3 段口径摆动腿反转失明（重大）**：600519 5m 实测——3 段成枢的 V 型转折中枢 z2 的包络被暴力离开段撑爆（dd=1322, gg=1565 全窗口最高），`_swing_segments` 反转确认 `dd>谷.gg` 永假 → 摆动腿退化单条 down → **30m tongjibie 中枢丢失**；4 段口径无此病（包络停 1431）。生产 v8=3 段口径——**疑为 §102「30m 同级别信号稀疏」的系统性根因**；与 core_envelope 张力（G1.23）同源。v34 语义测试此前一直隐式跑在缺键=4 上，掩盖了该退化。已处置：v34 测试显式钉 4 段口径；新增现状记录测试 `test_600519_5m_l0min3_swing_blindness_known_issue` 防无声漂移；矩阵 P1 提级（修复候选：脱离判定改本体/核心包络，或第三段暴力离开的成枢语义裁决）。
3. 全量回归 **906 passed, 3 skipped**。

### 四、修复路线图（矩阵定稿，每轮一主题，全程 v8 严格口径对照、结果只进 review）

R78 区间套介入闭环（6b+价格距离闸+C2.7 失效口径）→ R79 退出重构（领先退出+等反抽+旧3卖免疫+3buy盘整高点出）→ R80 门控重构（主跌段识别+策略分层）→ R81 39课程式主线化 → R82 背驰重构（分级MACD+趋势前提+盘整柱剔除+黄白线条件，bump v9）→ R83 仓位状态机+中阴检测 → R84 结构底层（新笔A/B+取整容差+9段强制+摆动腿失明修复，全基线重算）。

## 第七十八轮 R78 候选判定层 + R84 摆动腿失明修复（缓存 v8→v9）

本轮起执行路线图。两项落地：

### 一、R78 第一步：区间套介入候选判定层

`zs_upgrade.kuozhan_level_candidates` + `NestCandidate`（6b 设计 §3.1 最小可验证落地，纯结构/无未来/不碰信号链与缓存）：离开腿冲出 ZG 但回试腿未走出（retest=None）窗口产 3buy 候选，`kuozhan_level_signals_ex` 此窗口正好留白（其 3buy 要 retest.end≥ZG），二者按 retest 是否走出**互斥**。解 L1 确认滞后 170-519 bar（原文 H.56 行13922「没必要等回抽走完，在次级别第一类买点介入即可」）。3 个 TDD 测试 + commit c9813327。

**R78 信号链接入（commits f8736727/c03fe402/bd9ff260/2f2f01f5）**：`collect_nest_cascade_signals`（engine.py，候选×L0 确认）+ `3buy_nest/1buy_nest` 注册入 BUYS + `signal_source=nest_cascade`（live_backtest，=upgrade 全流+介入事件，meta 隔离免污染 v9 缓存；补齐 3 处硬编码 signal_source 校验白名单）。候选窗口扩到回试腿进行中。全量 909 passed。

**已知未闭环（R78 核心剩余工作）**：NVDA nest_cascade 实测 **0 介入事件**，尽管该窗口确有 1 个 L1 3buy。根因＝`collect_nest_cascade_signals` 是**无状态同 bar 合取**（当 bar 候选 active AND 当 bar L0 买点 price≥ZG），与时间错位冲突：候选 active（回试腿进行中）时回试腿内的 L0 底背驰买点尚未首次可见；待 L0 可见时回试腿往往已钉死、候选消失。正解＝遍历所有 L1 中枢 + 回试窗口配对（无状态等价跨 bar 状态机:每 bar 重算所有中枢候选 + wf fresh 去重,无需显式 active 集合）。

**R78 闭环根因与修复(commits ae86b0f8 + 对象身份修复)**:
1. **候选只看 zss[-1]**:walk-forward 中枢快速易主,最近中枢回试窗口瞬时,错过历史中枢(如 NVDA L1 3buy 中枢 zd=211.256 在产生 3buy 时已非最近)。改 kuozhan_level_candidates 遍历所有中枢 + NestCandidate 加 retest_end_date,候选窗口 = 离开冲出 ZG + 回试腿已开始且未破核心,确认配对限 (leave_end, retest_end]。
2. **对象身份不一致(真根因)**:get_kuozhan_candidates 曾单独调 get_recursive_branch_levels() 拿 L0,而 get_kuozhan_levels() 内部又调一次——两次返回不同对象实例,L1 中枢 expanded_with(指向第二次 L0 对象)在第一次 L0 的 seg_of[id] 查不到 → 段定位全失败 → 候选恒空。修复:get_kuozhan_levels 返回各级 lower,候选复用同次对象(id 一致)。诊断脚本 scripts/diag_nest_nvda.py 验证 cands=2 nest=2,全量 910 passed。

候选/确认/对象一致三层已修通(诊断脚本 cut=06-04 cands=2 nest=2)。但端到端 backtest 两个窗口(06-01~06-10、含候选段 05-08~05-20)仍 NEST=0,揭示第三层根因:候选依赖「已形成的 kuozhan L1 中枢」,而该中枢形成晚于离开腿冲出。诊断脚本 cut=06-04 能看到 05-11 候选,是因数据到 06-04 时 L1 中枢已事后形成;但 walk-forward 在 05-11~05-12 回试进行的 bar,kuozhan L1 中枢尚未形成 → 候选空;待 L1 中枢首次可见(更晚),回试 L0 买点 anchor 已是过去 bar → 被 first-seen/stale 机制丢弃。R78 真正闭环需把候选触发基础从「kuozhan L1 中枢」下移到「L0 摆动腿离开冲出事件」(早于 L1 中枢形成),非当前会话能完成;机制三层已修通,为该重设计扫清前置障碍。

### 二、R84 提前落地：摆动腿反转失明修复

R84 本是路线图收尾项，但 R78 探测确认摆动腿失明是**硬阻断**（600519 类 V 型标的 L1 kuozhan/L2 tongjibie 中枢全丢，candidates/signals_ex 皆无输入），按「先修数据再调参，缺陷数据上的优化不可信」纪律提前。

**根因（精确机制）**：3 段成枢的 V 型底/顶中枢，第三段是暴力离开段（反弹/回落腿）冲出核心区；`correct_exit` 因 `min_body=3` 对 3 段中枢剥不动离开段（剥后仅 2 段<本体下限）→ 中枢本体 gg/dd 被离开段远摆撑爆（600519 z2 dd=1322 但 gg=1565=全窗口最高）→ `_swing_segments` 反转确认「后中枢 dd>谷中枢 gg=1565」永假 → 摆动腿退化单条 → 升级链全断。4 段及以上中枢无此病（离开段已由 correct_exit 剥除）。

**修复**：新增 `zslx_branch._swing_body(z)`——反转判定专用本体包络：末段确为离开段（终点比起点更远离核心区 [zd,zg]）且剥后≥2 段时取剩余段 [min low, max high]，否则退化 `zs.dd/zs.gg`（与旧行为一致）。`_swing_segments` 全部 dd/gg 访问改用 `_swing_body`。

**实测（600519 5m 3 段口径）**：摆动腿 单腿失明 → `up/down/up/down` 4 腿严格交替；30m tongjibie 中枢 0→1（zd=1325）；L1 kuozhan 中枢 0→1。**不破坏**：4 段口径（`test_600519_5m_v_shape` down/up/down 保持）、000001（tongjibie zd∈3850-3900 保持）、全量 **909 passed**。失明现状测试 `test_600519_5m_l0min3_swing_blindness_known_issue` 反转为正向修复确认 `test_600519_5m_l0min3_swing_reversal_restored`。

**缓存 v8→v9**：摆动腿语义变更影响所有标的 L1/L2 中枢与信号 → 按第75轮纪律 bump（旧 v8 core3 证据作废，待 v9 全基线重建回填信号变化量化）。只影响回测 signal cache，不影响实时 live_monitor（后者用实时扫描非 wf 缓存）。

### 三、R84 修复的生产价值实证（NVDA 核心坏单 -10.6%→-4.04%）

v9 预热信号变化：QQQ events 25→25（无 V 型失明，不变）、**NVDA 37→30（减 7）**、TSLA 一年预热中。NVDA 单标的 v9 cache-hit 回测对照 R76 v8 core3 报告（同一 NVDA L1 3buy 信号）：

| 口径 | NVDA 第一笔（同 06-02 15:47 @225.6 入场） | 单笔 |
| --- | --- | ---: |
| v8（R84 修复前，R76 core3 报告） | 持有到 **06-09 16:11 @201.68** 结构边界全退 | **-10.60%** |
| v9（R84 修复后） | **06-03 13:49 @216.48** 提前早退 | **-4.04%** |

**减亏 6.56pp、退出提前 6 天、退出价 +14.8**。机理坐实：v8 摆动腿失明使 NVDA 的 L1/L2 中枢结构缺失 → 退出信号（小级别卖点/结构失效）缺位 → 被迫持有到远端结构边界 201.68；R84 恢复中枢结构后 06-03 即出现退出信号。这是「先修数据再调参」纪律的直接回报——R77 矩阵诊断的「退出杀跌/已病才退」病症，部分根源在结构层失明而非交易层规则。v9 NVDA 单标的整体 -6.69%（15 trades/30 signals）。

完整 core3 v9 组合对照（TSLA 一年预热 28 分钟完成，cache `hits=3/misses=0`，无未来口径全真）：

| 指标 | v8（R84 前，R76） | v9（R84 后） | 变化 |
| --- | ---: | ---: | ---: |
| 组合收益 | -4.04% | **-2.97%** | **+1.07pp** |
| 等权基准 | -6.64% | -6.64% | — |
| 超额 | +2.60% | **+3.67%** | **+1.07pp** |
| 最大回撤 | 4.35% | **3.03%** | **-1.32pp（降 30%）** |
| 基准回撤 | 9.79% | 9.79% | — |
| 交易 | 3 | **16** | 恢复信号 |
| 信号 | 98 | 91 | NVDA -7 |

**R84 地基修复的整体生产价值坐实**：摆动腿失明修复让 core3 在同一下跌窗口（基准 -6.64%）下，收益 -4.04%→-2.97%、超额 +2.60%→+3.67%、**回撤 4.35%→3.03%（降 30%）**。交易 3→16 是因为 R84 恢复了被失明吞掉的 L1/L2 中枢与买卖点（NVDA 06-09 新增多笔 1buy 全部小赚 +0.6%~+2%，原 v8 这些信号因摆动腿失明根本不存在）。绝对收益仍为负（下跌市），但相对 v8 全面改善且回撤大幅下降——验证 R77 矩阵「收益低/回撤高部分根源在结构层失明」的判断。robustness/matrix 审计需迁移到 v9 报告。

### R78 第四层结论——区间套介入的适用前提(2026-06-13)

逐 cut 诊断(NVDA:05-12 cands=0/05-13 cands=0/05-15 cands=2,候选 retest_end=05-12)确证 kuozhan L1 中枢在 05-15 才首次形成,而其回试(05-11~05-12)早已过去。对快速结构标的,L1 中枢是事后识别的,回试低点在实时没有 L1 中枢可依托。原文区间套(27/61课、C5.46)前提是大级别结构已存在且离开/回试正在进行;对大级别中枢形成滞后于价格的标的该窗口实时不可达。R78 的 NVDA 0 介入在这些窗口是正确行为而非 bug;验证须用 L1 中枢已先形成、离开冲出+回试在交易窗口内的标的/时段。区间套对慢结构有效、快结构天然受限(与第69轮级别选择标的特异同源)。机制三层已修通提交,候选触发下移到 L0 离开冲出事件是 R78 真闭环下一步重设计。

### R79 退出重构架构勘明(2026-06-13,接续入口)

领先退出(A2.44:次级别向上段不创新高/盘整背驰即退)接入层:portfolio._position_structural_invalidation(行663)只接 bar high/low + 预算信号,无次级别走势结构访问,故须在信号链层(engine 产 lead_sell 事件,基于 cd 次级别走势段不创新高/盘背)实现。R79 四组件:①领先退出=engine信号链;②C2.36破位等反抽=portfolio退出改破位后等次级别反抽不创新高再卖;③H.6旧3卖免疫=portfolio记入场后新走势段数三段未完前旧3卖不触发;④E1.41 3buy无趋势盘整高点出=与①同源。R84已把NVDA退出-10.6%→-4.04%(退出改善主体),R79做领先/等反抽精细化。R78真闭环(候选触发下移L0离开冲出)+R79-R83全部待新会话。

### R78 第五层(最终)结论——真闭环方向定案(2026-06-13)

追查 kuozhan_zhongshu(zs_upgrade.py:123)确证:扩张型 L1 中枢(is_kuozhan,行148)需两个完整 L0 中枢的包络重叠,第二个 L0 中枢完整时离开+回试早已过去,故扩张型升级天然滞后于价格(非 bug、非 pending 可解:扩张语义是两中枢关系而非 3 段重叠)。R78 真闭环不能依赖 kuozhan 升级的 L1 中枢,须新增独立检测:直接在 L0 摆动腿/中枢序列上识别趋势(>=2 同向中枢本体分离)+背驰段进行中,在背驰段内下推次级别笔级一类买点介入,这正是原文 27/61课区间套本义(背驰段进行中即下推、不等中枢升级;C5.38/C5.41 先假设进入背驰段可证伪)。当前 nest_cascade 用 get_kuozhan_levels 已升级 L1 中枢作候选基础是方向性偏差。R78 下一步=engine 新增 collect_qs_beichi_candidates(基于 L0 中枢趋势+背驰段,不经 kuozhan 升级),组件已备(is_qs/is_beichi/_swing_alternating_segs/笔级买点)。机制三层+五层诊断已扫清全部前置。

### R78 实现关键细节(2026-06-13,collect_qs_beichi_candidates 必读)

若用 l0.done_divergence(已完成趋势底背驰段)会遇到与 nest_cascade 相同的 stale 问题:背驰段在线段离开段钉死时才可见,那时笔级底背驰确认(低点)已是过去 bar,被 walk-forward first-seen/stale 丢弃。真闭环必须用 provisional(进行中)趋势背驰段(L0 live hypotheses,ZsHypothesis.divergence provisional=True),而 LevelResult(recursive_branch.py:25)当前只暴露 done_divergence、未暴露 live。前置:①get_recursive_branch_levels 暴露 L0 live provisional qs 底背驰段;②collect_qs_beichi_candidates 在 provisional 背驰段 active 时配对其后首次可见笔级 1buy(use_xd=False)介入;③退出走 C5.41 力度证伪。与 6b §3.1 状态机本质一致。

### R78 第六层(最底层)结论(2026-06-13)

diag_live_qs.py 实测 NVDA L0 live leave hypothesis 背驰:05-13/06-09=None、06-02=pz+up顶背驰,无(is_beichi+qs+down)底背驰,故 live_qs_divergence 恒空。根因 zs_branch._is_trend:qs 须>=2 同向中枢本体分离,右边缘 pending 中枢与前中枢常 expand/方向不定,趋势底背驰段进行中多被判 core 读法或 pz。区间套进行中趋势背驰段在右边缘天然难识别(趋势需结构、结构形成中),缠论实时区间套本质困难非 bug。真闭环最后一公里=zs_branch 增强 live 趋势底背驰识别,研究级。基础设施 LevelResult.live_qs_divergence+collect_qs_beichi_candidates 已就绪 910 passed,待底层数据充实激活。

### R78 真闭环解法方向(新会话第一步)

第六层根因的解=zs_branch._is_trend(live=True) 逻辑增强:当前要求 live pending 中枢本身与前中枢构成趋势(右边缘常不成立),改为若已 done 中枢序列已确立下跌趋势(>=2 同向本体分离)且 live 离开段向下延续,则判 live 离开段为趋势底背驰段。需传 done 趋势上下文入 _divergence_for。风险:影响图表 live 背驰+须全基线回测,fixture 钉死 NVDA 05-11 应识别+000001 不误报。落地后 live_qs_divergence 充实→collect_qs_beichi_candidates 激活。诊断完整+解法明确+基础设施就绪(910 passed)。

### R78 第七层(真闭环达成)——推翻第六层误判,真正阻塞点与修复(2026-06-13)

**第六层结论是测试假象,予以推翻。** 第六层「live_qs_divergence 恒空、需 zs_branch._is_trend 研究级增强」的诊断错在 **cut 选错**:diag_live_qs.py 测的 05-12/05-13/06-02/06-09 **全是 NVDA 见顶后的上涨/顶背驰区**(中枢 215~222、leave_dir 全 up,是卖区而非买区);"05-11 应识别买点"是误记——05-11 NVDA 正在 215 局部高点,做空信号才对。

逼近**真实趋势底背驰**(L0 done[4]@05-05,zs.dd=194.51)逐 cut 实测(diag_lead_nvda_0505.py)推翻全部:05-04 17:00~05-05 15:30 区间,live leave 读法 **a=down c=down is_beichi=True rel(prev,live)=trend_down**,即**当前 _is_trend 无需任何改动**就把它判为 qs(classify_rel==trend_down ⟺ is_qs(prev,live,GD,core)==down,代数等价)。直接验证:这些 cut **L0.live_qs=1 早已非空**。zs_branch 增强方向(第六层解法)是不必要的——基础设施本就工作。

**真正的阻塞点**在 collect_qs_beichi_candidates 的次级别确认口径 `s.bs_type=="1buy"` 过严:
- 笔级 1buy 在 L0 背驰段内**结构性极罕见**(单条 L0 离开段往往不含 ≥2 笔级中枢、无法成笔级趋势背驰);
- 且**反向有害**:NVDA 真底 194.51 之前的笔级 1buy 全是下跌途中假底(04-29@207.34、05-01@198.65,均在 L0 背驰段开始前 05-05 13:46),严格 1buy 要么不触发要么提前套牢;
- L0 背驰段窗口内唯一买向笔级信号是 **3buy@197.90(05-05 14:32)**——真底 194.51 之后、笔级已重夺中枢、是更可靠的介入点。

**真正的根因(比第七层更底层)——nest_cascade 在 walk_forward 模式整条死线**:run_backtest 的 wf 信号流装配(_walk_forward_signals 调用处)只有 `if signal_source=="upgrade"` 与 `else→"branch"` 两支,**全无 nest_cascade 分支**;`--signal-source nest_cascade` 静默落入 else 用 `signal_source="branch"` 采集 → collect_nest_cascade_signals / collect_qs_beichi_candidates **在 wf 路径从不执行**(_collect_visible_signals 里的 nest 采集逻辑对 run_backtest 是死代码)。这才是 R78 长期 0 介入的**首要**根因——此前各轮宣称 nest_cascade「已接入」从未在真 wf 验证过。修复:wf 装配的 upgrade 分支条件改 `signal_source in ("upgrade","nest_cascade")` 且传真实 signal_source(原硬编码 "upgrade");mid 流与 core_signal_level/swing_signal_level 自动设档同步纳入 nest_cascade。

**第二根因——wf 重算签名对笔级失明**:_collection_state_signature 对 upgrade/nest_cascade 只用线段(xd)tail,笔级买点出现在某线段中途时签名不变 → wf 不在该 bar 重算 → 进行中(transient)nest 介入信号永不被捕获。修复:nest_cascade 签名并入 `len(bis)`(新笔完成即触发重算;不并末笔签名,避免进行中笔每根延伸全量重算拖慢数十倍)。

**第三:次级别确认口径**:`s.bs_type=="1buy"` → `s.bs_type in ("2buy","3buy")`。wf 实测(NVDA 下跌段)证笔级 1buy 是「转折前」趋势背驰点、多腿下跌每腿触发一次全假底(05-01@198.65→续跌 194.51);2buy/3buy 是「转折后」确认(中枢重夺),才标志最小级别真转上(05-05@197.90→反弹 215,+8.6%)。原文 H.56「第一类买点」严格读法低回撤导向下次优,定档 2buy/3buy。缓存 bump v9→v11。

**验证**:① 信号级:真底 05-05 14:40 触发 1buy_nest@197.90/stop<194.506,**提前于 done 背驰坐实(05-05 16:20)**;2buy/3buy 口径排除 05-01 1buy 假底、保留 05-04+05-05 两个 3buy;② 上涨区 3 cut + A 股 SH.000001(5.8万 bar 仅 1 趋势底背驰)全无误报;③ wf 实测信号链已产 1buy_nest/3buy_nest(btail is_buy 版曾产 4+4,2buy/3buy 收敛);④ 全量 910 passed(v11 全改动后)。

**终版 wf 端到端实证(NVDA 单标的 max-pos 1,2026-04-14~06-10,基准 +5.1%/DD15.5%)**:nest_cascade -3.2%/DD12.3%/3 笔/胜率 33%(1buy_nest 信号 3 个=05-01 1buy 假底已被 2buy/3buy 排除;3buy_nest 4 个)。**关键正面验证——区间套提前介入抓到真底大反弹**:首笔 = 1buy_nest 05-04 17:33 入场 @196.9 → 05-11 16:45 small_level_sell_point 出场 **+11.95%**(吃满 196.9→~220 全程反弹)。无区间套(纯 upgrade)首仓须等 L1/L2 升级信号(晚得多、滞后 170-519 根 1m bar),这 +11.95% 正是区间套兑现的滞后削减。组合 -3.2% 的拖累全在**后续两笔非 nest 的 upgrade 流亏损**(05-13 -5.5%、06-02 -6.7%,NVDA 该窗口后段震荡下行+结构失效退出),非区间套入场之过。**结论:区间套机制端到端跑通且提前介入兑现单笔大幅 alpha(+11.95% vs 等基础设施下更晚入场);整体收益受标的难做窗口+upgrade 流出场拖累,指向 R79 退出重构(非更多入场)是下一杠杆——与第八层稀疏性结论、R84 退出修复主体一致。**

### R78 第八层——区间套买点的结构稀疏性(诚实定量,2026-06-13)

全量扫描(diag_scan_qs_bottoms.py,各标的最终结构 L0/L1 下跌趋势底背驰计数):

| 标的 | bars | L0 done中枢 | L0 down背驰 | **L0 趋势底背驰** | L1 趋势底背驰 | up背驰(对照) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NVDA | 15601 | 19 | 2 | **1** | 0 | 2 |
| TSLA | 97339 | 139 | 13 | **8** | 0 | 20 |
| QQQ | 15601 | 21 | 0 | **0** | 0 | 5 |
| SH.000001 | 58804 | 86 | 3 | **1** | 0 | 14 |

两条架构性事实:① **趋势底背驰只在 L0、L1 恒 0**(L1 走势类型级在这些周期/区间里从不形成 ≥2 同向下跌中枢)→ 原「大级别(L1)→小级别(L0)」区间套结构性空集,旧 nest_cascade 用 get_kuozhan 升级 L1 中枢作候选基础是错配的级别;真区间套是 **L0 下跌趋势背景 → 笔级提前确认**。② **买侧(down 趋势底背驰)天然稀疏**(NVDA 1/TSLA 8/SH 1,即便 5.8 万 bar),up背驰恒多于 down背驰——区间套 1buy_nest 是低频但真实的提前介入增强,不会淹没回测(无误报风险已证),但对总收益的贡献量级有限,系统主体买卖点仍来自 3buy/3sell。**诚实结论:区间套解决了稀疏趋势底背驰买点的滞后,但这类买点本身不多;R84(摆动腿失明→回撤降 30%)仍是已验证的最大收益杠杆,下一杠杆是 R79 退出重构而非更多买点。**
