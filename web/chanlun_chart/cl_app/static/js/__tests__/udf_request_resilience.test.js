'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const udfRoot = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'src');

function read(name) {
  return fs.readFileSync(path.join(udfRoot, name), 'utf8');
}

test('Requester aborts a stalled fetch at a bounded deadline and clears its timer', () => {
  const source = read('requester.ts');
  assert.match(source, /AbortController/);
  assert.match(source, /setTimeout\([^]*?\.abort\(\)/);
  assert.match(source, /clearTimeout/);
  assert.match(source, /response\.ok/);
});

test('DataPulseProvider isolates pending work per subscriber and bounds every refresh', () => {
  const source = read('data-pulse-provider.ts');
  assert.match(source, /Set<string>/);
  assert.match(source, /_requestsPending\.has\(listenerGuid\)/);
  assert.match(source, /Promise\.race/);
  assert.match(source, /clearTimeout/);
});

test('cold history gets a 180s deadline while incremental polling stays at 15s', async () => {
  const delays = [];
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: (url) => {
      const body = String(url).includes('/config')
        ? {
            supports_search: true,
            supported_resolutions: ['1', '5', '30', '1D'],
          }
        : {
            s: 'ok', update: false, bar_time_label: 'start',
            t: [1000], o: [1], h: [1], l: [1], c: [1], v: [1],
            fxs: [], bis: [], xds: [],
          };
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(body)),
      });
    },
    setTimeout: (_callback, delay) => {
      delays.push(delay);
      return delays.length;
    },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const bundlePath = path.join(
    __dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js'
  );
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, { filename: 'bundle.js' });

  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed('http://test');
  const history = datafeed._historyProvider;
  delays.length = 0;
  await history.getBars(
    { ticker: 'us:TSLA.US' },
    '1',
    { from: 1000, to: 2000, firstDataRequest: true },
  );
  assert.equal(delays.at(-1), 180_000);

  delays.length = 0;
  await history.getBars(
    { ticker: 'us:TSLA.US' },
    '1',
    { from: 1900, to: 2000, firstDataRequest: false },
  );
  assert.equal(delays.at(-1), 15_000);
});

test('cold history retries one startup timeout and returns the recovered bars', async () => {
  const delays = [];
  let historyAttempts = 0;
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: (url) => {
      if (String(url).includes('/config')) {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify({
            supports_search: true,
            supported_resolutions: ['1', '5', '30', '1D'],
          })),
        });
      }
      historyAttempts += 1;
      if (historyAttempts === 1) {
        return Promise.reject(new Error('Request timed out after 180000ms'));
      }
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({
          s: 'ok', update: false,
          t: [1000], o: [1], h: [1], l: [1], c: [1], v: [1],
          fxs: [], bis: [], xds: [],
        })),
      });
    },
    setTimeout: (callback, delay) => {
      delays.push(delay);
      if (delay === 750) {
        Promise.resolve().then(callback);
      }
      return delays.length;
    },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const bundlePath = path.join(
    __dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js'
  );
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, { filename: 'bundle.js' });

  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed('http://test');
  const result = await datafeed._historyProvider.getBars(
    { ticker: 'a:SZ.301268' },
    '5',
    { from: 1000, to: 2000, firstDataRequest: true },
  );

  assert.equal(historyAttempts, 2);
  assert.equal(delays.filter((delay) => delay === 750).length, 1);
  assert.equal(result.bars.length, 1);
});

test('a superseded initial-history abort settles quietly', async () => {
  const warnings = [];
  let pendingHistoryReject;
  let historyAttempts = 0;
  const sandbox = {
    console: {
      ...console,
      warn: (...args) => warnings.push(args.map(String).join(' ')),
    },
    Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    AbortController,
    fetch: (url, options = {}) => {
      if (String(url).includes('/config')) {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify({
            supports_search: true,
            supported_resolutions: ['1', '5', '30', '1D'],
          })),
        });
      }
      historyAttempts += 1;
      if (historyAttempts === 1) {
        return new Promise((_resolve, reject) => {
          pendingHistoryReject = reject;
          options.signal.addEventListener('abort', () => {
            reject(new Error('signal is aborted without reason'));
          }, { once: true });
        });
      }
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify({
          s: 'ok', update: false,
          t: [2000], o: [2], h: [2], l: [2], c: [2], v: [2],
          fxs: [], bis: [], xds: [],
        })),
      });
    },
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const bundlePath = path.join(
    __dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js'
  );
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, { filename: 'bundle.js' });

  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed('http://test');
  const history = datafeed._historyProvider;
  const staleRequest = history.getBars(
    { ticker: 'a:SZ.000001' },
    '1',
    { from: 1000, to: 2000, firstDataRequest: true },
  );
  const currentRequest = history.getBars(
    { ticker: 'a:SZ.000002' },
    '1',
    { from: 1000, to: 2000, firstDataRequest: true },
  );

  const [staleResult, currentResult] = await Promise.allSettled([
    staleRequest,
    currentRequest,
  ]);
  assert.equal(staleResult.status, 'rejected');
  assert.equal(currentResult.status, 'fulfilled');
  assert.equal(currentResult.value.bars.length, 1);
  assert.equal(historyAttempts, 2);
  assert.equal(pendingHistoryReject !== undefined, true);
  assert.equal(
    warnings.some((message) => message.includes('signal is aborted without reason')),
    false,
  );
});

