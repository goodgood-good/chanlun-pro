'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const Reconcile = require('../chart_structure_reconcile.js');

const BASE = 1700000000;

function loadChartManager() {
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
    schema: 'chanlun-chart-center/v4',
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
    schema: 'chanlun-chart-structure/v4',
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

test('visible range shrink and expand keep intersecting center geometry and entity', () => {
  const { cm, calls } = manager();
  const item = center();
  const scope = Reconcile.scopeKey(scopeContext(cm), item);
  cm._strictStructureContextToken = 'token';
  const create = (renderItem) => cm.chart.createMultipointShape(renderItem.points, {});

  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 50, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 300, to: BASE + 550 }, create, 'token');
  cm._reconcileStrictScope(scope, [item], { from: BASE + 50, to: BASE + 600 }, { from: BASE + 150, to: BASE + 550 }, create, 'token');

  assert.equal(calls.create.length, 1);
  assert.equal(calls.remove.length, 0);
  assert.deepEqual(calls.create[0].points.map((point) => point.time), [BASE + 100, BASE + 500]);
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

  assert.equal(calls.create.length, 4);
  assert.equal(calls.remove.length, 3);
  assert.deepEqual(calls.create.map((entry) => entry.points.map((point) => point.time)), [
    [BASE + 200, BASE + 500],
    [BASE + 100, BASE + 500],
    [BASE + 100, BASE + 400],
    [BASE + 100, BASE + 500],
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

test('strict unavailable clears every strict entity and exposes an error state', () => {
  const { cm, calls } = manager();
  cm._drawStrictStructure(chartData(), '5');
  const unavailable = chartData('unavailable', undefined);
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(calls.remove.length, 1);
  assert.equal(cm._strictContainers.size, 0);
  assert.equal(cm._strictStructureStatus.state, 'unavailable');
  assert.equal(cm._strictStructureStatus.code, 'strict_evidence_invalid');
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

test('level-scoped consolidation and trend divergences render with explicit labels', () => {
  const { cm, calls } = manager('chart-manager-divergence');
  const strict = snapshot();
  strict.levels[0].divergences = [divergence('consolidation'), divergence('trend')];

  cm._drawStrictStructure(chartData('replace', strict), '5');

  const texts = calls.create.map((entry) => entry.options.text).filter(Boolean);
  assert.deepEqual(texts.sort(), ['5m·盘整背驰', '5m·趋势背驰'].sort());
});
