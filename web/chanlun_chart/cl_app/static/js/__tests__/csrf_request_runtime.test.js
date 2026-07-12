'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function response(status, options = {}) {
  const body = options.body || '';
  const value = {
    status,
    ok: status >= 200 && status < 300,
    redirected: options.redirected === true,
    url: options.url || 'http://app.local/api',
    text: async () => body,
    json: async () => options.json || {},
  };
  value.clone = () => response(status, options);
  return value;
}

function loadLayer(nativeFetch, jQuery) {
  const template = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'templates', 'dark.html'),
    'utf8',
  );
  const start = template.indexOf('>') + 1;
  const scriptStart = template.indexOf('>', template.indexOf('<script')) + 1;
  const scriptEnd = template.indexOf('</script>', scriptStart);
  const meta = { content: 'old-token' };
  let assigned = null;
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    fetch: nativeFetch,
    jQuery,
    Headers,
    URL,
    Promise,
    setTimeout,
    clearTimeout,
    location: {
      href: 'http://app.local/page?a=1',
      origin: 'http://app.local',
      pathname: '/page',
      search: '?a=1',
      assign(url) { assigned = url; },
    },
    localStorage: { tv_chart: '{}' },
    document: {
      querySelector(selector) {
        return selector === 'meta[name="csrf-token"]' ? meta : null;
      },
      getElementsByTagName() { return [{ appendChild() {} }]; },
      createElement() { return {}; },
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(template.slice(scriptStart, scriptEnd), sandbox, { filename: 'dark-request-layer.js' });
  return { sandbox, meta, assigned: () => assigned, start };
}

function makeJqueryHarness() {
  const requests = [];
  function jquery() { return {}; }
  jquery.ajaxSetup = () => {};
  jquery.ajax = (options) => {
    const doneHandlers = [];
    const failHandlers = [];
    const request = {
      options,
      done(handler) { doneHandlers.push(handler); return this; },
      fail(handler) { failHandlers.push(handler); return this; },
      abort() {},
      resolve(data, textStatus = 'success', xhr = {}) {
        doneHandlers.forEach((handler) => handler(data, textStatus, xhr));
      },
      reject(xhr, textStatus = 'error', error = new Error('failed')) {
        failHandlers.forEach((handler) => handler(xhr, textStatus, error));
      },
    };
    requests.push(request);
    return request;
  };
  jquery.Deferred = () => {
    let state = 'pending';
    let settledContext;
    let settledArgs;
    const doneHandlers = [];
    const failHandlers = [];
    const notify = (handlers) => handlers.forEach((handler) => handler.apply(settledContext, settledArgs));
    const promise = {
      done(handler) {
        if (state === 'resolved') handler.apply(settledContext, settledArgs);
        else if (state === 'pending') doneHandlers.push(handler);
        return this;
      },
      fail(handler) {
        if (state === 'rejected') handler.apply(settledContext, settledArgs);
        else if (state === 'pending') failHandlers.push(handler);
        return this;
      },
    };
    return {
      resolveWith(context, args) {
        if (state !== 'pending') return;
        state = 'resolved'; settledContext = context; settledArgs = args; notify(doneHandlers);
      },
      rejectWith(context, args) {
        if (state !== 'pending') return;
        state = 'rejected'; settledContext = context; settledArgs = args; notify(failHandlers);
      },
      promise() { return promise; },
    };
  };
  return { jquery, requests };
}
test('expired CSRF refreshes from /api/session and retries the unsafe fetch once', async () => {
  const calls = [];
  const h = loadLayer(async (input, init = {}) => {
    const url = String(input);
    calls.push({ url, init });
    if (url === '/api/session') {
      return response(200, { json: { ok: true, csrf_token: 'new-token' } });
    }
    if (calls.filter((call) => call.url === '/save').length === 1) {
      return response(400, { body: 'The CSRF token has expired.' });
    }
    return response(200, { body: '{"ok":true}' });
  });

  const result = await h.sandbox.fetch('/save', { method: 'POST' });

  assert.equal(result.status, 200);
  assert.deepEqual(calls.map((call) => call.url), ['/save', '/api/session', '/save']);
  assert.equal(calls[2].init.headers.get('X-CSRFToken'), 'new-token');
  assert.equal(h.meta.content, 'new-token');
});

