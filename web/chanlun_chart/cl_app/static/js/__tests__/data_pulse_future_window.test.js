'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const FIXED_NOW_MS = 1_784_687_664_000;

function loadDatafeeds(requestUrls) {
  class FixedDate extends Date {
    static now() {
      return FIXED_NOW_MS;
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
