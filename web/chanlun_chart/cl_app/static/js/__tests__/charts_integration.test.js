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
    Utils: { get_market: () => 'a' },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map(), activeManagerId: null }),
    Datafeeds: { UDFCompatibleDatafeed: function () {} },
    TradingView: { widget: function () {} },
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    navigator: { onLine: true },
    location: { reload: () => {} },
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
  vm.runInContext(src, sb, { filename: 'charts.js' });
  return { ChartManager: sb.__CM_EXPORT, ChartUtils: sb.__CU_EXPORT, sb };
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

function spyWidget() {
  const calls = { resetCache: 0, resetData: 0 };
  const widget = {
    resetCache: () => { calls.resetCache++; },
    activeChart: () => ({ resetData: () => { calls.resetData++; } }),
  };
  return { widget, calls };
}

test('vm 能加载真实 charts.js 并取出 ChartManager', () => {
  const { ChartManager } = loadChartManager();
  assert.ok(ChartManager, 'ChartManager 应被加载');
  assert.equal(typeof ChartManager.prototype._doReset, 'function');
  assert.equal(typeof ChartManager.prototype._getViewLatestSec, 'function');
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

test('_doReset: widget 无 resetCache → 降级裸 resetData(容错)', () => {
  const { ChartManager, sb } = loadChartManager();
  let resetDataCalls = 0;
  const widget = { activeChart: () => ({ resetData: () => { resetDataCalls++; } }) }; // 无 resetCache
  const cm = makeManager(ChartManager, widget);
  sb._setNowSec(10000);
  const did = cm._doReset('k', 'reconnect', 5000);
  assert.equal(did, true);
  assert.equal(resetDataCalls, 1, '无 resetCache 仍执行 resetData');
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

function openStream(cm, sb) {
  cm._sse = null; cm._sseGotData = false; cm._sseHealthTimer = null; cm._sseFallbackInterval = null;
  cm.widget.symbolInterval = () => ({ symbol: 'a:SH.000001', interval: '5' });
  const applyCalls = [], feedCalls = [];
  cm.udf_datafeed._historyProvider.applyChanlunUpdate = (data) => { applyCalls.push(data); };
  cm.udf_datafeed.feedRealtimeBar = (k) => { feedCalls.push(k); };
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

// ── 买卖点偏移方向(前端H1: 买点系统性画错边) ──
// 设计意图(charts.js:306 注释): 买点放 K 线下方(price-off)、卖点放上方(price+off)。
// 默认 branch_core 路径买卖点文本是小写 bs_type(1buy/3buy/类1buy), 偏移判定必须与
// 颜色/箭头(toLowerCase().includes("b"))同口径, 否则买点被画到上方与卖点同侧。
test('mmdOffsetPoint: 买点(默认小写)下移 price<锚点, 卖点上移 price>锚点', () => {
  const { ChartUtils } = loadChartManager();
  assert.ok(ChartUtils && typeof ChartUtils.mmdOffsetPoint === 'function', 'ChartUtils 应可加载');
  const mk = (text) => ({ text, points: { price: 100, time: 1000 } });
  // off = |100| * 0.01 = 1 (offsetBase=0 → 走 priceRatioFallback)
  const buy = ChartUtils.mmdOffsetPoint(mk('1buy'), 0.8, 0.01);
  assert.ok(buy.price < 100, `买点应下移(<100), 实际 ${buy.price}`);
  const sell = ChartUtils.mmdOffsetPoint(mk('1sell'), 0.8, 0.01);
  assert.ok(sell.price > 100, `卖点应上移(>100), 实际 ${sell.price}`);
});

test('mmdOffsetPoint: 买点全变体(1buy/2buy/3buy/类1buy/大写1B/3B)都下移', () => {
  const { ChartUtils } = loadChartManager();
  const mk = (text) => ({ text, points: { price: 100, time: 1000 } });
  for (const t of ['1buy', '2buy', '3buy', '类1buy', '1B', '3B']) {
    const pt = ChartUtils.mmdOffsetPoint(mk(t), 0.8, 0.01);
    assert.ok(pt.price < 100, `买点 ${t} 应下移(<100), 实际 ${pt.price}`);
  }
});

test('mmdOffsetPoint: 卖点全变体(1sell/2sell/3sell/类1sell/大写1S/3S)都上移', () => {
  const { ChartUtils } = loadChartManager();
  const mk = (text) => ({ text, points: { price: 100, time: 1000 } });
  for (const t of ['1sell', '2sell', '3sell', '类1sell', '1S', '3S']) {
    const pt = ChartUtils.mmdOffsetPoint(mk(t), 0.8, 0.01);
    assert.ok(pt.price > 100, `卖点 ${t} 应上移(>100), 实际 ${pt.price}`);
  }
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
