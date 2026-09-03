'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const templatesDir = path.join(__dirname, '..', '..', '..', 'templates');
const cssPath = path.join(__dirname, '..', '..', 'css', 'ui-system.css');
const chartAnalysisCssPath = path.join(__dirname, '..', '..', 'css', 'chart_analysis.css');
const zixuanCssPath = path.join(__dirname, '..', '..', 'css', 'zixuan.css');
const zixuanJsPath = path.join(__dirname, '..', 'zixuan.js');
const bkgnJsPath = path.join(__dirname, '..', 'bkgn.js');
const layuiAccessibilityPath = path.join(__dirname, '..', 'layui_accessibility.js');

const fullPages = [
  'index.html',
  'login.html',
  'setting.html',
  'symbols.html',
  'jobs.html',
  'options.html',
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
  assert.match(css, /\.cp-page-stack\s*\{[^}]*align-content:\s*start/s);
});

test('operational pages use explicit task-oriented sections', () => {
  const expected = {
    'setting.html': ['系统设置', '消息通知', '网络代理', '配置指南'],
    'symbols.html': ['标的中心', '筛选标的', '标的列表'],
    'jobs.html': ['任务运行状态', '调度任务'],
    'options.html': ['图表显示配置', '配置范围', '基础结构'],
    'xuangu_list.html': ['统一选股任务', '任务与方向', '数据范围', '结果写入'],
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
  assert.match(login, /autocomplete=["']username["']/);
  assert.match(index, /行情与结构工作台/);
  assert.match(screening, /cp-product-nav/);
  assert.match(audit, /cp-product-nav/);
  assert.match(audit, /历史研究 \/ 审计成果/);
});

test('visible workbench copy uses the user-facing anti-repaint review term', () => {
  for (const name of ['index.html', 'early_screening.html', 'research_audit.html']) {
    const source = template(name);
    assert.doesNotMatch(source, /审计锁/, `${name} exposes obsolete internal lock wording`);
  }
  assert.match(template('index.html'), /末端结构封存状态/);
  assert.match(template('research_audit.html'), /末端结构封存状态/);
});

test('decision-support pages never imply that a real account is connected', () => {
  assert.doesNotMatch(
    template('early_screening.html'),
    /账户|现金|持仓|仓位|组合热度/,
    'early screening exposes account-dependent wording',
  );
  assert.doesNotMatch(
    template('research_audit.html'),
    /账户/,
    'research audit implies account integration instead of historical replay',
  );
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

test('operational empty states and compact chart options remain intentional', () => {
  const jobs = template('jobs.html');
  const options = template('options.html');
  const symbols = template('symbols.html');
  const screener = template('xuangu_list.html');

  assert.match(jobs, /cp-empty-state/);
  assert.match(jobs, /暂时没有已注册的调度任务/);
  assert.match(options, /cp-page--compact/);
  assert.match(options, /cp-actions--sticky/);
  assert.match(symbols, /aria-label', '跳转页码'/);
  assert.match(screener, /\{\{ market_label \}\}/);
});

test('mobile table pagination wraps the direct-page control instead of clipping it', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /\.cp-data-shell\s+\.layui-laypage\s+select\s*\{[^}]*min-height:\s*34px/s);
  assert.match(css, /\.layui-laypage-skip input,[\s\S]*\.layui-laypage-btn\s*\{[^}]*height:\s*34px/s);
  assert.match(css, /@media\s*\(max-width:\s*720px\)[\s\S]*\.cp-data-shell\s+\.layui-table-page\s*\{[^}]*height:\s*auto[^}]*overflow:\s*visible/s);
  assert.match(css, /\.cp-data-shell\s+\.layui-laypage\s+\.layui-laypage-skip\s*\{[^}]*flex:\s*0\s+0\s+100%/s);
});

test('Layui-generated controls retain names, states and keyboard access', () => {
  const dark = template('dark.html');
  const accessibility = fs.readFileSync(layuiAccessibilityPath, 'utf8');
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(dark, /js\/layui_accessibility\.js/);
  assert.match(accessibility, /if \(!window\.layui\) return/);
  assert.match(accessibility, /setAttribute\("role", "combobox"\)/);
  assert.match(accessibility, /setAttribute\("autocomplete", "off"\)/);
  assert.match(accessibility, /setAttribute\(\s*"aria-expanded"/);
  assert.match(accessibility, /setAttribute\("role", "option"\)/);
  assert.match(accessibility, /widget\.tabIndex = source\.disabled \? -1 : 0/);
  assert.match(accessibility, /event\.key !== " " && event\.key !== "Enter"/);
  assert.match(accessibility, /new window\.MutationObserver\(scheduleSync\)/);
  assert.match(css, /\.layui-form-checkbox\[lay-skin="primary"\]\s*\{[^}]*min-height:\s*32px/s);
});

test('shared adapters make generated accordions, search widgets, dialogs and notices operable', () => {
  const accessibility = fs.readFileSync(layuiAccessibilityPath, 'utf8');
  const index = template('index.html');
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(index, /id="code_search"[^>]*aria-label="搜索并切换标的"/);
  assert.match(accessibility, /function syncCollapse\(/);
  assert.match(accessibility, /\.layui-colla-title\[role=\\"button\\"\]/);
  assert.match(accessibility, /function syncXmSelect\(/);
  assert.match(accessibility, /widget\.setAttribute\("tabindex", "0"\)/);
  assert.match(accessibility, /setAttribute\("role", "searchbox"\)/);
  assert.match(accessibility, /function handleSelectKeys\(/);
  assert.match(accessibility, /document\.addEventListener\("keydown", function \(event\) \{\s*handleSelectKeys\(event\);\s*\}, true\)/s);
  assert.match(accessibility, /function handleXmSelectKeys\(/);
  assert.match(accessibility, /function syncDropdown\(/);
  assert.match(accessibility, /setAttribute\("role", "menu"\)/);
  assert.match(accessibility, /setAttribute\("role", "menuitem"\)/);
  assert.match(accessibility, /function handleDropdownKeys\(/);
  assert.match(accessibility, /event\.key === "ArrowRight"/);
  assert.match(accessibility, /event\.key === "Tab"/);
  assert.match(accessibility, /layer\.setAttribute\("role", alertDialog \? "alertdialog" : "dialog"\)/);
  assert.match(accessibility, /layer\.setAttribute\("aria-modal", shade \? "true" : "false"\)/);
  assert.match(accessibility, /node\.inert = true/);
  assert.match(accessibility, /event\.key === "Escape"/);
  assert.match(accessibility, /event\.key !== "Tab"/);
  assert.match(accessibility, /layer\.setAttribute\("role", "status"\)/);
  assert.match(accessibility, /classList\.contains\("layui-layer-loading"\)/);
  assert.match(accessibility, /layer\.setAttribute\("aria-busy", "true"\)/);
  assert.match(accessibility, /frame\.setAttribute\("title"/);
  assert.match(accessibility, /#tv_charts_area iframe/);
  assert.match(accessibility, /当前标的行情与缠论图/);
  assert.match(accessibility, /trigger\.focus\(\)/);
  assert.match(css, /\.layui-menu-item-parent\.cp-menu-open\s*>\s*\.layui-menu-body-panel/);
  assert.match(css, /\.layui-menu-body-title\[role="menuitem"\]:focus-visible/);
  assert.match(css, /dl dd\.cp-option-active/);
  assert.match(css, /\.cp-ui iframe:focus,/);
  assert.match(css, /\.cp-ui details:not\(\[open\]\) > :not\(summary\)\s*\{[^}]*display:\s*none\s*!important/s);
});

test('workbench auxiliary tables offer keyboard-equivalent selection and context actions', () => {
  const index = template('index.html');
  const zixuan = fs.readFileSync(zixuanJsPath, 'utf8');
  const bkgn = fs.readFileSync(bkgnJsPath, 'utf8');

  assert.match(index, /id="zixuan_stock_wrap"[^>]*tabindex="0"[^>]*role="region"/);
  assert.match(index, /Shift\+F10/);
  assert.match(index, /id="bkgn_layer_toggle"[^>]*aria-expanded="true"[^>]*aria-controls="bkgn_layer_body"/);
  assert.match(index, /id="bkgn_table_wrap"[^>]*tabindex="0"[^>]*role="region"/);
  assert.match(index, /id="zixuan_keyboard_status"[^>]*aria-live="polite"/);
  assert.match(index, /id="bkgn_keyboard_status"[^>]*aria-live="polite"/);
  assert.match(zixuan, /function bindWatchlistKeyboard\(/);
  assert.match(zixuan, /event\.key === "ContextMenu" \|\| \(event\.shiftKey && event\.key === "F10"\)/);
  assert.match(zixuan, /new MouseEvent\("contextmenu"/);
  assert.match(bkgn, /function bind_bkgn_table_keyboard\(/);
  assert.match(bkgn, /attr\("aria-expanded", collapsed \? "false" : "true"\)/);
});

test('form action docks stay in document flow and never cover controls', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(css, /\.cp-actions--sticky\s*\{[^}]*position:\s*static[^}]*bottom:\s*auto/s);
  assert.doesNotMatch(css, /\.cp-actions--sticky\s*\{[^}]*position:\s*sticky/s);
});

test('critical workbench summary values wrap instead of disappearing at text zoom', () => {
  const css = fs.readFileSync(chartAnalysisCssPath, 'utf8');

  assert.match(css, /\.ca-analysis-group-head > small\s*\{[^}]*overflow:\s*visible[^}]*white-space:\s*normal/s);
  assert.match(css, /#chart_menu \.ca-menu-title-main\s*\{[^}]*overflow:\s*visible[^}]*white-space:\s*normal/s);
  assert.match(css, /#chart_menu \.layui-colla-title\s*\{[^}]*height:\s*auto[^}]*min-height:\s*46px/s);
  assert.match(css, /#chart_menu \.layui-colla-icon\s*\{[^}]*position:\s*absolute[^}]*top:\s*50%[^}]*right:\s*13px[^}]*left:\s*auto[^}]*margin-top:\s*-7px/s);

  for (const selector of ['\\.ca-symbol-name', '\\.ca-summary-facts strong']) {
    assert.match(css, new RegExp(`${selector}\\s*\\{[^}]*overflow-wrap:\\s*anywhere`, 's'));
    assert.match(css, new RegExp(`${selector}\\s*\\{[^}]*white-space:\\s*normal`, 's'));
    assert.doesNotMatch(css, new RegExp(`${selector}\\s*\\{[^}]*text-overflow:\\s*ellipsis`, 's'));
  }
});

test('workbench semantic colors remain readable in light and dark themes', () => {
  const chartCss = fs.readFileSync(chartAnalysisCssPath, 'utf8');
  const zixuanCss = fs.readFileSync(zixuanCssPath, 'utf8');
  const zixuanJs = fs.readFileSync(zixuanJsPath, 'utf8');
  const token = (scope, name) => {
    const match = scope.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, 'i'));
    assert.ok(match, `missing hexadecimal token ${name}`);
    return match[1];
  };
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((part) => parseInt(part, 16) / 255)
      .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const contrast = (left, right) => {
    const a = luminance(left);
    const b = luminance(right);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const assertReadable = (scope, names, background) => {
    for (const name of names) {
      assert.ok(
        contrast(token(scope, name), background) >= 4.5,
        `${name} must retain 4.5:1 contrast against ${background}`,
      );
    }
  };

  const chartDarkStart = chartCss.indexOf('html.cl-theme-dark #chart_menu');
  const chartLight = chartCss.slice(0, chartDarkStart);
  const chartDark = chartCss.slice(chartDarkStart, chartCss.indexOf('#chart_menu *,', chartDarkStart));
  const zixuanDarkStart = zixuanCss.indexOf('html.cl-theme-dark');
  const zixuanLight = zixuanCss.slice(0, zixuanDarkStart);
  const zixuanDark = zixuanCss.slice(zixuanDarkStart, zixuanCss.indexOf('.zx-watch-content', zixuanDarkStart));

  assertReadable(chartLight, [
    '--ca-text-muted', '--ca-blue', '--ca-live', '--ca-buy', '--ca-sell', '--ca-warning',
  ], '#f6f7f9');
  assertReadable(chartDark, [
    '--ca-text-muted', '--ca-blue', '--ca-live', '--ca-buy', '--ca-sell', '--ca-warning',
  ], '#232324');
  assert.ok(contrast('#ffffff', token(chartLight, '--ca-blue-fill')) >= 4.5);
  assert.ok(contrast('#ffffff', token(chartDark, '--ca-blue-fill')) >= 4.5);

  assertReadable(zixuanLight, [
    '--zx-text-muted', '--zx-primary', '--zx-success', '--zx-danger',
    '--zx-rate-up', '--zx-rate-down', '--zx-rate-flat', '--zx-marker-warning',
  ], '#f6f8fb');
  assertReadable(zixuanDark, [
    '--zx-text-muted', '--zx-primary', '--zx-success', '--zx-danger',
    '--zx-rate-up', '--zx-rate-down', '--zx-rate-flat', '--zx-marker-warning',
  ], '#232324');
  assert.ok(contrast('#ffffff', token(zixuanLight, '--zx-primary-fill')) >= 4.5);
  assert.ok(contrast('#ffffff', token(zixuanDark, '--zx-primary-fill')) >= 4.5);

  assert.match(chartCss, /#chart_menu \.layui-input::placeholder[\s\S]*color:\s*var\(--ca-text-muted\)/);
  assert.match(chartCss, /#chart_menu xm-select \.xm-tips\s*\{[^}]*color:\s*var\(--ca-text-muted\)/s);
  assert.match(zixuanCss, /\.zx-watch-panel \.layui-font-gray\s*\{[^}]*color:\s*var\(--zx-text-muted\)/s);
  assert.match(zixuanJs, /function accessibleDisplayColor\(/);
  assert.match(zixuanJs, /var\(--zx-rate-up, #c93612\)/);
  assert.match(zixuanJs, /var\(--zx-rate-down, #08796f\)/);
});

test('symbol columns prioritize code and name at narrow widths', () => {
  const symbols = template('symbols.html');

  assert.match(symbols, /matchMedia\('\(max-width: 720px\)'\)/);
  assert.match(symbols, /width: compactColumns \? 132 : 220/);
  assert.match(symbols, /if \(!compactColumns\) \{[\s\S]*field: 'pinyin'[\s\S]*field: 'type'/);
  assert.match(symbols, /compactMedia\.addEventListener\('change', handleColumnBreakpoint\)/);
});
