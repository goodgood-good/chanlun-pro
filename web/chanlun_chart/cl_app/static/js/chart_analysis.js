(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChartAnalysis = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';
  const layers = ['fx', 'bi', 'xd', 'center_all'];
  let managerId = null;
  let initialized = false;
  function formatPrice(value) {
    const n = Number(value);
    return value == null || !Number.isFinite(n) ? '—' : n.toLocaleString('zh-CN', {maximumFractionDigits: 6});
  }
  function formatResolution(value) {
    const key = String(value || '');
    return /^\d+$/.test(key) ? key + 'm' : key;
  }
  function summarizeChartData(data) {
    const snapshot = data?.strict_structure;
    const valid = data?.strict_structure_mode === 'replace'
      && snapshot?.analysis_scope === 'native_centers' && snapshot.levels?.length === 1;
    const centers = valid ? snapshot.levels[0].centers || [] : [];
    return {bars: data?.bars?.length || 0, strokes: data?.bis?.length || 0,
      segments: data?.xds?.length || 0, centers, ready: valid,
      loading: data?.strict_structure_error?.code === 'strict_structure_pending'};
  }
  function currentManager(detail) {
    const managers = Object.values(root.__cm || {});
    if (detail?.managerId) managerId = detail.managerId;
    const registry = root.getTVRegistry?.();
    const active = registry?.activeManagerId || managerId;
    return managers.find(manager => manager.id === active || manager.instanceId === active)
      || managers[0] || null;
  }

  function layerIsVisible(manager, layer) {
    return !!manager && layers.includes(layer) && manager.cl_show_config?.[layer] !== false;
  }
  function setLayerVisibility(manager, layer, visible) {
    if (!manager || !layers.includes(layer)) return false;
    manager.cl_show_config = {...manager.cl_show_config, [layer]: !!visible};
    if (typeof root.saveClShowConfig === 'function') {
      root.saveClShowConfig(manager.id, manager._curResolution, manager.cl_show_config);
    }
    manager.debouncedDrawChanlun?.();
    return true;
  }
  function text(id, value) {
    const element = root.document?.getElementById(id);
    if (element) element.textContent = value;
  }
  function refresh(detail = {}) {
    const manager = currentManager(detail);
    let data = null;
    let identity = null;
    try {
      identity = manager?.getCurrentChartIdentity?.() || manager?.widget?.symbolInterval?.();
      data = manager?.getChartData?.()?.barsResult || null;
    } catch (_) { /* The chart may still be resolving a symbol. */ }
    const summary = summarizeChartData(data);
    text('ca-current-symbol', identity ? identity.symbol + ' · ' + formatResolution(identity.interval) : '等待行情');
    text('ca-selection-symbol', identity?.symbol || '市场 · 代码');
    text('ca-centers-detail-label', '查看中枢区间（' + summary.centers.length + '）');
    text('ca-bar-count', summary.bars || '—');
    text('ca-bi-count', summary.ready ? summary.strokes : '—');
    text('ca-xd-count', summary.ready ? summary.segments : '—');
    text('ca-center-count', summary.ready ? summary.centers.length : '—');
    text('ca-native-status', summary.ready
      ? (summary.centers.length ? '已显示本周期线段中枢' : '当前已加载区间尚无满足完整条件的中枢')
      : summary.loading ? 'K 线已加载，正在计算笔、线段与中枢…'
      : data ? '结构暂未就绪，请查看图表加载状态' : '正在加载行情…');
    const tbody = root.document?.getElementById('ca-native-centers');
    if (tbody) {
      tbody.replaceChildren();
      summary.centers.forEach((center, index) => {
        const row = root.document.createElement('tr');
        const values = ['Z' + (index + 1), formatPrice(center.core?.zd_price) + ' ～ ' + formatPrice(center.core?.zg_price),
          center.third_class_confirmed ? '已结束' : center.state === 'superseded' ? '后继中枢已成立' : '延伸 / 观察'];
        values.forEach(value => {const cell = root.document.createElement('td'); cell.textContent = value; row.appendChild(cell);});
        tbody.appendChild(row);
      });
    }
    root.document?.querySelectorAll('[data-chart-layer]').forEach(button => {
      button.disabled = !manager;
      button.setAttribute('aria-pressed', String(layerIsVisible(manager, button.dataset.chartLayer)));
    });
    return summary;
  }
  function init() {
    if (initialized || !root.document) return;
    initialized = true;
    root.document.querySelectorAll('[data-chart-layer]').forEach(button => button.addEventListener('click', () => {
      const manager = currentManager({}); const layer = button.dataset.chartLayer;
      setLayerVisibility(manager, layer, !layerIsVisible(manager, layer)); refresh();
    }));
    root.document.getElementById('ca-refresh-analysis')?.addEventListener('click', () => refresh());
    root.addEventListener('chanlun-bars-ready', event => refresh(event.detail || {}));
    root.addEventListener('chanlun-chart-context-changed', () => refresh());
    root.setInterval(() => {if (!root.document.hidden) refresh();}, 5000);
    refresh();
  }
  if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
  }
  return {formatPrice, formatResolution, summarizeChartData, layerIsVisible, setLayerVisibility, refresh, init};
});
