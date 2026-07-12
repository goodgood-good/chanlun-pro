'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function selectableCollection() {
  const leaf = {
    0: { click() {} },
    length: 1,
    click() { return this; },
  };
  const middle = { find() { return leaf; } };
  return {
    options: [],
    empty() { this.options.length = 0; return this; },
    html() { return this.options.map((item) => item.value).join(','); },
    append(option) {
      if (typeof option === 'string') {
        const match = option.match(/value=['"]([^'"]*)/);
        this.options.push({ value: match ? match[1] : '', text: match ? match[1] : '' });
      } else {
        this.options.push({ value: option.value, text: option.text });
      }
      return this;
    },
    siblings() { return { find() { return middle; } }; },
  };
}

function genericCollection() {
  return {
    click() { return this; },
    on() { return this; },
    val() { return this; },
    addClass() { return this; },
    removeClass() { return this; },
    attr() { return this; },
    html() { return this; },
  };
}

function optionNode(props) {
  return { value: props.value, text: props.text };
}

function loadZiXuanSelect() {
  const select = selectableCollection();
  const ajaxCalls = [];
  function $(value, props) {
    if (value === '#zixuan_groups' || value === select) return select;
    if (value === '<option>') return optionNode(props || {});
    return genericCollection();
  }
  $.ajax = (options) => { ajaxCalls.push(options); return {}; };
  const layui = {
    table: { render() {}, on() {}, setRowChecked() {} },
    dropdown: { render() {}, reloadData() {} },
    form: { render() {}, on() {} },
    use(deps, callback) {
      if (typeof deps === 'function') deps();
      else callback();
    },
    each(values, callback) { values.forEach((value, index) => callback(index, value)); },
  };
  const sandbox = {
    $,
    layui,
    xmSelect: { render() { return { update() {} }; } },
    Utils: {
      get_market() { return 'a'; },
      get_code() { return 'a:alpha'; },
      get_selected_items() { return []; },
    },
    document: { createElement() { return {}; }, createTextNode() { return {}; } },
    setTimeout() { return 1; },
    clearTimeout() {},
    Map,
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
  const source = fs.readFileSync(path.join(__dirname, '..', 'zixuan.js'), 'utf8');
  vm.runInContext(source + '\n;globalThis.__ZiXuan = ZiXuan;', sandbox, { filename: 'zixuan.js' });
  return { ZiXuan: sandbox.__ZiXuan, ajaxCalls, select };
}

function loadAiSelect() {
  const select = selectableCollection();
  function $(value, props) {
    if (value === '#ai_frequencys' || value === select) return select;
    if (value === '<option>') return optionNode(props || {});
    return genericCollection();
  }
  const layui = {
    form: { render() {} },
    each(values, callback) { values.forEach((value, index) => callback(index, value)); },
    use() {},
  };
  const sandbox = {
    $,
    layui,
    market_frequencys: { a: ['d', '5m'] },
    Utils: { get_market() { return 'a'; }, get_code() { return 'a:alpha'; } },
    SafeHtml: { escapeText(value) { return String(value); }, renderMarkdown(value) { return String(value); } },
    change_chart_ticker() {},
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '..', 'ai.js'), 'utf8');
  vm.runInContext(source + '\n;globalThis.__AI = AI;', sandbox, { filename: 'ai.js' });
  return { AI: sandbox.__AI, select };
}

test('watchlist group initialization replaces options instead of duplicating them', () => {
  const h = loadZiXuanSelect();
  const groups = [{ name: 'Core' }, { name: 'Watch' }];

  h.ZiXuan.init_zixuan_opts();
  h.ajaxCalls[0].success(groups);
  h.ZiXuan.init_zixuan_opts();
  h.ajaxCalls[1].success(groups);

  assert.deepEqual(h.select.options.map((item) => item.value), ['Core', 'Watch']);
});

test('AI frequency initialization replaces options instead of duplicating them', () => {
  const h = loadAiSelect();

  h.AI.init_ai_opts();
  h.AI.init_ai_opts();

  assert.deepEqual(h.select.options.map((item) => item.value), ['d', '5m']);
});
