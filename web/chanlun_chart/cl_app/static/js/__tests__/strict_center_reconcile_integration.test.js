'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const Reconcile = require('../chart_structure_reconcile.js');

const BASE = 1700000000;
const DAILY_BAR_AT = 1784649600;
const DAILY_CLOSE_AT = 1784703600;

function loadChartManager(runtimeOverrides = {}) {
  const sandbox = {
    console, Math, JSON, Intl, Array, Object, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isFinite, isNaN, Set, Map, WeakMap, WeakSet, Symbol,
    Promise, Error, URLSearchParams,
    Date,
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: (callback) => { callback(); return 0; },
    clearTimeout() {},
    performance: { now: () => 0 },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    CustomEvent: function CustomEvent(type, detail) { this.type = type; this.detail = detail; },
    EventSource: function EventSource() {},
    Utils: { get_market: () => 'a', get_local_data: () => '5' },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map(), activeManagerId: null }),
    Datafeeds: { UDFCompatibleDatafeed: function UDFCompatibleDatafeed() {} },
    TradingView: { widget: function widget() {} },
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    navigator: { onLine: true },
    location: { search: '', reload() {}, assign() {} },
    ChartStructureReconcile: Reconcile,
    ...runtimeOverrides,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    createElement: () => ({
      style: {}, dataset: {}, classList: { add() {}, remove() {} },
      appendChild() {}, addEventListener() {}, setAttribute() {}, remove() {},
    }),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { appendChild() {} },
  };
  vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  source += '\n;globalThis.__STRICT_CM = ChartManager;';
  vm.runInContext(source, sandbox, { filename: 'charts.js' });
  return { ChartManager: sandbox.__STRICT_CM, sandbox };
}

function center(revision = 1, overrides = {}) {
  return {
    schema: 'chanlun-chart-center/v5',
    render_kind: 'formal_center',
    center_id: 'center-1',
    render_id: `center-1@${revision}@ongoing`,
    body_revision: revision,
    structural_level: 0,
    source_kind: 'segment',
    state: 'ongoing',
    tradable: true,
    points: [
      { time: BASE + 100, price: 11 },
      { time: BASE + 500, price: 10 },
    ],
    entry_unit_id: 'u1',
    core_unit_ids: ['u2', 'u3', 'u4'],
    initial_exit_unit_id: 'u5',
    initial_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    body_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    extension_unit_ids: [],
    pending_leave_unit_id: null,
    completion_leave_unit_id: null,
    completion_return_unit_id: null,
    completion_direction: null,
    completed_at: null,
    ...overrides,
  };
}

function centerPreview(overrides = {}) {
  return {
    schema: 'chanlun-chart-center/v5',
    render_kind: 'center_preview',
    center_id: 'preview-center-1',
    preview_id: 'preview-center-1',
    render_id: `preview-center-1@forming@${BASE + 600}`,
    body_revision: 0,
    structural_level: 0,
    source_kind: 'segment',
    state: 'forming',
    tradable: false,
    points: [
      { time: BASE + 100, price: 11 },
      { time: BASE + 600, price: 10 },
    ],
    core: { zd_tick: 1000, zg_tick: 1100, zd_price: 10, zg_price: 11 },
    entry_unit_id: 'u1',
    core_unit_ids: ['u2', 'u3', 'u4'],
    initial_exit_unit_id: 'u5',
    initial_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    body_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    extension_unit_ids: [],
    pending_leave_unit_id: 'u5',
    completion_leave_unit_id: null,
    completion_return_unit_id: null,
    completion_direction: null,
    completed_at: null,
    available_at: BASE + 600,
    ...overrides,
  };
}

function centerProjection(overrides = {}) {
  return {
    schema: 'chanlun-chart-center/v5',
    render_kind: 'center_projection',
    center_id: 'center-1',
    render_id: `center-1@1@ongoing@projection@${BASE + 600}`,
    body_revision: 1,
    structural_level: 0,
    source_kind: 'segment',
    state: 'ongoing',
    tradable: false,
    points: [
      { time: BASE + 100, price: 11 },
      { time: BASE + 600, price: 10 },
    ],
    source_center_render_id: 'center-1@1@ongoing',
    available_at: BASE + 500,
    ...overrides,
  };
}

