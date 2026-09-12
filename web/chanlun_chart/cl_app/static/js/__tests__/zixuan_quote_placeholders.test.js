'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('missing quotes stay unknown while a real zero change remains 0%', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'zixuan.js'), 'utf8');
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('function compactDecimal('), source.indexOf('function rateNode(')), sandbox);
  for (const value of [null, undefined, '', NaN]) {
    assert.equal(sandbox.formatQuotePrice(value, 'us'), '-');
    assert.equal(sandbox.formatQuoteRate(value), '- %');
  }
  assert.equal(sandbox.formatQuoteRate(0), '0%');
  assert.equal(sandbox.formatQuoteRate(-1.23), '-1.23%');
  assert.equal(sandbox.formatQuotePrice(319.12, 'us'), '319.12');
});
