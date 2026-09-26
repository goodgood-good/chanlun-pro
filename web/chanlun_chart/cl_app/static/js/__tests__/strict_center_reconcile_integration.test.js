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
  source += '\n;globalThis.__STRICT_CONFIG = normalizeClShowConfig;';
  source += '\n;globalThis.__STRICT_DYNAMIC_COLOR = getDynamicColor;';
  source += '\n;globalThis.__STRICT_VISUAL_API = { getSignalColor, getCenterVisualStyle };';
  vm.runInContext(source, sandbox, { filename: 'charts.js' });
  return { ChartManager: sandbox.__STRICT_CM, sandbox };
}

test('an unresolved pen connection is shown as pending without triggering load recovery', () => {
  const { cm } = manager();
  const data = chartData();
  data.barsResult.bis = [{ kind: 'bi', component_index: 0 }, { kind: 'bi', component_index: 1 }];
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'selection_pending');
  assert.ok(cm._strictStructureSnapshot);
  cm._strictReconcileComplete = () => true;
  assert.equal(cm._strictStructureReadyForCurrentContext(), true);
  data.barsResult.bis = [];
  data.barsResult.strict_structure.stroke_connection_pending = true;
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'selection_pending');
  data.barsResult.bis = [{ kind: 'bi', component_index: 0 }];
  data.barsResult.strict_structure.stroke_connection_pending = false;
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'ready');
});

test('conditional centers render separately and disappear when their scope resolves', () => {
  const { cm } = manager();
  const data = chartData();
  const conditional = center(1, {render_kind: 'conditional_center', center_id: 'pending-center',
    render_id: 'pending-center-v1', observation_scope_id: 'range-1', component_index: 1,
    selection_pending: true, state: 'selection_pending', locked: false,
    third_class_confirmed: false, tradable: false, core: {zd_price: 10, zg_price: 11}});
  const snapshot = data.barsResult.strict_structure;
  snapshot.conditional_centers = [conditional];
  snapshot.stroke_connection_pending = true;
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'selection_pending');
  const entries = () => Array.from(cm._strictContainers.values()).flat();
  assert.ok(entries().some(item => item.logicalKey === 'conditional_center:pending-center'));
  snapshot.conditional_centers = [];
  snapshot.stroke_connection_pending = false;
  snapshot.snapshot_revision = snapshot.render_revision = snapshot.structure_revision = 'resolved-2';
  cm._drawStrictStructure(data, '5');
  assert.ok(!entries().some(item => item.logicalKey === 'conditional_center:pending-center'));
  assert.ok(entries().some(item => item.logicalKey === 'formal_center:center-1'));
});

test('a conditional center cannot carry a formal confirmation flag', () => {
  const { cm } = manager();
  const data = chartData();
  data.barsResult.strict_structure.levels[0].centers = [];
  data.barsResult.strict_structure.conditional_centers = [center(1, {
    render_kind: 'conditional_center', observation_scope_id: 'range-1', component_index: 1,
    selection_pending: true, state: 'selection_pending', locked: true,
    third_class_confirmed: false, tradable: false, core: {zd_price: 10, zg_price: 11},
  })];
  assert.throws(() => cm._strictRenderGroups(data.barsResult.strict_structure, {interval: '5m'}),
    /conditional center scope or state is invalid/);
});

