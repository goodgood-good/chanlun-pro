'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const templatesDir = path.join(__dirname, '..', '..', '..', 'templates');
const cssPath = path.join(__dirname, '..', '..', 'css', 'ui-system.css');

const fullPages = [
  'index.html',
  'login.html',
  'setting.html',
  'symbols.html',
  'jobs.html',
  'options.html',
  'alert.html',
  'xuangu_list.html',
  'zixuan.html',
  'early_screening.html',
  'research_audit.html',
];

function template(name) {
  return fs.readFileSync(path.join(templatesDir, name), 'utf8');
}

test('every rendered page opts into one shared UI system and semantic page landmarks', () => {
  for (const name of fullPages) {
    const source = template(name);
    assert.match(source, /css\/ui-system\.css/, `${name} must load the shared UI system`);
    assert.match(source, /<body[^>]*class=["'][^"']*cp-ui\b/i, `${name} must opt into cp-ui`);
    assert.match(source, /<main\b/i, `${name} must expose a main landmark`);
    assert.match(source, /<h1\b/i, `${name} must expose one page-level heading`);
    assert.match(source, /<meta\s+name=["']viewport["']/i, `${name} must define a responsive viewport`);
  }
});

test('shared UI stylesheet defines tokens, accessible focus, responsive layout and motion fallback', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  for (const token of [
    '--cp-bg',
    '--cp-surface',
    '--cp-text',
    '--cp-muted',
    '--cp-border',
    '--cp-accent',
    '--cp-success',
    '--cp-danger',
    '--cp-radius-lg',
  ]) {
    assert.match(css, new RegExp(token), `missing design token ${token}`);
  }

  assert.match(css, /\.cp-ui\s+:focus-visible/);
  assert.match(css, /@media\s*\(max-width:\s*720px\)/);
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
  assert.match(css, /\.cp-page-header/);
  assert.match(css, /\.cp-section-card/);
  assert.match(css, /\.cp-data-shell/);
});

test('legacy form pages are reorganized into explicit task-oriented sections', () => {
  const expected = {
    'setting.html': ['系统设置', '消息通知', '网络代理', '配置指南'],
    'symbols.html': ['标的中心', '筛选标的', '标的列表'],
    'jobs.html': ['任务运行状态', '调度任务'],
    'options.html': ['缠论参数配置', '配置范围', '结构算法', '图表呈现'],
    'alert.html': ['预警配置', '基础任务', '笔级别条件', '线段级别条件', '通知与运行'],
    'xuangu_list.html': ['传统选股任务', '任务与方向', '数据范围', '结果写入'],
  };

  for (const [name, labels] of Object.entries(expected)) {
    const source = template(name);
    assert.match(source, /cp-page-header/, `${name} needs a unified page header`);
    assert.match(source, /cp-section-card/, `${name} needs card-based task sections`);
    for (const label of labels) {
      assert.match(source, new RegExp(label), `${name} missing section label: ${label}`);
    }
  }
});

test('login, research and workbench surfaces share product identity without losing their roles', () => {
  const login = template('login.html');
  const index = template('index.html');
  const screening = template('early_screening.html');
  const audit = template('research_audit.html');

  assert.match(login, /CHANLUN PRO/);
  assert.match(login, /行情结构研究工作台/);
  assert.match(login, /autocomplete=["']current-password["']/);
  assert.match(index, /行情与结构工作台/);
  assert.match(screening, /cp-product-nav/);
  assert.match(audit, /cp-product-nav/);
  assert.match(audit, /历史研究 \/ 审计成果/);
});

test('long configuration choices wrap inside their grid card without horizontal overflow', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /\.cp-config-form\s+\.layui-form-checkbox\[lay-skin=["']primary["']\][^{]*\{[^}]*max-width:\s*100%/s);
  assert.match(css, /\.cp-config-form\s+\.layui-form-checkbox\[lay-skin=["']primary["']\]\s*>\s*div[^{]*\{[^}]*white-space:\s*normal/s);
});

test('mobile configuration actions use a compact two-column control dock', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /@media\s*\(max-width:\s*720px\)[\s\S]*\.cp-config-form\s*>\s*\.cp-actions--sticky[^{]*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
});
