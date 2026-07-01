'use strict';
// history-provider 轮询合并回归测试(2026-07-01 前端实时显示审查发现)：TV 30s 轮询响应
// (update=true 且无 full_snapshot)对被后端证伪的"已完成"笔/线段/中枢等形态只增不删
// (updateLineSegments 按起点 key 做 upsert)。SSE 推送带 full_snapshot 会整体替换、不受
// 影响；但纯轮询/SSE 关闭的降级路径下，已作废的形态会永久"幽灵"驻留，直到手动刷新。
// 用 vm 加载真实 dist/bundle.js(UMD, browser-global 分支)，经 applyChanlunUpdate
// (SSE 复用入口，跳过网络请求，但与 getBars 共享同一份 _processHistoryResponse 合并逻辑)
// 直接喂两次 response，断言 bars_result 里的形态数组是否符合预期。
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

function seg(headTime, headPrice, tailTime, tailPrice, linestyle) {
  return {
    linestyle: String(linestyle),
    points: [
      { time: headTime, price: headPrice },
      { time: tailTime, price: tailPrice },
    ],
  };
}

function mmd(time, price, text) {
  return { points: { time, price }, text: text || 'buy' };
}

function fullResponse(times, prices, bis, mmds) {
  return {
    s: 'ok', update: false,
    t: times, c: prices, o: prices, h: prices, l: prices, v: prices.map(() => 0),
    fxs: [], bis: bis, xds: [], bi_zss: [], xd_zss: [], bcs: [], mmds: mmds || [],
  };
}

function pollResponse(times, prices, bis, mmds) {
  return {
    s: 'ok', update: true,
    t: times, c: prices, o: prices, h: prices, l: prices, v: prices.map(() => 0),
    fxs: [], bis: bis, xds: [], bi_zss: [], xd_zss: [], bcs: [], mmds: mmds || [],
  };
}

test('30s 轮询窗口内被证伪的已完成笔应被移除(幽灵形态复现回归)', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  // 初始全量加载：K线到 t=2000s，含一根已完成笔 X(起点 t=1000)
  const ghostSeg = seg(1000, 10, 1500, 12, 0);
  hp.applyChanlunUpdate(
    fullResponse([1000, 1500, 2000], [10, 12, 13], [ghostSeg]),
    Object.assign({}, base, { from: 0, to: 2000, firstDataRequest: 'true' })
  );

  // 30s 轮询(右侧最新窗口, to=2500 >= 已知最新K线时间 2000)：后端重算后 X 已不在
  // 响应里(起点被新行情证伪)，换成另一根笔 Y(起点 t=1800)。
  const freshSeg = seg(1800, 11, 2500, 14, 1);
  hp.applyChanlunUpdate(
    pollResponse([2000, 2500], [13, 14], [freshSeg]),
    Object.assign({}, base, { from: 500, to: 2500, firstDataRequest: 'false' })
  );

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const bis = hp.bars_result.get(resKey).bis;
  const heads = bis.map((s) => s.points[0].time);
  assert.ok(!heads.includes(1000), `幽灵笔起点 1000 不应残留，实际 heads=${JSON.stringify(heads)}`);
  assert.ok(heads.includes(1800), '新笔起点 1800 应存在');
});

test('向左滚动的历史响应不应误删右侧现有形态', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  // 初始全量加载：最新笔 X 起点在 t=5000(较新/靠右)
  const recentSeg = seg(5000, 20, 5500, 22, 0);
  hp.applyChanlunUpdate(
    fullResponse([4000, 5000, 5500], [18, 20, 22], [recentSeg]),
    Object.assign({}, base, { from: 0, to: 5500, firstDataRequest: 'true' })
  );

  // 向左滚动(backward)：请求更早历史 to=3000 < 已知最新K线时间(5500)，
  // 响应只含更早的一根笔 Z(起点 t=500)，不含 X(本就不在这段更早窗口内)。
  const olderSeg = seg(500, 9, 900, 10, 0);
  hp.applyChanlunUpdate(
    pollResponse([500, 900, 1000], [9, 10, 10], [olderSeg]),
    Object.assign({}, base, { from: 0, to: 3000, firstDataRequest: 'false' })
  );

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const bis = hp.bars_result.get(resKey).bis;
  const heads = bis.map((s) => s.points[0].time);
  assert.ok(heads.includes(5000), `向左滚动不应误删右侧现有笔 5000，实际 heads=${JSON.stringify(heads)}`);
  assert.ok(heads.includes(500), '更早的新笔 500 应被加入');
});