function divergence(kind = 'trend', overrides = {}) {
  return {
    schema: 'chanlun-chart-divergence/v4',
    render_kind: 'strict_divergence',
    render_id: `${kind}-divergence-1`,
    divergence_id: `${kind}-divergence-1`,
    kind,
    direction: 'down',
    structural_level: 0,
    source_kind: 'segment',
    price_basis_revision: 'raw-v1',
    compare_unit_id: 'u1',
    signal_unit_id: 'u5',
    anchor_at: BASE + 500,
    anchor_tick: 1000,
    anchor_price: 10,
    confirmed_at: BASE + 500,
    available_at: BASE + 500,
    metrics: { is_divergent: true, strength_source: 'macd' },
    tradable: true,
    points: [{ time: BASE + 500, price: 10 }],
    ...overrides,
  };
}

function snapshot(overrides = {}) {
  return {
    schema: 'chanlun-chart-structure/v5',
    symbol: 'SH.600519',
    source_frequency: '5m',
    display_frequency: '5m',
    source_closed_at: BASE + 600,
    price_basis_revision: 'raw-v1',
    structure_price_quantum: '0.01',
    strict_config_revision: 'strict-config-v1',
    structure_revision: 'sha256:structure-1',
    snapshot_revision: 'sha256:snapshot-1',
    render_revision: 'sha256:render-1',
    stroke_center_observations: [],
    levels: [{
      structural_level: 0,
      label: '5m',
      origin: 'current_chart_recursive',
      centers: [center()],
      center_previews: [],
      center_projections: [],
      current_trends: [],
      completed_trend_snapshots: [],
      confirmed_points: [],
      approaching_points: [],
      divergences: [],
    }],
    ...overrides,
  };
}

function chartData(mode = 'replace', strict = snapshot(), bars = null) {
  const result = {
    bars: bars || [
      { time: BASE * 1000, high: 12, low: 9 },
      { time: (BASE + 100) * 1000, high: 12, low: 9 },
      { time: (BASE + 500) * 1000, high: 12, low: 9 },
      { time: (BASE + 600) * 1000, high: 12, low: 9 },
    ],
    strict_structure_mode: mode,
  };
  if (strict !== undefined) result.strict_structure = strict;
  return {
    symbolKey: 'a:SH.600519_5',
    chartSymbol: 'a:SH.600519',
    barsResult: result,
    from: BASE + 50,
    visibleRange: { from: BASE + 50, to: BASE + 550 },
  };
}

function dailyChartData(rawCloseAt = DAILY_CLOSE_AT) {
  const data = chartData('replace', snapshot({
    symbol: 'SH.513100',
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: DAILY_CLOSE_AT,
    levels: [],
  }), [
    { time: DAILY_BAR_AT * 1000, high: 1.5, low: 1.4 },
  ]);
  data.symbolKey = 'a:SH.513100_1D';
  data.chartSymbol = 'a:SH.513100';
  data.barsResult.times = [rawCloseAt * 1000];
  data.from = DAILY_BAR_AT;
  data.visibleRange = { from: DAILY_BAR_AT, to: DAILY_BAR_AT };
  return data;
}

function manager(instanceId = 'chart-manager-1') {
  const { ChartManager } = loadChartManager();
  const cm = Object.create(ChartManager.prototype);
  const calls = { create: [], remove: [] };
  let nextId = 1;
  cm.id = instanceId.replace('chart-manager-', '');
  cm.instanceId = instanceId;
  cm.obj_charts = {};
  cm._reconcileOwnedIds = new Set();
  cm._reconcileRetry = { count: 0, timer: null };
  cm._reconcileGuard = {};
  cm._scheduleVerifyRebuild = () => {};
  cm.cl_show_config = {};
  cm.chart = {
    createMultipointShape(points, options) {
      const id = `${instanceId}-shape-${nextId++}`;
      calls.create.push({ id, points: points.map((point) => ({ ...point })), options });
      return id;
    },
    createShape(points, options) {
      const id = `${instanceId}-shape-${nextId++}`;
      calls.create.push({ id, points, options });
      return id;
    },
    removeEntity(id) { calls.remove.push(id); },
  };
  return { cm, calls };
}

function scopeContext(cm) {
  return {
    chartInstanceId: cm.instanceId,
    symbol: 'SH.600519',
    interval: '5m',
    price_basis_revision: 'raw-v1',
  };
}

