(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.PointExitInfo = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const names = {'1buy':'一买','2buy':'二买','3buy':'三买','1sell':'一卖','2sell':'二卖','3sell':'三卖'};
  function price(value) {
    return value == null || !Number.isFinite(Number(value)) ? '—'
      : Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 8, useGrouping: false});
  }
  function time(value) {
    return !Number.isFinite(value) ? '—' : new Date(value * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
  }
  function describe(plan, sources = {}) {
    if (!plan) return {stop: '依据不足', profit: '待结构证据', lines: ['尚无可核对的止盈止损价格依据。'], sources: []};
    const sl = plan.stop_loss || {}, tp = plan.take_profit || {};
    const provisional = plan.point_status !== 'confirmed';
    const suffix = provisional ? '（预案）' : '';
    const edge = plan.side === 'buy' ? '中枢上沿 ZG' : '中枢下沿 ZD';
    const slBasis = {center_edge: edge, parent_extreme: '二类点所依赖的前一转折极值', turn_extreme: '该点的转折极值'}[sl.basis] || '结构边界';
    const trigger = {lt:'<', gt:'>', lte:'≤', gte:'≥'}[sl.trigger] || '';
    const stop = sl.price == null ? '依据不足' : `${trigger} ${price(sl.price)}${suffix}`;
    let profit = '待同级反向点';
    const lines = [`止损参考：${stop}；${slBasis}。该价格由结构失效条件推导。`];
    if (tp.price != null) {
      if (tp.basis === 'opposite_point') {
        profit = `${price(tp.price)}（反向点）`;
        lines.push(`止盈 / 退出参考：${price(tp.price)}；后续同级${names[tp.point_type] || '反向点'}，确认于 ${time(tp.available_at)}。这是后续确认的历史拐点价，不是入场时已知的目标价或确认时成交价；退出可能亏损。`);
      } else {
        profit = `${price(tp.price)}（${tp.basis === 'terminal_dd' ? 'DD' : 'GG'}观察）${suffix}`;
        lines.push(`止盈观察：${profit}；取趋势末中枢的外围极值，不是中枢核心边界，也不是固定兑现价格。仍需观察反向买卖点。`);
      }
    } else {
      lines.push('止盈参考：等待同级反向买卖点形成，当前没有可确定的固定目标价。');
    }
    if (tp.basis === 'opposite_point' && plan.profit_observation?.price != null) {
      lines.push(`原一类点的末中枢回抽观察价：${price(plan.profit_observation.price)}。`);
    }
    if (provisional) lines.push('该买卖点尚未确认；以上价格为条件预案，随未完成结构更新。');
    if (plan.side === 'sell') lines.push('卖点后的价格用于反向风险和回补观察，不表示已建立空头仓位。');
    const refs = [...new Set([...(sl.sources || []), ...(tp.sources || []), ...(plan.profit_observation?.sources || [])])];
    return {stop, profit, lines, sources: refs.map(id => {
      const source = sources[id];
      return source ? `${source.title}：${source.path}，第 ${source.lines} 行（正文 / 作者回复；价格映射为系统推导）。` : id;
    })};
  }
  function title(plan) {
    return describe(plan).lines.join('\n');
  }
  return {price, time, describe, title, names};
});
