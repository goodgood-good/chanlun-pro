# 区间套 operable 闭环到买卖点介入决策——设计研究报告

- 日期：2026-06-13
- 背景：第75轮原文一致性审计 gap D（判据 C2.16）——区间套目前只是"时间区间包含"近似的标注（operable/nest_depth），不改变介入时点；L1 级买卖点确认滞后实证 170-519 根 1m bar，NVDA 2026-06-02 L1 3buy 案例 -10.60%。
- 目标：设计"L1+ 背驰类买点候选 → 下推次级别（L0/笔级）寻找 walk-forward 首次可见的确认 → 作为介入触发"的 operable 闭环，并给出量化预案。
- 状态：已完成（五节全部落盘；§4.1 含 2026-06-13 实跑试算结果）。

---

## 一、原文判据：区间套的原文口径

> 判据编号沿用 `docs/yuanwen_study/topic5_beichi_qujiantao.md`（C5.x）、`topic4_maimaidian.md`（C4.x）、`topic2_yanshen_kuozhan_kuozhang_3buy.md`（C2.x）；「行NNNNN」为 chanlun.txt 真实行号。

### 1.1 程序定理与嵌套保证

- **C5.38 精确大转折点寻找程序定理**（27课，行17064-17068）：「某大级别的转折点，可以通过不同级别背驰段的逐级收缩范围而确定」；程序＝「先找到其背驰段，然后在次级别图里，找出相应背驰段在次级别里的背驰段，将该过程反复进行下去，直到最低级别，相应的转折点就在该级别背驰段确定的范围内」。
- **C5.39 嵌套保证**（27课万科例，行17057-17060）：「季度图上的第三段，在月线上，可以找到针对月线最后中枢的背驰段，而这背驰段，一定在季度线的背驰段里，而且区间比之小」——季→月→周→日→30m→5m→1m→分笔，「这区间不断缩小」。
- **C5.40 精度边界**（行17062、17070）：级别非无限可分、无数学极限点；「1 分钟的背驰段，一般就是以分钟计算的事情，对于大级别的转折点，已经足够精确了」。
- **C5.7 区间套入口**（37课，行24437）：背驰段 c 至少包含两个次级别中枢（构成次级别趋势）→「继续套用 a+A+b+B+c 的形式进行次级别分析……形成类似区间套的状态，这样对其后的背驰就可以更精确地进行定位」。

### 1.2 当下递归规则（61课标准图解，walk-forward 候选机制的原文依据）

- **C5.41 / C5.26 先假设、等证伪**（行33015-33017、26691）：「65 开始的走势，由于没实际走出来，所以在和 55-60 比较时，都可以先假设是进入背驰段。而当走势实际走出来，一旦力度大于前者，那么就可以断定背驰段不成立，也就不会出现背驰」。即：**大级别候选的成立不等待大级别自身完成确认，而是「未被否定前即视为候选」**；候选的否定路径明确（力度/新高证伪）。
- **递归进入内部结构**（行33017）：「在没有证据否定背驰之前，就要观察从 65 开始的一段其内部结构中的背驰情况，这种方法可以逐次下去，这就是区间套的定位方法，这种方法，可以在**当下**精确地定位走势的转折点」。
- **C5.42 每一重内部约束**（行33018-33020）：每一重仍受「创新高才有背驰」（69 未创新高不构成背驰）与「相邻同向段先比」（69-70 对 67-68 无盘整背驰则 70 不卖）约束。
- **四重嵌套定位**（行33028-33033）：65 第一重→69 第二重（背驰段的背驰段）→71 第三重→71 内部第四重，「72 点这个背驰点的精确定位，是由 65 开始背驰段的背驰段的背驰段的背驰段构成的……这一切，都可以**当下地**进行」。
- 27课实战版三重背驰（行17416-17418、17433-17436）：「在 5 分钟进入背弛段后，找 1 分钟相应段的背弛段，再找 1 分钟背弛段的背弛段，这样就可以精确定位了」——2007-02-06 大盘底实例。

### 1.3 嵌套条件：结构嵌套，而非单纯时间包含

原文嵌套对象是「**背驰段→其内部结构中的背驰段**」的父子结构关系，时间包含只是它的必要投影：

1. **结构维度**：次级别背驰段必须是本级别背驰段 c 的内部结构（c 内部针对其最后一个次级别中枢的离开段，C5.7 行24437；61课每一重起点 65→69→71 都是前一重内部针对最近中枢的三买/离开点，行33020-33022）。任意时间上重叠但结构上不属于 c 内部走势的背驰，不构成区间套的一环。
2. **价格（空间）维度**：每一重都必须创新高/新低（C5.42 行33018-33019：「只有等 70 点出现时，大盘才进入真正的背驰危险区」），转折点的价格范围随之逐级收缩（C5.39「区间比之小」「区间不断缩小」）。
3. **时间维度**：嵌套链每一重的起点严格右移（65→69→71→72），时间区间是逐级收缩的右端子区间——时间包含是结果而非判据本身。

结论：**用「时间区间包含」近似区间套（现状实现）只取了第 3 条投影，丢掉了第 1 条结构归属与第 2 条创新高/新低收缩**，这正是第75轮审计 gap D 的实质。

### 1.4 方向约束：同向性

区间套链各重背驰段必须同向（找底部则每一重都是下跌背驰段，找顶部则每一重都是上涨背驰段）。27课定理的「逐级收缩」隐含同向（万科例全部为下跌背驰段，行17057-17060）；P6 设计文档（`docs/chanlun_core_redesign_6_区间套_design.md` §1）援引宪法 §7.2 明确为「同向、逐级时间包含的嵌套背驰链」。**注意 61课的「区间套找卖点」与本研究「大级别买点候选下推次级别确认」方向相反但程序同构**。

### 1.5 精确介入点的原文定义

