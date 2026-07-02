'use strict';
// H1(阶段E) datafeed 注入回归:charts.js 断档 gap-reset 前置 _historyProvider._forceRefreshOnce=true,
// datafeed getBars 在 firstDataRequest 时注入 force_refresh=1(用后即清),让后端绕过缓存重算补齐断档。
// 测真实 dist/bundle.js(与 history_provider_poll_merge 同法),spy fetch 断言请求 URL 是否含 force_refresh。
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadDatafeedsWithSpy() {
  const fetchCalls = [];
  const sb = {
    console, Math, JSON, Array, Object, String, Number, Boolean, Promise, Error, Map, Set,
    fetch: (url) => {
      fetchCalls.push(String(url));
      const u = String(url);
      // datafeed 构造会先拉 /config 校验(须 supports_search 或 group_request),否则异步 reject
      // 泄漏成 unhandledRejection。/config 返回合法配置,其余(history)返回一根 K 线。
      const body = u.includes('/config')
        ? {
            supports_search: true, supports_group_request: false,
            supported_resolutions: ['1', '5', '30', '1D'],
            supports_marks: false, supports_timescale_marks: false,
          }
        : { s: 'ok', t: [1000], o: [1], h: [1], l: [1], c: [1], v: [1] };
      return Promise.resolve({ text: () => Promise.resolve(JSON.stringify(body)) });
    },
    setTimeout: () => 0, clearTimeout: () => {},
    setInterval: () => 0, clearInterval: () => {},
  };
  sb.globalThis = sb; sb.self = sb; sb.window = sb;
  vm.createContext(sb);
  const bundlePath = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'dist', 'bundle.js');
  vm.runInContext(fs.readFileSync(bundlePath, 'utf8'), sb, { filename: 'bundle.js' });
  return { Datafeeds: sb.Datafeeds, fetchCalls };
}

function getHistoryProvider() {
  const { Datafeeds, fetchCalls } = loadDatafeedsWithSpy();
  const df = new Datafeeds.UDFCompatibleDatafeed('http://t');
  return { df, hp: df._historyProvider, fetchCalls };
}

const SYM = { ticker: 'a:SH.600519' };
const PP_FIRST = { from: 1000, to: 2000, firstDataRequest: true };

test('getBars: _forceRefreshOnce=true 时 firstDataRequest 注入 force_refresh=1', async () => {
  const { hp, fetchCalls } = getHistoryProvider();
  assert.ok(hp, '_historyProvider 应可访问');
  hp._forceRefreshOnce = true;
  try { await hp.getBars(SYM, '5', PP_FIRST); } catch (e) { /* 只关心请求 URL */ }
  const url = fetchCalls[fetchCalls.length - 1];
  assert.ok(url && url.includes('force_refresh=1'), `firstDataRequest 应注入 force_refresh=1: ${url}`);
});

test('getBars: force_refresh 一次性 — 第二次 firstDataRequest 不再带', async () => {
  const { hp, fetchCalls } = getHistoryProvider();
  hp._forceRefreshOnce = true;
  try { await hp.getBars(SYM, '5', PP_FIRST); } catch (e) { /* ignore */ }
  try { await hp.getBars(SYM, '5', PP_FIRST); } catch (e) { /* ignore */ }
  const url2 = fetchCalls[fetchCalls.length - 1];
  assert.ok(url2 && !url2.includes('force_refresh'), `第二次应无 force_refresh(用后即清): ${url2}`);
});

test('getBars: 未置标志 → 不注入 force_refresh(现状不回归)', async () => {
  const { hp, fetchCalls } = getHistoryProvider();
  try { await hp.getBars(SYM, '5', PP_FIRST); } catch (e) { /* ignore */ }
  const url = fetchCalls[fetchCalls.length - 1];
  assert.ok(url && !url.includes('force_refresh'), `未置标志不应 force_refresh: ${url}`);
});

test('getBars: 置标志但非 firstDataRequest(polling) → 不注入', async () => {
  const { hp, fetchCalls } = getHistoryProvider();
  hp._forceRefreshOnce = true;
  try { await hp.getBars(SYM, '5', { from: 1000, to: 2000, firstDataRequest: false }); } catch (e) { /* ignore */ }
  const url = fetchCalls[fetchCalls.length - 1];
  assert.ok(url && !url.includes('force_refresh'), `polling 不应 force_refresh: ${url}`);
});
