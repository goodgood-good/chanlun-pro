# 缠论 V3 实盘交易体系：审计、实现、测试、数据验收与因果回测最终报告

生成时间：2026-07-26（Asia/Shanghai）
唯一交易规格：`audit/chanlun_live_strategy/complete_strategy_v3.md`（未修改）
原文证据库：`audit/chanlun_lesson_corpus`（未修改）

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 结构核心修改 | **零**（48 文件哈希前后一致，`git status src/chanlun/core/` 为空） |
| 数据等级 | **`COMPONENT_ONLY`** |
| 回测标签 | **组件回测**，非完整交易体系回测 |
| 严格合法入场链 | **0**（11 只 ETF / 8,699 标的-交易日） |
| 收益可评价 | **否**（`performance_evaluable=false`，`empty_replay=true`） |
| 战略周期 / 短差周期 | 0 / 0（样本不足，远低于 100 / 200 门槛） |
| 最终状态 | **`RESEARCH_ONLY / LIVE_DISABLED`** |

**核心发现**：入场链 0 命中既不是对齐实现缺陷，也不是样本选择问题，而是冻结结构核心的买卖点供给与规格 §8.2 入场链联合要求之间的结构性缺口。详见第 15 节。

---

## 1. 修改文件清单

| 文件 | 改动 |
|---|---|
| `src/chanlun/decision_support/trading_system/v3_multisymbol_replay.py` | R-05：`ReplayMetrics` 新增 `ledger_valid` / `performance_evaluable` / `empty_replay`，新增 `EMPTY_REPLAY_RETURNS_NOT_EVALUABLE`、`PERFORMANCE_NOT_EVALUABLE` 告警 |
| `src/chanlun/decision_support/trading_system/v3_replay_payload_builder.py` | R-04：新增 `_bars_after`；持久战略退出的柱供给由「当日剩余」改为「跨 session 到数据尽头」 |
| `tools/backtest_chanlun_v3_multisymbol_events.py` | R-06：新增 `_validate_builder_contract` 入口强校验；报告回写 `builder_contract` |
| `tests/trading_system/test_v3_multisymbol_replay.py` | 新增 R-05、R-06 两个测试及 `frozen_builder_contract` 脚手架 |
| `tests/trading_system/test_v3_replay_payload_builder.py` | 新增 R-04 测试 `test_persistent_exit_keeps_working_across_later_sessions` |
| `src/chanlun/decision_support/trading_system/backtest/pit_metadata.py` | 删除未使用 import（既存 lint 缺陷） |
| `src/chanlun/decision_support/trading_system/v31_snapshot.py` | 删除未使用 import（既存 lint 缺陷） |

新增审计产物：`v3_final_traceability_matrix.md`、`v3_final_report.md`、`frozen_core_hashes_before.json`、`frozen_core_hashes_after.json`、`final_v3_replay_payload_build.json`、`final_v3_replay_payload.json`、`final_v3_multisymbol_replay.json`。

**未触碰**：`src/chanlun/core/**` 全部 48 个结构核心文件；唯一交易规格；原文证据库。

## 2. 冻结结构文件及前后哈希

清单与逐文件哈希：`audit/chanlun_live_integration/frozen_core_hashes_before.json` / `frozen_core_hashes_after.json`。

```
frozen_core_files = 48
modified          = []
frozen_core_modified = false
git status src/chanlun/core/ = (空)
```

覆盖 `cl_kline_process`（包含处理）、`bi_calculator`（分型/`ORIGINAL_OLD_PEN`）、`xd_calculator`（线段）、`zs_calculator` / `strict_structure/center_machine`（中枢）、`strict_structure/**`（递归引擎、买卖点、背驰）全部。

代表性结构输出一致性：`tests/core/` 黄金回归 **303 passed**，含结构指纹、增量==全量对拍、前缀因果性。

## 3. 规则到代码和测试的最终追踪矩阵

见 `audit/chanlun_live_integration/v3_final_traceability_matrix.md`，覆盖用户要求的全部 14 个领域，共 A1–A17 / B1–B11 / C1–C19 / D1–D8 计 55 条。

汇总：`EXACT` 40、`PARTIAL` 11、`CONFLICT` 1（B4，见第 15 节）、`MISSING(数据)` 3（个股三程序研究快照、逐笔成交与历史报价）。

## 4. 全部测试命令和结果

```text
python -m pytest -q tests/trading_system/            → 370 passed
python -m pytest -q tests/core/                      → 303 passed
python -m pytest -q tests/trading_system/ tests/core/ → 673 passed in 31.71s
python -m ruff check src/chanlun/decision_support/trading_system/ \
    tools/backtest_chanlun_v3_multisymbol_events.py \
    tools/build_chanlun_v3_replay_payload.py tests/trading_system/
                                                     → All checks passed!
```

