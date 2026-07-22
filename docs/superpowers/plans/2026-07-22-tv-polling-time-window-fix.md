# TradingView 实时轮询时间窗口修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 QMT 当前在制 K 线，同时消除冷标的首次加载后实时轮询倒退 60 秒导致的 TradingView `time violation`。

**Architecture:** 只在前端 `DataPulseProvider` 的实时轮询查询上界增加固定 60 秒容差，使其与 TradingView 首次历史加载的内置时间口径一致。TypeScript 是唯一手写实现，页面实际加载的 `dist/bundle.js` 由现有 npm 构建生成；后端 `/tv/history`、QMT 数据和缠论结构计算保持不变。

**Tech Stack:** TypeScript 5.5、Rollup 4、Node.js `node:test`、Flask/Tornado、Playwright CLI。

## Global Constraints

- 盘中必须继续显示正在形成的当前 K 线。
- 实时查询容差固定为 60 秒，不扩展为当前图表周期长度。
- 不修改后端历史接口、QMT 行情实现、缓存或缠论计算。
- 不手工编辑 `dist/bundle.js`；必须由 `npm.cmd run build` 从 TypeScript 源码生成。
- Node 测试必须使用 `--test-reporter=tap` 逐文件运行并解析 `# pass N`；不得一次传入多个文件。
- 保留工作区现有未提交改动，只暂存本计划明确列出的文件；不推送远端。
- 每个提交使用中文消息，并附 `Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>`。

---

### Task 1: 对齐 DataPulse 实时轮询上界

**Files:**
- Create: `web/chanlun_chart/cl_app/static/js/__tests__/data_pulse_future_window.test.js`
- Modify: `web/chanlun_chart/cl_app/static/datafeeds/udf/src/data-pulse-provider.ts:19-29,118-138`
- Modify (generated): `web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`

**Interfaces:**
- Consumes: `UDFCompatibleDatafeed.subscribeBars(symbolInfo, resolution, callback, listenerGuid, resetCallback)` 和真实 bundle 中的 `DataPulseProvider._updateDataForSubscriber(listenerGuid)`。
- Produces: 每次实时轮询传给 `HistoryProvider.getBars` 的 `periodParams.to = floor(Date.now() / 1000) + 60`；其他参数和返回类型不变。

- [ ] **Step 1: 写入真实 bundle 的失败回归测试**

创建 `web/chanlun_chart/cl_app/static/js/__tests__/data_pulse_future_window.test.js`：

```javascript
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FIXED_NOW_MS = 1_784_687_664_000;

function loadDatafeeds(requestUrls) {
  class FixedDate extends Date {
    static now() {
      return FIXED_NOW_MS;
    }
  }

  const configuration = {
    supports_search: false,
    supports_group_request: false,
    supported_resolutions: ['1'],
    supports_marks: false,
    supports_timescale_marks: false,
    supports_time: false,
  };
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    Date: FixedDate,
    fetch: (url) => {
      const requestUrl = String(url);
      requestUrls.push(requestUrl);
      const payload = requestUrl.includes('/history?') ? { s: 'no_data' } : configuration;
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sb.globalThis = sb;
  sb.self = sb;
  sb.window = sb;
  vm.createContext(sb);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sb, { filename: 'bundle.js' });
  return sb.Datafeeds;
}

test('DataPulse 实时轮询上界应比当前时间多 60 秒以保留在制 K 线', async () => {
  const requestUrls = [];
  const { UDFCompatibleDatafeed } = loadDatafeeds(requestUrls);
  const datafeed = new UDFCompatibleDatafeed('http://test-datafeed', 30_000);
  const symbolInfo = { ticker: 'A:SH.688050', name: 'A:SH.688050' };

  datafeed.subscribeBars(symbolInfo, '1', () => {}, 'guid-1', () => {});
  await datafeed._dataPulseProvider._updateDataForSubscriber('guid-1');

  const historyUrl = requestUrls.find((url) => url.includes('/history?'));
  assert.ok(historyUrl, `应发出 history 请求，实际请求=${JSON.stringify(requestUrls)}`);
  const to = Number(new URL(historyUrl).searchParams.get('to'));
  assert.strictEqual(to, FIXED_NOW_MS / 1000 + 60);
});
```

- [ ] **Step 2: 单独运行新测试并确认 RED**

在仓库根目录运行：

```powershell
$testFile = 'web\chanlun_chart\cl_app\static\js\__tests__\data_pulse_future_window.test.js'
$out = & node --test --test-reporter=tap $testFile 2>&1
$exitCode = $LASTEXITCODE
$out
if ($exitCode -eq 0) { throw 'RED 阶段测试意外通过' }
if (-not ($out -match '1784687664') -or -not ($out -match '1784687724')) {
  throw 'RED 失败原因不是轮询上界缺少 60 秒容差'
}
```

Expected: 退出码非 0，断言显示 actual `1784687664`、expected `1784687724`。

- [ ] **Step 3: 最小修改 TypeScript 源码**

在 `data-pulse-provider.ts` 的接口定义之后加入常量：

```typescript
const REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS = 60;
```

把 `_updateDataForSubscriber` 中的查询上界改为：

```typescript
const rangeEndTime = parseInt((Date.now() / 1000).toString())
  + REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS;
```

保留 `rangeStartTime = rangeEndTime - periodLengthSeconds(...)`、`countBack: 2` 和 `firstDataRequest: false` 的现有行为。

- [ ] **Step 4: 从源码构建实际运行 bundle**

```powershell
npm.cmd run build
```

