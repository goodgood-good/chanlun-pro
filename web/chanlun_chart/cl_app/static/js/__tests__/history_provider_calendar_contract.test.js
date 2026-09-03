'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeed(
  fetchImpl = () => Promise.reject(new Error('no network')),
  options = {},
) {
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
  const datafeed = new sandbox.Datafeeds.UDFCompatibleDatafeed(
    'http://test',
    10_000,
    undefined,
    options,
  );
  return { datafeed, historyProvider: datafeed._historyProvider };
}

test('every embedded first request asks for complete current history atomically', async () => {
  for (const resolution of ['1D', '30', '5', '1']) {
    const pending = [];
    const fetchImpl = (url) => new Promise((resolve) => {
      pending.push({ url, resolve });
    });
    const { historyProvider } = loadDatafeed(fetchImpl, {
      historyParams: { embedded: 1 },
    });
    const sourceClose = Date.UTC(2026, 7, 31, 7) / 1000;
    const request = historyProvider.getBars(
      { ticker: 'a:SH.603610', name: 'a:SH.603610' },
      resolution,
      {
        from: 0,
        to: 2_000_000_000,
        countBack: 329,
        firstDataRequest: true,
      },
    );
    const historyRequest = pending.find((item) => item.url.includes('/history?'));
    const parsed = new URL(historyRequest.url);
    assert.equal(parsed.searchParams.get('embedded'), '1');
    assert.equal(parsed.searchParams.get('numeric_delta'), '1');
    assert.equal(
      parsed.searchParams.get('countback'),
      null,
      `${resolution} must not expose a short first frame`,
    );

    historyRequest.resolve({
      ok: true,
      text: async () => JSON.stringify(response([sourceClose], `embedded-${resolution}`)),
    });
    await request;
  }
});

test('embedded complete-history floor settles older probes without another request', async () => {
  const pending = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    pending.push({ url, resolve });
  });
  const { historyProvider } = loadDatafeed(fetchImpl, {
    historyParams: { embedded: 1 },
  });
  const symbolInfo = { ticker: 'a:SH.603610', name: 'a:SH.603610' };
  const floor = 1_700_000_000;
  const initial = historyProvider.getBars(symbolInfo, '1', {
    from: floor,
    to: floor + 120,
    countBack: 329,
    firstDataRequest: true,
  });
  const initialRequest = pending.find((item) => item.url.includes('/history?'));
  const payload = response([floor, floor + 60, floor + 120], 'embedded-floor');
  payload.history_floor = floor;
  initialRequest.resolve({
    ok: true,
    text: async () => JSON.stringify(payload),
  });
  await initial;

  const requestCount = pending.filter((item) => item.url.includes('/history?')).length;
  const older = await historyProvider.getBars(symbolInfo, '1', {
    from: floor - 1_800,
    to: floor,
    countBack: 31,
    firstDataRequest: false,
  });
  assert.equal(older.meta.noData, true);
  assert.deepEqual(Array.from(older.bars), []);
  assert.equal(
    pending.filter((item) => item.url.includes('/history?')).length,
    requestCount,
  );

  historyProvider._clearBarsResultForSymbolResolution(symbolInfo.ticker, '1');
  const afterReset = historyProvider.getBars(symbolInfo, '1', {
    from: floor - 1_800,
    to: floor,
    countBack: 31,
    firstDataRequest: false,
  });
  const requestsAfterReset = pending.filter((item) => item.url.includes('/history?'));
  assert.equal(requestsAfterReset.length, requestCount + 1);
  requestsAfterReset.at(-1).resolve({
    ok: true,
    text: async () => JSON.stringify({ s: 'no_data' }),
  });
  assert.equal((await afterReset).meta.noData, true);
});

test('standalone first requests retain TradingView countback semantics', async () => {
  const pending = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    pending.push({ url, resolve });
  });
  const { historyProvider } = loadDatafeed(fetchImpl);
  const sourceClose = Date.UTC(2026, 7, 31, 7) / 1000;
  const request = historyProvider.getBars(
    { ticker: 'a:SH.603610', name: 'a:SH.603610' },
    '5',
    {
      from: 0,
      to: 2_000_000_000,
      countBack: 329,
      firstDataRequest: true,
    },
  );
  const historyRequest = pending.find((item) => item.url.includes('/history?'));
  assert.equal(new URL(historyRequest.url).searchParams.get('countback'), '329');
  historyRequest.resolve({
    ok: true,
    text: async () => JSON.stringify(response([sourceClose], 'standalone')),
  });
  await request;
});

