# Strict Calendar-Bar Source-Time Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让严格结构在日线被 TradingView 归一化图表坐标后，仍按 UDF 原始末根时间精确通过身份校验，并继续拒绝真正的旧快照。

**Architecture:** `barsResult.times` 保存 `/tv/history response.t` 的原始毫秒时间，是严格快照身份的权威来源；`barsResult.bars[].time` 保持 TradingView 图表坐标用途。两个严格快照消费者分别增加同语义的小型边界函数：字段存在时严格校验原始 `times`，字段缺失时兼容旧数据源回退 `bars`。

**Tech Stack:** 浏览器 JavaScript、Node.js `node:test`、TradingView UDF、Playwright CLI、PowerShell、Git。

## Global Constraints

- 只修改严格快照的两个前端消费者及其专用测试，不修改后端行情、严格结构算法或 TradingView 坐标。
- 标的、周期和原始末根时间继续精确匹配；禁止时间容差和旧结构降级。
- `times` 字段存在但为空或非法时必须失败；只有字段完全缺失时才回退 `bars[].time`。
- 所有 Node 测试必须逐文件执行 `node --test --test-reporter=tap`，并解析真实 `# pass N`。
- 使用当前工作区，不创建 worktree，不修改、合并或推送 `pre`。
- 只暂存本计划涉及的文件，保留工作区已有无关改动；提交信息使用中文并附指定 `Co-Authored-By`。

---

### Task 1: 结构解盘摘要使用原始末根时间

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/chart_analysis.js:616-656`
- Test: `web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js`

**Interfaces:**
- Consumes: `source.times?: number[]`（UDF 原始毫秒时间）、`source.bars: Bar[]`。
- Produces: `strictSourceClosedAt(source, bars) -> number`，返回用于严格身份校验的 epoch 秒；非法输入抛出明确错误。

- [ ] **Step 1: 写日线正例与真实错配保护测试**

在测试常量附近增加：

```js
const DAILY_BAR_AT = 1784649600;   // 2026-07-22 00:00 +08:00
const DAILY_CLOSE_AT = 1784703600; // 2026-07-22 15:00 +08:00
```

在严格快照测试中增加：

```js
test('daily summary validates strict source close against raw transport time', () => {
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const summary = Analysis.summarizeChartData(barsResult(strict, {
    times: [DAILY_CLOSE_AT * 1000],
    bars: [{ time: DAILY_BAR_AT * 1000, close: 11, isBarClosed: false }],
  }), { ...context, resolution: '1D' });

  assert.equal(summary.state, 'ready');
  assert.equal(summary.sourceClosedAt, DAILY_CLOSE_AT);
});

test('daily summary still rejects a genuinely stale raw transport time', () => {
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const summary = Analysis.summarizeChartData(barsResult(strict, {
    times: [(DAILY_CLOSE_AT - 86400) * 1000],
    bars: [{ time: DAILY_BAR_AT * 1000, close: 11, isBarClosed: false }],
  }), { ...context, resolution: '1D' });

  assert.equal(summary.state, 'syncing');
  assert.match(summary.statusDetail, /末根/);
});
```

- [ ] **Step 2: 运行测试并确认 RED 原因正确**

PowerShell：

```powershell
$file = 'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js'
$out = & node --test --test-reporter=tap $file 2>&1
$out
if ($out -notmatch '# fail ([1-9]\d*)') { throw 'expected RED failure was not observed' }
```

预期：日线正例失败，摘要状态为 `syncing` 而非 `ready`；不是语法错误或夹具错误。

- [ ] **Step 3: 增加最小源时间选择函数并接入校验**

在 `validateStrictSnapshot` 前增加：

```js
function strictSourceClosedAt(source, bars) {
  if (Object.prototype.hasOwnProperty.call(source, 'times')) {
    if (!Array.isArray(source.times) || source.times.length === 0) {
      throw new Error('严格结构原始末根时间无效');
    }
    const sourceClose = toSeconds(source.times[source.times.length - 1]);
    if (!Number.isInteger(sourceClose)) {
      throw new Error('严格结构原始末根时间无效');
    }
    return sourceClose;
  }
  return toSeconds(bars[bars.length - 1] && bars[bars.length - 1].time);
}
```

把现有：

```js
const loadedClose = toSeconds(bars[bars.length - 1] && bars[bars.length - 1].time);
```

替换为：

```js
const loadedClose = strictSourceClosedAt(source, bars);
```

- [ ] **Step 4: 运行测试并确认 GREEN**

```powershell
$file = 'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js'
$out = & node --test --test-reporter=tap $file 2>&1
$out
if ($LASTEXITCODE -ne 0 -or $out -notmatch '# pass (\d+)' -or $out -notmatch '# fail 0') { throw 'chart analysis test did not pass' }
```

预期：退出码 `0`、`# fail 0`，新增正例和负例均通过。