function center(revision = 1, overrides = {}) {
  return {
    schema: 'chanlun-chart-center',
    render_kind: 'formal_center',
    center_id: 'center-1',
    price_basis_revision: 'raw-test',
    available_at: BASE + 500,
    render_id: `center-1@${revision}@ongoing`,
    body_revision: revision,
    structural_level: 0,
    source_kind: 'segment',
    state: 'ongoing',
    tradable: false,
    points: [
      { time: BASE + 100, price: 11 },
      { time: BASE + 500, price: 10 },
    ],
    entry_unit_id: 'u1',
    establishment_leave_unit_id: 'u5',
    initial_exit_unit_id: 'u5',
    lifecycle_role_count: 5,
    minimum_lifecycle_role_count: 5,
    core_component_count: 3,
    overlap_component_count: 5,
    establishment_component_count: 5,
    establishment_segment_ids: ['u1', 'u2', 'u3', 'u4', 'u5'],
    core_unit_ids: ['u2', 'u3', 'u4'],
    initial_unit_ids: ['u2', 'u3', 'u4'],
    body_unit_ids: ['u2', 'u3', 'u4'],
    extension_unit_ids: [],
    pending_leave_unit_id: null,
    completion_leave_unit_id: null,
    completion_return_unit_id: null,
    completion_direction: null,
    completed_at: null,
    ...overrides,
  };
}


