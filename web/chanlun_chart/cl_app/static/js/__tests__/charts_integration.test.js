'use strict';
// charts.js 接线层集成测试：用 vm + 最小 window/document stub 把**真实 charts.js** 加载进
// Node，用 mock widget/datafeed 驱动真实的 _doReset / _getViewLatestSec，验证断网恢复治本
// 的副作用编排(防抖/退避/resetCache+resetData/前缀回退)。覆盖纯函数单测够不到的接线层
// (reviewer L-2 缺口)。不依赖 jsdom/浏览器；TradingView 像素渲染不在覆盖范围(那是 TV 职责)。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// ── 把真实 charts.js 加载进 vm 沙箱，取出 ChartManager 类 ──────────────
function loadChartManager() {
  const sseGap = require('../sse_gap.js');
  let _nowMs = 1000 * 1000; // 可控时钟(秒*1000)

  const RealDate = Date;
  function MockDate(...a) { return a.length ? new RealDate(...a) : new RealDate(_nowMs); }
  MockDate.now = () => _nowMs;
  MockDate.UTC = RealDate.UTC.bind(RealDate);
  MockDate.prototype = RealDate.prototype;

  const sb = {
    console, Math, JSON, Intl, Array, Object, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isFinite, isNaN, Set, Map, WeakMap, WeakSet, Symbol, Promise, Error,
    URLSearchParams,
    Date: MockDate,
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    performance: { now: () => 0 },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    CustomEvent: function () {},
    EventSource: function (url) {
      this.url = url; this._listeners = {}; this.readyState = 1;
      this.addEventListener = (type, cb) => { this._listeners[type] = cb; };
      this.close = () => {};
      sb.__lastES = this; // 暴露最近创建的 ES 供测试驱动 'chanlun' 帧
    },
    Utils: { get_market: () => 'a', get_local_data: () => '5' },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map(), activeManagerId: null }),
    Datafeeds: { UDFCompatibleDatafeed: function () {} },
    TradingView: { widget: function () {} },
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    navigator: { onLine: true },
    location: { search: '', reload: () => {} },
  };
  sb.window = sb; sb.self = sb; sb.globalThis = sb;
  sb.window.SseGap = sseGap;
  sb.document = {
    addEventListener() {}, removeEventListener() {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {}, addEventListener() {} }),
    getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    body: { appendChild() {} },
  };
  sb._setNowSec = (sec) => { _nowMs = sec * 1000; };

  vm.createContext(sb);
  let src = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  src += '\n;var __CM_EXPORT = (typeof ChartManager !== "undefined") ? ChartManager : null;';
  src += '\n;var __CU_EXPORT = (typeof ChartUtils !== "undefined") ? ChartUtils : null;';
  src += '\n;var __CDF_EXPORT = (typeof CHART_DISABLED_FEATURES !== "undefined") ? CHART_DISABLED_FEATURES : null;';
  src += '\n;var __ICI_EXPORT = (typeof getInitialChartInterval !== "undefined") ? getInitialChartInterval : null;';
  src += '\n;var __SLLC_EXPORT = (typeof shouldLoadLastChart !== "undefined") ? shouldLoadLastChart : null;';
  src += '\n;var __RDS_EXPORT = (typeof requestedDefaultStudies !== "undefined") ? requestedDefaultStudies : null;';
  src += '\n;var __CWVO_EXPORT = (typeof chartWidgetViewportOptions !== "undefined") ? chartWidgetViewportOptions : null;';
  vm.runInContext(src, sb, { filename: 'charts.js' });
  return {
    ChartManager: sb.__CM_EXPORT,
    ChartUtils: sb.__CU_EXPORT,
    disabledFeatures: sb.__CDF_EXPORT,
    initialChartInterval: sb.__ICI_EXPORT,
    shouldLoadLastChart: sb.__SLLC_EXPORT,
    requestedDefaultStudies: sb.__RDS_EXPORT,
    chartWidgetViewportOptions: sb.__CWVO_EXPORT,
    sb,
  };
}

// 裸实例(绕过依赖重的构造函数/init)，手动注入治本相关字段。
function makeManager(ChartManager, mockWidget, barsResultMap) {
  const cm = Object.create(ChartManager.prototype);
  cm._resetState = {};
  cm._needResetOnNextData = false;
  cm._disconnectedSinceMs = null;
  cm.widget = mockWidget;
  cm.udf_datafeed = { _historyProvider: { bars_result: barsResultMap || new Map() } };
  return cm;
}

function makeDrawingPersistenceManager(ChartManager) {
  const cm = Object.create(ChartManager.prototype);
  cm._drawingSaveInFlight = false;
  cm._drawingSavePending = null;
  cm._persistedDrawingFingerprints = new Map();
  cm._drawingLoadsInFlight = new Map();
  return cm;
}

function spyWidget() {
  const calls = { resetCache: 0, resetData: 0 };
  const widget = {
    resetCache: () => { calls.resetCache++; },
    activeChart: () => ({ resetData: () => { calls.resetData++; } }),
  };
  return { widget, calls };
}

test('widget startup uses only schema-valid theme and time-scale options', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  assert.match(source, /theme:\s*normalizeChartTheme\(Utils\.get_local_data\("theme"\)\)/);
  assert.match(source, /time_scale:\s*\{\s*min_bar_spacing:/);
  assert.doesNotMatch(source, /max_bar_spacing/);
});

test('embedded cross-market symbol changes reuse the current chart page', () => {
  const { ChartManager, sb } = loadChartManager();
  const writes = [];
  let navigations = 0;
  let watchlistRenders = 0;
  sb.Utils.get_market = () => 'a';
  sb.Utils.set_local_data = (key, value) => writes.push([key, value]);
  sb.location.assign = () => { navigations += 1; };
  sb.ZiXuan = { render_zixuan_opts: () => { watchlistRenders += 1; } };
  sb.__CHANLUN_EMBEDDED_CHART = true;

  const manager = Object.create(ChartManager.prototype);
  let clears = 0;
  manager.clear_draw_chanlun = () => { clears += 1; };
  manager.reloadDrawingsForCurrentContext = () => {};
  manager._openSseStream = () => {};
  manager.handleSymbolChange({ ticker: 'US:AAPL.US' });

  assert.equal(navigations, 0);
  assert.equal(clears, 1);
  assert.equal(watchlistRenders, 0);
  assert.deepEqual(writes, [['market', 'us'], ['us_code', 'AAPL.US']]);
});

test('standalone cross-market symbol changes retain the full-page navigation fallback', () => {
  const { ChartManager, sb } = loadChartManager();
  const writes = [];
  const targets = [];
  sb.Utils.get_market = () => 'a';
  sb.Utils.set_local_data = (key, value) => writes.push([key, value]);
  sb.location.assign = (target) => targets.push(target);
  sb.__CHANLUN_EMBEDDED_CHART = false;

  const manager = Object.create(ChartManager.prototype);
  manager.handleSymbolChange({ ticker: 'US:MSFT.US' });

  assert.deepEqual(writes, [['market', 'us']]);
  assert.deepEqual(targets, ['/?market=us']);
});

function makeReconcileManager(ChartManager) {
  const cm = Object.create(ChartManager.prototype);
  const calls = { create: 0, remove: 0, setProperties: [] };
  let nextId = 1;
  cm.obj_charts = { S: { test_shapes: [] } };
  cm._reconcileGuard = {};
  cm._reconcileOwnedIds = new Set();
  cm._reconcileRetry = { count: 0, timer: null };
  cm.chart = {
    removeEntity() { calls.remove++; },
    getShapeById() {
      return { setProperties(props) { calls.setProperties.push(props); } };
    },
  };
  const create = () => {
    calls.create++;
    return nextId++;
  };
  return { cm, calls, create };
}

function zone(index, delta = 0, linestyle = '0') {
  const time = 1700000000 + index * 1000;
  return {
    linestyle,
    points: [
      { time, price: 10 + index },
      { time: time + 900, price: 11 + index + delta },
      { time: time + 1800, price: 9 + index },
      { time: time + 2700, price: 10 + index },
    ],
  };
}

function makeDataReadyManager(ChartManager) {
  const cm = Object.create(ChartManager.prototype);
  let ready = false;
  let readyCallback = null;
  let symbol = 'A:SH.600000';
  let resolution = '5';
  const calls = { draw: 0, debounced: 0, widen: 0 };
  cm.instanceId = 'chart-manager-1';
  cm._dataContextGeneration = 0;
  cm._tvDataReadyGeneration = -1;
  cm._tvDataReadyIdentity = null;
  cm._pendingChanlunDrawGeneration = null;
  cm._pendingChanlunDrawIdentity = null;
  cm._dataReadyProbeGeneration = null;
  cm._dataReadyProbeIdentity = null;
  cm._initialLoadDone = false;
  cm._strictStructureStatus = { state: 'ready', code: null };
  cm._strictStructureSnapshot = { render_revision: 'fixture' };
  cm._strictStructureContextToken = 'a:sh.600000|5|fixture';
  cm._strictReconcileComplete = () => true;
  cm.chart = {
    symbol: () => symbol,
    resolution: () => resolution,
    dataReady(callback) {
      if (callback) readyCallback = callback;
      return ready;
    },
  };
  cm.getCurrentChartIdentity = () => ({ symbol, interval: resolution });
  cm.draw_chanlun = () => { calls.draw++; };
  cm.debouncedDrawChanlun = () => { calls.debounced++; };
  cm._maybeWidenDefaultView = () => { calls.widen++; };
  return {
    cm,
    calls,
    setReady(value) { ready = value; },
    setIdentity(nextSymbol, nextResolution) {
      symbol = nextSymbol;
      resolution = nextResolution;
    },
    fireReady() { if (readyCallback) readyCallback(); },
  };
}