- **趋势背驰类（1买/1卖）**：转折点「在最低级别背驰段确定的范围内」（C5.38 行17068）；61课 72 点＝四重背驰点出现后「卖是唯一的选择，而区别只在于卖多少」（C5.44 行33035-33037）——**介入时点＝最低可见级别背驰点当下出现之时**，仓位按操作级别定（操作级别≤5m 全清/全进，大级别按震荡容纳量对冲）。
- **2/3 类买卖点（gap D 引用的 C2.16）**：「3买回试段的『完成』以再次级别 3 段走势类型呈现为准（日线中枢：30分钟回试由5分钟图 3 段确认），**精确买点参考该次级别的第一类买点**」（L10399-10401、L13115-13120）。即 2/3 买的介入点同样下推：回试段＝次级别向下走势，其结束点＝次次级别结构完成处的**次级别一买**。
- **C4.27 方向性约束**（L17055、L17418、L30640）:大级别买点可且应通过逐级嵌套的次级别背驰段精确化（大级别买点 ⊇ 各级别小买点序列）；**反向不成立**——小级别买点不必然反推大级别买点，升级须满足小转大必要条件（C5.34：最后一个次级别中枢的三类卖/买点，行26925-26933，必要非充分）。
- **C5.46 实战安全级差**（行18062-18063）：「关键是先找到大一点级别的背驰段，然后再用小级别的背驰来找精确买点……最好就是在 30 分钟的背驰段用 5 分钟找买点，短线这样就比较安全了」——级差以一档为宜，不必一推到底。
- **C4.28 介入程式**（L31259、L30785）：散户日线以上 1买非必需，2买/3买介入最晚最确定；资金有规模「至少从第二类买点开始利用部分前戏的介入」——多批次介入与区间套逐重加仓相容。

### 1.6 对本设计的直接约束（小结）

1. 大级别候选＝「进入背驰段」状态，先假设成立、可被证伪（C5.41），无须等大级别走势类型完成——**这是解除 L1 确认滞后 170-519 bar 的理论钥匙**。
2. 介入触发＝候选背驰段**内部结构**中次级别（递归至更低）背驰点/一类买点的当下出现（C5.38、C2.16），不是时间窗口里任意低级别信号。
3. 每一重必须校验：同向、结构归属（针对前一重内部最近中枢）、创新高/新低（C5.42）。
4. 失效路径原生存在：候选被力度证伪（新高/面积反超）→ 链整体作废（C5.41）；3买回试跌破 ZG/跌回中枢→候选转化（C5.23 盘整背驰转三卖先验）。
5. 仓位语义：区间套给出的是**介入时点**；仓位/批次由操作级别决定（C5.44、C4.28），引擎不应把区间套介入误写成满仓语义。

---

## 二、现状：annotate_nest / nest_mode / nest_soft / operable 的完整链路（file:line）

> 行号基于当前工作区（分支 fix/zhongshu-l0，2026-06-13）。

### 2.1 信号生产的两条源（nest 只接了其中一条）

| 信号源 | 函数 | 来源 | nest 通道 |
|---|---|---|---|
| `branch`（原生图全量 L0+升级级） | `engine.collect_branch_signals` `src/chanlun/recursive_bt/engine.py:333-418` | `cd.get_branch_bspoints(use_xd=...)` | **有**：`annotate_nest=True` 时写 `Signal.nest_operable/nest_depth`（engine.py:403-414） |
| `upgrade`（kuozhan 升级级 L1+，**v8 生产主链**） | `engine.collect_signals` `engine.py:240-256` | `cd.get_kuozhan_levels()` | **无**：不写 nest 字段，`Signal.nest_operable` 恒 None、`nest_depth` 恒 0 |

`Signal` 数据类定义于 engine.py:188-199（`nest_operable: Optional[bool] = None`、`nest_depth: int = 0`）。

### 2.2 operable 标注怎么算（结构 READ，与 CL 主链平行）

`collect_branch_signals` 内嵌 `_interval_nest_reads()`（engine.py:361-382）：

1. 取 `cd.get_bis()`（或 xds）→ `RecursiveBranchCalculator(l0_min_zs_lines).calculate(units, ld, wzgx, freq)` 重建多级 LevelResult（engine.py:374-380；wzgx 取 `cd.config["zs_wzgx"]`，与 `CL._recursive_wzgx` 统一，engine.py:370-372 注释即 2026-06-12 审计修复点）；
2. `BeichiNestCalculator().calculate(levels)` 构建嵌套背驰森林（engine.py:381）；
3. `IntervalNestCalculator().calculate(forest)` 标注（engine.py:382）。

**嵌套判定（gap D 的近似所在）**：`src/chanlun/core/beichi_nest.py:3-4` 模块注释自述「按『同向 + 严格时间包含』自底向上挂成嵌套森林」；span 取 `leave_seg` 的 K 线序号区间 `(c.start.k.k_index, c.end.k.k_index)`（beichi_nest.py:31-35）；挂载条件 `hi_s <= lo_s and lo_e <= hi_e`（严格时间包含、边界可贴合，beichi_nest.py:48），多候选取跨度最小者。**未校验**：①次级别背驰是否结构上属于大级别背驰段 c 的内部（针对 c 内最近次级别中枢）；②创新高/新低逐重收缩（C5.42）。

**operable 定义**：`src/chanlun/core/interval_nest.py:30-46`——DFS 标 `depth`（根=1）、`is_innermost`（无 children）、`is_nested`（depth>1），`operable = is_innermost and is_nested`。纯快照标注，每次全量重算，无事件/时间语义。

**匹配回买卖点**：engine.py:387-397 以 `id(divergence)` 为主键、`_div_key`（level+kind+leave_seg 端点）为备援键建索引；engine.py:405-410 给带 `divergence` 的 bspoint 写 `nest_operable/nest_depth`，找不到 read 时写 `False/0`。无 `divergence` 的点（典型如三类点的非背驰回试）保持 `None/0`——P6 设计文档明确「三类点（回试、非背驰，不在嵌套森林）的可操作性留后」（`docs/chanlun_core_redesign_6_区间套_design.md` §0 不含清单）。

### 2.3 annotate_nest 在回测的生效环节