function snapshot(overrides = {}) {
  const value = {schema:'chanlun-chart-structure', analysis_scope:'native_centers',
    symbol:'SH.600519', source_frequency:'5m', display_frequency:'5m', source_closed_at:BASE+600,
    price_basis_revision:'raw-test', structure_price_quantum:'0.01', strict_config_revision:'strict-config-test',
    structure_revision:'sha256:structure-1',snapshot_revision:'sha256:snapshot-1',render_revision:'sha256:render-1',
    levels:[{structural_level:0,label:'5m',origin:'native_segments',centers:[center()]}], ...overrides};
  if (!value.levels.length) value.levels=[{structural_level:0,label:value.display_frequency,origin:'native_segments',centers:[]}];
  return value;
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

function manager(instanceId = 'chart-manager-1', runtimeOverrides = {}) {
  const { ChartManager, sandbox } = loadChartManager(runtimeOverrides);
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
  // Reconciliation mechanics are exercised with the opt-in formal layer on;
  // product defaults are covered by cl_show_config_per_resolution.test.js.
  cm.cl_show_config = {
    center_all: true,
  };
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
  return { cm, calls, sandbox };
}

function scopeContext(cm) {
  return {
    chartInstanceId: cm.instanceId,
    symbol: 'SH.600519',
    interval: '5m',
    price_basis_revision: 'raw-test',
  };
}

test('forming core draws before an exit exists and is replaced when the exit locks', () => {
  const {cm,calls} = manager();
  const preview = center(1, {render_kind:'center_preview', state:'forming', third_class_confirmed:false,
    preview_status:'awaiting_leave', draw_geometry:true, establishment_leave_unit_id:null,
    initial_exit_unit_id:null, establishment_segment_ids:['u1','u2','u3','u4'],
    establishment_component_count:4, lifecycle_role_count:4, overlap_component_count:4});
  const forming = snapshot({levels:[{structural_level:0,origin:'native_segments',centers:[],center_previews:[preview]}]});
  cm._drawStrictStructure(chartData('replace',forming),'5');
  assert.equal(calls.create.length,1);
  assert.equal(calls.create[0].options.overrides.linestyle,2);
  assert.match(calls.create[0].options.text,/待离开段/);
  cm._drawStrictStructure(chartData(),'5');
  assert.equal(calls.create.length,2);
  assert.deepEqual(calls.remove,[calls.create[0].id]);
  assert.equal(calls.create[1].options.overrides.linestyle,0);
});

test('owner projection with no extra geometry does not draw a duplicate rectangle', () => {
  const {cm,calls} = manager();
  cm._drawStrictStructure(chartData(),'5');
  const preview = center(1,{render_kind:'center_preview',state:'forming',third_class_confirmed:false,
    owner_center_id:'center-1',draw_geometry:false,preview_status:'awaiting_completion_confirmation'});
  const data = snapshot({levels:[{structural_level:0,origin:'native_segments',centers:[center()],center_previews:[preview]}]});
  cm._drawStrictStructure(chartData('replace',data),'5');
  assert.equal(calls.create.length,2);
  assert.deepEqual(calls.remove,[calls.create[0].id]);
  assert.match(calls.create[1].options.title,/回试待确认/);
  assert.match(calls.create[1].options.title, /回试已位于中枢区间外，相关线段待最终确认/);
  assert.doesNotMatch(calls.create[1].options.title, /延伸/);
  assert.deepEqual(calls.create[0].points,calls.create[1].points);
});

for (const pointType of ['3buy', '3sell']) {
  test(`${pointType} return updates the owner label and confirmation removes only its pending tail`, () => {
    const {cm, calls} = manager();
    const owner = center();
    const completedOther = center(1, {center_id: 'other-center', render_id: 'other-completed',
      state: 'completed', third_class_confirmed: true, center_ended: true});
    const preview = center(1, {render_kind: 'center_preview', state: 'forming',
      third_class_confirmed: false, owner_center_id: owner.center_id,
      draw_geometry: true, preview_status: 'extending'});
    const point = {render_kind: 'point_approaching', render_id: 'p3-pending', point_id: 'p3-pending',
      point_type: pointType, side: pointType === '3buy' ? 'buy' : 'sell', status: 'approaching',
      center_id: owner.center_id, structural_level: 0, source_kind: 'segment',
      price_basis_revision: 'raw-test', available_at: BASE + 500,
      points: [{time: BASE + 500, price: pointType === '3buy' ? 12 : 9}]};
    const draw = (centerItem, previews, points = []) => cm._drawStrictStructure(chartData('replace', snapshot({
      analysis_scope: 'centers_and_signals', levels: [{structural_level: 0, origin: 'native_segments',
        centers: [centerItem, completedOther], center_previews: previews, points}],
    })), '5');
    draw(owner, [preview]);
    const extension = calls.create.find(item => item.options.text === 'Z1·延伸中·待确认');
    const unrelated = calls.create.find(item => item.options.text === 'Z2');
    assert.ok(extension && unrelated);

    // Identical geometry must still refresh the tail label and owner title.
    draw(owner, [{...preview, preview_status: 'awaiting_completion_confirmation'}], [point]);
    const waiting = calls.create.find(item => item.options.text === 'Z1·回试待确认');
    assert.ok(waiting);
    assert.ok(calls.remove.includes(extension.id));
    assert.match(waiting.options.title, /相关线段待最终确认/);
    assert.doesNotMatch(waiting.options.title, /延伸/);
    const pendingPoint = calls.create.find(item => item.options.shape === 'text');
    assert.equal(pendingPoint.options.text, (pointType === '3buy' ? '三买' : '三卖') + '·待确认');

    draw({...owner, state: 'completed', render_id: 'center-1@completed', third_class_confirmed: true,
      center_ended: true}, [], [{...point, render_kind: 'point_confirmed', status: 'confirmed',
      point_id: 'p3-confirmed', render_id: 'p3-confirmed'}]);
    assert.ok(calls.remove.includes(waiting.id));
    assert.ok(calls.remove.includes(pendingPoint.id));
    assert.ok(!calls.remove.includes(unrelated.id), 'another completed center keeps its entity');
    const active = calls.create.filter(item => !calls.remove.includes(item.id));
    assert.equal(active.length, 3);
    assert.match(active.find(item => item.options.text === 'Z1').options.title, /中枢已结束/);
    assert.ok(active.every(item => !/待确认|延伸/.test(item.options.text + item.options.title)));
  });
}

test('center level and global switches also control unfinished frames', () => {
  const {cm,calls} = manager();
  const preview = center(1,{render_kind:'center_preview',state:'forming',third_class_confirmed:false,
    draw_geometry:true,preview_status:'awaiting_segment_confirmation'});
  const data = chartData('replace',snapshot({levels:[{structural_level:0,origin:'native_segments',centers:[],center_previews:[preview]}]}));
  cm._drawStrictStructure(data,'5');
  assert.equal(calls.create.length,1);
  cm.cl_show_config.center_L0=false;
  cm._drawStrictStructure(data,'5');
  assert.equal(calls.remove.length,1);
  cm.cl_show_config.center_L0=true;
  cm.cl_show_config.center_all=false;
  cm._drawStrictStructure(data,'5');
  assert.equal(calls.create.length,1);
});

test('frozen boundaries are explained without claiming extension or a confirmed third point', () => {
  for (const state of ['divergence_closed', 'superseded']) {
    const {cm, calls} = manager();
    const closed = center(1, {state, third_class_confirmed: false, center_ended: false});
    cm._drawStrictStructure(chartData('replace', snapshot({levels: [
      {structural_level: 0, origin: 'native_segments', centers: [closed]},
    ]})), '5');
    assert.equal(calls.create.length, 1);
    assert.match(calls.create[0].options.title, /原划分已固定/);
    assert.doesNotMatch(calls.create[0].options.title, /延伸|等待回试|中枢已结束/);
  }
});


// UI contract fixture only. Production proof-DAG fixtures are tested separately.


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
  cm.chart.getAllShapes = () => [];
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
  cm._dataContextGeneration = 3;
  cm._tvDataReadyGeneration = 3;
  cm._tvDataReadyIdentity = 'sh.600519|5';
  cm._pendingChanlunDrawGeneration = null;
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
      origin: 'native_segments',
      centers: [center(1, {
        points: [
          { time: rawStart, price: 2.2 },
          { time: rawEnd, price: 2.1 },
        ],
      })],
      center_previews: [],
      center_projections: [],
      current_trends: [],
      pending_movements: [],
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

test('strict failure clears an expired snapshot and enters automatic recovery', () => {
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
  assert.equal(cm._strictStructureStatus.state, 'recovering');
});

test('exit prices update point details without moving anchors or redrawing on cutoff-only changes', () => {
  const {cm, calls} = manager('chart-manager-exits', {PointExitInfo: require('../point_exit_info.js')});
  const point = {structural_level: 0, source_kind: 'segment', price_basis_revision: 'raw-test',
    available_at: BASE + 500, points: [{time: BASE + 500, price: 11}],
    render_kind: 'point_confirmed', render_id: 'p3', point_id: 'p3', point_type: '3buy', side: 'buy'};
  const plan = {version:'v1',side:'buy',point_status:'confirmed',as_of:BASE+600,
    stop_loss:{price:10.01,trigger:'lte',basis:'center_edge'},take_profit:{price:null}};
  const s = snapshot({analysis_scope:'centers_and_signals', point_exit_plans:{p3:plan},
    levels:[{...snapshot().levels[0],points:[point]}]});
  const draw = () => cm._drawStrictStructure(chartData('replace',s),'5');
  draw();
  const mark = calls.create.find(x=>x.options.shape==='text');
  assert.equal(mark.options.text,'三买');
  assert.match(mark.options.title,/止损参考：≤ 10.01/);
  const count = calls.create.length;
  plan.as_of++;
  draw();
  assert.equal(calls.create.length,count,'a newer cutoff alone must not recreate every point');
  plan.take_profit={price:12,basis:'opposite_point',point_type:'1sell',available_at:BASE+601};
  draw();
  assert.equal(calls.create.length,count+1);
  assert.deepEqual(calls.create.at(-1).points,mark.points);
  assert.match(calls.create.at(-1).options.title,/后续同级一卖/);
});

test('requested signal and observation switches remove only their actual chart entities', () => {
  const {cm, calls} = manager('chart-manager-analysis');
  const common = {structural_level: 0, source_kind: 'segment', price_basis_revision: 'raw-test',
    available_at: BASE + 500, points: [{time: BASE + 500, price: 11}]};
  const point = {...common, render_kind: 'point_confirmed', render_id: 'p3', point_id: 'p3',
    point_type: '3buy', side: 'buy'};
  const divergences = ['consolidation', 'trend'].map(kind => ({...common, kind, direction: 'down',
    render_kind: 'strict_divergence', render_id: kind, divergence_id: kind, metrics: {is_divergent: true}}));
  const data = chartData('replace', snapshot({analysis_scope: 'centers_and_signals',
    levels: [{...snapshot().levels[0], points: [point], divergences}],
    stroke_center_observations: [center(1, {render_kind: 'center_observation',
      source_kind: 'stroke_observation', center_id: 'stroke-center', render_id: 'stroke-center'})],
  }));
  cm.cl_show_config.center_observation = true;
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'ready');
  assert.equal(calls.create.length, 5);
  assert.equal(calls.create.filter(item => item.options.shape === 'text').length, 3);
  assert.ok(calls.create.some(item => item.options.text === '三买'));
  assert.ok(calls.create.some(item => item.options.text === '\n盘背'));
  assert.ok(calls.create.some(item => item.options.text === '\n\n趋背'));
  assert.equal(calls.create.find(item => item.options.text === '三买').options.title, '三买');
  for (const key of ['point_3buy', 'divergence_consolidation_L0', 'center_observation', 'center_all']) {
    const before = calls.remove.length;
    cm.cl_show_config[key] = false;
    cm._drawStrictStructure(data, '5');
    assert.equal(calls.remove.length, before + 1, key);
    assert.equal(calls.create.length, 5, 'unaffected shapes keep their identities');
  }
  assert.equal(Array.from(cm._strictContainers.values()).flat().length, 1);
});

