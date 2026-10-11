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
    path.join(__dirname, '..', 'app_request.js'),
    'utf8',
  );
  const meta = { content: 'old-token' };
  const events = {};
  let assigned = null;
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    fetch: nativeFetch,
    jQuery,
    Headers,
    URL,
    Request,
    AbortController,
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
      addEventListener(name, handler) { events[name] = handler; },
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
  vm.runInContext(template, sandbox, { filename: 'app_request.js' });
  return { sandbox, meta, events, assigned: () => assigned };
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
});

test('jQuery abort always settles the proxy and runs cleanup once', async () => {
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
});

test('throwing done handler cannot skip complete cleanup', () => {
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

const expired = () => response(400, {body: JSON.stringify({code:'csrf_failed',errmsg:'CSRF token is missing or expired.'})});
const tick = () => new Promise(setImmediate);

test('parallel page writes share one token refresh and each rejected write is retried once', async () => {
  let release,refreshes=0;
  const calls=[];
  const h=loadLayer(async(url,init)=>{
    if(url==='/api/session'){refreshes++;return new Promise(resolve=>{release=resolve;});}
    calls.push({url,token:init.headers.get('X-CSRFToken')});
    return init.headers.get('X-CSRFToken')==='old-token'?expired():response(200);
  });
  const a=h.sandbox.fetch('/screening/reviews',{method:'POST',body:'a'});
  const b=h.sandbox.fetch('/monitor/start',{method:'POST',body:'b'});
  await tick();assert.equal(refreshes,1);
  release(response(200,{json:{csrf_token:'fresh-token'}}));
  assert.deepEqual((await Promise.all([a,b])).map(r=>r.status),[200,200]);
  for(const url of ['/screening/reviews','/monitor/start'])
    assert.deepEqual(calls.filter(c=>c.url===url).map(c=>c.token),['old-token','fresh-token']);
});

test('a late rejection carrying the old token uses the already refreshed token', async () => {
  let releaseLate,refreshes=0;
  const h=loadLayer(async(url,init)=>{
    if(url==='/api/session'){refreshes++;return response(200,{json:{csrf_token:'fresh-token'}});}
    if(url==='/late'&&init.headers.get('X-CSRFToken')==='old-token')return new Promise(resolve=>{releaseLate=resolve;});
    return init.headers.get('X-CSRFToken')==='old-token'?expired():response(200);
  });
  const late=h.sandbox.fetch('/late',{method:'POST'});
  await h.sandbox.fetch('/first',{method:'POST'});
  releaseLate(expired());
  assert.equal((await late).status,200);assert.equal(refreshes,1);
});

test('aborting one writer during shared refresh cannot replay it or abort another writer', async () => {
  let release;
  const calls=[];
  const h=loadLayer(async(url,init)=>{
    if(url==='/api/session')return new Promise(resolve=>{release=resolve;});
    calls.push(url);
    return init.headers.get('X-CSRFToken')==='old-token'?expired():response(200);
  });
  const controller=new AbortController();
  const cancelled=h.sandbox.fetch('/cancelled',{method:'POST',signal:controller.signal});
  const rejected=assert.rejects(cancelled,error=>error.name==='AbortError');
  const other=h.sandbox.fetch('/other',{method:'POST'});
  await tick();controller.abort();await rejected;
  release(response(200,{json:{csrf_token:'fresh-token'}}));
  assert.equal((await other).status,200);
  assert.equal(calls.filter(url=>url==='/cancelled').length,1);
  assert.equal(calls.filter(url=>url==='/other').length,2);
});

test('a second CSRF rejection is returned and a business error mentioning csrf is never replayed', async () => {
  let writes=0,refreshes=0;
  const h=loadLayer(async url=>{
    if(url==='/api/session'){refreshes++;return response(200,{json:{csrf_token:'fresh-token'}});}
    writes++;return expired();
  });
  assert.equal((await h.sandbox.fetch('/save',{method:'POST'})).status,400);
  assert.equal(writes,2);assert.equal(refreshes,1);
  let ordinary=0;
  const other=loadLayer(async()=>{ordinary++;return response(400,{body:JSON.stringify({error:'invalid_field',msg:'csrf_notes is not supported'})});});
  assert.equal((await other.sandbox.fetch('/save',{method:'POST'})).status,400);
  assert.equal(ordinary,1);
});

test('network errors and timeout aborts cannot trigger token refresh or duplicate writes', async () => {
  let calls=0;
  const h=loadLayer(async()=>{calls++;throw new TypeError('offline');});
  await assert.rejects(h.sandbox.fetch('/save',{method:'POST'}),/offline/);
  assert.equal(calls,1);
  const controller=new AbortController();controller.abort();
  await assert.rejects(h.sandbox.fetch('/save',{method:'POST',signal:controller.signal}),error=>error.name==='AbortError');
  assert.equal(calls,1);
});

test('request objects retain their body for a single CSRF replay', async () => {
  const bodies=[];
  const h=loadLayer(async(input)=>{
    if(input==='/api/session')return response(200,{json:{csrf_token:'fresh-token'}});
    bodies.push(await input.text());
    return bodies.length===1?expired():response(200);
  });
  const input=new Request('http://app.local/save',{method:'POST',body:'review-note'});
  assert.equal((await h.sandbox.fetch(input)).status,200);
  assert.deepEqual(bodies,['review-note','review-note']);
});

test('loading the shared layer twice does not wrap fetch a second time', () => {
  const h=loadLayer(async()=>response(200));
  const original=h.sandbox.fetch,api=h.sandbox.AppRequest;
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../app_request.js'),'utf8'),h.sandbox);
  assert.equal(h.sandbox.fetch,original);assert.equal(h.sandbox.AppRequest,api);
});

test('aborting jQuery during token refresh settles once without sending a second POST', async () => {
  let release;
  const jq=makeJqueryHarness();
  const h=loadLayer(()=>new Promise(resolve=>{release=resolve;}),jq.jquery);
  let completed=0;
  const request=h.sandbox.AppRequest.ajax({url:'/save',method:'POST',complete(){completed++;}});
  const failed=new Promise(resolve=>request.fail(resolve));
  jq.requests[0].reject({status:400,responseText:'The CSRF token has expired.'});
  request.abort();await failed;
  release(response(200,{json:{csrf_token:'fresh-token'}}));await tick();
  assert.equal(jq.requests.length,1);assert.equal(completed,1);
});

test('long-open native logout refreshes once while intercepted forms and login remain untouched', async () => {
  let refreshes=0,submits=0,prevented=0;
  const h=loadLayer(async()=>{refreshes++;return response(200,{json:{csrf_token:'fresh-token'}});});
  h.sandbox.Date={now:()=>Date.now()+11*60*60*1000};
  const form={method:'POST',action:'http://app.local/logout',submit(){submits++;}};
  h.events.submit({target:form,defaultPrevented:true,preventDefault(){prevented++;}});
  assert.equal(refreshes,0);
  h.sandbox.location.pathname='/login';
  h.events.submit({target:form,defaultPrevented:false,preventDefault(){prevented++;}});
  assert.equal(refreshes,0);
  h.sandbox.location.pathname='/';
  h.events.submit({target:form,defaultPrevented:false,preventDefault(){prevented++;}});
  await tick();
  assert.equal(refreshes,1);assert.equal(submits,1);assert.equal(prevented,1);
});
