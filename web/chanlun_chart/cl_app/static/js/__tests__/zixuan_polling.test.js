'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadZiXuan(customNodes) {
  const ajaxCalls = [];
  const timers = [];
  const intervalCalls = [];
  const nodes = customNodes || [{ code: 'sh.000001' }, { code: 'sz.000002' }];
  let replacements = 0;
  let currentMarket = 'a';
  let currentCode = 'sh.000001';
  let nowMs = 1_000_000;
  const watchStatus = { text: '', state: '' };

  class FakeDate extends Date {
    static now() { return nowMs; }
  }

  function collection(items) {
    return {
      each(callback) {
        items.forEach((item, index) => callback.call(item, index, item));
        return this;
      },
      filter(callback) {
        return collection(items.filter((item, index) => callback.call(item, index, item)));
      },
      data(key) {
        return items[0] ? items[0][key] : undefined;
      },
      change() {
        return this;
      },
      text() {
        return this;
      },
      attr() {
        return this;
      },
      replaceWith() {
        replacements += items.length;
        return this;
      },
    };
  }

  function $(value) {
    if (value === '.code_rate') return collection(nodes);
    if (value === '#zixuan_watch_status') {
      return {
        text(next) {
          if (arguments.length === 0) return watchStatus.text;
          watchStatus.text = String(next);
          return this;
        },
        attr(name, next) {
          if (name === 'data-state') watchStatus.state = String(next);
          return this;
        },
      };
    }
    if (value && typeof value === 'object' && Object.hasOwn(value, 'code')) {
      return collection([value]);
    }
    return collection([]);
  }
  $.ajax = (options) => {
    ajaxCalls.push(options);
    return {};
  };

  const table = {
    render(options) {
      options.done();
    },
    on() {},
    setRowChecked() {},
  };
  let dropdownData = null;
  const layui = {
    table,
    dropdown: {
      render() {},
      reloadData(_id, options) { dropdownData = options.data; },
    },
    use(deps, callback) {
      if (typeof deps === 'function') deps();
      else callback();
    },
    each(values, callback) {
      values.forEach((value, index) => callback(index, value));
    },
  };

  function createElement(tagName) {
    const tag = String(tagName || 'div').toLowerCase();
    return {
      tagName: tag,
      className: '',
      dataset: {},
      style: {},
      children: [],
      textContent: '',
      appendChild(child) {
        this.children.push(child);
      },
      get outerHTML() {
        if (tag === 'input') {
          const type = this.type ? ` type="${this.type}"` : '';
          const checked = this.defaultChecked ? ' checked=""' : '';
          return `<input${type}${checked}>`;
        }
        const content = this.children
          .map((child) => child.outerHTML || child.textContent || '')
          .join('');
        return `<${tag}>${content}</${tag}>`;
      },
    };
  }
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    document: {
      createElement,
      createTextNode(text) { return { textContent: String(text) }; },
    },
    Utils: {
      get_market() { return currentMarket; },
      get_code() { return currentCode; },
    },
    $,
    AppRequest: { ajax: $.ajax },
    layui,
    setTimeout(callback, delay) {
      const timer = { callback, delay, cleared: false, fired: false };
      timers.push(timer);
      return timers.length;
    },
    clearTimeout(id) {
      if (timers[id - 1]) timers[id - 1].cleared = true;
    },
    setInterval(callback, delay) {
      intervalCalls.push({ callback, delay });
      return intervalCalls.length;
    },
    clearInterval() {},
    JSON,
    Number,
    String,
    Array,
    Object,
    Date: FakeDate,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  const file = path.join(__dirname, '..', 'zixuan.js');
  const source = fs.readFileSync(file, 'utf8') + '\n;globalThis.__ZiXuan = ZiXuan;';
  vm.runInContext(source, sandbox, { filename: 'zixuan.js' });

  return {
    ZiXuan: sandbox.__ZiXuan,
    ajaxCalls,
    timers,
    intervalCalls,
    replacements: () => replacements,
    dropdownData: () => dropdownData,
    watchStatus: () => ({ ...watchStatus }),
    setIdentity(market, code) { currentMarket = market; currentCode = code; },
    fireLatestTimer() {
      const timer = [...timers].reverse().find((item) => !item.cleared && !item.fired);
      assert.ok(timer, 'expected an active timeout');
      timer.fired = true;
      nowMs += timer.delay;
      timer.callback();
      return timer;
    },
  };
}