test('recursive center level is drawn only after its own switch is enabled', () => {
  const {cm, calls} = manager('chart-manager-higher-center');
  const higher = center(1, {structural_level: 1, source_kind: 'trend_type',
    center_id: 'higher-center', render_id: 'higher-center', formation_rule: 'recursive_three',
    core: {zd_tick: 1000, zg_tick: 1100}, establishment_component_count: 3,
    establishment_segment_ids: ['u2', 'u3', 'u4']});
  const data = chartData('replace', snapshot({analysis_scope: 'centers_and_signals',
    levels: [snapshot().levels[0], {structural_level: 1, origin: 'completed_lower_structures',
      centers: [higher], points: [], divergences: []}],
  }));
  cm._drawStrictStructure(data, '5');
  assert.equal(calls.create.length, 1);
  cm.cl_show_config.center_L1 = true;
  cm._drawStrictStructure(data, '5');
  assert.equal(cm._strictStructureStatus.state, 'ready');
  assert.equal(calls.create.length, 2);
  cm.cl_show_config.center_L1 = false;
  cm._drawStrictStructure(data, '5');
  assert.deepEqual(calls.remove, [calls.create[1].id]);
});

test('strict failure without a same-context snapshot clears and enters recovery', () => {
  const { cm } = manager();
  const unavailable = chartData('unavailable', undefined);
  unavailable.barsResult.strict_structure_error = { code: 'strict_evidence_invalid' };

  cm._drawStrictStructure(unavailable, '5');

  assert.equal(cm._strictContainers?.size || 0, 0);
  assert.equal(cm._strictStructureStatus.state, 'recovering');
  assert.equal(cm._strictStructureStatus.code, 'strict_evidence_invalid');
});

