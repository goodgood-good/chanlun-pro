'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Analysis = require('../chart_analysis.js');
const templatePath = path.join(__dirname, '..', '..', '..', 'templates', 'index.html');
const staticJsPath = path.join(__dirname, '..');
const analysisCssPath = path.join(__dirname, '..', '..', 'css', 'chart_analysis.css');

function point(time, price) {
  return { time, price };
}

function line(startTime, startPrice, endTime, endPrice, linestyle = '1') {
  return {
    linestyle,
    points: [point(startTime, startPrice), point(endTime, endPrice)],
  };
}

function signal(time, price, text, level) {
  return { points: point(time, price), text, level };
}

test('summarizeChartData explains current structure without turning it into a trade command', () => {
  const data = {
    bars: [
      { time: 1700000000000, close: 10.1, isBarClosed: true },
      { time: 1700000300000, close: 10.4, isBarClosed: true },
      { time: 1700000600000, close: 10.8, isBarClosed: false },
    ],
    bis: [line(1700000000, 9.8, 1700000600, 10.8, '1')],
    xds: [line(1699997000, 12.2, 1700000000, 9.8, '1')],
    bi_zss: [{
      linestyle: '0',
      points: [point(1699998000, 10.6), point(1699999500, 10.0)],
    }],
    xd_zss: [{
      linestyle: '1',
      points: [point(1699996000, 11.3), point(1699997000, 9.6)],
    }],
    bi_mmds: [signal(1700000300, 10.4, '3buy', 'bi')],
    xd_mmds: [signal(1699990000, 11.8, '1sell', 'xd')],
    bi_bcs: [signal(1699999500, 10.0, 'PZ', 'bi')],
    xd_bcs: [],
    mmds: [],
    bcs: [],
  };

  const summary = Analysis.summarizeChartData(data, { resolution: '5' });

  assert.equal(summary.price, '10.80');
  assert.equal(summary.barState, '收盘待确认');
  assert.equal(summary.bi.text, '向上 · 形成中');
  assert.equal(summary.bi.meta, '9.80 → 10.80 · 未闭合');
  assert.equal(summary.xd.text, '向下 · 形成中');
  assert.equal(summary.xd.meta, '12.20 → 9.80 · 未闭合');
  assert.equal(summary.biZone.text, '10.00–10.60');
  assert.equal(summary.biZone.position, '上方');
  assert.equal(summary.biZone.meta, '已完成 · 高于上沿 0.20（1.89%）');
  assert.equal(summary.xdZone.text, '9.60–11.30');
  assert.equal(summary.xdZone.position, '中枢内');
  assert.equal(summary.xdZone.meta, '形成中 · 位于区间内 · 距上沿 0.50');
  assert.match(summary.plan.now, /笔中枢上方/);
  assert.match(summary.plan.now, /线段中枢内/);
  assert.equal(summary.mmd.label, '三类买点');
  assert.equal(summary.mmd.levelLabel, '笔');
  assert.equal(summary.mmd.recency, '距当前 1 根 K 线');
  assert.match(summary.mmd.meta, /信号价 10\.40/);
  assert.match(summary.mmd.meta, /现价较信号 \+3\.85%/);
  assert.equal(summary.mmd.tone, 'buy');
  assert.equal(summary.bc.label, '盘整背驰');
  assert.equal(summary.bc.recency, '当前加载区间之前');
  assert.equal(summary.verdict, '线段向下未完成，当前笔向上反向运行');
  assert.equal(
    summary.verdictDetail,
    '现价位于笔中枢上方、线段中枢内；形成中的笔或线段边界仍可能变化。',
  );
  assert.match(summary.plan.wait, /当前向上笔闭合/);
  assert.match(summary.plan.wait, /线段中枢 9\.60–11\.30/);
  assert.match(summary.plan.boundary, /9\.80/);
  assert.match(summary.plan.boundary, /触发结构重算/);
  assert.match(summary.plan.boundary, /不是自动开仓、平仓或止损价/);
  assert.doesNotMatch(summary.plan.now, /买入|卖出|开仓|清仓/);
});