注：`tools/` 目录另有 3 处既存 lint 缺陷（`audit_qmt_prefix_invariance.py`、
`backtest_chanlun_v3_independent_timeframes.py`、`snapshot_qmt_pit_metadata.py`），
不在本次改动面内，未擅自改动他人文件。

R-04 的 TDD 反证（把 `_bars_after` 临时改回 `_session_bars_after`）：

```text
FAILED tests/trading_system/test_v3_replay_payload_builder.py::
       test_persistent_exit_keeps_working_across_later_sessions   （修复前）
1 passed                                                          （修复后）
```

本次新增测试覆盖：正常案例、等号边界、缺失数据、拒绝案例、T+1、部分成交、费用与最低佣金、涨跌停/停牌、公司行为、账户与订单重启，均已在既有 370 个用例中分布（详见矩阵 C 段）。

## 5. 数据来源、时间范围、覆盖率及数据等级

| 来源 | 内容 | 范围 |
|---|---|---|
| `financial_data_query_bars.sqlite3` | 510300/510050 不复权 1m、000300.CSI 日线 | 2018-02-02 ~ 2026-07-24 |
| QMT 本地分钟库 | ETF 与个股 1m/5m | 各标的区间不一 |
| `etf_proxy_pit.sqlite3` | 点时成分、主数据、交易状态/ST、复权因子、分红 | 2017-01-03 ~ 2026-07-24 |
| `qmt_csi300_etf_corporate_actions_v1.json` | ETF 有效日公司行为 | 逐标的 |

回测样本（11 只场内 ETF，取各标的本地数据支持的最长完整无未来区间）：

| 标的 | 交易日 | 标的 | 交易日 |
|---|---:|---|---:|
| 510300.SH | 1,044 | 510360.SH | 698 |
| 510310.SH | 1,044 | 159925.SZ | 578 |
| 510330.SH | 1,044 | 510390.SH | 578 |
| 510500.SH | 1,044 | 510380.SH | 574 |
| 159919.SZ | 977 | 512100.SH | 241 |
| 510050.SH | 877 | **合计** | **8,699** |

5m/30m 全部由同一权威 1m 流按交易所时段边界聚合；每个通过验收的交易日输出 240 根已完成分钟线。

**数据等级：`COMPONENT_ONLY`**（`external_data_acceptance.json`，`sha256:0dd18a43…`）。八项裁决：同源 PASS、只用完成柱 PASS、点时复权 PASS、点时成分 PARTIAL、停牌/ST/上市退市 PASS、基本面比价 NOT_APPLICABLE（ETF 路径）、逐笔成交与历史报价 **WAIVED_FOR_RESEARCH_BY_USER**、T+1/费用/数量增量 PARTIAL。

阻断原因：`BLOCKED_BY_FROZEN_STRUCTURE`、`STRICT_V3_CANDIDATE_PIT_SNAPSHOT_SET_UNAVAILABLE`、`BROKER_VINTAGE_EXECUTION_RULES_UNAVAILABLE`、`HIGH_TIMEFRAME_FACT_ADAPTER_NOT_CERTIFIED`。

## 6. 参数快照和哈希

```text
strategy_parameter_set_id           sha256:b16882b09253581c2adbdc5ded720be3a0c75fa51e40acd8109dc7f1de6c3f0d
execution_parameter_set_id          sha256:105895c7a899cf3fa11b7a22dbbf77aaba486767fb84c17aca303d357bd16e5f
alignment_parameter_set_id          sha256:3c4cb0f976e4343fa9fe10341d104500ab58fc4695ca845a3f0bd8bba54f81f5
alignment_contract_id               V31_L0_COMPLETION_EVIDENCE_L1_L2_CAUSAL_V2
timeframe_override_parameter_set_id sha256:9e3edcce078436460844c84d5e89fcc0f03fb6f1c96ae84cf08d07bbe979f317
live_status                         LIVE_DISABLED
```

`INDIVIDUAL_THREE_PROGRAM` 与 `ETF_PROXY` 是同一参数快照中互斥的激活路径。本次**只激活 `ETF_PROXY`**；个股路径因无点时、带正式披露 ID 与签名的行业机会/基本面/比价研究快照而关闭，未运行、未混用。参数在回测前冻结，未按测试区间结果调整。

## 7. 代码版本与产物哈希