- 开关解析：`live_backtest.py:2015` `annotate_nest = "nest" in args.require or "nest_soft" in args.require`，传入 `load_chart_cache_syms/load_online_syms`（2051、2072）。
- 进 v8 缓存 key：`_signal_cache_meta`（live_backtest.py:239-283）把 `"annotate_nest": bool(...)` 写入 meta（269 行），`_SIGNAL_CACHE_VERSION = "v8"`（live_backtest.py:47）→ 开/关 nest 是**不同的缓存条目**，互不污染。
- wf 扫描内生效：`_walk_forward_signals_by_main_bar`（live_backtest.py:783 起）逐 main-bar 喂 CL（959-969），状态签名变化时 `_collect_visible_signals(..., annotate_nest=annotate_nest, ...)`（975-982）→ branch 分支传入 `collect_branch_signals`（752-756）。
- **upgrade 分支不传 nest**：`_collect_visible_signals` 的 upgrade 分支（live_backtest.py:741-750）调 `collect_upgrade_signals`（=engine.collect_signals，live_backtest.py:25 别名）且 `include_l0_upgrade_signals` 的 L0 补充也硬编码 `annotate_nest=False`（746 行）。
- 其它硬编码 False：live_backtest.py:1311、1368、1410、1475（大/中级别流、scalp 流等）。只有操作级 branch 流（1420 行）透传 annotate_nest。
- CSV 落盘：`_signal_event_rows_for_bars`（live_backtest.py:1058-1105）输出 `nest_operable`/`nest_depth` 列（1098-1099）。
- **实测后果**：`us_core3_mtf3_20260601_0610_v8_registry_layered_signals.csv` 共 98 个事件、stream 全为 `upgrade`，`nest_operable` 全空、`nest_depth` 全 0——生产主链（registry/upgrade 源）的 nest 列形同虚设。

### 2.4 nest_mode 消费端（filter / soft）

- **live_monitor.py**（实时监控）：
  - CLI/配置：`--require-nest`、`--nest-mode(off/filter/soft)`（解析 live_monitor.py:1913-1918；`require_nest=True` 强制 `nest_mode="filter"`，489 行）。
  - `_nest_signal_ok`（live_monitor.py:457-461）：**只约束 1/2 类买点**（`nest_operable is True` 才过），3 类买点与所有卖点直接放行。
  - filter：`collect_monitor_events` 内买点分发处直接 `continue` 丢弃（515-516）；soft：进 `recommended_buy_ratio`（543-554）。
  - monitor 的操作级信号用 branch 源且**常开** nest 标注：`collect_branch_signals(self.cd_op, use_xd=False, annotate_nest=True)`（live_monitor.py:873）。
- **engine.recommended_buy_ratio**（engine.py:112-118）：`nest_mode == "soft"` 且 1/2 类买点时——`operable is True`→×1.0；`nest_depth>0`→×0.75；否则×0.5。即 soft 是**仓位折扣**。
- **portfolio.py**（组合撮合）：`nest_mode = "filter" if "nest" in require else ("soft" if "nest_soft" in require else "off")`（portfolio.py:526）；`_nest_filter_ok`（148-152，逻辑同 monitor）在三处买点过滤（692、1257、1299）；soft 折扣经 `recommended_buy_ratio`（758-764）。

### 2.5 为何不改变介入时点（根因分解）

1. **介入时点由「首次可见」唯一决定**：wf 扫描以 `_signal_identity` 维护 `seen/active` 集合，新身份首次出现即记入该 main-bar 的 `fresh`（live_backtest.py:1004-1018），成交= t+1 开盘（1071-1078 `next_fill_*`）。nest 是挂在该信号上的附注；filter 只能在这个**既定时点**丢弃信号、soft 只能缩放仓位，二者都**不产生任何更早的事件**。
2. **operable 的标注时点 ≥ 大级别信号可见时点**：嵌套森林只收「已固化背驰段」（beichi_nest.py:19-23），大级别背驰固化之时即其买卖点可见之时；换言之 operable=True 是在大级别确认**之后**附加的质量标签，结构上不可能把介入点提前。原文要求的是反向时序——大级别处于「候选（背驰段进行中）」时就下推次级别找介入（C5.38/C5.41）。
3. **生产主链没接 nest**：v8 registry 回测与分层报告走 upgrade 源（kuozhan L1+），该链不标 nest（2.1/2.3 节实测）；`annotate_nest` 只在 require 含 nest/nest_soft 时才开（live_backtest.py:2015），而生产默认 require 不含。
4. **嵌套判定是时间近似**：「严格时间包含+同向」缺结构归属与创新高/新低校验（2.2 节），意味着即便接通，operable 的成色也未达原文判据（第75轮审计 gap D 原文：「区间套用时间区间包含近似空间嵌套，operable 标注未闭环到买卖点介入决策（C2.16）」，spec §75 审计表 D 行）。
5. **快照标注 vs 事件语义**：`NestRead` 无 walk-forward 首次可见语义；右边缘 provisional 背驰不入森林（P6 设计 §0 留后），故嵌套链在右边缘的「当下递归」（61课，C5.41）完全缺位。

### 2.6 与滞后实证的对接

- L0 买卖点 anchor→首次可见中位 9 根 1m bar；**L1 级 170-519 根**（4 个 L1 事件：170/170/519/312，`D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md`）。审计结论原句：「A lower-level cascade can be used for smaller swing/scalp layers only after its own signal is visible」「Using the higher-level anchor before visible_time would be a future signal」。
- NVDA 案例（gap D 核心案例）在 CSV 中的原始行：L1 3buy `anchor=2026-05-29 19:59`、`visible=2026-06-02 15:46`、`fill=15:47 @225.599`、`structural_stop_below=201.488`（=该 L1 中枢 zs_zg）；同 anchor 的 L1 1buy 并发。入场后 MFE +0.06%、7 天阴跌至 201.68 全退，-10.60%。**注意**：anchor 价 210.877→fill 价 225.599，确认滞后本身吃掉了 +7.0% 的位置优势，而结构止损边界是按 anchor 时刻结构（中枢 ZG）定的——「按 anchor 结构设止损、按 visible 价格入场」的错位是该笔风险收益比畸形（上 0.06% / 下 10.7%）的直接原因。

