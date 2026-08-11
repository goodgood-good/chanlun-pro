'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadDatafeeds() {
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: () => Promise.reject(new Error('no network in test')),
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, { filename: 'bundle.js' });
  return sandbox.Datafeeds;
}

function makeDatafeed() {
  const { UDFCompatibleDatafeed } = loadDatafeeds();
  return new UDFCompatibleDatafeed('http://test-datafeed');
}

function segment(headTime, headPrice, tailTime, tailPrice, linestyle) {
  return {
    linestyle: String(linestyle),
    points: [
      { time: headTime, price: headPrice },
      { time: tailTime, price: tailPrice },
    ],
  };
}

function response({ times, prices, bis = [], update = false }) {
  return {
    s: 'ok',
    update,
    t: times,
    c: prices,
    o: prices,
    h: prices,
    l: prices,
    v: prices.map(() => 0),
    fxs: [],
    bis,
    xds: [],
  };
}

const SYMBOL = 'a:sh.513100';
const RESOLUTION = '5';
const BASE_PARAMS = { symbol: SYMBOL, resolution: RESOLUTION };
const RESULT_KEY = SYMBOL.toLowerCase() + RESOLUTION.toLowerCase();

test('authoritative current window removes a disproved completed stroke', () => {
  const history = makeDatafeed()._historyProvider;
  const removed = segment(1000, 10, 1500, 12, 0);
  const current = segment(1800, 11, 2500, 14, 1);

  history.applyChanlunUpdate(
    response({ times: [1000, 1500, 2000], prices: [10, 12, 13], bis: [removed] }),
    { ...BASE_PARAMS, from: 0, to: 2000, firstDataRequest: 'true' },
  );
  history.applyChanlunUpdate(
    response({ times: [2000, 2500], prices: [13, 14], bis: [current], update: true }),
    { ...BASE_PARAMS, from: 500, to: 2500, firstDataRequest: 'false' },
  );

  const heads = history.bars_result.get(RESULT_KEY).bis.map((item) => item.points[0].time);
  assert.deepStrictEqual(heads, [1800]);
});

test('backward history merge preserves recent strokes and adds older strokes', () => {
  const history = makeDatafeed()._historyProvider;
  const recent = segment(5000, 20, 5500, 22, 0);
  const older = segment(500, 9, 900, 10, 0);

  history.applyChanlunUpdate(
    response({ times: [4000, 5000, 5500], prices: [18, 20, 22], bis: [recent] }),
    { ...BASE_PARAMS, from: 0, to: 5500, firstDataRequest: 'true' },
  );
  history.applyChanlunUpdate(
    response({ times: [500, 900, 1000], prices: [9, 10, 10], bis: [older], update: true }),
    { ...BASE_PARAMS, from: 0, to: 3000, firstDataRequest: 'false' },
  );

  const heads = history.bars_result.get(RESULT_KEY).bis.map((item) => item.points[0].time);
  assert.deepStrictEqual(heads.sort((a, b) => a - b), [500, 5000]);
});

test('an update without window bounds cannot remove existing strokes', () => {
  const history = makeDatafeed()._historyProvider;
  const existing = segment(1000, 10, 1500, 12, 0);

  history.applyChanlunUpdate(
    response({ times: [1000, 1500, 2000], prices: [10, 12, 13], bis: [existing] }),
    { ...BASE_PARAMS, from: 0, to: 2000, firstDataRequest: 'true' },
  );
  assert.doesNotThrow(() => {
    history.applyChanlunUpdate(
      response({ times: [2000, 2500], prices: [13, 14], update: true }),
      BASE_PARAMS,
    );
  });

  const heads = history.bars_result.get(RESULT_KEY).bis.map((item) => item.points[0].time);
  assert.deepStrictEqual(heads, [1000]);
});

test('backward history merge preserves recent bars while adding older bars', () => {
  const history = makeDatafeed()._historyProvider;

  history.applyChanlunUpdate(
    response({ times: [4000, 5000, 5500], prices: [18, 20, 22] }),
    { ...BASE_PARAMS, from: 0, to: 5500, firstDataRequest: 'true' },
  );
  history.applyChanlunUpdate(
    response({ times: [500, 900, 1000], prices: [9, 10, 10], update: true }),
    { ...BASE_PARAMS, from: 0, to: 3000, firstDataRequest: 'false' },
  );

  const times = history.bars_result.get(RESULT_KEY).bars.map((bar) => bar.time);
  assert.ok(times.includes(500 * 1000));
  assert.ok(times.includes(5500 * 1000));
});
