'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SCHEMA = 'chanlun-account-chart-preferences/v1';

function storage(initial) {
  const values = new Map(Object.entries(initial || {}));
  return {
    get length() { return values.size; },
    key(index) { return [...values.keys()][index] ?? null; },
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(String(key), String(value)); },
    removeItem(key) { values.delete(String(key)); },
    snapshot() { return Object.fromEntries(values); },
  };
}

function loadPreferences(bootstrap, initial, options = {}) {
  const localStorage = storage(initial);
  const timers = [];
  const fetchCalls = [];
  const listeners = {};
  const sandbox = {
    console: { warn() {} },
    document: {
      querySelector(selector) {
        return selector === 'meta[name="csrf-token"]' ? { content: 'csrf-test' } : null;
      },
    },
    localStorage,
    __CHANLUN_ACCOUNT_PREFERENCES__: bootstrap,
    URLSearchParams,
    fetch(url, options) {
      fetchCalls.push({ url, options });
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });
    },
    setTimeout(callback, delay) {
      timers.push({ callback, delay, cleared: false });
      return timers.length;
    },
    clearTimeout(id) {
      if (timers[id - 1]) timers[id - 1].cleared = true;
    },
    addEventListener(type, callback) { listeners[type] = callback; },
    location: { search: options.search || '', reload() {}, assign() {}, replace() {} },
    Promise,
    JSON,
    Object,
    Array,
    String,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '..', 'account_preferences.js'), 'utf8');
  vm.runInContext(source, sandbox, { filename: 'account_preferences.js' });
  return { sandbox, localStorage, timers, fetchCalls, listeners };
}

test('server account preferences replace another account local chart state', () => {
  const serverTvChart = JSON.stringify({ theme: 'dark', chart_layout_type: 'four' });
  const h = loadPreferences(
    {
      username: 'alice',
      account_key: 'alice-key',
      exists: true,
      preferences: {
        schema: SCHEMA,
        values: { tv_chart: serverTvChart, chart_menu_width: '520' },
      },
    },
    {
      tv_chart: JSON.stringify({ theme: 'Light', chart_layout_type: 'single' }),
      chart_menu_width: '330',
      cl_show_config_1_5: JSON.stringify({ schema: 'old-user', bi: false }),
    },
  );

  assert.equal(h.localStorage.getItem('tv_chart'), serverTvChart);
  assert.equal(h.localStorage.getItem('chart_menu_width'), '520');
  assert.equal(h.localStorage.getItem('cl_show_config_1_5'), null);
  assert.equal(h.fetchCalls.length, 0);
});

test('a new account starts clean instead of inheriting unscoped browser state', () => {
  const h = loadPreferences(
    {
      username: 'bob',
      account_key: 'bob-key',
      exists: false,
      preferences: { schema: SCHEMA, values: {} },
    },
    { tv_chart: JSON.stringify({ chart_layout_type: 'four' }), chart_menu_width: '600' },
  );

  assert.equal(h.localStorage.getItem('tv_chart'), null);
  assert.equal(h.localStorage.getItem('chart_menu_width'), null);
});

test('a new account ignores unscoped state after another account used the browser', () => {
  const h = loadPreferences(
    {
      username: 'alice',
      account_key: 'alice-key',
      exists: false,
      preferences: { schema: SCHEMA, values: {} },
    },
    {
      tv_chart: JSON.stringify({ chart_layout_type: 'vertical-2' }),
      'chanlun_account_preferences_v1:bob-key': JSON.stringify({
        schema: SCHEMA,
        pending: false,
        values: { tv_chart: JSON.stringify({ chart_layout_type: 'vertical-2' }) },
      }),
    },
  );

  assert.equal(h.localStorage.getItem('tv_chart'), null);
  assert.equal(h.fetchCalls.length, 0);
});

