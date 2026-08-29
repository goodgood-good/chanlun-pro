'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');

function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function: ${name}`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unterminated function: ${name}`);
}

function loadOutsideDismissApi() {
  const context = {};
  vm.runInNewContext(`
    ${extractFunction('_collectSameOriginDocuments')}
    ${extractFunction('bindClDisplayMenuOutsideDismiss')}
    this.__api = { _collectSameOriginDocuments, bindClDisplayMenuOutsideDismiss };
  `, context);
  return context.__api;
}

function loadButtonAccessibilityApi() {
  const context = {};
  vm.runInNewContext(`
    ${extractFunction('bindClDisplayButtonAccessibility')}
    this.__api = { bindClDisplayButtonAccessibility };
  `, context);
  return context.__api;
}

function loadFloatingMenuApi() {
  const context = {};
  vm.runInNewContext(`
    ${extractFunction('_topWindowOffset')}
    ${extractFunction('_elementRectInTopWindow')}
    ${extractFunction('positionClDisplayMenuNearPointer')}
    ${extractFunction('clampClDisplayMenuToViewport')}
    ${extractFunction('bindClDisplayMenuViewportGuard')}
    ${extractFunction('bindClDisplayMenuDrag')}
    this.__api = {
      positionClDisplayMenuNearPointer,
      clampClDisplayMenuToViewport,
      bindClDisplayMenuViewportGuard,
      bindClDisplayMenuDrag,
    };
  `, context);
  return context.__api;
}

function fakeDocument(frames = []) {
  const listeners = new Map();
  return {
    querySelectorAll(selector) {
      return selector === 'iframe' ? frames : [];
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      if (listeners.has(type)) listeners.get(type).delete(listener);
    },
    dispatch(type, targetOrEvent) {
      const looksLikeEvent = targetOrEvent && (
        Object.prototype.hasOwnProperty.call(targetOrEvent, 'target')
        || Object.prototype.hasOwnProperty.call(targetOrEvent, 'pointerId')
        || Object.prototype.hasOwnProperty.call(targetOrEvent, 'clientX')
        || Object.prototype.hasOwnProperty.call(targetOrEvent, 'key')
      );
      const event = looksLikeEvent
        ? targetOrEvent
        : { target: targetOrEvent };
      if (!event.type) event.type = type;
      for (const listener of [...(listeners.get(type) || [])]) listener(event);
    },
    listenerCount(type) {
      return (listeners.get(type) || new Set()).size;
    },
  };
}

test('缠论显示菜单使用固定分组顺序和严格递归层级', () => {
  const titles = ['基础结构', '中枢控制', '走势类型', '买卖点', '背驰', '画线设置'];
  const indexes = titles.map((title) => source.indexOf(`_grpTitle('${title}'`));
  assert.ok(indexes.every((index) => index >= 0), `missing titles: ${indexes}`);
  assert.deepEqual([...indexes].sort((a, b) => a - b), indexes);

  for (const label of [
    '笔中枢',
    '中枢总开关',
    '走势类型总开关',
    '买卖点总开关',
    '背驰总开关',
    '盘整背驰',
    '趋势背驰',
    '独立周期画线',
  ]) {
    assert.ok(source.includes(label), `missing menu label: ${label}`);
  }
  assert.ok(source.includes('const _displayLevels = recursiveDisplayLevels(_curInterval)'));
  assert.ok(source.includes('key: `center_L${item.level}`'));
  assert.ok(source.includes('key: `trend_L${item.level}`'));
  assert.ok(source.includes('key: `point_L${item.level}`'));
  assert.ok(source.includes('key: `divergence_consolidation_L${item.level}`'));
  assert.ok(source.includes('key: `divergence_trend_L${item.level}`'));
  for (const removedKey of ['center_1m', 'center_5m', 'center_30m', 'center_d']) {
    assert.equal(source.includes(removedKey), false);
  }
  assert.equal(source.includes('严格递归中枢总开关'), false);
  assert.ok(source.includes("_cbRow('center_all', '中枢总开关')"));
  assert.ok(source.includes("_grpTitle('背驰', '由当前 K 线递归产生')"));
  assert.equal(source.includes('形成中 / 投影（非正式）'), false);
  assert.equal(source.includes('待定尾段（非正式）'), false);
  assert.equal(source.includes("_cbRow('center_provisional'"), false);
  assert.equal(source.includes("_cbRow('pending_movement'"), false);
  assert.ok(source.includes('const _pointLevels = _displayLevels.map'));
  assert.ok(source.includes('..._pointLevels.map((item) => item.key)'));
});