test('visible range shrink and expand rebuild only the clipped crossing center', () => {
  const { cm, calls } = manager();
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  cm._strictStructureContextToken = 'token';
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 50, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 300, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 150, to: BASE + 550 }, create, 'token');

  assert.equal(calls.create.length, 3);
  assert.equal(calls.remove.length, 2);
  assert.deepEqual(calls.create.map((entry) => entry.points.map((point) => point.time)), [
    [BASE + 100, BASE + 500],
    [BASE + 300, BASE + 500],
    [BASE + 150, BASE + 500],
  ]);
});

test('loaded range expansion rebuilds only when clipped center geometry changes', () => {
  const { cm, calls } = manager();
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  cm._strictStructureContextToken = 'token';
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(scope, [item], { from: BASE + 200, to: BASE + 600 }, { from: BASE + 200, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 200, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 400 }, { from: BASE + 200, to: BASE + 400 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 200, to: BASE + 550 }, create, 'token');

  assert.equal(calls.create.length, 3);
  assert.equal(calls.remove.length, 2);
  assert.deepEqual(calls.create.map((entry) => entry.points.map((point) => point.time)), [
    [BASE + 200, BASE + 500],
    [BASE + 200, BASE + 400],
    [BASE + 200, BASE + 500],
  ]);
});

test('body revision replaces one entity under the same logical center key', () => {
  const { cm, calls } = manager();
  const first = center(1);
  const second = center(2);
  const scope = Reconcile.scopeKey(scopeContext(cm), first);
  cm._strictStructureContextToken = 'token';
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(scope, [first], { from: BASE, to: BASE + 600 }, { from: BASE, to: BASE + 600 }, create, 'token');
  cm._reconcileStrictScope(scope, [second], { from: BASE, to: BASE + 600 }, { from: BASE, to: BASE + 600 }, create, 'token');

  assert.equal(calls.create.length, 2);
  assert.deepEqual(calls.remove, [calls.create[0].id]);
});

test('late promise from an older revision is removed and never enters current container', async () => {
  const { cm, calls } = manager();
  const first = center(1);
  const second = center(2);
  const scope = Reconcile.scopeKey(scopeContext(cm), first);
  cm._strictStructureContextToken = 'token';
  let resolveOld;
  const oldPromise = new Promise((resolve) => { resolveOld = resolve; });

  cm._reconcileStrictScope(scope, [first], { from: BASE, to: BASE + 600 }, { from: BASE, to: BASE + 600 }, () => oldPromise, 'token');
  cm._reconcileStrictScope(scope, [second], { from: BASE, to: BASE + 600 }, { from: BASE, to: BASE + 600 }, () => 'current-id', 'token');
  resolveOld('stale-id');
  await Promise.resolve();
  await Promise.resolve();

  const current = cm._strictContainers.get(scope);
  assert.deepEqual(Array.from(current, (entry) => entry.id), ['current-id']);
  assert.ok(calls.remove.includes('stale-id'));
  assert.equal(cm._reconcileOwnedIds.has('stale-id'), false);
});

test('TradingView-snapped center is rejected until its source bars are loaded', () => {
  const { cm, calls } = manager('chart-manager-snapped-center');
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  const retryReasons = [];
  let snapToLoadedBoundary = true;
  cm._strictStructureContextToken = 'token';
  cm._scheduleReconcileRetry = (reason) => retryReasons.push(reason);
  cm.chart.getShapeById = (id) => {
    const created = calls.create.find((entry) => entry.id === id);
    return {
      getPoints() {
        const points = created.points.map((point) => ({ ...point }));
        if (snapToLoadedBoundary) points[0].time = BASE + 300;
        return points;
      },
    };
  };
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(
    scope,
    [item],
    { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 },
    create,
    'token',
  );

  assert.equal(cm._strictContainers.get(scope).length, 0);
  assert.deepEqual(calls.remove, [calls.create[0].id]);
  assert.deepEqual(retryReasons, ['strict-create-snapped']);

  snapToLoadedBoundary = false;
  cm._reconcileStrictScope(
    scope,
    [item],
    { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 },
    create,
    'token',
  );

  assert.equal(cm._strictContainers.get(scope).length, 1);
  assert.equal(cm._strictContainers.get(scope)[0].id, calls.create[1].id);
});