test('automatic repair asks for fresh data once while manual refresh still forces a fetch', async () => {
  const h = timedBundle();
  const hp = h.datafeed._historyProvider;
  hp._refreshIfStaleOnce = true;
  const first = h.start();
  await h.advance(0);
  assert.equal(new URL(h.attempts[0].url).searchParams.get('refresh_if_stale'), '1');
  assert.equal(new URL(h.attempts[0].url).searchParams.get('force_refresh'), null);
  await h.respond(0);
  assert.equal(first.bars.length, 1);
  assert.equal(first.errors.length, 0);
  hp._forceRefreshOnce = true;
  const second = h.start();
  await h.advance(0);
  assert.equal(new URL(h.attempts[1].url).searchParams.get('refresh_if_stale'), null);
  assert.equal(new URL(h.attempts[1].url).searchParams.get('force_refresh'), '1');
  await h.respond(1);
  assert.equal(second.bars.length, 1);
  assert.equal(second.errors.length, 0);
});

function timedBundle() {
  let now = 0, sequence = 0;
  const timers = new Map(), attempts = [], warnings = [], events = [];
  const setTimer = (callback, delay) => {
    const id = ++sequence;
    timers.set(id, { callback, at: now + delay });
    return id;
  };
  const response = body => ({ ok: true, text: () => Promise.resolve(JSON.stringify(body)) });
  const sandbox = {
    console: { ...console, warn: (...args) => warnings.push(args.map(String).join(' ')) },
    Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set, AbortController,
    Date: class extends Date { static now() { return now; } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    dispatchEvent: event => events.push(event),
    fetch: (url, options = {}) => {
      if (String(url).includes('/config')) return Promise.resolve(response({
        supports_search: true, supported_resolutions: ['1', '5', '30', '1D'],
      }));
      return new Promise((resolve, reject) => {
        const attempt = { url: String(url), started: now, signal: options.signal, resolve, reject };
        attempts.push(attempt);
        options.signal.addEventListener('abort', () => {
          attempt.abortedAt = now;
          reject(new Error('AbortError: request aborted'));
        }, { once: true });
      });
    },
    setTimeout: setTimer, clearTimeout: id => timers.delete(id),
    setInterval: () => 0, clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(udfRoot, '../dist/bundle.js'), 'utf8'), sandbox);
  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed('http://test');
  const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
  return {
    datafeed, attempts, warnings, events, timers,
    async advance(milliseconds) {
      await flush();
      const end = now + milliseconds;
      while (true) {
        const pending = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!pending) break;
        now = pending[1].at;
        timers.delete(pending[0]);
        pending[1].callback();
        await flush();
      }
      now = end;
      await flush();
    },
    async respond(index, time = 1000) {
      attempts[index].resolve(response({
        s: 'ok', update: false,
        t: [time], o: [1], h: [1], l: [1], c: [1], v: [1], fxs: [], bis: [], xds: [],
      }));
      await flush();
    },
    async respondEmpty(index) {
      attempts[index].resolve(response({ s: 'no_data' }));
      await flush();
    },
    start(ticker = 'a:SZ.301004') {
      const result = { bars: [], errors: [] };
      datafeed.getBars({ ticker }, '5', { from: 1000, to: 2000, firstDataRequest: true },
        (bars, meta) => result.bars.push({ bars, meta }), error => result.errors.push(error));
      return result;
    },
  };
}

test('actual bundle accepts the first cold response at 146 seconds without abort or retry', async () => {
  const e = timedBundle(), result = e.start();
  await e.advance(145_999);
  assert.equal(e.attempts.length, 1);
  assert.equal(e.attempts[0].signal.aborted, false);
  assert.equal(result.bars.length, 0);
  await e.advance(1);
  await e.respond(0);
  assert.equal(result.bars.length, 1);
  assert.equal(result.errors.length, 0);
  assert.equal(e.warnings.length, 0);
  assert.equal(e.datafeed._historyProvider.hasPendingHistoryWork(), false);
  assert.equal(e.timers.size, 0);
});