```text
git HEAD                              527f6c8c982accaf75aedf8ed72369aceb3e1538（未提交、未推送）
final_v3_replay_payload_build.json    sha256:ec5080cf7f83deaf8d2f2665aa232d0b8d145e83983a389b70e2099085e79fc6
final_v3_replay_payload.json          sha256:d5333c6056984a16e597fd15160a59a04adba804dbec05b585a0d17c6314f0fa
final_v3_multisymbol_replay.json      sha256:c38a648b469c816b462e68ae23384aacaa7b4ebe1784fe3d61f5977a975e7174
external_data_acceptance.json         sha256:0dd18a43fab2c20430f846ac7077f326cd58ee0f08639010f540913ef2f0f05a
recursive_structure_availability.json sha256:9290b7793eb5a3dc806e0ac8246d2122cf727a937e526021d756ebc209b22744
```

## 8. 回测标签

**组件回测（`COMPONENT_ONLY`）。不是完整交易体系回测。**

理由：入场链 0 命中导致组合调度、五槽位、短差层、T+1 批次账本、公司行为缩放在真实数据上**从未被触发**；这些模块只在单元测试的合成事实上验证过。逐笔成交与历史报价缺失，撮合以已完成 1 分钟柱代理并已登记用户豁免——该豁免不能把结果提升到激活验收等级。

## 9–11. 绩效指标

```text
performance_evaluable      = false
empty_replay               = true
ledger_valid               = true      （仅表示账本恒等式自洽）
net_return                 = 0         ← 空回放恒等式产物，非策略收益
max_drawdown               = 0         ← 空回放恒等式产物，非策略回撤
order_count / fill_count   = 0 / 0
strategic_cycle_count      = 0         （门槛 100，样本严重不足）
tactical_cycle_count       = 0         （门槛 200，样本严重不足）
warnings = [INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION,
            STRATEGIC_SAMPLE_BELOW_100, TACTICAL_SAMPLE_BELOW_200,
            EMPTY_REPLAY_RETURNS_NOT_EVALUABLE]
```

年化收益、夏普、利润因子、胜率、盈亏比、换手率、成本、容量：**均不可计算**（无成交）。

**明确声明：上述 `0` 不是 V3 策略的收益与回撤结论。** 本次已实现 R-05，使 replay 产物自带 `performance_evaluable=false` 与 `empty_replay=true`，`metrics.valid=true` 不再可能被误读为绩效有效。

## 12–13. 分年度/行情阶段、基准与消融对比

**不产出。** 前置门（收益可评价）失败时不得评价后续门的收益。基准对比、无短差版本与关键模块消融在有非零成交样本前无意义，强行输出等同于把 0 包装成结论。

## 14. 拒单、未成交、规则违规与异常

```text
订单 0、成交 0、拒单 0、规则违规 0、undefined_state_transitions 0
core_intrusions 0、Q_CYCLE_breaches 0、duplicate_orders 0
```

入场链逐条拒绝原因（55 个 L0 候选）：

| 拒绝原因 | 次数 |
|---|---:|
| `NO_COMPLETED_L1_UP_DEPARTURE_ALIGNED_WITH_L0_LEAVE_UNIT` | 30 |
| `FIRST_COMPLETED_L1_DOWN_RETURN_NOT_ALIGNED_WITH_L0_RETURN_UNIT` | 12 |
| `NO_SUBSEQUENT_COMPLETED_L1_DOWN_RETURN` | 10 |
| `NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL` | 2 |
| `FIRST_L1_RETURN_LOW_BELOW_L0_ZG` | 1 |

## 15. 未来函数、幸存者偏差与实盘/回测差异

| 检查 | 裁决 | 证据 |
|---|---|---|
| 未来函数 | PASS | 逐前缀冻结线段；`ReplayBatch` 拒绝估值时点之后的事实；`available_at` 以 checkpoint 取下界 |
| 信号柱成交 | PASS | `v3_bar_execution.py` 跳过 `closed_at <= signal_bar_end` |
| 碰价即成交 | PASS | 买入仅接受整根柱 `high < limit`，卖出仅 `low > limit`；等价触碰记零成交 |
| 前缀因果性 | PASS | `test_prefix_causality.py`；追加未来 K 线不改变历史信号 |
| 实盘/回测一致性 | PASS | `decide_live` 与 `decide_backtest` 同调 `V3DecisionCore`，`test_v3_decision_parity.py` |
| 幸存者偏差 | PARTIAL | ETF 池按法律名称与上市日选取、不做当前交易状态过滤；但 QMT 历史板块成分接口对 2019/2022/2026 返回同一当前集合，个股池无法点时构造 |

### 命门：为什么合法链恒为 0

规格 §5.2 要求 L0/L1/L2 在递归结构上构成**直接上下级**才能登记，否则禁止产生新订单。两条路径都不能同时满足「合规」与「可运行」：