test('pending initial structure keeps candles and polls without forcing another build', async () => {
  const timers = new Map();
  let nextTimer = 0;
  let stage = 'pending';
  let resets = 0;
  const requests = [];
  const { cm } = manager('chart-manager-1', {
    AbortController,
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (url) => { requests.push(url); return {ok:true, json:async()=>({state:stage})}; },
  });
  cm._currentDataIdentityKey = () => 'us:AAPL.US|1';
  cm._strictStructureReadyForCurrentContext = () => false;
  cm.widget = {symbolInterval:()=>({symbol:'us:AAPL.US', interval:'1'}), resetCache() {}};
  cm.chart.resetData = () => { resets++; };
  cm._requestChanlunDrawWhenReady = () => {};
  cm.udf_datafeed = {_historyProvider:{}};
  cm._strictUnavailable('strict_structure_pending');
  assert.equal(cm._strictStructureStatus.state, 'loading');
  assert.equal(resets, 0);
  async function tick() {
    const [id, value] = [...timers.entries()].find(([_id, t])=>t.delay < 5000);
    timers.delete(id);
    await value.callback();
  }
  await tick();
  assert.equal(resets, 0);
  assert.equal(cm.udf_datafeed._historyProvider._forceRefreshOnce, undefined);
  stage = 'complete';
  await tick();
  assert.equal(resets, 1);
  assert.equal(requests.length, 2);
  assert.ok(requests.every(url=>url.startsWith('/tv/structure-status?')));
  assert.equal(cm.udf_datafeed._historyProvider._forceRefreshOnce, undefined);
  // A repeated preview render during the cache reload must not restart it.
  const reloadGeneration=cm._dataContextGeneration;
  cm.handleSymbolChange({ticker:'US:AAPL.US'});
  assert.equal(cm._dataContextGeneration,reloadGeneration);
  cm._strictUnavailable('strict_structure_pending');
  assert.equal(resets, 1);
  assert.equal([...timers.values()].filter(t=>t.delay < 5000).length, 0);
  // If that history response is lost, retry the status read without a forced
  // calculation. A user context change can still cancel this watchdog.
  const [watchId,watch] = [...timers.entries()].find(([_id,t])=>t.delay === 5000);
  timers.delete(watchId);
  await watch.callback();
  assert.equal([...timers.values()].filter(t=>t.delay < 5000).length, 1);
  cm._resetDataReadyContext();
  assert.equal(timers.size, 0);
  assert.equal(cm._strictInitialReloadIdentity, null);
});