test('an unsaved account snapshot survives reload and wins over stale server state', async () => {
  const pendingTvChart = JSON.stringify({ chart_layout_type: 'vertical-2' });
  const h = loadPreferences(
    {
      username: 'alice',
      account_key: 'alice-key',
      exists: true,
      preferences: {
        schema: SCHEMA,
        values: { tv_chart: JSON.stringify({ chart_layout_type: 'single' }) },
      },
    },
    {
      tv_chart: JSON.stringify({ chart_layout_type: 'single' }),
      'chanlun_account_preferences_v1:alice-key': JSON.stringify({
        schema: SCHEMA,
        pending: true,
        values: { tv_chart: pendingTvChart },
      }),
    },
  );

  assert.equal(h.localStorage.getItem('tv_chart'), pendingTvChart);
  const timer = h.timers.find((item) => !item.cleared);
  assert.ok(timer);
  timer.callback();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.fetchCalls.length, 1);
  assert.deepEqual(JSON.parse(h.fetchCalls[0].options.body), {
    schema: SCHEMA,
    values: { tv_chart: pendingTvChart },
    merge: true,
    changed_keys: ['tv_chart'],
  });
});

test('preference writes use the account API and include CSRF', async () => {
  const h = loadPreferences(
    {
      username: 'alice',
      account_key: 'alice-key',
      exists: true,
      preferences: { schema: SCHEMA, values: {} },
    },
  );

  h.sandbox.AccountPreferences.setItem('chart_menu_collapsed', '1');
  const timer = h.timers.find((item) => !item.cleared);
  assert.ok(timer);
  timer.callback();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(h.fetchCalls.length, 1);
  assert.equal(h.fetchCalls[0].url, '/api/chart/preferences');
  assert.equal(h.fetchCalls[0].options.headers.X_CSRFToken, undefined);
  assert.equal(h.fetchCalls[0].options.headers.X_CSRF_TOKEN, undefined);
  assert.equal(h.fetchCalls[0].options.headers.X_CSRF, undefined);
  assert.equal(h.fetchCalls[0].options.headers['X-CSRFToken'], 'csrf-test');
  assert.deepEqual(JSON.parse(h.fetchCalls[0].options.body), {
    schema: SCHEMA,
    values: { chart_menu_collapsed: '1' },
    merge: true,
    changed_keys: ['chart_menu_collapsed'],
  });
});

test('embedded decision-support chart is read-only and cannot overwrite account state', () => {
  const serverTvChart = JSON.stringify({ market: 'a', a_code: 'SH.600000' });
  const sharedTvChart = JSON.stringify({ market: 'us', us_code: 'AAPL.US' });
  const h = loadPreferences(
    {
      username: 'alice',
      account_key: 'alice-key',
      exists: true,
      preferences: { schema: SCHEMA, values: { tv_chart: serverTvChart } },
    },
    { tv_chart: sharedTvChart },
    { search: '?chart_embed=decision-support&market=a&code=SZ.000001' },
  );

  assert.equal(h.sandbox.AccountPreferences.readOnly, true);
  assert.equal(h.sandbox.AccountPreferences.getItem('tv_chart'), serverTvChart);
  h.sandbox.AccountPreferences.setItem(
    'tv_chart',
    JSON.stringify({ market: 'a', a_code: 'SZ.000001' }),
  );
  assert.equal(h.localStorage.getItem('tv_chart'), sharedTvChart);
  assert.equal(h.fetchCalls.length, 0);
  assert.equal(h.timers.filter((item) => !item.cleared).length, 0);
});

test('screening view is an account-approved preference', async () => {
  const h = loadPreferences({
    username: 'alice',
    account_key: 'alice-key',
    exists: true,
    preferences: { schema: SCHEMA, values: {} },
  });
  const view = JSON.stringify({
    contract: 'contract-v1',
    layout: 'quad',
    chartSizing: { heights: { quad: 920 } },
  });

  assert.equal(h.sandbox.AccountPreferences.isApprovedKey('trading_screening_view'), true);
  h.sandbox.AccountPreferences.setItem('trading_screening_view', view);
  const timer = h.timers.find((item) => !item.cleared);
  assert.ok(timer);
  timer.callback();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    JSON.parse(h.fetchCalls[0].options.body).values.trading_screening_view,
    view,
  );
});
