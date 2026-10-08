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
  if (options.viewport) {
    Object.assign(sandbox, options.viewport);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'workspace_device.js'), 'utf8'), sandbox);
  }
  const source = fs.readFileSync(path.join(__dirname, '..', 'account_preferences.js'), 'utf8');
  vm.runInContext(source, sandbox, { filename: 'account_preferences.js' });
  return { sandbox, localStorage, timers, fetchCalls, listeners };
}

test('phone preferences start single and save without changing desktop layouts', async () => {
  const desktop=JSON.stringify({theme:'dark',market:'us',chart_layout_type:'four',us_code:'AAPL.US',us_interval_1:'30'});
  const h=loadPreferences({account_key:'alice-key',exists:true,preferences:{schema:SCHEMA,values:{tv_chart:desktop}}},
    {tv_chart:desktop},{viewport:{innerWidth:393}});
  assert.equal(h.sandbox.WorkspaceDevice.isPhone,true);
  const initial=JSON.parse(h.sandbox.AccountPreferences.getItem('tv_chart'));
  assert.equal(initial.chart_layout_type,'single');assert.equal(initial.us_interval_1,'30');
  const phone=JSON.stringify({...initial,us_interval_1:'1'});
  h.sandbox.AccountPreferences.setItem('tv_chart',phone);
  await h.sandbox.AccountPreferences.flush();
  const sent=JSON.parse(h.fetchCalls[0].options.body);
  assert.deepEqual(sent.changed_keys,['phone_tv_chart']);
  assert.equal(sent.values.tv_chart,desktop);assert.equal(sent.values.phone_tv_chart,phone);
  assert.equal(h.localStorage.getItem('tv_chart'),desktop);
});

test('pending phone changes preserve newer server desktop settings and other device keys', async () => {
  const desktop=JSON.stringify({chart_layout_type:'four'}),phone=JSON.stringify({chart_layout_type:'single',us_interval_1:'1'});
  const h=loadPreferences({account_key:'alice-key',exists:true,preferences:{schema:SCHEMA,values:{tv_chart:desktop,phone_chart_menu_width:'320'}}},
    {'chanlun_account_preferences_v1:alice-key':JSON.stringify({schema:SCHEMA,pending:true,changedKeys:['phone_tv_chart'],values:{phone_tv_chart:phone}})},
    {viewport:{innerWidth:393}});
  await h.sandbox.AccountPreferences.flush();
  const sent=JSON.parse(h.fetchCalls[0].options.body);
  assert.deepEqual(sent.changed_keys,['phone_tv_chart']);assert.equal(sent.values.tv_chart,desktop);
  assert.equal(sent.values.phone_chart_menu_width,'320');
});

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

test('removed analysis preferences are never uploaded', () => {
 const h=loadPreferences({username:'alice',account_key:'alice',exists:false,preferences:{schema:SCHEMA,values:{}}},{});
 assert.equal(h.sandbox.AccountPreferences.isApprovedKey('trading_screening_view'),false);
 assert.equal(h.sandbox.AccountPreferences.isApprovedKey('chart_menu_width'),true);
});