test('TradingView price snapping is rejected even when point times still match', () => {
  const { cm, calls } = manager('chart-manager-price-snapped-center');
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  const retryReasons = [];
  cm._strictStructureContextToken = 'token';
  cm._strictStructureSnapshot = snapshot();
  cm._scheduleReconcileRetry = (reason) => retryReasons.push(reason);
  cm.chart.getShapeById = (id) => {
    const created = calls.create.find((entry) => entry.id === id);
    return {
      getPoints() {
        const points = created.points.map((point) => ({ ...point }));
        points[0].price += 0.01;
        return points;
      },
    };
  };

  cm._reconcileStrictScope(
    scope,
    [item],
    { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 },
    (renderItem) => cm.chart.createMultipointShape(renderItem.points, {}),
    'token',
  );

  assert.equal(cm._strictContainers.get(scope).length, 0);
  assert.deepEqual(calls.remove, [calls.create[0].id]);
  assert.deepEqual(retryReasons, ['strict-create-snapped']);
});

test('unreadable same-tick TradingView geometry is retried instead of accepted', () => {
  const { cm, calls } = manager('chart-manager-unreadable-center');
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  const retryReasons = [];
  let readable = false;
  cm._strictStructureContextToken = 'token';
  cm._scheduleReconcileRetry = (reason) => retryReasons.push(reason);
  cm.chart.getShapeById = (id) => {
    if (!readable) return null;
    const created = calls.create.find((entry) => entry.id === id);
    return { getPoints: () => created.points.map((point) => ({ ...point })) };
  };
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(
    scope,
    [item],
    { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 },
    create,
    'token',
  );
  assert.equal(cm._strictContainers.get(scope).length, 0);
  assert.deepEqual(retryReasons, ['strict-create-snapped']);

  readable = true;
  cm._reconcileStrictScope(
    scope,
    [item],
    { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 },
    create,
    'token',
  );
  assert.equal(cm._strictContainers.get(scope).length, 1);
});

test('retained strict entity is rebuilt when TradingView drifts after initial acceptance', () => {
  const { cm, calls } = manager('chart-manager-post-create-drift');
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  cm._strictStructureContextToken = 'token';
  let driftedId = null;
  cm.chart.getShapeById = (id) => {
    const created = calls.create.find((entry) => entry.id === id);
    if (!created) return null;
    return {
      getPoints() {
        const points = created.points.map((point) => ({ ...point }));
        if (id === driftedId) points[0].time += 300;
        return points;
      },
    };
  };
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(
    scope, [item], { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 }, create, 'token',
  );
  driftedId = cm._strictContainers.get(scope)[0].id;

  cm._reconcileStrictScope(
    scope, [item], { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 }, create, 'token',
  );

  assert.equal(calls.create.length, 2);
  assert.deepEqual(calls.remove, [driftedId]);
  assert.notEqual(cm._strictContainers.get(scope)[0].id, driftedId);
});

test('retained strict entity is rebuilt when TradingView no longer owns its id', () => {
  const { cm, calls } = manager('chart-manager-missing-retained');
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  cm._strictStructureContextToken = 'token';
  let missingId = null;
  cm.chart.getShapeById = (id) => {
    if (id === missingId) return null;
    const created = calls.create.find((entry) => entry.id === id);
    return created
      ? { getPoints: () => created.points.map((point) => ({ ...point })) }
      : null;
  };
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(
    scope, [item], { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 }, create, 'token',
  );
  missingId = cm._strictContainers.get(scope)[0].id;

  cm._reconcileStrictScope(
    scope, [item], { from: BASE, to: BASE + 600 },
    { from: BASE, to: BASE + 600 }, create, 'token',
  );

  assert.equal(calls.create.length, 2);
  assert.deepEqual(calls.remove, [missingId]);
  assert.notEqual(cm._strictContainers.get(scope)[0].id, missingId);
});

test('saved drawing state is awaited before automatic Chanlun redraw', async () => {
  const { cm } = manager('chart-manager-drawing-state-await');
  let resolveApply;
  let redraws = 0;
  let resolved = false;
  cm._activeDrawingMutations = new Set();
  cm._activeContextToken = 'token';
  cm._drawingsCache = new Map();
  cm.isApplyingDrawingState = false;
  cm.chart.removeAllShapes = () => {};
  cm.chart.applyLineToolsState = () => new Promise((resolve) => { resolveApply = resolve; });
  cm.debouncedDrawChanlun = () => { redraws += 1; };

  const applying = cm.applyUserDrawingsState(
    { sources: new Map(), groups: new Map() },
    'token',
    'cache-key',
  ).then((value) => { resolved = true; return value; });
  await Promise.resolve();

  assert.equal(resolved, false);
  assert.equal(redraws, 0);

  resolveApply();
  assert.equal(await applying, true);
  assert.equal(redraws, 1);
});

