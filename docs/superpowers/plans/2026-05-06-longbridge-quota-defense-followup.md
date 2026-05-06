# Longbridge 月度配额防御 — Follow-up 实测记录

> 配套 plan：`docs/superpowers/plans/2026-05-06-longbridge-quota-defense.md`
> 实施完成时间：2026-05-06
> 当前 master HEAD：`59701c9`

## 完成的 commit 链

按顺序（base = `5cf8fb9 Revert "perf(cq): ..."`）：

1. `187c2ad feat(config): 增加长桥月度配额 + US 历史源切换 + prewarm zixuan-only 三个配置键`
2. `67decbc feat(quota): 长桥 history kline 月度配额追踪器（JSON 落盘 + 线程安全）`
3. `97695ff fix(quota): _save_to_disk 改 tmp+replace 原子写 + 失败日志，对齐项目惯例`（LB-2 code review 修复）
4. `711089a feat(cq): 集成 LbQuotaTracker，301607 主动短路 + 成功调用记账`
5. `d822446 fix(cq): 主动短路改用内部 _PreemptiveQuotaExhausted 哨兵，避开 SDK ABI + 加入 retry 黑名单`（LB-3 code review 修复）
6. `60fd100 feat(cq): US 历史 K 线 alpaca fallback 路由（config.US_HISTORY_KLINE_SOURCE 开关）`
7. `2c5fc4c feat(prewarm): US 市场 prewarm 限制为自选股（US_PREWARM_ZIXUAN_ONLY），保护长桥月度配额`
8. `59701c9 feat(app): 启动日志加长桥月度配额可观测性`

测试基线：**83 passed**（baseline 72 + 8 quota tracker + 3 alpaca 路由 = 83）。

## 用户运行后验证 checklist

复盘前请先：
1. 确认 `src/chanlun/config.py` 里这三键已配置（参考 `config.py.demo` 末尾）：
   - `LB_QUOTA_MONTHLY_LIMIT = 100`（按订阅档位调整：基础 100 / 10K HKD+ 400 / 800K HKD+ 2000 / 6M HKD+ 3000）
   - `US_HISTORY_KLINE_SOURCE = "longbridge"`（先保持默认观察行为，确认 alpaca 可用后再切）
   - `US_PREWARM_ZIXUAN_ONLY = True`（默认即开，立即收敛 prewarm 范围）

### 验证 1：启动横幅出现配额日志

在 PyCharm 跑 `web/chanlun_chart/app.py`，启动后观察日志，应看到形如：

```
[lb_quota] 当月已用 N/100 symbol，exhausted=...，US_HISTORY_KLINE_SOURCE=longbridge
```

**实测结果**（用户填写）：
```
（粘贴启动后的 [lb_quota] 行）
```

### 验证 2：prewarm 限制为自选股

启动后在 web UI 触发"启动当前市场全量缠论数据预热"（POST `/symbols/prewarm`），日志应见：

```
[_apply_market_filter] US prewarm 仅限自选股，N → M
```

其中 `N` 是过滤前的 US 标的数（>5000），`M` 应是你的自选股表里 US 标的数（通常 < 100）。

**实测结果**（用户填写）：
```
（粘贴 _apply_market_filter 行）
```

### 验证 3：未在自选股的 US 标的请求

打开一个**未在自选股、本月也未查询过**的 US symbol（如 `MSFT.US`，前提是它不在你 zixuan 表里）：

#### 3a. `US_HISTORY_KLINE_SOURCE = "longbridge"` 且 quota 已 exhausted
预期：
- 日志见 `[lb_quota] preemptive short-circuit symbol=MSFT.US count=... limit=100`（DEBUG 级，可能默认未输出，调级别看）
- 不再触发 longbridge SDK，不浪费配额
- 前端 K 线为空（业务上 `_fetch_segment_data` 收到 `_PreemptiveQuotaExhausted` → mark_exhausted（已经 exhausted, no-op）→ 跳出循环）

**实测结果**（用户填写）：
```
（粘贴日志 + 前端是否空图）
```

#### 3b. 切到 `US_HISTORY_KLINE_SOURCE = "alpaca"` 后再开
预期：
- 日志**不再**见 `[lb_quota]`、不再触发 longbridge
- alpaca 拉成功 → 前端正常显示 K 线
- 需先确认 alpaca-py 凭证：`ALPACA_APIKEY` / `ALPACA_SECRET`（或现有 `EXCHANGE_US = "alpaca"` 已经能正常工作）

**实测结果**（用户填写）：
```
（粘贴 alpaca 路径下 MSFT.US 是否拿到 K 线）
```

### 验证 4：月初配额自动重置（无法即时验证，仅记录）

`LbQuotaTracker._check_month_rollover` 在每次方法调用时检测月份切换：
- 当 `_current_month_key()` 返回不同字符串（YYYY-MM 维度），自动 `_symbols = set()` + `_exhausted = False` + 加载新月文件（不存在时从 0 开始）
- 测试 `test_month_change_resets_to_empty` 验证此逻辑

实际下月（2026-06-01）首次启动时：
- 旧文件 `~/.chanlun_pro/lb_quota_2026-05.json` 不再被读
- 新文件 `~/.chanlun_pro/lb_quota_2026-06.json` 不存在 → 从 0 开始
- 横幅应显示 `[lb_quota] 当月已用 0/100 symbol，exhausted=False`

## 已知遗留 / 不在本批范围

- 月初提醒 cron 任务（不必要：tracker 已自动按月切换）
- A 股 / HK 股的同类配额防御（不需要：基础订阅 100 配额主要影响 US；A 股/HK 实际撞 301607 概率低）
- 升级订阅级别（业务/付费决策，非代码）
- alpaca SDK 凭证向导 / 自动检测可用性（用户自行配置）

## 风险与回滚速查

| 改动 | 回滚命令 |
|---|---|
| 整批 8 commit | `git reset --hard 5cf8fb9` 退回到 plan 实施前 |
| 仅 prewarm zixuan-only | 设 `config.US_PREWARM_ZIXUAN_ONLY = False` |
| 仅 alpaca 路由 | 设 `config.US_HISTORY_KLINE_SOURCE = "longbridge"` |
| 仅主动短路 | 设 `config.LB_QUOTA_MONTHLY_LIMIT = 0`（关闭预检查，仅保留反应式 mark_exhausted） |

## 推送到远端的建议

8 个 commit + 之前 P1 实施期间的 4 个（probe + revert + fix + revert）混在 master 上。**建议**：
- `git log --oneline 5cf8fb9..HEAD` 当前 8 个干净 commit 是这一批的全部
- 推到 origin：`git push origin master`（与之前推过的 master 无冲突，纯前进）
- 如需 PR review，可单开分支 `git branch perf/lb-quota-defense 59701c9` 后 cherry-pick 8 个 commit
