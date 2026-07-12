'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const templatePath = path.join(__dirname, '..', '..', '..', 'templates', 'index.html');

function readTemplate() {
  return fs.readFileSync(templatePath, 'utf8');
}

function startTagFor(template, id) {
  const match = template.match(new RegExp(`<([a-z][a-z0-9-]*)\\b([^>]*\\bid=["']${id}["'][^>]*)>`, 'i'));
  assert.ok(match, `expected #${id} in index.html`);
  return { tagName: match[1].toLowerCase(), source: match[0] };
}

function createElement() {
  const attributes = new Map();
  const classes = new Set();
  const listeners = new Map();
  return {
    style: {},
    title: '',
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
    addEventListener(name, handler) { listeners.set(name, handler); },
    dispatch(name, event = {}) {
      const handler = listeners.get(name);
      if (handler) handler(event);
    },
  };
}

function loadResizeModule(initialCollapsed = false) {
  const template = readTemplate();
  const marker = template.indexOf("var STORAGE_KEY = 'chart_menu_width';");
  const start = template.lastIndexOf('(function () {', marker);
  const terminator = template.indexOf('})();', marker);
  assert.notEqual(marker, -1, 'resize module marker must exist');
  assert.notEqual(start, -1, 'resize module start must exist');
  assert.notEqual(terminator, -1, 'resize module end must exist');

  const elements = {
    chart_menu: createElement(),
    chart_container: createElement(),
    chart_resize_handle: createElement(),
    chart_menu_toggle: createElement(),
    'toggle-menu': createElement(),
  };
  const body = createElement();
  const storage = new Map([
    ['chart_menu_collapsed', initialCollapsed ? '1' : '0'],
  ]);
  const sandbox = {
    document: {
      readyState: 'complete',
      body,
      getElementById(id) { return elements[id] || null; },
      addEventListener() {},
    },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
    },
    innerWidth: 1200,
    chart_widgets: [],
    addEventListener() {},
    dispatchEvent() {},
    Event: function Event(type) { this.type = type; },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(template.slice(start, terminator + '})();'.length), sandbox, {
    filename: 'chart-menu-resize.js',
  });
  return { sandbox, elements, body };
}

test('sidebar controls use native buttons and preserve the expand handle dimensions', () => {
  const template = readTemplate();
  const expand = startTagFor(template, 'chart_menu_toggle');
  const menu = startTagFor(template, 'toggle-menu');

  assert.equal(expand.tagName, 'button');
  assert.equal(menu.tagName, 'button');
  assert.match(expand.source, /\btype=["']button["']/i);
  assert.match(menu.source, /\btype=["']button["']/i);
  assert.match(expand.source, /\baria-controls=["']chart_menu["']/i);
  assert.match(menu.source, /\baria-controls=["']chart_menu["']/i);
  assert.match(expand.source, /width:\s*18px/i);
  assert.match(expand.source, /height:\s*48px/i);
});

test('keyboard focus remains visibly styled for sidebar controls and lists', () => {
  const template = readTemplate();

  assert.doesNotMatch(template, /outline\s*:\s*none/i);
  for (const id of ['chart_menu_toggle', 'toggle-menu', 'symbols_list_wrap', 'bkgn_stock_wrap']) {
    assert.match(template, new RegExp(`#${id}:focus-visible\\b`));
  }
});

test('sidebar collapse and expand synchronize accessible state', () => {
  const { sandbox, elements, body } = loadResizeModule(false);
  const expand = elements.chart_menu_toggle;
  const menu = elements['toggle-menu'];

  assert.equal(expand.getAttribute('aria-expanded'), 'true');
  assert.equal(menu.getAttribute('aria-expanded'), 'true');
  assert.match(expand.getAttribute('aria-label'), /收起/);
  assert.match(menu.getAttribute('aria-label'), /收起/);

  sandbox.ChartMenuResize.collapse();
  assert.equal(body.classList.contains('chart-menu-collapsed'), true);
  assert.equal(expand.getAttribute('aria-expanded'), 'false');
  assert.equal(menu.getAttribute('aria-expanded'), 'false');
  assert.match(expand.getAttribute('aria-label'), /展开/);
  assert.match(menu.getAttribute('aria-label'), /展开/);

  sandbox.ChartMenuResize.expand();
  assert.equal(body.classList.contains('chart-menu-collapsed'), false);
  assert.equal(expand.getAttribute('aria-expanded'), 'true');
  assert.equal(menu.getAttribute('aria-expanded'), 'true');
  assert.match(expand.getAttribute('aria-label'), /收起/);
  assert.match(menu.getAttribute('aria-label'), /收起/);
});
