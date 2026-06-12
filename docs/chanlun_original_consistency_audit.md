# 缠论原文一致性审计

本文不是收益报告，而是把当前 `chanlun.core` / `recursive_bt` 的实现与《缠中说禅 108 课》原文逻辑做结构对照。原文索引由 `scripts/audit_chanlun_original.py` 从 `docs/缠中说禅缠论108课教你炒股票配图+回复版本（共3697页）.docx` 抽取，结果位于 `D:/chanlun_pro/reports/chanlun_original_index.json`。

抽取概况：

- 有效段落：20728
- 图片：1061
- 覆盖关键词：分型/笔/线段、中枢、延伸/扩展/扩张、走势类型、同级别分解、一二三类买卖点、背驰、区间套

## 1. 当前结论

现有实盘回测链路已经解决了“先全局算买卖点再回测”的未来函数问题，但收益目标没有稳定达成，原因不能继续只从参数和仓位上找。当前实现至少存在以下原文一致性风险：

1. L0 中枢最小确认口径在代码内自相矛盾。
2. 回测交易信号默认使用笔级买卖点，而不是线段/走势类型链条上的正式操作级买卖点。
3. 30m 同级别分解和 30m 以下非同级别分解已经有升级链，但交易入口主要仍吃 `get_branch_bspoints(use_xd=False)`，没有完整吃到 1m→5m→30m 级联后的买卖点/背驰。
4. 二类买卖点、三类买卖点、区间套在核心中已有实现，但组合交易决策没有把“高级别买点持有，次级别一类卖点短差，次级别一类买点回补”的原文程序完整表达出来。
5. 趋势核心仓尝试证明了一个事实：如果底层级别/方向信号不严格，仓位模型只会放大或隐藏结构问题。

## 2. 原文证据点

以下只记录定位与短摘要，不长篇搬运原文。

| 主题 | 原文索引定位 | 审计要点 |
| --- | --- | --- |
| 中枢定义 | `chanlun_original_index.json` `center` 组，idx 2620 附近 | 中枢是至少三个连续次级别走势类型的重叠；最低不可再分级别需另行定义，不能把“确认所需的下一段”混入中枢定义本身。 |
| 盘整/趋势 | `center` 组，idx 2621-2622 附近 | 一个中枢为盘整；两个以上依次同向中枢为趋势。 |
| 走势分解 | `same_level` 组，idx 2626、3024、3025 附近 | 任何走势可分解为同级别上涨、下跌、盘整；任一走势类型至少由三段以上次级别走势类型构成。 |
| 中枢扩张 | `center_change` 组，idx 3703-3705 附近 | 新生同级别中枢与前中枢波动区间触及，会形成更大级别中枢；这与单纯延伸、新生不重叠中枢不同。 |
| 二类买点定律 | `bsp` 组，idx 1370-1373 附近 | 大级别二类买点可由低级别一类买点精确定位，买点可递归归结到一类买点。 |
| 短差程序 | `cascade` 组，idx 1376、1387、1484 附近 | 大级别买点介入后，次级别一类卖点减仓，次级别一类买点回补。 |
| 线段端点 | `fx_bi_xd` 组，idx 3944-3947 附近 | 市场走势由线段构成，线段端点与买卖点关系需要严格列举，不应把笔级噪音直接当正式走势端点。 |
| 背驰 | `divergence` 组，idx 1368、1373、1774、1832-1834 附近 | 背驰以力度衰竭为核心，趋势背驰必须先有趋势结构；面积/力度比较不能脱离走势类型。 |

## 3. 当前实现差异

### P0. L0 中枢 3 段定义与 4 段确认混在一起

相关实现：

- `src/chanlun/core/zs_calculator.py:21`
- `src/chanlun/core/zs_calculator.py:27`
- `src/chanlun/core/recursive_calculator.py:222`
- `src/chanlun/core/recursive_branch.py:80`

问题：

- `ZsCalculator` 默认 `min_zs_lines=4`，注释称这是“原文一致”。
- `recursive_calculator.py` 又明确写 L0 线段中枢最小 4 段是“项目口径，偏离原文”。
- 原文定义层面是三段次级别走势类型重叠；实盘确认层面可以要求等下一段确认第三段完成，但这应是 `confirmed_at` / `visible_at` 的时间属性，而不是改写中枢结构本身。

可能后果：

- 1m/5m 中枢形成滞后。
- 三买/三卖的“离开+回试”可能被推迟或漏掉。
- 高级别递归升级依赖的 L0 中枢数量变少，导致 5m/30m 结构稀疏。

修复方向：

- 把“结构定义”与“实盘确认”拆开：
  - `core_lines=3` 表示原文中枢本体；
  - `confirmed_at` 表示实盘可确认时间；
  - 回测只能在 `visible_at <= current_bar` 后使用该结构。

### P0. 交易信号默认取笔级买卖点

相关实现：

- `src/chanlun/recursive_bt/live_backtest.py:548`
- `src/chanlun/core/cl.py:439`
- `src/chanlun/core/cl.py:442`
- `src/chanlun/core/cl.py:466`

问题：

- `live_backtest` 里的 walk-forward 信号收集调用 `collect_branch_signals(cd, use_xd=False)`。
- `use_xd=False` 在 `CL.get_branch_bspoints` 中意味着用笔作为构成单元。
- 用户要求的是 1m K 上展示笔，同时展示 1m/5m/30m 的中枢、买卖点、背驰；这里的“笔”应是观察层，不应自动成为正式操作级买卖点。

可能后果：

- 信号过密、噪音大。
- 强趋势里过早卖出，QQQ/NVDA 横向复验的收益被短线卖点压低。
- 调仓逻辑看似低回撤，但可能不是原文意义上的走势类型交易。

修复方向：

- `live_backtest` 增加信号构成单元参数：
  - `--signal-unit xd` 作为正式交易默认；
  - `--signal-unit bi` 仅用于观察/敏感度实验。
- 图表继续展示笔，但交易信号应以线段及其递归中枢/走势类型为主。

### P0. 级联买卖点未成为交易入口主链

相关实现：

- `src/chanlun/core/cl.py:495`
- `src/chanlun/core/cl.py:502`
- `src/chanlun/core/zs_upgrade.py:1`
- `src/chanlun/core/zs_upgrade.py:408`
- `src/chanlun/core/zs_upgrade.py:455`
- `src/chanlun/recursive_bt/engine.py:325`

问题：

- 代码中已有 `1m -> 5m(kuozhan) -> 30m(tongjibie)` 的升级链。
- 但实盘回测的交易入口仍主要收集 `get_branch_bspoints` 的一二三类点，而不是把 `get_kuozhan_levels` 中的 5m/30m 买卖点、背驰作为主信号层。
- 这与用户提出的“5m 中枢由 1m 走势类型构成，1m 走势类型由 1m 中枢构成，1m 中枢由线段构成；级联分析降低滞后”方向不一致。

可能后果：

- 高级别买卖点在图上有，但交易系统没真正按它们做主决策。
- 30m 同级别分解的价值被弱化，仓位管理只能依靠粗糙方向门控。

修复方向：

- 重构信号事件为分层事件：
  - L0/1m 线段级：执行与短差；
  - L1/5m 非同级别：活动仓主买卖点；
  - L2/30m 同级别：核心仓开闭、禁买/恢复。
- 回测撮合时必须记录每笔交易来自哪个级别、哪类买卖点、哪条背驰链。

### P1. 二买与区间套没有完整进入组合交易程序

相关实现：

- `src/chanlun/core/bs2_branch.py:1`
- `src/chanlun/core/interval_nest_calculator.py:1`
- `src/chanlun/core/cl.py:390`
- `src/chanlun/core/cl.py:560`
- `src/chanlun/recursive_bt/portfolio.py:669`
- `src/chanlun/recursive_bt/portfolio.py:730`

问题：

- 核心层已实现二买、区间套、背驰嵌套。
- 组合层的交易规则仍主要是“小级别买点进、小级别卖点出/大级别 down 出”，没有把“高级别买点介入后，次级别一卖短差，次级别一买回补”表达为状态机。

修复方向：

- position 需要拆状态：
  - `core`: 来自 30m 同级别买点/趋势完成；
  - `swing`: 来自 5m 二/三买；
  - `scalp`: 来自 1m 区间套末端一买。
- 小级别卖点默认只影响 `scalp/swing`，30m 三卖或 30m 趋势背驰才影响 `core`。

### P1. 背驰比较口径需要实盘可见化

相关实现：

- `src/chanlun/core/beichi_calculator.py:117`
- `src/chanlun/core/beichi_calculator.py:224`
- `src/chanlun/core/macd_htf.py:1`

当前背驰实现比旧版更接近原文，包含创新高/新低前提、力度衰竭、高周期 MACD。但交易报告还缺少：

- 背驰比较段 A/C 的可视化输出；
- 每个买卖点是否来自趋势背驰、盘整背驰、区间套确认；
- 背驰信号的 `anchor_time` 与 `visible_time`。

如果没有这些审计字段，回测结果难以证明“没有未来信号”且“符合原文背驰结构”。

## 4. 修复优先级

1. 先让交易信号默认从 `bi` 改为 `xd`，并把 `bi` 降级为观察层。
2. 把中枢三段定义与第四段确认拆成结构时间与可见时间。
3. 用 `get_kuozhan_levels` 生成 1m/5m/30m 分层事件，替代单层 `small_by_bar`。
4. portfolio 状态机拆成核心仓、波段仓、短差仓。
5. 所有事件输出 `level`, `unit`, `bs_type`, `divergence_kind`, `anchor_time`, `visible_time`, `source_path`。
6. 再跑 TSLA/QQQ/NVDA 严格 walk-forward，不再优化旧信号体系。

## 5. 对当前收益结果的解释

TSLA 两个月窗口中旧活动策略跑赢买持，QQQ/NVDA 强趋势段显著低于买持，这不应解释为“缠论无效”。更合理的解释是：当前交易入口过度依赖笔级/短级别卖点，且没有按原文把高级别核心仓和次级别短差分开，导致强趋势中过早被洗出。

因此，下一轮应停止单纯参数搜索，先修复 P0 结构口径，再做严格回测。

## 6. 2026-06-12 实盘滞后与级联信号复核

本轮已把回测信号源拆为 `branch` 与 `upgrade`：