function barsReadyEvent() {
  return {
    detail: {
      managerId: 'chart-manager-1',
      symbol: 'a:sh.600000',
      resolution: '5',
    },
  };
}

test('分型只使用已加载真实K线坐标，日线收盘时刻会规整到对应日K', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  const rawClose = Date.UTC(2026, 6, 22, 7) / 1000;
  const chartTime = Date.UTC(2026, 6, 22) / 1000;
  const missingRawClose = Date.UTC(2026, 6, 23, 7) / 1000;
  const source = [
    {
      text: 'ding',
      points: [
        { time: rawClose, price: 12 },
        { time: rawClose, price: 12 },
      ],
    },
    {
      text: 'di',
      points: [
        { time: missingRawClose, price: 9 },
        { time: missingRawClose, price: 9 },
      ],
    },
  ];

  const rendered = cm._fractalRenderList(
    source,
    [{ time: chartTime * 1000 }],
    { from: chartTime - 1, to: chartTime + 1 },
    '1D',
  );

  assert.equal(rendered.length, 1, '对应K线尚未加载的分型不得交给TV吸附');
  assert.deepEqual(rendered[0].points.map((point) => point.time), [chartTime, chartTime]);
  assert.deepEqual(source[0].points.map((point) => point.time), [rawClose, rawClose], '不得修改后端原始证据');
});

test('A股日内结构统一从收盘身份映射到开盘坐标且不修改原证据', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SZ.001270', interval: '5' }),
  };
  const closeTime = Date.UTC(2026, 6, 30, 3, 30) / 1000;
  const source = [{
    points: [
      { time: closeTime - 300, price: 11 },
      { time: closeTime, price: 10 },
    ],
  }];

  assert.equal(cm._centerChartTimeCoordinate(closeTime, '5'), closeTime - 300);
  const rendered = cm._baseStructureRenderList(source, '5');
  assert.deepEqual(
    rendered[0].points.map((point) => point.time),
    [closeTime - 600, closeTime - 300],
  );
  assert.deepEqual(
    source[0].points.map((point) => point.time),
    [closeTime - 300, closeTime],
  );

  cm.widget = {
    symbolInterval: () => ({ symbol: 'currency_spot:BTC/USDT', interval: '5' }),
  };
  assert.equal(cm._centerChartTimeCoordinate(closeTime, '5'), closeTime);
});

test('dense base structures are rendered as bounded open paths instead of one line-tool per segment', () => {
  const { ChartManager, ChartUtils } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  cm.widget = {
    symbolInterval: () => ({ symbol: 'currency_spot:BTC/USDT', interval: '5' }),
  };
  const source = [];
  for (let index = 0; index < 130; index += 1) {
    source.push({
      state: 'completed',
      locked: true,
      linestyle: 0,
      points: [
        { time: 1_700_000_000 + index * 300, price: 10 + (index % 2) },
        { time: 1_700_000_000 + (index + 1) * 300, price: 10 + ((index + 1) % 2) },
      ],
    });
  }
  source.push({
    state: 'forming',
    locked: false,
    linestyle: 1,
    points: [
      { time: 1_700_000_000 + 130 * 300, price: 10 },
      { time: 1_700_000_000 + 131 * 300, price: 11 },
    ],
  });

  const batches = cm._baseStructurePathRenderList(source, '5', 0);
  assert.deepEqual(batches.map((item) => item.segmentCount), [64, 64, 2, 1]);
  assert.deepEqual(batches.map((item) => item.points.length), [65, 65, 3, 2]);
  assert.equal(batches.at(-1).linestyle, 2, 'forming tail remains visually dashed');

  let created = null;
  ChartUtils.createPathShape({
    createMultipointShape(points, options) {
      created = { points, options };
      return 'path-1';
    },
  }, batches[0], { color: '#123456', linewidth: 2 });
  assert.equal(created.options.shape, 'path');
  assert.equal(created.options.overrides['linetoolpath.lineColor'], '#123456');
  assert.equal(created.options.overrides['linetoolpath.lineWidth'], 2);
  assert.equal(created.options.overrides['linetoolpath.leftEnd'], 0);
  assert.equal(created.options.overrides['linetoolpath.rightEnd'], 0);
  assert.equal('filled' in created.options.overrides, false, '开放路径不得携带多边形闭合状态');
});

test('稳定重绘已覆盖几何校验时取消旧验证定时器但保留新创建验证', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  cm._verifyRebuildTimer = 99;
  cm._verifyRebuildVersion = 4;
  cm._verifyRebuildScheduledAt = -300;
  cm._strictPendingCreates = new Map();
  cm._strictReconcileComplete = () => true;
  cm._defaultViewFallbackTimer = null;
  cm._defaultViewAttemptTimer = null;
  cm._defaultViewRetryTimer = null;
  cm._defaultViewApplySettleTimer = null;
  cm._defaultViewApplyPending = false;

  assert.equal(cm._cancelCoveredVerifyRebuild(4, -300), true);
  assert.equal(cm._verifyRebuildTimer, null);

  cm._verifyRebuildTimer = 100;
  cm._verifyRebuildVersion = 5;
  cm._verifyRebuildScheduledAt = -300;
  assert.equal(
    cm._cancelCoveredVerifyRebuild(4, -300),
    false,
    'redraw-created strict geometry increments the version and still needs deferred verification',
  );
  assert.equal(cm._verifyRebuildTimer, 100);
});

test('drawChartElements 为分型接入真实K线规整与创建后锚点校验', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  const rawClose = Date.UTC(2026, 6, 22, 7) / 1000;
  const chartTime = Date.UTC(2026, 6, 22) / 1000;
  const calls = [];
  cm.obj_charts = {};
  cm.chart = {};
  cm.cl_show_config = { fx: true };
  cm.reconcile = (...args) => calls.push(args);
  cm._drawStrictStructure = () => {};
  cm.updateDrawPalette = () => {};
  cm._sweepOrphanTimer = null;
  cm._disposed = false;

  cm.drawChartElements({
    symbolKey: 'a:SH.600000_1D',
    from: chartTime - 1,
    visibleRange: { from: chartTime - 1, to: chartTime + 1 },
    barsResult: {
      bars: [{ time: chartTime * 1000 }],
      fxs: [{
        text: 'ding',
        points: [
          { time: rawClose, price: 12 },
          { time: rawClose, price: 12 },
        ],
      }],
      bis: [], xds: [],
    },
  }, '1D');

  const fractalCall = calls.find((args) => args[0] === 'fxs');
  assert.ok(fractalCall, '必须进入分型 reconcile');
  assert.deepEqual(fractalCall[1][0].points.map((point) => point.time), [chartTime, chartTime]);
  assert.equal(typeof fractalCall[7], 'function', '必须校验TV创建后的实际锚点');
});

test('reconcile: 列表尾部边界变化且数量/from 不变时仍替换 shape', () => {
  const { ChartManager } = loadChartManager();
  const { cm, calls, create } = makeReconcileManager(ChartManager);
  const initial = Array.from({ length: 8 }, (_, index) => zone(index));
  cm.reconcile('test_shapes', initial, 1600000000, 'S', create, false, true);
  assert.equal(calls.create, 8);

  const corrected = Array.from(
    { length: 8 },
    (_, index) => zone(index, index === 7 ? 0.5 : 0),
  );
  cm.reconcile('test_shapes', corrected, 1600000000, 'S', create, false, true);

  assert.equal(calls.remove, 1, '旧的最后一个中枢必须删除');
  assert.equal(calls.create, 9, '修正后的最后一个中枢必须重建');
});

test('reconcile: 完全相同的图形状态重复进入时保持零操作', () => {
  const { ChartManager } = loadChartManager();
  const { cm, calls, create } = makeReconcileManager(ChartManager);
  const zones = Array.from({ length: 8 }, (_, index) => zone(index));
  cm.reconcile('test_shapes', zones, 1600000000, 'S', create, false, true);
  const before = { ...calls };
  cm.reconcile('test_shapes', zones, 1600000000, 'S', create, false, true);
  assert.equal(calls.create, before.create);
  assert.equal(calls.remove, before.remove);
});

test('reconcile: 旧异步创建晚到时不得把旧范围重新写回当前容器', async () => {
  const { ChartManager } = loadChartManager();
  const { cm, calls } = makeReconcileManager(ChartManager);
  let resolveOld;
  const oldPromise = new Promise((resolve) => { resolveOld = resolve; });
  const oldZone = zone(0);
  const currentZone = zone(1);

  cm.reconcile('test_shapes', [oldZone], 1_600_000_000, 'S', () => oldPromise, false, true);
  cm.reconcile('test_shapes', [currentZone], 1_600_000_000, 'S', () => 200, false, true);
  resolveOld(100);
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(cm.obj_charts.S.test_shapes.map((entry) => entry.id), [200]);
  assert.equal(calls.remove, 1, '晚到的旧实体应立即删除');
});