- [ ] **Step 5: 直读复核并提交独立单元**

```powershell
Select-String -LiteralPath web/chanlun_chart/cl_app/static/js/chart_analysis.js -Pattern 'strictSourceClosedAt|原始末根时间' -Context 2,8
git diff --check -- web/chanlun_chart/cl_app/static/js/chart_analysis.js web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js
git add -- web/chanlun_chart/cl_app/static/js/chart_analysis.js web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js
git commit --only -m "图表：按原始时间校验结构解盘快照" -m "Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>" -- web/chanlun_chart/cl_app/static/js/chart_analysis.js web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js
```

用独立命令确认 HEAD 已变化且提交只包含上述两个文件。

### Task 2: 严格图形绘制使用原始末根时间

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js:2174-2225`
- Test: `web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js`

**Interfaces:**
- Consumes: `chartData.barsResult.times?: number[]` 与现有 `_strictLoadedRange(bars)`。
- Produces: `ChartManager._strictSourceClosedAt(barsResult) -> number`；绘图范围仍由 `_strictLoadedRange` 返回的 TradingView 坐标决定。

- [ ] **Step 1: 写绘图校验的日线正例与真实错配负例**

在测试常量附近增加同一组 `DAILY_BAR_AT`、`DAILY_CLOSE_AT`，并增加：

```js
test('daily strict drawing validates source close against raw transport time', () => {
  const { cm } = manager('chart-manager-daily-source-time');
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const data = chartData('replace', strict, [
    { time: DAILY_BAR_AT * 1000, high: 12, low: 9 },
  ]);
  data.barsResult.times = [DAILY_CLOSE_AT * 1000];

  const validated = cm._validateStrictStructureSnapshot(strict, data, '1D');

  assert.equal(validated.context.interval, 'd');
});