---

## 三、闭环设计：大级别候选 × 次级别确认 → 介入触发

### 3.0 滞后的结构根源（设计靶点）

kuozhan L1 信号的判定单位是「下级中枢摆动腿」（`src/chanlun/core/zs_upgrade.py:202-267`）：

- L1 3buy：`leave.dir=="up"` 且 `retest.end.val >= z.zg`（zs_upgrade.py:262-264）——**retest 腿必须有终点**。摆动腿来自 `_swing_alternating_segs`（zs_upgrade.py:378-423，复用 `ZslxBranchCalculator._swing_segments`），右边缘最后一腿会随新数据 repaint，一条腿要等**下一条反向腿展开（即新的 L0 中枢摆动结构形成）**才被钉死——这就是 anchor（回试低点）→首次可见要 170-519 根 1m bar 的机制原因。
- L1 1buy：`enter.dir==leave.dir` 且 `is_beichi` 且 `is_qs`（zs_upgrade.py:250-260），anchor=leave.end——同样等腿完成。

而原文的介入语义（§1.5）恰好落在这个「腿进行中」的窗口里：离开段冲出已可见 → 3buy 候选成立（进行式）；回试腿进行中 → 在其**内部**找次级别一买介入（C2.16）。现状引擎在这个窗口里**什么都不产出**。

### 3.1 状态机与新事件类型

每级（先做 L1，L2 同构留后）只跟踪**最近一个中枢**的候选（与 v8「最近中枢单 3buy」语义对齐）：

```
(无)──离开腿完成且冲出──▶ CANDIDATE(3buy) ──回试腿内 L0 同向买点首次可见──▶ ENTRY_EMITTED
                              │                                            │
                              │ 回试可见低点 < ZG                            │ 回试可见低点 < ZG
                              ▼                                            ▼
                         INVALIDATED(静默作废)                    INVALIDATED → 发 nest_invalid(强制退出)
                              ▲
 CANDIDATE/ENTRY ──回试腿完成且终点≥ZG──▶ CONFIRMED(原生 L1 3buy 照常发出,候选闭合,不重复发 entry)
```

1buy 候选同构：`CANDIDATE(1buy)` = is_qs 趋势前提 + 离开腿（向下）创新低 + 背驰段假设未被证伪（C5.26「先假设进入背驰段」）；确认 = 背驰段内部 L0 1buy/底背驰首次可见（C5.38 递归）；失效 = 力度证伪（leave 完成后 `is_beichi` 为 False，或创新低且次级别确认链断，C5.41）。

新事件（均为 `engine.Signal`，复用现有 dataclass，不加新类）：

| 事件 | bs_type | level | anchor(date/price) | 语义 |
|---|---|---|---|---|
| 介入触发 | `"3buy_nest"` / `"1buy_nest"` | 候选的大级别（1+） | 次级别确认信号的 anchor | 买入，仓位走 buy_class（`"3buy_nest"[0]=="3"`，engine.py:75-79 兼容） |
| 候选失效 | `"nest_invalid_3buy"` / `"nest_invalid_1buy"` | 同上 | 失效确认 bar | 强制退出由该候选建立的仓位 |

不新增「候选可见」对外事件（候选只是引擎内部状态；要审计可在 CSV 加 `cand_born_time` 列）。`_signal_identity=(date, level, bs_type)`（live_backtest.py:646-651）下新 bs_type 与原生 L0/L1 信号身份天然不冲突、互不吞并（fresh/seen 机制按身份去重）。**注意**：`BUYS`/`SELLS` 常量（engine.py，`Signal.is_buy` 依赖之）必须把 `"3buy_nest"/"1buy_nest"` 加入 BUYS，`nest_invalid_*` 加入 SELLS（或单独 EXIT 集合），否则方向判定为 False。

### 3.2 要求 a：次级别确认同样 walk-forward 首次可见

确认信号取自**现有 branch 流**（`collect_branch_signals`，engine.py:333），它本身就在同一个 wf 扫描里逐 bar 重算、经 fresh 机制获得首次可见语义（live_backtest.py:1004-1018）。`nest_entry` 事件 = 「候选 active」AND「branch L0 买点身份首次出现」的合取，在**同一根 main bar** 上成立——两个条件都只用当根 bar 收盘时刻的可见状态，无任何未来引用。候选状态本身亦然：离开腿完成/冲出 ZG、回试低点跌破 ZG，都是当根可见的结构事实；右边缘 repaint 导致候选消失再重现时，已发出的 entry 身份在 seen_keys 里不重发（与现状 live_backtest.py:803-810 docstring 行为一致）。

过滤条件（结构归属 + 价格域，对齐 §1.3 的「结构嵌套」而非时间近似）：

- **时间域**：确认信号 anchor_date > 候选离开腿终点 date（即落在回试腿/背驰段内部，而非候选生成前的旧信号）；
- **方向域**：与候选同向（买候选只配买点确认）；
- **价格域**（3buy 候选）：确认信号价格 ≥ cand.zg——保证「若回试就此结束，几何条件 `retest.end >= ZG` 仍可成立」；回试可见低点一旦 < ZG，候选即失效，不存在「破 ZG 后又确认」的路径；
- **结构域**（1buy 候选）：确认必须是 L0 一类买点或 L0 摆动腿底背驰（`get_kuozhan_levels` 的 L0 `bcs` 或 branch 流 1buy），即「背驰段的背驰段」（C5.38），不接受任意 L0 3buy（防把趋势中继误当转折确认；C4.27 反向不成立）。

### 3.3 要求 b：signal_source 与 v8 缓存兼容（不 bump v9 的路径）