test('late TradingView data-ready event reopens an exhausted reconcile budget', () => {
  const { cm } = manager('chart-manager-late-ready');
  let draws = 0;
  cm._dataContextVersion = 3;
  cm._tvDataReadyVersion = 3;
  cm._tvDataReadyIdentity = 'sh.600519|5';
  cm._pendingChanlunDrawVersion = null;
  cm._pendingChanlunDrawIdentity = null;
  cm._reconcileRetry = { count: 7, timer: null };
  cm._currentDataIdentityKey = () => 'sh.600519|5';
  cm._chartDataReadyNow = () => true;
  cm._maybeWidenDefaultView = () => {};
  cm.debouncedDrawChanlun = () => { draws += 1; };

  assert.equal(cm.handleDataReady(3, 'sh.600519|5'), true);
  assert.equal(cm._reconcileRetry.count, 0);
  assert.equal(draws, 1);
});

test('disposing a chart cancels strict retry, verification and orphan timers', () => {
  let nextTimerId = 1;
  const callbacks = new Map();
  const cleared = new Set();
  const scheduleTimer = (callback) => {
    const id = nextTimerId++;
    callbacks.set(id, callback);
    return id;
  };
  const { ChartManager } = loadChartManager({
    setTimeout: scheduleTimer,
    clearTimeout(id) {
      cleared.add(id);
    },
  });
  const cm = Object.create(ChartManager.prototype);
  cm.id = 'disposed';
  cm.instanceId = 'chart-manager-disposed';
  cm.obj_charts = {};
  cm._reconcileRetry = { count: 0, timer: null };
  cm._reconcileGuard = {};
  cm._reconcileOwnedIds = new Set();
  cm._disposed = false;
  let draws = 0;
  let sweeps = 0;
  cm.draw_chanlun = () => { draws += 1; };
  cm.sweepOrphanShapes = () => { sweeps += 1; };

  cm._scheduleReconcileRetry('test');
  cm._scheduleVerifyRebuild();
  cm._sweepOrphanTimer = scheduleTimer(() => {
    cm._sweepOrphanTimer = null;
    if (cm._disposed) return;
    cm.sweepOrphanShapes();
  });
  const timerIds = [...callbacks.keys()];
  cm.dispose();
  timerIds.forEach((id) => callbacks.get(id)());

  assert.equal(cm._disposed, true);
  assert.equal(cm._reconcileRetry.timer, null);
  assert.equal(cm._verifyRebuildTimer, null);
  assert.equal(cm._sweepOrphanTimer, null);
  assert.equal(timerIds.every((id) => cleared.has(id)), true);
  assert.equal(draws, 0);
  assert.equal(sweeps, 0);
});

test('two chart instances own identical center ids independently', () => {
  const first = manager('chart-manager-1');
  const second = manager('chart-manager-2');

  first.cm._drawStrictStructure(chartData(), '5');
  second.cm._drawStrictStructure(chartData(), '5');
  first.cm._clearAllStrictScopes('dispose');

  assert.equal(first.calls.remove.length, 1);
  assert.equal(second.calls.remove.length, 0);
  assert.equal(second.cm._reconcileOwnedIds.size, 1);
});

test('daily snapshot uses raw history close for identity and normalized bar time for coordinates', () => {
  const { cm } = manager('chart-manager-daily');
  const data = dailyChartData();

  const validated = cm._validateStrictStructureSnapshot(
    data.barsResult.strict_structure,
    data,
    '1D',
  );

  assert.equal(validated.loadedRange.to, DAILY_BAR_AT);
});

