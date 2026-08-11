'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadChartManager() {
  const sandbox = {
    console, Math, JSON, Intl, Array, Object, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isFinite, isNaN, Set, Map, WeakMap, WeakSet, Symbol,
    Promise, Error, URLSearchParams, Date,
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: (callback) => { callback(); return 0; },
    clearTimeout() {},
    performance: { now: () => 0 },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    EventSource: function EventSource() {},
    Utils: { get_market: () => 'us', get_local_data: () => '30' },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map() }),
    Datafeeds: { UDFCompatibleDatafeed: function UDFCompatibleDatafeed() {} },
    TradingView: { widget: function widget() {} },
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    navigator: { onLine: true },
    location: { search: '', reload() {}, assign() {} },
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
  source += '\n;globalThis.__DRAWING_CM = ChartManager;';
  vm.runInContext(source, sandbox, { filename: 'charts.js' });
  return sandbox.__DRAWING_CM;
}

test('only explicit valid user drawings are persisted', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._userDrawingIds = new Set(['manual-1']);

  const saved = manager.serializeUserDrawingsState({
    sources: new Map([
      ['manual-1', { type: 'LineToolTrendLine', state: { points: [1, 2] } }],
      ['auto-1', { type: 'LineToolTrendLine', state: { points: [3, 4] } }],
      ['null-entry', null],
    ]),
    groups: new Map([['polluted-group', { lineTools: ['manual-1', 'auto-1'] }]]),
  });

  assert.equal(saved.schema, 'chanlun-user-drawings');
  assert.deepEqual(Object.keys(saved.sources), ['manual-1']);
  assert.deepEqual(Object.keys(saved.groups), []);
});

test('unsupported drawing states are rejected and current sources are normalized', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._userDrawingIds = new Set(['stale']);

  assert.throws(
    () => manager.deserializeUserDrawingsState({
      sources: { GiRd26: { type: 'LineToolTrendLine' } },
    }),
    /drawing_state_schema_invalid/,
  );
  assert.equal(manager._userDrawingIds.size, 0);

  const current = manager.deserializeUserDrawingsState({
    schema: 'chanlun-user-drawings',
    sources: {
      manual: { type: 'LineToolRectangle', state: {} },
      invalid: null,
    },
    groups: {},
  });
  assert.deepEqual(Array.from(current.sources.keys()), ['manual']);
  assert.deepEqual(Array.from(manager._userDrawingIds), ['manual']);
});

test('settled automatic ownership revokes a racing user classification', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._reconcileOwnedIds = new Set();
  manager._userDrawingIds = new Set(['GiRd26']);
  manager._coloredDrawings = new Set(['GiRd26']);

  manager._markAutomaticShapeId('GiRd26');

  assert.equal(manager._reconcileOwnedIds.has('GiRd26'), true);
  assert.equal(manager._userDrawingIds.has('GiRd26'), false);
  assert.equal(manager._coloredDrawings.has('GiRd26'), false);
});

test('manual reload clears overlays through the storage boundary before resetting K line data', async () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  const calls = [];
  manager._disposed = false;
  manager.widget = {
    resetCache() { calls.push('reset-cache'); },
  };
  manager.chart = {
    resetData() { calls.push('reset-data'); },
  };
  manager.udf_datafeed = { _historyProvider: {} };
  manager.scheduleDrawingsSave = async (reason) => {
    calls.push(`save:${reason}`);
  };
  manager._resetDataReadyContext = () => {
    calls.push('reset-data-ready-context');
  };
  manager.reloadDrawingsForCurrentContext = async (reason, options) => {
    calls.push(`reload-drawings:${reason}`);
    assert.equal(options.bypassCache, true);
    assert.equal(options.redrawAutomatic, false);
  };
  manager._requestChanlunDrawWhenReady = () => {
    calls.push('wait-for-kline-before-redraw');
  };

  const first = manager.manualReloadData();
  const racingClick = manager.manualReloadData();
  assert.equal(racingClick, first, '并发点击必须复用同一轮重载');
  assert.equal(await first, true);

  assert.deepEqual(calls, [
    'save:manual-data-reload',
    'reset-data-ready-context',
    'reload-drawings:manual-data-reload',
    'reset-cache',
    'reset-data',
    'wait-for-kline-before-redraw',
  ]);
  assert.equal(manager.udf_datafeed._historyProvider._forceRefreshOnce, true);
  assert.equal(manager._manualReloadInFlight, null);
});
