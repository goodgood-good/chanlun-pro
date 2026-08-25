'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadClConfigApi() {
  const store = new Map();
  const sandbox = {
    console,
    Math,
    JSON,
    Array,
    Object,
    String,
    Number,
    Boolean,
    RegExp,
    parseInt,
    parseFloat,
    isFinite,
    isNaN,
    Set,
    Map,
    WeakMap,
    WeakSet,
    Symbol,
    Promise,
    Error,
    TypeError,
    Date,
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    performance: { now: () => 0 },
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => { store.set(key, String(value)); },
      removeItem: (key) => { store.delete(key); },
    },
    Utils: {
      get_market: () => 'a',
      get_local_data: () => null,
      set_local_data: () => {},
    },
    getTVRegistry: () => ({
      chartManagers: new Map(),
      datafeeds: new Map(),
      widgets: new Map(),
      activeManagerId: null,
    }),
    Datafeeds: { UDFCompatibleDatafeed: function () {} },
    TradingView: { widget: function () {} },
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    navigator: { onLine: true },
    location: { reload: () => {}, assign: () => {} },
    CustomEvent: function () {},
    EventSource: function () {},
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    addEventListener() {},
    removeEventListener() {},
    createElement: () => ({
      style: {},
      classList: { add() {}, remove() {} },
      appendChild() {},
      addEventListener() {},
    }),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { appendChild() {} },
  };
  vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  source += `
;var __CL_API = {
  resolutionKey: _resolutionKey,
  load: loadClShowConfig,
  save: saveClShowConfig,
  resolve: resolveClConfigForResolution,
  levels: recursiveDisplayLevels,
  normalize: normalizeClShowConfig,
  enabled: strictItemEnabled,
  ChartManager,
  DEFAULT: CL_SHOW_DEFAULT,
};`;
  vm.runInContext(source, sandbox, { filename: 'charts.js' });
  return { api: sandbox.__CL_API, store };
}

function currentConfig(api, overrides = {}, interval = '5') {
  return api.normalize({ ...api.DEFAULT, ...overrides }, interval);
}

function makeManager(ChartManager, id) {
  const manager = Object.create(ChartManager.prototype);
  manager.id = id;
  manager._curResolution = null;
  manager.cl_show_config = null;
  return manager;
}

test('resolution keys are canonical and isolated by chart period', () => {
  const { api } = loadClConfigApi();
  assert.equal(api.resolutionKey('1D'), '1d');
  assert.equal(api.resolutionKey(' 5 '), '5');
  assert.equal(api.resolutionKey(null), '_');

  api.save('cm1', '5', currentConfig(api, { fx: false }, '5'));
  assert.equal(api.load('cm1', '5').fx, false);
  assert.equal(api.load('cm1', '30'), null);
});

test('only the current display schema is accepted', () => {
  const { api } = loadClConfigApi();
  assert.equal(api.DEFAULT.schema, 'chanlun-chart-config-v2');
  assert.throws(
    () => api.normalize({ ...api.DEFAULT, schema: "unsupported" }, '5'),
    /cl_show_config_current_schema_required/,
  );
  assert.throws(
    () => api.save('cm1', '5', { fx: false }),
    /cl_show_config_current_schema_required/,
  );
});

test('production defaults show only formal centers and alternating movements', () => {
  const { api } = loadClConfigApi();
  const config = api.normalize(null, '5');

  assert.equal(config.fx, false);
  assert.equal(config.bi, false);
  assert.equal(config.xd, false);
  assert.equal(config.center_observation, false);
  assert.equal(config.center_all, true);
  assert.equal(config.center_provisional, false);
  assert.equal(config.trend_all, true);
  assert.equal(config.pending_movement, false);
  assert.equal(api.enabled(config, {
    render_kind: 'formal_center', structural_level: 0,
  }), true);
  assert.equal(api.enabled(config, {
    render_kind: 'center_preview', structural_level: 0,
  }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'center_projection', structural_level: 0,
  }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'strict_trend', structural_level: 0,
  }), true);
  assert.equal(api.enabled(config, {
    render_kind: 'pending_movement', structural_level: 0,
  }), false);
});

