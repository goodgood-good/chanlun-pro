'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadAlert(taskName) {
  let renderOptions = null;
  function $() {
    return { val() { return taskName; } };
  }
  const table = {
    render(options) { renderOptions = options; },
    on() {},
  };
  const sandbox = {
    $,
    layui: {
      table,
      form: {},
      use(_deps, callback) { callback(); },
    },
    Utils: { get_market() { return 'a'; } },
    document: {},
    encodeURIComponent,
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '..', 'alert.js'), 'utf8');
  vm.runInContext(source + '\n;globalThis.__Alert = Alert;', sandbox, { filename: 'alert.js' });
  return { Alert: sandbox.__Alert, renderOptions: () => renderOptions };
}

test('alert record URL encodes the selected task name', () => {
  const h = loadAlert('Alpha&Beta +#');

  h.Alert.get_alert_records();

  assert.equal(
    h.renderOptions().url,
    '/alert_records/a?task_name=Alpha%26Beta%20%2B%23',
  );
});
