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
  migrate: (typeof _migrateStrictDisplayConfig !== 'undefined') ? _migrateStrictDisplayConfig : null,
  levels: (typeof recursiveDisplayLevels !== 'undefined') ? recursiveDisplayLevels : null,
  centerPeriods: (typeof centerControlPeriods !== 'undefined') ? centerControlPeriods : null,
  normalize: (typeof normalizeClShowConfig !== 'undefined') ? normalizeClShowConfig : null,
  enabled: (typeof strictItemEnabled !== 'undefined') ? strictItemEnabled : null,
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

// ───────────────────────── Task 2: 迁移基准 + 按周期解析 ─────────────────────────

test('_clShowConfigBaseline 回退链:旧无周期 key 优先于全局旧 key', () => {
  const { api, store } = loadClConfigApi();
  store.set('cl_show_config_cm1', JSON.stringify({ fx: false }));
  store.set('cl_show_config', JSON.stringify({ bi: false }));
  const base = api.baseline('cm1', '5');
  assert.equal(base.fx, false, '应取旧无周期 key');
  assert.equal(base.bi, api.DEFAULT.bi, '无周期 key 命中即不再取全局旧 key');
});

test('_clShowConfigBaseline 无周期 key 缺失时用全局旧 key', () => {
  const { api, store } = loadClConfigApi();
  store.set('cl_show_config', JSON.stringify({ bi: false }));
  assert.equal(api.baseline('cm1', '5').bi, false);
});

test('_clShowConfigBaseline 全空 → schema v9 显示基础笔中枢和四个固定周期中枢', () => {
  const { api } = loadClConfigApi();
  const base = api.baseline('cm1', '5');
  assert.equal(base.schema_version, 9);
  assert.equal(base.fx, api.DEFAULT.fx);
  assert.equal(base.pen_center, true);
  assert.equal(base.center_observation, false);
  assert.equal(base.center_all, false);
  assert.equal(base.center_control_all, true);
  assert.equal(base.center_1m, true);
  assert.equal(base.center_5m, true);
  assert.equal(base.center_30m, true);
  assert.equal(base.center_d, true);
  assert.notEqual(base, api.DEFAULT);
});

test('旧递归中枢开关不会污染新的笔中枢与周期中枢控制', () => {
  const { api, store } = loadClConfigApi();
  store.set('cl_show_config_cm1', JSON.stringify({
    zs_bi: false,
    zs_xd: true,
    zs_L1: false,
    mmd: false,
    point_1buy: false,
  }));

  const migrated = api.baseline('cm1', '5');

  assert.equal(migrated.pen_center, true);
  assert.equal(migrated.center_observation, false);
  assert.equal(migrated.center_1m, true);
  assert.equal(migrated.center_5m, true);
  assert.equal(migrated.center_30m, true);
  assert.equal(migrated.center_d, true);
  assert.equal(migrated.point_all, false);
  assert.equal(migrated.point_1buy, false, '旧总开关不得重启已关闭的一买子开关');
  for (const oldKey of ['zs_bi', 'zs_xd', 'zs_L1', 'mmd']) {
    assert.equal(Object.hasOwn(migrated, oldKey), false, `迁移后不得保留 ${oldKey}`);
  }
});

test('schema v8 显式笔中枢和固定周期中枢开关可独立关闭', () => {
  const { api } = loadClConfigApi();
  const migrated = api.normalize(
    { schema_version: 8, pen_center: false, center_5m: false },
    '5',
  );

  assert.equal(migrated.pen_center, false);
  assert.equal(migrated.center_5m, false);
  assert.equal(migrated.center_1m, true);
  assert.equal(migrated.center_30m, true);
  assert.equal(migrated.center_d, true);
  assert.equal(migrated.center_control_all, true, 'v8 升级后总开关默认开启');
  for (const pointType of ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell']) {
    assert.equal(api.DEFAULT[`point_${pointType}`], true, `${pointType} 默认应开启`);
  }
  assert.equal(api.DEFAULT.point_all, true);
  assert.equal(Object.hasOwn(api.DEFAULT, 'point_approaching'), false);
});

test('schema v3 迁移后禁用递归中枢并启用新的双层中枢控制', () => {
  const { api } = loadClConfigApi();
  const migrated = api.normalize(
    { schema_version: 3, center_observation: false, center_all: true },
    '5',
  );

  assert.equal(migrated.schema_version, 9);
  assert.equal(migrated.pen_center, true);
  assert.equal(migrated.center_observation, false);
  assert.equal(migrated.center_all, false);
  assert.equal(migrated.center_1m, true);
  assert.equal(migrated.center_5m, true);
  assert.equal(api.enabled(migrated, { render_kind: 'center_observation' }), false);
  assert.equal(api.enabled(migrated, { render_kind: 'formal_center', structural_level: 0 }), false);
});