- `branch`：沿用 `get_branch_bspoints()`，可选 `--signal-unit bi|xd`；
- `upgrade`：逐根 walk-forward 调用 `get_kuozhan_levels()`，直接收集 1m->5m/30m 级联后的 L1/L2 买卖点；
- `--recursive-l0-min-zs-lines 3|4`：显式区分原文三段定义与旧工程四段确认口径；
- `--signal-warmup-bars -1`：保留全部历史作为预热；正数仍表示有限根数预热。

TSLA 诊断结果：

| 口径 | 窗口/历史 | L1 信号 | L2 信号 | 结论 |
| --- | --- | ---: | ---: | --- |
| L0=4 | 2026-04-14~2026-06-10 | 3 | 0 | 旧口径压缩升级结构 |
| L0=3 | 2026-04-14~2026-06-10 | 6 | 0 | 原文三段恢复更多 5m 信号，并生成 1 个 30m 中枢 |
| L0=3 | 全历史至 2026-06-10 | 23 | 2 | 更长历史会恢复 L2 买卖点，但计算成本显著上升 |

严格实盘时点证据：

- 静态锚点：`2026-06-08 18:33:00+00:00`，L1 `3buy`，price `412.94`；
- walk-forward 可见时间：`2026-06-09 17:15:00+00:00`；
- 回测成交时间：`2026-06-09 17:16:00+00:00` 下一根 1m 开盘，entry `390.12`；
- 因此当前严格回测没有用锚点时间提前交易，已体现买卖点确认滞后。

可视化复核：

- HTML：`D:/chanlun_pro/reports/chanlun_visual_audit_tsla_latest.html`；
- 生成脚本：`scripts/render_chanlun_visual_audit.py`；
- 读取口径：与严格回测相同，使用 `market_runtime.load_chart_cache_klines()` 读取原始 chart-cache；
- 浏览器复核：1m/5m/30m 三个面板均非空；1m 面板含 1m/5m/30m 递归层级总数，且显示 5m `3buy`、当前级别买卖点和背驰标记。

TSLA 短窗严格结果：

- 命令口径：`signal_source=upgrade`，`recursive_l0_min_zs_lines=3`，`signal_warmup_bars=6000`；
- 输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wf6000_window_upgrade_l0min3_summary.json`；
- 结果：收益 `-2.18%`，买持 `-3.72%`，超额 `+1.54%`，最大回撤 `4.25%`，交易 `1` 笔。

TSLA 完整历史预热严格结果：

- 命令口径：`signal_source=upgrade`，`recursive_l0_min_zs_lines=3`，`signal_warmup_bars=-1`；
- 输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_signal_audit_summary.json`；
- 信号审计：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_signal_audit_signals.csv`；
- 结果：收益 `0.00%`，买持 `-3.72%`，超额 `+3.72%`，最大回撤 `0.00%`，交易 `0` 笔，信号事件 `4` 个；
- 性能修复：`signal_source=upgrade` 的 CL 扫描开启 `skip_legacy_mmd`，跳过旧笔/线段买卖点全量扫描；完整历史首根可见处理从约 `42.7s` 降到约 `2.3s`，同口径二次运行命中信号缓存。

有限预热与完整历史差异：

- `6000` 根预热在 2026-06-09 17:15 看见 L1 `3buy` 并成交一笔；
- `-1` 完整历史预热同一时刻同时看见旧 L1 `3sell` 与该 `3buy`，并且更早已有两个 L1 `3sell` 事件，组合层没有开仓；
- 因此正式结论不能用有限预热的收益替代。有限预热只能作为性能/敏感性测试，真实实盘口径必须保留足够长的历史状态，最好使用 `-1` 或持久化的递归状态缓存。

新结论：

1. “先全量算出锚点再回测”会把 `2026-06-08 18:33` 当作可交易信号，这是未来函数；严格 walk-forward 必须等到 `2026-06-09 17:15` 才能看见。
2. 当前 `upgrade` 链证明了用户提出的大小级别级联方向是对的，但 L2/30m 买卖点仍很稀疏，说明还需要继续审计 30m 同级别分解、走势类型完成度与中枢扩展/扩张实现。
3. 有限预热会截断高级别结构；真实实盘应使用足够长的历史状态，报告中必须记录 `signal_warmup_bars`，否则同一算法会因历史长度不同产生不同高级别信号。

## 7. 30m 同级别分解候选审计

原文复核：

- 36 课强调走势连接符合结合律，同一段走势可按不同级别和组合解释；这说明中枢划分存在多义性，但不是含糊性。
- 39 课在 `a+A` 的 5 分钟同级别分解中，把 `A` 写成 `A1+A2+...+Am`，并说明 `A1、A2、A3` 构成 30 分钟中枢。
- 同段还强调“下一个 Ai+2 是当下产生的，但这不会影响所有前面 Ai+1 的同级别唯一性分解”。因此实盘交易不能把所有重叠三段候选同时计成 30m 中枢，必须在当下形成一条一致、非重叠的分解路径。
- 36 课同时允许根据操作有利原则在未完全走出时重组，但也说明中枢扩展定义是在两个中枢都完全走出来后定义；所以重组只能用于解释/辅助判断，不能回头改写已交易前缀。

TSLA 全历史至 `2026-06-10` 的 L2 诊断：

审计产物：`D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.json`，生成脚本：`scripts/audit_tongjibie_tsla.py`。

| 项目 | 结果 |
| --- | --- |
| L1/5m 中枢数 | 66 |
| 30m 同级别交替段数 | 16 |
| 所有连续三段重叠候选 | 7 组 |
| 最终非重叠分解入选 | 4 组：`(0,2)`, `(5,7)`, `(8,10)`, `(13,15)` |
| L2 买卖点 | `2025-11-24 3buy`、`2026-04-17 3sell` |

结论：

1. TSLA 的 L2 信号稀疏不是因为所有重叠候选都应入选；把 `(1,3)`、`(2,4)` 这类重叠候选也交易，会破坏原文的同级别唯一性分解。
2. 当前代码新增 `_tongjibie_candidate_groups()`，只做审计候选；交易仍由 `_tongjibie_groups()` 采用非重叠三段路径。
3. 若仍无法达到收益/回撤目标，优先排查的是：L1 走势类型完成度、L0 三段结构与实盘确认时间拆分、30m 方向状态机、二买/三买与次级别一卖短差回补，而不是简单放宽 30m 中枢数量。

## 8. 大级别底仓与次级别短差状态机

原文复核：

- 大级别买点介入后，次级别一类卖点可以先减仓，次级别一类买点再回补；这不是独立新开仓，而是同一大级别持仓内的成本调整。
- 因此仓位状态必须至少区分“核心底仓”和“活动仓”。小级别卖点不应机械清掉大级别向上的全部筹码；大级别向下或较大级别卖点才清核心仓。

本轮实现：

- `portfolio_backtest(..., trend_core_hold_ratio=...)` 在可见 30m 方向为 `up` 时，把入场仓位拆出 `core_shares` 与活动仓。
- 小级别卖点成交时，只卖出 `shares - core_shares`，并把该标的标记为 `activity_reentry=wait_buy`。
- 后续同标的出现当前可见小级别买点时，生成 `activity_refill` 挂单，在下一根主时钟开盘补回活动仓缺口；该挂单不占新开仓名额，也不会绕过停牌/涨跌停/30m down 检查。
- 大级别 `down` 仍全平，包括核心仓。

验证：

- 新增测试 `test_portfolio_backtest_trend_core_refills_activity_on_later_buy_point`：买入后保留 50% 核心仓，小级别 `3sell` 只卖活动仓，后续 `1buy` 补回活动仓，最终 30m `down` 全平。
- 回归命令通过：`179 passed, 1 skipped`。
- TSLA 严格实盘式复跑使用 `signal_mode=walk_forward`、`signal_source=upgrade`、`signal_warmup_bars=-1`、`trend_core_hold_ratio=0.5`；输出 `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_core_refill_verify_summary.json`。
- 该复跑信号缓存命中 `hits=1`、事件数 `4`、交易数 `0`，与全历史预热审计一致：`2026-06-09 17:15` 的 L1 `3buy` 与历史/同刻 L1 `3sell` 共存，严格组合层不能后验只取买点交易。

结论：

1. 当前已经不再只是“保护底仓”，而是具备大级别底仓保护后的次级别卖点减仓、买点回补闭环。
2. 这次修改只在仓位撮合层生效，信号仍以 `visible_time` 为唯一可交易时间，未引入未来信号。
3. 下一步若收益/回撤仍不达标，应继续补“30m 同级别买点开核心仓、5m 活动仓、1m 短差仓”的三层资金状态，而不是把所有层级买卖点混成同一仓位。

## 9. 30m 同级别三买三卖稀疏原因审计

本轮把 TSLA 同级别分解审计升级为 v2，产物：`D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit_v2.json`。

新增审计字段：

- `recursive_level_counts`：递归 L0/L1... 的中枢与走势类型数量；
- `get_kuozhan_levels_counts`：CL 正式升级链的 L1/L2 中枢、买卖点、背驰数量；
- `l1_zhongshu`：每个 5m 级别中枢的区间、完成状态、来源 L0 索引；
- `candidate_groups.reason`：重叠候选为什么只做审计、不进入交易；
- `l2_zhongshu.third_signal_diagnostic`：每个 30m 中枢后续段是否构成三买/三卖。

TSLA 结果：

| 项目 | 结果 |
| --- | --- |
| `get_kuozhan_levels()` L1 | 中枢 `66`、买卖点 `23`、背驰 `13` |
| `get_kuozhan_levels()` L2 | 中枢 `4`、买卖点 `2`、背驰 `0` |
| 所有三段重叠候选 | `7` |
| 非重叠同级别入选 | `4` |
| L2 买卖点 | `2025-11-24 3buy`、`2026-04-17 3sell` |

L2 中枢后三买/三卖诊断：

| group | 核心区间 | 状态 | 解释 |
| --- | --- | --- | --- |
| `(0,2)` | `[314.75,330.11]` | 不触发 | 末段向下，后续上段回试终点 `321.55` 高于 `ZD=314.75`，不满足三卖“不回中枢核心”的条件 |
| `(5,7)` | `[330.40,340.55]` | 触发 `3buy` | 后续下段回试终点 `420.49 >= ZG=340.55` |
| `(8,10)` | `[422.12,451.46]` | 触发 `3sell` | 后续上段回试终点 `403.09 <= ZD=422.12` |
| `(13,15)` | `[406.39,412.43]` | 未完成 | 右边缘尚无后续回试段，不能提前确认三买 |

结论：

1. L2 稀疏不是 `get_kuozhan_levels()` 与审计脚本不一致；正式链路和手工链路计数一致。
2. 重叠候选 `(1,3)`、`(2,4)`、`(7,9)` 被排除的原因是与已入选同级别前缀重叠；为了增加信号而交易这些候选，会破坏 39 课强调的同级别唯一分解。
3. 2026-06 窗口最近的 30m 中枢 `(13,15)` 还没有后续回试段，所以不能在严格实盘中提前给出 30m `3buy`。
4. 因此下一步不应放宽 L2 分组来制造交易，而应完善“30m 结构背景 + 5m/1m 活动仓”的跨级仓位状态，以及继续审计 L1 走势类型完成时间是否存在可见性滞后。

## 10. L2 卖点控制核心仓的级别路由

继续复查组合层后发现一个原文一致性风险：虽然仓位已经有 `core_shares`，但退出逻辑原先只判断“是否有卖点”，没有区分该卖点来自 L1/5m 还是 L2/30m。这样在持有核心仓时，L2/30m 卖点也可能被当成 `small_level_sell_point`，只卖活动仓，违背“大级别卖点处理核心仓”的资金分层口径。

本轮修复：

- `portfolio_backtest(..., core_signal_level=...)` 新增核心信号级别阈值；
- 当卖点 `level >= core_signal_level` 时，优先按 `big_level_sell_point` 处理，卖出比例锁为 `1.0`，不再被小级别 `sell_ratio_overrides` 改成半仓/四分之一仓；
- 小于该级别的卖点仍按小级别短差处理，只影响活动仓；
- `live_backtest` 在 `signal_source=upgrade` 时自动把目标 `big_level` 映射到核心级别：`1m->30m` 为 L2，`5m->30m` 为 L1；
- `summary.json` 新增 `core_signal_level`，便于审计。

验证：

- 新增测试 `test_portfolio_backtest_core_signal_level_exits_trend_core`：L1 `3sell` 只卖活动仓且可尊重小级别卖出比例覆盖，后续 L2 `3sell` 以 `big_level_sell_point` 全平剩余核心仓。
- `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` 现验证 `signal_source=upgrade` 下自动传入 `core_signal_level=2`。
- TSLA 严格复跑输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_core_level_verify_summary.json`。
- 该复跑记录 `core_signal_level=2`、`signal_cache_stats.hits=1`、`signal_event_count=4`、`trade_count=0`。信号审计仍显示 L1 `3buy` 的 `anchor_time=2026-06-08 18:33:00+00:00`、`visible_time=2026-06-09 17:15:00+00:00`、`next_fill_time=2026-06-09 17:16:00+00:00`，没有提前交易锚点。