- 新增 `signal_source="nest_cascade"`。`_signal_cache_meta` 仅当 `signal_source != "branch"` 时把 source 写入 meta（live_backtest.py:274-275），`upgrade` 与 `nest_cascade` 的 meta 不同 → **缓存键天然隔离**，现有 v8 条目（branch/upgrade）一个字节都不动，**无需 bump v9**。
- `Signal` 不加新必填字段：介入用 `structural_stop_below=cand.zg`（既有字段，语义恰好是「3买失效下边界=ZG」，bs_branch.py:26 同口径）、`zs_zd/zs_zg` 携带候选中枢。旧 pickle 反序列化不受影响（消费端普遍 `getattr(sig, x, default)`）。
- `_collection_state_signature`（live_backtest.py:971 调用处）需为 `nest_cascade` 提供签名：保守起见返回 None（每 bar 必 collect，正确性优先），优化版=upgrade 签名 ⊕ 候选状态指纹（最后腿端点 k_index、候选 kind/zg）。签名只影响性能不影响语义。
- **必须 bump v9 的边界**：若把候选/确认事件**混入现有 upgrade 源输出**（改 `engine.collect_signals` 返回），旧缓存条目语义被改变（同 meta 不同结果）→ 必须 bump。本设计明确不走这条路。同理，改 `collect_branch_signals` 默认行为也触发 bump；保持新源独立即可规避。
- 与 `annotate_nest`（READ 标注）正交：旧 nest/nest_soft 的 filter/soft 行为原样保留（回归约束见 3.7），`nest_cascade` 是新的介入通道而非对旧标注的改造。

### 3.4 要求 c：失效路径与 NVDA 案例预期行为

**失效路径（与原文一一对应）**：

| 路径 | 触发 | 动作 | 原文依据 |
|---|---|---|---|
| 3buy 候选破 ZG | 回试可见低点 < cand.zg | 未入场→静默作废；已入场→发 `nest_invalid_3buy`，t+1 开盘全退 | 3买几何条件不可再满足（C4.9）；盘整背驰转三卖先验（C5.23） |
| 1buy 候选力度证伪 | leave 腿完成且 `is_beichi`=False（力度反超） | 同上 | C5.41「一旦力度大于前者，断定背驰段不成立」 |
| 1buy 链断新低 | 入场后创新低且无新的 L0 底背驰承接 | 发 `nest_invalid_1buy` 退出 | 区间套链断=最内层背驰被否定（C5.26） |
| 候选自然闭合 | 回试腿完成且 ≥ZG → 原生 L1 3buy 发出 | 已入场者持有（仓位归并到 L1 信号管理）；未入场者按原生信号走现行通道 | 候选→确认的正常路径 |
| 候选过期 | 同级出现新中枢（最近中枢易主） | 旧候选作废（未入场）；已入场转由常规退出管理 | v8「最近中枢」语义对齐 |

**NVDA.US 2026-06-02 案例预期行为**（候选中枢 zd=195.795/zg=201.488，CSV 实测字段）：

- 现状：L1 3buy visible=06-02 15:46 → fill 15:47 @**225.599**；止损边界 201.488（距入场 **-10.7%**）；MFE +0.06%；06-09 16:11 结构失效退出 201.68，**-10.60%**。
- 闭环后：离开腿冲出 201.488 完成时（5-29 之前）候选已生成；回试腿 5-29 19:59 见低 210.877。介入触发=回试期间首个满足价格域（≥201.488）的 L0 买点/底背驰首次可见——按回试低点区域估计 fill ≈ **211-216**（精确值需把重放窗口提前到 5-26 验证；当前 CSV 从 6-01 起截断，窗口前 L0 事件不可见）。
- 改善结构：①入场价 225.599→≈211-216，同一失效边界 201.488 下止损距离 10.7%→**4.7%-6.7%**；②06-02 盘中冲高 ≈225.9，MFE 从 +0.06% → 约 **+4.6%-7.0%**，配合 L0 级卖点级联离场有保本/小赚的现实路径；③若仍持有到失效，退出由「跌破 ZG=201.488 当根可见」触发（nest_invalid），与现版 06-09 结构失效退出 201.68 时点接近，亏损从 -10.60% 收窄到约 **-4.8%~-6.8%**（近乎减半）。
- 附带修复：06-03/06-04/06-05 反复出现的后续 L1 3buy（fill 216.19/219.72/208.485，皆「可见即成交」）在闭环下同样要求各自候选的次级别确认+价格域，不再以确认时点价格盲目成交。
- **诚实声明**：以上为结构推演+CSV 字段佐证，非重放实测；候选首次可见时刻、L0 确认的具体时点须按 §4 预案与 §3.7 测试在提前窗口下重放钉死。

### 3.5 伪代码