test('daily strict center renders on calendar coordinates without retry deletion', () => {
  const { cm, calls } = manager('chart-manager-daily-center');
  const rawStart = Date.UTC(2025, 11, 16, 7) / 1000;
  const rawEnd = Date.UTC(2026, 0, 28, 7) / 1000;
  const chartStart = Date.UTC(2025, 11, 16) / 1000;
  const chartEnd = Date.UTC(2026, 0, 28) / 1000;
  const strict = snapshot({
    symbol: 'SH.513100',
    source_frequency: 'd',
    display_frequency: 'd',
    source_closed_at: rawEnd,
    levels: [{
      structural_level: 0,
      label: 'd',
      origin: 'current_chart_recursive',
      centers: [center(1, {
        points: [
          { time: rawStart, price: 2.2 },
          { time: rawEnd, price: 2.1 },
        ],
      })],
      center_previews: [],
      center_projections: [],
      current_trends: [],
      completed_trend_snapshots: [],
      confirmed_points: [],
      approaching_points: [],
      divergences: [],
    }],
  });
  const data = chartData('replace', strict, [
    { time: chartStart * 1000, high: 2.3, low: 2.0 },
    { time: chartEnd * 1000, high: 2.3, low: 2.0 },
  ]);
  data.symbolKey = 'a:SH.513100_1D';
  data.chartSymbol = 'a:SH.513100';
  data.barsResult.times = [rawStart * 1000, rawEnd * 1000];
  data.from = chartStart;
  data.visibleRange = { from: chartStart, to: chartEnd };
  cm.chart.getShapeById = (id) => {
    const created = calls.create.find((entry) => entry.id === id);
    return { getPoints: () => created.points.map((point) => ({ ...point })) };
  };

  cm._drawStrictStructure(data, '1D');

  assert.deepEqual(
    calls.create[0].points.map((point) => point.time),
    [chartStart, chartEnd],
  );
  assert.equal([...cm._strictContainers.values()][0].length, 1);
  assert.equal(cm._strictStructureStatus.state, 'ready');
  assert.equal(cm._reconcileRetry.count, 0);
});

test('daily snapshot rejects a stale raw history close even when chart bar time is unchanged', () => {
  const { cm } = manager('chart-manager-daily-stale');
  const data = dailyChartData(DAILY_CLOSE_AT - 86400);

  assert.throws(
    () => cm._validateStrictStructureSnapshot(data.barsResult.strict_structure, data, '1D'),
    /source close does not match loaded bars/,
  );
});

test('same-context strict unavailable briefly retains the last good entity as stale', () => {
  const { cm, calls } = manager();
  cm._drawStrictStructure(chartData(), '5');
  const originalSnapshot = cm._strictStructureSnapshot;
  const originalId = calls.create[0].id;
  const unavailable = chartData('unavailable', undefined);
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(calls.remove.length, 0);
  assert.equal(cm._strictStructureSnapshot, originalSnapshot);
  assert.equal([...cm._strictContainers.values()][0][0].id, originalId);
  assert.equal(cm._strictStructureStatus.state, 'stale');
  assert.equal(cm._strictStructureStatus.code, 'strict_evidence_invalid');
});

test('strict unavailable clears a snapshot after its bounded retention window', () => {
  const { cm, calls } = manager();
  cm._drawStrictStructure(chartData(), '5');
  const unavailable = chartData('unavailable', undefined, [
    { time: BASE * 1000, high: 12, low: 9 },
    { time: (BASE + 3600) * 1000, high: 12, low: 9 },
  ]);
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(calls.remove.length, 1);
  assert.equal(cm._strictStructureSnapshot, null);
  assert.equal(cm._strictStructureStatus.state, 'unavailable');
});

test('strict unavailable without a same-context snapshot still clears and reports unavailable', () => {
  const { cm } = manager();
  const unavailable = chartData('unavailable', undefined);
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(cm._strictContainers?.size || 0, 0);
  assert.equal(cm._strictStructureStatus.state, 'unavailable');
  assert.equal(cm._strictStructureStatus.code, 'strict_evidence_invalid');
});

test('strict unavailable for a different symbol clears the prior symbol entities', () => {
  const { cm, calls } = manager();
  cm._drawStrictStructure(chartData(), '5');
  const unavailable = chartData('unavailable', undefined);
  unavailable.chartSymbol = 'a:SH.000001';
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(calls.remove.length, 1);
  assert.equal(cm._strictContainers.size, 0);
  assert.equal(cm._strictStructureSnapshot, null);
  assert.equal(cm._strictStructureStatus.state, 'unavailable');
});

