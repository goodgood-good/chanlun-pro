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

function response({
  times,
  prices,
  bis = [],
  update = false,
  strictMode,
  strictStructure,
}) {
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
    ...(strictMode ? { strict_structure_mode: strictMode } : {}),
    ...(strictStructure ? { strict_structure: strictStructure } : {}),
  };
}

const SYMBOL = 'a:sh.513100';
const RESOLUTION = '5';
const BASE_PARAMS = { symbol: SYMBOL, resolution: RESOLUTION };
const RESULT_KEY = SYMBOL.toLowerCase() + RESOLUTION.toLowerCase();

function initialBuildFixture(history) {
  const preview = {
    ...response({ times: [1000, 1500, 2000], prices: [10, 12, 13], strictMode: 'unavailable' }),
    initial_structure_build_id: 'build-one',
    strict_structure_error: { code: 'strict_structure_pending' },
  };
  history.applyChanlunUpdate(preview, BASE_PARAMS);
  const patch = {
    schema: 'chanlun-structure-patch-v1', build_id: 'build-one',
    source_count: 3, source_first: 1000, source_last: 2000, bar_time_label: 'end',
    fxs: [], bis: [segment(1000, 10, 2000, 13, 0)], xds: [],
    strict_structure_mode: 'replace',
    strict_structure: { schema: 'chanlun-chart-structure', source_closed_at: 2000 },
  };
  for (const column of ['macd_dif', 'macd_dea', 'macd_hist',
    'higher_macd_dif', 'higher_macd_dea', 'higher_macd_hist']) patch[column] = [null, 0, 1];
  return { preview, patch };
}

test('completed build installs structure and indicators without replacing candles or time coordinates', () => {
  const history = makeDatafeed()._historyProvider;
  const { preview, patch } = initialBuildFixture(history);
  const before = history.bars_result.get(RESULT_KEY);
  assert.equal(history.applyStructurePatch(patch, BASE_PARAMS), true);
  const after = history.bars_result.get(RESULT_KEY);
  assert.strictEqual(after.bars, before.bars);
  assert.strictEqual(after.times, before.times);
  assert.equal(after.strict_structure_mode, 'replace');
  assert.equal(after.bis.length, 1);
  assert.ok(Number.isNaN(after.higher_macd_dif[0]));
  assert.deepEqual(after.higher_macd_dif.slice(1), [0, 1]);
  assert.equal(after.initial_structure_build_id, undefined);
  // A slower request carrying the original preview must not undo completion.
  history.applyChanlunUpdate(preview, BASE_PARAMS);
  assert.strictEqual(history.bars_result.get(RESULT_KEY), after);
});

test('A-share structure patches use the same implicit closing labels as the initial history response', () => {
  const history = makeDatafeed()._historyProvider;
  const { patch } = initialBuildFixture(history);
  delete patch.bar_time_label;
  const before = history.bars_result.get(RESULT_KEY);
  assert.equal(before.bar_time_label, 'end');
  assert.equal(history.applyStructurePatch(patch, BASE_PARAMS), true);
  const after = history.bars_result.get(RESULT_KEY);
  assert.strictEqual(after.times, before.times);
  assert.strictEqual(after.bars, before.bars);
  assert.equal(after.strict_structure_mode, 'replace');
});

test('US patches still require explicit matching time labels and reject invalid labels atomically', () => {
  const history = makeDatafeed()._historyProvider;
  const { preview, patch } = initialBuildFixture(history);
  const params = { symbol: 'us:QQQ.US', resolution: '5' };
  history.applyChanlunUpdate({ ...preview, bar_time_label: 'start' }, params);
  const before = history.bars_result.get('us:qqq.us5');
  for (const label of [undefined, null, 'invalid', 'end']) {
    assert.equal(history.applyStructurePatch({ ...patch, bar_time_label: label }, params), false);
    assert.strictEqual(history.bars_result.get('us:qqq.us5'), before);
  }
  assert.equal(history.applyStructurePatch({ ...patch, bar_time_label: 'start' }, params), true);
});

test('a patch for another build, changed candle frame or truncated indicator is rejected atomically', () => {
  for (const change of [
    { build_id: 'older-build' }, { source_count: 2 }, { source_last: 1999 },
    { bar_time_label: 'start' }, { macd_hist: [1] }, { strict_structure_mode: 'unchanged' },
  ]) {
    const history = makeDatafeed()._historyProvider;
    const { patch } = initialBuildFixture(history);
    const before = history.bars_result.get(RESULT_KEY);
    assert.equal(history.applyStructurePatch({ ...patch, ...change }, BASE_PARAMS), false);
    assert.strictEqual(history.bars_result.get(RESULT_KEY), before);
    assert.equal(before.strict_structure_error.code, 'strict_structure_pending');
  }
});

