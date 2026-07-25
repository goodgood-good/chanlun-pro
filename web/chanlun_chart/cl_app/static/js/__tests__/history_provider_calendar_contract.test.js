'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeed(fetchImpl = () => Promise.reject(new Error('no network'))) {
  const sandbox = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise,
    Error, Map, Set, Date,
    fetch: fetchImpl,
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
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
  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed('http://test');
  return { datafeed, historyProvider: datafeed._historyProvider };
}

function response(times, revision, update = false) {
  const values = times.map((_time, index) => 10 + index);
  return {
    s: 'ok',
    update,
    full_snapshot: true,
    t: times,
    c: values,
    o: values,
    h: values,
    l: values,
    v: values,
    fxs: [],
    bis: [],
    xds: [],
    bi_zss: [],
    xd_zss: [],
    bcs: [],
    mmds: [],
    strict_structure_mode: 'replace',
    strict_structure: {
      schema: 'chanlun-chart-structure/v5',
      source_closed_at: times[times.length - 1],
      render_revision: revision,
    },
  };
}

function params(symbol, resolution, firstDataRequest = 'true') {
  return {
    symbol,
    resolution,
    from: 0,
    to: 2_000_000_000,
    firstDataRequest,
  };
}

test('calendar bars keep raw source times while cache uses chart coordinates', () => {
  const { historyProvider } = loadDatafeed();
  const symbol = 'a:SH.513100';
  const dailyTimes = [
    Date.UTC(2026, 6, 22, 7) / 1000,
    Date.UTC(2026, 6, 23, 7) / 1000,
  ];
  const result = historyProvider.applyChanlunUpdate(
    response(dailyTimes, 'daily-1'),
    params(symbol, '1D'),
  );
  const key = `${symbol.toLowerCase()}1d`;
  const stored = historyProvider.bars_result.get(key);

  assert.deepEqual(
    Array.from(stored.times),
    dailyTimes.map((time) => time * 1000),
  );
  assert.deepEqual(
    Array.from(stored.bars, (bar) => bar.time),
    [Date.UTC(2026, 6, 22), Date.UTC(2026, 6, 23)],
  );

  result.bars[0].time = 1;
  assert.equal(stored.bars[0].time, Date.UTC(2026, 6, 22));

  historyProvider.applyChanlunUpdate(
    response(dailyTimes, 'daily-2', true),
    params(symbol, '1D', 'false'),
  );
  assert.equal(stored.bars.length, 2);
  assert.equal(stored.times.length, 2);
});

test('weekly and monthly bars use their period-start chart coordinates', () => {
  const { historyProvider } = loadDatafeed();
  const weeklyClose = Date.UTC(2017, 2, 5, 7) / 1000;
  const monthlyClose = Date.UTC(2026, 6, 31, 7) / 1000;

  historyProvider.applyChanlunUpdate(
    response([weeklyClose], 'weekly'),
    params('a:SH.000001', '1W'),
  );
  historyProvider.applyChanlunUpdate(
    response([monthlyClose], 'monthly'),
    params('a:SH.513100', '1M'),
  );

  assert.equal(
    historyProvider.bars_result.get('a:sh.0000011w').bars[0].time,
    Date.UTC(2017, 1, 27),
  );
  assert.equal(
    historyProvider.bars_result.get('a:sh.5131001m').bars[0].time,
    Date.UTC(2026, 6, 1),
  );
});

test('SSE realtime bars use the same calendar coordinate as history bars', () => {
  const { datafeed } = loadDatafeed();
  const received = [];
  const symbolInfo = { ticker: 'a:SH.513100', name: 'a:SH.513100' };
  const rawClose = Date.UTC(2026, 6, 23, 7) / 1000;
  const key = `${symbolInfo.ticker.toLowerCase()}1d`;
  datafeed.subscribeBars(
    symbolInfo,
    '1D',
    (bar) => received.push(bar),
    'daily-guid',
    () => {},
  );

  datafeed.feedRealtimeBar(
    key,
    response([rawClose], 'realtime'),
    '1D',
  );

  assert.equal(received.length, 1);
  assert.equal(received[0].time, Date.UTC(2026, 6, 23));
});

test('calendar authoritative windows remove invalidated legacy shapes', () => {
  const { historyProvider } = loadDatafeed();
  const symbol = 'a:SH.513100';
  const rawClose = Date.UTC(2026, 6, 31, 7) / 1000;
  const first = response([rawClose], 'shape-1');
  first.xds = [{
    linestyle: '0',
    points: [
      { time: rawClose, price: 11 },
      { time: rawClose, price: 10 },
    ],
  }];
  historyProvider.applyChanlunUpdate(first, params(symbol, '1M'));

  const update = response([rawClose], 'shape-2', true);
  update.full_snapshot = false;
  historyProvider.applyChanlunUpdate(update, {
    symbol,
    resolution: '1M',
    from: Date.UTC(2026, 6, 1) / 1000,
    to: Date.UTC(2026, 6, 23) / 1000,
    firstDataRequest: 'false',
  });

  assert.deepEqual(
    Array.from(historyProvider.bars_result.get('a:sh.5131001m').xds),
    [],
  );
});

test('out-of-order first requests cannot regress the shared cache', async () => {
  const pending = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    pending.push({ url, resolve });
  });
  const { historyProvider } = loadDatafeed(fetchImpl);
  const symbolInfo = { ticker: 'a:SH.513100', name: 'a:SH.513100' };
  const period = {
    from: 0,
    to: 2_000_000_000,
    firstDataRequest: true,
  };
  const sourceClose = Date.UTC(2026, 6, 23, 7) / 1000;

  const olderRequest = historyProvider.getBars(symbolInfo, '1D', period);
  const newerRequest = historyProvider.getBars(symbolInfo, '1D', period);
  const historyRequests = pending.filter((request) => request.url.includes('/history?'));
  assert.equal(historyRequests.length, 2);

  historyRequests[1].resolve({
    ok: true,
    text: async () => JSON.stringify(response([sourceClose], 'new')),
  });
  const newerResult = await newerRequest;
  historyRequests[0].resolve({
    ok: true,
    text: async () => JSON.stringify(response([sourceClose], 'old')),
  });
  const olderResult = await olderRequest;

  const stored = historyProvider.bars_result.get('a:sh.5131001d');
  assert.equal(stored.strict_structure.render_revision, 'new');
  assert.equal(olderResult.strict_structure.render_revision, 'new');
  newerResult.bars[0].time = 1;
  assert.equal(stored.bars[0].time, Date.UTC(2026, 6, 23));
});

test('a regressive full snapshot is ignored even outside getBars', () => {
  const { historyProvider } = loadDatafeed();
  const latest = Date.UTC(2026, 6, 23, 7) / 1000;
  const older = Date.UTC(2026, 6, 22, 7) / 1000;
  const request = params('a:SH.513100', '1D');

  historyProvider.applyChanlunUpdate(response([latest], 'new'), request);
  historyProvider.applyChanlunUpdate(response([older], 'old'), request);

  const stored = historyProvider.bars_result.get('a:sh.5131001d');
  assert.equal(stored.strict_structure.render_revision, 'new');
  assert.deepEqual(Array.from(stored.times), [latest * 1000]);
});