test('schema v4 的旧观察开关不会误关新笔中枢或周期中枢', () => {
  const { api } = loadClConfigApi();
  const disabled = api.normalize(
    { schema_version: 4, center_observation: false },
    '5',
  );

  assert.equal(disabled.pen_center, true);
  assert.equal(disabled.center_observation, false);
  assert.equal(disabled.center_all, false);
  assert.equal(disabled.center_1m, true);
  assert.equal(disabled.center_5m, true);
  assert.equal(api.enabled(disabled, { render_kind: 'center_observation' }), false);
  assert.equal(api.enabled({}, { render_kind: 'center_observation' }), false);
});

test('schema v5 的严格递归选择不会迁移到新的周期中枢控制', () => {
  const { api } = loadClConfigApi();
  const enabled = api.normalize(
    { schema_version: 5, center_observation: false, center_all: true, center_L0: true },
    '5',
  );

  assert.equal(enabled.pen_center, true);
  assert.equal(enabled.center_observation, false);
  assert.equal(enabled.center_all, false);
  assert.equal(enabled.center_1m, true);
  assert.equal(enabled.center_5m, true);
  assert.equal(api.enabled(enabled, { render_kind: 'formal_center', structural_level: 0 }), false);
});

test('resolveClConfigForResolution 已配置周期 → 加载存储值,persist=false', () => {
  const { api } = loadClConfigApi();
  api.save('cm1', '5', { fx: false });
  const r = api.resolve('cm1', '5', { fx: true, bi: true });
  assert.equal(r.cfg.fx, false, '用存储值,非 currentCfg');
  assert.equal(r.persist, false);
});

test('resolveClConfigForResolution 未配置周期 → 继承 currentCfg 副本,persist=true', () => {
  const { api } = loadClConfigApi();
  const current = { fx: false, bi: true };
  const r = api.resolve('cm1', '30', current);
  assert.equal(r.cfg.fx, false);
  assert.equal(r.persist, true);
  assert.notEqual(r.cfg, current, '返回副本,不共享引用');
});

test('resolveClConfigForResolution 未配置且 currentCfg=null → 用 baseline 迁移', () => {
  const { api, store } = loadClConfigApi();
  store.set('cl_show_config_cm1', JSON.stringify({ fx: false }));
  const r = api.resolve('cm1', '5', null);
  assert.equal(r.cfg.fx, false, '迁移旧无周期配置');
  assert.equal(r.persist, true);
});

// ───────────────────────── Task 3: _applyResolutionConfig 切周期编排 ─────────────────────────

function makeCm(ChartManager, id) {
  const cm = Object.create(ChartManager.prototype);
  cm.id = id;
  cm._curResolution = null;
  cm.cl_show_config = null;
  return cm;
}

test('_applyResolutionConfig 切走时把当前配置存回旧周期 key', () => {
  const { api } = loadClConfigApi();
  const cm = makeCm(api.ChartManager, 'cm1');
  cm.cl_show_config = Object.assign({}, api.DEFAULT, { bi: false });
  cm._curResolution = '5';
  cm._applyResolutionConfig('30');
  assert.equal(api.load('cm1', '5').bi, false, '旧周期 5 的配置被存回');
  assert.equal(cm._curResolution, '30');
});

test('_applyResolutionConfig 切到未配置周期 → 继承当前配置', () => {
  const { api } = loadClConfigApi();
  const cm = makeCm(api.ChartManager, 'cm1');
  cm.cl_show_config = Object.assign({}, api.DEFAULT, { fx: false });
  cm._curResolution = '5';
  cm._applyResolutionConfig('30');
  assert.equal(cm.cl_show_config.fx, false, '30 未配过 → 继承 5 的 fx:false');
  assert.equal(api.load('cm1', '30').fx, false, '并固化到 30 的 key');
});

test('_applyResolutionConfig 切换 A→B→A：隔离且各自恢复', () => {
  const { api } = loadClConfigApi();
  const cm = makeCm(api.ChartManager, 'cm1');
  cm.cl_show_config = Object.assign({}, api.DEFAULT, { fx: false });
  cm._curResolution = '5';
  cm._applyResolutionConfig('30');
  cm.cl_show_config.fx = true;
  cm._applyResolutionConfig('5');
  assert.equal(cm.cl_show_config.fx, false, '切回 5 恢复其独立配置');
  cm._applyResolutionConfig('30');
  assert.equal(cm.cl_show_config.fx, true, '30 保留自己改过的 fx:true');
});