结论：现在组合撮合层已经能区分 L1 活动仓卖点与 L2 核心仓卖点；这比单纯的 `big_dir_at` 风控更贴近原文“各级别资金独立、按操作级别处理”的要求。

## 11. 30m 下行背景中的低级别活动仓

继续复查 TSLA 严格窗口后，发现上一轮 `core_signal_level` 虽然保护了核心仓路由，但仍有一个过硬门控：只要 30m `big_dir_at == down`，所有 5m/1m 买点都会被组合层过滤。原文里“大级别不逆势开核心仓”和“在小级别买卖点上做短差”不是同一件事；在较大级别下跌未确认反转前，可以不开核心仓，但不能把已经可见的低级别买点一律视为不存在。

本轮新增一个显式、保守、可审计的活动仓口径：

- `portfolio_backtest(..., big_down_activity_buy_ratio_multiplier=...)` 默认为 `0.0`，旧口径完全不变；
- 只有显式传入大于 0 的乘数时，才允许 `big_dir_at == down` 背景中的低级别买点开活动仓；
- 该买点必须满足 `signal.level < core_signal_level`，因此不会把 30m/L2 核心买点误当成小级别短差；
- 活动仓成交后标记 `big_down_activity=True`，核心仓比例强制为 `0`；
- 这类活动仓不会因为开仓时的 30m down 背景在下一根 bar 被机械平掉，但仍会被小级别卖点或核心级别卖点退出；若大级别脱离 down 后，该豁免清除，后续再转 down 仍按大级别风控退出。

验证：

- 新增测试 `test_portfolio_backtest_blocks_big_down_activity_by_default`：默认参数下，30m down 背景中的 L1 `3buy` 不开仓。
- 新增测试 `test_portfolio_backtest_allows_lower_level_activity_in_big_down_when_enabled`：启用乘数后，L1 `3buy` 下一根 bar 开活动仓，后续 L1 `3sell` 下一根 bar 退出。
- 新增测试 `test_portfolio_backtest_preserves_buy_ratio_on_final_close`：窗口末尾强平也保留入场仓位比例，便于审计。
- 新增测试 `test_walk_forward_dedupes_reappearing_signal_identity`：同一 `anchor_time + level + bs_type` 信号在 replay 中消失后再出现，不再二次触发交易事件。
- `test_live_backtest_passes_confirmed_bs_point_ratio_multipliers` 现覆盖 CLI 到组合层的 `big_down_activity_buy_ratio_multiplier` 透传。
- 相关回归：`184 passed, 1 skipped`。

TSLA 严格实盘式复跑：

> 历史口径说明：以下短窗结果来自 v6 连续 signal registry 修正之前，只能证明当时 `visible_time` 没有提前成交；不能再作为最终实盘交易结论。最终口径以第 23 节 `signal_seen_registry_complete=true` 的 v6 registry probe 为准，旧 L1 `3sell` 不再触发窗口内二次退出。

- 命令口径：`signal_mode=walk_forward`、`signal_source=upgrade`、`signal_warmup_bars=-1`、`core_signal_level=2`、`big_down_activity_buy_ratio_multiplier=0.25`；
- 输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_bigdown_activity025_summary.json`；
- 信号缓存：同口径复跑 `hits=1`、`misses=0`、`entries=2`；
- 信号事件：去重后窗口内只剩 2 个新事件，分别为 L1 `3sell` (`visible_time=2026-06-08 19:53:00+00:00`) 与 L1 `3buy` (`visible_time=2026-06-09 17:15:00+00:00`)；
- 结果：收益 `-0.60%`，买持 `-3.72%`，超额 `+3.12%`，最大回撤 `1.18%`，基准回撤 `8.92%`，交易 `1` 笔；
- 成交明细：L1 `3buy` 的 `visible_time=2026-06-09 17:15:00+00:00`，`next_fill_time=2026-06-09 17:16:00+00:00`，实际 `entry_date=2026-06-09 17:16:00+00:00`、`entry_px=390.12`；`anchor_time=2026-06-08 18:33:00+00:00` 仍只作静态归属，不参与成交。
- 仓位比例：CSV 中 `buy_ratio=0.275`，来源为基础 3买比例 `1.0` × 策略 3买乘数 `1.1` × 30m down 活动仓乘数 `0.25`。

结论：这一步把用户提出的“用大小级别级联缓解买卖点滞后”落实到资金执行层：30m 继续作为同级别分解和核心风控背景，5m/1m 在严格可见买点后可以用缩小活动仓试错。它没有放宽信号可见时间，也没有提前使用锚点，因此仍满足无未来信号要求。

## 12. 核心仓只由核心级别买点建立

继续复核分层仓位后，发现上一轮还有一个更细的语义风险：如果只根据 30m 方向为 `up` 来拆出 `core_shares`，那么低级别 L1 买点也可能生成核心仓。原文的操作级别口径要求“按自己的操作级别处理”，因此 30m 核心仓应由 30m/L2 买点建立；低于核心级别的买点只能建活动仓或短差仓。

本轮修复：

- 买单现在携带触发信号的 `level`；
- `PTrade` 新增 `entry_level`、`exit_level`、`entry_layer`、`exit_layer`、`core_shares_before`、`activity_shares_before`；
- 当 `core_signal_level > 0` 时，只有 `entry_level >= core_signal_level` 的买点才允许按 `trend_core_hold_ratio` 建核心仓；
- 低于核心级别的买点即使 30m 方向为 `up`，也只记录为 `entry_layer=activity`；
- L1 卖点退出记录为 `exit_layer=activity`，L2/30m 卖点退出记录为 `exit_layer=core_all`，窗口末尾强平记录为 `exit_layer=all`。

验证：

- 调整 `test_portfolio_backtest_core_signal_level_exits_trend_core`：L2 `3buy` 建核心+活动仓，L1 `3sell` 只卖活动仓，L2 `3sell` 全平核心仓。
- 新增 `test_portfolio_backtest_low_level_buy_does_not_create_core_when_core_level_set`：L1 `3buy` 在 30m up 背景中也不建核心仓。
- 相关回归：`185 passed, 1 skipped`。

TSLA 严格复跑：

- 输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_layer_audit_summary.json`；
- 结果仍为收益 `-0.60%`、买持 `-3.72%`、最大回撤 `1.18%`、交易 `1`、信号事件 `2`；
- 交易明细：`entry_level=1`、`entry_layer=activity`、`core_shares_before=0.0`、`activity_shares_before=697.792417005608`；
- 时间审计不变：L1 `3buy` 的 `visible_time=2026-06-09 17:15:00+00:00`，实际 `entry_date=2026-06-09 17:16:00+00:00`，仍然没有使用 `anchor_time=2026-06-08 18:33:00+00:00` 提前成交。

结论：当前组合层已从“两层仓位”推进到“可审计的操作级别分层”：30m/L2 才能建核心仓，L1/5m 只能建活动仓。下一步应继续把活动仓内部拆成 5m 波段仓与 1m 短差仓，而不是继续用单一 activity 桶承接所有低级别信号。

## 13. 活动仓拆分为 5m 波段仓与 1m 短差仓

本轮继续把“activity”桶拆成更接近原文操作级别的两层：

- `core_signal_level`：核心层，`1m -> 30m` 映射为 L2；
- `swing_signal_level`：波段层，`1m -> 5m` 映射为 L1；
- 低于 `swing_signal_level` 的信号作为 1m 短差/短线层，记录为 `scalp`。

实现：

