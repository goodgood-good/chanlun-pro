'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadBkgn() {
  const getRequests = [];
  const postRequests = [];
  const tableHandlers = new Map();
  const stockRenders = [];
  const stockTemplates = [];
  const closedLoads = [];
  const messages = [];
  let nextLoadId = 0;

  function requestChain(options, legacyCallback) {
    const request = {
      options,
      legacyCallback,
      doneHandler: null,
      failHandler: null,
      alwaysHandler: null,
      resolve(payload) {
        if (this.legacyCallback) this.legacyCallback(payload);
        if (this.doneHandler) this.doneHandler(payload);
        if (this.alwaysHandler) this.alwaysHandler();
      },
      reject() {
        if (this.failHandler) this.failHandler({}, 'error', new Error('failed'));
        if (this.alwaysHandler) this.alwaysHandler();
      },
    };
    const chain = {
      done(handler) { request.doneHandler = handler; return this; },
      fail(handler) { request.failHandler = handler; return this; },
      always(handler) { request.alwaysHandler = handler; return this; },
    };
    request.chain = chain;
    return request;
  }

  function jqueryObject() {
    return {
      length: 1,
      off() { return this; },
      on() { return this; },
      toggleClass() { return this; },
      slideToggle() { return this; },
      text() { return this; },
      val() { return this; },
      focus() { return this; },
    };
  }

  function $() {
    return jqueryObject();
  }
  $.get = (url, callback) => {
    const request = requestChain({ url, method: 'GET' }, callback);
    getRequests.push(request);
    return request.chain;
  };
  $.post = (url, data, callback) => {
    const request = requestChain({ url, method: 'POST', data }, callback);
    postRequests.push(request);
    return request.chain;
  };  $.ajax = (options) => {
    const request = requestChain(options);
    if (String(options.type || options.method || 'GET').toUpperCase() === 'POST') {
      postRequests.push(request);
    } else {
      getRequests.push(request);
    }
    return request.chain;
  };

  const table = {
    cache: {},
    render(options) {
      if (options.elem === '#bkgn_stock_table') {
        stockRenders.push(options.data.map((item) => ({ ...item })));
        stockTemplates.push(options.cols[0][0].templet);
      }
      if (typeof options.done === 'function') options.done();
    },
    on(name, handler) { tableHandlers.set(name, handler); },
    setRowChecked() {},
  };
  const layui = { table };
  const layer = {
    load() { nextLoadId += 1; return nextLoadId; },
    close(id) { closedLoads.push(id); },
    closeAll() {},
    msg(message) { messages.push(message); },
  };
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    $,
    AppRequest: { ajax: $.ajax },
    layui,
    layer,
    Utils: { get_market() { return 'a'; } },
    change_chart_ticker() {},
    document: {
      createElement(tagName) {
        const tag = String(tagName).toLowerCase();
        return {
          className: '',
          textContent: '',
          get outerHTML() {
            const escape = (value) => String(value).replace(/[&<>"']/g, (ch) => ({
              '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
            })[ch]);
            const classAttr = this.className ? ` class="${escape(this.className)}"` : '';
            return `<${tag}${classAttr}>${escape(this.textContent)}</${tag}>`;
          },
        };
      },
    },
    clearTimeout() {},
    setTimeout() { return 1; },
    Map,
    Object,
    String,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '..', 'bkgn.js'), 'utf8');
  vm.runInContext(source + '\n;globalThis.__BKGN = BKGN;', sandbox, { filename: 'bkgn.js' });

  return {
    BKGN: sandbox.__BKGN,
    getRequests,
    postRequests,
    stockRenders,
    stockTemplates,
    closedLoads,
    messages,
    openBoardList() {
      this.BKGN.init_bkgn_opts();
      this.getRequests[0].resolve({
        code: 0,
        data: [
          { type: 'hy', bkgn_code: 'A', bkgn_name: 'Board A' },
          { type: 'hy', bkgn_code: 'B', bkgn_name: 'Board B' },
        ],
      });
      const handler = tableHandlers.get('row(bkgn_table)');
      assert.equal(typeof handler, 'function');
      return handler;
    },
  };
}

test('board member request failure closes its loading overlay', () => {
  const h = loadBkgn();
  const clickBoard = h.openBoardList();

  clickBoard({ data: { type: 'hy', bkgn_code: 'A' }, index: 0 });
  assert.equal(h.postRequests.length, 1);
  h.postRequests[0].reject();

  assert.deepEqual(h.closedLoads, [1]);
  assert.deepEqual(h.messages, ['获取股票列表失败']);
});

test('late board response cannot overwrite the current selection', () => {
  const h = loadBkgn();
  const clickBoard = h.openBoardList();

  clickBoard({ data: { type: 'hy', bkgn_code: 'A' }, index: 0 });
  clickBoard({ data: { type: 'hy', bkgn_code: 'B' }, index: 1 });

  h.postRequests[1].resolve({ code: 0, data: { 'b-code': { name: 'B stock' } } });
  h.postRequests[0].resolve({ code: 0, data: { 'a-code': { name: 'A stock' } } });

  assert.deepEqual(
    h.stockRenders.map((rows) => Array.from(rows, (row) => row.code)),
    [['b-code']],
  );
  assert.deepEqual(h.closedLoads, [2, 1]);
});

test('board stock code template renders untrusted code as text', () => {
  const h = loadBkgn();
  const clickBoard = h.openBoardList();
  const code = '<img src=x onerror=alert(1)>';

  clickBoard({ data: { type: 'hy', bkgn_code: 'A' }, index: 0 });
  h.postRequests[0].resolve({ code: 0, data: { [code]: { name: 'Unsafe' } } });
  const html = h.stockTemplates[0]({ code });

  assert.doesNotMatch(html, /<img/i);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});
test('board list request has a deadline and exposes load failure', () => {
  const h = loadBkgn();

  h.BKGN.init_bkgn_opts();
  assert.equal(h.getRequests[0].options.timeout, 10000);
  h.getRequests[0].reject();

  assert.deepEqual(h.messages, ['获取板块概念失败']);
});