function failRequest(call) {
  assert.equal(typeof call.error, 'function', 'AJAX errors must be handled');
  assert.equal(typeof call.complete, 'function', 'next poll must wait for completion');
  call.error({}, 'error', new Error('temporary failure'));
  call.complete({}, 'error');
}

function completeSuccess(call, payload) {
  call.success(payload, 'success', {});
  assert.equal(typeof call.complete, 'function', 'next poll must wait for completion');
  call.complete({}, 'success');
}

test('checked watchlist groups preserve their serialized checkbox state', () => {
  const h = loadZiXuan();
  h.ZiXuan.render_zixuan_opts();

  h.ajaxCalls[0].success([
    { zx_name: 'Core', exists: 1, code: 'sh.000001' },
  ]);

  const template = h.dropdownData()[0].templet;
  assert.match(template, /<input type="checkbox" checked="">/);
});
test('late group-membership responses cannot overwrite a newer symbol and path segments are encoded', () => {
  const h = loadZiXuan();
  h.setIdentity('a', 'sh/000001');
  h.ZiXuan.render_zixuan_opts();
  h.setIdentity('hk', '00700/特殊');
  h.ZiXuan.render_zixuan_opts();

  assert.equal(h.ajaxCalls[0].url, '/get_stock_zixuan/a/sh__000001');
  assert.equal(h.ajaxCalls[1].url, '/get_stock_zixuan/hk/00700__%E7%89%B9%E6%AE%8A');
  h.ajaxCalls[1].success([{ zx_name: 'Current', exists: 1, code: '00700/特殊' }]);
  h.ajaxCalls[0].success([{ zx_name: 'Stale', exists: 1, code: 'sh/000001' }]);

  assert.equal(h.dropdownData()[0].title, 'Current');
});
test('render loads the selected group once, then starts bounded rate polling', () => {
  const h = loadZiXuan();

  h.ZiXuan.render_zixuan_stocks();

  assert.equal(h.ajaxCalls.length, 1);
  assert.equal(h.ajaxCalls[0].url, '/get_zixuan_stocks/a/%E6%88%91%E7%9A%84%E5%85%B3%E6%B3%A8');
  assert.equal(h.ajaxCalls[0].timeout, 10000);
  assert.equal(h.intervalCalls.length, 0);
  assert.equal(h.timers.length, 0);

  h.ajaxCalls[0].success({ code: 0, data: [] });
  assert.equal(h.ajaxCalls.length, 2);
  assert.equal(h.ajaxCalls[1].url, '/ticks');
  assert.equal(h.ajaxCalls[1].timeout, 8000);
});

test('does not overlap requests while one is in flight', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();

  h.ZiXuan.stocks_update_rate();
  h.ZiXuan.stocks_update_rate();

  assert.equal(h.ajaxCalls.length, 1);
  failRequest(h.ajaxCalls[0]);
  assert.deepEqual(h.timers.map((timer) => timer.delay), [6000]);
});

test('one global group batches quotes by each member market', () => {
  const h = loadZiXuan([
    { market: 'a', code: 'SH.600000' },
    { market: 'hk', code: '00700' },
    { market: 'a', code: 'SZ.000001' },
  ]);

  h.ZiXuan.stocks_update_rate();

  assert.equal(h.ajaxCalls.length, 2);
  assert.deepEqual(
    h.ajaxCalls.map((call) => call.data.market).sort(),
    ['a', 'hk'],
  );
  assert.deepEqual(
    JSON.parse(h.ajaxCalls.find((call) => call.data.market === 'a').data.codes),
    ['SH.600000', 'SZ.000001'],
  );
  completeSuccess(h.ajaxCalls[0], { ok: true, market_state: 'open', ticks: [] });
  assert.equal(h.timers.length, 0, 'wait for every market batch');
  completeSuccess(h.ajaxCalls[1], { ok: true, market_state: 'closed', ticks: [] });
  assert.deepEqual(h.timers.map((timer) => timer.delay), [3000]);
});

