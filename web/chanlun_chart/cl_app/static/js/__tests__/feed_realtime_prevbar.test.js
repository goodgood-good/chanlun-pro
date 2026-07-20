'use strict';
// R8-S1: SSE feedRealtimeBar 只喂末根 → 新根出现时刚收盘那根(倒数第二根)的最终 OHLC 被丢弃,
// 已收盘蜡烛永久停在收盘前 ≤8s 旧值(DataPulseProvider 轮询的 previousBar 补发被 SSE 推进的
// lastBarTime 废掉)。修复=feedRealtimeBar 在 t.length>=2 时先喂倒数第二根 finalize 再喂末根。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeeds() {
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: () => Promise.reject(new Error('no network')),
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  };
  sb.globalThis = sb; sb.self = sb; sb.window = sb;
  vm.createContext(sb);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sb, { filename: 'bundle.js' });
  return sb.Datafeeds;
}

test('feedRealtimeBar 新根出现时先 finalize 前一根(不丢刚收盘最终 OHLC)', () => {
  const { UDFCompatibleDatafeed } = loadDatafeeds();
  const df = new UDFCompatibleDatafeed('http://test');
  const received = [];
  const symbolInfo = { ticker: 'A:SH.513100', name: 'A:SH.513100' };
  df.subscribeBars(symbolInfo, '5', (bar) => received.push(bar), 'guid1', () => {});
  const key = symbolInfo.ticker.toLowerCase() + '5';
  // 帧1: T1(5500)成形中, close=22
  df.feedRealtimeBar(key, { t: [4000, 5000, 5500], o: [18, 20, 21], h: [18, 20, 23], l: [18, 20, 20], c: [18, 20, 22], v: [1, 1, 1] });
  // 初次推送时 TV 已可能通过 getBars 渲染到 T1；不能补发更早的 T0，
  // 否则会触发 putToCacheNewBar time violation。
  assert.deepEqual(received.map((bar) => bar.time), [5500 * 1000]);
  // 帧2: 新根 T2(6000)出现, 同帧含 T1 最终值(close 22→25, high 23→25)
  df.feedRealtimeBar(key, { t: [4000, 5000, 5500, 6000], o: [18, 20, 21, 25], h: [18, 20, 25, 26], l: [18, 20, 20, 24], c: [18, 20, 25, 25], v: [1, 1, 5, 1] });
  // 断言: T1(5500)被 finalize 到最终 close=25/high=25, 而非停在帧1的 22/23
  const t1 = received.filter(b => b.time === 5500 * 1000);
  assert.ok(t1.length > 0, 'T1 应被喂过');
  assert.strictEqual(t1[t1.length - 1].close, 25, 'T1 应 finalize close=25(修复前=22)');
  assert.strictEqual(t1[t1.length - 1].high, 25, 'T1 应 finalize high=25(修复前=23)');
  // T2 也应收到
  assert.ok(received.some(b => b.time === 6000 * 1000 && b.close === 25), 'T2 应收到');
});
