(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChartAnalysis = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';
  const layers = ['fx', 'bi', 'xd', 'center_all'];
  let managerId = null;
  let initialized = false;
  let pointRenderKey = null;
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
      && ['native_centers', 'centers_and_signals'].includes(snapshot?.analysis_scope)
      && snapshot.levels?.length >= 1;
    const centers = valid ? snapshot.levels[0].centers || [] : [];
    const previews = valid ? snapshot.levels[0].center_previews || [] : [];
    return {bars: data?.bars?.length || 0, strokes: data?.bis?.length || 0,
      segments: data?.xds?.length || 0, centers, previews, ready: valid,
      ...(valid && snapshot.segment_construction ? {segmentConstruction: snapshot.segment_construction} : {}),
      loading: data?.strict_structure_error?.code === 'strict_structure_pending'};
  }
  function segmentStatusText(summary) {
    if (!summary.ready || !summary.segmentConstruction) return '';
    const state = summary.segmentConstruction;
    if (state.status === 'unresolved') {
      return `已确认 ${state.confirmed_segments} 条线段；末尾 ${state.tail.pen_count} 笔的线段分界待判定，灰色区间不作为选股确认段。`;
    }
    if (state.preview_segments) return `已确认 ${state.confirmed_segments} 条线段；${state.preview_segments} 条虚线为形成中预览，端点尚未确认。`;
    if (state.awaiting_confirmation_segments) return `已确认 ${state.confirmed_segments} 条线段；${state.awaiting_confirmation_segments} 条线段仍在等待证据笔确认。`;
    return state.status === 'awaiting_pens' ? `已确认 ${state.confirmed_segments} 条线段；末尾等待新段形成。` : `已确认 ${state.confirmed_segments} 条线段。`;
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
    refreshPointExits(data, manager);
    const forming = summary.previews.filter(item => !item.owner_center_id);
    const count = summary.centers.length + forming.length;
    text('ca-current-symbol', identity ? identity.symbol + ' · ' + formatResolution(identity.interval) : '等待行情');
    text('ca-selection-symbol', identity?.symbol || '市场 · 代码');
    text('ca-centers-detail-label', '查看中枢区间（' + count + '）');
    text('ca-bar-count', summary.bars || '—');
    text('ca-bi-count', summary.ready ? summary.strokes : '—');
    text('ca-xd-count', summary.ready ? summary.segments : '—');
    text('ca-segment-status', segmentStatusText(summary));
    text('ca-center-count', summary.ready ? count : '—');
    text('ca-native-status', summary.ready
      ? (count ? `本周期中枢 ${summary.centers.length} 个，形成中 ${forming.length} 个；虚线表示未确认部分`
        : '当前已加载区间尚无满足完整条件的中枢')
      : summary.loading ? 'K 线已加载，正在计算笔、线段与中枢…'
      : data ? '结构暂未就绪，请查看图表加载状态' : '正在加载行情…');
    const tbody = root.document?.getElementById('ca-native-centers');
    if (tbody) {
      tbody.replaceChildren();
      [...summary.centers, ...forming].forEach((center, index) => {
        const row = root.document.createElement('tr');
        const pending = center.render_kind === 'center_preview';
        const projection = summary.previews.find(item => item.owner_center_id === center.center_id);
        const status = pending ? (center.preview_status === 'awaiting_leave' ? '形成中 / 待离开段'
          : center.preview_status === 'awaiting_completion_confirmation' ? '回试待确认' : '形成中 / 待线段确认')
          : center.third_class_confirmed ? '已结束' : center.state === 'divergence_closed' ? '背驰分界'
          : center.state === 'superseded' ? '后继中枢已成立'
          : projection?.preview_status === 'awaiting_completion_confirmation' ? '回试待确认' : '延伸 / 观察';
        const values = [pending ? 'P' + (index - summary.centers.length + 1) : 'Z' + (index + 1),
          formatPrice(center.core?.zd_price) + ' ～ ' + formatPrice(center.core?.zg_price), status];
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
    root.document.getElementById('ca-point-details')?.addEventListener('toggle', () => refresh());
    root.addEventListener('chanlun-bars-ready', event => refresh(event.detail || {}));
    root.addEventListener('chanlun-chart-context-changed', () => refresh());
    root.setInterval(() => {if (!root.document.hidden) refresh();}, 5000);
    refresh();
  }
  function refreshPointExits(data, manager) {
    const snapshot = data?.strict_structure;
    const points = data?.strict_structure_mode === 'replace'
      ? (snapshot?.levels || []).flatMap(level => (level.points || []).map(point => ({...point, level_label: level.label}))) : [];
    text('ca-points-detail-label', `买卖点止盈止损参考（${points.length}）`);
    const container = root.document?.getElementById('ca-point-exits');
    if (!container || !root.document?.getElementById('ca-point-details')?.open || !root.PointExitInfo) return;
    const key = JSON.stringify([manager?.id, snapshot?.symbol, snapshot?.source_frequency,
      snapshot?.snapshot_revision, snapshot?.point_exit_version, points.length]);
    if (key === pointRenderKey) return;
    pointRenderKey = key;
    container.replaceChildren();
    points.sort((a,b) => b.anchor_at - a.anchor_at || a.structural_level - b.structural_level);
    for (const point of points) {
      const info = root.PointExitInfo.describe(snapshot.point_exit_plans?.[point.point_id], snapshot.point_exit_sources);
      const article = root.document.createElement('article');
      article.className = 'ca-point-exit';
      const label = root.document.createElement('strong');
      label.textContent = `${point.level_label || snapshot.source_frequency} · ${root.PointExitInfo.names[point.point_type] || point.point_type} · ${root.PointExitInfo.time(point.anchor_at)}`;
      article.appendChild(label);
      for (const value of [`止损参考 ${info.stop}`, `止盈 / 退出参考 ${info.profit}`]) {
        const p = root.document.createElement('p'); p.textContent = value; article.appendChild(p);
      }
      const detail = root.document.createElement('details'), heading = root.document.createElement('summary');
      heading.textContent = '触发条件与依据'; detail.appendChild(heading);
      for (const value of [...info.lines, ...info.sources]) {
        const p = root.document.createElement('p'); p.textContent = value; detail.appendChild(p);
      }
      article.appendChild(detail); container.appendChild(article);
    }
    const sources = root.document.getElementById('ca-point-exit-sources');
    if (sources) {
      sources.replaceChildren();
      for (const source of Object.values(snapshot?.point_exit_sources || {})) {
        const p = root.document.createElement('p');
        p.textContent = `${source.title}：${source.path}，第 ${source.lines} 行。`;
        sources.appendChild(p);
      }
    }
  }
  if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
  }
  return {formatPrice, formatResolution, summarizeChartData, segmentStatusText, layerIsVisible, setLayerVisibility, refresh, init};
});