test('菜单不暴露接近触发、中枢投影或未完成结构复选框', () => {
  assert.equal(source.includes('接近触发（未确认）'), false);
  assert.equal(source.includes("_cbRow('center_projection'"), false);
  assert.equal(source.includes("cbId('center_projection')"), false);
  assert.equal(source.includes("cbId('center_provisional')"), false);
  assert.equal(source.includes("cbId('pending_movement')"), false);
});

test('显示设置浮层保留纵向拖动空间并在内部滚动', () => {
  assert.ok(source.includes('width:min(440px,calc(100vw - 16px));min-width:0;'));
  assert.ok(source.includes('max-width:calc(100vw - 16px);max-height:min(72vh,680px);overflow:auto;'));
  assert.ok(source.includes('grid-template-columns:repeat(2,minmax(0,1fr))'));
});

test('显示设置入口与弹窗提供键盘和对话框语义', () => {
  assert.ok(source.includes("btnDisplay.setAttribute('role', 'button')"));
  assert.ok(source.includes("btnDisplay.setAttribute('tabindex', '0')"));
  assert.ok(source.includes("btnDisplay.setAttribute('aria-haspopup', 'dialog')"));
  assert.ok(source.includes('role="dialog" aria-modal="false"'));
  assert.ok(source.includes('aria-label="拖动缠论显示设置；方向键移动"'));
  assert.ok(source.includes("event.key !== 'Enter' && event.key !== ' '"));
  assert.ok(source.includes('bindClDisplayButtonAccessibility(btnDisplay)'));
});

test('嵌套页面按菜单宿主窗口定位而不是误用最外层窗口', () => {
  assert.ok(source.includes(
    'const menuWindow = menuElement?.ownerDocument?.defaultView || window;',
  ));
  assert.ok(source.includes('event,\n                        btnDisplay,\n                        menuWindow,'));
  assert.ok(source.includes('dragHandle,\n                        menuWindow,'));
  assert.ok(source.includes('menuElement,\n                        menuWindow,'));
});