test('actual bundle waits 180 seconds, retries once after 750ms and can accept a slow retry', async () => {
  const e = timedBundle(), result = e.start();
  await e.advance(179_999);
  assert.equal(e.attempts[0].signal.aborted, false);
  await e.advance(1);
  assert.equal(e.attempts[0].abortedAt, 180_000);
  await e.advance(749);
  assert.equal(e.attempts.length, 1);
  await e.advance(1);
  assert.equal(e.attempts.length, 2);
  assert.equal(e.attempts[1].started, 180_750);
  await e.advance(146_000);
  await e.respond(1);
  assert.equal(result.bars.length, 1);
  assert.equal(result.errors.length, 0);
  assert.equal(e.warnings.filter(text => text.includes('retrying once')).length, 1);
  assert.equal(e.timers.size, 0);
});

test('actual bundle reports a bounded failure after two real initial timeouts and never starts a third', async () => {
  const e = timedBundle(), result = e.start();
  await e.advance(360_750);
  assert.equal(e.attempts.length, 2);
  assert.equal(e.attempts.every(attempt => attempt.signal.aborted), true);
  assert.equal(result.bars.length, 0);
  assert.equal(result.errors.length, 1);
  assert.match(String(result.errors[0]), /Request timed out after 180000ms/);
  await e.advance(390_000);
  assert.equal(e.attempts.length, 2);
  assert.equal(e.timers.size, 0);
  assert.equal(e.datafeed._historyProvider.hasPendingHistoryWork(), false);
  assert.equal(e.events.filter(event => event.type === 'chanlun-history-error').length, 1);
  assert.match(e.events.find(event => event.type === 'chanlun-history-error').detail.message, /行情连接/);
});

test('an empty initial history result explains the missing candles without inventing any bars', async () => {
  const e = timedBundle(), result = e.start();
  await e.respondEmpty(0);
  const errors = e.events.filter(event => event.type === 'chanlun-history-error');
  assert.equal(errors.length, 1);
  assert.match(errors[0].detail.message, /暂未获取到 K 线/);
  assert.equal(result.bars[0].bars.length, 0);
  assert.equal(result.bars[0].meta.noData, true);
});

test('switching during the longer cold wait aborts the old request without publishing stale bars or retrying', async () => {
  const e = timedBundle(), stale = e.start('a:SZ.000001');
  await e.advance(100_000);
  const current = e.start('a:SZ.000002');
  await e.advance(146_000);
  assert.equal(e.attempts[0].abortedAt, 100_000);
  await e.respond(1, 2000);
  assert.equal(stale.bars.length, 0);
  assert.equal(current.bars.length, 1);
  assert.equal(current.errors.length, 0);
  assert.equal(e.attempts.length, 2);
  assert.equal(e.events.some(event => event.detail?.symbol === 'a:sz.000001'), false);
  assert.equal(e.events.some(event => event.detail?.symbol === 'a:sz.000002'), true);
  assert.equal(e.datafeed._historyProvider.bars_result.has('a:sz.0000015'), false);
  assert.equal(e.warnings.length, 0);
});

test('a cancelled initial request records a retry for that symbol even if SSE fills its cache', async () => {
  const e = timedBundle(), hp = e.datafeed._historyProvider;
  e.start('a:SZ.000001');
  await e.advance(100);
  e.start('a:SZ.000002');
  await e.advance(0);
  assert.equal(hp.hasInitialHistoryFailure('A:sz.000001', '5'), true);
  assert.equal(hp.hasInitialHistoryFailure('a:SZ.000002', '5'), false);
  hp.applyChanlunUpdate({s:'ok', update:true, t:[1000], c:[1], o:[1], h:[1], l:[1], v:[1],
    fxs:[], bis:[], xds:[]}, {symbol:'a:SZ.000001', resolution:'5'});
  assert.equal(hp.bars_result.has('a:sz.0000015'), true);
  assert.equal(hp.hasInitialHistoryFailure('a:SZ.000001', '5'), true,
    'an aggregate update does not repair TradingView\'s cached initial error');
  assert.equal(hp.consumeInitialHistoryFailure('a:SZ.000001', '5'), true);
  assert.equal(hp.consumeInitialHistoryFailure('a:SZ.000001', '5'), false);
  await e.respond(1);
});

test('a successful first retry clears failure state and newer same-key requests ignore older aborts', async () => {
  const e = timedBundle(), hp = e.datafeed._historyProvider;
  e.start();
  await e.respondEmpty(0);
  assert.equal(hp.hasInitialHistoryFailure('a:SZ.301004', '5'), true);
  e.start();
  e.start();
  await e.advance(0);
  await e.respond(2);
  assert.equal(hp.hasInitialHistoryFailure('a:SZ.301004', '5'), false);
  assert.equal(hp.consumeInitialHistoryFailure('a:SZ.301004', '5'), false);
});