test('US quote batches allow the backend fallback window without slowing other markets', () => {
  const h = loadZiXuan([
    { market: 'a', code: 'SH.600000' },
    { market: 'us', code: 'AAPL.US' },
  ]);

  h.ZiXuan.stocks_update_rate();

  assert.equal(
    h.ajaxCalls.find((call) => call.data.market === 'a').timeout,
    8000,
  );
  assert.equal(
    h.ajaxCalls.find((call) => call.data.market === 'us').timeout,
    12000,
  );
});

test('manual quote refresh preserves rendered prices and skips table reconstruction', () => {
  const h = loadZiXuan([{ market: 'a', code: 'SH.600000' }]);
  h.ZiXuan.stocks_update_rate();
  completeSuccess(h.ajaxCalls[0], {
    ok: true,
    market_state: 'open',
    ticks: [{ code: 'SH.600000', price: 10.5, rate: 1.2 }],
  });
  assert.equal(h.replacements(), 1);
  assert.equal(h.timers[0].delay, 3000);

  h.ZiXuan.refresh_rates();

  assert.equal(h.timers[0].cleared, true);
  assert.equal(h.ajaxCalls.length, 2, 'manual refresh starts quotes immediately');
  assert.equal(h.replacements(), 1, 'the last rendered quote remains visible');
  assert.deepEqual(h.watchStatus(), { text: '正在刷新行情…', state: 'loading' });
});

test('manual refresh during an active request does not create a duplicate batch', () => {
  const h = loadZiXuan([{ market: 'us', code: 'TSLA.US' }]);
  h.ZiXuan.stocks_update_rate();

  assert.equal(h.ZiXuan.refresh_rates(), true);
  assert.equal(h.ajaxCalls.length, 1);
  completeSuccess(h.ajaxCalls[0], {
    ok: true,
    market_state: 'closed',
    ticks: [{ code: 'TSLA.US', price: 320.5, rate: -0.4 }],
  });
  assert.equal(h.replacements(), 1);
});

test('one unavailable market backs off independently while healthy markets keep refreshing', () => {
  const nodes = [
    { market: 'a', code: 'SH.600000' },
    { market: 'currency_spot', code: 'BTC/USDT' },
  ];
  const h = loadZiXuan(nodes);

  h.ZiXuan.stocks_update_rate();
  const aCall = h.ajaxCalls.find((call) => call.data.market === 'a');
  const cryptoCall = h.ajaxCalls.find((call) => call.data.market === 'currency_spot');
  completeSuccess(aCall, { ok: true, market_state: 'open', ticks: [] });
  failRequest(cryptoCall);

  assert.deepEqual(h.watchStatus(), {
    text: '数字货币现货暂不可用；其余 1 个市场继续更新',
    state: 'warning',
  });
  assert.equal(nodes[1].quoteState, 'unavailable');
  assert.equal(h.timers[0].delay, 3000);

  h.fireLatestTimer();
  assert.equal(h.ajaxCalls.length, 3, 'crypto remains in backoff after three seconds');
  assert.equal(h.ajaxCalls[2].data.market, 'a');
  completeSuccess(h.ajaxCalls[2], { ok: true, market_state: 'open', ticks: [] });

  h.fireLatestTimer();
  const newCalls = h.ajaxCalls.slice(3);
  assert.deepEqual(
    newCalls.map((call) => call.data.market).sort(),
    ['a', 'currency_spot'],
    'crypto retries at six seconds without pausing A-share quotes',
  );
});

