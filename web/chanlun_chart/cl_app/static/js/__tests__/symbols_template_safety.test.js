'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

function createElement(tagName) {
  const tag = String(tagName).toLowerCase();
  return {
    className: '',
    href: '',
    target: '',
    rel: '',
    textContent: '',
    get outerHTML() {
      const attrs = [
        this.className && `class="${escapeHtml(this.className)}"`,
        this.href && `href="${escapeHtml(this.href)}"`,
        this.target && `target="${escapeHtml(this.target)}"`,
        this.rel && `rel="${escapeHtml(this.rel)}"`,
      ].filter(Boolean).join(' ');
      return `<${tag}${attrs ? ` ${attrs}` : ''}>${escapeHtml(this.textContent)}</${tag}>`;
    },
  };
}

function loadCodeTemplate() {
  let renderOptions = null;
  function $(value) {
    if (typeof value === 'function') { value(); return undefined; }
    return {
      val(next) {
        if (arguments.length > 0) return this;
        return value === '#market_select' ? 'a' : '';
      },
      click() { return this; },
      keydown() { return this; },
      text() { return this; },
    };
  }
  const table = { render(options) { renderOptions = options; } };
  const sandbox = {
    $,
    layui: {
      form: { on() {} },
      table,
      layer: {},
      use(_deps, callback) { callback(); },
    },
    document: { createElement },
    encodeURIComponent,
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const template = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'templates', 'symbols.html'),
    'utf8',
  );
  const start = template.indexOf('$(function () {');
  const end = template.indexOf('</script>', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  vm.createContext(sandbox);
  vm.runInContext(template.slice(start, end), sandbox, { filename: 'symbols-inline.js' });
  return renderOptions.cols[0][0].templet;
}

test('symbols code link renders untrusted code as text while preserving its route', () => {
  const template = loadCodeTemplate();
  const code = '<img src=x onerror=alert(1)>';

  const html = template({ code });

  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /href="\/\?market=a&amp;code=%3Cimg%20src%3Dx/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener"/);
});
