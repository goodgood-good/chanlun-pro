'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../notification_channels.js'), 'utf8');
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); };
const initial = () => ({holdings: {configured: true, webhook_masked: '••••abcd', secret_configured: true, keyword: '持仓'},
  others: {configured: true, webhook_masked: '••••efgh', secret_configured: false, keyword: '其他'}});
const response = (channels, status = 200) => ({ok: status < 400, status, json: async () => ({channels, error: '模拟失败'})});
class Element {
  constructor() { this.value = ''; this.checked = false; this.handlers = {}; this.disabled = false; this.textContent = ''; }
  addEventListener(type, callback) { this.handlers[type] = callback; }
}
function fixture(fetcher = async () => response(initial())) {
  const elements = new Map(), requests = [], events = [];
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  vm.runInNewContext(source, {document: {getElementById: get, querySelector: () => ({content: 'fixture-csrf'}),
    dispatchEvent: event => events.push(event.type)}, AbortController, CustomEvent: class { constructor(type) { this.type = type; } },
    setTimeout: () => 1, clearTimeout() {}, fetch: (url, options) => { requests.push({url, options}); return fetcher(url, options); }});
  return {get, requests, events, submit: () => get('notification-channels-form').handlers.submit({preventDefault() {}}),
    edit: (channel, name, value) => { const input = get(`notification-channel-${channel}-${name}`);
      if (name.startsWith('clear-')) { input.checked = value; input.handlers.change(); }
      else { input.value = value; input.handlers.input(); }
    }};
}

test('loads masked addresses and never fills stored webhook or secret into inputs', async () => {
  const ui = fixture(); await settle();
  assert.equal(ui.get('notification-channel-holdings-webhook').value, '');
  assert.equal(ui.get('notification-channel-holdings-secret').value, '');
  assert.match(ui.get('notification-channel-holdings-status').textContent, /••••abcd.*已设置加签/);
  assert.equal(ui.get('notification-channel-holdings-keyword').value, '持仓');
  assert.equal(ui.get('notification-channels-save').disabled, true);
});

test('editing one destination sends only its changed values with CSRF and clears submitted credentials', async () => {
  const ui = fixture(); await settle();
  ui.edit('holdings', 'webhook', 'https://oapi.dingtalk.com/robot/send?access_token=test-token');
  assert.equal(ui.get('notification-channels-save').disabled, false);
  await ui.submit();
  const posted = ui.requests.find(row => row.options.method === 'POST');
  assert.deepEqual(JSON.parse(posted.options.body), {holdings: {webhook: 'https://oapi.dingtalk.com/robot/send?access_token=test-token'}});
  assert.equal(posted.options.headers['X-CSRFToken'], 'fixture-csrf');
  assert.equal(ui.get('notification-channel-holdings-webhook').value, '');
  assert.match(ui.get('notification-channels-message').textContent, /已保存/);
  assert.deepEqual(ui.events, ['notification-channels-saved']);
});

test('blank credentials preserve saved values and explicit clear removes only the selected credential', async () => {
  const ui = fixture(); await settle();
  ui.edit('others', 'clear-secret', true);
  ui.edit('others', 'keyword', '');
  assert.equal(ui.get('notification-channel-others-secret').disabled, true);
  await ui.submit();
  assert.deepEqual(JSON.parse(ui.requests.at(-1).options.body), {others: {secret: '', keyword: ''}});
  assert.equal(ui.get('notification-channel-others-secret').disabled, false);
  assert.equal(ui.get('notification-channel-others-clear-secret').checked, false);
});

test('failed save retains the draft for retry and duplicate submits are blocked', async () => {
  let finish;
  const ui = fixture(async (url, options) => options.method === 'POST' ? new Promise(resolve => { finish = resolve; }) : response(initial()));
  await settle();
  ui.edit('holdings', 'secret', 'draft-secret');
  const submitting = ui.submit(); await ui.submit();
  assert.equal(ui.requests.length, 2);
  assert.equal(ui.get('notification-channels-fields').disabled, true);
  finish(response(null, 503)); await submitting;
  assert.equal(ui.get('notification-channel-holdings-secret').value, 'draft-secret');
  assert.equal(ui.get('notification-channels-save').disabled, false);
  assert.match(ui.get('notification-channels-message').textContent, /保存失败.*修改已保留/);
  assert.equal(ui.events.length, 0);
});

test('failed initial read stays disabled and can recover without posting anything', async () => {
  let failed = true;
  const ui = fixture(async () => failed ? response(null, 503) : response(initial())); await settle();
  assert.equal(ui.get('notification-channels-fields').disabled, true);
  assert.equal(ui.get('notification-channels-reload').hidden, false);
  await ui.submit(); assert.equal(ui.requests.length, 1);
  failed = false; ui.get('notification-channels-reload').handlers.click(); await settle();
  assert.equal(ui.get('notification-channels-fields').disabled, false);
  assert.equal(ui.get('notification-channels-reload').hidden, true);
});
