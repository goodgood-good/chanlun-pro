# 笔中枢状态线型修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让各 K 线周期的笔中枢观察框按真实 `ongoing/completed` 状态分别使用虚线和实线，消除“多个已完成中枢看起来仍未完成”的误导。

**Architecture:** 保持后端严格快照、正式递归中枢和笔中枢观察层的数据边界不变，只修复 `ChartManager._createStrictShape()` 对 `center_observation` 的状态映射。笔中枢继续使用独立颜色、1 像素线宽和观察层透明度；只有线型随状态变化。

**Tech Stack:** JavaScript、Node.js `node:test`、TradingView Charting Library 包装层、PowerShell。

## Global Constraints

- 遵循已确认规格 `docs/superpowers/specs/2026-07-21-recursive-five-segment-display-screening-design.md:186-192`：进行中中枢为虚线，完成中枢为实线。
- 不修改正式中枢状态机、递归走势装配、QMT 行情或缓存协议。
- 不把笔中枢观察层并入正式递归、买卖点、选股或交易真值。
- Node 测试必须逐文件使用 `node --test --test-reporter=tap`，并解析 `# pass N`。
- 只提交本任务文件，不带入工作区既有修改；不触碰 `pre` 分支。

---

### Task 1: 让笔中枢线型跟随状态

**Files:**
- Modify: `web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js`
- Modify: `web/chanlun_chart/cl_app/static/js/charts.js`

**Interfaces:**
- Consumes: 严格快照中的 `center_observation.state`，取值为 `ongoing` 或 `completed`。
- Produces: `ChartManager._createStrictShape(item, currentInterval, bars)` 创建的矩形；`ongoing` 使用 `CHART_CONFIG.LINE_STYLES.DASHED`，`completed` 使用 `CHART_CONFIG.LINE_STYLES.SOLID`。

- [ ] **Step 1: 写失败的观察层线型回归测试**

在现有正式中枢虚实线测试后加入：

```javascript
test('stroke observation is dashed only while ongoing and solid when completed', () => {
  const observation = (state) => center(1, {
    render_kind: 'center_observation',
    source_kind: 'stroke_observation',
    state,
    tradable: false,
    render_id: `stroke-center-1@1@${state}`,
  });

  const ongoing = manager('chart-manager-observation-ongoing');
  ongoing.cm._drawStrictStructure(chartData('replace', snapshot({
    stroke_center_observations: [observation('ongoing')],
    levels: [],
  })), '5');
  assert.equal(ongoing.calls.create[0].options.overrides.linestyle, 2);

  const completed = manager('chart-manager-observation-completed');
  completed.cm._drawStrictStructure(chartData('replace', snapshot({
    stroke_center_observations: [observation('completed')],
    levels: [],
  })), '5');
  assert.equal(completed.calls.create[0].options.overrides.linestyle, 0);
});
```

- [ ] **Step 2: 逐文件运行测试并确认 RED**

Run:

```powershell
$out = node --test --test-reporter=tap 'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js'
$out
if ($LASTEXITCODE -eq 0 -or -not ($out -match '2 !== 0')) { exit 1 }
```

Expected: 新测试失败，完成的笔中枢实际线型仍为 `2`，断言期望 `0`。

- [ ] **Step 3: 最小实现状态映射**

把 `center_observation` 分支改为先计算状态线型，并同时传入 item 与 overrides：

```javascript
if (item.render_kind === 'center_observation') {
    const linestyle = item.state === 'ongoing'
        ? CHART_CONFIG.LINE_STYLES.DASHED
        : CHART_CONFIG.LINE_STYLES.SOLID;
    return ChartUtils.createZhongshuShape(this.chart, { ...item, linestyle }, {
        color: getDynamicColor(currentInterval, 'bi_zss'),
        linewidth: 1,
        overrides: { transparency: 98, linestyle },
    });
}
```

- [ ] **Step 4: 逐文件运行测试并确认 GREEN**

Run:

```powershell
$out = node --test --test-reporter=tap 'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js'
$out
if ($LASTEXITCODE -ne 0 -or -not ($out -match '# pass 10') -or -not ($out -match '# fail 0')) { exit 1 }
```

Expected: `# pass 10`、`# fail 0`。

- [ ] **Step 5: 回归完整图表集成测试**

Run:

```powershell
$out = node --test --test-reporter=tap 'web/chanlun_chart/cl_app/static/js/__tests__/charts_integration.test.js'
$out
if ($LASTEXITCODE -ne 0 -or -not ($out -match '# pass 46') -or -not ($out -match '# fail 0')) { exit 1 }
```

Expected: `# pass 46`、`# fail 0`。

- [ ] **Step 6: 在真实 513100 四周期页面验证**

使用现有 9900 应用逐一切换 `1`、`5`、`30`、`1D`，读取 `window.__cm['1']._strictStructureSnapshot` 与 `_strictContainers`，并通过 `getShapeById(id).getProperties().linestyle` 交叉核对：

- `center_observation + completed` 的实际线型只能为 `0`；
- `center_observation + ongoing` 的实际线型只能为 `2`；
- `formal_center + completed` 保持 `0`，`formal_center + ongoing` 保持 `2`；
- 四周期严格快照状态均为 `ready`，浏览器控制台无新增错误。

- [ ] **Step 7: 提交经过验证的独立修复**

```powershell
git add -- 'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js' 'web/chanlun_chart/cl_app/static/js/charts.js'
git commit --only -m '图表：按状态区分笔中枢虚实线' -m 'Co-Authored-By: Codex Opus 4.8 (1M context) <noreply@anthropic.com>' -- 'web/chanlun_chart/cl_app/static/js/__tests__/strict_center_reconcile_integration.test.js' 'web/chanlun_chart/cl_app/static/js/charts.js'
```

提交后独立运行 `git rev-parse --short HEAD` 与 `git status --short`，确认 HEAD 前进且既有工作区修改未被带入。
