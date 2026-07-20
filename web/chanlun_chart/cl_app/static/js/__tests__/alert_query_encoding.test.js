'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'alert.js'), 'utf8');

test('alert record client surface is removed', () => {
  assert.doesNotMatch(source, /get_alert_records/);
  assert.doesNotMatch(source, /\/alert_records\//);
  assert.doesNotMatch(source, /table_alert_reocrds/);
});