test('schedules the normal poll only after a valid open response completes', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();
  const call = h.ajaxCalls[0];

  call.success({
    ok: true,
    market_state: 'open',
    ticks: [{ code: 'sh.000001', price: 12.5, rate: 1.25 }],
  });

  assert.equal(h.replacements(), 1);
  assert.equal(h.timers.length, 0, 'success alone must not schedule before completion');
  call.complete({}, 'success');
  assert.deepEqual(h.timers.map((timer) => timer.delay), [3000]);
});

test('uses capped error backoff delays and keeps a single recursive poll', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();

  const expectedDelays = [6000, 12000, 24000, 30000, 30000];
  for (let index = 0; index < expectedDelays.length; index += 1) {
    failRequest(h.ajaxCalls[index]);
    assert.equal(h.timers[index].delay, expectedDelays[index]);
    h.fireLatestTimer();
    assert.equal(h.ajaxCalls.length, index + 2);
  }

  assert.equal(h.intervalCalls.length, 0);
});

test('open success resets the error backoff to six seconds', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();

  failRequest(h.ajaxCalls[0]);
  h.fireLatestTimer();
  failRequest(h.ajaxCalls[1]);
  h.fireLatestTimer();
  completeSuccess(h.ajaxCalls[2], { ok: true, market_state: 'open', ticks: [] });
  assert.equal(h.timers[2].delay, 3000);

  h.fireLatestTimer();
  failRequest(h.ajaxCalls[3]);
  assert.equal(h.timers[3].delay, 6000);
});

test('closed market schedules a slow recheck so polling can recover next session', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();

  completeSuccess(h.ajaxCalls[0], {
    ok: true,
    market_state: 'closed',
    ticks: [{ code: 'sh.000001', price: 12.5, rate: 1.25 }],
  });

  assert.deepEqual(h.timers.map((timer) => timer.delay), [300000]);
  assert.equal(h.replacements(), 1);

  h.fireLatestTimer();
  assert.equal(h.ajaxCalls.length, 2);
});

test('collapsing pauses in-flight polling and expanding resumes immediately', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();
  const call = h.ajaxCalls[0];

  h.ZiXuan.set_rate_polling_active(false);
  completeSuccess(call, { ok: true, market_state: 'open', ticks: [] });

  assert.equal(h.timers.length, 0);
  h.ZiXuan.stocks_update_rate();
  assert.equal(h.ajaxCalls.length, 1, 'manual callbacks must stay paused while collapsed');

  h.ZiXuan.set_rate_polling_active(true);
  assert.equal(h.ajaxCalls.length, 2, 'expanding should refresh immediately');
});

test('index collapse handler owns the watchlist polling lifecycle', () => {
  const template = fs.readFileSync(
    path.join(__dirname, '..', '..', '..', 'templates', 'index.html'),
    'utf8',
  );

  assert.match(
    template,
    /if \(ca_title === "自选组"\) \{\s*ZiXuan\.set_rate_polling_active\(is_open\);\s*\}/,
  );
});

test('unknown market state still renders valid ticks and keeps normal polling', () => {
  const h = loadZiXuan();
  h.ZiXuan.stocks_update_rate();

  completeSuccess(h.ajaxCalls[0], {
    ok: true,
    market_state: 'unknown',
    ticks: [{ code: 'sh.000001', price: 20, rate: 9 }],
  });

  assert.equal(h.replacements(), 1);
  assert.deepEqual(h.timers.map((timer) => timer.delay), [3000]);
});

test('failed and malformed responses preserve the DOM and retry', () => {
  const cases = [
    { ok: false, market_state: 'closed', now_trading: false, ticks: [{ code: 'sh.000001', price: 20, rate: 9 }] },
    { ok: true, market_state: 'open' },
  ];

  for (const payload of cases) {
    const h = loadZiXuan();
    h.ZiXuan.stocks_update_rate();
    completeSuccess(h.ajaxCalls[0], payload);
    assert.equal(h.replacements(), 0);
    assert.deepEqual(h.timers.map((timer) => timer.delay), [6000]);
  }
});
