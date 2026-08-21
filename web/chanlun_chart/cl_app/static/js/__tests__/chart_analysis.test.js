'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Analysis = require('../chart_analysis.js');
const templatePath = path.join(__dirname, '..', '..', '..', 'templates', 'index.html');
const staticJsPath = path.join(__dirname, '..');
const analysisCssPath = path.join(__dirname, '..', '..', 'css', 'chart_analysis.css');

test('summarizeChartData handles an empty or still-loading chart honestly', () => {
  const summary = Analysis.summarizeChartData({ bars: [] }, { resolution: '1D' });

  assert.equal(summary.price, '--');
  assert.equal(summary.verdict, '正在同步严格缠论结构');
  assert.equal(summary.verdictDetail, '严格结构传输状态缺失');
  assert.equal(summary.bi.text, '尚未形成');
  assert.equal(summary.bi.meta, '等待至少两个有效端点');
  assert.equal(summary.xd.text, '尚未形成');
  assert.equal(summary.xd.meta, '等待至少两个有效端点');
  assert.equal(summary.biZone.text, '尚无笔中枢观察');
  assert.equal(summary.xdZone.text, '尚无严格中枢');
  assert.equal(summary.mmd.empty, true);
  assert.equal(summary.bc.empty, true);
  assert.match(summary.plan.wait, /等待同一标的、周期和末根闭合时间/);
});

test('formatResolution covers intraday and higher timeframes', () => {
  assert.equal(Analysis.formatResolution('5'), '5 分钟');
  assert.equal(Analysis.formatResolution('120'), '2小时');
  assert.equal(Analysis.formatResolution('360'), '6小时');
  assert.equal(Analysis.formatResolution('720'), '12小时');
  assert.equal(Analysis.formatResolution('1D'), '日线');
  assert.equal(Analysis.formatResolution('3D'), '3日线');
  assert.equal(Analysis.formatResolution('1W'), '周线');
  assert.equal(Analysis.formatResolution('1M'), '月线');
  assert.equal(Analysis.formatResolution('10S'), '10 秒');
});