test('reconcile: TradingView 后续把分型吸附到错误K线时自动删除并重建', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  const calls = { create: [], remove: [] };
  const created = new Map();
  let nextId = 1;
  let driftFirst = false;
  cm.obj_charts = { S: { fxs: [] } };
  cm._reconcileGuard = {};
  cm._reconcileEpochs = new Map();
  cm._reconcileOwnedIds = new Set();
  cm._pendingRemovalIds = new Set();
  cm._reconcileRetry = { count: 0, timer: null };
  cm._disposed = false;
  cm._verifyingUntil = null;
  let stableRechecks = 0;
  cm._scheduleVerifyRebuild = () => { stableRechecks++; };
  cm.chart = {
    removeEntity(id) { calls.remove.push(id); created.delete(id); },
    getShapeById(id) {
      const item = created.get(id);
      if (!item) return null;
      return {
        getPoints() {
          const points = item.points.map((point) => ({ ...point }));
          if (id === 1 && driftFirst) points[0].time += 300;
          return points;
        },
      };
    },
  };
  const fractal = {
    text: 'ding',
    points: [
      { time: 1_700_000_000, price: 12 },
      { time: 1_700_000_000, price: 12 },
    ],
  };
  const create = (item) => {
    const id = nextId++;
    created.set(id, item);
    calls.create.push(id);
    return id;
  };
  const verify = (item, id) => cm._fractalCreatedAnchorMatches(item, id);

  cm.reconcile('fxs', [fractal], 1_600_000_000, 'S', create, false, false, verify);
  driftFirst = true;
  cm.reconcile('fxs', [fractal], 1_600_000_000, 'S', create, false, false, verify);

  assert.deepEqual(calls.create, [1, 2]);
  assert.deepEqual(calls.remove, [1]);
  assert.deepEqual(cm.obj_charts.S.fxs.map((entry) => entry.id), [2]);
  assert.equal(stableRechecks, 2, '每次接受分型后都要在稳定画布上再复核一次');
});

test('reconcile: 截断范围后的未完成状态变化必须刷新样式', () => {
  const { ChartManager } = loadChartManager();
  const { cm, calls, create } = makeReconcileManager(ChartManager);
  const pending = Array.from({ length: 8 }, (_, index) => zone(index, 0, '1'));
  cm.reconcile('test_shapes', pending, 1600000000, 'S', create, false, true);
  const corrected = Array.from(
    { length: 8 },
    (_, index) => zone(index, 0, index === 7 ? '0' : '1'),
  );
  cm.reconcile('test_shapes', corrected, 1600000000, 'S', create, false, true);
  assert.equal(calls.setProperties.at(-1)?.linestyle, 0);
});

test('首次 bars-ready 早于 TradingView dataReady 时不得创建缠论 shape', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  assert.equal(fx.calls.draw, 0);
  assert.equal(fx.calls.debounced, 0);
  assert.equal(fx.cm._initialLoadDone, false);
});

test('draw_chanlun 旁路调用在 dataReady 前只登记待绘制，不读取图表数据', async () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  sb.setTimeout = (callback) => { callback(); return 0; };
  fx.cm._intervalGeneration = 0;
  fx.cm._intervalSwitchSeq = 0;
  fx.cm.getChartData = () => {
    throw new Error('未就绪时不应读取图表数据');
  };
  fx.cm.draw_chanlun = ChartManager.prototype.draw_chanlun.bind(fx.cm);

  await fx.cm.draw_chanlun();

  assert.equal(fx.cm._pendingChanlunDrawGeneration, 0);
  assert.equal(fx.cm._initialLoadDone, false);
});

test('当前代际 dataReady 后只消费一次首次待绘制请求', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  fx.setReady(true);
  fx.fireReady();
  fx.fireReady();
  assert.equal(fx.calls.draw, 1);
  assert.equal(fx.calls.debounced, 0);
  assert.equal(fx.cm._initialLoadDone, true);
});

test('embedded data-ready waits until automatic drawings are stably complete', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = true;
  sb.EmbeddedChartBridge = {
    notifyDataReady(identity) { notifications.push(identity); },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._automaticShapeCreateCount = 1;
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.cm._verifyRebuildTimer = null;
  fx.cm._sweepOrphanTimer = null;
  fx.setReady(true);

  fx.cm.handleDataReady(0, 'a:sh.600000|5');
  assert.equal(notifications.length, 0);

  function runUntil(target) {
    while (true) {
      const next = [...timers.entries()]
        .sort((left, right) => left[1].due - right[1].due)[0];
      if (!next || next[1].due > target) break;
      timers.delete(next[0]);
      now = next[1].due;
      next[1].callback();
    }
    now = target;
  }

  runUntil(700);
  assert.equal(notifications.length, 0, 'pending shapes must keep the parent loading state');
  fx.cm._automaticShapeCreateCount = 0;
  runUntil(950);
  assert.equal(notifications.length, 1);
  assert.deepEqual(notifications[0], { symbol: 'A:SH.600000', interval: '5' });
});

test('embedded stable-ready has no fixed latency once every tracked phase is idle', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = true;
  sb.EmbeddedChartBridge = {
    notifyDataReady(identity) { notifications.push(identity); },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.setReady(true);

  fx.cm.handleDataReady(0, 'a:sh.600000|5');
  while (timers.size > 0 && now < 200) {
    const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
    timers.delete(next[0]);
    now = next[1].due;
    next[1].callback();
  }

  assert.equal(now, 100);
  assert.equal(notifications.length, 1, 'idle chart should report after only the quiet window');
});

test('standalone stable-ready keeps the transition veil until strict drawing work is idle', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = false;
  sb.ChartTransition = {
    markReady(identity) { notifications.push(identity); },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.setReady(true);
  fx.cm.handleDataReady(0, 'a:sh.600000|5');

  while (timers.size > 0 && now < 200) {
    const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
    timers.delete(next[0]);
    now = next[1].due;
    next[1].callback();
  }

  assert.equal(notifications.length, 1);
  assert.deepEqual(notifications[0], {
    symbol: 'A:SH.600000',
    interval: '5',
    managerId: 'chart-manager-1',
  });
});

test('stable-ready waits for supplemental history requests and their idle window', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  let historyPending = true;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = false;
  sb.ChartTransition = {
    markReady(identity) { notifications.push(identity); },
  };
  fx.cm.udf_datafeed = {
    _historyProvider: {
      hasPendingHistoryWork(quietMs) {
        assert.equal(quietMs, 120);
        return historyPending;
      },
    },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.setReady(true);
  fx.cm.handleDataReady(0, 'a:sh.600000|5');

  const runUntil = (target) => {
    while (true) {
      const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
      if (!next || next[1].due > target) break;
      timers.delete(next[0]);
      now = next[1].due;
      next[1].callback();
    }
    now = target;
  };
  runUntil(800);
  assert.equal(notifications.length, 0, 'an in-flight older-history page must retain the veil');

  historyPending = false;
  runUntil(1_000);
  assert.equal(notifications.length, 1, 'the veil may clear only after history and drawing quiet periods');
});

test('embedded stable-ready never reveals bars before strict structure is ready', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = true;
  sb.EmbeddedChartBridge = {
    notifyDataReady(identity) { notifications.push(identity); },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.cm._strictStructureStatus = { state: 'waiting', code: null };
  fx.cm._strictStructureSnapshot = null;
  fx.cm._strictStructureContextToken = null;
  fx.setReady(true);
  fx.cm.handleDataReady(0, 'a:sh.600000|5');

  const runUntil = (target) => {
    while (true) {
      const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
      if (!next || next[1].due > target) break;
      timers.delete(next[0]);
      now = next[1].due;
      next[1].callback();
    }
    now = target;
  };
  runUntil(500);
  assert.equal(notifications.length, 0, 'K-lines alone must not remove the parent veil');

  fx.cm._strictStructureStatus = { state: 'ready', code: null };
  fx.cm._strictStructureSnapshot = { render_revision: 'strict-ready' };
  fx.cm._strictStructureContextToken = 'a:sh.600000|5|strict-ready';
  runUntil(700);
  assert.equal(notifications.length, 1);
});

test('embedded stable-ready includes drawing restore viewport and debounced redraw work', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  let now = 0;
  let nextTimerId = 1;
  let debouncePending = true;
  const timers = new Map();
  const notifications = [];
  sb.performance.now = () => now;
  sb.setTimeout = (callback, delay = 0) => {
    const id = nextTimerId++;
    timers.set(id, { callback, due: now + Number(delay || 0) });
    return id;
  };
  sb.clearTimeout = (id) => timers.delete(id);
  sb.__CHANLUN_EMBEDDED_CHART = true;
  sb.EmbeddedChartBridge = {
    notifyDataReady(identity) { notifications.push(identity); },
  };
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.cm._reconcileRetry = { count: 0, timer: null };
  fx.cm._is_switching_interval = true;
  fx.cm._defaultViewApplyPending = true;
  fx.cm.debouncedDrawChanlun.pending = () => debouncePending;
  fx.setReady(true);
  fx.cm.handleDataReady(0, 'a:sh.600000|5');

  const runUntil = (target) => {
    while (true) {
      const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
      if (!next || next[1].due > target) break;
      timers.delete(next[0]);
      now = next[1].due;
      next[1].callback();
    }
    now = target;
  };
  runUntil(400);
  assert.equal(notifications.length, 0);

  fx.cm._is_switching_interval = false;
  fx.cm._defaultViewApplyPending = false;
  debouncePending = false;
  runUntil(600);
  assert.equal(notifications.length, 1);
});

test('initial Chanlun draw waits for the final viewport and then runs exactly once', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.setReady(true);
  fx.cm._defaultViewAttemptTimer = 71;

  fx.cm.handleBarsReadyEvent(barsReadyEvent());

  assert.equal(fx.calls.draw, 0, 'the old viewport must never receive an initial full draw');
  assert.equal(fx.cm._pendingChanlunDrawGeneration, 0);
  assert.equal(fx.cm._initialLoadDone, false);

  fx.cm._defaultViewAttemptTimer = null;
  assert.equal(fx.cm._resumePendingInitialDraw(), true);
  assert.equal(fx.calls.draw, 1);
  assert.equal(fx.calls.debounced, 0);
  assert.equal(fx.cm._pendingChanlunDrawGeneration, null);
});

