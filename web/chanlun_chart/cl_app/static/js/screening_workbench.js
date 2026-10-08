(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof document !== 'undefined') api.boot();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const POINTS = {'1buy':'一买','2buy':'二买','3buy':'三买','1sell':'一卖','2sell':'二卖','3sell':'三卖'};
  const STAGES = {confirmed:'已确认',entry_pending:'正式三买已确认 · 入场证待核',triggered:'正式点确认',executable:'历史执行条件成立',approaching:'形成与等待',formed:'形成待确认',observed:'结构观察',monitoring:'关注观察',active:'结构跟踪',invalidated:'已失效',closed:'已结束'};
  const STATES = {idle:'尚未运行选股',starting:'准备选股',running:'正在选股',completed:'本次筛选完成',cancelled:'已取消 · 保留部分结果',interrupted:'任务中断',failed:'任务失败'};
  const RESULTS_LOADING = '正在读取并校验本轮选股结果，完成后会自动显示，无需重新选股。';
  function resultVersionMessage(status) {
    if (status.source_current !== false || !status.run_id) return '';
    if (status.frozen_native_review)
      return '源码已更新；下方显示原截止时刻经证据核验的中枢历史结果。重新选股可更新到最新行情。';
    return status.source_revision
      ? '该批次与当前规则版本不兼容，需重新选股。'
      : '本次结果的算法版本尚不可核对。';
  }
  function emptyResultsMessage(status={}, candidateCount=0) {
    if (status.results_loading && !status.results_available) return RESULTS_LOADING;
    if (status.results_loading && !candidateCount) return '已核验的部分暂未发现候选，正在继续读取新增结果。';
    if (status.status === 'idle') return '尚未运行选股，请在上方选择范围和周期后开始选股。';
    if (activeRun(status.status)) return '选股正在执行，已完成并通过核验的候选会自动显示。';
    if (status.source_current === false && !status.frozen_native_review) return resultVersionMessage(status);
    if (!candidateCount)
      return '本轮没有通过核验的买卖点结果，请查看下方数据诊断及选股截止时刻。';
    return '当前过滤条件下没有候选。可重置筛选、切换关注组，或查看上方选股进度及下方数据诊断。';
  }
  const activeRun = status => ['starting','running'].includes(status);
  const EVIDENCE = {formal_center:'正式中枢成立',complete_leave:'离开段完成',complete_first_return:'首次回试完成',core_boundary_held:'回试未返回中枢区间',trend_divergence:'趋势背驰证据',consolidation_divergence:'盘整背驰证据',lower_extreme_retested:'破前极值后的完成回试',first_rebound:'首次反弹',first_retest:'首次回试',lower_level_confirmation:'次级别确认',internal_first_retest:'首次回试位于一买比较组合内部；二买可用时刻不早于一买确认。'};
  Object.assign(EVIDENCE,{
    live_first_return:'首次回试正在形成',core_boundary_currently_held:'当前回试仍在中枢区间外',
    temporary_consolidation_divergence:'暂现盘整背驰，末段尚未完成',temporary_trend_divergence:'暂现趋势背驰，末段尚未完成',
    confirmed_first_class_parent:'对应一类点已确认',complete_adjacent_rebound:'相邻反弹段已完成',live_first_pullback:'首次回试正在形成',
    prior_extreme_currently_held:'当前仍守住前一极值',live_width_matched_departure_leg:'正在形成的离开组合与比较组合段数一致',
    live_complete_departure_leg:'完整离开走势的末段仍在形成',complete_entry_departure_legs:'按完整进入段与离开走势比较力度',
    formal_consolidation_prefix:'前序盘整结构已成立',single_center_consolidation:'单中枢盘整结构',formal_trend_prefix:'前序趋势结构已成立',two_separated_centers:'存在两个分离中枢',
    unfinished_segment_participates:'未完成线段参与观察',provisional_center_completion:'中枢结束条件尚待确认',
    strength_source_macd:'以 MACD 比较力度',macd_any_indicator_decay:'MACD 至少一项力度指标减弱',
    macd_histogram_peak_decay:'MACD 柱峰值减弱',macd_histogram_area_decay:'MACD 柱面积减弱',macd_dif_extreme_decay:'DIF 极值减弱',
    comparison_leg_width_1:'进入比较段由 1 段构成',comparison_leg_width_3:'进入比较段由 3 段构成',
    signal_leg_width_1:'离开比较段由 1 段构成',signal_leg_width_3:'离开比较走势由 3 段构成',
    parent_first_class_confirmed:'等待来源一类点确认'
  });
  Object.assign(EVIDENCE, {
    two_separated_same_grade_centers:'两个本周期中枢严格分离',
    completed_third_class_departure:'离开、首次返回及后续离开组合已完成',
    new_trend_price_extreme:'完整比较组合创出同向价格新极值',
    native_macd_histogram_area_decay:'本周期 MACD 同向柱面积缩小',
    native_center:'本周期中枢已确认', independent_leave:'独立离开段已确认',
    first_complete_return_held_core:'首次完整返回守住中枢边界',
    first_complete_retest_held:'首次完整回试守住一类点极值',
    first_retest_from_price_anchor:'从一类点真实极值检查第一次回试',
    user_declared_signal_policy:'按本周期已确认线段作为次级成员判定'
  });
  const INTERVALS = {'1m':'1','2m':'2','3m':'3','5m':'5','10m':'10','15m':'15','30m':'30',
    '60m':'60','120m':'120','180m':'180','240m':'240',d:'1D','2d':'2D','3d':'3D',w:'1W',m:'1M'};
  const CHART_COUNTS = {single:1,'horizontal-2':2,'vertical-2':2,'horizontal-7-3':2,three:3,four:4};
  const STRATEGY = '5m_with_1m_confirmation';
  const HIGHER_CONTEXT = {up_trend_forming:'上涨走势形成中',down_trend_forming:'下跌走势形成中',
    up_segment:'向上线段',down_segment:'向下线段',consolidation:'盘整',unknown:'方向未知'};
  const HIGHER_REASONS = {NO_CURRENT_30M_DIRECTION:'截止时刻没有当前方向证据',
    '30M_CUTOFF_NOT_ALIGNED':'30m 与 5m 截止时刻不一致',DATA_GAPS:'30m 行情缺失',
    STALE_DATA:'30m 行情未更新',SESSION_CALENDAR_UNAVAILABLE:'交易日历未覆盖',
    CALENDAR_COVERAGE_UNKNOWN:'交易日历待核验',INVALID_SESSION:'30m 时段待核验'};
  function higherContextText(context) {
    if (!context || context.category === 'unknown') {
      if (context?.reason === 'PENDING') return '30m 背景计算中';
      if (context?.reason === 'NOT_COMPUTED') return '30m 背景尚未计算';
      return `30m 方向未知${context?.reason ? ` · ${HIGHER_REASONS[context.reason] || context.reason}` : ''}`;
    }
    return `30m ${HIGHER_CONTEXT[context.category] || context.category} · 截止 ${date(context.source_closed_at)} · 证据 ${date(context.evidence_available_at)}`;
  }
  function higherContextDetail(context) {
    if (!context) return '30m 背景尚未计算';
    const summary=higherContextText(context);
    const trend=context.trend;
    if (!trend) return summary;
    const kind=trend.kind==='trend'?'趋势':trend.kind==='consolidation'?'盘整':'走势待判';
    const phase=trend.state==='forming'?'形成中':trend.state==='complete'?'已完成':'已锁定';
    return `${summary}；30m 走势类型记录：${kind} · ${phase} · ${trend.center_count} 个中枢，记录可用时间 ${date(trend.available_at)}`;
  }
  const isWaiting = row => ['approaching','formed','observed','entry_pending'].includes(row.stage);
  const pointStage = row => row.point?.status || row.stage;
  const MISSING = {terminal_unit_locked:'等待末段完成并锁定',unfinished_segment_lock:'等待未完成线段锁定',formal_center_confirmation:'等待中枢正式确认',lower_1m_confirmation:'等待对应区间内的 1m 买卖点确认'};
  const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
  function cutoffSummary(status) {
    const markets = {a:'A 股',us:'美股',hk:'港股',currency:'合约',currency_spot:'现货',fx:'外汇',ny_futures:'纽约期货',futures:'国内期货'};
    if (status.market_cutoffs) {
      return Object.entries(status.market_cutoffs).flatMap(([market,periods]) =>
        Object.entries(periods||{}).map(([frequency,closed]) => {
          const prefix=`${markets[market]||market} ${frequency}`;
          const check=status.market_freshness?.[market]?.[frequency];
          if(!Number.isFinite(closed))return `${prefix}：${check?.state==='pending'||activeRun(status.status)?'等待行情截止记录':'未取得有效截止时间'}`;
          const labels={cutoff_current:'截止时间已核对',stale:'已有更新的 K 线',unknown:`时效未核对${check?.message?'：'+check.message:''}`};
          return `${prefix} 截止 ${date(closed)}${labels[check?.state]?`（${labels[check.state]}）`:''}`;
        }));
    }
    return Object.entries(status.cutoffs||{}).map(([frequency,closed]) => `${frequency} 截止 ${date(closed)}`);
  }
  const price = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—';
  const knownRate = rate => rate !== null && rate !== undefined && rate !== '' && Number.isFinite(Number(rate));
  function quoteStale(quote) {
    const age=Date.now()-quote?.received_at;
    return !!quote?.stale || Number.isFinite(quote?.received_at)&&(age<0||age>=30000);
  }
  function quoteText(quote) {
    if (!quote) return '行情待更新';
    const rate = knownRate(quote.rate) ? `${Number(quote.rate)>0?'+':''}${price(quote.rate)}%` : '涨跌幅暂缺';
    const value = `${price(quote.price)} · ${rate}`;
    return quoteStale(quote)?`上次报价 ${value}（待更新）`:value;
  }
  function chartPeriods(row, frequency, layout, settings={}) {
    const focus = INTERVALS[frequency] ? frequency : '5m';
    const order = row.nested_confirmation ? ['5m','1m','30m','d'] : ['30m','5m','1m','d'];
    const defaults=[focus, ...order.filter(f => f !== focus)].slice(0,CHART_COUNTS[layout]||1);
    return defaults.map((period,index)=>index===0?focus:
      Object.keys(INTERVALS).find(f=>INTERVALS[f]===settings[`${row.market||'a'}_interval_${index+1}`])||period);
  }
  function chartUrl(row, frequency='5m', layout='single', periods=chartPeriods(row,frequency,layout)) {
    const count = CHART_COUNTS[layout] || 1;
    return '/?' + new URLSearchParams({market:row.market || 'a',code:row.code,layout:count===1?'single':layout,
      frequency:periods[0],intervals:periods.slice(0,count).map(f=>INTERVALS[f]).join(','),chart_sidebar:'collapsed',workspace_layout:'1'});
  }
  function evidenceUrl(row, source, frequency=row.frequency) {
    return '/screening/evidence?' + new URLSearchParams({source,code:row.code,frequency,point:row.point.point_id,...(row.market&&row.market!=='a'?{market:row.market}:{})});
  }
  function filterRows(rows, filters, reviewed={}, sectorNames={}) {
    const query = (filters.query || '').trim().toLowerCase();
    const visible=rows.filter(row => {
      const point = row.point || {};
      if (filters.market !== 'all' && row.market !== filters.market) return false;
      if (filters.origin === 'screening' && row.origin === 'watchlist') return false;
      if (filters.origin === 'watchlist' && row.origin !== 'watchlist') return false;
      if (filters.frequency !== 'all' && row.frequency !== filters.frequency) return false;
      if (filters.point !== 'all' && point.point_type !== filters.point) return false;
      const higher = row.higher_context?.category || 'unknown';
      if (filters.higher && filters.higher !== 'all') {
        if (filters.higher === 'up' && !['up_trend_forming','up_segment'].includes(higher)) return false;
        else if (filters.higher === 'down' && !['down_trend_forming','down_segment'].includes(higher)) return false;
        else if (!['up','down'].includes(filters.higher) && higher !== filters.higher) return false;
      }
      if (filters.sector && !(row.sectors || []).includes(filters.sector)) return false;
      if (filters.review === 'reviewed' && !reviewed[row.id]) return false;
      if (filters.review === 'pending' && (reviewed[row.id] || row.origin === 'watchlist')) return false;
      if (filters.stage === 'confirmed' && !['confirmed','triggered','executable'].includes(row.stage)) return false;
      if (filters.stage === 'forming' && !['approaching','formed','observed','entry_pending'].includes(row.stage)) return false;
      if (filters.stage === 'tracking' && !['monitoring','active'].includes(row.stage)) return false;
      const risk=row.operational_macd_risk?.risk_filter_state;
      if (filters.risk === 'not_triggered' && risk !== 'basic_zero_axis_risk_not_triggered') return false;
      if (filters.risk === 'avoid' && risk !== 'basic_avoid_on_selected_period') return false;
      if (filters.risk === 'pending' && !['pending_frozen_evidence','pending_indicator_evidence',
          'selected_period_zero_axis_boundary_pending'].includes(risk)) return false;
      if (row.origin !== 'watchlist') {
        const main=pointStage(row);
        if (filters.point_status === 'confirmed' && main !== 'confirmed') return false;
        if (filters.point_status === 'forming' && !['approaching','formed','observed'].includes(main)) return false;
        if (filters.lower_status && filters.lower_status !== 'all' && row.nested_confirmation?.state !== filters.lower_status) return false;
        if (filters.center_order === 'first' && ['3buy','3sell'].includes(point.point_type) && point.center_ordinal !== 1) return false;
        if (filters.age !== undefined && filters.age !== 'all' &&
            (!Number.isInteger(row.confirmation_age_sessions) || row.confirmation_age_sessions > Number(filters.age))) return false;
        if (filters.gain !== undefined && filters.gain !== 'all' &&
            (typeof row.gain_pct !== 'number' || row.gain_pct > Number(filters.gain))) return false;
      }
      const searchable = [row.code,row.name,row.market,...(row.sectors || []).map(id=>sectorNames[id])].join(' ').toLowerCase();
      return !query || searchable.includes(query);
    });
    if (filters.sort === 'distance') visible.sort((a,b) =>
      (typeof a.gain_pct === 'number'?Math.abs(a.gain_pct):Infinity) -
      (typeof b.gain_pct === 'number'?Math.abs(b.gain_pct):Infinity) ||
      (b.selection_available_at||b.point?.available_at||0) - (a.selection_available_at||a.point?.available_at||0));
    return visible;
  }
  function pageRows(rows, page=1, size=50) {
    const pages=Math.max(1,Math.ceil(rows.length/size));
    page=Math.max(1,Math.min(pages,page));
    const start=(page-1)*size,end=Math.min(start+size,rows.length);
    return {rows:rows.slice(start,end),page,pages,start,end,total:rows.length};
  }
  function boot() {
    const $ = id => document.getElementById(id);
    if (!$('dashboard-panel')) return;
    const periodButtons=Array.from(document.querySelectorAll('[data-frequency]'));
    const state = {data:null,serial:0,version:'',rows:[],selected:null,watch:[],quotes:new Map(),
      reviews:{latest:{},history:[]},drafts:new Map(),filters:{query:'',market:'all',origin:'screening',frequency:'all',point:'all',review:'all',stage:'all',risk:'all',sector:'',
        point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'},
      frequency:'5m',sectorExpanded:false,queuePage:1,chartKey:'',chartVisible:typeof window==='undefined'||typeof IntersectionObserver==='undefined',quoteRequests:new Map(),watchSerial:0,
      details:new Map(),detailPending:new Set(),detailSerial:0,
      watchGroup:'',watchLoaded:false,watchLoading:false,watchError:'',
      mutationPending:false,refreshPending:false,settingsInitialized:false,settingsEdited:false,
      reviewsLoadedSource:null,reviewSerial:0,reviewWriteEpoch:0,savingReview:false,
      stopChartSync:null,chartPeriods:[],fullChartPreviousList:null};
    try {
      state.workspaceSettings=JSON.parse(globalThis.AccountPreferences?.getItem('tv_chart')||'{}');
      if(CHART_COUNTS[state.workspaceSettings.chart_layout_type])$('chart-layout').value=state.workspaceSettings.chart_layout_type;
    }catch(_){state.workspaceSettings={};}
    function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(className)el.className=className;return el; }
    function renderQuote(element,quote) {
      if(!quote){element.textContent='行情待更新';return;}
      const rate=quote.rate,known=knownRate(rate),stale=quoteStale(quote);
      const change=node('span',known?`${Number(rate)>0?'+':''}${price(rate)}%`:'涨跌幅暂缺',
        stale?'quote-neutral':known&&Number(rate)>0?'quote-up':known&&Number(rate)<0?'quote-down':'quote-neutral');
      element.replaceChildren(node('span',`${stale?'上次报价 ':''}${price(quote.price)} · `),change);
      if(stale)element.append(node('small','（待更新）','quote-neutral'));
    }
    function message(text) {$('page-message').textContent=text;$('page-message').hidden=!text;$('page-message').classList.toggle('is-loading',text===RESULTS_LOADING);}
    async function api(url, body, form=false) {
      const headers = body === undefined ? {} : {'X-CSRFToken':document.querySelector('meta[name=csrf-token]').content};
      if (body !== undefined && !form) headers['Content-Type']='application/json';
      try {
        const response = await fetch(url,{credentials:'same-origin',cache:'no-store',headers,
          ...(body!==undefined ? {method:'POST',body:form?new URLSearchParams(body):JSON.stringify(body)} : {}),signal:AbortSignal.timeout(20000)});
        if (response.redirected || response.status===401) throw new Error('登录已失效，请在行情页登录后刷新。');
        const value=await response.json();
        if (!response.ok || value.ok===false) {
          const error=new Error(value.error?.message || value.error || value.msg || '读取失败，请重试。');
          error.retryAfterSeconds=value.error?.retry_after_seconds??value.retry_after_seconds;
          error.status=response.status;
          throw error;
        }
        return value;
      } catch (error) {
        if (error.name === 'TimeoutError') throw new Error(url==='/ticks'?'行情请求超时，稍后自动重试。':body !== undefined
          ? '请求响应超时，操作可能已受理，请刷新状态后再试。'
          : ['/screening/workbench','/screening/status'].includes(url) ? '读取选股数据超时，页面会自动重试。' : '请求超时，请稍后重试。');
        throw error;
      }
    }
    const version = s => [s.run_id,s.status,s.completed,s.cancel_requested,s.source_current,s.frozen_native_review,s.current_source_revision,s.result_reader_revision,s.cutoff_current,s.freshness_state,JSON.stringify(s.market_freshness||{}),s.evidence_version,s.selection_policy_version,s.higher_context_version,s.live_recheck_version].join(':');
    function pointLabel(p={}) {
      const label=POINTS[p.point_type]||'人工关注';
      if(p.evidence_scope==='native_center_signal')return `${label} · 本周期中枢信号`;
      if(p.point_type==='1buy'||p.point_type==='1sell')return `${label} · 本级趋势背驰`;
      if(p.point_type==='2buy'||p.point_type==='2sell')return `${label} · ${p.variant==='weak_divergence'?'破前极值后盘整背驰':p.variant==='lower_retest'?'破前极值后完成回试':p.variant==='strict'?'守前极值':'变体待核对'}`;
      return label;
    }
    function operationalRiskText(risk) {
      if(!risk)return null;
      const frequency=risk.declared_minimum_frequency||risk.physical_frequency||'所选周期';
      if(risk.risk_filter_state==='basic_avoid_on_selected_period')
        return `${frequency} MACD 黄白线均在零轴下：基础避险状态；证据获知 ${date(risk.known_at)}。正式买卖点身份独立。`;
      if(risk.risk_filter_state==='basic_zero_axis_risk_not_triggered')
        return `${frequency} MACD 黄白线均在零轴上：本根已收盘K线未触发零轴基础避险；仍须独立核对结构。`;
      if(risk.risk_filter_state==='selected_period_zero_axis_boundary_pending')
        return `${frequency} MACD 黄白线触轴或分居两侧：零轴避险状态待核，不据此改写正式点。`;
      return `${frequency} MACD 零轴风险证据待核；正式买卖点身份不因缺证改写。`;
    }
    function sectorNames() {return Object.fromEntries((state.data?.sectors||[]).map(s=>[s.id,s.name]));}
    function selectionKey(){return `screening-selection:${document.querySelector('main').dataset.user}:${state.data?.source}`;}
    function previousSelection(){try{return sessionStorage.getItem(selectionKey());}catch(_){return null;}}
    function watchRows() {
      const names=sectorNames();
      return state.watch.map(row=>({...row,id:`watch:${row.market}:${row.code}`,frequency:state.frequency,point:{},stage:'monitoring',origin:'watchlist',
        sectors:(state.data?.candidates.find(r=>r.code===row.code&&r.market===row.market)?.sectors||[]).filter(id=>names[id])}));
    }
    function visibleRows() {
      return filterRows([...(state.data?.candidates||[]),...watchRows()],state.filters,state.reviews.latest,sectorNames());
    }
    function renderSummary() {
      if (!state.data) return;
      const {status,candidates,sectors}=state.data;
      const initialLoading=status.results_loading&&!status.results_available;
      renderRunControls();
      const incompleteRun=status.status==='completed'&&status.total>0
        &&status.error_count/status.total>=.2;
      $('run-status').textContent=(STATES[status.status] || status.status)
        +(status.results_loading?' · 正在读取结果…':incompleteRun?' · 结果不完整':'');
      const settings=status.settings||{};
      const nativeSignals=!status.run_id || settings.alert_mode==='native_center_signals'
        || candidates.some(row=>row.point?.evidence_scope==='native_center_signal');
      $('run-detail').textContent=[status.total ? `已处理 ${status.completed||0} / ${status.total} 只` : '',
        status.results_through!==undefined?`已核验 ${status.results_through} 只${status.results_partial?'，候选持续更新':''}`:'',
        status.effective_workers?`并行 ${status.effective_workers} 进程`:settings.workers?`设定 ${settings.workers} 进程`:'',
        settings.strategy===STRATEGY?'5m 主信号 · 区间内 1m 结构确认':settings.frequencies?.map(f=>`${f} K线周期`).join(' / '),
        nativeSignals?'按本周期中枢买卖点筛选':'',settings.point_types?.map(p=>POINTS[p]||p).join(' / '),
        settings.minimum_operating_frequency?`MACD基础避险最低周期 ${settings.minimum_operating_frequency}`:'',
        settings.recent_sessions ? `最近 ${settings.recent_sessions} 个交易日确认` : '',
        status.cancel_requested&&activeRun(status.status)?'正在取消选股计算':''].filter(Boolean).join(' · ');
      $('symbol-count').textContent=initialLoading?'—':new Set(candidates.map(r=>r.market+':'+r.code)).size;
      $('signal-count').textContent=initialLoading?'—':candidates.length;
      const waiting=candidates.filter(isWaiting).length;
      const higher=state.data.higher_context||{};
      $('higher-context-status').textContent=higher.status==='disabled'?'递归 30m 走势背景已停用；请直接选择 30m 中枢提醒。':higher.status==='completed'?
        `30m 独立背景已完成 ${higher.completed}/${higher.total} 只；按本轮 5m 截止时刻核对。方向筛选只影响页面，不改变买卖点、监听或通知。`:
        higher.status==='running'||higher.status==='starting'?
        `30m 独立背景计算中 ${higher.completed||0}/${higher.total||0} 只；缺少的证据暂列“未知”。`:
        higher.status==='failed'?`30m 背景计算失败：${higher.error||'请查看任务日志'}`:
        status.source_current===false?'本轮为旧版选股结果；30m 独立背景将在新一轮选股完成后计算。':'30m 独立背景等待本轮选股完成。';
      $('stage-counts').textContent=initialLoading?'正在读取候选…':nativeSignals?
        `${status.frozen_native_review?'原截止时刻已确认买卖点':'正式买卖点提醒'} ${candidates.length-waiting} 条`:
        `筛选已通过 ${candidates.length-waiting} · 条件待齐 ${waiting}`;
      $('signal-breakdown').textContent=initialLoading?'正在读取候选…':nativeSignals?`本周期中枢信号 ${candidates.length-waiting} 条`:`筛选通过 ${candidates.length-waiting} · 待齐 ${waiting}`;
      $('sector-count').textContent=sectors.filter(s=>s.signal_count>0).length;
      $('review-count').textContent=Object.keys(state.reviews.latest).length;
      $('error-count').textContent=status.error_count ?? '—';
      $('point-counts').replaceChildren(...Object.entries(POINTS).map(([p,label])=>{
        const rows=candidates.filter(row=>row.point?.point_type===p);
        const confirmed=rows.filter(row=>pointStage(row)==='confirmed').length;
        return node('span',nativeSignals?`${label} ${rows.length} · 已确认 ${confirmed}`:`${label} ${rows.length} · 正式点已确认 ${confirmed}`);
      }));
      const third=candidates.filter(row=>['3buy','3sell'].includes(row.point?.point_type));
      const both=third.filter(row=>pointStage(row)==='confirmed'&&row.nested_confirmation?.state==='confirmed').length;
      const lowerWaiting=third.filter(row=>pointStage(row)==='confirmed'&&row.nested_confirmation?.state==='waiting').length;
      const entryWaiting=third.filter(row=>row.stage==='entry_pending').length;
      const directConfirmed=third.filter(row=>pointStage(row)==='confirmed'&&row.stage!=='entry_pending').length;
      const forming=third.filter(row=>pointStage(row)!=='confirmed').length;
      $('third-breakdown').textContent=initialLoading?'正在校验三类点证据…':nativeSignals
          ? `三类买卖点提醒 ${third.length} 条；按本周期中枢离返程序规则判定。`:
        (settings.strategy===STRATEGY||third.some(row=>row.nested_confirmation))
          ? `三类点线索 ${third.length} 条：5m 与 1m 均确认 ${both} · 5m 已确认、1m 待齐 ${lowerWaiting} · 5m 尚在形成 ${forming}。当前列表默认只看 5m 已确认，可展开候选筛选切换。`
          : `三类点线索 ${third.length} 条：正式点已确认且筛选条件齐 ${directConfirmed} · 正式三买已确认但入场证待核 ${entryWaiting} · 正式点尚在形成 ${forming}。`;
      $('diagnostics').textContent=[`任务：${state.data.source}`,`行情截止：${cutoffSummary(status).join(' / ')||'—'}`,
        `行情截止：${status.cutoff_current===true?'最新收盘时刻':status.cutoff_current===false?'历史或待更新':'待核对'}；历史行情修订未重新核对`,
        `代码版本：${status.source_current===true?'当前':status.source_current===false?'历史':'待核对'}`,
        `数据问题：${status.error_count??'—'} 个标的`,status.error||''].filter(Boolean).join('；');
      const coverage=status.universe_coverage,markets={SH:'沪市',SZ:'深市',BJ:'北交所'};
      $('coverage-detail').textContent=coverage?`实际股票池：${Object.entries(coverage.markets||coverage.exchanges).map(([e,n])=>`${markets[e]||e} ${n} 只`).join('，')}`+
        (['all_a','all_a_watchlist'].includes(settings.scope)&&coverage.absent_exchanges.length?`；本次股票池未覆盖：${coverage.absent_exchanges.map(e=>markets[e]||e).join('、')}`:''):'';
      const diagnostics=state.data.diagnostics||{},labels=diagnostics.reason_labels||{};
      $('diagnostic-reasons').textContent=Object.entries(diagnostics.rejection_counts||{}).map(([r,n])=>`${labels[r]||r}：${n}`).join('；');
      $('diagnostic-errors').replaceChildren(...(diagnostics.errors||[]).map(r=>node('li',`${r.code} ${r.frequency||''}：${(r.reasons||[]).map(k=>labels[k]||k).join('；')} ${r.error||''}${r.data_quality?.missing_bars?`；缺口 ${r.data_quality.missing_bars} 根，始于 ${date(r.data_quality.first_missing_at)}`:''}`)),
        ...(status.live_unverified_count?[node('li',`${status.live_unverified_count} 条候选尚未通过最新复核，暂不显示，核验通过后自动更新。`)]:[]),
        ...(status.excluded_requested_codes||[]).map(code=>node('li',`${code}：未进入本次股票池，请核对行情源是否支持及 ST / 退市过滤条件。`)));
    }
    function renderSectors() {
      const query=$('sector-search').value.trim().toLowerCase();
      const sectors=(state.data?.sectors||[]).filter(s=>s.name.toLowerCase().includes(query));
      $('sector-list').replaceChildren();
      $('sector-status').textContent=sectors.length ? `${state.data.sectors.length} 个板块；目录与结构记录日期 ${date(state.data.sector_catalog_at)}。候选数量随本次结果更新，结构强弱为历史记录。` : '没有匹配的板块目录。个股筛选与图表仍可使用。';
      const all=node('button',`全部板块 · ${state.data?.candidates.length||0} 条`,'sector-card'+(!state.filters.sector?' active':''));
      all.type='button';all.addEventListener('click',()=>{state.filters.sector='';renderSectors();renderQueue();});$('sector-list').append(all);
      for (const sector of sectors.slice(0,state.sectorExpanded||query?sectors.length:9)) {
        const b=node('button',undefined,'sector-card'+(state.filters.sector===sector.id?' active':''));b.type='button';
        b.append(node('strong',sector.name),node('small',`${sector.symbol_count} 只候选 · ${sector.signal_count} 条线索 / ${sector.member_count} 成员`),
          node('small',`原强弱名次 ${sector.historical_rank??'—'} · ${ {up:'向上',down:'向下',neutral:'中性'}[sector.historical_regime]||sector.historical_regime||'未知'}`));
        b.addEventListener('click',()=>{state.filters.sector=sector.id;renderSectors();renderQueue();});$('sector-list').append(b);
      }
      $('sector-more').textContent=state.sectorExpanded?'收起板块':'展开全部板块';
      $('selected-sector').textContent=sectorNames()[state.filters.sector]||'全部板块';
    }
    function renderQueue() {
      state.rows=visibleRows();
      const page=pageRows(state.rows,state.queuePage);
      state.queuePage=page.page;
      $('visible-count').textContent=`${state.rows.length} 条`;
      $('queue-page').textContent=page.total?`${page.page} / ${page.pages} 页 · ${page.start+1}–${page.end} / ${page.total} 条`:'0 条';
      $('queue-prev').disabled=page.page===1;
      $('queue-next').disabled=page.page===page.pages;
      $('signal-list').replaceChildren();
      const names=sectorNames();
      for (const row of page.rows) {
        const b=node('button',undefined,'signal-card'+(state.selected?.id===row.id?' active':''));b.type='button';b.dataset.id=row.id;
        const title=node('div',undefined,'row');title.append(node('strong',row.name||row.code),node('span',pointLabel(row.point),'badge'));
        const quote=state.quotes.get(`${row.market}:${row.code}`);
        b.append(title,node('small',`${row.code} · ${row.frequency} · ${row.stage==='entry_pending'?STAGES.entry_pending:STAGES[pointStage(row)]||pointStage(row)}`),
          node('small',row.origin==='watchlist'?'人工关注 · 点击查看图表':`拐点 ${date(row.point.anchor_at)}`),
          node('small',row.origin==='watchlist'?'':`${pointStage(row)==='confirmed'?'确认':'观测'} ${date(row.point.available_at)}`),
          node('small',(row.sectors||[]).map(id=>names[id]).filter(Boolean).slice(-2).join(' / ')||'板块暂缺'),
          node('small',quote?quoteText(quote):`截止收盘 ${price(row.latest_price)}`,'queue-quote'),
          node('small',state.reviews.latest[row.id]?'已保存人工复核':'','badge'));
        if(row.exit_plan){
          const exits=globalThis.PointExitInfo?.describe(row.exit_plan);
          if(exits)b.append(node('small',`止损 ${exits.stop}`,'queue-stop'),node('small',`止盈 ${exits.profit}`,'queue-exit'));
        }
        if(row.nested_confirmation)b.append(node('small',`${row.nested_confirmation.frequency||'1m'} 区间内结构确认：${row.nested_confirmation.state==='confirmed'?'已确认':'等待确认'}`));
        if(row.operational_macd_risk?.risk_filter_state==='basic_avoid_on_selected_period')
          b.append(node('small','已选最低周期 MACD 基础避险','badge'));
        if(row.origin!=='watchlist')b.append(node('small',higherContextText(row.higher_context)));
        if(row.latest_recheck_closed_at)b.append(node('small',`复核行情截止 ${date(row.latest_recheck_closed_at)}`));
        b.addEventListener('click',()=>{selectRow(row);renderQueue();});$('signal-list').append(b);
      }
      $('queue-empty').hidden=!!state.rows.length;
      $('queue-empty').textContent=emptyResultsMessage(state.data?.status,state.data?.candidates?.length||0);
      if (state.selected && !state.rows.some(r=>r.id===state.selected.id)) {
        clearSelection();
      }
      if (!state.selected && page.rows.length) selectRow(page.rows.find(r=>r.id===previousSelection())||page.rows[0]);
      else if(state.selected)selectRow(state.rows.find(r=>r.id===state.selected.id));
    }
    function list(id, values) {$(id).replaceChildren(...values.map(v=>node('li',v)));}
    function candidateDetailKey(row) {
      return [state.data?.source,row.candidate_evidence_version??state.data?.status.evidence_version,state.data?.status.result_reader_revision,row.selection_check_version,row.id].join(':');
    }
    function selectRow(row) {
      const detailKey=candidateDetailKey(row);
      if(row.detail_loaded===false&&row.origin!=='watchlist'){
        const cached=state.details.get(detailKey);
        if(cached)row={...cached,...row,point:cached.point,nested_confirmation:cached.nested_confirmation,detail_loaded:true};
        else{
          const changed=state.selected?.id!==row.id;
          state.selected=row;
          if(changed){state.frequency=row.frequency||'5m';$('chart-source').value='live';}
          $('selected-detail').hidden=false;$('selected-title').textContent=`${row.name||row.code} · ${row.code}`;
          $('selected-tags').textContent=`${row.frequency} · ${pointLabel(row.point)}`;
          list('evidence-established',[]);list('evidence-missing',[]);list('evidence-risk',[]);
          list('exit-price-detail',[]);list('exit-price-sources',[]);list('confirmation-exit-detail',[]);
          $('decision-title').textContent='正在读取候选依据';$('decision-detail').textContent='';
          $('anchor-price').textContent=price(row.point?.anchor_price);
          $('invalidation-price').textContent=price(row.point?.invalidation_price);
          $('close-price').textContent=price(row.latest_price);
          const preview=row.exit_plan&&globalThis.PointExitInfo?.describe(row.exit_plan);
          $('stop-loss-price').textContent=preview?.stop||'—';$('take-profit-price').textContent=preview?.profit||'—';
          $('live-quote').textContent=quoteText(state.quotes.get(`${row.market}:${row.code}`));
          $('raw-evidence').textContent='';
          if(changed)renderReview();
          $('review-fields').disabled=true;
          $('candidate-detail-state').textContent='正在读取所选候选的详细证据…';
          for(const card of $('signal-list').querySelectorAll('.signal-card'))card.classList.toggle('active',card.dataset.id===row.id);
          if(!state.detailPending.has(detailKey)){
            ++state.detailSerial;const source=state.data.source;
            state.detailPending.add(detailKey);
            void api('/screening/candidate?'+new URLSearchParams({source,id:row.id})).then(data=>{
              if(data.source!==source||data.candidate?.id!==row.id)throw new Error('候选详情与选中项不一致，请刷新结果。');
              const current=state.data?.candidates.find(candidate=>candidate.id===row.id);
              if(source!==state.data?.source||!current||detailKey!==candidateDetailKey(current))return;
              const sameEvidence=current.candidate_evidence_version!==undefined
                ?data.candidate.candidate_evidence_version===current.candidate_evidence_version
                :data.evidence_version===state.data.status.evidence_version;
              if(!sameEvidence||data.result_reader_revision!==state.data.status.result_reader_revision||data.candidate.selection_check_version!==current.selection_check_version)
                throw new Error('候选依据已更新，等待列表刷新后重新读取。');
              data.candidate={...current,...data.candidate};
              state.details.set(detailKey,data.candidate);
              if(state.details.size>100)state.details.delete(state.details.keys().next().value);
              if(state.selected?.id===row.id)selectRow(data.candidate);
            }).catch(error=>{if(source===state.data?.source&&state.selected?.id===row.id&&detailKey===candidateDetailKey(state.selected))$('candidate-detail-state').textContent=error.message;})
              .finally(()=>state.detailPending.delete(detailKey));
          }
          renderChart();
          return;
        }
      }
      $('candidate-detail-state').textContent='';
      $('review-fields').disabled=row.origin==='watchlist';
      const changed=state.selected?.id!==row.id;
      state.selected=row;
      try{sessionStorage.setItem(selectionKey(),row.id);}catch(_){/* Session persistence is optional. */}
      for(const card of $('signal-list').querySelectorAll('.signal-card'))card.classList.toggle('active',card.dataset.id===row.id);
      if(changed){state.frequency=row.frequency||'5m';$('chart-source').value='live';}
      const p=row.point||{};
      const nested=row.nested_confirmation;
      const missing=row.selection_missing_conditions||p.missing_conditions||[];
      const higherDetail=row.origin==='watchlist'?'':higherContextDetail(row.higher_context);
      $('selected-detail').hidden=false;$('selected-title').textContent=`${row.name||row.code} · ${row.code}`;
      $('selected-tags').textContent=[row.frequency,pointLabel(p),row.stage==='entry_pending'?STAGES.entry_pending:STAGES[pointStage(row)]||pointStage(row)].filter(Boolean).join(' · ');
      if(nested)$('selected-tags').textContent+=` · ${nested.frequency||'1m'} 区间内结构确认${nested.state==='confirmed'?'已确认':'等待确认'}`;
      const periodFreshness=state.data.status.market_freshness?.[row.market]?.[row.frequency];
      const current=state.data.status.source_current===true&&(periodFreshness
        ?periodFreshness.state==='cutoff_current':state.data.status.cutoff_current===true);
      $('decision-title').textContent=row.origin==='watchlist'?'人工关注 · 查看当前结构':row.stage==='entry_pending'?'正式第三买已确认 · 次次级入场证待核':isWaiting(row)?(pointStage(row)==='confirmed'?'5m 买卖点已确认 · 区间内 1m 结构确认待齐':'形成与等待 · 5m 买点尚未确认'):current?(nested?'5m 买卖点与区间内 1m 结构确认通过':'本次筛选通过 · 待人工判断'):'本次结果待更新';
      if(row.operational_macd_risk?.risk_filter_state==='basic_avoid_on_selected_period')
        $('decision-title').textContent+=' · MACD基础避险';
      $('decision-detail').textContent=row.origin==='watchlist'?'关注组不代表买卖点确认。':
        `拐点 ${date(p.anchor_at)}；${pointStage(row)==='confirmed'?'确认':'观测'} ${date(p.available_at)}。${!isWaiting(row)&&row.confirmation_delay_sessions!==undefined?`确认间隔 ${row.confirmation_delay_sessions} 个交易日。`:''}`+
        (nested?` 5m ${p.status==='confirmed'?'已确认':'待确认'}；1m ${nested.state==='confirmed'?'已确认':'等待确认'}。`:'')+
        (isWaiting(row)&&row.stage!=='entry_pending'?` 待满足：${missing.map(c=>MISSING[c]||c).join('；')}。`:'')+
        (row.stage==='entry_pending'?' 这条正式第三买尚未取得当次筛选所需的次次级入场证，不能列为已选入场候选。':'');
      if(row.confirmation_segment?.state==='in_progress')$('decision-detail').textContent+=` 后继${{up:'向上',down:'向下'}[row.confirmation_segment.segment?.direction]||''}线段进行中，尚未完成。`;
      if(row.latest_recheck_closed_at)$('decision-detail').textContent+=` 最近有效性复核：行情截止 ${date(row.latest_recheck_closed_at)}，检查完成 ${date(row.latest_recheck_at)}。`;
      if(higherDetail)$('decision-detail').textContent+=` ${higherDetail}。`;
      $('anchor-price').textContent=price(p.anchor_price);$('invalidation-price').textContent=price(p.invalidation_price);$('close-price').textContent=price(row.latest_price);
      const exits=globalThis.PointExitInfo?.describe(row.exit_plan,state.data.point_exit_sources)
        || {stop:'依据不足',profit:'待结构证据',lines:[],sources:[]};
      $('stop-loss-price').textContent=exits.stop;$('take-profit-price').textContent=exits.profit;
      list('exit-price-detail',exits.lines);
      list('exit-price-sources',exits.sources);
      const lowerExits=globalThis.PointExitInfo?.describe(row.confirmation_exit_plan,state.data.point_exit_sources);
      list('confirmation-exit-detail',nested?['1m 确认点独立参考：'+(lowerExits?`止损 ${lowerExits.stop}；止盈 / 退出 ${lowerExits.profit}`:'等待 1m 结构证据。'),
        '1m 用于入场确认；5m 退出观察仍按 5m 级别判断。',...(lowerExits?.lines||[])]:[]);
      $('live-quote').textContent=quoteText(state.quotes.get(`${row.market}:${row.code}`));
      const established=(p.evidence_codes||[]).filter(c=>c!=='unified_strict_signal_engine').map(c=>EVIDENCE[c]||c);
      if(p.center_zd_price!==undefined&&p.center_zd_price!==null)established.unshift(`中枢区间 [${price(p.center_zd_price)}, ${price(p.center_zg_price)}]${p.center_ordinal?` · ${p.side==='sell'?'向下':'向上'}序列第 ${p.center_ordinal} 个中枢`:''}`);
      if(row.audit?.cold_rebuild)established.push('独立重算通过');
      if(row.audit?.confirmation_replay)established.push('确认时刻回放通过');
      if(row.audit?.confirmation_boundary)established.push('确认前一根 K 线边界核对通过');
      if(p.price_anchor_unit_id&&p.price_anchor_unit_id!==p.anchor_unit_id)established.push('价格拐点取自完整比较组合的实际极值；确认仍需等待组合完成。');
      if(nested){
        established.push(`1m 检查区间：${date(nested.interval.start_after)} 至 ${date(nested.interval.end_at)}。`);
        if(nested.point)established.push(`对应 1m ${POINTS[nested.point.point_type]}：拐点 ${date(nested.point.anchor_at)}，${nested.state==='confirmed'?'确认':'观测'} ${date(nested.point.available_at)}。`);
        if(nested.audit?.confirmation_replay)established.push('1m 确认时刻回放通过');
      }
      list('evidence-established',established.length?established:['请结合图表观察当前中枢和买卖点。']);
      list('evidence-missing',[
        ...missing.map(c=>MISSING[c]||c),...(row.decision_reasons||[]).map(c=>state.data?.diagnostics?.reason_labels?.[c]||c),
        ...(row.observation_validation==='legacy_not_rechecked'?['本条来自本轮保留的未确认记录，尚未按新检查重新选股；请结合当前图表观察。']:[]),
        '人工核对中枢区间、背驰依据与组合边界；程序复算通过不等于人工规则已经确认。',
        p.evidence_scope==='native_center_signal'?`${row.frequency} 独立按本周期中枢信号判定；图中其他周期仅用于对照。`:
          nested?'日线和 30m 用于背景观察；入选依据为 5m 主信号及其对应区间内的 1m 结构确认。':'各物理周期独立核对。']);
      list('evidence-risk',row.origin==='watchlist'?['以图中实际结构确认风险边界。']:[
        `结构失效边界 ${price(p.invalidation_price)}；原拐点 ${price(p.anchor_price)}。`,
        ...(operationalRiskText(row.operational_macd_risk)?[operationalRiskText(row.operational_macd_risk)]:[]),
        '本次任务只评价截止时刻之前的 K 线；新行情出现后需重新确认有效性。',
        ...(p.evidence_scope==='native_center_signal'?['后续同周期反向点更晚确认后，本条当前提醒结束；历史图表标记仍保留。']:[]),
        p.point_type==='3buy'?'三买回试跌破中枢上沿才失效；恰好触及边界仍符合本程序规则。':p.point_type==='3sell'?'三卖回抽升破中枢下沿才失效；恰好触及边界仍符合本程序规则。':'继续核对后续反向结构与背驰。']);
      $('raw-evidence').textContent=JSON.stringify(row,null,2);
      if(changed)renderReview();
      renderChart();
    }
    function syncPeriodControls() {
      const primary=$('chart-source').value==='live'&&$('chart-layout').value!=='single';
      periodButtons.forEach(b=>{
        const active=b.dataset.frequency===state.frequency;
        b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));
      });
      $('chart-frequency').textContent=`${primary?'首图周期':'图表周期'}：${{d:'日线',w:'周线',m:'月线'}[state.frequency]||state.frequency}`;
    }
    function stopChartSync() {
      if(state.stopChartSync)state.stopChartSync();
      state.stopChartSync=null;
    }
    function watchChartInterval() {
      stopChartSync();
      if(!state.selected||$('chart-source').value!=='live')return;
      const frame=$('chart-frame'),doc=frame.contentDocument;
      let stopped=false,timer=null,tries=0;
      const subscriptions=[];
      const current=()=>!stopped&&frame.contentDocument===doc&&state.selected&&$('chart-source').value==='live';
      state.stopChartSync=()=>{
        stopped=true;
        if(timer!==null)clearTimeout(timer);
        for(const [subscription,changed] of subscriptions){
          try{subscription.unsubscribe(null,changed);}catch(_){/* The previous iframe may already be disposed. */}
        }
      };
      function attach() {
        if(!current())return;
        const widgets=Array.from(frame.contentWindow?.chart_widgets||[]);
        // iframe load can precede the asynchronous chart bootstrap. Once the
        // widget exists, onChartReady owns readiness; no recurring chart poll.
        if(widgets.length!==(CHART_COUNTS[$('chart-layout').value]||1)||widgets.some(w=>!w?.onChartReady)){
          if(++tries<240)timer=setTimeout(attach,250);return;
        }
        const charts=widgets.map(()=>null);
        const changed=()=>{
          if(!current()||!charts[0]||widgets.some((w,i)=>frame.contentWindow.chart_widgets[i]!==w))return;
          const periods=charts.map((chart,i)=>{
            if(!chart)return state.chartPeriods[i];
            const actual=String(chart.resolution()).toUpperCase();
            const interval={D:'1D',W:'1W',M:'1M'}[actual]||actual;
            return Object.keys(INTERVALS).find(f=>INTERVALS[f]===interval);
          });
          if(periods.some(f=>!f))return;
          state.chartPeriods=periods;state.frequency=periods[0];
          state.chartKey=[state.selected.market,state.selected.code,state.frequency,$('chart-layout').value].join(':');
          syncPeriodControls();
          $('open-chart').href=chartUrl(state.selected,state.frequency,$('chart-layout').value,periods);
        };
        widgets.forEach((widget,i)=>widget.onChartReady(()=>{
          if(!current()||frame.contentWindow.chart_widgets[i]!==widget)return;
          charts[i]=widget.activeChart();
          const subscription=charts[i].onIntervalChanged();
          subscriptions.push([subscription,changed]);subscription.subscribe(null,changed);changed();
        }));
      }
      attach();
    }
    function renderChart(force=false) {
      const row=state.selected;if(!row)return;
      const evidenceAvailable=row.origin!=='watchlist'&&row.evidence_chart_available!==false;
      if(!evidenceAvailable)$('chart-source').value='live';
      const frozen=evidenceAvailable&&$('chart-source').value==='evidence';
      $('chart-source').disabled=!evidenceAvailable;
      $('chart-layout').disabled=frozen;
      $('chart-stage').dataset.chartLayout=frozen?'single':$('chart-layout').value;
      const evidencePeriods=row.nested_confirmation?['5m','1m']:[row.frequency];
      if(frozen&&!evidencePeriods.includes(state.frequency))state.frequency=row.frequency;
      document.querySelectorAll('[data-frequency]').forEach(b=>{b.disabled=frozen&&!evidencePeriods.includes(b.dataset.frequency);});
      $('chart-context').textContent=frozen?'与缠论结构解盘共用图表；当前显示选股截止时保存的行情与结构，不接入后续行情。':'与缠论结构解盘共用图表及显示设置，按独立周期更新。当前结构可能与选股截止时刻不同。'+(!evidenceAvailable&&row.origin!=='watchlist'?' 本条旧记录未保存完整图表证据，原始候选依据见下方。':'');
      if(frozen){
        state.chartPeriods=[state.frequency];syncPeriodControls();
        const url=evidenceUrl(row,state.data.source,state.frequency),key='evidence:'+state.data.source+':'+row.id+':'+state.frequency;
        $('open-chart').href=url;$('open-chart').textContent='新窗口查看本次证据图';
        if(!state.chartVisible){$('chart-status').textContent='滚动到图表时加载';return;}
        if(force||state.chartKey!==key){stopChartSync();state.chartKey=key;$('chart-frame').src=url;$('chart-status').textContent='正在读取本次保存的证据';}
        return;
      }
      const layout=$('chart-layout').value,key=[row.market,row.code,state.frequency,layout].join(':');
      const periods=state.chartKey===key&&state.chartPeriods.length===(CHART_COUNTS[layout]||1)
        ?state.chartPeriods:chartPeriods(row,state.frequency,layout,state.workspaceSettings);
      state.chartPeriods=periods;syncPeriodControls();
      const url=chartUrl(row,state.frequency,layout,periods);
      $('open-chart').href=url;
      $('open-chart').textContent='完整图表 ↗';
      if(!state.chartVisible){$('chart-status').textContent='滚动到图表时加载';return;}
      if(!force&&state.chartKey===key)return;
      const previous=state.chartKey.split(':'),frame=$('chart-frame');
      // Reuse the chart's existing symbol transition and drawing isolation.
      if(!force&&previous[3]===layout&&previous[0]===row.market){
        try {
          const host=frame.contentWindow, widgets=host.chart_widgets;
          const count=CHART_COUNTS[layout];
          if(widgets?.length===count&&typeof host.change_chart_ticker==='function'){
            const charts=widgets.map(w=>w.activeChart());
            if(charts.every(c=>typeof c.setResolution==='function')){
              host.change_chart_ticker(row.market,row.code);
              state.chartKey=key;
              charts.forEach((c,i)=>c.setResolution(INTERVALS[periods[i]]));
              $('chart-status').textContent='已向图表发送切换请求';return;
            }
          }
        }catch(_){/* Initial load or an incomplete chart falls back to its exact URL. */}
      }
      stopChartSync();state.chartKey=key;frame.src=url;$('chart-status').textContent='正在加载图表，结构进度见图内提示';
    }
    function reviewFormValue() {
      return {judgements:Object.fromEntries(Array.from($('review-form').querySelectorAll('select[name]'),e=>[e.name,e.value])),notes:$('review-notes').value};
    }
    function draftKey(){return `${state.data?.source}:${state.selected?.id}`;}
    function renderReview() {
      if(!state.selected)return;
      const id=state.selected.id, stored=state.reviews.latest[id],draft=state.drafts.get(draftKey());
      const values=draft||stored;
      $('review-fields').disabled=state.selected.origin==='watchlist'||state.selected.detail_loaded===false;
      for(const select of $('review-form').querySelectorAll('select[name]'))select.value=values?.judgements?.[select.name]|| (select.name==='disposition'?'WATCH':'UNCERTAIN');
      $('review-notes').value=values?.notes||'';
      $('review-state').textContent=stored?`最近保存 ${date(stored.saved_at)}`:'待人工复核';
      $('review-message').textContent=state.selected.origin==='watchlist'?'关注标的需先产生正式候选，才能保存该候选的复核记录。':draft?'未保存的草稿已保留':'';
      const history=state.reviews.history.filter(r=>r.candidate_id===id);
      $('review-history-count').textContent=history.length;
      $('review-history').replaceChildren(...history.map(r=>{const a=node('article');a.append(node('strong',date(r.saved_at)),node('p',r.notes||'未填写笔记'),node('small',`处置：${{WATCH:'继续观察',REJECT:'拒绝',NEEDS_MORE_DATA:'需要更多数据'}[r.judgements.disposition]}`));return a;}));
    }
    function clearSelection() {
      state.detailSerial++;
      stopChartSync();
      state.selected=null;$('selected-detail').hidden=true;$('selected-title').textContent='当前筛选未选中标的';
      $('selected-tags').textContent='';
      $('chart-frame').src='about:blank';state.chartKey='';state.chartPeriods=[];$('open-chart').href='/';
    }
    function showResults(data) {
      const changed=state.data?.source!==data.source;
      state.data=data;
      if(changed){clearSelection();state.details.clear();state.queuePage=1;state.reviews={latest:{},history:[]};state.reviewsLoadedSource=null;state.reviewSerial++;state.filters.sector='';}
      renderSummary();renderSectors();renderQueue();
      void refreshQuotes();
    }
    function renderRunControls() {
      const status=state.data?.status,active=activeRun(status?.status);
      $('start').disabled=!status||active||state.mutationPending;
      $('start').textContent=status?.run_id?'重新选股':'开始选股';
      $('cancel').hidden=!active;$('cancel').disabled=!active||status?.cancel_requested||state.mutationPending;
      $('cancel').textContent=status?.cancel_requested?'正在取消…':'取消选股';
      $('screen-fields').disabled=state.mutationPending;$('refresh').disabled=state.mutationPending;
      $('run-progress').hidden=!active;$('run-progress').max=status?.total||1;$('run-progress').value=status?.completed||0;
      if(!state.settingsInitialized&&status){
        state.settingsInitialized=true;
        const s=status.settings;
        if(s&&!state.settingsEdited){
          $('scope').value=s.scope;$('codes').value=(s.codes||[]).join(', ');
          $('screen-strategy').value='direct_structural';
          for(const option of $('screen-frequency').options)
            option.selected=(s.frequencies||['5m']).includes(option.value);
          $('screen-grade').value='0';
          $('screen-risk-period').value=s.minimum_operating_frequency||'';
          if(s.strategy===STRATEGY||s.strategy==='direct_structural')
            for(const input of document.querySelectorAll('#screen-form [name=point]'))input.checked=s.point_types.includes(input.value);
          $('recent').value=s.recent_sessions;$('anchor').value=s.max_anchor_sessions;
          $('workers').value=status.source_current===false?12:(s.workers??12);
          $('max-gain').value=s.max_anchor_gain_pct??10;$('exclude-st').checked=s.exclude_st!==false;
          $('weak-second').checked=s.include_weak_second===true;
          updateScope();updateScreenMode();
        }
      }
    }
    function updateScope() {$('codes-label').hidden=$('scope').value!=='codes';$('codes').required=$('scope').value==='codes';}
    function selectedScreenFrequencies() {
      const control=$('screen-frequency');
      return control.selectedOptions
        ? Array.from(control.selectedOptions,option=>option.value)
        : [control.value].filter(Boolean);
    }
    function updateScreenMode() {
      const direct=$('screen-strategy').value==='direct_structural';
      $('direct-frequency-label').hidden=!direct;$('direct-grade-label').hidden=true;
      $('screen-frequency').disabled=!direct;$('screen-grade').disabled=!direct;
      $('nested-period-note').hidden=direct;$('direct-period-note').hidden=!direct;
      $('nested-explanation').hidden=direct;$('direct-explanation').hidden=!direct;
      const allowed=direct?selectedScreenFrequencies():['1m','5m'];
      for(const option of $('screen-risk-period').options)
        option.disabled=!!option.value&&!allowed.includes(option.value);
      if($('screen-risk-period').value&&!allowed.includes($('screen-risk-period').value))
        $('screen-risk-period').value='';
    }
    function screeningSettings() {
      const scope=$('scope').value;
      const direct=$('screen-strategy').value==='direct_structural';
      return {scope,codes:scope==='codes'?$('codes').value.toUpperCase().split(/[\s,，;；]+/).filter(Boolean):[],
        strategy:direct?'direct_structural':STRATEGY,
        frequencies:direct?selectedScreenFrequencies():['5m'],
        structural_levels:[0],
        point_types:Array.from(document.querySelectorAll('#screen-form [name=point]:checked'),i=>i.value),
        recent_sessions:Number($('recent').value),max_anchor_sessions:Number($('anchor').value),workers:Number($('workers').value),
        max_anchor_gain_pct:Number($('max-gain').value),exclude_st:$('exclude-st').checked,
        include_weak_second:false};
    }
    async function changeRun(action) {
      const current=state.data?.status;
      if(state.mutationPending||!current)return;
      if(action==='start'?activeRun(current.status):(!activeRun(current.status)||current.cancel_requested))return;
      state.mutationPending=true;++state.serial;renderRunControls();message('');
      let changed=false;
      try {
        const status=await api('/screening/'+action,action==='start'?screeningSettings():{});
        const source=status.run_id||'idle';
        showResults(action==='cancel'&&source===state.data.source?{...state.data,status}:
          {source,status,candidates:[],sectors:[],point_counts:{}});
        state.version='';changed=true;
      }catch(error){message(error.message);}
      finally{state.mutationPending=false;renderRunControls();}
      if(changed)await refresh(true);
    }
    async function loadReviews(source, serial) {
      if(state.savingReview)return;
      const request=++state.reviewSerial,writeEpoch=state.reviewWriteEpoch;
      const reviews=await api('/screening/reviews?'+new URLSearchParams({source}));
      if(serial!==state.serial||request!==state.reviewSerial||source!==state.data?.source||writeEpoch!==state.reviewWriteEpoch)return;
      state.reviews=reviews;state.reviewsLoadedSource=source;
      renderSummary();renderQueue();renderReview();
    }
    async function refresh(force=false) {
      if(state.mutationPending||state.refreshPending)return;
      state.refreshPending=true;
      const serial=++state.serial;
      let newerProofStatus=null;
      try {
        if(!force) {
          const status=await api('/screening/status');
          if(serial!==state.serial)return;
          if(state.data?.source===(status.run_id||'idle')&&[
            'source_current','current_source_revision','result_reader_revision','live_recheck_version'
          ].some(key=>status[key]!==state.data.status[key]))newerProofStatus=status;
          if(version(status)===state.version){
            if(state.reviewsLoadedSource!==state.data?.source)await loadReviews(state.data.source,serial);
            if(serial===state.serial)message('');
            return;
          }
          if(state.data&&state.data.source!==(status.run_id||'idle')) {
            // Once a newer run is known, discard the old display even if the
            // new result request is slow or fails. A failed read must retry.
            showResults({source:status.run_id||'idle',status,candidates:[],sectors:[],point_counts:{}});
          }
        }
        const data=await api('/screening/workbench?compact=1');
        if(serial!==state.serial)return;
        showResults(data);state.version=data.status.results_loading?'':version(data.status);
        message(data.status.results_loading?(data.status.results_available?`已展示 ${data.status.results_through} 只标的中通过核验的候选，正在读取新增结果。`:RESULTS_LOADING):'');
        if(data.status.results_loading&&!data.status.results_available)return;
        state.reviewsLoadedSource=null;
        await loadReviews(data.source,serial);
      }catch(error){
        if(serial===state.serial){
          if(newerProofStatus){
            // The status read has already superseded the displayed proof.
            // A failed result read cannot keep those rows actionable. Preserve
            // reviews/drafts and retry the verified list on the next poll.
            showResults({source:newerProofStatus.run_id||'idle',status:{...newerProofStatus,
              results_loading:true,results_available:false},candidates:[],sectors:[],point_counts:{}});
            state.version='';
          }
          message(error.message);
        }
      }
      finally{state.refreshPending=false;}
    }
    function renderWatch() {
      const marketNames={a:'A 股',us:'美股',hk:'港股',futures:'国内期货',ny_futures:'纽约期货',fx:'外汇',currency:'数字货币合约',currency_spot:'数字货币现货'};
      for(const market of new Set(state.watch.map(row=>row.market))){
        if(!Array.from($('market-filter').options).some(o=>o.value===market)){
          const option=node('option',marketNames[market]||market);option.value=market;$('market-filter').append(option);
        }
      }
      const group=state.watchGroup;
      $('watch-count').textContent=!group?(state.watchError?'分组读取失败':'暂无分组'):state.watchLoading?`${group} · 正在读取…`:
        state.watchError?`${group} · 读取失败${state.watchLoaded?`（上次 ${state.watch.length} 个标的）`:''}`:
        `${group} · ${state.watch.length} 个标的`;
      const counts=new Map();
      for(const row of state.watch)counts.set(row.market,(counts.get(row.market)||0)+1);
      const order=Object.keys(marketNames);
      $('watch-market-counts').textContent=state.watchLoaded?[...counts].sort(([a],[b])=>{
        const rank=market=>order.includes(market)?order.indexOf(market):order.length;
        return rank(a)-rank(b)||String(a).localeCompare(String(b));
      }).map(([market,count])=>`${marketNames[market]||market||'未标注市场'} ${count}`).join(' · '):'';
      $('watch-list').replaceChildren(...state.watch.map(row=>{
        const button=node('button');button.type='button';const title=node('span',row.name||row.code);title.append(node('small',`${marketNames[row.market]||row.market||'未标注市场'} · ${row.code}`));
        const quote=node('span',undefined,'watch-price');renderQuote(quote,state.quotes.get(`${row.market}:${row.code}`));
        button.append(title,quote);
        button.addEventListener('click',()=>{state.filters.origin='all';$('origin-filter').value='all';state.filters.market='all';$('market-filter').value='all';state.filters.point='all';state.filters.sector='';state.filters.query='';$('signal-search').value='';state.filters.frequency='all';$('frequency-filter').value='all';state.filters.stage='all';$('stage-filter').value='all';state.filters.review='all';$('review-filter').value='all';syncPoints();
          selectRow(watchRows().find(r=>r.id===`watch:${row.market}:${row.code}`));renderQueue();renderSectors();});return button;
      }));
    }
    async function refreshWatch() {
      const serial=++state.watchSerial;
      const group=$('watch-group').value;
      if(state.watchGroup!==group){state.watch=[];state.watchLoaded=false;}
      state.watchGroup=group;state.watchError='';state.watchLoading=!!group;
      $('watch-status').textContent=group?'正在读取当前分组…':'暂无可用自选分组，可在分组管理中添加。';
      $('watch-quote-status').textContent='';renderWatch();if(state.data)renderQueue();
      if(!group)return;
      try{
        const data=await api('/get_zixuan_stocks/a/'+encodeURIComponent(group));
        if(serial!==state.watchSerial)return;
        if(!Array.isArray(data.data))throw new Error('分组返回数据无效，请重试。');
        state.watch=data.data;state.watchLoaded=true;state.watchLoading=false;
        $('watch-status').textContent=state.watch.length?'统计当前分组的全部标的，点击可查看图表。':'当前分组暂无标的，可在自选分组管理中添加。';renderWatch();if(state.data)renderQueue();void refreshQuotes();
      }catch(error){if(serial===state.watchSerial){state.watchLoading=false;state.watchError=error.message;$('watch-status').textContent=error.message;renderWatch();}}
    }
    async function initWatch() {
      try{const groups=await api('/get_zixuan_groups/a');$('watch-group').replaceChildren(...groups.map(g=>{const o=node('option',g.name);o.value=g.name;return o;}));
        if(groups.some(g=>g.name==='我的关注'))$('watch-group').value='我的关注';await refreshWatch();
      }catch(error){state.watchLoading=false;state.watchError=error.message;renderWatch();$('watch-status').textContent=error.message;}
    }
    async function refreshQuotes() {
      if(!$('auto-quotes').checked||document.hidden)return;
      let expired=false;
      for(const [key,quote] of state.quotes){
        if(!quote.stale&&quoteStale(quote)){state.quotes.set(key,{...quote,stale:true});expired=true;}
      }
      if(expired)renderQuoteDisplays();
      const targets=[...(state.selected?[state.selected]:[]),...pageRows(state.rows,state.queuePage).rows.slice(0,15),...state.watch];
      const groups=new Map();for(const row of targets){if(!groups.has(row.market))groups.set(row.market,new Set());groups.get(row.market).add(row.code);}
      for(const [market,request] of state.quoteRequests){
        if(!groups.has(market)){clearTimeout(request.timer);request.timer=null;request.wanted=null;}
      }
      const jobs=[];
      for(const [market,codes] of groups){
        const signature=JSON.stringify([...codes].sort());
        let request=state.quoteRequests.get(market);
        if(!request){request={inflight:false,nextAt:0,state:'idle',timer:null,batches:[],cooldownUntil:0};state.quoteRequests.set(market,request);}
        request.wanted=signature;
        if(request.inflight)continue;
        if(request.signature!==signature){
          const targets=[...codes];request.batches=[];request.signature=signature;
          for(let offset=0;offset<targets.length;offset+=80)request.batches.push({codes:targets.slice(offset,offset+80),nextAt:0,state:'idle'});
        }
        if(Date.now()<request.cooldownUntil)continue;
        const due=request.batches.filter(batch=>Date.now()>=batch.nextAt);
        if(!due.length)continue;
        clearTimeout(request.timer);request.timer=null;
        request.inflight=true;
        jobs.push((async()=>{
          try{
            for(const batch of due){
              if(request.wanted!==signature||!$('auto-quotes').checked||document.hidden)break;
              try{
                const data=await api('/ticks',{market,codes:JSON.stringify(batch.codes)},true);
                if(data.quote_state==='deferred'){
                  batch.state='waiting';batch.nextAt=Date.now()+quoteWait(data.retry_after_seconds,5);
                  request.cooldownUntil=batch.nextAt;break;
                }
                const returned=new Set();
                for(const tick of data.ticks||[]){
                  returned.add(tick.code);state.quotes.set(`${market}:${tick.code}`,{...tick,received_at:Date.now()});
                }
                const missing=batch.codes.filter(code=>!returned.has(code));
                markQuoteReferences(market,missing);
                batch.state=missing.length?'partial':'ready';batch.nextAt=Date.now()+15000;renderQuoteDisplays();
              }catch(error){
                batch.state='error';batch.nextAt=Date.now()+quoteWait(error.retryAfterSeconds,15);
                if(markQuoteReferences(market,batch.codes))renderQuoteDisplays();
                // Honor explicit provider cooldowns, while an isolated batch
                // failure must not prevent the remaining stocks from updating.
                if(error.status===429||Number(error.retryAfterSeconds)>0){request.cooldownUntil=batch.nextAt;break;}
              }
            }
          }finally{
            request.inflight=false;
            request.state=request.batches.some(b=>b.state==='error'||b.state==='partial')?'error':request.batches.some(b=>b.state==='waiting')?'waiting':'ready';
            const next=Math.max(request.cooldownUntil,Math.min(...request.batches.map(b=>b.nextAt)));
            scheduleQuote(request,(next-Date.now())/1000,1);
            renderQuoteStatus();
            if(request.wanted&&request.wanted!==signature&&Date.now()>=request.cooldownUntil)void refreshQuotes();
          }
        })());
      }
      renderQuoteStatus();
      await Promise.allSettled(jobs);
    }
    function markQuoteReferences(market,codes){
      let changed=false;
      for(const code of codes){
        const key=`${market}:${code}`,quote=state.quotes.get(key);
        if(quote&&!quote.stale){state.quotes.set(key,{...quote,stale:true});changed=true;}
      }
      return changed;
    }
    function quoteWait(seconds,fallback){
      const wait=Number(seconds);return (Number.isFinite(wait)&&wait>0?Math.max(1,wait):fallback)*1000;
    }
    function scheduleQuote(request,seconds,fallback) {
      const delay=quoteWait(seconds,fallback);
      request.nextAt=Date.now()+delay;
      clearTimeout(request.timer);
      if(request.wanted)request.timer=setTimeout(()=>{request.timer=null;void refreshQuotes();},delay);
    }
    function renderQuoteStatus() {
      if(state.watchLoading||state.watchError||!state.watch.length)return;
      const markets=[...new Set(state.watch.map(row=>row.market))];
      const requests=markets.map(market=>state.quoteRequests.get(market));
      $('watch-quote-status').textContent=requests.some(request=>request?.state==='error')?'部分行情暂不可用':
        requests.some(request=>request?.inflight||request?.state==='waiting')?'行情加载中…':'';
    }
    function renderQuoteDisplays(){
      renderWatch();
      for(const card of $('signal-list').querySelectorAll('.signal-card')){
        const row=state.rows.find(r=>r.id===card.dataset.id),quote=row&&state.quotes.get(`${row.market}:${row.code}`);
        if(quote)card.querySelector('.queue-quote').textContent=quoteText(quote);
      }
      if(state.selected)$('live-quote').textContent=quoteText(state.quotes.get(`${state.selected.market}:${state.selected.code}`));
    }
    function syncPoints(){document.querySelectorAll('[data-point]').forEach(b=>{b.classList.toggle('active',b.dataset.point===state.filters.point);b.setAttribute('aria-pressed',String(b.dataset.point===state.filters.point));});}
    $('scope').addEventListener('change',updateScope);
    $('screen-strategy').addEventListener('change',updateScreenMode);
    $('screen-frequency').addEventListener('change',updateScreenMode);
    $('screen-form').addEventListener('input',()=>{state.settingsEdited=true;});
    $('screen-form').addEventListener('invalid',()=>{$('screen-settings').open=true;},true);
    $('screen-form').addEventListener('submit',event=>{event.preventDefault();return changeRun('start');});
    $('cancel').addEventListener('click',()=>changeRun('cancel'));
    document.querySelectorAll('[data-point]').forEach(b=>b.addEventListener('click',()=>{state.filters.point=b.dataset.point;syncPoints();renderQueue();}));
    let chartObserver=null;
    function activateChart(){state.chartVisible=true;if(chartObserver){chartObserver.disconnect();chartObserver=null;}}
    document.querySelectorAll('[data-frequency]').forEach(b=>b.addEventListener('click',()=>{activateChart();state.frequency=b.dataset.frequency;renderChart();}));
    for(const [id,key] of [['market-filter','market'],['origin-filter','origin'],['frequency-filter','frequency'],['review-filter','review'],['stage-filter','stage'],['risk-filter','risk'],
      ['point-status-filter','point_status'],['lower-status-filter','lower_status'],['higher-context-filter','higher'],['center-order-filter','center_order'],['age-filter','age'],['gain-filter','gain'],['sort-filter','sort']])
      $(id).addEventListener('change',()=>{state.filters[key]=$(id).value;state.queuePage=1;renderQueue();});
    $('signal-search').addEventListener('input',()=>{state.filters.query=$('signal-search').value;state.queuePage=1;renderQueue();});
    $('sector-search').addEventListener('input',renderSectors);
    $('sector-more').addEventListener('click',()=>{state.sectorExpanded=!state.sectorExpanded;renderSectors();});
    $('queue-prev').addEventListener('click',()=>{state.queuePage--;clearSelection();renderQueue();});
    $('queue-next').addEventListener('click',()=>{state.queuePage++;clearSelection();renderQueue();});
    $('reset').addEventListener('click',()=>{
      Object.assign(state.filters,{query:'',market:'all',origin:'screening',frequency:'all',point:'all',review:'all',stage:'all',risk:'all',sector:'',
        point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'});
      $('signal-search').value='';$('sector-search').value='';
      for(const id of ['market-filter','frequency-filter','review-filter','stage-filter','risk-filter','lower-status-filter','higher-context-filter','center-order-filter','age-filter','gain-filter'])$(id).value='all';
      $('point-status-filter').value='confirmed';$('sort-filter').value='recent';$('origin-filter').value='screening';syncPoints();renderSectors();renderQueue();
    });
    $('refresh').addEventListener('click',()=>{void refresh(true);void refreshQuotes();});
    $('watch-group').addEventListener('change',()=>void refreshWatch());$('watch-refresh').addEventListener('click',()=>void refreshWatch());
    $('auto-quotes').addEventListener('change',()=>void refreshQuotes());
    $('chart-layout').addEventListener('change',()=>{activateChart();renderChart();});$('chart-reload').addEventListener('click',()=>{activateChart();renderChart(true);});
    let chartFitFrame=0;
    function fitChartHeight(){
      if(typeof window==='undefined'||document.body.classList.contains('theater')||$('selected-detail').hidden)return;
      const stage=$('chart-stage');
      const viewport=window.visualViewport?.height||window.innerHeight;
      const nav=document.querySelector('.app-nav')?.getBoundingClientRect().height||0;
      // Give the chart itself a viewport of space below the navigation.
      // The controls above it and the structure conclusion below it remain
      // reachable by scrolling the page.
      const height=Math.max(Math.min(260,viewport*.4),viewport-nav-24);
      stage.style.height='';
      stage.style.setProperty('--chart-auto-height',`${Math.floor(height)}px`);
    }
    function scheduleChartFit(){
      if(typeof window==='undefined')return;
      if(chartFitFrame)window.cancelAnimationFrame(chartFitFrame);
      chartFitFrame=window.requestAnimationFrame(()=>{chartFitFrame=0;fitChartHeight();});
    }
    if(typeof window!=='undefined'){
      window.addEventListener('resize',scheduleChartFit);
      window.visualViewport?.addEventListener('resize',scheduleChartFit);
      if(typeof ResizeObserver!=='undefined'){
        const observer=new ResizeObserver(scheduleChartFit);
        for(const selector of ['.app-nav','#analysis > header','.chart-filters','#chart-periods','.chart-toolbar']){
          const element=document.querySelector(selector);if(element)observer.observe(element);
        }
      }
      scheduleChartFit();
    }
    $('chart-reset-size').addEventListener('click',fitChartHeight);
    $('chart-source').addEventListener('change',()=>{activateChart();renderChart();});
    $('chart-frame').addEventListener('load',()=>{
      if($('chart-frame').src==='about:blank')return;
      if(state.selected)$('chart-status').textContent='图表页面已载入，数据状态见图内提示';
      watchChartInterval();
    });
    if(!state.chartVisible){
      chartObserver=new IntersectionObserver(entries=>{
        if(entries.some(entry=>entry.isIntersecting)){
          activateChart();
          if(state.selected)renderChart();
        }
      },{rootMargin:'120px 0px'});
      chartObserver.observe($('chart-stage'));
    }
    function listHidden(hidden){$('workspace').classList.toggle('list-hidden',hidden);$('toggle-list').textContent=hidden?'展开列表':'收起列表';$('toggle-list').setAttribute('aria-pressed',String(hidden));}
    $('toggle-list').addEventListener('click',()=>listHidden(!$('workspace').classList.contains('list-hidden')));
    function theater(on){
      const active=document.body.classList.contains('theater');
      if(on&&!active){state.fullChartPreviousList=$('workspace').classList.contains('list-hidden');listHidden(true);}
      document.body.classList.toggle('theater',on);
      $('theater').textContent=on?'退出满屏':'图表满屏';$('theater').setAttribute('aria-pressed',String(on));
      if(!on&&active){listHidden(!!state.fullChartPreviousList);state.fullChartPreviousList=null;}
      if(on)$('theater').focus?.();
      scheduleChartFit();
    }
    $('theater').addEventListener('click',()=>theater(!document.body.classList.contains('theater')));
    document.addEventListener('keydown',e=>{if(e.key==='Escape')theater(false);});
    $('review-form').addEventListener('input',()=>{if(state.selected)state.drafts.set(draftKey(),reviewFormValue());});
    $('review-form').addEventListener('submit',async event=>{
      event.preventDefault();if(!state.selected||state.selected.origin==='watchlist')return;
      const source=state.data.source,candidate_id=state.selected.id,key=draftKey(),submitted=reviewFormValue();
      const submittedDraft=state.drafts.get(key);
      if(state.savingReview)return;
      state.savingReview=true;state.reviewWriteEpoch++;state.reviewsLoadedSource=null;
      $('save-review').disabled=true;$('review-message').textContent='正在保存…';
      try{
        const item=await api('/screening/reviews',{source,candidate_id,...submitted});
        if(state.drafts.get(key)===submittedDraft)state.drafts.delete(key);
        if(state.data?.source===source){state.reviews.latest[candidate_id]=item;state.reviews.history.unshift(item);renderSummary();renderQueue();
          if(state.selected?.id===candidate_id){renderReview();$('review-message').textContent=state.drafts.has(key)?'已保存提交的版本；后续修改仍为未保存草稿。':'已保存。刷新页面后记录仍会保留。';}}
      }catch(error){if(state.selected?.id===candidate_id)$('review-message').textContent=error.message;}
      finally{state.savingReview=false;$('save-review').disabled=false;}
    });
    $('export').addEventListener('click',async()=>{
      if(!state.data)return;
      const source=state.data.source,ids=new Set(state.rows.map(row=>row.id));
      $('export').disabled=true;
      try{
        const full=state.data.compact?await api('/screening/workbench?compact=0'):state.data;
        if(full.source!==source)throw new Error('选股批次已更新，请刷新后导出。');
        const rows=[...full.candidates.filter(row=>ids.has(row.id)),...state.rows.filter(row=>row.origin==='watchlist')];
        const url=URL.createObjectURL(new Blob([JSON.stringify({source,status:full.status,filters:state.filters,candidates:rows,reviews:state.reviews.latest},null,2)],{type:'application/json'}));
        const a=node('a');a.href=url;a.download=`screening-${source}.json`;a.click();URL.revokeObjectURL(url);
      }catch(error){message(error.message);}finally{$('export').disabled=false;}
    });
    void refresh(true);void initWatch();
    const poll=async()=>{if(!document.hidden)await refresh(false);setTimeout(poll,5000);};setTimeout(poll,5000);
    const quotes=()=>{void refreshQuotes();setTimeout(quotes,15000);};setTimeout(quotes,15000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshQuotes();});
  }
  return {boot,filterRows,chartUrl,evidenceUrl,quoteText,cutoffSummary,resultVersionMessage,emptyResultsMessage,pageRows};
});