test('line-segment zone reads branch-core recursive level zero before legacy xd_zss', () => {
  const data = {
    bars: [{ time: 1700000600000, close: 10.8, isBarClosed: true }],
    xd_zss: [{
      linestyle: '0',
      points: [point(1700000300, 8.0), point(1700000400, 7.0)],
    }],
    recursive_levels: [
      {
        level: 0,
        zss: [{
          linestyle: '1',
          points: [point(1700000000, 11.3), point(1700000500, 9.6)],
        }],
      },
      {
        level: 1,
        zss: [{
          linestyle: '0',
          points: [point(1700000550, 20), point(1700000580, 5)],
        }],
      },
    ],
  };

  const summary = Analysis.summarizeChartData(data, { resolution: '5' });

  assert.equal(summary.xdZone.text, '9.60–11.30');
  assert.equal(summary.xdZone.position, '中枢内');
  assert.equal(summary.xdZone.status, '形成中');
});
test('summarizeChartData handles an empty or still-loading chart honestly', () => {
  const summary = Analysis.summarizeChartData({ bars: [] }, { resolution: '1D' });

  assert.equal(summary.price, '--');
  assert.equal(summary.verdict, '等待形成可解释的笔或线段');
  assert.equal(summary.verdictDetail, '当前数据不足，暂不判断方向或中枢位置。');
  assert.equal(summary.bi.text, '尚未形成');
  assert.equal(summary.bi.meta, '等待至少两个有效端点');
  assert.equal(summary.xd.text, '尚未形成');
  assert.equal(summary.xd.meta, '等待至少两个有效端点');
  assert.equal(summary.biZone.text, '尚无笔中枢');
  assert.equal(summary.xdZone.text, '尚无线段中枢');
  assert.equal(summary.mmd.empty, true);
  assert.equal(summary.bc.empty, true);
  assert.match(summary.plan.wait, /等待至少一笔形成并闭合/);
});

test('latest signal wins across split and compatibility arrays with readable labels', () => {
  const data = {
    bars: [
      { time: 1700000000000, close: 8.2, isBarClosed: true },
      { time: 1700000300000, close: 8.4, isBarClosed: true },
    ],
    bis: [],
    xds: [],
    bi_mmds: [signal(1699990000, 8.0, '1buy', 'bi')],
    xd_mmds: [signal(1700000300, 8.4, 'l2sell', 'xd')],
    mmds: [signal(1699995000, 8.3, '2buy', 'bi')],
    bi_bcs: [signal(1699990000, 8.0, 'QS', 'bi')],
    xd_bcs: [signal(1700000000, 8.2, 'PZ', 'xd')],
    bcs: [],
  };

  const summary = Analysis.summarizeChartData(data, { resolution: '5' });

  assert.equal(summary.mmd.label, '类二类卖点');
  assert.equal(summary.mmd.levelLabel, '线段');
  assert.equal(summary.mmd.tone, 'sell');
  assert.equal(summary.mmd.recency, '最近一根 K 线');
  assert.match(summary.mmd.meta, /现价较信号 \+0\.00%/);
  assert.equal(summary.bc.label, '盘整背驰');
  assert.equal(summary.bc.levelLabel, '线段');
  assert.equal(summary.bc.recency, '距当前 1 根 K 线');
});

test('formatResolution covers intraday and higher timeframes', () => {
  assert.equal(Analysis.formatResolution('5'), '5 分钟');
  assert.equal(Analysis.formatResolution('1D'), '日线');
  assert.equal(Analysis.formatResolution('1W'), '周线');
  assert.equal(Analysis.formatResolution('1M'), '月线');
  assert.equal(Analysis.formatResolution('10S'), '10 秒');
});

