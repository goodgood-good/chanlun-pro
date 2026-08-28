'use strict';

(function initChanlunStructureLineStates(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChanlunStructureLineStates = api;

  // ``ChartManager`` is a global lexical binding declared by charts.js.  The
  // patch is loaded immediately after that file and before any page creates a
  // chart manager, so every structure line uses the same three-state contract.
  try {
    if (typeof ChartManager !== 'undefined') api.install(ChartManager);
  } catch (_error) {
    // Unit-test/partial pages may intentionally omit charts.js.
  }
})(typeof window !== 'undefined' ? window : globalThis, function buildApi() {
  function stateOf(item) {
    if (item && item.locked === true) return 'locked';
    const explicit = String(item && item.state || '').trim().toLowerCase();
    if (['forming', 'formed', 'locked'].includes(explicit)) return explicit;
    const style = Number.parseInt(item && item.linestyle, 10);
    if (style === 1) return 'forming';
    if (style === 2) return 'formed';
    return 'locked';
  }

  function carriesLineState(item) {
    return Boolean(item && (
      Object.prototype.hasOwnProperty.call(item, 'state')
      || Object.prototype.hasOwnProperty.call(item, 'locked')
      || Object.prototype.hasOwnProperty.call(item, 'linestyle')
    ));
  }

  function renderKey(baseKey, item) {
    return carriesLineState(item) ? `${baseKey}::${stateOf(item)}` : baseKey;
  }

  function normalizeBaseStructureLine(item) {
    if (!item || (
      !Object.prototype.hasOwnProperty.call(item, 'state')
      && !Object.prototype.hasOwnProperty.call(item, 'locked')
    )) return item;
    // 兼容服务重启前已经落盘的旧图表缓存：内部 state 仍能准确区分
    // formed/forming，渲染层强制保证只有 forming 使用虚线。
    const linestyle = stateOf(item) === 'forming' ? '2' : '0';
    return String(item.linestyle) === linestyle ? item : { ...item, linestyle };
  }

  function uniqueRenderList(sourceList) {
    if (!Array.isArray(sourceList)) return [];
    const stable = [];
    const forming = [];
    sourceList.map(normalizeBaseStructureLine).forEach((item) => {
      if (stateOf(item) === 'forming') forming.push(item);
      else stable.push(item);
    });
    if (forming.length) stable.push(forming[forming.length - 1]);
    return stable;
  }

  function install(Manager) {
    const prototype = Manager && Manager.prototype;
    if (!prototype || prototype.__chanlunThreeStateLinesInstalled) return;
    const originalMakeKey = prototype.makeKey;
    if (typeof originalMakeKey !== 'function') return;

    prototype.makeKey = function threeStateStructureLineKey(item) {
      return renderKey(originalMakeKey.call(this, item), item);
    };
    prototype.getUniqueRenderList = uniqueRenderList;
    Object.defineProperty(prototype, '__chanlunThreeStateLinesInstalled', {
      configurable: false,
      enumerable: false,
      value: true,
      writable: false,
    });
  }

  return Object.freeze({
    stateOf,
    renderKey,
    normalizeBaseStructureLine,
    uniqueRenderList,
    install,
  });
});
