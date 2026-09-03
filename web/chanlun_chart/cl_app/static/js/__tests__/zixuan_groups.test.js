'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const staticRoot = path.join(__dirname, '..', '..');
const templateRoot = path.join(__dirname, '..', '..', '..', 'templates');

function read(file) {
  return fs.readFileSync(file, 'utf8');
}

function loadZiXuan() {
  const ajaxCalls = [];
  const messages = [];
  const values = new Map();
  const textValues = new Map();
  const classes = new Map();

  function getClasses(selector) {
    if (!classes.has(selector)) classes.set(selector, new Set());
    return classes.get(selector);
  }

  function collection(selector) {
    return {
      length: 1,
      val(value) {
        if (arguments.length === 0) return values.get(selector) || '';
        values.set(selector, String(value));
        return this;
      },
      text(value) {
        if (arguments.length === 0) return textValues.get(selector) || '';
        textValues.set(selector, String(value));
        return this;
      },
      empty() { return this; },
      append() { return this; },
      prop() { return this; },
      attr() { return this; },
      removeAttr() { return this; },
      addClass(name) { getClasses(selector).add(name); return this; },
      removeClass(name) { getClasses(selector).delete(name); return this; },
      toggleClass(name, enabled) {
        if (enabled) getClasses(selector).add(name);
        else getClasses(selector).delete(name);
        return this;
      },
      focus() { return this; },
      off() { return this; },
      on() { return this; },
      click() { return this; },
      change() { return this; },
      each() { return this; },
      filter() { return this; },
      siblings() { return { find() { return []; } }; },
      find() { return []; },
      data() { return undefined; },
      replaceWith() { return this; },
    };
  }

  function $(selector, props) {
    if (selector === '<option>') return props || {};
    return collection(selector);
  }
  $.ajax = (options) => { ajaxCalls.push(options); return {}; };

  const table = {
    render() {},
    on() {},
    setRowChecked() {},
  };
  const layui = {
    table,
    dropdown: { render() {}, reloadData() {} },
    form: { render() {}, on() {} },
    layer: { msg(message) { messages.push(String(message)); }, open() {} },
    each(valuesToVisit, callback) {
      valuesToVisit.forEach((value, index) => callback(index, value));
    },
    use(deps, callback) {
      if (typeof deps === 'function') deps();
      else callback();
    },
  };
  const sandbox = {
    $,
    layui,
    layer: layui.layer,
    AppRequest: { ajax: $.ajax },
    xmSelect: { render() { return { update() {} }; } },
    Utils: {
      get_market() { return 'a'; },
      get_code() { return 'SH.600000'; },
      get_selected_items() { return []; },
    },
    change_chart_ticker() {},
    document: {
      createElement(tagName) {
        return {
          tagName,
          className: '',
          dataset: {},
          style: {},
          children: [],
          appendChild(child) { this.children.push(child); },
          get outerHTML() { return `<${tagName}></${tagName}>`; },
        };
      },
      createTextNode(value) { return { textContent: String(value) }; },
    },
    setTimeout() { return 1; },
    clearTimeout() {},
    JSON,
    Number,
    String,
    Array,
    Object,
    encodeURIComponent,
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  const source = read(path.join(staticRoot, 'js', 'zixuan.js'));
  vm.runInContext(source + '\n;globalThis.__ZiXuan = ZiXuan;', sandbox, {
    filename: 'zixuan.js',
  });
  return {
    ZiXuan: sandbox.__ZiXuan,
    ajaxCalls,
    messages,
    value(selector) { return values.get(selector) || ''; },
    text(selector) { return textValues.get(selector) || ''; },
  };
}

test('watchlist surface exposes direct group creation and operational status', () => {
  const template = read(path.join(templateRoot, 'index.html'));
  const css = read(path.join(staticRoot, 'css', 'zixuan.css'));

  assert.match(template, /id="zixuan_watch_panel"/);
  assert.match(template, /id="create_zixuan_group"[^>]*aria-expanded="false"/);
  assert.match(template, /id="zixuan_group_creator"[^>]*hidden/);
  assert.match(template, /id="zixuan_group_name"[^>]*maxlength="64"/);
  assert.match(template, /id="zixuan_watch_status"[^>]*aria-live="polite"/);
  assert.match(template, /id="zixuan_group_count"/);
  assert.match(template, /id="zixuan_stock_count"/);
  assert.match(css, /\.zx-watch-panel\s*\{/);
  assert.match(css, /@container\s+zx-watch/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /\.zx-watch-table\s*\{[^}]*overflow:\s*hidden/s);
  assert.match(css, /\.zx-watch-table \.layui-table-body\s*\{[^}]*overflow-x:\s*hidden[^}]*overflow-y:\s*auto/s);
  assert.match(css, /\.zx-group-picker \.zx-secondary-button\s*\{[^}]*width:\s*auto/s);
  const source = read(path.join(staticRoot, 'js', 'zixuan.js'));
  assert.match(source, /function formatQuotePrice\(value, market\)/);
  assert.match(source, /priceLine\.textContent = formatQuotePrice\(price, market\)/);
  assert.match(source, /title:\s*"涨跌\/现价"[\s\S]*?width:\s*92/);
});

test('creating a group normalizes its name, selects it, and loads its stocks', () => {
  const h = loadZiXuan();

  const accepted = h.ZiXuan.create_group('  趋势启动  ');

  assert.equal(accepted, true);
  assert.equal(h.ajaxCalls.length, 1);
  assert.equal(h.ajaxCalls[0].type, 'POST');
  assert.equal(h.ajaxCalls[0].url, '/opt_zixuan_group/a');
  assert.deepEqual(
    JSON.parse(JSON.stringify(h.ajaxCalls[0].data)),
    { opt: 'ADD', zx_group: '趋势启动' },
  );

  h.ajaxCalls[0].success({ ok: true, group: '趋势启动', msg: '分组已创建' });
  assert.equal(h.ajaxCalls.length, 2);
  assert.equal(h.ajaxCalls[1].url, '/get_zixuan_groups/a');

  h.ajaxCalls[1].success([{ name: '我的关注' }, { name: '趋势启动' }]);
  assert.equal(h.ZiXuan.zx_group, '趋势启动');
  assert.equal(h.ajaxCalls.length, 4);
  assert.equal(
    h.ajaxCalls.find((call) => call.url.startsWith('/get_zixuan_stocks/')).url,
    '/get_zixuan_stocks/a/%E8%B6%8B%E5%8A%BF%E5%90%AF%E5%8A%A8',
  );
  assert.equal(
    h.ajaxCalls.find((call) => call.url.startsWith('/get_stock_zixuan/')).url,
    '/get_stock_zixuan/a/SH.600000',
  );
  assert.equal(h.text('#zixuan_group_count'), '2');
});

test('invalid group names fail locally without issuing a request', () => {
  const h = loadZiXuan();

  assert.equal(h.ZiXuan.create_group('   '), false);
  assert.equal(h.ZiXuan.create_group('非法/分组'), false);
  assert.equal(h.ajaxCalls.length, 0);
  assert.match(h.text('#zixuan_group_error'), /不能为空|不能包含/);
});

test('group creation suppresses duplicate submissions while the request is active', () => {
  const h = loadZiXuan();

  assert.equal(h.ZiXuan.create_group('盘中观察'), true);
  assert.equal(h.ZiXuan.create_group('盘中观察'), false);
  assert.equal(h.ajaxCalls.length, 1);

  h.ajaxCalls[0].complete();
  assert.equal(h.ZiXuan.create_group('盘中观察'), true);
  assert.equal(h.ajaxCalls.length, 2);
});

test('group manager separates creation, group list, and file transfer workflows', () => {
  const template = read(path.join(templateRoot, 'zixuan.html'));

  assert.match(template, /class="zx-manager-shell"/);
  assert.match(template, /id="zixuan_manager_add_form"/);
  assert.match(template, /id="zixuan_manager_group_list"/);
  assert.match(template, /id="zixuan_transfer_form"/);
  assert.doesNotMatch(template, /name="zixuan_opt"/);
  assert.match(template, /AppRequest\.ajax\(\{[^]*\/opt_zixuan_group\//);
  assert.match(template, /confirmMessage\.textContent\s*=/);
});