test('a candle update invalidates the previous initial build even if its last timestamp is unchanged', () => {
  const history = makeDatafeed()._historyProvider;
  const { patch } = initialBuildFixture(history);
  history.applyChanlunUpdate(response({ times: [2000], prices: [14], update: true }), BASE_PARAMS);
  assert.equal(history.applyStructurePatch(patch, BASE_PARAMS), false);
  assert.equal(history.bars_result.get(RESULT_KEY).bars.at(-1).close, 14);
});

test('indicator columns share identical ordering across overlap, duplicate timestamps and missing new values', () => {
  const history = makeDatafeed()._historyProvider;
  const columns = ['macd_dif', 'macd_dea', 'macd_hist',
    'higher_macd_dif', 'higher_macd_dea', 'higher_macd_hist'];
  const first = response({ times: [1000, 2000], prices: [10, 20] });
  const next = response({ times: [500, 1000, 1000, 2500], prices: [5, 11, 12, 25], update: true });
  columns.forEach((column, i) => {
    first[column] = [i + 1, i + 2];
    next[column] = [i + 3, i + 4, null, i + 5];
  });
  history.applyChanlunUpdate(first, BASE_PARAMS);
  history.applyChanlunUpdate(next, BASE_PARAMS);
  const result = history.bars_result.get(RESULT_KEY);
  assert.deepEqual(result.times, [500000, 1000000, 2000000, 2500000]);
  columns.forEach((column, i) => {
    assert.deepEqual(result[column], [i + 3, NaN, i + 2, i + 5]);
  });
});

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
  assert.ok(times.includes((500 - 300) * 1000));
  assert.ok(times.includes((5500 - 300) * 1000));
  // Strict/MACD identity keeps the unmodified market-close timestamps while
  // only the TradingView Bar coordinates use the interval opening time.
  const rawTimes = history.bars_result.get(RESULT_KEY).times;
  assert.ok(rawTimes.includes(500 * 1000));
  assert.ok(rawTimes.includes(5500 * 1000));
});

test('backward unchanged delta preserves an unconsumed atomic strict snapshot', () => {
  const history = makeDatafeed()._historyProvider;
  const snapshot = {
    schema: 'chanlun-chart-structure',
    source_closed_at: 5500,
    render_revision: 'strict-initial',
  };

  history.applyChanlunUpdate(
    response({
      times: [4000, 5000, 5500],
      prices: [18, 20, 22],
      strictMode: 'replace',
      strictStructure: snapshot,
    }),
    { ...BASE_PARAMS, from: 0, to: 5500, firstDataRequest: 'true' },
  );
  history.applyChanlunUpdate(
    response({
      times: [500, 900, 1000],
      prices: [9, 10, 10],
      update: true,
      strictMode: 'unchanged',
    }),
    { ...BASE_PARAMS, from: 0, to: 3000, firstDataRequest: 'false' },
  );

  const stored = history.bars_result.get(RESULT_KEY);
  assert.equal(stored.strict_structure_mode, 'replace');
  assert.equal(stored.strict_structure.render_revision, 'strict-initial');
});

test('unchanged realtime tail does not present a stale snapshot as replace', () => {
  const history = makeDatafeed()._historyProvider;
  const snapshot = {
    schema: 'chanlun-chart-structure',
    source_closed_at: 5500,
    render_revision: 'strict-initial',
  };

  history.applyChanlunUpdate(
    response({
      times: [4000, 5000, 5500],
      prices: [18, 20, 22],
      strictMode: 'replace',
      strictStructure: snapshot,
    }),
    { ...BASE_PARAMS, from: 0, to: 5500, firstDataRequest: 'true' },
  );
  history.applyChanlunUpdate(
    response({
      times: [6000],
      prices: [23],
      update: true,
      strictMode: 'unchanged',
    }),
    { ...BASE_PARAMS, from: 6000, to: 6000, firstDataRequest: 'false' },
  );

  const stored = history.bars_result.get(RESULT_KEY);
  assert.equal(stored.strict_structure_mode, 'unchanged');
  assert.equal(stored.strict_structure.render_revision, 'strict-initial');
});
