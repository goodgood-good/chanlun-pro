# 严格结构日线源时间校验修复设计

## 背景与根因

当前分支在 `SH.513100 / 1D` 上稳定显示
`严格缠论结构暂不可用（strict_context_mismatch）`。运行时证据显示：

- `/tv/history` 原始末根时间与严格快照 `source_closed_at` 均为
  `1784703600`，即 `2026-07-22 15:00:00 +08:00`；
- UDF `HistoryProvider` 将该原始时间另存于 `bars_result.times`；
- TradingView 接收日线 `Bar` 后，把共享对象的 `bars[].time` 归一为该交易日
  `00:00:00 +08:00`，即 `1784649600`；
- 两个严格快照消费者随后错误地用归一化后的图表坐标时间校验
  `source_closed_at`，从而拒绝本来与原始行情完全一致的权威快照。

`master` 不包含严格快照协议和这项末根时间校验，所以没有相同提示；它与当前分支
都已保留 UDF 原始 `times`。本问题因此是严格协议迁移引入的前端回归，不是 QMT
行情缺失，也不是严格结构计算失败。

## 方案比较与结论

采用“原始传输时间用于数据身份，TradingView 时间用于图表坐标”的方案：

1. 严格校验优先读取 `barsResult.times` 的最后一个有效时间；
2. 老数据源没有 `times` 时，才回退现有 `bars[].time`；
3. `loadedRange`、可见区间和图形裁剪继续使用 `bars[].time`，不改变 TradingView
   坐标语义；
4. 标的、周期和末根原始时间仍必须精确相等，不增加容差，不允许旧快照降级混用。

不采用以下方案：

- 把后端日线及结构证据统一改成午夜：会破坏真实收盘时间、审计语义和结构版本；
- 允许 24 小时误差或只比较日期：可能放过真正的跨交易日旧快照；
- 移除末根校验或恢复 `master` 的旧图层路径：会丢失严格协议的 fail-closed 保证。

## 改动边界

仅修改两个严格快照消费者：

- `charts.js`：绘图前上下文校验使用原始末根时间；
- `chart_analysis.js`：右侧结构解盘摘要使用同一规则。

不修改后端 `/tv/history`、严格结构算法、中枢状态机、周期递归、价格基准、显示菜单
和 TradingView 数据坐标。数据源已有的 `times` 字段就是本次修复的权威输入，不新增
协议字段。

## 数据流与失败行为

```text
/tv/history response.t[-1]
        |
        +--> bars_result.times[-1] --------> 严格 source_closed_at 校验
        |
        +--> Bar.time --> TradingView 归一 --> 可见区间 / 图形坐标 / 裁剪
```

若 `times` 存在但为空、非有限数或不是整秒时间，校验失败；只有字段完全缺失时才为
兼容旧数据源回退 `bars[].time`。原始时间与快照不一致时仍保持
`strict_context_mismatch`，并清空严格图形，禁止使用旧结构兜底。

## 测试与验收

遵循 RED-GREEN：

1. 在 `charts_integration.test.js` 增加日线回归：`bars[].time` 为午夜、
   `times[-1]` 与 `source_closed_at` 为收盘时刻，严格绘图必须进入 `ready`；
2. 在 `chart_analysis_strict_snapshot.test.js` 增加相同日线回归，摘要必须进入
   `ready`；
3. 增加负例：原始 `times[-1]` 真正不匹配时仍必须被拒绝；
4. 逐文件用 `node --test --test-reporter=tap` 运行并结构化核对 `# pass N`；
5. 重启 `9900` 应用，在真实浏览器验证 `SH.513100` 的 `1m / 5m / 30m / 1D`：
   四个周期均为严格结构 `ready`，日线不再出现 `strict_context_mismatch`；
6. 直接检查日线原始响应、`bars_result.times[-1]` 与快照时间仍精确一致，并确认
   真正的错误标的、周期或原始末根时间继续 fail-closed。

## 非目标

- 不借本修复处理 UDF 轮询时可能出现的日线 `bars` 对象别名或重复合并问题；该问题
  与严格快照身份校验不同，若运行时证据表明影响行情显示，应单独设计和测试；
- 不改变中枢数量、区间、完成状态或虚实线；
- 不修改或合并 `pre` 分支，不处理工作区已有的无关改动。