test('context reset cancels an old embedded stable-ready report', () => {
  const { ChartManager, sb } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  const queued = new Map();
  let timerId = 0;
  let notified = 0;
  sb.__CHANLUN_EMBEDDED_CHART = true;
  sb.EmbeddedChartBridge = { notifyDataReady() { notified += 1; } };
  sb.setTimeout = (callback) => {
    timerId += 1;
    queued.set(timerId, callback);
    return timerId;
  };
  sb.clearTimeout = (id) => queued.delete(id);
  fx.cm._activeDrawingMutations = new Set();
  fx.cm._strictPendingCreates = new Map();
  fx.setReady(true);

  fx.cm.handleDataReady(0, 'a:sh.600000|5');
  fx.cm._resetDataReadyContext();
  for (const callback of queued.values()) callback();

  assert.equal(notified, 0);
});

test('周期切换后旧 dataReady 回调不得绘制到新代际', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  const oldGeneration = fx.cm._dataContextGeneration;
  fx.cm._resetDataReadyContext();
  fx.setReady(true);
  fx.cm.handleDataReady(oldGeneration);
  assert.equal(fx.calls.draw, 0);
  assert.equal(fx.calls.debounced, 0);
});

test('同代际旧标的 dataReady 回调不得通过身份校验', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  const generation = fx.cm._dataContextGeneration;
  fx.setIdentity('A:SH.600001', '5');
  fx.setReady(true);
  fx.cm.handleDataReady(generation, 'a:sh.600000|5');
  assert.equal(fx.calls.draw, 0);
  assert.equal(fx.calls.debounced, 0);
});

test('当前代际已就绪时后续 bars-ready 继续合并为防抖重绘', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  fx.setReady(true);
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  assert.equal(fx.calls.draw, 1, '首次就绪绘制直接执行');
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  assert.equal(fx.calls.draw, 1);
  assert.equal(fx.calls.debounced, 1, '后续更新使用现有防抖入口');
});

test('完全相同的 bars-ready 快照不会延长嵌入图表稳定等待', () => {
  const { ChartManager } = loadChartManager();
  const fx = makeDataReadyManager(ChartManager);
  const snapshot = {
    render_revision: 'render-1',
    snapshot_revision: 'snapshot-1',
    structure_revision: 'structure-1',
    source_closed_at: 1_700_000_300,
  };
  const chartData = {
    from: 1_700_000_000,
    visibleRange: { from: 1_700_000_000, to: 1_700_000_600 },
    barsResult: {
      bars: [{ time: 1_700_000_000_000 }, { time: 1_700_000_300_000 }],
      fxs: [],
      bis: [],
      xds: [],
      strict_structure_mode: 'replace',
      strict_structure: snapshot,
    },
  };
  let cancelled = 0;
  fx.cm.cl_show_config = {};
  fx.cm._strictStructureSnapshot = snapshot;
  fx.cm.getChartData = () => chartData;
  fx.cm.debouncedDrawChanlun.cancel = () => { cancelled += 1; };
  fx.cm._tvDataReadyGeneration = 0;
  fx.cm._tvDataReadyIdentity = 'a:sh.600000|5';
  fx.cm._lastCompletedRenderSignature = fx.cm._chartRenderInputSignature(chartData, '5');
  fx.setReady(true);

  fx.cm.handleBarsReadyEvent(barsReadyEvent());

  assert.equal(fx.calls.debounced, 0);
  assert.equal(cancelled, 1, '重复尾帧应取消同内容的防抖任务');

  chartData.barsResult.xds = [{
    points: [
      { time: 1_700_000_300, price: 1 },
      { time: 1_700_000_600, price: 2 },
    ],
  }];
  fx.cm.handleBarsReadyEvent(barsReadyEvent());
  assert.equal(fx.calls.debounced, 1, '结构输入变化后仍必须重绘');
});

test('绘制输入指纹把 replace 与同一份 unchanged 严格快照视为等价', () => {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  const snapshot = {
    render_revision: 'render-1',
    snapshot_revision: 'snapshot-1',
    structure_revision: 'structure-1',
    source_closed_at: 1_700_000_300,
  };
  cm.cl_show_config = { bi: true, xd: true };
  cm._strictStructureSnapshot = snapshot;
  cm.getCurrentChartIdentity = () => ({ symbol: 'A:SH.600000', interval: '5' });
  const chartData = {
    from: 1_700_000_000,
    visibleRange: { from: 1_700_000_000, to: 1_700_000_600 },
    barsResult: {
      bars: [{ time: 1_700_000_000_000 }, { time: 1_700_000_300_000 }],
      fxs: [{ text: 'ding', points: [{ time: 1_700_000_300, price: 12 }] }],
      bis: [],
      xds: [],
      strict_structure_mode: 'replace',
      strict_structure: snapshot,
    },
  };
  const replaceSignature = cm._chartRenderInputSignature(chartData, '5');
  chartData.barsResult.strict_structure_mode = 'unchanged';
  delete chartData.barsResult.strict_structure;

  assert.equal(cm._chartRenderInputSignature(chartData, '5'), replaceSignature);
  chartData.barsResult.bars.unshift({ time: 1_600_000_000_000 });
  chartData.barsResult.fxs.unshift({
    text: 'di',
    points: [{ time: 1_600_000_000, price: 8 }],
  });
  chartData.barsResult.bis.unshift({
    points: [
      { time: 1_600_000_000, price: 8 },
      { time: 1_600_000_300, price: 9 },
    ],
  });
  assert.equal(
    cm._chartRenderInputSignature(chartData, '5'),
    replaceSignature,
    '仅扩展到可视区外的历史不得触发无效重绘',
  );
  chartData.visibleRange.to += 300;
  assert.notEqual(
    cm._chartRenderInputSignature(chartData, '5'),
    replaceSignature,
    '真实视窗变化不能被去重',
  );
});

test('一次完整绘制会取消已被当前输入覆盖的防抖重绘', async () => {
  const { ChartManager, sb } = loadChartManager();
  sb.setTimeout = (callback) => { callback(); return 1; };
  const cm = Object.create(ChartManager.prototype);
  const chartData = {
    from: 1_700_000_000,
    visibleRange: { from: 1_700_000_000, to: 1_700_000_600 },
    barsResult: {
      bars: [{ time: 1_700_000_000_000 }, { time: 1_700_000_300_000 }],
      fxs: [], bis: [], xds: [], strict_structure_mode: 'unavailable',
    },
  };
  let cancelled = 0;
  cm._disposed = false;
  cm._intervalGeneration = 0;
  cm._intervalSwitchSeq = 0;
  cm._dataContextGeneration = 0;
  cm._tvDataReadyGeneration = 0;
  cm._tvDataReadyIdentity = 'a:sh.600000|5';
  cm._lastCompletedRenderSignature = null;
  cm.cl_show_config = {};
  cm.chart = { dataReady: () => true, getVisibleRange: () => chartData.visibleRange };
  cm.widget = { symbolInterval: () => ({ symbol: 'A:SH.600000', interval: '5' }) };
  cm.getCurrentChartIdentity = () => ({ symbol: 'A:SH.600000', interval: '5' });
  cm.getChartData = () => chartData;
  cm.drawChartElements = () => {};
  cm.markDrawingMutationStart = () => {};
  cm.markDrawingMutationEnd = () => {};
  cm.debouncedDrawChanlun = () => {};
  cm.debouncedDrawChanlun.pending = () => true;
  cm.debouncedDrawChanlun.cancel = () => { cancelled += 1; };

  await cm.draw_chanlun();

  assert.equal(cancelled, 1);
  assert.equal(cm._latestAppliedBarTime, 1_700_000_300_000);
  assert.equal(
    cm._lastCompletedRenderSignature,
    cm._chartRenderInputSignature(chartData, '5'),
  );
});

test('vm 能加载真实 charts.js 并取出 ChartManager', () => {
  const { ChartManager } = loadChartManager();
  assert.ok(ChartManager, 'ChartManager 应被加载');
  assert.equal(typeof ChartManager.prototype._doReset, 'function');
  assert.equal(typeof ChartManager.prototype._getViewLatestSec, 'function');
});

test('中枢矩形的局部样式覆盖不得丢失级别颜色和线宽', () => {
  const { ChartUtils } = loadChartManager();
  const calls = [];
  const chart = {
    createMultipointShape(points, options) {
      calls.push({ points, options });
      return 'center-shape';
    },
  };
  const points = [
    { time: 1_700_000_000, price: 12 },
    { time: 1_700_000_300, price: 10 },
  ];

  const id = ChartUtils.createZhongshuShape(
    chart,
    { points, linestyle: 2 },
    {
      color: '#FF0000',
      linewidth: 3,
      overrides: { transparency: 100, linestyle: 0 },
    },
  );

  assert.equal(id, 'center-shape');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.overrides.color, '#FF0000');
  assert.equal(calls[0].options.overrides.linecolor, '#FF0000');
  assert.equal(calls[0].options.overrides.backgroundColor, '#FF0000');
  assert.equal(calls[0].options.overrides.linewidth, 3);
  assert.equal(calls[0].options.overrides.transparency, 100);
  assert.equal(calls[0].options.overrides.linestyle, 0);
});

