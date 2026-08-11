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

test('cold history gets a 45s deadline while incremental polling stays at 15s', async () => {
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
            s: 'ok', update: false,
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
  assert.equal(delays.at(-1), 45_000);

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
        return Promise.reject(new Error('Request timed out after 45000ms'));
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
