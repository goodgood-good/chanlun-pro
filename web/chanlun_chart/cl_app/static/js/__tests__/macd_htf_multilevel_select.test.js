'use strict';
// 多级别图表(同标的、多个不同周期 widget) MACD_HTF 显示错周期的回归测试。
// 根因: getPreferredChartContext 旧逻辑仅按 ticker 取「首个」widget, 其周期若为日/周线,
// interval 修正块会把 5m/30m 等子图的 rawInterval 覆盖成 d/w, 导致这些子图显示日线 HTF。
// 修复: 同标的多 widget 时按「周期等价」挑出本指标 study 自己的那个 widget。
const { test } = require('node:test');
const assert = require('node:assert');
const Idx = require('../tv_indicators/chart_idx_macd_backend.js');
const { pickPreferredWidgetIndex, _resEquiv } = Idx._internal;

test('_resEquiv: 直接相等与日/周/月等价写法', () => {
  assert.equal(_resEquiv('5', '5'), true);
  assert.equal(_resEquiv('30', '30'), true);
  assert.equal(_resEquiv('1d', 'd'), true);
  assert.equal(_resEquiv('1d', '1440'), true);
  assert.equal(_resEquiv('1w', 'w'), true);
  assert.equal(_resEquiv('1m', 'm'), true);   // 月线
  assert.equal(_resEquiv('180', '3h'), true);
  assert.equal(_resEquiv('240', '4h'), true);
  assert.equal(_resEquiv('360', '6h'), true);
  assert.equal(_resEquiv('480', '8h'), true);
  assert.equal(_resEquiv('720', '12h'), true);
  // 不同周期不等价
  assert.equal(_resEquiv('5', '30'), false);
  assert.equal(_resEquiv('5', '1d'), false);
  assert.equal(_resEquiv('30', 'd'), false);
});

test('多级别[1d,30,5]: 5m 图选到自己(index2)而非首个日线', () => {
  const intervals = ['1D', '30', '5'];   // 首个是日线主图
  assert.equal(pickPreferredWidgetIndex(intervals, '5'), 2);
  assert.equal(pickPreferredWidgetIndex(intervals, '30'), 1);
  assert.equal(pickPreferredWidgetIndex(intervals, '1d'), 0);
  assert.equal(pickPreferredWidgetIndex(intervals, 'd'), 0);   // 命名日线等价
});

test('单图: 只有一个 widget 时永远返回它(保留 current system 数字周期修正)', () => {
  // context 传来的数字周期怪癖("1" 实为日线)由单图 fallback + 后续修正处理
  assert.equal(pickPreferredWidgetIndex(['1D'], '1'), 0);
  assert.equal(pickPreferredWidgetIndex(['1D'], '1d'), 0);
  assert.equal(pickPreferredWidgetIndex(['5'], '5'), 0);
});

test('多级别但选不出(全不等价)→ 回退首个, 不抛错', () => {
  assert.equal(pickPreferredWidgetIndex(['1D', '1W'], '5'), 0);
});

test('空列表 → -1', () => {
  assert.equal(pickPreferredWidgetIndex([], '5'), -1);
  assert.equal(pickPreferredWidgetIndex(null, '5'), -1);
});

test('actual MACD study reads the matching close-labeled bar at its opening chart coordinate', () => {
  const oldWindow = global.window;
  const close = Date.UTC(2026, 6, 30, 20);
  const stored = { bar_time_label: 'end', times: [close - 1800000, close],
    macd_dif: [11, 22], macd_dea: [1, 2], macd_hist: [10, 20] };
  const df = { _historyProvider: { bars_result: new Map([['us:aapl.us30', stored]]) } };
  global.window = { ChanlunTVRegistry: { widgets: new Map(), datafeeds: new Map([['chart1', df]]) } };
  try {
    const study = new (Idx.idx({}).constructor)();
    const context = { symbol: { ticker: 'us:AAPL.US', interval: '30', time: close - 1800000 } };
    assert.deepEqual(study.main(context, () => {}), [20, 0, 22, 2]);
    stored.bar_time_label = 'start';
    assert.deepEqual(study.main(context, () => {}), [10, 0, 11, 1]);
    assert.deepEqual(stored.times, [close - 1800000, close]);
  } finally { global.window = oldWindow; }
});

test('MACD scans an unavailable higher column once and picks up a newly installed column', () => {
  const oldWindow = global.window;
  const close = Date.UTC(2026, 6, 30, 20);
  let scanned = 0;
  const unavailable = new Proxy([NaN, NaN], {get(target, key, receiver) {
    if (/^[0-9]+$/.test(String(key))) scanned++;
    return Reflect.get(target, key, receiver);
  }});
  const stored = {bar_time_label:'end', times:[close-1800000, close],
    macd_dif:[11,22], macd_dea:[1,2], macd_hist:[10,20], higher_macd_dif:unavailable};
  const df = {_historyProvider:{bars_result:new Map([['us:aapl.us30', stored]])}};
  global.window = {ChanlunTVRegistry:{widgets:new Map(), datafeeds:new Map([['chart1',df]])}};
  try {
    const study = new (Idx.idx({}).constructor)();
    const context = {symbol:{ticker:'us:AAPL.US', interval:'30', time:close-1800000}};
    for (let i=0; i<50; i++) assert.deepEqual(study.main(context, () => {}), [20,0,22,2]);
    assert.equal(scanned, 2);
    stored.higher_macd_dif = [110,220];
    stored.higher_macd_dea = [10,20];
    stored.higher_macd_hist = [100,200];
    assert.deepEqual(study.main(context, () => {}), [200,0,220,20]);
  } finally { global.window = oldWindow; }
});