test('走势线透明度覆盖不得丢失级别颜色、线宽和完成状态', () => {
  const { ChartUtils } = loadChartManager();
  const calls = [];
  const chart = {
    createMultipointShape(points, options) {
      calls.push({ points, options });
      return 'trend-shape';
    },
  };
  const points = [
    { time: 1_700_000_000, price: 10 },
    { time: 1_700_000_300, price: 12 },
  ];

  const id = ChartUtils.createLineShape(
    chart,
    { points, linestyle: 2 },
    { color: '#0F766E', linewidth: 2, overrides: { transparency: 30 } },
  );

  assert.equal(id, 'trend-shape');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.overrides.linecolor, '#0F766E');
  assert.equal(calls[0].options.overrides.linewidth, 2);
  assert.equal(calls[0].options.overrides.linestyle, 2);
  assert.equal(calls[0].options.overrides.transparency, 30);
});

test('CSP 模式禁用 blob iframe 并使用同源 TradingView 启动页', () => {
  const { disabledFeatures } = loadChartManager();
  assert.ok(Array.isArray(disabledFeatures));
  assert.ok(disabledFeatures.includes('use_blob_for_iframe_loading'));
});

test('embedded 1m keeps the complete default range across responsive resizes', () => {
  const { chartWidgetViewportOptions, sb } = loadChartManager();
  assert.equal(typeof chartWidgetViewportOptions, 'function');

  sb.window.__CHANLUN_EMBEDDED_CHART = true;
  const embeddedOneMinute = chartWidgetViewportOptions('1');
  assert.equal(embeddedOneMinute.minBarSpacing, 0.01);
  assert.ok(
    embeddedOneMinute.enabledFeatures.includes('lock_visible_time_range_on_resize'),
  );

  const embeddedFiveMinute = chartWidgetViewportOptions('5');
  assert.equal(embeddedFiveMinute.minBarSpacing, 0.05);
  assert.ok(
    !embeddedFiveMinute.enabledFeatures.includes('lock_visible_time_range_on_resize'),
  );

  sb.window.__CHANLUN_EMBEDDED_CHART = false;
  const standaloneOneMinute = chartWidgetViewportOptions('1');
  assert.equal(standaloneOneMinute.minBarSpacing, 0.05);
  assert.ok(
    !standaloneOneMinute.enabledFeatures.includes('lock_visible_time_range_on_resize'),
  );
});

test('URL 启动周期覆盖共享 localStorage，避免多 iframe 周期互相污染', () => {
  const { initialChartInterval, shouldLoadLastChart, sb } = loadChartManager();
  assert.equal(typeof initialChartInterval, 'function');
  assert.equal(typeof shouldLoadLastChart, 'function');
  assert.equal(shouldLoadLastChart(), true);
  sb.__chanlunUrlBootstrap = { intervals: ['30'] };
  assert.equal(initialChartInterval('1'), '30');
  assert.equal(initialChartInterval('2'), '5');
  assert.equal(shouldLoadLastChart(), false);
});

test('行情页把 URL 周期保留为当前 iframe 的内存启动配置', () => {
  const template = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'templates', 'index.html'),
    'utf8',
  );
  assert.match(template, /window\.__chanlunUrlBootstrap\s*=/);
  assert.match(template, /intervals:\s*selectedIntervals\.slice\(\)/);
});

test('MACD_HTF 默认启用且通知 URL 可同时请求标准 MACD', () => {
  const { requestedDefaultStudies } = loadChartManager();
  assert.equal(typeof requestedDefaultStudies, 'function');
  assert.deepEqual(Array.from(requestedDefaultStudies('')), ['MACD_HTF']);
  assert.deepEqual(Array.from(requestedDefaultStudies('?default_study=unknown')), ['MACD_HTF']);
  assert.deepEqual(
    Array.from(requestedDefaultStudies('?default_study=MACD&default_study=MACD_HTF')),
    ['MACD_HTF', 'MACD'],
  );
  assert.deepEqual(
    Array.from(requestedDefaultStudies(
      '?default_study=MACD_HTF&default_study=unknown&default_study=MACD_HTF',
    )),
    ['MACD_HTF'],
  );
});

test('ensureRequestedDefaultStudies 复用已有 MACD_HTF，不重复创建', async () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  let createCalls = 0;
  sb.location.search = '?default_study=MACD_HTF';
  cm.chart = {
    getAllStudies: () => [{ id: 'existing-macd', name: 'MACD_HTF' }],
    createStudy: async () => { createCalls++; return 'unexpected'; },
  };

  const ids = await cm.ensureRequestedDefaultStudies();

  assert.deepEqual(Array.from(ids), ['existing-macd']);
  assert.equal(createCalls, 0);
  assert.equal(cm.macdStudyId, 'existing-macd');
});

test('ensureRequestedDefaultStudies 缺失时只创建一次并共享初始化 Promise', async () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  const calls = [];
  sb.location.search = '?default_study=MACD_HTF';
  cm.chart = {
    getAllStudies: () => [],
    createStudy: async (...args) => { calls.push(args); return 'created-macd'; },
  };

  const first = cm.ensureRequestedDefaultStudies();
  const second = cm.ensureRequestedDefaultStudies();
  assert.equal(first, second, '同一实例只运行一轮默认指标初始化');
  assert.deepEqual(Array.from(await first), ['created-macd']);
  assert.deepEqual(calls, [['MACD_HTF', false, false]]);
  assert.equal(cm.macdStudyId, 'created-macd');
});

test('ensureRequestedDefaultStudies 创建失败后允许下一次 data-ready 重试', async () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  let calls = 0;
  let warnings = 0;
  sb.location.search = '?default_study=MACD_HTF';
  sb.console = { ...console, warn: () => { warnings++; } };
  cm.chart = {
    getAllStudies: () => [],
    createStudy: async () => {
      calls++;
      if (calls === 1) throw new Error('study unavailable');
      return 'retry-created-macd';
    },
  };

  assert.deepEqual(Array.from(await cm.ensureRequestedDefaultStudies()), []);
  assert.deepEqual(Array.from(await cm.ensureRequestedDefaultStudies()), ['retry-created-macd']);
  assert.equal(calls, 2);
  assert.equal(warnings, 1);
});

test('两张图的 MACD_HTF pending guard 按实例隔离', async () => {
  const { ChartManager } = loadChartManager();
  const first = makeManager(ChartManager, null);
  const second = makeManager(ChartManager, null);
  let finishFirst;
  const calls = [];
  first.chart = {
    getAllStudies: () => [],
    createStudy: () => new Promise((resolve) => {
      calls.push('first');
      finishFirst = resolve;
    }),
  };
  second.chart = {
    getAllStudies: () => [],
    createStudy: async () => { calls.push('second'); return 'second-macd'; },
  };

  const pendingFirst = first.ensureRequestedDefaultStudies();
  assert.deepEqual(Array.from(await second.ensureRequestedDefaultStudies()), ['second-macd']);
  assert.deepEqual(calls, ['first', 'second']);
  finishFirst('first-macd');
  assert.deepEqual(Array.from(await pendingFirst), ['first-macd']);
});

test('chart-ready 接线会触发默认指标初始化但不阻塞后续流程', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  assert.match(
    source,
    /this\.chart\s*=\s*this\.widget\.activeChart\(\);[\s\S]*this\.ensureRequestedDefaultStudies\(\)\.catch\(/,
  );
});

test('_getViewLatestSec: 直接命中键 → 末根秒数(ms 归一)', () => {
  const { ChartManager } = loadChartManager();
  const map = new Map();
  map.set('a:sh.0000015', { bars: [{ time: 1000000 }, { time: 1300000 }] });
  const cm = makeManager(ChartManager, null, map);
  assert.equal(cm._getViewLatestSec('a:sh.0000015', '5'), 1300);
});

test('_getViewLatestSec: M-1 前缀回退(写键无 market 前缀, 读键带前缀)', () => {
  const { ChartManager } = loadChartManager();
  const map = new Map();
  // 轮询用 ticker 写键(此处模拟成无前缀), SSE 读键带 "a:" 前缀 → 须剥前缀回退命中
  map.set('sh.0000015', { bars: [{ time: 2000000 }, { time: 2300000 }] });
  const cm = makeManager(ChartManager, null, map);
  assert.equal(cm._getViewLatestSec('a:sh.0000015', '5'), 2300);
});

test('_getViewLatestSec: 无命中 → null(安全退化)', () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null, new Map());
  assert.equal(cm._getViewLatestSec('a:none5', '5'), null);
});

test('_maybeWidenDefaultView: 旧视窗完全落在已加载历史外时回到最新行情', () => {
  const { ChartManager, sb } = loadChartManager();
  const latest = 1_784_691_000;
  const earliest = latest - 86_400;
  const bars = [
    { time: earliest * 1000 },
    { time: latest * 1000 },
  ];
  const map = new Map([['a:sh.5131005', { bars }]]);
  const cm = makeManager(ChartManager, null, map);
  const applied = [];
  cm._viewSetFor = null;
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SH.513100', interval: '5' }),
  };
  cm.chart = {
    getVisibleRange: () => ({
      from: earliest - 31 * 86_400,
      to: earliest - 30 * 86_400,
    }),
    setVisibleRange: (range) => { applied.push(range); },
  };
  sb.setTimeout = (callback) => { callback(); return 0; };

  cm._maybeWidenDefaultView();

  assert.equal(applied.length, 1);
  assert.deepEqual(applied[0], { from: earliest, to: latest });
});

