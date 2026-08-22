'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', '..', '..', relativePath), 'utf8');
}

test('settings save has timeout, failure feedback, and completion cleanup', () => {
  const source = read('templates/setting.html');
  assert.match(source, /AppRequest\.ajax\(/);
  assert.match(source, /timeout:\s*15000/);
  assert.match(source, /error:\s*function[^]*配置保存失败/);
  assert.match(source, /complete:\s*function[^]*prop\(["']disabled["'],\s*false\)/);
});

test('symbol list load is bounded and uses the unified request layer', () => {
  const source = read('templates/index.html');
  assert.match(source, /AppRequest\.ajax\(\{[^]*url:\s*['"]\/symbols\/list['"][^]*timeout:\s*10000/);
});

test('drawing persistence stores only current user sources and validates acknowledgement', () => {
  const source = read('static/js/charts.js');
  assert.match(source, /USER_DRAWING_STATE_SCHEMA\s*=\s*["']chanlun-user-drawings["']/);
  assert.match(source, /serializeUserDrawingsState\(state\)/);
  assert.match(source, /groups:\s*\{\}/);
  assert.match(source, /enqueueLatestDrawingSave/);
  assert.match(source, /payload\.status\s*!==\s*'ok'/);
  assert.doesNotMatch(source, /saveChartToServer/);
});

test('watchlist mutations use the CSRF-refreshing request layer', () => {
  const watchlistTemplate = read('templates/zixuan.html');

  assert.match(watchlistTemplate, /AppRequest\.ajax\(\{[^]*\/opt_zixuan_group\//);
});