test('stored non-current or malformed configuration is removed', () => {
  const { api, store } = loadClConfigApi();
  const key = 'cl_show_config_cm1_5';

  store.set(key, JSON.stringify({ ...api.DEFAULT, schema: "unsupported", fx: false }));
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);

  store.set(key, JSON.stringify({ ...api.DEFAULT, schema: 'chanlun-chart-config' }));
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);

  store.set(key, '{not-json');
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);
});

test('current schema retains only current fields and period levels', () => {
  const { api } = loadClConfigApi();
  const config = api.normalize({
    ...api.DEFAULT,
    fx: false,
    center_L0: false,
    point_L1: false,
    removed_switch: true,
  }, '5');

  assert.equal(config.fx, false);
  assert.equal(config.center_L0, false);
  assert.equal(config.center_L1, true);
  assert.equal(config.center_L2, true);
  assert.equal(config.point_L0, true);
  assert.equal(config.point_L1, false);
  assert.equal(config.point_L2, true);
  assert.equal(Object.hasOwn(config, 'center_L3'), false);
  assert.equal(Object.hasOwn(config, 'point_L3'), false);
  assert.equal(Object.hasOwn(config, 'removed_switch'), false);
});

test('recursive display levels are derived from the active period', () => {
  const { api } = loadClConfigApi();
  assert.deepEqual(Array.from(api.levels('1')).map((item) => item.label), [
    '1m', '5m', '30m', '日线',
  ]);
  assert.deepEqual(Array.from(api.levels('5')).map((item) => item.label), [
    '5m', '30m', '日线',
  ]);
  assert.deepEqual(Array.from(api.levels('30')).map((item) => item.label), [
    '30m', '日线',
  ]);
  assert.deepEqual(Array.from(api.levels('1D')).map((item) => item.label), ['日线']);
});

test('current center trend point and divergence gates remain independent', () => {
  const { api } = loadClConfigApi();
  const config = currentConfig(api, {
    center_all: false,
    center_L1: true,
    trend_all: true,
    trend_L1: false,
    point_all: true,
    point_1buy: false,
    point_L1: false,
    divergence_all: true,
    divergence_consolidation_L1: false,
    divergence_trend_L1: true,
  });

  assert.equal(api.enabled(config, { render_kind: 'formal_center', structural_level: 1 }), false);
  assert.equal(api.enabled(config, { render_kind: 'strict_trend', structural_level: 1 }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'point_confirmed', structural_level: 0, point_type: '1buy',
  }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'point_confirmed', structural_level: 1, point_type: '2buy',
  }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'point_approaching', structural_level: 0, point_type: '2buy',
  }), true);
  assert.equal(api.enabled(config, {
    render_kind: 'strict_divergence', structural_level: 1, kind: 'consolidation',
  }), false);
  assert.equal(api.enabled(config, {
    render_kind: 'strict_divergence', structural_level: 1, kind: 'trend',
  }), true);
});

test('resolution switching persists each period under the current schema', () => {
  const { api } = loadClConfigApi();
  const manager = makeManager(api.ChartManager, 'cm1');
  manager.cl_show_config = currentConfig(api, { fx: false }, '5');
  manager._curResolution = '5';

  manager._applyResolutionConfig('30');
  assert.equal(api.load('cm1', '5').fx, false);
  assert.equal(manager.cl_show_config.fx, false);
  assert.equal(manager.cl_show_config.schema, api.DEFAULT.schema);

  manager.cl_show_config.fx = true;
  manager._applyResolutionConfig('5');
  assert.equal(manager.cl_show_config.fx, false);
  manager._applyResolutionConfig('30');
  assert.equal(manager.cl_show_config.fx, true);
});