test('_maybeWidenDefaultView: 5 分钟正式走势、30 分钟与日线使用扩展跨度', () => {
  const { ChartManager, sb } = loadChartManager();
  const latest = 1_784_691_000;
  const day = 86_400;
  sb.setTimeout = (callback) => { callback(); return 0; };

  for (const { interval, spanDays } of [
    { interval: '5', spanDays: 90 },
    { interval: '30', spanDays: 90 },
    { interval: '1D', spanDays: 800 },
  ]) {
    const symbol = 'a:SH.513100';
    const resultKey = symbol.toLowerCase() + interval.toLowerCase();
    const bars = [
      { time: (latest - 2_000 * day) * 1000 },
      { time: latest * 1000 },
    ];
    const cm = makeManager(
      ChartManager,
      null,
      new Map([[resultKey, { bars }]]),
    );
    const applied = [];
    cm._viewSetFor = null;
    cm.widget = {
      symbolInterval: () => ({ symbol, interval }),
    };
    cm.chart = {
      getVisibleRange: () => ({ from: latest - 10 * day, to: latest }),
      setVisibleRange: (range) => { applied.push(range); },
    };

    cm._maybeWidenDefaultView();

    assert.deepEqual(applied, [{ from: latest - spanDays * day, to: latest }]);
  }
});

test('_maybeWidenDefaultView: 选股嵌入图的 1m 展示全部已加载数据且独立图仍保持两日', () => {
  const { ChartManager, sb } = loadChartManager();
  const latest = 1_784_691_000;
  const day = 86_400;
  const symbol = 'a:SH.513100';
  const bars = [
    { time: (latest - 30 * day) * 1000 },
    { time: latest * 1000 },
  ];
  sb.setTimeout = (callback) => { callback(); return 0; };

  for (const { embedded, expectedRange } of [
    {
      embedded: true,
      expectedRange: { from: latest - 30 * day, to: latest },
    },
    {
      embedded: false,
      expectedRange: { from: latest - 2 * day, to: latest },
    },
  ]) {
    sb.window.__CHANLUN_EMBEDDED_CHART = embedded;
    const cm = makeManager(
      ChartManager,
      null,
      new Map([[`${symbol.toLowerCase()}1`, { bars }]]),
    );
    const applied = [];
    cm._viewSetFor = null;
    cm.widget = {
      symbolInterval: () => ({ symbol, interval: '1' }),
    };
    cm.chart = {
      getVisibleRange: () => ({ from: latest - day, to: latest }),
      setVisibleRange: (range) => { applied.push(range); },
    };

    cm._maybeWidenDefaultView();

    assert.deepEqual(applied, [expectedRange]);
  }
});

test('_maybeApplyCausalFocus: 选股嵌入图的 1m 审计视窗同样覆盖五日', () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null, new Map());
  const focusAt = 1_735_891_200;
  const applied = [];
  sb.window.__CHANLUN_EMBEDDED_CHART = true;
  sb.window.__chanlunReviewChartLock = {
    candidate_id: `sha256:${'1'.repeat(64)}`,
    source_sha256: `sha256:${'2'.repeat(64)}`,
    review_as_of: focusAt + 10 * 86_400,
    focus_at: focusAt,
    symbol: 'SH.000001',
    chart_interval: '1',
    lock_kind: 'RISK_POINT_AUDIT',
  };
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SH.000001', interval: '1' }),
  };
  cm.chart = {
    setVisibleRange: (range) => { applied.push(range); },
    createMultipointShape: () => 'audit-marker-1m',
  };
  sb.setTimeout = (callback) => { callback(); return 0; };

  assert.equal(cm._maybeApplyCausalFocus(), true);
  assert.equal(applied.length, 1);
  assert.equal(applied[0].to - applied[0].from, 5 * 86_400);
});

test('_maybeWidenDefaultView: TradingView 切标的瞬态拒绝会被消费并重试', async () => {
  const { ChartManager, sb } = loadChartManager();
  const latest = 1_784_691_000;
  const earliest = latest - 120 * 86_400;
  const symbol = 'a:SH.600203';
  const callbacks = [];
  const applied = [];
  let attempts = 0;
  sb.setTimeout = (callback) => {
    callbacks.push(callback);
    return callbacks.length;
  };
  const cm = makeManager(
    ChartManager,
    null,
    new Map([[`${symbol.toLowerCase()}5`, {
      bars: [{ time: earliest * 1000 }, { time: latest * 1000 }],
    }]]),
  );
  cm._dataContextGeneration = 7;
  cm._viewSetFor = null;
  cm._viewSetGeneration = null;
  cm.widget = {
    symbolInterval: () => ({ symbol, interval: '5' }),
  };
  cm.chart = {
    getVisibleRange: () => ({ from: latest - 86_400, to: latest }),
    setVisibleRange: (range) => {
      attempts += 1;
      applied.push(range);
      return attempts === 1
        ? Promise.reject(new Error('Value is null'))
        : Promise.resolve();
    },
  };

  cm._maybeWidenDefaultView();
  callbacks.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(callbacks.length, 1, '拒绝后应安排一次有界重试');
  callbacks.shift()();
  assert.equal(callbacks.length, 1, '重试先重新核对最新图表上下文');
  callbacks.shift()();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(attempts, 2);
  assert.deepEqual(applied[0], applied[1]);
  assert.equal(cm._viewSetFor, `${symbol}|5`);
  assert.equal(cm._viewSetGeneration, 7);
});

test('_maybeApplyCausalFocus: 风险点审计锁聚焦锚点且不越过因果截止', () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null, new Map());
  const focusAt = 1_735_891_200;
  const reviewAsOf = focusAt + 10 * 86_400;
  const applied = [];
  const markers = [];
  sb.window.__chanlunReviewChartLock = {
    candidate_id: `sha256:${'1'.repeat(64)}`,
    source_sha256: `sha256:${'2'.repeat(64)}`,
    review_as_of: reviewAsOf,
    focus_at: focusAt,
    symbol: 'SH.000001',
    chart_interval: '30',
    lock_kind: 'RISK_POINT_AUDIT',
  };
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SH.000001', interval: '30' }),
  };
  cm.chart = {
    setVisibleRange: (range) => { applied.push(range); },
    createMultipointShape: (points, options) => {
      markers.push({ points, options });
      return 'audit-marker-1';
    },
  };
  sb.setTimeout = (callback) => { callback(); return 0; };

  assert.equal(cm._maybeApplyCausalFocus(), true);
  assert.equal(applied.length, 1);
  assert.ok(applied[0].from < focusAt);
  assert.ok(applied[0].to > focusAt);
  assert.ok(applied[0].to <= reviewAsOf);
  assert.equal(markers.length, 1);
  assert.deepEqual(markers[0].points, [{ time: focusAt }]);
  assert.equal(Object.hasOwn(markers[0].points[0], 'price'), false);
  assert.equal(markers[0].options.shape, 'vertical_line');
  assert.match(markers[0].options.text, /无价格锚点/);
  assert.equal(cm._maybeApplyCausalFocus(), true);
  assert.equal(applied.length, 1, '同一稳定点只聚焦一次');
  assert.equal(markers.length, 1, '同一稳定点只创建一条审计竖线');
});

test('_maybeApplyCausalFocus: QMT GICS3 复合代码保留内部冒号并可定位', () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null, new Map());
  const focusAt = 1_735_891_200;
  const sector = `qmt-gics3:${'a'.repeat(64)}`;
  sb.window.__chanlunReviewChartLock = {
    candidate_id: `sha256:${'1'.repeat(64)}`,
    source_sha256: `sha256:${'2'.repeat(64)}`,
    review_as_of: focusAt + 86_400,
    focus_at: focusAt,
    symbol: sector,
    chart_interval: '30',
    lock_kind: 'RISK_POINT_AUDIT',
  };
  cm.widget = {
    symbolInterval: () => ({ symbol: `a:${sector}`, interval: '30' }),
  };
  let markerPoint = null;
  cm.chart = {
    setVisibleRange: () => {},
    createMultipointShape: (points) => {
      markerPoint = points[0];
      return 'sector-marker';
    },
  };
  sb.setTimeout = (callback) => { callback(); return 0; };

  assert.equal(cm._maybeApplyCausalFocus(), true);
  assert.deepEqual(markerPoint, { time: focusAt });
});

test('_maybeApplyCausalFocus: 标的或周期与风险点锁不一致时不得聚焦', () => {
  const { ChartManager, sb } = loadChartManager();
  const cm = makeManager(ChartManager, null, new Map());
  sb.window.__chanlunReviewChartLock = {
    candidate_id: `sha256:${'1'.repeat(64)}`,
    source_sha256: `sha256:${'2'.repeat(64)}`,
    review_as_of: 1_735_891_200,
    focus_at: 1_735_804_800,
    symbol: 'SH.000001',
    chart_interval: '30',
    lock_kind: 'RISK_POINT_AUDIT',
  };
  cm.chart = { setVisibleRange: () => assert.fail('不应聚焦不匹配图表') };
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SH.000002', interval: '30' }),
  };
  assert.equal(cm._maybeApplyCausalFocus(), false);
  cm.widget = {
    symbolInterval: () => ({ symbol: 'a:SH.000001', interval: '5' }),
  };
  assert.equal(cm._maybeApplyCausalFocus(), false);
});

test('_doReset: 首次 → 调 resetCache+resetData, 记账 backoff=0', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, true);
  assert.equal(calls.resetCache, 1, 'resetCache 先调(契约)');
  assert.equal(calls.resetData, 1);
  assert.equal(cm._resetState['k'].backoffLevel, 0);
  assert.equal(cm._resetState['k'].lastResetSec, 10000);
});

