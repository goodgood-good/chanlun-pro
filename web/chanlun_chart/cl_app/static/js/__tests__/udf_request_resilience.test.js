'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const udfRoot = path.join(__dirname, '..', '..', 'datafeeds', 'udf', 'src');

function read(name) {
  return fs.readFileSync(path.join(udfRoot, name), 'utf8');
}

test('Requester aborts a stalled fetch at a bounded deadline and clears its timer', () => {
  const source = read('requester.ts');
  assert.match(source, /AbortController/);
  assert.match(source, /setTimeout\([^]*?\.abort\(\)/);
  assert.match(source, /clearTimeout/);
  assert.match(source, /response\.ok/);
});

test('DataPulseProvider isolates pending work per subscriber and bounds every refresh', () => {
  const source = read('data-pulse-provider.ts');
  assert.match(source, /Set<string>/);
  assert.match(source, /_requestsPending\.has\(listenerGuid\)/);
  assert.match(source, /Promise\.race/);
  assert.match(source, /clearTimeout/);
});