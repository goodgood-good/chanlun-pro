'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeed(requestUrls) {
  const configuration = {
    supports_search: true,
    supported_resolutions: ['1', '5', '30'],
  };
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: (url) => {
      const requestUrl = String(url);
      requestUrls.push(requestUrl);
      const payload = requestUrl.includes('/history?')
        ? { s: 'ok', t: [1000], o: [1], h: [1], l: [1], c: [1], v: [1] }
        : requestUrl.includes('/symbols?')
          ? {
              name: 'qmt-gics3:test', ticker: 'a:qmt-gics3:test',
              exchange: 'a', listed_exchange: 'a', type: 'index',
              session: '0930-1130,1300-1500', timezone: 'Asia/Shanghai',
              minmov: 1, pricescale: 1000000, supported_resolutions: ['30'],
            }
          : configuration;
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    },
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sandbox, { filename: 'bundle.js' });
  return sandbox.Datafeeds;
}

test('every UDF history request carries the immutable human-review lock', async () => {
  const requestUrls = [];
  const { UDFCompatibleDatafeed } = loadDatafeed(requestUrls);
  const lock = {
    review_candidate_id: `sha256:${'1'.repeat(64)}`,
    review_source_sha256: `sha256:${'2'.repeat(64)}`,
    review_as_of: 1784784600,
  };
  const datafeed = new UDFCompatibleDatafeed(
    'http://test-datafeed',
    30_000,
    undefined,
    { historyParams: lock },
  );

  await datafeed._historyProvider.getBars(
    { ticker: 'a:SH.600000' },
    '30',
    { from: 1700000000, to: 1900000000, firstDataRequest: true },
  );

  const historyUrl = requestUrls.find((url) => url.includes('/history?'));
  assert.ok(historyUrl);
  const params = new URL(historyUrl).searchParams;
  assert.equal(params.get('review_candidate_id'), lock.review_candidate_id);
  assert.equal(params.get('review_source_sha256'), lock.review_source_sha256);
  assert.equal(params.get('review_as_of'), String(lock.review_as_of));
  assert.equal(params.get('symbol'), 'a:SH.600000');
});

test('fixed history parameters cannot replace UDF symbol or time-range fields', async () => {
  const requestUrls = [];
  const { UDFCompatibleDatafeed } = loadDatafeed(requestUrls);
  const datafeed = new UDFCompatibleDatafeed(
    'http://test-datafeed',
    30_000,
    undefined,
    { historyParams: { symbol: 'a:FORGED', to: 999, review_as_of: 123 } },
  );

  await datafeed._historyProvider.getBars(
    { ticker: 'a:SH.600000' },
    '5',
    { from: 100, to: 200, firstDataRequest: false },
  );

  const params = new URL(requestUrls.find((url) => url.includes('/history?'))).searchParams;
  assert.equal(params.get('symbol'), 'a:SH.600000');
  assert.equal(params.get('to'), '200');
  assert.equal(params.get('review_as_of'), '123');
});

test('UDF symbol resolution carries only the immutable causal-review lock', async () => {
  const requestUrls = [];
  const { UDFCompatibleDatafeed } = loadDatafeed(requestUrls);
  const lock = {
    review_candidate_id: `sha256:${'3'.repeat(64)}`,
    review_source_sha256: `sha256:${'4'.repeat(64)}`,
    review_as_of: 1784784600,
    symbol: 'a:FORGED',
    to: 999,
  };
  const datafeed = new UDFCompatibleDatafeed(
    'http://test-datafeed',
    30_000,
    undefined,
    { historyParams: lock },
  );
  await new Promise((resolve) => datafeed.onReady(resolve));
  await new Promise((resolve, reject) => {
    datafeed.resolveSymbol('a:qmt-gics3:test', resolve, reject);
  });

  const symbolsUrl = requestUrls.find((url) => url.includes('/symbols?'));
  assert.ok(symbolsUrl);
  const params = new URL(symbolsUrl).searchParams;
  assert.equal(params.get('review_candidate_id'), lock.review_candidate_id);
  assert.equal(params.get('review_source_sha256'), lock.review_source_sha256);
  assert.equal(params.get('review_as_of'), String(lock.review_as_of));
  assert.equal(params.get('symbol'), 'a:qmt-gics3:test');
  assert.equal(params.has('to'), false);
});