test('zone summary exposes auditable tower level bounds segments and point metadata', () => {
  const data = {
    bars: [{ time: 1700000600000, close: 10.8, isBarClosed: true }],
    bi_zss: [{
      linestyle: '0',
      tower: 'xd', // stale payload metadata must not relabel the bi channel
      recursive_level: null,
      zd: 10,
      zg: 10.6,
      points: [point(1700000000, 10.6), point(1700000500, 10)],
      entering_segment: {
        direction: 'down', start_price: 11.2, end_price: 10,
      },
      leaving_segment: {
        direction: 'up', start_price: 10.1, end_price: 10.8,
      },
      associated_points: ['3buy'],
    }],
    xd_zss: [{
      linestyle: '1',
      tower: 'bi', // stale payload metadata must not relabel the xd channel
      recursive_level: 0,
      zd: 9.6,
      zg: 11.3,
      points: [point(1699996000, 11.3), point(1699997000, 9.6)],
      entering_segment: null,
      leaving_segment: null,
      associated_points: [],
    }],
  };

  const summary = Analysis.summarizeChartData(data, { resolution: '5' });

  assert.equal(summary.biZone.tower, '笔');
  assert.equal(summary.biZone.recursiveLevel, '观察层');
  assert.equal(summary.biZone.zd, '10.00');
  assert.equal(summary.biZone.zg, '10.60');
  assert.equal(summary.biZone.completion, '已完成');
  assert.equal(summary.biZone.enteringSegment, '向下 · 11.20 → 10.00');
  assert.equal(summary.biZone.leavingSegment, '向上 · 10.10 → 10.80');
  assert.equal(summary.biZone.associatedPoint, '三类买点');
  assert.equal(summary.xdZone.tower, '线段');
  assert.equal(summary.xdZone.recursiveLevel, 'L0');
  assert.equal(summary.xdZone.completion, '形成中');
  assert.equal(summary.xdZone.associatedPoint, '暂无关联买卖点');
});

test('analysis exposes independent dual-tower chart layer controls without removed copy', () => {
  const template = fs.readFileSync(templatePath, 'utf8');

  for (const [key, label] of [
    ['bi', '笔'],
    ['xd', '线段'],
    ['bi_zs', '笔中枢'],
    ['xd_zs', '线段中枢'],
    ['bi_mmd', '笔买卖点'],
    ['xd_mmd', '线段买卖点'],
    ['recursive', '递归级别'],
  ]) {
    assert.match(template, new RegExp(`data-chart-layer=["']${key}["'][^>]*>${label}<`));
  }
  for (const id of [
    'ca-bi-zone-tower', 'ca-bi-zone-level', 'ca-bi-zone-bounds',
    'ca-bi-zone-completion', 'ca-bi-zone-entry', 'ca-bi-zone-exit',
    'ca-bi-zone-point', 'ca-xd-zone-tower', 'ca-xd-zone-level',
    'ca-xd-zone-bounds', 'ca-xd-zone-completion', 'ca-xd-zone-entry',
    'ca-xd-zone-exit', 'ca-xd-zone-point',
  ]) {
    assert.match(template, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }
  assert.match(template, /笔中枢/);
  assert.match(template, /线段中枢/);
  assert.doesNotMatch(template, /AI 深度解读/);
  assert.doesNotMatch(template, /原文课次与结构标签/);
});

test('layer controls mutate drawing visibility only and redraw the active chart', () => {
  let redraws = 0;
  const manager = {
    cl_show_config: {
      bi: true, xd: true, zs_all: true, zs_bi: true, zs_xd: true,
      mmd: true, mmd_bi: true, mmd_xd: true,
      zs_L1: true, zs_L2: true, xd_L1: true, xd_L2: true,
      mmd_L1: true, mmd_L2: true, bc_L1: true, bc_L2: true,
    },
    _recMaxLevel: 2,
    debouncedDrawChanlun() { redraws += 1; },
  };

  assert.equal(Analysis.setLayerVisibility(manager, 'bi_zs', false), true);
  assert.equal(manager.cl_show_config.zs_bi, false);
  assert.equal(Analysis.setLayerVisibility(manager, 'recursive', false), true);
  for (const prefix of ['zs', 'xd', 'mmd', 'bc']) {
    assert.equal(manager.cl_show_config[`${prefix}_L1`], false);
    assert.equal(manager.cl_show_config[`${prefix}_L2`], false);
  }
  assert.equal(redraws, 2);
  assert.equal(Analysis.setLayerVisibility(manager, 'unknown', true), false);
  assert.equal(redraws, 2);
});

test('index page exposes a real current-chart analysis region and its assets', () => {
  const template = fs.readFileSync(templatePath, 'utf8');

  for (const id of [
    'ca-overview',
    'ca-overview-toggle',
    'ca-overview-body',
    'ca-current-symbol',
    'ca-current-interval',
    'ca-current-price',
    'ca-structure-verdict',
    'ca-bi-state',
    'ca-xd-state',
    'ca-bi-zone-state',
    'ca-bi-zone-meta',
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
    '结构判读',
    '走势状态',
    '双中枢定位',
    '最近结构证据',
    '验证清单',
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