Working directory: `web/chanlun_chart/cl_app/static/datafeeds/udf`

Expected: TypeScript 编译和 Rollup 打包均退出 0，`dist/bundle.js` 包含 `rangeEndTime` 对应的 `+ 60` 逻辑。

- [ ] **Step 5: 单独运行新测试并确认 GREEN**

```powershell
$testFile = 'web\chanlun_chart\cl_app\static\js\__tests__\data_pulse_future_window.test.js'
$out = & node --test --test-reporter=tap $testFile 2>&1
$exitCode = $LASTEXITCODE
$out
if ($exitCode -ne 0 -or -not ($out -match '# pass 1')) {
  throw '新回归测试未通过'
}
```

Expected: `# pass 1`、`# fail 0`。

- [ ] **Step 6: 逐文件运行相邻前端回归测试**

对下列文件分别执行同一条命令，禁止合并：

```powershell
$cases = @(
  @{ Path = 'web\chanlun_chart\cl_app\static\js\__tests__\feed_realtime_prevbar.test.js'; Pass = 1 },
  @{ Path = 'web\chanlun_chart\cl_app\static\js\__tests__\force_refresh_datafeed.test.js'; Pass = 4 },
  @{ Path = 'web\chanlun_chart\cl_app\static\js\__tests__\history_provider_poll_merge.test.js'; Pass = 8 }
)
foreach ($case in $cases) {
  $out = & node --test --test-reporter=tap $case.Path 2>&1
  $exitCode = $LASTEXITCODE
  $out
  if ($exitCode -ne 0 -or -not ($out -match ("# pass " + $case.Pass))) {
    throw ("前端回归失败: " + $case.Path)
  }
}
```

Expected: 三个文件分别为 `# pass 1`、`# pass 4`、`# pass 8`，全部 `# fail 0`。

- [ ] **Step 7: 按单测名运行静态资源缓存回归**

分别运行：

```powershell
& 'D:\software\Python310\python.exe' -m pytest 'tests/web/test_cached_static_runtime.py::test_index_uses_runtime_version_for_unhashed_charting_entrypoint' -q
& 'D:\software\Python310\python.exe' -m pytest 'tests/web/test_cached_static_runtime.py::test_static_version_changes_when_standalone_entrypoint_changes' -q
```

Expected: 每条命令各 `1 passed`。

- [ ] **Step 8: 旁路直读并核对改动范围**

```powershell
Select-String -LiteralPath 'web\chanlun_chart\cl_app\static\datafeeds\udf\src\data-pulse-provider.ts' -Pattern 'REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS','rangeEndTime' -Context 1,2
Select-String -LiteralPath 'web\chanlun_chart\cl_app\static\datafeeds\udf\dist\bundle.js' -Pattern 'REALTIME_QUERY_FUTURE_TOLERANCE_SECONDS','rangeEndTime' -Context 1,2
git diff --name-only
```

Expected: 源码与生成 bundle 都含 60 秒容差；本任务新增/修改仅为测试、TypeScript 源码和 bundle，另加既有用户未提交文件但不纳入暂存。

- [ ] **Step 9: 精确暂存并提交修复**

```powershell
git add -- 'web/chanlun_chart/cl_app/static/js/__tests__/data_pulse_future_window.test.js' 'web/chanlun_chart/cl_app/static/datafeeds/udf/src/data-pulse-provider.ts' 'web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js'
git diff --cached --name-only
git commit -m '图表：修复实时轮询时间倒退' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>'
```

Expected: 暂存列表恰好为上述 3 个文件；提交成功且不包含工作区原有改动。

### Task 2: 真实页面运行时验收

**Files:**
- Verify only: `web/chanlun_chart/cl_app/static/datafeeds/udf/dist/bundle.js`

**Interfaces:**
- Consumes: 本地 `http://127.0.0.1:9900/`、现有登录配置、Playwright CLI 会话、QMT 实时行情。
- Produces: 冷标的强制刷新后的首次 history 与后续 polling 末根时间不倒退，浏览器控制台无新增 `time violation`。

- [ ] **Step 1: 新建浏览器会话并加载变更后的 bundle**

使用 Playwright CLI 新会话打开 `http://127.0.0.1:9900/login`，按真实页面流程登录，随后执行 `snapshot`。从页面网络请求确认 `bundle.js?v=<static_version>` 已重新加载，而不是复用旧会话资源。

- [ ] **Step 2: 触发冷标的强制刷新**

在最新 snapshot 中找到包含 `SH.688050` 的自选行并点击；重新 snapshot 后点击工具栏中的“重新加载数据”。记录这一动作之后的网络请求编号。

- [ ] **Step 3: 等待至少两轮实时轮询并检查请求**

等待出现一个 `firstDataRequest=true&force_refresh=1` history 请求和至少两个 `firstDataRequest=false` history 请求。读取这些响应末尾的 `t`，确认每次后续轮询末根时间均大于或等于首次加载末根时间，不再回退 60 秒。

- [ ] **Step 4: 检查页面和控制台**

重新 snapshot，确认图表标题为 `SH.688050`、当前价为数值且 K 线图区域存在；运行 `console error`，确认 Step 2 之后没有新增 `putToCacheNewBar: time violation` 或 `time order violation`。

- [ ] **Step 5: 独立核验提交与工作区**

```powershell
git rev-parse --short HEAD
git show --name-status --oneline --decorate -1
git status --short
```

Expected: HEAD 为 Task 1 的中文修复提交；提交只含 3 个任务文件；用户原有未提交改动仍在且未被覆盖或纳入提交。