test('a completed initial patch redraws structures and indicators without resetting history', async () => {
  const timers = [];
  const calls = { reset: 0, patch: 0, indicator: 0, draw: 0 };
  const patch = { build_id: 'current-build' };
  const { cm } = manager('chart-manager-1', {
    AbortController,
    setTimeout(callback, delay) { timers.push({callback, delay}); return timers.length; },
    clearTimeout() {},
    fetch: async url => {
      assert.ok(url.includes('build_id=current-build'));
      return { ok: true, json: async () => ({state:'complete', patch}) };
    },
  });
  cm._currentDataIdentityKey = () => 'us:qqq.us|1';
  cm._strictStructureReadyForCurrentContext = () => false;
  cm.widget = {symbolInterval: () => ({symbol:'us:QQQ.US', interval:'1'}), resetCache() { calls.reset++; }};
  cm.chart.resetData = () => calls.reset++;
  cm.udf_datafeed = {_historyProvider: {
    bars_result: new Map([['us:qqq.us1', {initial_structure_build_id:'current-build'}]]),
    applyStructurePatch(value) { assert.strictEqual(value, patch); calls.patch++; return true; },
  }};
  cm._refreshPatchedIndicators = () => calls.indicator++;
  cm._requestChanlunDrawWhenReady = options => { assert.equal(options.immediate, true); calls.draw++; };
  cm._pollInitialStructure();
  await timers.find(t => t.delay < 5000).callback();
  assert.deepEqual(calls, {reset:0, patch:1, indicator:1, draw:1});
  assert.equal(cm._strictRecoveryTimer, null);
});