```python
# ---------- src/chanlun/core/zs_upgrade.py ----------
@dataclass
class NestCandidate:
    kind: str              # "3buy" | "1buy"（卖向对称留后）
    level: int             # 大级别（1+）
    zs: ZS                 # 锚定中枢（3buy=被离开中枢; 1buy=趋势最后中枢 B）
    leave_end_date: Any    # 离开/背驰段终点（时间域下界）
    zg: float
    zd: float
    invalid_below: float   # 3buy: zs.zg; 1buy: 动态=已见最低点（链断判定配合）

def kuozhan_level_candidates(zss, lower_zss, ld, wzgx, frequency) -> List[NestCandidate]:
    """右边缘候选。只看最后一个中枢 z=zss[-1]（v8 最近中枢语义）。
    复用 kuozhan_level_signals_ex 的段定位（segs=_swing_alternating_segs(lower_zss)，
    enter/leave/retest 同 zs_upgrade.py:240-249）：
      3buy 候选: leave 存在且 dir=="up" 且 leave 端点已冲出 z.zg
                 且 (retest 不存在 or retest 为右边缘最后一腿)   # 腿未被下一腿钉死
                 且 (retest 当前可见低点 >= z.zg)               # 价格域未失效
      1buy 候选: k>0 且 is_qs(zss[k-1], z, wzgx)               # 趋势前提（C5.1/C5.2）
                 且 leave.dir=="down" 且 leave 可见低点 < enter 低点  # 创新低（C5.6）
                 且 未被「leave 完成后 is_beichi=False」证伪          # C5.41
    """

# ---------- src/chanlun/core/cl.py ----------
def get_kuozhan_candidates(self):
    """lazy; 与 get_kuozhan_levels（cl.py:552-608）同源数据、同逐级容错风格。"""

# ---------- src/chanlun/recursive_bt/engine.py ----------
NEST_BUYS = {"3buy_nest", "1buy_nest"}                    # 并入 BUYS
NEST_EXITS = {"nest_invalid_3buy", "nest_invalid_1buy"}   # 并入 SELLS/EXIT 集合

def collect_nest_cascade_signals(cd: CL) -> List[Signal]:
    out = []
    cands = cd.get_kuozhan_candidates()
    if not cands:
        return out
    sub = collect_branch_signals(cd, use_xd=True, annotate_nest=False)  # L0 确认池
    l0_bcs = L0 摆动腿背驰事件  # get_kuozhan_levels() L0 bcs，1buy 确认补充
    for cand in cands:
        if cand.kind == "3buy":
            if 回试可见低点 < cand.zg:                       # 失效（含已入场情形）
                out.append(Signal(当根date, cand.level, "nest_invalid_3buy", cand.zg))
                continue
            for sig in sub:                                  # 时间域+方向域+价格域
                if (sig.is_buy and sig.date > cand.leave_end_date
                        and sig.price >= cand.zg):
                    out.append(Signal(sig.date, cand.level, "3buy_nest", sig.price,
                                      structural_stop_below=cand.zg,
                                      zs_zd=cand.zd, zs_zg=cand.zg))
                    break                                    # 每候选只触发一次
        elif cand.kind == "1buy":
            if 候选被力度证伪 or (创新低 and 无新L0底背驰):
                out.append(Signal(当根date, cand.level, "nest_invalid_1buy", 当前价))
                continue
            for sig in (sub 中的 L0 1buy) + 同向 l0_bcs:      # 结构域：背驰的背驰
                if sig.date > cand.leave_start_date:
                    out.append(Signal(sig.date, cand.level, "1buy_nest", sig.price,
                                      structural_stop_below=cand.invalid_below))
                    break
    return out
    # 注：每 bar 全量重算、函数内无状态；「每候选只发一次 entry/invalid」由 wf fresh
    # 机制（_signal_identity 含 date → 身份固定）天然保证。

# ---------- src/chanlun/recursive_bt/live_backtest.py ----------
# _collect_visible_signals（741-762）增分支:
#   elif signal_source == "nest_cascade":
#       signals = collect_upgrade_signals(cd) + collect_nest_cascade_signals(cd)
#       （保留原生 L1 流：CONFIRMED 闭合路径与门控方向事件不缺位）
# _collection_state_signature: nest_cascade → None（每 bar collect，先对后快）
# argparse/signal_source 校验（820-822）增枚举 nest_cascade
# 缓存: meta["signal_source"]="nest_cascade" 自动隔离（274-275 已有机制，不改）

# ---------- src/chanlun/recursive_bt/portfolio.py ----------
# 买入: buy_class("3buy_nest")==3 自动走现有 ratio 通道（engine.py:75-79）
# 退出: bs_type in NEST_EXITS → 该标的由 *_nest 建立的仓位强制全退（优先级=结构失效）
# 归并: 持仓记 entry_source；其后原生 L1 3buy（CONFIRMED）对已持 nest 仓位仅刷新
#       管理基准、不重复加仓
```

### 3.6 涉及函数清单（实现盘点）

| 文件 | 函数/对象 | 改动 |
|---|---|---|
| `src/chanlun/core/zs_upgrade.py` | `NestCandidate`（新）、`kuozhan_level_candidates`（新） | 右边缘候选判定；复用 `_swing_alternating_segs`/`is_beichi`/`is_qs` |
| `src/chanlun/core/cl.py` | `get_kuozhan_candidates`（新 getter，lazy） | 与 `get_kuozhan_levels`（cl.py:552-608）同源、同容错风格 |
| `src/chanlun/recursive_bt/engine.py` | `BUYS`/`SELLS` 常量、`collect_nest_cascade_signals`（新） | 新 bs_type 注册；候选×确认合取 |
| `src/chanlun/recursive_bt/live_backtest.py` | `_collect_visible_signals`（741-762）、source 校验（820-822）、`_collection_state_signature`（971 调用处）、argparse | 新源分支 |
| `src/chanlun/recursive_bt/portfolio.py` | 买点收集/退出处理（686-698、1251-1263 邻域） | NEST_EXITS 强退；entry_source 归并 |
| `src/chanlun/recursive_bt/live_monitor.py` | `collect_monitor_events`（472 起） | 转发 *_nest 事件与失效告警（钉钉链路复用） |
| 不动 | `beichi_nest.py`/`interval_nest.py`/旧 nest_mode 全链 | READ 标注与 filter/soft 原样保留 |

### 3.7 测试钉法

1. **单元（受控构造，tests/core/test_zs_upgrade_candidates.py 新建）**：构造 L0 中枢摆动序列——
   - 离开腿冲出+回试进行中 → 候选存在；回试完成且 ≥ZG → 候选消失、原生 3buy 出现（闭合不双发）；
   - 回试可见低点 < ZG → 候选消失且同一中枢不再生；
   - is_qs 趋势+创新低 → 1buy 候选；leave 完成后力度反超 → 候选消失（C5.41 证伪负例）；
   - 非趋势（单中枢）创新低 → **无** 1buy 候选（C5.1/C5.2 负例）。
2. **wf 无未来钉死（tests/recursive_bt/test_nest_cascade_wf.py，真实 fixture 必须 parquet 不能 csv——包含处理对 ~4e-16 噪声敏感）**：仿 `tsla_cascade_confirmation_audit.md` 快照法——对每个 `*_nest`/`nest_invalid_*` 事件断言：visible 前一 bar 的 CL 快照中该身份不存在、visible bar 存在；`3buy_nest.price >= zs_zg`；`3buy_nest.visible_bar <= 对应原生 L1 3buy.visible_bar`（提前性，允许相等）。
3. **缓存回归**：同参数二跑 `nest_cascade` 结果 byte-identical；旧参数组合（branch/upgrade，含 require nest/nest_soft）的 meta hash、信号 CSV 与改动前逐字节一致（v8 条目零扰动）。
4. **NVDA 验收 fixture**：窗口提前至 2026-05-26 重放，断言存在 `3buy_nest` 且 fill < 225.599、其后 `nest_invalid_3buy` 退出价位于 201.488 邻域（或被 L0 卖点级联更早带走）；TSLA 窗口断言 06-08 anchor 的 L1 3buy 产生提前介入或诚实记 no_subconfirm（无确认也是合法结果，必须显式记录）。

