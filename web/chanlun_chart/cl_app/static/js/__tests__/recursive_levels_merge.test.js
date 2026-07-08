'use strict';
// R1-C7 回归: update 合并分支曾无条件整体替换 recursive_levels —— 向左滚动(backward)
// 响应把右侧最新高级别买卖点/背驰 clobber 成老窗口版本(通常为空), 前端 reconcile 随即
// 把已画的高级别箭头/标签/背驰气泡从图上删除(R11 BUG2 只修顶层 mmds/bcs 的兄弟盲区)。
// 修复口径: full_snapshot 整体替换; 非快照按 level 键合并, 嵌套 mmds/bcs 走
// updateTextPoints 同口径(backward 只增不删 / 右侧权威窗内被证伪的剔除),
// zss/zslx_lines 等后端全局透传字段以新响应为准。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeeds() {
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: () => Promise.reject(new Error('no network in test')),
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
  };
  sb.globalThis = sb; sb.self = sb; sb.window = sb;
  vm.createContext(sb);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  const src = fs.readFileSync(bundlePath, 'utf8');
  vm.runInContext(src, sb, { filename: 'bundle.js' });
  return sb.Datafeeds;
}

function makeDatafeed() {
  const { UDFCompatibleDatafeed } = loadDatafeeds();
  return new UDFCompatibleDatafeed('http://test-datafeed');
}

function tp(time, price, text) {
  return { points: { time, price }, text: text || 'buy' };
}

function seg(headTime, headPrice, tailTime, tailPrice, linestyle) {
  return {
    linestyle: String(linestyle),
    points: [
      { time: headTime, price: headPrice },
      { time: tailTime, price: tailPrice },
    ],
  };
}

function levelObj(level, mmds, bcs, zss) {
  return { level, zss: zss || [], zslx_lines: [], mmds: mmds || [], bcs: bcs || [] };
}

function baseResp(update, times, prices, rl) {
  return {
    s: 'ok', update,
    t: times, c: prices, o: prices, h: prices, l: prices, v: prices.map(() => 0),
    fxs: [], bis: [], xds: [], bi_zss: [], xd_zss: [], bcs: [], mmds: [],
    recursive_levels: rl,
  };
}

const SYMBOL = 'a:sh.513100';
const RESOLUTION = '5';
const BASE = { symbol: SYMBOL, resolution: RESOLUTION };
const RES_KEY = SYMBOL.toLowerCase() + RESOLUTION.toLowerCase();

test('向左滚动不应clobber右侧最新高级别买卖点/背驰', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  hp.applyChanlunUpdate(
    baseResp(false, [4000, 5000, 5500], [18, 20, 22],
      [levelObj(1, [tp(5000, 20, '3buy')], [tp(5500, 22, 'pz')])]),
    Object.assign({}, BASE, { from: 0, to: 5500, firstDataRequest: 'true' })
  );
  hp.applyChanlunUpdate(
    baseResp(true, [500, 900], [9, 10], [levelObj(1, [], [])]),
    Object.assign({}, BASE, { from: 0, to: 3000, firstDataRequest: 'false' })
  );
  const lv = hp.bars_result.get(RES_KEY).recursive_levels;
  assert.strictEqual(lv.length, 1);
  const mmdTimes = (lv[0].mmds || []).map((p) => p.points.time);
  const bcTimes = (lv[0].bcs || []).map((p) => p.points.time);
  assert.ok(mmdTimes.includes(5000), `backward不应清掉L1最新买卖点5000, 实际=${JSON.stringify(mmdTimes)}`);
  assert.ok(bcTimes.includes(5500), `backward不应清掉L1最新背驰5500, 实际=${JSON.stringify(bcTimes)}`);
});

test('右侧权威窗口内被证伪的高级别买卖点应被剔除(幽灵对称语义)', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  hp.applyChanlunUpdate(
    baseResp(false, [4000, 5000, 5500], [18, 20, 22],
      [levelObj(1, [tp(5000, 20, '3buy')], [tp(5500, 22, 'pz')])]),
    Object.assign({}, BASE, { from: 0, to: 5500, firstDataRequest: 'true' })
  );
  hp.applyChanlunUpdate(
    baseResp(true, [5500, 6000], [22, 23], [levelObj(1, [], [])]),
    Object.assign({}, BASE, { from: 4500, to: 6000, firstDataRequest: 'false' })
  );
  const lv = hp.bars_result.get(RES_KEY).recursive_levels;
  assert.deepStrictEqual((lv[0].mmds || []).map((p) => p.points.time), [],
    '权威窗内未被新响应提及的高级别买卖点应剔除');
  assert.deepStrictEqual((lv[0].bcs || []).map((p) => p.points.time), [],
    '权威窗内未被新响应提及的高级别背驰应剔除');
});

test('zss等透传字段以新响应为准且不影响mmds合并', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  hp.applyChanlunUpdate(
    baseResp(false, [4000, 5000, 5500], [18, 20, 22],
      [levelObj(1, [tp(5000, 20, '3buy')], [], [seg(4000, 18, 5000, 20, 0)])]),
    Object.assign({}, BASE, { from: 0, to: 5500, firstDataRequest: 'true' })
  );
  hp.applyChanlunUpdate(
    baseResp(true, [500, 900], [9, 10],
      [levelObj(1, [], [], [seg(500, 9, 900, 10, 0)])]),
    Object.assign({}, BASE, { from: 0, to: 3000, firstDataRequest: 'false' })
  );
  const lv = hp.bars_result.get(RES_KEY).recursive_levels;
  const zsHeads = (lv[0].zss || []).map((s) => s.points[0].time);
  assert.deepStrictEqual(zsHeads, [500], 'zss 全局透传字段应以新响应为准');
  const mmdTimes = (lv[0].mmds || []).map((p) => p.points.time);
  assert.ok(mmdTimes.includes(5000), 'zss 替换不应影响 mmds 的合并保留');
});