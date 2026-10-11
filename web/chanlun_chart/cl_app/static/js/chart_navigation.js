(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ChartNavigation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const counts = {single:1, 'horizontal-2':2, 'horizontal-7-3':2, 'vertical-2':2, three:3, four:4};
  const intervals = new Set(['1','2','3','5','10','15','30','60','120','180','240','1D','1W','1M']);
  const frequencyIntervals = {'1m':'1','2m':'2','3m':'3','5m':'5','10m':'10','15m':'15','30m':'30',
    '60m':'60','120m':'120','180m':'180','240m':'240',d:'1D',w:'1W',m:'1M'};
  function normalizedInterval(value) {
    const text = String(value || '').trim().toUpperCase();
    const interval = {D:'1D',W:'1W',M:'1M'}[text] || text;
    return intervals.has(interval) ? interval : null;
  }
  function isManagedLayout(env) {
    try {
      return !!(env.parent && env.parent !== env && env.parent.location.origin === env.location.origin
        && env.parent.document.getElementById('chart-layout')
        && env.parent.document.getElementById('chart-frame')?.contentWindow === env);
    } catch (_) { return false; }
  }
  function layoutUrl(env, requestedLayout) {
    if (!Object.prototype.hasOwnProperty.call(counts, requestedLayout)) return null;
    const layout = env.WorkspaceDevice?.isPhone ? 'single' : requestedLayout;
    const query = new URLSearchParams(env.location.search || '');
    const evidence = query.has('screening_source') || query.has('screening_point');
    const target = !evidence && env.ChartAnalysis?.selectedIdentity?.();
    if (target) {
      // A stock selected after opening a list link must not revert to that
      // link's older symbol when the user explicitly changes the layout.
      query.set('market', target.market);
      query.set('code', target.code);
    }
    const previous = (query.get('intervals') || '').split(',').map(normalizedInterval);
    const market = query.get('market') || env.Utils?.get_market?.() || '';
    const current = Array.from(env.chart_widgets || []).map(widget => {
      try { return normalizedInterval(widget.activeChart?.()?.resolution?.()); } catch (_) { return null; }
    });
    const fallback = ['5','1D','30','1'];
    let selected = Array.from({length:counts[layout]}, (_, index) => {
      let saved;
      try { saved = normalizedInterval(env.Utils?.get_local_data?.(`${market}_interval_${index+1}`)); } catch (_) {}
      return (index === 0 && normalizedInterval(target?.interval)) || current[index] || previous[index]
        || (index === 0 && frequencyIntervals[query.get('frequency')]) || saved || fallback[index];
    });
    if (evidence) {
      // A layout choice may duplicate the saved view, not request unsaved
      // daily/other-period evidence through ordinary workspace defaults.
      const evidenceInterval = frequencyIntervals[query.get('frequency')] || previous[0] || current[0];
      if (!evidenceInterval) return null;
      selected = selected.map(() => evidenceInterval);
    }
    query.set('layout', layout);
    query.set('intervals', selected.join(','));
    // Keep symbol, frozen-evidence identity and all other caller parameters.
    return (env.location.pathname || '/') + '?' + query.toString() + (env.location.hash || '');
  }
  return {isManagedLayout, layoutUrl};
});