test('_doReset: 防抖窗口内第二次 → 拒绝, 不再调 resetData(G4)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  cm._doReset('k', 'gap-detect', 5000);          // 首次 reset
  sb._setNowSec(10010);                          // +10s, < 30s 防抖
  const did = cm._doReset('k', 'gap-detect', 5000);
  assert.equal(did, false);
  assert.equal(calls.resetData, 1, '防抖期内不重复 reset');
});

test('_doReset: 防抖过 + 画布没前进(无效) → 退避 +1(M-2)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  cm._doReset('k', 'watchdog', 5000);            // 首次, backoff=0, view=5000
  sb._setNowSec(10031);                          // +31s > 30s 放行
  cm._doReset('k', 'watchdog', 5000);            // view 仍 5000(没前进) → 上次无效
  assert.equal(calls.resetData, 2);
  assert.equal(cm._resetState['k'].backoffLevel, 1, '无效 reset → 退避 +1');
});

test('_doReset: 退避放大间隔 → 被拒不消耗退避(M-2 关键)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  cm._doReset('k', 'watchdog', 5000);   // reset#1 backoff=0
  sb._setNowSec(10031);
  cm._doReset('k', 'watchdog', 5000);   // reset#2 无效→backoff=1, 间隔变 60s
  assert.equal(cm._resetState['k'].backoffLevel, 1);
  sb._setNowSec(10061);                 // 距 reset#2 仅 30s < 60s → 被拒
  const did = cm._doReset('k', 'watchdog', 5000);
  assert.equal(did, false);
  assert.equal(calls.resetData, 2, '退避间隔内被拒');
  assert.equal(cm._resetState['k'].backoffLevel, 1, '被拒不抬退避(仍 1)');
});

test('_doReset: 画布前进(上次生效) → 退避归零', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  cm._doReset('k', 'watchdog', 5000);   // backoff=0
  sb._setNowSec(10031);
  cm._doReset('k', 'watchdog', 5000);   // 无效 → backoff=1
  sb._setNowSec(10100);                 // > 60s 放行
  cm._doReset('k', 'watchdog', 6000);   // view 6000>5000 前进 → 上次生效
  assert.equal(cm._resetState['k'].backoffLevel, 0, '画布前进 → 退避归零');
});

test('_doReset: widget 缺少现行 resetCache 契约时拒绝 reset', () => {
  const { ChartManager, sb } = loadChartManager();
  let resetDataCalls = 0;
  const widget = { activeChart: () => ({ resetData: () => { resetDataCalls++; } }) };
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, false);
  assert.equal(resetDataCalls, 0, '契约不完整时不得执行 resetData');
});

test('_doReset: 无 activeChart → 不 reset 且不污染记账', () => {
  const { ChartManager, sb } = loadChartManager();
  const widget = { resetCache: () => {}, activeChart: () => null };
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, false);
  assert.equal(cm._resetState['k'], undefined, '未 reset 不应写记账');
});

// ── onmessage 编排：真实 _openSseStream 注册的 'chanlun' 回调驱动 ──────────
// resKey = 'a:SH.000001'(→'a:sh.000001') + '5' = 'a:sh.0000015'
const RES_KEY = 'a:sh.0000015';

test('embedded symbol switches release SSE connection slots until the parent batch resumes', () => {
  const { ChartManager, sb } = loadChartManager();
  sb.window.__CHANLUN_EMBEDDED_CHART = true;
  const widget = {
    symbolInterval: () => ({ symbol: 'a:SH.000001', interval: '5' }),
  };
  const cm = makeManager(ChartManager, widget, new Map());
  let closeCalls = 0;
  cm._disposed = false;
  cm._embeddedChartActive = true;
  cm._embeddedRealtimeDeferredRequestId = null;
  cm._embeddedRealtimeFallbackTimer = null;
  cm._sse = { close() { closeCalls += 1; } };
  cm._sseConnectionId = null;
  cm._sseChannelId = 'test-channel';
  cm._sseHealthTimer = null;
  cm._sseFallbackInterval = null;
  delete sb.__lastES;

  assert.equal(cm.deferEmbeddedRealtime('chart-41'), true);
  assert.equal(closeCalls, 1);
  assert.equal(cm._embeddedRealtimeDeferredRequestId, 'chart-41');
  cm._openSseStream();
  assert.equal(sb.__lastES, undefined, 'deferred switch must not reopen EventSource');

  assert.equal(cm.resumeEmbeddedRealtime('chart-40'), false);
  assert.equal(sb.__lastES, undefined, 'stale parent request must not resume realtime');
  assert.equal(cm.resumeEmbeddedRealtime('chart-41'), true);
  assert.ok(sb.__lastES, 'matching batch completion resumes the new symbol stream');
  assert.equal(cm._embeddedRealtimeDeferredRequestId, null);
});

test('standalone charts never enter the embedded realtime defer protocol', () => {
  const { ChartManager, sb } = loadChartManager();
  sb.window.__CHANLUN_EMBEDDED_CHART = false;
  const cm = makeManager(ChartManager, null, new Map());
  assert.equal(cm.deferEmbeddedRealtime('chart-1'), false);
  assert.equal(cm.resumeEmbeddedRealtime('chart-1'), false);
});

function openStream(cm, sb) {
  cm._sse = null; cm._sseGotData = false; cm._sseHealthTimer = null; cm._sseFallbackInterval = null;
  cm.widget.symbolInterval = () => ({ symbol: 'a:SH.000001', interval: '5' });
  const applyCalls = [], feedCalls = [];
  cm.udf_datafeed._historyProvider.applyChanlunUpdate = (data) => { applyCalls.push(data); };
  cm.udf_datafeed.feedRealtimeBar = (...args) => { feedCalls.push(args); };
  cm._openSseStream();
  const es = sb.__lastES;
  const fire = (obj) => es._listeners['chanlun']({ data: JSON.stringify(obj) });
  return { es, fire, applyCalls, feedCalls };
}

test('onmessage: 正常帧(无断网/无 gap) → 走 apply+feed, 不 reset', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] }); // 画布末根=1000s
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire, applyCalls, feedCalls } = openStream(cm, sb);
  fire({ s: 'ok', t: [700, 1000], c: [1, 1] }); // 领先 0/1 根
  assert.equal(calls.resetData, 0, '无 gap 不 reset');
  assert.equal(applyCalls.length, 1, '走 applyChanlunUpdate');
  assert.equal(feedCalls.length, 1, '走 feedRealtimeBar');
});

test('onmessage: reconnect-flag 置位 → 首帧无条件 reset 并 return(不走 apply)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] });
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire, applyCalls } = openStream(cm, sb);
  cm._needResetOnNextData = true;
  fire({ s: 'ok', t: [1000, 1300, 1600], c: [1, 1, 1] });
  assert.equal(calls.resetData, 1, 'reconnect → reset');
  assert.equal(cm._needResetOnNextData, false, 'flag 被消费');
  assert.equal(applyCalls.length, 0, 'reconnect 帧 return, 不走增量');
});

test('onmessage: 断流 ≥30s → 置 flag → reset(M-3 真断档)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] });
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire } = openStream(cm, sb);
  cm._disconnectedSinceMs = 10000 * 1000 - 35000; // 断开 35s
  fire({ s: 'ok', t: [1000, 1300], c: [1, 1] });
  assert.equal(calls.resetData, 1, '断流≥30s 视为真断档 → reset');
  assert.equal(cm._disconnectedSinceMs, null, '断流时刻已消费清空');
});

test('onmessage: 瞬断 <30s → 不置 flag → 不 reset(M-3 防弱网闪)', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] });
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire, applyCalls } = openStream(cm, sb);
  cm._disconnectedSinceMs = 10000 * 1000 - 5000; // 断开仅 5s
  fire({ s: 'ok', t: [700, 1000], c: [1, 1] });   // 无 gap
  assert.equal(calls.resetData, 0, '瞬断不 reset');
  assert.equal(applyCalls.length, 1, '正常走增量');
  assert.equal(cm._disconnectedSinceMs, null, '断流时刻仍被清空');
});

test('onmessage: 断档帧(SSE 领先多根) → gap-detect reset', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] }); // 画布停在 1000s
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire, applyCalls } = openStream(cm, sb);
  const t = []; for (let i = 0; i <= 10; i++) t.push(1000 + i * 300); // 1000→4000, 缺 9 根
  fire({ s: 'ok', t, c: t.map(() => 1) });
  assert.equal(calls.resetData, 1, 'gap 断档 → reset');
  assert.equal(applyCalls.length, 0, 'gap 帧 return');
});

test('onmessage: authoritative SSE snapshot replays a small gap without reset', () => {
  const { ChartManager, sb } = loadChartManager();
  const { widget, calls } = spyWidget();
  const map = new Map(); map.set(RES_KEY, { bars: [{ time: 1000000 }] });
  const cm = makeManager(ChartManager, widget, map);
  sb._setNowSec(10000);
  const { fire, applyCalls, feedCalls } = openStream(cm, sb);
  const t = []; for (let i = 0; i <= 6; i++) t.push(1000 + i * 300);

  fire({
    s: 'ok', full_snapshot: true, t,
    o: t.map(() => 1), h: t.map(() => 1), l: t.map(() => 1),
    c: t.map(() => 1), v: t.map(() => 1),
  });

  assert.equal(calls.resetData, 0);
  assert.equal(applyCalls.length, 1);
  assert.equal(feedCalls.length, 1);
  assert.equal(feedCalls[0][3], 1000);
});

