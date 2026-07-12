'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadPrewarmController() {
  const requests = [];
  const timeouts = [];
  const intervals = [];
  const applied = [];

  function deferredRequest(options) {
    const request = {
      options,
      doneHandler: null,
      failHandler: null,
      alwaysHandler: null,
      resolve(payload) {
        if (typeof options.success === 'function') options.success(payload, 'success', {});
        if (this.doneHandler) this.doneHandler(payload, 'success', {});
        if (typeof options.complete === 'function') options.complete({}, 'success');
        if (this.alwaysHandler) this.alwaysHandler({}, 'success');
      },
      reject() {
        if (typeof options.error === 'function') options.error({}, 'error', new Error('failed'));
        if (this.failHandler) this.failHandler({}, 'error', new Error('failed'));
        if (typeof options.complete === 'function') options.complete({}, 'error');
        if (this.alwaysHandler) this.alwaysHandler({}, 'error');
      },
    };
    const chain = {
      done(handler) { request.doneHandler = handler; return this; },
      fail(handler) { request.failHandler = handler; return this; },
      always(handler) { request.alwaysHandler = handler; return this; },
    };
    request.chain = chain;
    requests.push(request);
    return chain;
  }

  function jqueryObject() {
    return {
      on() { return this; },
      hide() { return this; },
      show() { return this; },
      text() { return this; },
      css() { return this; },
      removeClass() { return this; },
      addClass() { return this; },
    };
  }

  function $(selector) {
    return jqueryObject(selector);
  }
  $.ajax = deferredRequest;

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    $,
    AppRequest: { ajax: deferredRequest },
    setTimeout(callback, delay) {
      const timer = { callback, delay, cleared: false, fired: false };
      timeouts.push(timer);
      return timeouts.length;
    },
    clearTimeout(id) {
      if (timeouts[id - 1]) timeouts[id - 1].cleared = true;
    },
    setInterval(callback, delay) {
      intervals.push({ callback, delay, cleared: false });
      return intervals.length;
    },
    clearInterval(id) {
      if (intervals[id - 1]) intervals[id - 1].cleared = true;
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const template = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'templates', 'index.html'),
    'utf8',
  );
  const start = template.indexOf('var PrewarmController = {');
  const marker = template.indexOf('// SymbolsPanel', start);
  const controllerEnd = template.lastIndexOf('        };', marker);
  const end = controllerEnd === -1 ? -1 : controllerEnd + '        };'.length;
  assert.notEqual(start, -1, 'PrewarmController source must exist');
  assert.notEqual(end, -1, 'PrewarmController source terminator must exist');

  vm.createContext(sandbox);
  vm.runInContext(
    template.slice(start, end) + '\n;globalThis.__controller = PrewarmController;',
    sandbox,
    { filename: 'PrewarmController.js' },
  );

  const controller = sandbox.__controller;
  controller.symbolsPanel = { state: { market: 'a' } };
  controller.applyTaskToUI = (task) => applied.push(task);
  controller.setBtnText = () => {};

  return {
    controller,
    requests,
    timeouts,
    intervals,
    applied,
    fireNextTimeout() {
      const timer = timeouts.find((item) => !item.cleared && !item.fired);
      assert.ok(timer, 'expected an active timeout');
      timer.fired = true;
      timer.callback();
      return timer;
    },
  };
}

test('prewarm polling is completion-driven and single-flight', () => {
  const h = loadPrewarmController();

  h.controller.startPolling();

  assert.equal(h.intervals.length, 0);
  assert.equal(h.timeouts.length, 1);
  assert.equal(h.timeouts[0].delay, 1500);

  h.fireNextTimeout();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.timeout, 8000);

  h.controller.poll();
  assert.equal(h.requests.length, 1, 'a second status request must not overlap');

  h.requests[0].resolve({ ok: true, task: { status: 'running' } });
  assert.equal(h.timeouts.length, 2);
  assert.equal(h.timeouts[1].delay, 1500);

  h.fireNextTimeout();
  assert.equal(h.requests.length, 2);
});

test('market change invalidates older status responses', () => {
  const h = loadPrewarmController();
  const oldTask = { status: 'finished', market: 'a' };
  const currentTask = { status: 'finished', market: 'hk' };

  h.controller.fetchStatusOnce();
  h.controller.symbolsPanel.state.market = 'hk';
  h.controller.refreshOnMarketChange();
  h.applied.length = 0;

  assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ ok: true, task: currentTask });
  h.requests[0].resolve({ ok: true, task: oldTask });

  assert.deepEqual(h.applied, [currentTask]);
});
test('cancel requested while start is in flight is serialized after start completes', () => {
  const h = loadPrewarmController();

  h.controller.start();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.url, '/symbols/prewarm');
  assert.equal(h.requests[0].options.timeout, 10000);

  h.controller.cancel();
  assert.equal(h.requests.length, 1, 'cancel must wait until the start response establishes a task');

  h.requests[0].resolve({ ok: true, task: { status: 'running', market: 'a' } });
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].options.url, '/symbols/prewarm/cancel');
  assert.equal(h.requests[1].options.timeout, 10000);
});test('failed start reconciles with status in case the server accepted it', () => {
  const h = loadPrewarmController();

  h.controller.start();
  h.requests[0].reject();

  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].options.url, '/symbols/prewarm/status');
  assert.equal(h.requests[1].options.timeout, 8000);
});

test('failed deferred cancel resumes polling instead of freezing the UI', () => {
  const h = loadPrewarmController();

  h.controller.start();
  h.controller.cancel();
  h.requests[0].resolve({ ok: true, task: { status: 'running', market: 'a' } });
  h.requests[1].reject();

  assert.equal(h.timeouts.length, 1);
  assert.equal(h.timeouts[0].delay, 1500);
});