- `portfolio_backtest(..., swing_signal_level=...)` 新增波段层级参数；
- `live_backtest` 在 `signal_source=upgrade` 时自动推断：`1m->5m` 为 `swing_signal_level=1`；
- 持仓记录新增 `swing_shares`、`scalp_shares`、`swing_target_shares`、`scalp_target_shares`；
- 交易导出新增 `swing_shares_before`、`scalp_shares_before`；
- 入场分层：
  - `level >= core_signal_level`：核心+波段，记录为 `core_swing`；
  - `swing_signal_level <= level < core_signal_level`：5m 波段仓，记录为 `swing`；
  - `level < swing_signal_level`：1m 短差仓，记录为 `scalp`。
- 卖点分层：
  - L0/1m 卖点只处理 `scalp`；
  - L1/5m 卖点处理 `swing + scalp`，不动核心仓；
  - L2/30m 卖点或 30m down 风控才处理核心仓。

验证：

- 新增 `test_portfolio_backtest_scalp_sell_does_not_sell_swing_layer`：L0 卖点不会误伤 L1 swing 仓；
- `test_portfolio_backtest_core_signal_level_exits_trend_core` 改为验证 L2 `3buy` 建 `core_swing`，L1 `3sell` 只出 `swing`，L2 `3sell` 全平核心；
- 相关回归：`186 passed, 1 skipped`。

TSLA 严格复跑：

- 输出：`D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_swing_scalp_summary.json`；
- 参数：`core_signal_level=2`、`swing_signal_level=1`、`big_down_activity_buy_ratio_multiplier=0.25`；
- 结果：收益 `-0.60%`，买持 `-3.72%`，超额 `+3.12%`，最大回撤 `1.18%`，交易 `1`，信号事件 `2`；
- 交易层级：`entry_level=1`、`entry_layer=swing`、`core_shares_before=0.0`、`swing_shares_before=697.792417005608`、`scalp_shares_before=0.0`。

结论：当前 TSLA 交易已经不再是模糊的 activity 仓，而是明确的 5m/L1 swing 仓；1m/L0 信号将进入 scalp 层，且 L0 卖点不会误伤 L1 波段仓。这一步更贴近“30m 同级别分解，30m 以下非同级别级联”的实盘资金分层。

## 14. 可视化报告叠加实盘回放层级审计

本轮把严格回放的资金层级与无未来信号时间审计叠加到 TSLA 多级别图表报告里。

实现：

- `scripts/render_chanlun_visual_audit.py` 新增 `--summary`、`--trades`、`--signals` 输入；
- HTML 顶部新增严格回放指标表：收益、买持、最大回撤、交易数、信号事件、`core_signal_level`、`swing_signal_level`；
- 新增 Layer Trades 表，直接展示 `entry_layer`、`entry_level`、`buy_ratio`、`core/swing/scalp` 股数；
- 新增 Signal Visibility Audit 表，展示 `anchor_time`、`visible_time`、`next_fill_time`、`anchor_to_visible_bars`；
- 1m 图上叠加信号可见线、下一根成交点、交易入场/出场层级标记；5m/30m 图仍保留结构、买卖点和背驰审计窗口。

生成与验证：

- 输出：`D:/chanlun_pro/reports/chanlun_visual_audit_tsla_swing_scalp.html`；
- 浏览器验证：页面标题为 `Chanlun Visual Audit - TSLA.US`，有 `3` 个 SVG 面板和 `3` 个 panel；
- 1m 面板摘要：bars `1170`、BI `79`、BI centers `9`、递归层级含 L0 `119` 个中枢、L1 `52` 个中枢、L2 `2` 个中枢；
- 1m overlay：信号事件 `2`、交易 `1`；
- 页面文本包含 `swing`、`2026-06-09 17:15:00+00:00`、`2026-06-09 17:16:00+00:00`，证明图表报告同时展示了层级归属与买点滞后。

结论：现在不只 CSV 能证明无未来信号，HTML 图表也能同时看到 1m/5m/30m 结构、买卖点/背驰，以及 L1 swing 仓在信号可见后下一根 bar 成交的实盘路径。

## 15. 分层归因报告：先定位问题，再讨论调参

本轮新增分层归因报告，用于回答“如果严格按原文逻辑仍无法达到收益/回撤目标，问题到底在信号、级别、仓位层，还是样本不足”。

实现：

- `strategy_optimizer.py` 新增 `build_layer_attribution_report()`、`render_layer_attribution_markdown()`、`write_layer_attribution_report()`；
- 报告按 `entry_layer`、`entry_level`、`exit_layer` 分组统计交易数、胜率、平均收益、复利收益、最大回撤、平均持仓小时；
- 每个 layer 带 `sample_state`，并按 `min_trades` 生成 `layer_guidance`，避免用一两笔交易就后验推翻策略；
- 新增测试 `test_layer_attribution_summarizes_layers_levels_and_guidance`，覆盖 swing/scalp 分层、level 归因、exit layer 归因和 Markdown 输出。

TSLA 严格窗口归因：

| 项目 | 结果 |
| --- | --- |
| JSON | `D:/chanlun_pro/reports/tsla_swing_scalp_layer_attribution.json` |
| Markdown | `D:/chanlun_pro/reports/tsla_swing_scalp_layer_attribution.md` |
| 回放收益 | `-0.60%` |
| 买持收益 | `-3.72%` |
| 最大回撤 | `1.18%` |
| 交易数 | `1` |
| 信号事件 | `2` |
| entry layer | `swing` |
| entry level | `1` |
| swing 单笔收益 | `-2.19%` |
| sample_state | `thin` |
| guidance | `watch` |

结论：

1. 当前严格 TSLA 窗口的亏损来自一笔 L1/5m swing 活动仓，退出为窗口末尾 `final_close/all`，不是 30m 核心仓错误开仓。
2. 该样本只有 1 笔，不能据此认定“5m swing 层失效”，也不能为了收益曲线临时放宽同级别分解或提前使用锚点。
3. 下一步如果继续达不到目标，应扩大严格 walk-forward 样本窗口和标的集合，用分层归因分别检查：L2/30m 核心仓是否过少、L1/5m swing 是否有稳定正期望、L0/1m scalp 是否只应做回补短差。
4. 这一路径与原文一致：先固定可见性与操作级别，再检验各级别资金层的真实贡献；不能用未来函数式优化替代级联确认。

## 16. 级联确认滞后审计：锚点、可见点、成交点三分离

本轮新增 `scripts/audit_cascade_confirmation_tsla.py`，专门审计用户提出的“买卖点滞后是否能用大小级别级联分析解决”。报告口径与当前严格回测一致：原始 1m chart-cache → `CL.get_kuozhan_levels()` → L1/5m、L2/30m 信号；不读取全局买卖点后再回填交易。

产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.json` | 机器可读的锚点/可见点/结构快照审计 |
| `D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md` | 人工复核报告 |

审计方法：

- 对每个严格回放信号，取三个截断时刻重新计算缠论结构：`anchor_time`、`visible_time` 前一根、`visible_time`；
- 在每个截断时刻检查同一 `level + bs_type + anchor_time` 信号是否已经存在；
- 同时记录 BI、XD、L1/L2 中枢、买卖点、背驰数量；
- 从买卖点对象读取关联中枢、信号段、锚点，而不是用最终图表字符串猜测。

TSLA 严格窗口结果：

> 历史口径说明：本表保留为“滞后审计”记录，展示旧短窗里买卖点从锚点到可见点的延迟；v6 连续 signal registry 已证明 L1 `3sell` 的首次可见时间其实早于该短窗，不能作为 `2026-06-08` 之后的新交易信号。最终实盘回测应使用第 23 节的 v6 registry 结果。

| 信号 | 锚点 | 可见 | 下一根成交 | Anchor→Visible |
| --- | --- | --- | --- | ---: |
| L1 `3sell` | `2026-05-29 16:24:00+00:00` | `2026-06-08 19:53:00+00:00` | `2026-06-08 19:54:00+00:00` | `2549` 根 1m bar |
| L1 `3buy` | `2026-06-08 18:33:00+00:00` | `2026-06-09 17:15:00+00:00` | `2026-06-09 17:16:00+00:00` | `312` 根 1m bar |

关键证据：

- L1 `3sell` 在锚点时不存在，`2026-06-08 19:52` 仍不存在，`2026-06-08 19:53` 才可见；
- L1 `3buy` 在锚点时不存在，`2026-06-09 17:14` 仍不存在，`2026-06-09 17:15` 才可见；
- L1 `3buy` 可见瞬间，XD 数从 `877` 增至 `878`，L1 买卖点数从 `22` 增至 `23`，说明信号进入事件流依赖后续线段/下级结构确认；
- 严格成交仍为 `next_fill_time`，即 `2026-06-09 17:16:00+00:00`，没有使用 `2026-06-08 18:33:00+00:00` 锚点提前成交。

结论：

1. 原文级联确实能缓解大级别确认滞后的执行钝化：可以用已经可见的低级别买卖点管理 `swing/scalp` 小仓位。
2. 但级联不能把高级别买卖点锚点提前变成可交易信号；高级别信号仍必须等自身结构可见。
3. 当前实现的正确方向是“30m 同级别分解做核心背景，5m/1m 在各自可见买卖点上做活动仓/短差”，而不是“先算完整走势，再把最终锚点当成历史实时信号”。

## 17. 1m/L0 线段买卖点接入 scalp 层

上一轮资金层已有 `scalp`，但严格 `upgrade` 信号源主要只有 L1/L2，导致 1m/L0 短差层更多停留在图表展示。本轮把正式线段级 L0 买卖点接入严格实盘信号流。

实现：

- `live_backtest` 新增 `include_l0_upgrade_signals`；
- CLI 新增 `--include-l0-upgrade-signals` / `--no-include-l0-upgrade-signals`；
- 当 `signal_source=upgrade` 且开启该参数时，信号流 = `get_kuozhan_levels()` 的 L1/L2 + `get_branch_bspoints(use_xd=True)` 过滤出的 L0；
- L0 使用线段级 `use_xd=True`，不是笔级噪音；
- summary 新增 `include_l0_upgrade_signals`，walk-forward 信号缓存 key 也纳入该开关；
- L0 买点进入 `entry_layer=scalp`，L0 卖点只处理 `scalp`，不会误伤 L1 swing 或 L2 core。

TSLA 严格复跑：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_summary.json` | 开启 L0 的严格回放 summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_trades.csv` | 开启 L0 的分层交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_signals.csv` | 开启 L0 的信号可见审计 |
| `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_include_l0.html` | 含 L0/scalp overlay 的图表报告 |
| `D:/chanlun_pro/reports/tsla_include_l0_layer_attribution.md` | 开启 L0 后的分层归因 |

