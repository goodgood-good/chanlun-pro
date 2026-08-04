'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', '..', '..', relativePath), 'utf8');
}

test('AI analysis has a deadline and restores its button from complete on every outcome', () => {
  const source = read('static/js/ai.js');
  assert.match(source, /AppRequest\.ajax\(/);
  assert.match(source, /timeout:\s*120000/);
  assert.match(source, /error:\s*function[^]*分析失败/);
  assert.match(source, /complete:\s*function[^]*removeClass\("layui-btn-disabled"\)/);
  assert.equal((source.match(/removeClass\("layui-btn-disabled"\)/g) || []).length, 1);
});

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

test('drawing persistence stores only versioned user sources and validates acknowledgement', () => {
  const source = read('static/js/charts.js');
  assert.match(source, /USER_DRAWING_STATE_SCHEMA\s*=\s*["']chanlun-user-drawings\/v2["']/);
  assert.match(source, /serializeUserDrawingsState\(state\)/);
  assert.match(source, /groups:\s*\{\}/);
  assert.match(source, /enqueueLatestDrawingSave/);
  assert.match(source, /payload\.status\s*!==\s*'ok'/);
});

test('core mutation views use the CSRF-refreshing request layer', () => {
  const alertTemplate = read('templates/alert.html');
  const watchlistTemplate = read('templates/zixuan.html');
  const alertSource = read('static/js/alert.js');
  const chartSource = read('static/js/charts.js');

  assert.match(alertTemplate, /AppRequest\.ajax\(\{[^]*url:\s*["']\/alert_save["']/);
  assert.match(watchlistTemplate, /AppRequest\.ajax\(\{[^]*\/opt_zixuan_group\//);
  assert.match(alertSource, /AppRequest\.ajax\(\{[^]*\/alert_del\//);
  assert.match(chartSource, /AppRequest\.ajax\(\{[^]*\/tv\/del_marks/);
});