test('ordinary HTTP 400 is not retried', async () => {
  const calls = [];
  const h = loadLayer(async (input, init = {}) => {
    calls.push({ input: String(input), init });
    return response(400, { body: '{"error":"invalid_market"}' });
  });

  const result = await h.sandbox.fetch('/save', { method: 'POST' });

  assert.equal(result.status, 400);
  assert.equal(calls.length, 1);
});

test('HTTP 401 redirects to login with the current path as next', async () => {
  const h = loadLayer(async () => response(401, { body: '{"error":"authentication_required"}' }));

  await h.sandbox.fetch('/private', { method: 'GET' });

  assert.equal(h.assigned(), '/login?next=%2Fpage%3Fa%3D1');
});
test('jQuery CSRF failure retries once without firing premature callbacks', async () => {
  const jq = makeJqueryHarness();
  const h = loadLayer(
    async () => response(200, { json: { ok: true, csrf_token: 'jquery-token' } }),
    jq.jquery,
  );
  let successCalls = 0;
  let errorCalls = 0;
  let completeCalls = 0;
  const result = h.sandbox.AppRequest.ajax({
    url: '/save',
    method: 'POST',
    success() { successCalls += 1; },
    error() { errorCalls += 1; },
    complete() { completeCalls += 1; },
  });
  const finished = new Promise((resolve, reject) => result.done(resolve).fail(reject));

  jq.requests[0].reject({
    status: 400,
    responseText: 'The CSRF token has expired.',
    responseURL: 'http://app.local/save',
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(jq.requests.length, 2);
  assert.equal(successCalls, 0);
  assert.equal(errorCalls, 0);
  assert.equal(completeCalls, 0);

  jq.requests[1].resolve({ ok: true }, 'success', {
    status: 200,
    responseURL: 'http://app.local/save',
  });
  await finished;

  assert.equal(successCalls, 1);
  assert.equal(errorCalls, 0);
  assert.equal(completeCalls, 1);
  assert.equal(h.meta.content, 'jquery-token');
});test('jQuery abort always settles the proxy and runs cleanup once', async () => {
  const jq = makeJqueryHarness();
  const h = loadLayer(async () => response(200), jq.jquery);
  let errorCalls = 0;
  let completeCalls = 0;
  const result = h.sandbox.AppRequest.ajax({
    url: '/slow',
    method: 'POST',
    error() { errorCalls += 1; },
    complete() { completeCalls += 1; },
  });
  const settled = new Promise((resolve) => result.fail(resolve));

  result.abort();
  await settled;

  assert.equal(errorCalls, 1);
  assert.equal(completeCalls, 1);
});

test('throwing success callback cannot skip complete cleanup', async () => {
  const jq = makeJqueryHarness();
  const h = loadLayer(async () => response(200), jq.jquery);
  let completeCalls = 0;
  const result = h.sandbox.AppRequest.ajax({
    url: '/save',
    method: 'POST',
    success() { throw new Error('consumer failed'); },
    complete() { completeCalls += 1; },
  });
  const settled = new Promise((resolve) => result.done(resolve));

  assert.doesNotThrow(() => jq.requests[0].resolve({ ok: true }, 'success', {
    status: 200,
    responseURL: 'http://app.local/save',
  }));
  await settled;

  assert.equal(completeCalls, 1);
});test('throwing done handler cannot skip complete cleanup', () => {
  const jq = makeJqueryHarness();
  const h = loadLayer(async () => response(200), jq.jquery);
  let completeCalls = 0;
  const result = h.sandbox.AppRequest.ajax({
    url: '/save',
    method: 'POST',
    complete() { completeCalls += 1; },
  });
  result.done(() => { throw new Error('done consumer failed'); });

  assert.doesNotThrow(() => jq.requests[0].resolve({ ok: true }, 'success', {
    status: 200,
    responseURL: 'http://app.local/save',
  }));
  assert.equal(completeCalls, 1);
});