结果对比：

| 口径 | 信号数 | 交易数 | 收益 | 买持 | 超额 | 最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| L1/L2 only | `2` | `1` | `-0.60%` | `-3.72%` | `+3.12%` | `1.18%` |
| L0+L1/L2 | `29` | `2` | `-0.52%` | `-3.72%` | `+3.20%` | `0.80%` |

交易归因：

- 两笔交易均为 `entry_layer=scalp`；
- 第一笔 L0 `2buy`：`2026-06-08 15:07` 入场，L1 `3sell` 在 `2026-06-08 19:54` 退出，单笔 `+1.42%`；
- 第二笔 L0 `3buy`：`2026-06-09 14:20` 入场，L0 `3sell` / L1 `3buy` 同时可见后的 `2026-06-09 17:16` 退出，单笔 `-4.25%`；
- 分层归因显示 scalp 样本 `2` 笔，`sample_state=thin`，`guidance=watch`。

新发现：

`2026-06-09 17:15` 同一可见时刻同时出现 L1 `3buy` 与 L0 `3sell`，当前撮合先退出已有 scalp，没有在同一 bar 转入 L1 swing。这说明下一步需要按原文级别关系继续审计“低级别卖点与高级别买点冲突时，是否允许短差平仓后升级为 swing 仓”，而不能简单让所有买点按时间顺序抢单。

结论：1m/L0 已从图表展示进入严格实盘信号流，且仍保持 `anchor_time -> visible_time -> next_fill_time` 三分离。级联分析开始真实作用于仓位执行，但级别冲突处理仍需继续按原文完善。

## 18. 同 bar 低级别卖点与高级别买点的实盘滚动修正

上一轮发现 `2026-06-09 17:15` 同一可见时刻同时出现 L0 `3sell` 与 L1 `3buy`。原撮合逻辑在持仓状态下先挂卖单，随后开仓扫描因同名标的已有 pending sell 而跳过 L1 买点，导致低级别 scalp 出场后没有按已可见的高级别买点转入 swing。这个问题不是未来函数，而是现有组合层级逻辑与原文“级别联立、低级别服从高级别结构”的执行语义不够一致。

修正：

- 普通开仓与滚动开仓共用同一个买点候选构造函数，保留原有 `buy_classes`、嵌套过滤、30m/5m 方向、fund/value/ma/rs/d3、big-down activity、仓位比例乘数等门控；
- 当持仓出现非核心、非大级别 down 的卖点时，若该卖点会在下一根 bar 全部结清当前可卖层，并且同一可见 bar 上存在更高级别买点，则按同一 `visible_time` 追加一笔下一 bar 买单；
- pending 顺序保持为 sell 后 buy，因此成交路径仍是 `visible_time` 后下一根 bar 先卖出旧层，再以同一开盘价建立更高层级仓位；
- 只有 `buy_level > exit_level` 时才允许滚动，避免同级别买卖点互相抢单。

新增验证：

- `test_portfolio_backtest_same_bar_scalp_sell_rolls_into_higher_level_buy`：L0 `3buy` 建 scalp，随后同 bar L0 `3sell` + L1 `3buy` 可见，下一 bar 先退出 scalp，再建立 L1 swing；
- `tests/test_backtest_live_parity.py -q`：`79 passed`；
- 浏览器验证最新 HTML：标题 `Chanlun Visual Audit - TSLA.US`，`3` 个 SVG，`3` 个 panel，页面包含 `L0`、`L1`、`scalp`、`swing`、`2026-06-09 17:16:00+00:00`。

TSLA 最新严格复跑：

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

关键交易路径：

| 入场 | 出场 | 入场层 | 退出层 | 收益 |
| --- | --- | --- | --- | ---: |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp` | `swing` | `+1.42%` |
| `2026-06-09 14:20` | `2026-06-09 17:16` | `scalp` | `scalp` | `-4.25%` |
| `2026-06-09 17:16` | `2026-06-10 19:59` | `swing` | `all` | `-2.19%` |

信号可见性仍满足严格实盘：

| 信号 | 锚点 | 可见 | 下一根成交 |
| --- | --- | --- | --- |
| L1 `3buy` | `2026-06-08 18:33:00+00:00` | `2026-06-09 17:15:00+00:00` | `2026-06-09 17:16:00+00:00` |
| L0 `3sell` | `2026-06-09 17:14:00+00:00` | `2026-06-09 17:15:00+00:00` | `2026-06-09 17:16:00+00:00` |

结论：级联分析不能提前交易 L1 三买锚点，但可以在 L1 三买真正可见时，让 L0 三卖先结清短差仓，再用同一可见信息转入 L1 swing。这比上一版更贴近原文的级别关系：低级别处理买卖节奏，高级别决定仓位归属；任何交易仍只发生在信号可见后的下一根 bar。

## 19. 30m 同级别分解结构审计

本轮专门审计“30m 必须同级别分解，30m 以下采用非同级别分解”的结构生成路径，防止把 L2/30m 名字映射成级别，而实际仍用扩展/扩张生成。

代码合同：

- `CL._UPGRADE_CHAIN["1m"] = [("5m", "kuozhan"), ("30m", "tongjibie")]`；
- `CL._UPGRADE_CHAIN["5m"] = [("30m", "tongjibie")]`；
- `30m` 本级没有升级链，30m K 图展示本级笔、中枢、买卖点、背驰；
- `kuozhan` = 30m 以下非同级别升级，处理中枢延伸、扩展、扩张；
- `tongjibie` = 30m 同级别分解，使用低一级走势类型交替段，恰好三段重合，不把 6 段延伸吞成一个中枢。

新增/刷新审计产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.json` | TSLA 30m 同级别分解候选与入选组机器报告 |
| `D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.md` | 人工复核 Markdown 报告 |

TSLA 1m 链结构结果：

| 指标 | 值 |
| --- | ---: |
| 原始 1m bars | `96949` |
| L1/5m `kuozhan` 中枢 | `66` |
| 30m 同级别交替段 | `16` |
| 三段重合候选 | `7` |
| 入选非重叠组 | `4` |
| L2/30m `tongjibie` 中枢 | `4` |
| L2/30m 信号 | `2` |

入选 30m 同级别组：

| Group | Dirs | ZD | ZG | 时间范围 |
| --- | --- | ---: | ---: | --- |
| `0-2` | `DUD` | `314.750` | `330.110` | `2025-06-12 17:39` → `2025-07-08 17:34` |
| `5-7` | `UDU` | `330.400` | `340.550` | `2025-08-11 16:32` → `2025-11-05 15:21` |
| `8-10` | `DUD` | `422.120` | `451.460` | `2025-11-06 16:57` → `2026-04-07 18:56` |
| `13-15` | `UDU` | `406.390` | `412.430` | `2026-04-28 15:48` → `2026-06-04 15:49` |

同级别候选筛选证据：

- 全部候选有 `0-2`、`1-3`、`2-4`、`5-7`、`7-9`、`8-10`、`13-15`；
- `1-3`、`2-4`、`7-9` 因与已确认前缀重叠而不入选；
- 这说明 30m 采用的是一套可交易的前缀唯一分解，而不是把所有事后候选都画成信号。

验证：

- `test_original_level_ladder_contract_uses_30m_same_level_decomposition` 锁定升级链；
- `test_tongjibie_6_segments_two_zs_not_extended` 锁定 6 段不延伸、拆成两个 30m 盘整；
- `scripts/audit_tongjibie_tsla.py` 生成的 Markdown 报告可人工复核每个候选组。

结论：当前结构生成已经把 30m 与 30m 以下明确分开：1m→5m 用非同级别 `kuozhan`，5m→30m 用同级别 `tongjibie`。下一步收益/回撤不达标时，应继续检查同级别分解后的 30m 方向、L1/L0 入场确认与仓位比例，而不是再把 30m 当作扩展级中枢处理。

## 20. 三买结构失效过滤：可见但不可执行的旧锚点

本轮进一步审计“买卖点滞后”的执行问题：高级别三买在最终图上可能有很早的 `anchor_time`，但严格实盘必须等 `visible_time` 才知道；如果等到下一根成交时，价格已经跌破该三买关联中枢的 `ZG`，则这个三买虽然是历史结构信号，但已经不是可执行买点。

实现：

- `Signal` 新增结构字段：`structural_stop_below`、`structural_stop_above`、`zs_zd`、`zs_zg`；
- `collect_signals()` 与 `collect_branch_signals()` 从 `BuySellPoint.zs` 提取关联中枢边界；
- signals CSV 追加导出这些字段；
- 信号缓存版本升级为 `v3`，强制逐 bar 重新扫描，避免旧缓存缺少结构边界；
- 组合层候选生成时检查当前可见收盘价是否仍满足结构边界；
- pending 买单在下一根开盘成交前再次检查成交价：若 `3buy` 的开盘价低于关联 `ZG`，取消入场。

TSLA 关键证据：

| 信号 | Anchor | Visible | Next Fill | Fill Open | ZG | 处理 |
| --- | --- | --- | --- | ---: | ---: | --- |
| L1 `3buy` | `2026-06-08 18:33` | `2026-06-09 17:15` | `2026-06-09 17:16` | `390.12` | `405.63` | 取消入场 |

这说明上一轮出现的 L1 swing 亏损，不是因为要提前交易，也不是因为应忽略高级别，而是因为“信号可见时已经结构失效”。按原文第三类买点语义，三买必须是离开中枢后回试不破 `ZG`；若实盘成交价已经回到 `ZG` 下方，继续买入等于把一个失效三买当成有效三买。

最新严格复跑：

| 指标 | 结构失效过滤前 | 结构失效过滤后 |
| --- | ---: | ---: |
| 信号事件 | `29` | `29` |
| 交易数 | `3` | `2` |
| 收益 | `-1.11%` | `-0.52%` |
| 买持 | `-3.72%` | `-3.72%` |
| 超额 | `+2.61%` | `+3.20%` |
| 最大回撤 | `1.30%` | `0.80%` |

