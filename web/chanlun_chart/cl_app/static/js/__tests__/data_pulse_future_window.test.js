'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FIXED_NOW_MS = 1_784_687_664_000;

function loadDatafeeds(requestUrls, fixedNowMs = FIXED_NOW_MS) {
  class FixedDate extends Date {
    static now() {
      return fixedNowMs;
    }
  }

  const configuration = {
    supports_search: true,
    supported_resolutions: ['1'],
    supports_time: false,
  };
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    Date: FixedDate,
    fetch: (url) => {
      const requestUrl = String(url);
      requestUrls.push(requestUrl);
      const payload = requestUrl.includes('/history?') ? { s: 'no_data' } : configuration;
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  };
  sb.globalThis = sb;
  sb.self = sb;
  sb.window = sb;
  vm.createContext(sb);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sb, { filename: 'bundle.js' });
  return sb.Datafeeds;
}

test('DataPulse 实时轮询上界应比当前时间多 60 秒以保留在制 K 线', async () => {
  const requestUrls = [];
  const { UDFCompatibleDatafeed } = loadDatafeeds(requestUrls);
  const datafeed = new UDFCompatibleDatafeed('http://test-datafeed', 30_000);
  const symbolInfo = { ticker: 'A:SH.688050', name: 'A:SH.688050' };

  datafeed.subscribeBars(symbolInfo, '1', () => {}, 'guid-1', () => {});
  await datafeed._dataPulseProvider._updateDataForSubscriber('guid-1');

  const historyUrl = requestUrls.find((url) => url.includes('/history?'));
  assert.ok(historyUrl, `应发出 history 请求，实际请求=${JSON.stringify(requestUrls)}`);
  const to = Number(new URL(historyUrl).searchParams.get('to'));
  assert.strictEqual(to, FIXED_NOW_MS / 1000 + 60);
});

test('DataPulse 在 A 股闭市、午休和周末不应发出空 history 轮询', async () => {
  const closedTimes = [
    Date.parse('2026-08-26T00:05:00+08:00'),
    Date.parse('2026-08-26T11:41:00+08:00'),
    Date.parse('2026-08-26T15:11:00+08:00'),
    Date.parse('2026-08-29T10:00:00+08:00'),
  ];

  for (const nowMs of closedTimes) {
    const requestUrls = [];
    const { UDFCompatibleDatafeed } = loadDatafeeds(requestUrls, nowMs);
    const datafeed = new UDFCompatibleDatafeed('http://test-datafeed', 30_000);
    const symbolInfo = {
      exchange: 'a',
      ticker: 'a:SZ.002083',
      name: 'SZ.002083',
      timezone: 'Asia/Shanghai',
    };

    datafeed.subscribeBars(symbolInfo, '5', () => {}, `closed-${nowMs}`, () => {});
    await datafeed._dataPulseProvider._updateDataForSubscriber(`closed-${nowMs}`);

    assert.equal(
      requestUrls.some((url) => url.includes('/history?')),
      false,
      `闭市时刻 ${new Date(nowMs).toISOString()} 不应轮询 history`,
    );
  }
});

test('DataPulse A 股交易窗口及非 A 股市场继续实时轮询', async () => {
  const cases = [
    {
      nowMs: Date.parse('2026-08-26T10:00:00+08:00'),
      symbolInfo: { exchange: 'a', ticker: 'a:SZ.002083', name: 'SZ.002083' },
    },
    {
      nowMs: Date.parse('2026-08-26T11:40:00+08:00'),
      symbolInfo: { exchange: 'a', ticker: 'a:SZ.002083', name: 'SZ.002083' },
    },
    {
      nowMs: Date.parse('2026-08-26T15:10:00+08:00'),
      symbolInfo: { exchange: 'a', ticker: 'a:SZ.002083', name: 'SZ.002083' },
    },
    {
      nowMs: Date.parse('2026-08-26T00:05:00+08:00'),
      symbolInfo: {
        exchange: 'currency',
        ticker: 'currency:BTC/USDT',
        name: 'BTC/USDT',
      },
    },
  ];

  for (const [index, testCase] of cases.entries()) {
    const requestUrls = [];
    const { UDFCompatibleDatafeed } = loadDatafeeds(requestUrls, testCase.nowMs);
    const datafeed = new UDFCompatibleDatafeed('http://test-datafeed', 30_000);
    const guid = `open-${index}`;

    datafeed.subscribeBars(testCase.symbolInfo, '5', () => {}, guid, () => {});
    await datafeed._dataPulseProvider._updateDataForSubscriber(guid);

    assert.equal(
      requestUrls.some((url) => url.includes('/history?')),
      true,
      `实时窗口用例 ${index} 应继续轮询 history`,
    );
  }
});