test('TradingView 异步覆盖后仍恢复显示设置入口的可访问属性', () => {
  const { bindClDisplayButtonAccessibility } = loadButtonAccessibilityApi();
  const attributes = new Map();
  const timers = new Map();
  let nextTimer = 1;
  let activeObserver = null;
  class FakeMutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.disconnected = false;
      activeObserver = this;
    }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const ownerWindow = {
    MutationObserver: FakeMutationObserver,
    setTimeout(callback) {
      const id = nextTimer;
      nextTimer += 1;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  const button = {
    ownerDocument: { defaultView: ownerWindow },
    getAttribute(name) { return attributes.get(name) ?? null; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
  };

  const cleanup = bindClDisplayButtonAccessibility(button);
  assert.equal(attributes.get('role'), 'button');
  assert.equal(attributes.get('tabindex'), '0');
  assert.equal(attributes.get('aria-disabled'), 'false');

  attributes.set('tabindex', '-1');
  attributes.set('aria-disabled', 'true');
  activeObserver.callback();
  assert.equal(attributes.get('tabindex'), '0');
  assert.equal(attributes.get('aria-disabled'), 'false');

  cleanup();
  assert.equal(activeObserver.disconnected, true);
  assert.equal(timers.size, 0);
});

test('点击主页面或嵌套图表空白处会关闭菜单并清理监听器', () => {
  const { _collectSameOriginDocuments, bindClDisplayMenuOutsideDismiss } = loadOutsideDismissApi();
  const nestedDocument = fakeDocument();
  const chartDocument = fakeDocument([{ contentDocument: nestedDocument }]);
  const crossOriginFrame = {};
  Object.defineProperty(crossOriginFrame, 'contentDocument', {
    get() { throw new Error('cross origin'); },
  });
  const rootDocument = fakeDocument([
    { contentDocument: chartDocument },
    crossOriginFrame,
  ]);
  const documents = _collectSameOriginDocuments(rootDocument);
  assert.equal(documents.length, 3);
  assert.equal(documents[0], rootDocument);
  assert.equal(documents[1], chartDocument);
  assert.equal(documents[2], nestedDocument);

  const menu = { contains: (target) => target && target.area === 'menu' };
  const trigger = { contains: (target) => target && target.area === 'trigger' };
  let dismissCount = 0;
  const cleanup = bindClDisplayMenuOutsideDismiss(
    rootDocument,
    menu,
    trigger,
    () => { dismissCount += 1; },
  );

  rootDocument.dispatch('pointerdown', { area: 'menu' });
  chartDocument.dispatch('click', { area: 'trigger' });
  assert.equal(dismissCount, 0, '菜单内部及触发按钮点击不应被外部关闭器处理');

  nestedDocument.dispatch('pointerdown', { area: 'blank' });
  assert.equal(dismissCount, 1, '嵌套图表空白处按下时应立即关闭');
  for (const doc of documents) {
    assert.equal(doc.listenerCount('pointerdown'), 0);
    assert.equal(doc.listenerCount('click'), 0);
    assert.equal(doc.listenerCount('keydown'), 0);
  }

  nestedDocument.dispatch('click', { area: 'blank' });
  cleanup();
  assert.equal(dismissCount, 1, '关闭和手动 cleanup 都必须幂等');

  let rootDismissCount = 0;
  bindClDisplayMenuOutsideDismiss(
    rootDocument,
    menu,
    trigger,
    () => { rootDismissCount += 1; },
  );
  rootDocument.dispatch('click', { area: 'blank' });
  assert.equal(rootDismissCount, 1, '主文档空白处的 click 也应关闭菜单');

  let escapeDismissCount = 0;
  let escapePrevented = false;
  let escapeStopped = false;
  bindClDisplayMenuOutsideDismiss(
    rootDocument,
    menu,
    trigger,
    () => { escapeDismissCount += 1; },
  );
  rootDocument.dispatch('keydown', {
    type: 'keydown',
    key: 'Escape',
    target: { area: 'menu' },
    preventDefault() { escapePrevented = true; },
    stopPropagation() { escapeStopped = true; },
  });
  assert.equal(escapeDismissCount, 1, '菜单内部按 Escape 也应关闭');
  assert.equal(escapePrevented, true);
  assert.equal(escapeStopped, true);
});

test('显示设置弹窗以 iframe 内鼠标位置为锚点并限制在视口内', () => {
  const { positionClDisplayMenuNearPointer } = loadFloatingMenuApi();
  const topDocument = {
    documentElement: { clientWidth: 800, clientHeight: 600 },
  };
  const topWindow = {
    document: topDocument,
    innerWidth: 800,
    innerHeight: 600,
    scrollX: 100,
    scrollY: 200,
  };
  topDocument.defaultView = topWindow;
  const frameElement = {
    ownerDocument: topDocument,
    getBoundingClientRect: () => ({ left: 50, top: 40 }),
  };
  const childWindow = { frameElement };
  const childDocument = { defaultView: childWindow };
  const trigger = {
    ownerDocument: childDocument,
    getBoundingClientRect: () => ({
      left: 100, top: 50, right: 180, bottom: 80,
    }),
  };
  const menu = {
    style: {},
    getBoundingClientRect: () => ({ width: 300, height: 250 }),
  };

  const position = positionClDisplayMenuNearPointer(
    menu,
    { type: 'click', detail: 1, clientX: 120, clientY: 100, view: childWindow },
    trigger,
    topWindow,
  );

  assert.equal(position.left, 282);
  assert.equal(position.top, 352);
  assert.equal(position.anchor, 'pointer');
  assert.equal(menu.style.left, '282px');
  assert.equal(menu.style.top, '352px');

  const edgePosition = positionClDisplayMenuNearPointer(
    menu,
    { clientX: 740, clientY: 550, view: topWindow },
    trigger,
    topWindow,
  );
  assert.equal(edgePosition.left, 528, '右侧空间不足时应翻到鼠标左侧');
  assert.equal(edgePosition.top, 488, '下方空间不足时应翻到鼠标上方');

  const keyboardPosition = positionClDisplayMenuNearPointer(
    menu,
    {
      type: 'click', detail: 0, clientX: 0, clientY: 0, view: childWindow,
    },
    trigger,
    topWindow,
  );
  assert.equal(keyboardPosition.left, 262, '无鼠标坐标的 click 应回退到触发按钮附近');
  assert.equal(keyboardPosition.top, 332);
  assert.equal(keyboardPosition.anchor, 'trigger');
});

test('窗口尺寸变化会把显示设置弹窗重新约束到视口内并在关闭时解绑', () => {
  const {
    bindClDisplayMenuViewportGuard,
  } = loadFloatingMenuApi();
  const listeners = new Map();
  const document = {
    documentElement: { clientWidth: 600, clientHeight: 500 },
  };
  const topWindow = {
    document,
    innerWidth: 600,
    innerHeight: 500,
    scrollX: 0,
    scrollY: 0,
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
  const menu = {
    style: {},
    getBoundingClientRect: () => ({
      left: 401, top: 420, width: 440, height: 360,
    }),
  };

  const cleanup = bindClDisplayMenuViewportGuard(menu, topWindow);
  listeners.get('resize')();
  assert.equal(menu.style.left, '152px');
  assert.equal(menu.style.top, '132px');
  assert.equal(typeof listeners.get('orientationchange'), 'function');

  cleanup();
  assert.equal(listeners.size, 0);
});

test('显示设置标题栏可拖动并在关闭时清理拖动监听器', () => {
  const { bindClDisplayMenuDrag } = loadFloatingMenuApi();
  const listeners = new Map();
  const styleValues = new Map([
    ['user-select', { value: 'text', priority: 'important' }],
  ]);
  const rootStyle = {
    setProperty(name, value, priority = '') {
      styleValues.set(name, { value, priority });
    },
    removeProperty(name) { styleValues.delete(name); },
    getPropertyValue(name) { return styleValues.get(name)?.value || ''; },
    getPropertyPriority(name) { return styleValues.get(name)?.priority || ''; },
  };
  const document = {
    documentElement: {
      clientWidth: 800,
      clientHeight: 600,
      style: rootStyle,
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      if (listeners.has(type)) listeners.get(type).delete(listener);
    },
    dispatch(type, event) {
      for (const listener of [...(listeners.get(type) || [])]) listener(event);
    },
  };
  const windowListeners = new Map();
  const topWindow = {
    document,
    innerWidth: 800,
    innerHeight: 600,
    scrollX: 0,
    scrollY: 0,
    addEventListener(type, listener) { windowListeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (windowListeners.get(type) === listener) windowListeners.delete(type);
    },
  };
  const handleListeners = new Map();
  const handle = {
    style: {},
    addEventListener(type, listener) { handleListeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (handleListeners.get(type) === listener) handleListeners.delete(type);
    },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
  const menuRect = {
    left: 100, top: 120, width: 300, height: 250,
  };
  const menuStyle = {};
  Object.defineProperty(menuStyle, 'left', {
    get() { return `${menuRect.left}px`; },
    set(value) { menuRect.left = Number.parseFloat(value); },
  });
  Object.defineProperty(menuStyle, 'top', {
    get() { return `${menuRect.top}px`; },
    set(value) { menuRect.top = Number.parseFloat(value); },
  });
  const menu = {
    style: menuStyle,
    getBoundingClientRect: () => ({ ...menuRect }),
  };
  const cleanup = bindClDisplayMenuDrag(menu, handle, topWindow);

  handleListeners.get('pointerdown')({
    button: 0,
    pointerId: 7,
    clientX: 200,
    clientY: 200,
    preventDefault() {},
  });
  document.dispatch('pointermove', {
    pointerId: 7,
    clientX: 350,
    clientY: 280,
    preventDefault() {},
  });
  assert.equal(menu.style.left, '250px');
  assert.equal(menu.style.top, '200px');
  assert.equal(rootStyle.getPropertyValue('user-select'), 'none');

  document.dispatch('pointerup', { pointerId: 7 });
  assert.equal(handle.style.cursor, 'grab');
  assert.equal(rootStyle.getPropertyValue('user-select'), 'text');
  assert.equal(rootStyle.getPropertyPriority('user-select'), 'important');

  let keyboardPrevented = false;
  handleListeners.get('keydown')({
    key: 'ArrowRight',
    shiftKey: true,
    preventDefault() { keyboardPrevented = true; },
    stopPropagation() {},
  });
  assert.equal(menu.style.left, '290px');
  assert.equal(menu.style.top, '200px');
  assert.equal(keyboardPrevented, true);

  handleListeners.get('pointerdown')({
    button: 0,
    pointerId: 8,
    clientX: 300,
    clientY: 220,
    preventDefault() {},
  });
  handleListeners.get('lostpointercapture')({ pointerId: 8 });
  assert.equal(rootStyle.getPropertyValue('user-select'), 'text');

  handleListeners.get('pointerdown')({
    button: 0,
    pointerId: 9,
    clientX: 300,
    clientY: 220,
    preventDefault() {},
  });
  windowListeners.get('blur')({ type: 'blur' });
  assert.equal(rootStyle.getPropertyValue('user-select'), 'text');

  cleanup();
  assert.equal(handleListeners.size, 0);
  assert.equal(windowListeners.size, 0);
  assert.equal((listeners.get('pointermove') || new Set()).size, 0);
  assert.equal((listeners.get('pointerup') || new Set()).size, 0);
  assert.equal((listeners.get('pointercancel') || new Set()).size, 0);
});