新增审计产物：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/tsla_trade_invalidation_audit.json` | 每笔交易的结构边界与首次失效时间 |
| `D:/chanlun_pro/reports/tsla_trade_invalidation_audit.md` | 人工复核报告 |

剩余问题：

- 两笔 L0/scalp 交易仍在入场后发生结构边界跌破，说明下一步应研究“已持仓后结构失效是否应触发更早退出”，而不是只等 L0/L1 卖点；
- L0 `2buy` 的完整原文失效边界已导出为“一买前低/前高”，不再用二买锚点极值近似；
- 这一步只过滤“成交时已失效”的买点，不会用未来 bar，也不会提前交易高级别锚点。

结论：买卖点滞后不能靠事后锚点解决；正确实盘口径是：等待信号真实可见，但成交时还必须满足该买点的原文结构条件。这个过滤使 TSLA 回测更接近真实缠论交易，也把后续优化方向从“提前买”转向“结构失效后的退出管理”。

## 21. 持仓后结构失效退出

上一轮只过滤“成交时已经失效”的三买，本轮进一步处理“成交时有效，但持仓后跌破结构边界”的情况。按原文第三类买点定义，三买成立的核心条件是离开中枢后的第一次回试不破 `ZG`；如果持仓后价格跌破该买点关联中枢 `ZG`，就说明该买点结构失效，不能继续机械等待后续卖点。

实现：

- `build_symbol_from_klines()` 把 `high/low` 一并传给组合层；
- 持仓建立时保存入场信号的 `structural_stop_below/above`；
- 补仓时合并结构边界，买入侧取更严格的较高 `stop_below`；
- 每根 bar 收盘后检查持仓结构边界：
  - `low < structural_stop_below` → 下一根开盘挂 `structural_invalidation` 全平；
  - `high > structural_stop_above` → 对称失效；
- 退出仍然发生在下一根开盘，不用未来 bar，也不在跌破发生前提前成交。

新增测试：

- `test_portfolio_backtest_exits_next_bar_after_structural_invalidation`：三买开仓时有效，入场 bar 低点跌破 `ZG`，下一 bar 开盘按 `structural_invalidation` 退出；
- `tests/test_backtest_live_parity.py -q`：`81 passed`。

TSLA 最新严格复跑：

| 指标 | 结构失效入场过滤后 | 持仓后结构失效退出后 |
| --- | ---: | ---: |
| 信号事件 | `29` | `29` |
| 交易数 | `2` | `2` |
| 收益 | `-0.52%` | `-0.04%` |
| 买持 | `-3.72%` | `-3.72%` |
| 超额 | `+3.20%` | `+3.67%` |
| 最大回撤 | `0.80%` | `0.15%` |

关键交易变化：

| 入场 | 原退出 | 新退出 | 原收益 | 新收益 | 原因 |
| --- | --- | --- | ---: | ---: | --- |
| `2026-06-09 14:20` | `2026-06-09 17:16` | `2026-06-09 14:47` | `-4.25%` | `-0.78%` | L0 三买跌破 `ZG=404.405` |

最新交易：

| 入场 | 出场 | 层级 | 收益 | 退出 |
| --- | --- | --- | ---: | --- |
| `2026-06-08 15:07` | `2026-06-08 19:54` | `scalp` | `+1.42%` | L1 `3sell` |
| `2026-06-09 14:20` | `2026-06-09 14:47` | `scalp` | `-0.78%` | `structural_stop_below` |

结论：这一步把“买卖点滞后”拆成两个可执行约束：第一，信号必须真实可见；第二，可见后及持仓期间必须继续满足买点结构边界。这样不需要提前使用高级别锚点，也能避免把已经失效的三买拖到后续卖点才退出。

## 22. 二买/二卖结构边界正式导出

本轮修正 L0 `2buy` 的失效边界。上一轮审计曾把二买锚点价当作保守近似边界，但按原文第二类买点，“一买后反弹再回调不破前低”才是二买成立条件；因此二买的失效边界应是一买前低，而不是二买回调段锚点或成交附近价格。二卖对称，应使用一卖前高。

实现：

- `BuySellPoint` 新增显式 `structural_stop_below/above` 字段；
- 单级别 `second_class()`：`2buy -> 一买 anchor_fx.val`，`2sell -> 一卖 anchor_fx.val`；
- 跨级别 `Bs2BranchCalculator`：次级别一类买卖点生成本级别二买/二卖时，继承本级别一类买卖点极值作为失效边界；
- `_structural_signal_fields()` 优先保留买卖点对象显式边界，再回退到 `1buy/1sell` 锚点或 `3buy/3sell` 的 `ZG/ZD`；
- 信号缓存升级到 `v4`，TSLA 严格 replay 重新逐根扫描。

TSLA 证据：

| Signal | Visible | Fill | Formal Boundary | First Break | Result |
| --- | --- | --- | ---: | --- | --- |
| L0 `2buy` | `2026-06-08 15:06` | `2026-06-08 15:07` | `388.590` | none | 持有至 L1 `3sell`，`+1.42%` |
| L0 `3buy` | `2026-06-09 14:19` | `2026-06-09 14:20` | `404.405` | `2026-06-09 14:46` | 下一根 `14:47` 结构失效退出 |

关键纠偏：此前报告中 `403.410` 是二买回调锚点/附近价格，不是原文二买的“一买前低”。用它做失效边界会把有效二买误判为失效；正式导出后第一笔交易无结构破坏。

新增测试：

- `test_second_class_1buy_pullback_holds_low_is_2buy` 验证 L0 二买携带一买前低；
- `test_second_class_1sell_pullback_holds_high_is_2sell` 验证二卖携带一卖前高；
- `test_basic_2buy`、`test_2sell_symmetric`、`test_three_levels_each_has_second` 验证跨级别二买/二卖继承本级别一类点极值；
- `test_structural_signal_fields_preserves_explicit_second_class_stop` 验证实时信号采集不会丢失显式结构边界。

最新严格 replay 不变：`signal_event_count=29`，`trade_count=2`，`total_return=-0.04%`，`buy_hold=-3.72%`，`excess=+3.67%`，`max_drawdown=0.15%`。这说明本次不是为了提高收益而放宽规则，而是把原文二买边界从事后近似改成正式结构字段。

## 23. 长窗口回测口径：no-future 不等于完整历史状态

补充口径修正：`signal_warmup_bars=-1` 的完整历史 replay 必须按一个连续 `CL` 状态逐根推进。此前为长窗口设置 `signal_scan_chunk_bars` 时，分块实现会从历史起点反复重放到每个分块末端，语义上仍不看未来，但工程上既慢，也不如“一个实盘对象连续更新”贴近真实运行。因此当前实现已调整为：完整历史模式自动关闭分块扫描；分块只允许用于 `signal_warmup_bars>=0` 的 bounded warmup 压力测试。summary 会把 `requested_signal_scan_chunk_bars` 与实际 `signal_scan_chunk_bars` 区分写出。

当前暴露的实现瓶颈也要明确记录：严格逐根回放慢，不是因为需要未来信号，而是因为 `CL.process_klines` 内高一级 MACD 与递归结构仍有全量重算路径。后续优化必须做增量/缓存，不允许为了速度退回“先全量算完买卖点再回填交易”的批处理方式。

上一轮完整历史连续长窗 replay（`2026-04-14 16:00` 到 `2026-06-10 16:00`，`signal_warmup_bars=-1`）曾在约 `2131s` CPU 后仍未落盘 summary/trades/signals。当前 v5/v6 已完成不改变信号语义的性能修复：高一级 MACD 增量化、笔/线段无变化跳过、upgrade 回放跳过不消费的 legacy zslx 路径、单根 K 线 fast path，并把信号缓存版本提升到 `v6`。

v5 完整历史长窗 policy 版已落盘，输出 `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_fullhistory_incremental_v5_policy_upgrade_l0min3_include_l0_summary.json`。结果为：信号 `172`、交易 `11`、收益 `+15.50%`、买持 `+5.65%`、超额约 `+9.85%`、最大回撤 `3.09%`、胜率 `45.45%`、结构失效退出 `3`。该结果仍严格使用 `visible_time` 后下一根开盘成交，`anchor_time` 只作结构归属。

本轮把 TSLA 严格 replay 扩展到 `2026-04-14 16:00` 到 `2026-06-10 16:00`，使用当前 `signal_source=upgrade`、include-L0、二买/三买结构边界和结构失效退出。为了让长窗口能在可接受时间内完成，使用了 `signal_warmup_bars=1200`、`signal_scan_chunk_bars=3000`。这仍然没有未来函数：每个信号只在 `visible_time` 后可交易，下一根开盘成交；但它不是完整历史状态，因为每个分块只用有限历史 warmup 初始化缠论结构。

为避免以后把两种口径混同，summary 新增 `no_future_policy`：

| 字段 | 完整短窗 | 长窗评估 |
| --- | --- | --- |
| `strict_no_future` | `true` | `true` |
| `anchor_time_tradeable` | `false` | `false` |
| `decision_time` | `visible bar close` | `visible bar close` |
| `execution_time` | `next bar open` | `next bar open` |
| `history_state` | `full_prior_history` | `bounded_warmup` |
| `history_state_complete` | `true` | `false` |
| `signal_warmup_bars` | `-1` | `1200` |
| `chunked_signal_scan` | `false` | `true` |

长窗结果：

| 指标 | 值 |
| --- | ---: |
| 窗口 | `2026-04-14 16:00` 到 `2026-06-10 16:00` |
| 信号事件 | `127` |
| 交易 | `13` |
| 收益 | `+17.30%` |
| 买持 | `+5.65%` |
| 超额 | `+11.65%` |
| 最大回撤 | `6.16%` |
| 胜率 | `53.85%` |
| 结构失效退出 | `2` |

审计文件：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_upgrade_l0min3_include_l0_v4_summary.json` | 长窗 bounded-warmup summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_upgrade_l0min3_include_l0_v4_trades.csv` | 长窗交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_upgrade_l0min3_include_l0_v4_signals.csv` | 长窗信号 |
| `D:/chanlun_pro/reports/tsla_long_20260414_0610_trade_invalidation_audit.md` | 长窗结构失效审计 |
| `D:/chanlun_pro/reports/tsla_long_20260414_0610_layer_attribution.md` | 长窗分层归因 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_fullhistory_incremental_v5_policy_upgrade_l0min3_include_l0_summary.json` | 长窗 full-prior-history policy summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_fullhistory_incremental_v5_policy_upgrade_l0min3_include_l0_trades.csv` | 长窗 full-prior-history policy 交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wf_long_20260414_0610_fullhistory_incremental_v5_policy_upgrade_l0min3_include_l0_signals.csv` | 长窗 full-prior-history policy 信号 |
| `D:/chanlun_pro/reports/tsla_long_20260414_0610_fullhistory_incremental_v5_policy_trade_invalidation_audit.md` | 长窗 full-prior-history 结构失效审计 |
| `D:/chanlun_pro/reports/tsla_long_20260414_0610_fullhistory_incremental_v5_policy_layer_attribution.md` | 长窗 full-prior-history 分层归因 |

关键修正：长窗 full-prior-history 显示 L1 `3sell`（anchor `2026-05-29 16:24`）真实首次可见是 `2026-05-29 17:25`，不是短窗旧报告里的 `2026-06-08 19:53`。短窗从 `2026-06-08` 开始时如果只批量灌入前置 K 线，`seen_keys` 没有从 2025-06-10 缓存第一根连续滚动，就会导致历史上已出现又消失的右边缘信号在窗口内重新出现。v6 已改为 `emit_start_idx` 之前逐根扫描、只登记 `seen_keys` 不输出交易事件；summary 写入 `signal_seen_registry_complete` 与 `stale_reappearing_signal_risk`，区分“CL 历史状态完整”和“信号首次可见 registry 完整”。

v6 短窗 registry probe 证据：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v6_registry_probe_hit_summary.json` | 真正连续 registry 短窗 summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v6_registry_probe_hit_trades.csv` | 真正连续 registry 短窗交易 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v6_registry_probe_hit_signals.csv` | 真正连续 registry 短窗信号 |
| `D:/chanlun_pro/reports/tsla_wffull_window_v6_registry_trade_invalidation_audit.md` | 真正连续 registry 结构失效审计 |
| `D:/chanlun_pro/reports/tsla_wffull_window_v6_registry_layer_attribution.md` | 真正连续 registry 分层归因 |