test('atomic standalone first requests return one complete frame and expose request idleness', async () => {
  const pending = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    pending.push({ url, resolve });
  });
  const { historyProvider } = loadDatafeed(fetchImpl, {
    historyParams: { atomic_initial: 1 },
  });
  const sourceClose = Date.UTC(2026, 7, 31, 7) / 1000;
  const request = historyProvider.getBars(
    { ticker: 'a:SZ.301004', name: 'a:SZ.301004' },
    '5',
    {
      from: 0,
      to: 2_000_000_000,
      countBack: 335,
      firstDataRequest: true,
    },
  );
  const historyRequest = pending.find((item) => item.url.includes('/history?'));
  const parsed = new URL(historyRequest.url);
  assert.equal(parsed.searchParams.get('atomic_initial'), '1');
  assert.equal(parsed.searchParams.get('countback'), null);
  assert.equal(historyProvider.hasPendingHistoryWork(0), true);

  historyRequest.resolve({
    ok: true,
    text: async () => JSON.stringify(response([sourceClose], 'atomic-standalone')),
  });
  await request;

  assert.equal(historyProvider.hasPendingHistoryWork(0), false);
  assert.equal(historyProvider.hasPendingHistoryWork(1_000), true);
});

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
    strict_structure_mode: 'replace',
    strict_structure: {
      schema: 'chanlun-chart-structure',
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

test('A-share intraday close timestamps use opening chart coordinates only', () => {
  const { historyProvider } = loadDatafeed();
  const symbol = 'a:SZ.001270';
  const firstClose = Date.UTC(2026, 7, 31, 1, 35) / 1000;
  const lunchClose = Date.UTC(2026, 7, 31, 3, 30) / 1000;

  historyProvider.applyChanlunUpdate(
    response([firstClose, lunchClose], 'a-intraday'),
    params(symbol, '5'),
  );
  const stored = historyProvider.bars_result.get(`${symbol.toLowerCase()}5`);

  assert.deepEqual(
    Array.from(stored.times),
    [firstClose, lunchClose].map((time) => time * 1000),
    'strict evidence must retain the raw market close identity',
  );
  assert.deepEqual(
    Array.from(stored.bars, (bar) => bar.time),
    [firstClose - 300, lunchClose - 300].map((time) => time * 1000),
  );

  historyProvider.applyChanlunUpdate(
    response([firstClose], 'crypto-intraday'),
    params('currency_spot:BTC/USDT', '5'),
  );
  assert.equal(
    historyProvider.bars_result.get('currency_spot:btc/usdt5').bars[0].time,
    firstClose * 1000,
    'providers that already publish opening timestamps must not shift',
  );
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

test('A-share SSE bars use the same intraday opening coordinate as history', () => {
  const { datafeed } = loadDatafeed();
  const received = [];
  const symbolInfo = { ticker: 'a:SH.600000', name: 'a:SH.600000' };
  const rawClose = Date.UTC(2026, 7, 31, 3, 30) / 1000;
  const key = `${symbolInfo.ticker.toLowerCase()}5`;
  datafeed.subscribeBars(
    symbolInfo,
    '5',
    (bar) => received.push(bar),
    'a-five-minute-guid',
    () => {},
  );

  datafeed.feedRealtimeBar(key, response([rawClose], 'a-realtime'), '5');

  assert.equal(received.length, 1);
  assert.equal(received[0].time, (rawClose - 300) * 1000);
});

test('calendar authoritative windows remove invalidated shapes', () => {
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

test('a current first request cannot return a snapshot older than the aggregate cache', async () => {
  const pending = [];
  const fetchImpl = (url) => new Promise((resolve) => {
    pending.push({ url, resolve });
  });
  const { historyProvider } = loadDatafeed(fetchImpl, {
    historyParams: { embedded: 1 },
  });
  const symbolInfo = { ticker: 'a:SH.513100', name: 'a:SH.513100' };
  const earlier = Date.UTC(2026, 6, 22, 7) / 1000;
  const latest = Date.UTC(2026, 6, 23, 7) / 1000;
  const period = { from: earlier, to: latest, firstDataRequest: true };

  const latestRequest = historyProvider.getBars(symbolInfo, '1D', period);
  const firstPending = pending.filter((item) => item.url.includes('/history?')).at(-1);
  const latestPayload = response([earlier, latest], 'latest');
  latestPayload.history_floor = earlier;
  firstPending.resolve({
    ok: true,
    text: async () => JSON.stringify(latestPayload),
  });
  await latestRequest;

  const regressiveRequest = historyProvider.getBars(symbolInfo, '1D', period);
  const secondPending = pending.filter((item) => item.url.includes('/history?')).at(-1);
  const regressivePayload = response([earlier + 60, latest - 60], 'regressive');
  regressivePayload.history_floor = earlier + 60;
  secondPending.resolve({
    ok: true,
    text: async () => JSON.stringify(regressivePayload),
  });
  const result = await regressiveRequest;

  assert.equal(result.strict_structure.render_revision, 'latest');
  assert.equal(result.bars.at(-1).time, Date.UTC(2026, 6, 23));
  assert.equal(historyProvider._completeHistoryFloorByKey.get('a:sh.5131001d'), earlier);
});
