'use strict';
// cl_show_config 按周期保留：把真实 charts.js 加载进 vm 沙箱，取出内部 config 函数与 ChartManager，
// 配有状态 localStorage mock，验证按周期存取/隔离/继承/迁移。charts.js 无 module.exports，故用此法。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadClConfigApi() {
  const store = new Map();
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isFinite, isNaN, Set, Map, WeakMap, WeakSet, Symbol, Promise, Error,
    Date,
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    performance: { now: () => 0 },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
    Utils: { get_market: () => 'a', get_local_data: () => null, set_local_data: () => {} },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map(), activeManagerId: null }),
    Datafeeds: { UDFCompatibleDatafeed: function () {} },
    TradingView: { widget: function () {} },
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    navigator: { onLine: true }, location: { reload: () => {} },
    CustomEvent: function () {}, EventSource: function () {},
  };
  sb.window = sb; sb.self = sb; sb.globalThis = sb;
  sb.document = {
    addEventListener() {}, removeEventListener() {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {}, addEventListener() {} }),
    getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    body: { appendChild() {} },
  };
  vm.createContext(sb);
  let src = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  src += `
;var __CL_API = {
  resolutionKey: (typeof _resolutionKey !== 'undefined') ? _resolutionKey : null,
  load: (typeof loadClShowConfig !== 'undefined') ? loadClShowConfig : null,
  save: (typeof saveClShowConfig !== 'undefined') ? saveClShowConfig : null,
  baseline: (typeof _clShowConfigBaseline !== 'undefined') ? _clShowConfigBaseline : null,
  resolve: (typeof resolveClConfigForResolution !== 'undefined') ? resolveClConfigForResolution : null,
  ChartManager: (typeof ChartManager !== 'undefined') ? ChartManager : null,
  DEFAULT: (typeof CL_SHOW_DEFAULT !== 'undefined') ? CL_SHOW_DEFAULT : null,
};`;
  vm.runInContext(src, sb, { filename: 'charts.js' });
  return { api: sb.__CL_API, store, sb };
}

// ───────────────────────── Task 1: resolution key 归一 + 按周期存取 ─────────────────────────

test('_resolutionKey 归一化大小写与空值', () => {
  const { api } = loadClConfigApi();
  assert.equal(api.resolutionKey('1D'), '1d');
  assert.equal(api.resolutionKey('5'), '5');
  assert.equal(api.resolutionKey(''), '_');
  assert.equal(api.resolutionKey(null), '_');
  assert.equal(api.resolutionKey(undefined), '_');
});

test('saveClShowConfig 按周期存,loadClShowConfig 读回并 merge 默认', () => {
  const { api, store } = loadClConfigApi();
  api.save('cm1', '5', { fx: false, bi: true });
  assert.ok(store.has('cl_show_config_cm1_5'), '应存到带周期 key');
  const got = api.load('cm1', '5');
  assert.equal(got.fx, false);
  assert.equal(got.bi, true);
  assert.equal(got.xd, api.DEFAULT.xd, '未存的 key 应回落 CL_SHOW_DEFAULT');
});

test('loadClShowConfig 未命中该周期 → 返回 null 哨兵', () => {
  const { api } = loadClConfigApi();
  assert.equal(api.load('cm1', '30'), null);
});

test('周期隔离:周期 5 存的配置不影响周期 30', () => {
  const { api } = loadClConfigApi();
  api.save('cm1', '5', { fx: false });
  assert.equal(api.load('cm1', '30'), null);
  api.save('cm1', '30', { fx: true });
  assert.equal(api.load('cm1', '5').fx, false);
  assert.equal(api.load('cm1', '30').fx, true);
});

test('resolutionKey 归一:1D 与 1d 命中同一存储', () => {
  const { api } = loadClConfigApi();
  api.save('cm1', '1D', { fx: false });
  assert.equal(api.load('cm1', '1d').fx, false);
});