v6 从 TSLA 1m 缓存第一根 `2025-06-10 16:00` 扫到交易起点 `2026-06-08 13:30`，`emit_start_idx=96409`，`signal_seen_registry_complete=true`，`stale_reappearing_signal_risk=false`。短窗信号事件从旧口径 `29` 降为 `28`，交易从 `2` 笔降为 `1` 笔：L0 `2buy` 在 `2026-06-08 15:07` 入场，不再被旧 L1 `3sell` 于 `19:54` 退出，而是持有到 `2026-06-09 16:18` 跌破 `structural_stop_below=388.590` 后结构失效退出，单笔 `-3.84%`。cache-hit 复跑约 5 秒完成，证明该连续 registry 结果可复用。

因此，长窗 bounded-warmup 仍只能作为无未来压力测试；v5 full-prior-history 长窗可以证明 `2026-04-14` 之后的连续窗口无未来交易，但短窗最终口径必须以 v6 `signal_seen_registry_complete=true` 为准。不能再把短窗旧 L1 `3sell` 退出当作最终实盘结论。

## 24. 原文三段口径默认化与 v7 严格回放

本轮继续处理第 3 节 P0：L0 中枢“3 段定义”和“第 4 段确认可见”不能混成同一个 4 段定义。按原文，中枢本体是三段次级别走势类型重叠；对线段级实盘来说，下一段可以用来确认第三段已完成，但它应影响 `visible_time`，不应改写中枢本体。

实现修正：

- `live_backtest.DEFAULT_RECURSIVE_L0_MIN_ZS_LINES` 从 `4` 改为 `3`；
- `recursive_bt.engine.CL_CFG` 显式写入 `recursive_l0_min_zs_lines=3`；
- `_cl_config()` 永远把 `recursive_l0_min_zs_lines` 写入 CL config，避免 summary 写 3 而底层默认回 4；
- 信号缓存版本升为 `v7`，并且 cache meta 永远包含 `recursive_l0_min_zs_lines`，避免 3 段/4 段缓存串用；
- 新增 `test_calculator_min3_confirms_three_segment_center_on_next_leave`，锁定“三段成中枢、下一段脱离确认可见”的行为。

原文资料索引也升级为全量图表锚点：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/chanlun_original_index.json` | DOCX 原文索引：正文段落 `20728`、媒体图 `1061`、图表锚点段落 `1100`、课文标题 `122`、回复类锚点 `2399` |
| `D:/chanlun_pro/reports/chanlun_original_logic_matrix.json` | 原文要求覆盖矩阵 |
| `D:/chanlun_pro/reports/chanlun_original_logic_matrix.md` | 覆盖矩阵 Markdown，当前静态 gap count = `0` |

v7 TSLA 真正连续 registry 短窗回放：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_summary.json` | 首次 v7 原文三段 + 全历史 registry summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_trades.csv` | 首次 v7 trades |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_signals.csv` | 首次 v7 signals |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_hit_summary.json` | cache-hit 复跑 summary，`hits=1/misses=0` |
| `D:/chanlun_pro/reports/tsla_wffull_window_v7_registry_trade_invalidation_audit.md` | v7 结构失效审计 |
| `D:/chanlun_pro/reports/tsla_wffull_window_v7_registry_layer_attribution.md` | v7 分层归因 |
| `D:/chanlun_pro/reports/chanlun_visual_audit_tsla_v7_registry.html` | v7 多级别图表审计：1m 显示笔、1m/5m/30m 中枢、买卖点、背驰；5m 显示笔、5m/30m 中枢、买卖点、背驰；30m 显示笔、30m 中枢、买卖点、背驰 |

v7 结果与 v6 严格口径一致，但证据链更完整：`recursive_l0_min_zs_lines=3`，`signal_warmup_bars=-1`，`signal_seen_registry_complete=true`，`stale_reappearing_signal_risk=false`，信号事件 `28`，交易 `1` 笔，总收益 `-1.43%`，买持 `-2.54%`，最大回撤 `2.68%`，基准回撤 `8.34%`。L1 `3sell` 在窗口内为 `0` 行；唯一 L1 信号是 `2026-06-09 17:15` 可见的 L1 `3buy`。交易仍为 L0 `2buy` 在 `2026-06-08 15:07` 入场，`2026-06-09 16:18` 按 `structural_stop_below=388.590` 结构失效退出，单笔 `-3.84%`。

浏览器审计也按 v7 产物复核完成：1m 面板 `931` 根 K 线、`64` 笔、`6` 个笔中枢、递归层级中枢 `{L0:140,L1:66,L2:4}`、overlay 信号 `28`、交易 `1`；5m 面板 `8898` 根 K 线、`582` 笔、`73` 个笔中枢、递归层级中枢 `{L0:25,L1:1}`；30m 面板 `858` 根 K 线、`70` 笔、`10` 个笔中枢、递归层级中枢 `{L0:3}`。Playwright 截图与像素检查通过，截图位于 `D:/chanlun_pro/browser_verify/v7_visual_full.png`、`v7_visual_1m_panel.png`、`v7_visual_5m_panel.png`、`v7_visual_30m_panel.png`；各面板非白像素占比分别约 `9.61%`、`16.08%`、`13.49%`，排除空图或 SVG 未渲染。

结论：这轮不是调参优化收益，而是把原文三段中枢本体正式设为实盘回测默认口径，并用 v7 缓存隔离证明没有复用旧 4 段/旧 registry 结果。后续所有 TSLA 收益/回撤讨论，都应以 `v7`、`signal_seen_registry_complete=true`、`recursive_l0_min_zs_lines=3` 为最终短窗证据基线。

## 25. 原文交易体系矩阵与剩余缺口

本轮把“原文结构正确”继续推进到“交易体系是否完整”的审计。新增 `scripts/audit_original_trading_system_matrix.py`，输出：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/chanlun_original_trading_system_matrix.json` | 原文交易体系机器矩阵 |
| `D:/chanlun_pro/reports/chanlun_original_trading_system_matrix.md` | 原文交易体系人工复核矩阵 |

矩阵当前结果更新为：`6 pass / 1 partial / 1 gap`。

| 项 | 结论 |
| --- | --- |
| 原文全文/回复/图表索引 | `pass`，正文 `20728` 段、图片 `1061` 张、逻辑矩阵 gap 为 `0` |
| 30m 同级别 + 30m 以下非同级别 | `pass`，`1m -> 5m kuozhan -> 30m tongjibie` 已有代码、测试和 TSLA 审计 |
| 无未来逐根回放 | `pass`，v7 summary 证明 `visible_time` 决策、下一根开盘成交、连续 registry 完整 |
| 级联滞后控制 | `pass`，cascade 审计已切到 v7 signals；旧 L1 `3sell` 不再作为当前窗口事件 |
| 结构边界/失效退出 | `pass`，买卖点导出结构止损，TSLA 唯一交易按结构失效退出 |
| 三系统选股 | `partial`，A 股有基本面+比价+技术面可执行证据；当前 TSLA/core-9 US 回放仍是技术面为主，不能称为完整原文选股体系 |
| 多层仓位 | `pass`，core/swing/scalp 与买入比例已有；新增 `--sell-ratio-policy original_layered`，可按级别、大级别方向与核心/活动层机械化减仓 |
| 通用低回撤高收益证明 | `gap`，v7 TSLA 短窗只有 `1` 笔交易，是正确性基线，不足以证明通用鲁棒体系 |

同时重跑了 `scripts/audit_cascade_confirmation_tsla.py`：默认信号源已改为 `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_hit_signals.csv`，默认只审计 `L1+` 级联信号。当前报告仅剩 L1 `3buy`：anchor `2026-06-08 18:33`，visible `2026-06-09 17:15`，next fill `2026-06-09 17:16`，anchor 到 visible 滞后 `312` 根 1m bar。旧报告中的 L1 `3sell` 是 v6/v7 已排除的 stale reappearing 信号，不能再用于实盘结论。

新增分层卖出 policy：