---

## 四、量化预案：用 signals CSV 离线估计提前量与价格改善

### 4.1 估计器 A：现有 CSV 事后配对（本轮已试算）

数据：`D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_{signals}.csv`（98 事件，stream 全 upgrade，L1 买点 11 个、L0 买点 21 个）。

**计算步骤（已执行的可复现伪代码）**：

```python
df = read_csv(signals_csv); BUY = {"1buy","2buy","3buy"}
l1 = df[level>=1 & bs_type∈BUY]; l0 = df[level==0 & bs_type∈BUY]
for r in l1:                       # 每个大级别买点事件
    pool = l0[code==r.code
              & visible_time ∈ [r.anchor_time, r.visible_time]   # 候选窗口近似
              & next_fill_open >= r.zs_zg]                        # 3buy 价格域
    m = pool.earliest(visible_time)                               # 区间套介入代理
    lead_bars     = r.visible_bar - m.visible_bar                 # 提前量(1m bar)
    price_improve = (r.next_fill_open - m.next_fill_open) / r.next_fill_open
    # 变体 2: pool 再限 bs_type∈{"1buy","2buy"}(回试结束类,C2.16 口径)
汇总: matched 率 / lead 中位·最大 / improve 中位·均值; 逐事件明细表
```

**试算结果（2026-06-13 实跑）**：

| 变体 | 匹配 | lead 中位 | lead 最大 | 改善中位 | 改善均值 |
|---|---|---|---|---|---|
| 任意 L0 买点确认 | 8/11 | 0 bar | 262 bar | 0.0% | **-1.97%** |
| 仅 L0 1/2buy（回试结束类） | 1/11 | 0 bar | 0 bar | 0.0% | 0.0% |

逐事件要点：NVDA 06-04 19:06 anchor 的 L1 3buy（lead=262）与 TSLA 06-08（lead=232）价格改善为**负**（-5.39%/-4.95%）；NVDA 主案例（-10.60% 那笔）lead=0（窗口截断，见 4.2）；QQQ 06-09 无匹配。

**三条直接教训（修正第三节设计的输入）**：

1. **「提前」≠「价格改善」**：3buy 的回试是向下走的，候选窗口早期的 L0 买点价格更高——TSLA/NVDA 两例提前 200+ bar 反而贵 5%。价格改善的真实来源不是「早」，而是**确认事件落在回试低点附近**（=回试结束信号），印证 C2.16「精确买点=次级别第一类买点」的精确口径不可放宽为「任意同向买点」。
2. **确认池粒度不足**：upgrade 流的 L0 1buy 要求 L0 级 is_qs 趋势背驰，全 CSV 仅 2 个——严格口径在该信号池里几乎取不到确认。第三节的确认池必须用 **branch 流笔级买点**（`get_branch_bspoints(use_xd=False)`，笔粒度）或 L0 摆动腿背驰 `bcs`，本 CSV（upgrade 流）没有这两类事件，故估计器 A 系统性低估匹配率。
3. **窗口截断**：v8 registry CSV 不含窗口前已可见信号（NVDA 主案例 anchor=05-29 在 06-01 窗口外，anchor_bar 为空），candidate 生成至窗口起点间的 L0 事件全部不可见 → lead/improve 都是**下界**。

### 4.2 估计器 A 的方法论边界（必须随结果声明）

- 窗口 `[anchor_time, visible_time]` 是**事后**配对（anchor 在当时不可知）；它度量的是「若候选机制存在，确认事件可落位的时间带」，不是可交易策略本身。真实提前量上界还要更大：候选自离开腿完成即生成，早于 anchor（=回试低点）。
- `zs_zg` 取自 L1 信号行，该中枢在候选生成时已 done，字段本身无未来性；但配对窗口有。
- 结论只能用于**排序与方向判断**（哪类确认池/约束更有希望），绝对数字不可外推（§22 起绝对收益皆乐观上界的项目纪律同样适用）。

### 4.3 估计器 B：提前窗口 + 笔级确认池重放（实现后的正式度量）

```python
# 1) 数据窗提前: start = min(L1.anchor_time) - 10 交易日(覆盖候选生成段)
# 2) 跑两条 wf 流(同一 main_dates):
#    flow_U = _walk_forward_signals_by_main_bar(source="upgrade")       # 大级别基准
#    flow_N = _walk_forward_signals_by_main_bar(source="nest_cascade")  # 新源(§3)
# 3) 配对: 每个 flow_U 的 L1 买点事件 e ↔ flow_N 中同 code、同候选中枢
#    (zs_zd/zs_zg 相同)的 3buy_nest/1buy_nest 事件 n
# 4) 指标:
#    lead_bars      = e.visible_bar - n.visible_bar          # ≥0 断言(测试钉法 2)
#    price_improve  = (e.next_fill_open - n.next_fill_open) / e.next_fill_open
#    risk_distance  = (n.next_fill_open - zs_zg) / n.next_fill_open    # vs e 同式
#    no_subconfirm  = |无 n 的 e| / |e|                       # 诚实空集率
#    false_positive = |n 后无对应 e 且以 nest_invalid 退出| / |n|       # 候选假阳性
#    fp_cost        = 假阳性单笔损益分布(入场→invalid 退出)
# 5) 汇总: 分 bs_type×code 中位/分位; NVDA/TSLA 案例单列;
#    净效应 = Σ(改善×匹配) + Σ(假阳性成本) 对比基准组合收益(portfolio 全跑)
```

判定标准建议：`price_improve 中位 > 0` 且 `false_positive 成本不吞掉改善`（净效应为正）才进入 combo review 候选流程（不自动采纳，与 combo_b140 同纪律）。