test('a pending status response cannot reset a chart after the user switches symbols', async () => {
  const timers = [];
  let completeResponse;
  let identity = 'us:AAPL.US|1';
  let resets = 0;
  const { cm } = manager('chart-manager-1', {
    AbortController,
    setTimeout(callback, delay) { timers.push({callback,delay}); return timers.length; },
    clearTimeout() {},
    fetch: () => new Promise(resolve=>{ completeResponse=resolve; }),
  });
  cm._currentDataIdentityKey = () => identity;
  cm._strictStructureReadyForCurrentContext = () => false;
  cm.widget = {symbolInterval:()=>({symbol:'us:AAPL.US', interval:'1'}), resetCache(){resets++;}};
  cm._pollInitialStructure();
  const pending = timers.find(t=>t.delay < 5000).callback();
  identity = 'a:SH.600088|1';
  completeResponse({ok:true, json:async()=>({state:'complete'})});
  await pending;
  assert.equal(resets, 0);
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
  assert.equal(cm._strictStructureStatus.state, 'recovering');
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

test('native ongoing and completed centers use solid outlines', () => {
  const ongoing = manager('chart-manager-ongoing');
  ongoing.cm._drawStrictStructure(chartData(), '5');
  assert.equal(ongoing.calls.create[0].options.overrides.linestyle, 0);

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

test('orphan sweep does not inspect TradingView-owned shapes outside debug mode', () => {
  const { cm, sandbox } = manager('chart-manager-foreign-shape');
  let detailReads = 0;
  sandbox.__chanlunDebug = false;
  cm.chart.getAllShapes = () => [{ id: 'tv-native-shape', name: 'trend_line' }];
  cm.chart.getShapeById = () => {
    detailReads += 1;
    return { getPoints: () => [] };
  };

  cm.sweepOrphanShapes();

  assert.equal(detailReads, 0);
});


test('formal center validation accepts opposite external exits and rejects an exit inside the core', () => {
  const { sandbox } = loadChartManager();
  for (const reverse of [false, true]) {
    const ticks = [80, 120, 90, 115, 100, 110, 70].map(p => reverse ? 200 - p : p);
    const units = ticks.slice(1).map((end, index) => ({
      unit_id: `u${index + 1}`, direction: end > ticks[index] ? 'up' : 'down',
      start_tick: ticks[index], end_tick: end,
      low_tick: Math.min(ticks[index], end), high_tick: Math.max(ticks[index], end),
    }));
    const core = units.slice(1, 4), leave = units[5];
    const item = center(1, { core_formed: true, frame_qualified: true,
      frame_missing_conditions: [], center_ended: false, third_class_confirmed: false,
      center_end_available_at: null, available_at: BASE + 600,
      entering_segment: units[0], leaving_segment: leave, middle_three_components: core,
      core: { zd_tick: Math.max(...core.map(u => u.low_tick)), zg_tick: Math.min(...core.map(u => u.high_tick)) },
      establishment_leave_unit_id: 'u6', initial_exit_unit_id: 'u6', frame_leave_unit_id: 'u6',
      establishment_segment_ids: ['u1', 'u2', 'u3', 'u4', 'u6'],
    });
    assert.doesNotThrow(() => sandbox.validateStrictCenterRenderContract(item, 0));
    leave.end_tick = reverse ? item.core.zg_tick : item.core.zd_tick;
    assert.throws(() => sandbox.validateStrictCenterRenderContract(item, 0), /independent entry or leave/);
  }
});

test('legacy one-price physical center requires five verified closed-contact roles', () => {
  const {sandbox} = loadChartManager();
  const roles = ['u1','u2','u3','u4','u5'].map(unit_id => ({
    unit_id, low_tick: 990, high_tick: 1010, locked: true,
  }));
  const item = center(1, {core: {zd_tick: 1000, zg_tick: 1000},
    runtime_overlap_policy: 'physical_closed_interval_contact',
    overlap_component_count: 0, establishment_segments: roles});
  assert.doesNotThrow(() => sandbox.validateStrictCenterRenderContract(item, 0));
  roles[4].low_tick = 1001;
  assert.throws(() => sandbox.validateStrictCenterRenderContract(item, 0), /five-role overlap/);
  roles[4].low_tick = 990;
  item.runtime_overlap_policy = 'physical_positive_overlap';
  assert.throws(() => sandbox.validateStrictCenterRenderContract(item, 0), /five-role overlap/);
});
