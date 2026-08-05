# V3 前向模拟与 QMT 板块逐日快照启用报告

生成日：2026-07-28（Asia/Shanghai）

## 最终状态

- 运行状态：`PAPER_OBSERVATION`
- 实盘状态：`LIVE_DISABLED`
- 真实账户访问：`false`
- 真实订单通道：`false`
- 参数状态：冻结，未因回测或前向观察结果调整
- 今日决策状态：`DATA_BLOCKED`，未启动决策流水线，未生成模拟订单或成交

本报告只确认前向观察设施已经启用，不把数据门阻断产生的空结果表述为 0 收益或有效绩效。

## 冻结参数

| 项 | 值 |
|---|---|
| 参数集 ID | `sha256:7c7f7f0fe638110ad891b5f98f87f6f4b784bfd15980239261c964f80d06cf0b` |
| 参数文件 SHA-256 | `sha256:a0bf30aa0ca9511caed01923d772614e68677c49ebcf288da578d8c7c88972cd` |
| 前向合同 ID | `sha256:705e511cf0954c7446672498bcfca771ef797c832e71c1ec8a3ad9484c2a2cab` |
| 选股路径 | `QMT_CURRENT_SECTOR_TECHNICAL_ONLY` |
| 战略 / 短差 / 定位周期 | `30m / 5m / 1m`，递归层级 `2 / 1 / 0` |
| 初始资金 | 1,000,000 元 |
| 槽位 | 5 个，每槽 18%，账户敞口上限 90% |
| 短差比例 | 25% |
| 三程序 | 关闭 |
| Tick 数据 | 不使用 |
| 成交约束 | 只使用已完成 1m K 线，禁止信号柱成交 |

参数来源：`audit/chanlun_trading_system_backtest/recent_year_current_sector_no3p/parameter_snapshot.json`。

## 自动运行安排

| Windows 任务 | 时间 | 动作 | 状态 |
|---|---:|---|---|
| `Chanlun-V3-Forward-Capture` | 周一至周五 09:10 | 捕获当日 QMT GICS3 板块成分，并尝试生成盘前证券/公司行为点时快照 | `Ready` |
| `Chanlun-V3-Forward-Evaluate` | 周一至周五 15:20 | 在收盘后检查数据门；满足时运行同一决策核心和累计模拟账本 | `Ready` |

两个任务都只调用 `ops/run_v3_forward_paper_daily.ps1`。它们不会启动或重启 QMT，不会登录账户，不会连接下单接口，也不会发送通知。

## QMT 板块逐日快照

账本：`.cache/chanlun_v3_qmt_sector_ledger/qmt_gics3_catalog_ledger.json`

- 哈希链条目：2 个（2026-07-27、2026-07-28）
- 今日条目：66 个板块、5,465 条板块—股票成员关系
- 今日条目哈希：`sha256:b6fdabf6cb9908d212d3e86b1f2011ba8e10800740927ed659468fc4f1ef2eb3`
- 账本内容哈希：`sha256:7bc114357b7cfb5758bada6bab64f3535adff6df38397b99fa53b113cc883a63`
- 账本文件哈希：`sha256:26cb4c4989a3b9c1eb965194b4492932776b4464fb48ce4ba09e0ebf77005ced`
- 前一账本已按原文件 SHA-256 归档：`archive/e8bd277003a6f098e769ceca44d18c4a1064d9b44027610dc90c340f695a1572.json`
- 同日同版本重试是幂等的，不追加重复条目，也不改写已被前向事件引用的收据

今日 QMT 原生 RPC 无法连接，因此捕获器按约定回退为只读本地 QMT `Sector/Temple/GICS` 文件。源文件最新修改时间为 2026-07-27 14:54:55，收据明确标记 `local_source_from_prior_calendar_date=true`；该事实不会被伪装成 2026-07-28 原生实时板块数据。

## 前向模拟账本与数据门

账本：`.cache/chanlun_v3_forward_paper/forward_paper_ledger.json`

- 文件 SHA-256：`sha256:260e5c75d384c3484a4dab5a3bbb15eab41ce2f93e344bbe9586b6515ac1eafa`
- 当前事件数：4
- 事件链：`PAPER_STARTED` → `CAPTURED` → `DATA_BLOCKED` → `DATA_BLOCKED`
- 重复评估会复用同一阻断事实，不追加重复事件
- 每个事件记录参数集、证据哈希、前一事件哈希及 `LIVE_DISABLED`

2026-07-28 当前阻断原因：

- 当日本地 1m 行情为 0 / 最低 240 根，且没有完成到 15:00；
- 当日本地 5m 行情为 0 / 最低 48 根，且没有完成到 15:00；
- 可用证券状态和公司行为点时快照只覆盖至 2026-07-24。

因此今天尚无可评价的前向收益、订单或成交。这是安全的数据门结果，不是策略收益为 0。

数据门通过后，系统将按以下单一路径运行：

1. 使用当日盘前已捕获的 QMT 板块成分；
2. 板块触发后生成技术候选；
3. 使用 30m 战略结构、5m 短差结构和 1m 精确定位；
4. 复用回测与模拟共用的决策核心；
5. 使用日批次累计重放，延续现金、持仓、T+1、成本、持久退出及短差信号状态；
6. 只写模拟账本，保持 `LIVE_DISABLED`。

## 验收

```text
python -m pytest -q tests/trading_system tests/core \
  tests/exchange/test_qmt_screening_sector_source.py
                                                        800 passed in 41.51s

python -m ruff check <本次所有 Python 改动文件>
                                                        All checks passed!

PowerShell AST parse: ops/run_v3_forward_paper_daily.ps1
PowerShell AST parse: ops/register_v3_forward_paper_tasks.ps1
                                                        0 parse errors
```

安全扫描未在前向入口、快照入口和任务脚本中发现 `xttrader`、`StockAccount`、`order_stock`、`passorder` 等真实交易 API 引用。

## 运行文件

- 控制入口：`tools/run_v3_forward_paper.py`
- 每日板块快照：`tools/snapshot_qmt_gics3_sector_ledger.py`
- 前向合同和事件账本：`src/chanlun/decision_support/trading_system/v3_forward_paper.py`
- 每日运行器：`ops/run_v3_forward_paper_daily.ps1`
- 定时任务注册器：`ops/register_v3_forward_paper_tasks.ps1`