---

## 五、风险与开放问题

### 5.1 设计风险

1. **假阳性成本未知且方向不利**（4.1 教训 1 的延伸）：候选期介入承担「L1 最终不确认」的新风险敞口；3buy 候选破 ZG 失效的单笔损失 ≈ 介入价距 ZG（设计上 4-7%），频率未知。NVDA 案例闭环后也只是**减亏近半**（-10.6%→约 -5~-7%），不是免亏——区间套修复的是风险收益比的分母，不保证单笔为正。
2. **确认池选择是成败关键**：试算证明「任意 L0 买点」确认产生负改善、「严格 L0 1buy」几乎无匹配。设计落点必须是**笔级**（branch 流 use_xd=False）的一买/二买/底背驰，并考虑对 3buy 候选加确认价上界（如 `确认价 ≤ zg + α×(回试已见低点-zg)`，α 待定）抑制「回试后期反弹追价」——此上界无原文行号依据，属工程参数，须按 gap C 纪律标注【工程近似】并配回退开关。
3. **「破 ZG 即失效」的口径敏感性**：原文 3 买条件是回试段**终点**不破 ZG（C4.9），盘中瞬时下影破 ZG≠回试终点破。用「任意可见低点<ZG」失效会被插针洗出；用「笔端点/收盘」又引入确认延迟。需与 `zs_wzgx`（GD，含 GG/DD 瞬间波动的定理二口径）统一裁决，建议先收盘价口径+fixture 钉死两个反例（插针不洗/实破必走）。
4. **多候选并发**：同一 anchor 同时产生 L1 3buy+1buy（CSV 实测成对出现）→ 两候选同时 active，需去重（同中枢同向只发一个 entry，优先 3buy 几何口径）否则双倍仓位。
5. **性能**：`nest_cascade` 每 bar 全量 `collect_branch_signals`+候选重算，wf 扫描成本约翻倍；签名返回 None（每 bar 必 collect）加剧。长窗口预热必须沿用自愈循环（prewarm .ps1 模式），>10min 回测须 Start-Process 并先杀罗技代理（项目既有纪律）。
6. **门控交互**：nest_entry 是否受 30m/mid 门控约束？第69轮已证 30m 门控对程式类信号净伤害（误杀左侧低吸），而 nest_entry 正是左侧介入——建议 nest_entry 默认绕过 mid 门控、保留 big_dir!=down 红线，作为显式参数留给回测裁决。
7. **A 股口径**：分钟级新策略必须先过涨停锁死口径验证（第63轮教训：combo 增益九成是涨停虚增）；nest_entry 的 t+1 开盘撮合在涨停/ST±5% 下的可成交性需复用现有锁死逻辑。

### 5.2 开放问题

1. **候选生成时点未实证**：候选「离开腿完成且冲出」的首次可见时刻分布（相对 anchor 提前多少）没有任何实测数据——估计器 B 跑通前，§3.4 的 NVDA 改善区间只是结构推演。
2. **1buy 候选的力度预检要不要做**：C5.13「面积乘 2 法」有原文行号（行14407）可支撑候选期的背驰预判，但 macd_ld 的 htf 近似已被审计标注（gap C）——1buy 候选是否引入面积预检、用原生还是 htf 口径，留待实现期决断。
3. **L2（30m tongjibie）同构扩展**：30m 级确认滞后更甚（数量级类推），但 tongjibie 交替段语义（v34/v35 修复件）与 kuozhan 摆动腿不同，候选判定需另写 `tongjibie_level_candidates`；先 L1 验证净效应再扩。
4. **卖向对称**：`3sell_nest/1sell_nest`（持仓者的提前离场）对 us core9/A股 组合的影响可能大于买向（现版全部 L1 3sell 滞后 170-519 bar 意味着离场同样慢）；但卖向失效成本不对称（踏空 vs 亏损），需独立评估。
5. **no_subconfirm 的 fallback**：无次级别确认时，是放弃该 L1 信号还是退回现行「可见即成交」通道？C4.28（3买介入最晚最确定）支持保留原生通道做 fallback；但这会稀释闭环的统计可读性——建议回测分 `nest_only`/`nest_or_native` 两档对照。
6. **与 READ 标注的最终归并**：闭环跑通后，`annotate_nest`/`operable`（时间近似版）是废弃、还是升级为「结构归属+创新高校验」版（修 beichi_nest 的挂载条件）再服务于 filter/soft？建议待 nest_cascade 净效应落地后统一裁决，避免两套嵌套口径并存。

### 5.3 推进顺序建议

1. 实现 §3.5 最小闭环（仅 L1 3buy 候选 + 笔级一/二买确认 + 破 ZG 失效），过 §3.7 测试 1-3；
2. 跑估计器 B（NVDA/TSLA/QQQ 提前窗口），用 §4.3 指标裁决确认池与价格上界参数；
3. NVDA 验收 fixture（§3.7-4）钉死后，再扩 1buy 候选与卖向；
4. 全程不动 v8 既有缓存与 nest_mode 行为（回归约束），结果只进 review 候选、不自动采纳。

---

## 附：本研究引用的关键文件索引

| 类别 | 路径 |
|---|---|
| 原文判据 | `docs/yuanwen_study/topic5_beichi_qujiantao.md`（C5.x）、`topic4_maimaidian.md`（C4.x）、`topic2_yanshen_kuozhan_kuozhang_3buy.md`（C2.16） |
| P6 设计 | `docs/chanlun_core_redesign_6_区间套_design.md` |
| 审计 | spec §75 审计表 gap D（`docs/chanlun_realtime_trading_system_spec.md:4514`）、`D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md`、`nvda_core3_v8_trade_invalidation_audit.md` |
| 代码 | `src/chanlun/core/{cl,zs_upgrade,beichi_nest,interval_nest,bs_branch}.py`、`src/chanlun/recursive_bt/{engine,live_backtest,portfolio,live_monitor}.py` |
| 数据 | `D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_{trades,signals}.csv` |

（完）
