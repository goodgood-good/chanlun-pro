# TradingView 实时轮询时间窗口修复设计

日期：2026-07-22

## 背景与根因

QMT 的 1 分钟在制 K 线使用结束时刻标记。例如本地时间 10:34:24 时，当前在制 K 线的时间戳为 10:35:00。

TradingView 首次历史加载会把查询上界放到当前时间之后 60 秒，因此能收到这根 10:35:00 K 线；项目内 `DataPulseProvider` 的后续轮询却仅查询到当前时间，只能收到 10:34:00。冷标的首次加载后，第一轮轮询因此向后倒退 60 秒，TradingView 报 `putToCacheNewBar: time violation` 并丢弃更新，页面可能表现为部分标的行情加载异常。

运行时复现证据：同一标的 `SH.688050` 在 10:34:24 首次加载末根为 10:35:00，紧接着轮询末根为 10:34:00，回退恰好 60 秒。

## 目标

- 保留盘中正在形成的当前 K 线。
- 首次加载与实时轮询采用一致的 60 秒未来容差。
- 冷标的切换后不再产生时间倒退和 TradingView `time violation`。
- 不改变历史翻页、QMT 行情读取、缠论计算和缓存语义。

## 方案比较

1. **调整前端实时轮询窗口（采用）**：`DataPulseProvider` 的轮询结束时间设为当前时间加 60 秒，与 TradingView 首次加载一致。改动局部，保持当前在制 K 线可见。
2. **后端统一放宽轮询上界**：由 `/tv/history` 自动为实时轮询增加 60 秒。会隐式改变所有调用方的查询语义，影响面更大。
3. **仅丢弃倒退轮询结果**：可压制报错，但在制 K 线可能冻结到下一分钟，未解决窗口口径不一致。

## 设计

在 `web/chanlun_chart/cl_app/static/datafeeds/udf/src/data-pulse-provider.ts` 中定义明确的 60 秒实时查询容差，并在 `_updateDataForSubscriber` 计算 `rangeEndTime` 时加入该容差。

数据流保持不变：

1. TradingView 首次加载取得包含当前在制 K 线的历史数据。
2. `DataPulseProvider` 每轮仍请求最近 10 个周期、`countBack: 2`。
3. 轮询上界由 `now` 改为 `now + 60s`，确保可取得与首次加载相同或更新的末根。
4. 现有 `lastBarTime` 防回退逻辑继续作为保护层；SSE、历史翻页和后端接口不变。

源文件修改后通过现有 npm 构建生成实际由页面加载的 `static/datafeeds/udf/dist/bundle.js`，不手工维护两套逻辑。

## 测试与验收

严格按 TDD 执行：

1. 先增加 Node 运行时测试，冻结 `Date.now()`，触发真实 `DataPulseProvider` 轮询并断言发给 history provider 的 `to` 等于 `now + 60s`；修改实现前必须看到该测试因实际值仍为 `now` 而失败。
2. 最小修改 TypeScript 源码并重新构建 bundle，再逐文件运行新增测试及相关 history/polling/realtime 测试。
3. 运行受影响的 Python Web 测试，确认后端契约未变化。
4. 使用真实登录页面切换一个未预热的 A 股标的，等待至少两轮轮询；验收条件为 K 线与当前价正常显示，网络响应成功，控制台不再新增 `time violation`。

## 风险与边界

- 60 秒容差与 TradingView 当前首次加载行为一致，不扩展为任意周期长度，避免 5 分钟及以上周期过早纳入尚未形成的远期时间戳。
- 非 QMT 数据源即使没有结束时刻标记，增加 60 秒也只放宽请求上界，不会生成数据源不存在的 K 线。
- 若未来 TradingView 首次加载容差变化，应将两处口径提取为同一配置；本次保持最小改动，不做无关重构。