test('history pagination unchanged keeps the authoritative snapshot and entity id', () => {
  const { cm, calls } = manager();
  const initial = chartData();
  cm._drawStrictStructure(initial, '5');
  const originalSnapshot = cm._strictStructureSnapshot;
  const originalId = calls.create[0].id;
  const paged = chartData('unchanged', undefined, [
    { time: (BASE - 600) * 1000, high: 12, low: 9 },
    { time: BASE * 1000, high: 12, low: 9 },
    { time: (BASE + 100) * 1000, high: 12, low: 9 },
    { time: (BASE + 500) * 1000, high: 12, low: 9 },
    { time: (BASE + 600) * 1000, high: 12, low: 9 },
  ]);

  cm._drawStrictStructure(paged, '5');

  assert.equal(cm._strictStructureSnapshot, originalSnapshot);
  assert.equal(calls.create.length, 1);
  assert.equal(calls.remove.length, 0);
  assert.equal([...cm._strictContainers.values()][0][0].id, originalId);
});

test('ongoing center is dashed and third-point completed center is solid', () => {
  const ongoing = manager('chart-manager-ongoing');
  ongoing.cm._drawStrictStructure(chartData(), '5');
  assert.equal(ongoing.calls.create[0].options.overrides.linestyle, 2);

  const completed = manager('chart-manager-completed');
  const completedSnapshot = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [center(2, {
        render_id: 'center-1@2@completed',
        state: 'completed',
        completion_leave_unit_id: 'u6',
        completion_return_unit_id: 'u7',
        completion_direction: 'up',
        completed_at: BASE + 500,
      })],
    }],
  });
  completed.cm._drawStrictStructure(chartData('replace', completedSnapshot), '5');
  assert.equal(completed.calls.create[0].options.overrides.linestyle, 0);
});

test('forming center preview is non-tradable and renders as a thin dashed box', () => {
  const { cm, calls } = manager('chart-manager-preview');
  const item = centerPreview();
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [],
      center_previews: [item],
    }],
  });

  const grouped = [...cm._strictRenderGroups(strict, scopeContext(cm)).values()].flat();
  assert.equal(grouped.length, 1);
  assert.equal(grouped[0].tradable, false);

  cm._drawStrictStructure(chartData('replace', strict), '5');

  assert.equal(calls.create.length, 1);
  assert.equal(calls.create[0].options.shape, 'rectangle');
  assert.equal(calls.create[0].options.overrides.linestyle, 2);
  assert.equal(calls.create[0].options.overrides.linewidth, 1);
  assert.equal(calls.create[0].options.overrides.transparency, 90);
});

test('geometrically completed preview stays non-tradable but renders solid', () => {
  const { cm, calls } = manager('chart-manager-completed-preview');
  const item = centerPreview({
    state: 'completed',
    render_id: 'preview-1@completed@u7',
    completion_leave_unit_id: 'u5',
    completion_return_unit_id: 'u6',
    completion_direction: 'up',
  });
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [],
      center_previews: [item],
    }],
  });

  cm._drawStrictStructure(chartData('replace', strict), '5');

  assert.equal(calls.create.length, 1);
  assert.equal(calls.create[0].options.overrides.linestyle, 0);
  assert.equal(calls.create[0].options.overrides.linewidth, 2);
  assert.equal(calls.create[0].options.overrides.transparency, 82);
  assert.equal(item.tradable, false);
});

test('active center projection replaces its shorter formal box with one box', () => {
  const { cm, calls } = manager('chart-manager-active-projection');
  const projection = centerProjection();
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [center()],
      center_projections: [projection],
    }],
  });

  const grouped = [...cm._strictRenderGroups(strict, scopeContext(cm)).values()].flat();
  assert.equal(grouped.length, 1);
  assert.equal(grouped[0].render_kind, 'center_projection');

  cm._drawStrictStructure(chartData('replace', strict), '5');

  assert.equal(calls.create.length, 1);
  assert.deepEqual(
    calls.create[0].points.map((point) => point.time),
    [BASE + 100, BASE + 500],
  );
  assert.deepEqual(projection.points.map((point) => point.time), [BASE + 100, BASE + 600]);
});

test('overlapping provisional center supersedes an ongoing formal center that owns the same units', () => {
  const { cm } = manager('chart-manager-later-preview');
  const preview = centerPreview({
    points: [
      { time: BASE + 300, price: 10.8 },
      { time: BASE + 600, price: 9.8 },
    ],
  });
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [center()],
      center_previews: [preview],
      center_projections: [centerProjection()],
    }],
  });

  const grouped = [...cm._strictRenderGroups(strict, scopeContext(cm)).values()].flat();
  assert.deepEqual(grouped.map((item) => item.render_kind), ['center_preview']);
});