test('analysis exposes only base line controls and never duplicates strict display controls', () => {
  const template = fs.readFileSync(templatePath, 'utf8');

  for (const [key, label] of [
    ['bi', '笔'],
    ['xd', '线段'],
  ]) {
    assert.match(template, new RegExp(`data-chart-layer=["']${key}["'][^>]*>${label}<`));
  }
  for (const key of ['bi_zs', 'xd_zs', 'bi_mmd', 'xd_mmd', 'recursive']) {
    assert.doesNotMatch(template, new RegExp(`data-chart-layer=["']${key}["']`));
  }
  for (const id of [
    'ca-bi-zone-tower', 'ca-bi-zone-level', 'ca-bi-zone-bounds',
    'ca-bi-zone-completion', 'ca-bi-zone-entry', 'ca-bi-zone-exit',
    'ca-bi-zone-point', 'ca-bi-zone-return', 'ca-bi-zone-evidence',
    'ca-bi-zone-requirement',
    'ca-xd-zone-tower', 'ca-xd-zone-level',
    'ca-xd-zone-bounds', 'ca-xd-zone-completion', 'ca-xd-zone-entry',
    'ca-xd-zone-core', 'ca-xd-zone-exit', 'ca-xd-zone-return',
    'ca-xd-zone-point',
    'ca-xd-zone-evidence', 'ca-xd-zone-requirement',
  ]) {
    assert.match(template, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }
  assert.match(template, /笔中枢/);
  assert.match(template, /线段中枢/);
  assert.doesNotMatch(template, /AI 深度解读/);
  assert.doesNotMatch(template, /原文课次与结构标签/);
});

test('analysis layer controls mutate only base line visibility', () => {
  let redraws = 0;
  const manager = {
    cl_show_config: {
      bi: true,
      xd: true,
      center_all: true,
      center_L1: true,
      divergence_all: true,
      divergence_trend_L1: true,
    },
    debouncedDrawChanlun() { redraws += 1; },
  };

  assert.equal(Analysis.setLayerVisibility(manager, 'bi', false), true);
  assert.equal(manager.cl_show_config.bi, false);
  assert.equal(Analysis.setLayerVisibility(manager, 'recursive', false), false);
  assert.equal(manager.cl_show_config.center_L1, true);
  assert.equal(manager.cl_show_config.divergence_trend_L1, true);
  assert.equal(redraws, 1);
});

test('index page exposes a real current-chart analysis region and its assets', () => {
  const template = fs.readFileSync(templatePath, 'utf8');

  for (const id of [
    'ca-overview',
    'ca-overview-title',
    'ca-overview-summary',
    'ca-overview-toggle',
    'ca-overview-toggle-label',
    'ca-overview-body',
    'ca-current-symbol',
    'ca-current-interval',
    'ca-current-price',
    'ca-structure-verdict',
    'ca-formal-direction-row',
    'ca-formal-direction-state',
    'ca-formal-direction-meta',
    'ca-bi-state',
    'ca-xd-state',
    'ca-xd-zone-state',
    'ca-xd-zone-meta',
    'ca-mmd-state',
    'ca-bc-state',
    'ca-plan-now',
    'ca-plan-wait',
    'ca-plan-boundary',
    'ca-refresh-analysis',
    'ca-screening-link',
    'ca-audit-link',
    'ca-tools-title',
  ]) {
    assert.match(template, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }

  assert.match(template, /id=["']ca-overview-toggle["'][^>]*aria-controls=["']ca-overview-body["']/);
  assert.match(template, /id=["']ca-overview["'][^>]*class=["'][^"']*\bis-collapsed\b/);
  assert.match(template, /id=["']ca-overview-toggle["'][^>]*aria-expanded=["']false["']/);
  assert.match(template, /id=["']ca-overview-body["'][^>]*\bhidden\b/);
  assert.match(template, /<details\s+class=["']ca-evidence-details["']/);
  assert.match(template, /css\/chart_analysis\.css/);
  assert.match(template, /js\/chart_analysis\.js/);
  assert.match(template, /<meta\s+charset=["']utf-8["']\s*\/?>/i);
  assert.match(
    template,
    /<meta\s+name=["']viewport["']\s+content=["']width=device-width,\s*initial-scale=1,\s*viewport-fit=cover["']\s*\/?>/i,
  );
  assert.match(template, /不预测下一根 K 线/);
  assert.match(template, /不能直接视为交易指令/);
  for (const label of [
    'STRUCTURE WORKBENCH',
    '缠论结构解盘',
    '分析标的',
    '当前结构解读',
    '本周期结构',
    '结构主线',
    '中枢位置',
    '查看中枢构成证据',
    '最近信号',
    '后续验证条件',
    '盯盘与研究入口',
    '提前选股与审计',
    '自选盯盘',
    '全市场检索',
    '板块联动',
  ]) {
    assert.match(template, new RegExp(label), `missing optimized menu label: ${label}`);
  }
  assert.doesNotMatch(template, /预警记录/);
  assert.doesNotMatch(template, /alert_records_form/);
  for (const removedAiSurface of [
    'AI 深度解读',
    'AI分析助手',
    'table_ai_analysis',
    'js/ai.js',
    'marked.min.js',
    'dompurify-3.4.11.min.js',
    'AI.init_ai_opts',
    'AI.get_ai_analyse_records',
  ]) {
    const escaped = removedAiSurface.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    assert.doesNotMatch(template, new RegExp(escaped));
  }
});

test('current structure interpretation defaults closed and remembers only the redesigned preference', () => {
  const template = fs.readFileSync(templatePath, 'utf8');
  const source = fs.readFileSync(path.join(staticJsPath, 'chart_analysis.js'), 'utf8');
  const waitAt = template.indexOf('id="ca-plan-wait"');
  const boundaryAt = template.indexOf('id="ca-plan-boundary"');
  const factAt = template.indexOf('id="ca-plan-now"');

  assert.match(source, /OVERVIEW_COLLAPSE_STORAGE_KEY\s*=\s*['"]chart_analysis_overview_collapsed['"]/);
  assert.match(source, /stored\s*===\s*null\s*\?\s*true/);
  assert.doesNotMatch(source, /getItem\(['"]chart_analysis_overview_collapsed['"]\)/);
  assert.ok(waitAt > 0 && waitAt < boundaryAt && boundaryAt < factAt);
  assert.match(template, /id="ca-refresh-analysis"[^>]*class="ca-action ca-action--primary"/);
});

test('every chart-sidebar tool uses explicit purpose, action and empty-state copy', () => {
  const template = fs.readFileSync(templatePath, 'utf8');
  const zixuan = fs.readFileSync(path.join(staticJsPath, 'zixuan.js'), 'utf8');
  const bkgn = fs.readFileSync(path.join(staticJsPath, 'bkgn.js'), 'utf8');
  const analysisCss = fs.readFileSync(analysisCssPath, 'utf8');

  for (const copy of [
    '点击行切换主图，右键可排序、标色或移出分组',
    '输入代码、名称或拼音，点击结果立即切换主图',
    '缓存常用周期',
    '先选板块，再从成分股中切换主图',
    '仅 A 股市场提供板块数据',
  ]) {
    assert.match(template, new RegExp(copy), `missing tool guidance: ${copy}`);
  }

  assert.match(zixuan, /关注标的/);
  assert.match(zixuan, /涨跌 \/ 现价/);
  assert.match(zixuan, /当前分组暂无标的/);
  assert.match(zixuan, /输入代码、名称或拼音/);
  assert.match(bkgn, /板块名称/);
  assert.match(bkgn, /共 .* 只成分股/);
  assert.match(bkgn, /没有匹配的板块/);
  assert.match(bkgn, /没有匹配的成分股/);
  assert.match(
    analysisCss,
    /\.ca-signal-grid\s*\{[^}]*grid-template-columns:\s*1fr/s,
    'signal evidence needs a full-width row for readable timestamps and price distance',
  );

  for (const vagueCopy of [
    'CURRENT CHART',
    '当前结论',
    '观察清单',
    '预热全部',
    '商品代码搜索',
    '个股票',
  ]) {
    assert.doesNotMatch(`${template}\n${zixuan}\n${bkgn}`, new RegExp(vagueCopy));
  }
});