**路径一 · 真递归（L0=递归层2 / L1=层1 / L2=层0）→ `BLOCKED_BY_FROZEN_STRUCTURE`**

实测 510300 的 1m 全样本（250,560 根 / 1,044 交易日）：

```
线段单元 970 → level0 中枢 111 → LOCKED 走势类型 101（其中 94 个为盘整）
走势类型方向序列：down, up, down, down, up, down, down, up, up, ...
center_machine.validate_unit_sequence(level=1, TREND_TYPE)
  → ValueError: unit directions must alternate
递归止步 level 0；level 1 / level 2 永不存在
```

缠论中「上涨—盘整—上涨」是合法的走势类型连接，按净位移取向必然出现连续同向。该交替约束与真实走势类型序列结构性冲突。`validate_unit_sequence` 位于 `center_machine.py`（中枢算法），属绝对冻结范围，**未修改**。

**路径二 · 独立周期图 30m/5m/1m + 既有用户授权覆盖 → 因果对齐 0 命中**

| 环节 | 11 只 ETF 合计供给 |
|---|---:|
| L0 首中枢三买候选 | 55 |
| L1 独立完成走势 | 283 |
| **L2 定位点（1m level-0 一买/二买）** | **2** |
| 严格合法链 | **0** |

更根本的是 L2 定位点供给。510300 的 1m 全样本因果买卖点普查：

```
level0:3buy = 62    level0:3sell = 51
level0:1buy =  1    level0:2buy  =  0
```

**四年 1,044 个交易日只有 1 个一买、0 个二买。** §8.2 要求入场必须由 L2 一买（或带小转大证据的二买）在「首次完整 L1 回试终端」定位，三事件同窗联合命中的期望值≈0。

**结论：放宽对齐规则会制造规格外信号；扩大样本不改变每标的每四年 1 个一买的供给密度。** 该缺口只能通过（a）授权检视冻结核心的走势类型交替约束与一类点判定，或（b）由用户裁决修订规格 §8.2 的 L2 定位口径来关闭。两者都需要用户授权，本次未做。

## 16. 尚未完成及被阻断的项目

| 编号 | 级别 | 内容 | 状态 |
|---|---|---|---|
| R-01 | HIGH | builder 未校验候选父 V3 身份 | 已关闭（前序会话） |
| R-02 | HIGH | 声明哈希未先验复算 | 已关闭（前序会话） |
| **R-03** | **HIGH** | 结构信号来源不能由 builder 独立证明：coverage/signal 仅校验 completed/level/frequency，来源哈希取自输入自述 | **未关闭** |
| R-04 | HIGH | 持久战略退出不跨日续作 | **本次关闭**（含 TDD 反证） |
| R-05 | MEDIUM | `metrics.valid` 易被误读为收益有效 | **本次关闭** |
| R-06 | MEDIUM | CLI 不校验 `builder_contract` | **本次关闭** |
| B4 | CONFLICT | L0/L1/L2 非直接递归，现行覆盖把 `direct_recursive_levels_unique` 置 false | **需用户裁决** |
| — | — | 个股 `INDIVIDUAL_THREE_PROGRAM` 路径 | 无点时研究快照，路径关闭 |
| — | — | 逐笔成交与历史报价 | 无数据源，撮合以完成分钟柱代理 |
| — | — | 券商年份费率/涨跌停价历史 | 未认证 |
| — | — | 五槽组合调度、短差层、公司行为缩放 | 真实数据上从未触发，仅单元测试验证 |

## 17. 下一步模拟盘建议

在关闭 B4 之前**不要**进入模拟盘：当前体系在真实数据上不会产生任何订单，60 个交易日实时模拟只会得到 60 天空仓。

建议顺序：

1. **先做用户裁决（B4）**。二选一：(a) 授权在冻结核心之外独立复核走势类型交替约束与一类点判定口径，判断是实现保守还是原文要求；(b) 由你裁定 §8.2 的 L2 定位是否可放宽（例如允许 L2 三买或盘整背驰定位）——这属于修改唯一交易规格，必须由你签署，我不会自行发明。
2. **关闭 R-03**：为结构事实适配产物建立可复算的逐事件来源哈希，否则即使将来出现非空回放也无法排除手工构造事实。
3. **补齐数据**：券商年份费率表、历史涨跌停价、逐笔成交与报价；个股路径另需逐交易日 PIT 股票池、行业归属与财务披露可得时点。
4. 只有在真实数据上出现 ≥100 个战略周期与 ≥200 个短差周期后，才谈得上留出区间验证、滚动前推、基准与消融对比。
5. 无论后续结果如何，最终状态保持 **`LIVE_DISABLED`**。本次未提交、未推送、未连接真实账户、未发送真实订单或通知。