test('boundary-sharing later preview keeps the adjacent ongoing formal center', () => {
  const { cm } = manager('chart-manager-boundary-preview');
  const formal = center(3, {
    points: [
      { time: BASE + 100, price: 10.2 },
      { time: BASE + 500, price: 9.8 },
    ],
    initial_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    body_unit_ids: ['u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8'],
    extension_unit_ids: ['u6', 'u7', 'u8'],
  });
  const preview = centerPreview({
    points: [
      { time: BASE + 500, price: 11.2 },
      { time: BASE + 700, price: 10.8 },
    ],
    entry_unit_id: 'u8',
    core_unit_ids: ['u9', 'u10', 'u11'],
    initial_exit_unit_id: 'u12',
    initial_unit_ids: ['u8', 'u9', 'u10', 'u11', 'u12'],
    body_unit_ids: ['u8', 'u9', 'u10', 'u11', 'u12'],
  });
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [formal],
      center_previews: [preview],
      center_projections: [centerProjection()],
    }],
  });

  const grouped = [...cm._strictRenderGroups(strict, scopeContext(cm)).values()].flat();
  assert.deepEqual(
    grouped.map((item) => item.render_kind).sort(),
    ['center_preview', 'formal_center'],
  );
});

test('disjoint later preview keeps the earlier formal center but hides its open projection', () => {
  const { cm } = manager('chart-manager-disjoint-preview');
  const preview = centerPreview({
    entry_unit_id: 'u6',
    core_unit_ids: ['u7', 'u8', 'u9'],
    initial_exit_unit_id: 'u10',
    initial_unit_ids: ['u6', 'u7', 'u8', 'u9', 'u10'],
    body_unit_ids: ['u6', 'u7', 'u8', 'u9', 'u10'],
  });
  const strict = snapshot({
    levels: [{
      ...snapshot().levels[0],
      centers: [center()],
      center_previews: [preview],
      center_projections: [centerProjection()],
    }],
  });

  const grouped = [...cm._strictRenderGroups(strict, scopeContext(cm)).values()].flat();
  assert.deepEqual(
    grouped.map((item) => item.render_kind).sort(),
    ['center_preview', 'formal_center'],
  );
});

test('safeRemove retains ownership until TradingView confirms the entity disappeared', async () => {
  const { cm } = manager('chart-manager-remove-verification');
  const entityId = 'stubborn-auto-shape';
  let visible = true;
  let attempts = 0;
  cm._reconcileOwnedIds.add(entityId);
  cm.chart.getAllShapes = () => (visible ? [{ id: entityId }] : []);
  cm.chart.removeEntity = () => {
    attempts += 1;
    if (attempts >= 2) visible = false;
  };

  await cm.safeRemove(entityId);
  assert.equal(attempts, 1);
  assert.equal(cm._reconcileOwnedIds.has(entityId), true);

  cm.sweepOrphanShapes();
  await Promise.resolve();
  assert.equal(attempts, 2);
  assert.equal(cm._reconcileOwnedIds.has(entityId), false);
});

test('confirmed fifth unit removes preview and creates a formal ongoing center', () => {
  const { cm, calls } = manager('chart-manager-preview-confirmed');
  const previewSnapshot = snapshot({
    render_revision: 'sha256:render-preview',
    levels: [{
      ...snapshot().levels[0],
      centers: [],
      center_previews: [centerPreview()],
    }],
  });
  const formalSnapshot = snapshot({
    render_revision: 'sha256:render-formal',
    levels: [{
      ...snapshot().levels[0],
      centers: [center()],
      center_previews: [],
    }],
  });

  cm._drawStrictStructure(chartData('replace', previewSnapshot), '5');
  const previewShapeId = calls.create[0].id;
  cm._drawStrictStructure(chartData('replace', formalSnapshot), '5');

  assert.equal(calls.create.length, 2);
  assert.deepEqual(calls.remove, [previewShapeId]);
  assert.equal(calls.create[1].options.overrides.linestyle, 2);
});

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

test('level-scoped consolidation and trend divergences render with explicit labels', () => {
  const { cm, calls } = manager('chart-manager-divergence');
  const strict = snapshot();
  strict.levels[0].divergences = [divergence('consolidation'), divergence('trend')];

  cm._drawStrictStructure(chartData('replace', strict), '5');

  const texts = calls.create.map((entry) => entry.options.text).filter(Boolean);
  assert.deepEqual(texts.sort(), ['5m·盘整背驰', '5m·趋势背驰'].sort());
});