test('缺少已知K线时间(退化态)不应被当作权威窗口而误删已有形态', () => {
  // 复核发现(独立代码审查): existingMaxBarMs===undefined(obj_res 存在但 bars 为空,
  // 理论上不应正常出现的退化态)不应默认判定为"权威窗口"——没有K线证据时,更保守的
  // 默认是不做窗口删除,而不是假定这次响应权威。
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  // 人为构造退化态：K线为空但笔数组非空(真实后端不会产生,此处用于钉死防御性默认值)。
  const staleSeg = seg(1000, 10, 1500, 12, 0);
  hp.applyChanlunUpdate(
    fullResponse([], [], [staleSeg]),
    Object.assign({}, base, { from: 0, to: 2000, firstDataRequest: 'true' })
  );

  // 轮询响应不再提及该笔, 且不带 full_snapshot。
  hp.applyChanlunUpdate(
    pollResponse([2000, 2500], [13, 14], []),
    Object.assign({}, base, { from: 500, to: 2500, firstDataRequest: 'false' })
  );

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const bis = hp.bars_result.get(resKey).bis;
  const heads = bis.map((s) => s.points[0].time);
  assert.ok(heads.includes(1000), `无K线证据时不应误删已有形态,实际 heads=${JSON.stringify(heads)}`);
});

test('真实SSE生产调用签名(仅 {symbol,resolution},无 from/to)不应崩溃也不应误删', () => {
  // 复核发现(独立代码审查,3路收敛): 生产唯一的 applyChanlunUpdate 调用点
  // (charts.js:2519 hp.applyChanlunUpdate(data, { symbol, resolution }))不传 from/to。
  // 当前生产该路径恒带 full_snapshot 整体替换、这条窗口删除逻辑对它是 inert 的——
  // 但仍需确认"inert"是安全的静默跳过,而不是崩溃或误删(为将来可能出现的
  // 非 full_snapshot 场景兜底)。
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  const existingSeg = seg(1000, 10, 1500, 12, 0);
  hp.applyChanlunUpdate(
    fullResponse([1000, 1500, 2000], [10, 12, 13], [existingSeg]),
    Object.assign({}, base, { from: 0, to: 2000, firstDataRequest: 'true' })
  );

  // 真实生产签名：无 from/to，且这次响应不再提及 existingSeg、也不带 full_snapshot。
  assert.doesNotThrow(() => {
    hp.applyChanlunUpdate(pollResponse([2000, 2500], [13, 14], []), { symbol, resolution });
  }, '缺 from/to 时不应抛异常');

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const bis = hp.bars_result.get(resKey).bis;
  const heads = bis.map((s) => s.points[0].time);
  assert.ok(
    heads.includes(1000),
    `缺 from/to 时无法判断是否权威窗口,应保守不删,实际 heads=${JSON.stringify(heads)}`
  );
});

test('30s 轮询窗口内被证伪的买卖点(单点形态)应被移除(幽灵买卖点回归)', () => {
  // 复核发现(独立代码审查): updateTextPoints(fxs/bcs/mmds 等单点形态)只对
  // "新响应非空"的情形做了按时间分区的隐式删除, 新响应为空时直接原样保留旧点位——
  // 与 updateLineSegments 刚补上的"权威窗口内缺失即删"不对称, 买卖点/背驰这类单点
  // 幽灵形态在此路径下仍会残留。
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  const ghostMmd = mmd(1000, 10, 'buy');
  hp.applyChanlunUpdate(
    fullResponse([1000, 1500, 2000], [10, 12, 13], [], [ghostMmd]),
    Object.assign({}, base, { from: 0, to: 2000, firstDataRequest: 'true' })
  );

  // 30s 轮询(右侧最新窗口, to=2500 >= 已知最新K线时间 2000): 后端重算后该买点已被
  // 证伪撤销, 新响应 mmds 为空。
  hp.applyChanlunUpdate(
    pollResponse([2000, 2500], [13, 14], [], []),
    Object.assign({}, base, { from: 500, to: 2500, firstDataRequest: 'false' })
  );

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const mmds = hp.bars_result.get(resKey).mmds;
  assert.strictEqual(mmds.length, 0, `幽灵买卖点不应残留,实际 mmds=${JSON.stringify(mmds)}`);
});

test('向左滚动时新响应为空不应误删右侧现有买卖点', () => {
  const df = makeDatafeed();
  const hp = df._historyProvider;
  const symbol = 'a:sh.513100';
  const resolution = '5';
  const base = { symbol, resolution };

  const recentMmd = mmd(5000, 20, 'sell');
  hp.applyChanlunUpdate(
    fullResponse([4000, 5000, 5500], [18, 20, 22], [], [recentMmd]),
    Object.assign({}, base, { from: 0, to: 5500, firstDataRequest: 'true' })
  );

  // 向左滚动: to=3000 < 已知最新K线时间(5500), 这段更早窗口本就没有买卖点(mmds=[])。
  hp.applyChanlunUpdate(
    pollResponse([500, 900, 1000], [9, 10, 10], [], []),
    Object.assign({}, base, { from: 0, to: 3000, firstDataRequest: 'false' })
  );

  const resKey = symbol.toLowerCase() + resolution.toLowerCase();
  const mmds = hp.bars_result.get(resKey).mmds;
  assert.strictEqual(mmds.length, 1, `向左滚动不应误删右侧现有买卖点,实际 mmds=${JSON.stringify(mmds)}`);
  assert.strictEqual(mmds[0].points.time, 5000);
});
