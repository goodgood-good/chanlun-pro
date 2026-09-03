'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeHistoryProvider() {
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise,
    Error, Map, Set,
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
  const bundlePath = path.join(
    __dirname,
    '..',
    '..',
    'datafeeds',
    'udf',
    'dist',
    'bundle.js',
  );
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, {
    filename: 'bundle.js',
  });
  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed(
    'http://test-datafeed',
  );
  return datafeed._historyProvider;
}

function response(mode, strictStructure, update = false) {
  const payload = {
    s: 'ok',
    update,
    t: [1000],
    c: [10],
    o: [10],
    h: [10],
    l: [10],
    v: [1],
    fxs: [],
    bis: [],
    xds: [],
    strict_structure_mode: mode,
  };
  if (strictStructure !== undefined) {
    payload.strict_structure = strictStructure;
  }
  if (mode === 'unavailable') {
    payload.strict_structure_error = { code: 'strict_evidence_invalid' };
  }
  return payload;
}

const PARAMS = {
  symbol: 'a:sh.600519',
  resolution: '5',
  from: 0,
  to: 1000,
  firstDataRequest: 'true',
};
const KEY = 'a:sh.6005195';

test('history unchanged preserves the snapshot without replaying replace mode', () => {
  const hp = makeHistoryProvider();
  const first = {
    schema: 'chanlun-chart-structure',
    render_revision: 'sha256:render-1',
  };
  hp.applyChanlunUpdate(response('replace', first), PARAMS);
  hp.applyChanlunUpdate(
    response('unchanged', undefined, true),
    { ...PARAMS, firstDataRequest: 'false', from: 100, to: 500 },
  );

  const stored = hp.bars_result.get(KEY);
  assert.equal(stored.strict_structure_mode, 'unchanged');
  assert.equal(stored.strict_structure.render_revision, 'sha256:render-1');
});

test('replace swaps the whole strict object and unavailable clears it', () => {
  const hp = makeHistoryProvider();
  hp.applyChanlunUpdate(
    response('replace', {
      schema: 'chanlun-chart-structure',
      render_revision: 'sha256:render-1',
    }),
    PARAMS,
  );
  hp.applyChanlunUpdate(
    response('replace', {
      schema: 'chanlun-chart-structure',
      render_revision: 'sha256:render-2',
    }, true),
    { ...PARAMS, firstDataRequest: 'false' },
  );
  assert.equal(
    hp.bars_result.get(KEY).strict_structure.render_revision,
    'sha256:render-2',
  );

  hp.applyChanlunUpdate(
    response('unavailable', undefined, true),
    { ...PARAMS, firstDataRequest: 'false' },
  );
  const stored = hp.bars_result.get(KEY);
  assert.equal(stored.strict_structure_mode, 'unavailable');
  assert.equal(stored.strict_structure, undefined);
  assert.equal(
    stored.strict_structure_error.code,
    'strict_evidence_invalid',
  );
});

test('embedded numeric deltas restore exact history and MACD columns', () => {
  const hp = makeHistoryProvider();
  const payload = response('unchanged', undefined, false);
  Object.assign(payload, {
    time_delta: true,
    t: [1000, 60, 60, 60],
    c: [10, 11, 12, 13],
    o: [10, 11, 12, 13],
    h: [10, 11, 12, 13],
    l: [10, 11, 12, 13],
    v: [1, 2, 3, 4],
    macd_delta_scale: 1_000_000,
    macd_dif: [null, 123456, -3456, -220000],
    macd_dea: [0, 100000, 100000, -400000],
    macd_hist: [0, -100000, 200000, -100000],
    higher_macd_dif: [0, 10000, 10000, 10000],
    higher_macd_dea: [0, 20000, 20000, 20000],
    higher_macd_hist: [0, -10000, -10000, -10000],
  });

  hp.applyChanlunUpdate(payload, {
    ...PARAMS,
    to: 1180,
  });

  const stored = hp.bars_result.get(KEY);
  assert.deepEqual(Array.from(stored.times), [1000000, 1060000, 1120000, 1180000]);
  assert.equal(Number.isNaN(stored.macd_dif[0]), true);
  assert.deepEqual(Array.from(stored.macd_dif.slice(1)), [0.123456, 0.12, -0.1]);
  assert.deepEqual(Array.from(stored.higher_macd_hist), [0, -0.01, -0.02, -0.03]);
});