test('_applyResolutionConfig 相同周期重入不误存(oldRes===newRes)', () => {
  const { api } = loadClConfigApi();
  const cm = makeCm(api.ChartManager, 'cm1');
  cm.cl_show_config = Object.assign({}, api.DEFAULT);
  cm._curResolution = '5';
  cm._applyResolutionConfig('5');
  assert.equal(cm._curResolution, '5');
  assert.ok(cm.cl_show_config, '配置仍在');
});

test('四种图表周期只展示已确认的递归级别', () => {
  const { api } = loadClConfigApi();
  assert.deepEqual(Array.from(api.levels('1')).map((x) => x.label), ['1m', '5m', '30m', '日线']);
  assert.deepEqual(Array.from(api.levels('5')).map((x) => x.label), ['5m', '30m', '日线']);
  assert.deepEqual(Array.from(api.levels('30')).map((x) => x.label), ['30m', '日线']);
  assert.deepEqual(Array.from(api.levels('1D')).map((x) => x.label), ['日线']);
  assert.deepEqual(Array.from(api.levels('15')).map((x) => x.label), ['15m']);
});

test('中枢控制在所有主图周期都固定为四个真实周期且没有递归级别键', () => {
  const { api } = loadClConfigApi();
  const expected = [
    ['1m', '1m 中枢', 'center_1m'],
    ['5m', '5m 中枢', 'center_5m'],
    ['30m', '30m 中枢', 'center_30m'],
    ['d', '日线 中枢', 'center_d'],
  ];
  for (const interval of ['1', '5', '30', '1D']) {
    const actual = Array.from(api.centerPeriods(interval)).map(
      ({ period, label, key }) => [period, label, key],
    );
    assert.deepEqual(actual, expected);
  }
  assert.equal(Object.hasOwn(api.DEFAULT, 'center_L0'), false);
});

test('周期中枢总开关只 gate，不改写四个周期子开关偏好', () => {
  const { api } = loadClConfigApi();
  const disabled = api.normalize({
    schema_version: 9,
    center_control_all: false,
    center_1m: true,
    center_5m: false,
    center_30m: true,
    center_d: false,
  }, '5');

  assert.equal(disabled.center_control_all, false);
  assert.equal(disabled.center_1m, true);
  assert.equal(disabled.center_5m, false);
  assert.equal(disabled.center_30m, true);
  assert.equal(disabled.center_d, false);

  const enabled = api.normalize({ ...disabled, center_control_all: true }, '5');
  assert.equal(enabled.center_control_all, true);
  assert.equal(enabled.center_1m, true);
  assert.equal(enabled.center_5m, false);
  assert.equal(enabled.center_30m, true);
  assert.equal(enabled.center_d, false);
});

test('总开关只 gate 不改写子项偏好', () => {
  const { api } = loadClConfigApi();
  const cfg = { ...api.DEFAULT, center_all: false, center_L1: true };
  assert.equal(api.enabled(cfg, { render_kind: 'formal_center', structural_level: 1 }), false);
  assert.equal(api.enabled(cfg, { render_kind: 'center_preview', structural_level: 1 }), false);
  assert.equal(cfg.center_L1, true);
  cfg.center_all = true;
  assert.equal(api.enabled(cfg, { render_kind: 'formal_center', structural_level: 1 }), true);
  assert.equal(api.enabled(cfg, { render_kind: 'center_preview', structural_level: 1 }), true);
});

test('盘整背驰和趋势背驰按级别独立 gate', () => {
  const { api } = loadClConfigApi();
  const cfg = { ...api.DEFAULT, divergence_consolidation_L2: false, divergence_trend_L2: true };
  assert.equal(api.enabled(cfg, { render_kind: 'strict_divergence', structural_level: 2, kind: 'consolidation' }), false);
  assert.equal(api.enabled(cfg, { render_kind: 'strict_divergence', structural_level: 2, kind: 'trend' }), true);
});

test('v1 显示偏好迁移幂等且不复活旧 key', () => {
  const { api } = loadClConfigApi();
  const legacy = {
    point_confirmed: false,
    center_L1: false,
    trend_L1: true,
    zs_all: true,
    bc_L1: false,
    point_approaching: true,
    recursive_layers: true,
  };
  const once = api.normalize(legacy, '1');
  const twice = api.normalize(once, '1');
  assert.deepEqual(twice, once);
  assert.equal(once.schema_version, 9);
  assert.equal(once.point_all, false);
  assert.equal(once.center_1m, true);
  assert.equal(once.center_5m, true);
  assert.equal(Object.hasOwn(once, 'center_L1'), false);
  assert.equal(once.trend_L1, true);
  assert.equal(once.trend_all, true);
  for (const oldKey of ['zs_all', 'bc_L1', 'point_approaching', 'recursive_layers']) {
    assert.equal(Object.hasOwn(once, oldKey), false);
  }
});
