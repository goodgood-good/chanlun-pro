'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadClConfigApi({ focus = false } = {}) {
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
    body: { appendChild() {}, dataset: focus ? {chartFocus:'lowest-center'} : {} },
  };
  vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  source += `
;var __CL_API = {
  resolutionKey: _resolutionKey,
  load: loadClShowConfig,
  save: saveClShowConfig,
  resolve: resolveClConfigForResolution,
  normalize: normalizeClShowConfig,
  enabled: strictItemEnabled,
  ChartManager,
  DEFAULT: CL_SHOW_DEFAULT,
  requestedStudies: requestedDefaultStudies,
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



for (const [interval, lowest] of [['1', 0], ['5', 1], ['30', 2]]) {

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
  assert.equal(api.DEFAULT.schema, 'chanlun-chart-config-v7');
  assert.throws(
    () => api.normalize({ ...api.DEFAULT, schema: "unsupported" }, '5'),
    /cl_show_config_current_schema_required/,
  );
  assert.throws(
    () => api.save('cm1', '5', { fx: false }),
    /cl_show_config_current_schema_required/,
  );
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

  store.set(key, JSON.stringify({ ...api.DEFAULT, schema: 'chanlun-chart-config-v3' }));
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);

  store.set(key, JSON.stringify({ ...api.DEFAULT, schema: 'chanlun-chart-config-v4' }));
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);

  store.set(key, '{not-json');
  assert.equal(api.load('cm1', '5'), null);
  assert.equal(store.has(key), false);
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

test('native settings migrate v6 without restoring retired layers', () => {
 const {api}=loadClConfigApi();
 const config=api.normalize({schema:'chanlun-chart-config-v6',bi:false,xd:true,center_all:true,point_all:true,trend_L1:true},'5');
 assert.deepEqual(JSON.parse(JSON.stringify(config)),{schema:'chanlun-chart-config-v7',fx:false,bi:false,xd:true,center_all:true,center_L0:true});
 assert.equal(api.enabled(config,{render_kind:'formal_center',structural_level:0}),true);
 assert.equal(api.enabled(config,{render_kind:'formal_center',structural_level:1}),false);
 assert.equal(api.enabled(config,{render_kind:'strict_trend',structural_level:0}),false);
});