test('daily strict drawing rejects a genuinely stale raw transport time', () => {
  const { cm } = manager('chart-manager-daily-stale-source-time');
  const strict = snapshot({
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
  });
  const data = chartData('replace', strict, [
    { time: DAILY_BAR_AT * 1000, high: 12, low: 9 },
  ]);
  data.barsResult.times = [(DAILY_CLOSE_AT - 86400) * 1000];

  assert.throws(
    () => cm._validateStrictStructureSnapshot(strict, data, '1D'),
    /source close/,
  );
});
```

`strict_center_reconcile_integration.test.js` 是仓库现有的严格 `ChartManager` 集成夹具，承担设计中所述的 `charts.js` 集成回归，比通用 `charts_integration.test.js` 更聚焦。

- [ ] **Step 2: 运行测试并确认 RED 原因正确**

```powershell
$file = 'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js'
$out = & node --test --test-reporter=tap $file 2>&1
$out
if ($out -notmatch '# fail ([1-9]\d*)') { throw 'expected RED failure was not observed' }
```

预期：日线正例因 `strict structure source close does not match loaded bars` 失败。

- [ ] **Step 3: 增加最小绘图源时间选择方法并接入校验**

在 `_strictLoadedRange` 后增加：

```js
_strictSourceClosedAt(barsResult) {
    if (Object.prototype.hasOwnProperty.call(barsResult || {}, 'times')) {
        const times = barsResult.times;
        if (!Array.isArray(times) || times.length === 0) {
            throw new Error('strict structure raw source close is invalid');
        }
        return this._strictApi().barTimeMsToEpochSeconds(times[times.length - 1]);
    }
    return this._strictLoadedRange(barsResult?.bars).to;
}
```

保留：

```js
const loadedRange = this._strictLoadedRange(chartData.barsResult?.bars);
```

并把快照相等比较改为：

```js
const sourceClosedAt = this._strictSourceClosedAt(chartData.barsResult);
if (snapshot.source_closed_at !== sourceClosedAt) {
    throw new Error('strict structure source close does not match loaded bars');
}
```

- [ ] **Step 4: 运行专用与相邻图表测试并确认 GREEN**

逐文件执行：

```powershell
$files = @(
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js'
)
foreach ($file in $files) {
  $out = & node --test --test-reporter=tap $file 2>&1
  $out
  if ($LASTEXITCODE -ne 0 -or $out -notmatch '# pass (\d+)' -or $out -notmatch '# fail 0') { throw "test failed: $file" }
}
```

- [ ] **Step 5: 直读复核并提交独立单元**

```powershell
Select-String -LiteralPath web/chanlun_chart/cl_app/static/js/charts.js -Pattern '_strictSourceClosedAt|raw source close' -Context 2,9
git diff --check -- web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js
git add -- web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js
git commit --only -m "图表：按原始时间校验严格绘图快照" -m "Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>" -- web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js
```

用独立命令确认 HEAD 已变化、用户无关暂存项仍存在且提交文件集合正确。

### Task 3: 全链路回归、重启与真实浏览器验收

**Files:**
- Verify: `web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js`
- Verify: `web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js`
- Verify: `web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js`
- Verify: `web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js`
- Runtime: `web/chanlun_chart/app.py`

**Interfaces:**
- Consumes: 两个已提交的严格源时间选择边界。
- Produces: 当前 HEAD 对应的运行服务，以及 `SH.513100` 四周期均为 `ready` 的浏览器证据。

- [ ] **Step 1: 逐文件运行全部相关 Node 测试**

```powershell
$files = @(
  'web/chanlun_chart/cl_app/static/js/__tests__/chart_analysis_strict_snapshot.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/strict_structure_history_transport.test.js',
  'web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js'
)
foreach ($file in $files) {
  $out = & node --test --test-reporter=tap $file 2>&1
  $out
  if ($LASTEXITCODE -ne 0 -or $out -notmatch '# pass (\d+)' -or $out -notmatch '# fail 0') { throw "test failed: $file" }
}
```

- [ ] **Step 2: 敌对自审改动面和失败保护**

```powershell
git diff HEAD~2..HEAD -- web/chanlun_chart/cl_app/static/js/charts.js web/chanlun_chart/cl_app/static/js/chart_analysis.js web/chanlun_chart/cl_app/static/js/__tests__
Select-String -LiteralPath web/chanlun_chart/cl_app/static/js/charts.js,web/chanlun_chart/cl_app/static/js/chart_analysis.js -Pattern 'source_closed_at.*loadedRange|loadedClose.*bars\['
```

确认不存在日期容差、标的/周期校验弱化或后端协议变化。

- [ ] **Step 3: 精确确认并重启 `9900` 服务**

先用 `Get-NetTCPConnection -LocalPort 9900 -State Listen` 和 `Win32_Process.CommandLine` 确认 PID 的命令行确为当前工作区 `web\chanlun_chart\app.py nobrowser`，再停止该 PID；随后：

```powershell
Start-Process -FilePath 'D:\software\Python310\python.exe' -ArgumentList @('D:\project\chanlun-pro\web\chanlun_chart\app.py','nobrowser') -WorkingDirectory 'D:\project\chanlun-pro' -WindowStyle Hidden
```

使用独立命令轮询 `/readyz`，断言 HTTP 200 且响应 revision 等于 `git rev-parse HEAD`。

- [ ] **Step 4: 用真实浏览器验证 513100 四周期**

打开 `http://127.0.0.1:9900`，登录后切换 `SH.513100`，逐一设置 `1`、`5`、`30`、`1D`。每个周期等待数据就绪并记录：

```js
({
  symbol: tvWidget.activeChart().symbol(),
  resolution: tvWidget.activeChart().resolution(),
  status: window.__cm['1']._strictStructureStatus,
  snapshotSymbol: window.__cm['1']._strictStructureSnapshot?.symbol,
  snapshotDisplay: window.__cm['1']._strictStructureSnapshot?.display_frequency,
  rawLast: window.__cm['1'].getChartData()?.barsResult?.times?.at(-1),
  chartLast: window.__cm['1'].getChartData()?.barsResult?.bars?.at(-1)?.time,
  sourceClosed: window.__cm['1'].getChartData()?.barsResult?.strict_structure?.source_closed_at,
})
```

验收：四周期 `status.state === 'ready'`、`code === null`；日线允许 `rawLast !== chartLast`，但必须满足 `rawLast / 1000 === sourceClosed`；页面不再显示 `strict_context_mismatch`。

- [ ] **Step 5: 最终 Git 与运行时复核**

用两个独立命令分别确认：

- 当前 HEAD 是本次两个实现提交后的新值，`/readyz` revision 与其一致；
- `git status --short` 中只有任务前已有的无关改动，没有测试或浏览器产物；
- 两个实现提交分别只包含计划列出的文件，`pre` 指针未变化。