// ── H1 force_refresh(阶段E): _doReset 置一次性标志,datafeed 下次 firstDataRequest 绕过缓存重算 ──
test('_doReset: 置一次性 force_refresh 标志(H1) 且 resetData 仍同步(不破坏时序)', () => {
  const { ChartManager } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  cm.udf_datafeed = { _historyProvider: { bars_result: new Map() } };
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, 1);
  assert.equal(calls.resetData, 1, 'resetData 仍同步调用(时序不变,不破坏现有 _doReset 测试)');
  assert.equal(cm.udf_datafeed._historyProvider._forceRefreshOnce, true, '置一次性 force_refresh 标志');
});

test('_doReset: 无 udf_datafeed 时置标志不抛错(容错)', () => {
  const { ChartManager } = loadChartManager();
  const { widget, calls } = spyWidget();
  const cm = makeManager(ChartManager, widget);
  cm.udf_datafeed = null;   // 极早期 reset,datafeed 未挂
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, 1, '仍正常 reset');
  assert.equal(calls.resetData, 1);
});


// Finding 1(审计 MED): chart-ready 后按真实周期校正 _curResolution。构造函数的
// _curResolution 是猜测(localStorage 或 '1'),可能与 TV 实际显示周期(load_last_chart
// 存档 / TV 默认日线)不符;chart 就绪后须以 chart.resolution() 为准,否则首次加载 toggle
// 会把显示配置存到错误周期 key。
test('_alignResolutionOnReady: resolution() 与 _curResolution 不符 → 校正到真实周期', () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  cm.id = '1';
  cm._curResolution = '1';               // 构造猜测值
  cm.cl_show_config = { fx: true, bi: true };
  cm.chart = { resolution: () => 'D' };   // TV 实际显示日线
  let appliedRes = null;
  cm._applyResolutionConfig = (r) => { appliedRes = r; cm._curResolution = r; };
  cm._alignResolutionOnReady();
  assert.equal(appliedRes, 'D', '应按真实周期 D 校正');
  assert.equal(cm._curResolution, 'D');
});

test('_alignResolutionOnReady: resolution() 与 _curResolution 相同 → 不重入', () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  cm._curResolution = '5';
  cm.chart = { resolution: () => '5' };
  let called = 0;
  cm._applyResolutionConfig = () => { called++; };
  cm._alignResolutionOnReady();
  assert.equal(called, 0, '同周期不应重入(避免无谓覆盖)');
});

test('_alignResolutionOnReady: resolution() 抛错 → 吞掉不影响首绘(防御)', () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  cm._curResolution = '1';
  cm.chart = { resolution: () => { throw new Error('boom'); } };
  let called = 0;
  cm._applyResolutionConfig = () => { called++; };
  assert.doesNotThrow(() => cm._alignResolutionOnReady());
  assert.equal(called, 0);
});
test('drawing saves are serialized and pending calls collapse to the latest state', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null);
  const started = [];
  let finishFirst;
  let finishLatest;

  const first = cm.enqueueLatestDrawingSave(() => new Promise((resolve) => {
    started.push('first');
    finishFirst = resolve;
  }));
  const superseded = cm.enqueueLatestDrawingSave(() => {
    started.push('superseded');
    return Promise.resolve();
  });
  const latest = cm.enqueueLatestDrawingSave(() => new Promise((resolve) => {
    started.push('latest');
    finishLatest = resolve;
  }));

  assert.deepEqual(started, ['first']);
  finishFirst();
  await first;
  assert.deepEqual(started, ['first', 'latest']);
  finishLatest();
  await Promise.all([superseded, latest]);
});

test('drawing save failures reject the adapter promise', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeManager(ChartManager, null);

  await assert.rejects(
    cm.enqueueLatestDrawingSave(() => Promise.reject(new Error('write failed'))),
    /write failed/,
  );
});

test('concurrent drawing loads share one request but a later reload can refetch', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const key = '["undefined","1","a:SH.513100","all"]';
  let requests = 0;
  let finishFirst;
  const first = cm.loadDrawingStateOnce(key, () => new Promise((resolve) => {
    requests++;
    finishFirst = resolve;
  }));
  const concurrent = cm.loadDrawingStateOnce(key, async () => {
    requests++;
    return { stale: true };
  });

  assert.equal(first, concurrent, '同一存储键的并发读取应共享同一个 Promise');
  assert.equal(requests, 0, '任务通过微任务启动，便于同一轮调用先完成合并');
  await Promise.resolve();
  assert.equal(requests, 1);
  finishFirst({ status: 'ok' });
  assert.deepEqual(await concurrent, { status: 'ok' });

  await cm.loadDrawingStateOnce(key, async () => {
    requests++;
    return { status: 'new' };
  });
  assert.equal(requests, 2, '完成后的明确重载不能被永久缓存挡住');
});

test('drawing persistence fingerprints ignore object key order and isolate storage records', () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const first = {
    schema: 'chanlun-user-drawings',
    sources: {
      lineB: { state: { linewidth: 4, linecolor: '#0F766E' }, points: [{ time_t: 2, price: 3 }] },
      lineA: { points: [{ price: 1, time_t: 1 }], state: { linecolor: '#0F766E', linewidth: 4 } },
    },
    groups: {},
  };
  const reordered = {
    groups: {},
    sources: {
      lineA: { state: { linewidth: 4, linecolor: '#0F766E' }, points: [{ time_t: 1, price: 1 }] },
      lineB: { points: [{ price: 3, time_t: 2 }], state: { linecolor: '#0F766E', linewidth: 4 } },
    },
    schema: 'chanlun-user-drawings',
  };

  assert.equal(
    cm.drawingStateFingerprint(first),
    cm.drawingStateFingerprint(reordered),
    '服务端排序后的 JSON 与浏览器原始键序应视为同一画线状态',
  );
  assert.notEqual(
    cm.getDrawingPersistenceKey('default', 'default', 'A:SH.513100', 'all'),
    cm.getDrawingPersistenceKey('undefined', '1', 'a:SH.513100', 'all'),
    '不同 TradingView 布局记录不能共享持久化指纹',
  );
});

test('unchanged drawing state skips writes while changed state persists once', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const key = cm.getDrawingPersistenceKey('default', 'default', 'A:SH.513100', 'all');
  const baseline = {
    schema: 'chanlun-user-drawings',
    sources: { line1: { points: [{ time_t: 1, price: 1 }], state: { linewidth: 4 } } },
    groups: {},
  };
  const sameWithDifferentKeyOrder = {
    groups: {},
    sources: { line1: { state: { linewidth: 4 }, points: [{ price: 1, time_t: 1 }] } },
    schema: 'chanlun-user-drawings',
  };
  const changed = {
    schema: 'chanlun-user-drawings',
    sources: { line1: { points: [{ time_t: 1, price: 2 }], state: { linewidth: 4 } } },
    groups: {},
  };
  let writes = 0;
  cm.rememberPersistedDrawingState(key, baseline);

  await cm.enqueueDrawingStateSave(key, sameWithDifferentKeyOrder, async () => { writes++; });
  assert.equal(writes, 0, '初始化 auto-save 不应把刚读取的相同状态再写一次');

  await cm.enqueueDrawingStateSave(key, changed, async () => { writes++; });
  await cm.enqueueDrawingStateSave(key, changed, async () => { writes++; });
  assert.equal(writes, 1, '真实变化应写入一次，成功后的重复 auto-save 应跳过');
});

test('failed drawing write keeps the previous fingerprint so the same change can retry', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const key = cm.getDrawingPersistenceKey('default', 'default', 'A:SH.513100', 'all');
  const baseline = { schema: 'chanlun-user-drawings', sources: {}, groups: {} };
  const changed = {
    schema: 'chanlun-user-drawings',
    sources: { line1: { points: [{ time_t: 1, price: 2 }] } },
    groups: {},
  };
  let attempts = 0;
  cm.rememberPersistedDrawingState(key, baseline);

  await assert.rejects(
    cm.enqueueDrawingStateSave(key, changed, async () => {
      attempts++;
      throw new Error('write failed');
    }),
    /write failed/,
  );
  await cm.enqueueDrawingStateSave(key, changed, async () => { attempts++; });

  assert.equal(attempts, 2, '失败写入不能伪装成已持久化并阻断重试');
});

test('queued revert is compared after the in-flight write and is not dropped', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const key = cm.getDrawingPersistenceKey('default', 'default', 'A:SH.513100', 'all');
  const baseline = { schema: 'chanlun-user-drawings', sources: {}, groups: {} };
  const changed = {
    schema: 'chanlun-user-drawings',
    sources: { line1: { points: [{ time_t: 1, price: 2 }] } },
    groups: {},
  };
  const writes = [];
  let finishChanged;
  cm.rememberPersistedDrawingState(key, baseline);

  const first = cm.enqueueDrawingStateSave(key, changed, () => new Promise((resolve) => {
    writes.push('changed');
    finishChanged = resolve;
  }));
  const reverted = cm.enqueueDrawingStateSave(key, baseline, async () => { writes.push('reverted'); });

  assert.deepEqual(writes, ['changed']);
  finishChanged();
  await Promise.all([first, reverted]);
  assert.deepEqual(writes, ['changed', 'reverted']);
});

test('automatic save without a loaded baseline cannot overwrite unknown server state', async () => {
  const { ChartManager } = loadChartManager();
  const cm = makeDrawingPersistenceManager(ChartManager);
  const key = cm.getDrawingPersistenceKey('default', 'default', 'A:SH.513100', 'all');
  const state = { schema: 'chanlun-user-drawings', sources: {}, groups: {} };
  let writes = 0;

  await cm.enqueueDrawingStateSave(
    key,
    state,
    async () => { writes++; },
    { requireKnownBaseline: true },
  );

  assert.equal(writes, 0, '读取失败或尚未完成时，初始化 auto-save 不得删除服务端旧画线');
});