| 文件/字段 | 说明 |
| --- | --- |
| `src/chanlun/recursive_bt/engine.py:recommended_sell_ratio(policy="original_layered")` | 大级别 down 或核心级别卖点全退；大级别 up 时小级别三卖只减活动层的一部分 |
| `src/chanlun/recursive_bt/portfolio.py:sell_ratio_policy` | 回测循环把 exit level、core level、swing level 传入卖出比例函数 |
| `src/chanlun/recursive_bt/live_backtest.py:--sell-ratio-policy original_layered` | CLI 可显式启用原文分层减仓 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_summary.json` | TSLA v7 分层卖出复跑 summary |
| `D:/chanlun_pro/reports/us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_trades.csv` | TSLA v7 分层卖出复跑交易 |

TSLA v7 分层卖出复跑仍为 `1` 笔交易，收益 `-1.43%`、买持 `-2.54%`、最大回撤 `2.68%`、信号 `28`，`signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false`。结果与基线一致的原因是唯一退出为 `structural_invalidation/structural_stop_below`，按原文结构失效也必须全退；该复跑的意义是证明 `original_layered` policy 已进入严格无未来执行链，不是收益优化。

本轮继续补两类审计：

| 文件 | 说明 |
| --- | --- |
| `D:/chanlun_pro/reports/us_selection_source_audit.md` | US/core9 三系统选股数据源审计 |
| `D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md` | 现有 TSLA/US summary 的无未来证明强度分级 |

US 选股数据源审计结论：core9 的 `1m/5m/30m` 技术 K 线缓存齐全，技术系统为 `pass`；但本地 US 基本面/行业地位/估值缓存覆盖为 `0/9`，所以 fundamental 与 comparison 均为 `gap`。这意味着 TSLA/core9 当前不能被描述为“完整原文三系统选股”，只能说技术执行链严格。

稳健性证据审计结论：本地扫描到 `strict_original_registry` 报告 `5` 个，但最佳严格原文 registry 报告只有 `1` 笔交易；`bounded_warmup_walk_forward` 有 `35` 个、`legacy_or_unknown` 有 `50` 个、`strict_but_registry_incomplete` 有 `6` 个。旧 core9 高交易数报告只能作为研究参考，除非按 v7 三段中枢 + continuous registry 重生成，否则不能作为最终“通用低回撤高收益”证明。

工程尝试：用 `TSLA/QQQ/NVDA` 三标的跑 `2026-06-01` 到 `2026-06-10` 的 v7 full-history continuous registry + `original_layered`，首轮超过 `6` 分钟仍未落盘，已停止该进程。后续要扩大严格样本，应优先做可恢复/分标的事件缓存预热，而不是让一次性 core3/core9 full-history 扫描阻塞验收。

因此，若后续严格回测仍无法达到收益/回撤目标，优先检查顺序应是：

1. US/TSLA 是否补齐 point-in-time 基本面、行业地位和估值/比价数据源，而不是把单票技术面 replay 当完整选股；
2. 用可恢复的 v7 event-cache 预热扩大 TSLA/core9 样本窗口，并启用/对比 `original_layered`，证明收益/回撤，而不是回退到批量预计算或旧信号重现路径。

## 26. v7 严格信号缓存预热机制

为解决 core3/core9 strict replay 首跑超时且无落盘的问题，本轮新增 `scripts/prewarm_live_backtest_signal_cache.py`。该脚本不重新定义缠论逻辑，只逐标的调用现有 `live_backtest.load_chart_cache_syms()`，让每个标的的 v7 walk-forward 信号缓存独立完成、独立落盘、独立写 manifest。

关键约束：

| 约束 | 说明 |
| --- | --- |
| 不使用未来信号 | 仍由 `live_backtest` 逐根推进 CL 对象，信号在 visible bar close 后才进入事件表 |
| 不把 full-history registry 切假块 | `signal_warmup_bars=-1` 下保持连续扫描；chunk 参数只记录为请求值，不改变 no-future policy |
| 不改变交易规则 | 脚本只预热 `signal_cache_dir`，不调参、不筛选收益、不运行组合优化 |
| 可恢复 | 每个 code 完成后写 manifest；同配置再运行会跳过已完成 code |

已验证 TSLA v7 manifest：

| 文件 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_tsla_v7_registry_manifest.json` | `ok=1/1`、`registry_complete=1`、`events=28`、`cache hits/misses/writes=1/0/0` |

这一步把后续扩大严格样本的瓶颈从“一次性 core3/core9 组合回测”改成“可续跑的逐标的缓存预热”。不过它只解决工程可恢复性，不解决 US 三系统数据源缺口，也不增加严格样本交易数；因此原文交易体系矩阵中的稳健性证明仍保持 `gap`。

## 27. full-history registry checkpoint 审计

逐标的预热后继续尝试 `TSLA/QQQ/NVDA` 的 `2026-06-01` 到 `2026-06-10` strict v7 registry。未带 checkpoint 的 TSLA 预热日志显示：

| 日志 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/prewarm_logs/core3_202606_v7_prewarm.out.log` | TSLA 从 `5000/96710` 推进到 `70000/96710`，`emit_start_idx=93829`，事件仍为 `0` |
| `D:/chanlun_pro/reports/prewarm_logs/core3_202606_v7_prewarm.err.log` | 空 |

该运行被停止且没有 cache/manifest，因为旧机制只在单标的完整扫描结束后落盘。结论：严格无未来长窗扩容的主要瓶颈在单标的 full-history first-seen registry 扫描内部。

为此新增 checkpoint：

| 位置 | 作用 |
| --- | --- |
| `src/chanlun/recursive_bt/live_backtest.py:_signal_checkpoint_settings` | 根据严格 signal cache meta 生成 checkpoint 路径 |
| `src/chanlun/recursive_bt/live_backtest.py:_load_signal_checkpoint/_write_signal_checkpoint` | 保存/恢复完整 CL 状态与 first-seen registry |
| `scripts/prewarm_live_backtest_signal_cache.py:--checkpoint-every/--checkpoint-dir` | 在逐标的预热 CLI 中显式开启 checkpoint |

真实数据验证：

| 验证 | 结果 |
| --- | --- |
| 首跑 | TSLA `5000/96710` 后写出 `6ff7c746920fc72419678b3f50fec8ca.checkpoint.pkl` |
| 恢复 1 | 从 `5000` 继续到 `10000/96710`，checkpoint 文件增长 |
| 恢复 2 | 从 `10000` 继续到 `15000/96710`，checkpoint 文件继续增长 |
| 恢复 3 | 从 `15000` 继续到 `25000/96710`，checkpoint 文件约 `10.7MB` |
| 恢复 4 | 从 `25000` 继续到 `55000/96710`，checkpoint 文件约 `23.6MB` |
| 恢复 5 | 从 `55000` 继续到 `60000/96710`，checkpoint 文件约 `25.5MB` |

测试验证：

| 测试 | 说明 |
| --- | --- |
| `test_walk_forward_signal_scan_checkpoint_resume_matches_full_run` | checkpoint 后故意崩溃，再恢复，信号结果必须等于一次性完整扫描 |
| `test_walk_forward_signal_checkpoint_writes_before_emit_start` | 覆盖交易窗口前的历史注册分支，防止 `emit_start_idx` 很大时不落 checkpoint |

后续续跑已完成 TSLA `2026-06-01~2026-06-10` 的 full strict cache，并用 cache-hit 完成严格回测：

| 文件 | 结果 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_tsla_202606_v7_registry_manifest.json` | TSLA `ok=1/1`、`registry_complete=1`、`events=36` |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_summary.json` | `hits=1/misses=0/writes=0`、`signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false` |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_signals.csv` | 36 个可见信号，首批 L1/L0 卖点在 `2026-06-02 14:03` 可见 |
| `D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v7_registry_layered_trades.csv` | 0 笔交易 |

严格 TSLA 长窗结果：组合收益 `0.00%`，买持 `-9.64%`，最大回撤 `0.00%`，基准回撤 `11.68%`，交易 `0`。这说明严格原文门槛在这段样本中避免了下跌窗口交易，但也没有产生收益；因此它不能作为“低回撤高收益”证明。

`D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md` 已刷新：`strict_original_registry` 报告数为 `6`，但最佳严格报告仍只有 `1` 笔交易，`robust_claim_supported=false`。core3/core9 严格组合回测仍需等 QQQ/NVDA 也按同样 registry 方式完成。

## 28. core3 full strict registry 组合复核

本轮已完成 `QQQ.US`、`NVDA.US` 的同口径 strict cache 预热，并与 TSLA 一起做 `2026-06-01~2026-06-10` 的 core3 组合复跑。全部使用 `signal_mode=walk_forward`、`signal_source=upgrade`、`include_l0_upgrade_signals=true`、`recursive_l0_min_zs_lines=3`、`signal_warmup_bars=-1`、`sell_ratio_policy=original_layered`，且组合复跑命中缓存 `hits=3/misses=0/writes=0`。

| 产物 | 结论 |
| --- | --- |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_qqq_202606_v7_registry_manifest.json` | QQQ `registry_complete=1`、`events=31` |
| `D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_nvda_202606_v7_registry_manifest.json` | NVDA `registry_complete=1`、`events=40` |
| `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v7_registry_layered_summary.json` | core3 `signal_seen_registry_complete=true`、`stale_reappearing_signal_risk=false`、`signal_event_count=107` |
| `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v7_registry_layered_trades.csv` | 4 笔严格实盘式交易 |

结果：组合收益 `-4.00%`，等权基准 `-6.64%`，超额 `+2.64%`，最大回撤 `4.54%`，基准回撤 `9.79%`，胜率 `50%`，交易 `4`。这说明严格原文链路在该窗口降低回撤、跑赢下跌基准，但没有达到“收益高”的目标，也没有足够交易数支持通用鲁棒结论。

关键亏损来自 NVDA 的 L1 `3buy`：`visible_time=2026-06-02 15:46`，下一根 `2026-06-02 15:47` 成交，后续 `2026-06-09 16:11` 因 `structural_invalidation/structural_stop_below` 全退，单笔约 `-10.60%`。这条链路没有使用未来信号；问题更可能在当前买点质量、L1 走势类型完成度、结构止损边界或 30m 背景下活动仓准入。

审计矩阵已同步更新：`D:/chanlun_pro/reports/chanlun_original_trading_system_matrix.md` 仍为 `6 pass / 1 partial / 1 gap`；`D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md` 更新为 `strict_original_registry=7`、最佳严格交易数 `4`、`robust_claim_supported=false`。

因此，若后续仍无法达到目标，优先检查当前缠论逻辑是否和原文全文完全一致，而不是继续参数搜索：5m/1m 的走势类型是否只在真实完成后进入上级别，L0/L1 买点是否混入了过短的笔级噪声，30m 同级别背景是否只用来限制核心仓而错误放行了活动仓，以及结构失效是否过宽或过窄。
