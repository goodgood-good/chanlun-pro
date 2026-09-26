(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof document !== 'undefined') api.boot();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const POINTS = {'1buy':'一买','2buy':'二买','3buy':'三买','1sell':'一卖','2sell':'二卖','3sell':'三卖'};
  const STAGES = {confirmed:'已确认',triggered:'正式点确认',executable:'历史执行条件成立',approaching:'形成与等待',formed:'形成待确认',observed:'结构观察',monitoring:'关注观察',active:'结构跟踪',invalidated:'已失效',closed:'已结束'};
  const STATES = {idle:'尚未运行选股',starting:'准备选股',running:'正在选股',completed:'本次筛选完成',cancelled:'已取消 · 保留部分结果',interrupted:'任务中断',failed:'任务失败'};
  const RESULTS_LOADING = '正在读取并校验本轮选股结果，完成后会自动显示，无需重新选股。';
  const activeRun = status => ['starting','running'].includes(status);
  const EVIDENCE = {formal_center:'正式中枢成立',complete_leave:'离开段完成',complete_first_return:'首次回试完成',core_boundary_held:'回试未返回中枢区间',trend_divergence:'趋势背驰证据',consolidation_divergence:'盘整背驰证据',first_rebound:'首次反弹',first_retest:'首次回试',lower_level_confirmation:'次级别确认',internal_first_retest:'首次回试位于一买比较组合内部；二买可用时刻不早于一买确认。'};
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
  const INTERVALS = {'1m':'1','2m':'2','3m':'3','5m':'5','10m':'10','15m':'15','30m':'30',
    '60m':'60','120m':'120','180m':'180','240m':'240',d:'1D','2d':'2D','3d':'3D',w:'1W',m:'1M'};
  const CHART_COUNTS = {single:1,'horizontal-2':2,three:3,four:4};
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
  const isWaiting = row => ['approaching','formed','observed'].includes(row.stage);
  const pointStage = row => row.point?.status || row.stage;
  const MISSING = {terminal_unit_locked:'等待末段完成并锁定',unfinished_segment_lock:'等待未完成线段锁定',formal_center_confirmation:'等待中枢正式确认',lower_1m_confirmation:'等待对应区间内的 1m 买卖点确认'};
  const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
  const price = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—';
  function quoteText(quote) {
    if (!quote) return '行情待更新';
    const rate = quote.rate === null || quote.rate === undefined ? '涨跌幅暂缺' : `${Number(quote.rate)>0?'+':''}${price(quote.rate)}%`;
    const received=quote.received_at?` · 接收 ${new Date(quote.received_at).toLocaleTimeString('zh-CN',{hour12:false})}`:'';
    return `${price(quote.price)} · ${rate}${received}`;
  }
  function chartPeriods(row, frequency, layout) {
    const focus = INTERVALS[frequency] ? frequency : '5m';
    const order = row.nested_confirmation ? ['5m','1m','30m','d'] : ['30m','5m','1m','d'];
    return [focus, ...order.filter(f => f !== focus)].slice(0,CHART_COUNTS[layout]||1);
  }
  function chartUrl(row, frequency='5m', layout='single', periods=chartPeriods(row,frequency,layout)) {
    const count = CHART_COUNTS[layout] || 1;
    return '/?' + new URLSearchParams({market:row.market || 'a',code:row.code,layout:count===1?'single':layout,
      frequency:periods[0],intervals:periods.slice(0,count).map(f=>INTERVALS[f]).join(','),chart_sidebar:'collapsed'});
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
      if (filters.stage === 'forming' && !['approaching','formed','observed'].includes(row.stage)) return false;
      if (filters.stage === 'tracking' && !['monitoring','active'].includes(row.stage)) return false;
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
  function boot() {
    const $ = id => document.getElementById(id);
    if (!$('dashboard-panel')) return;
    const periodButtons=Array.from(document.querySelectorAll('[data-frequency]'));
    const state = {data:null,serial:0,version:'',rows:[],selected:null,watch:[],quotes:new Map(),
      reviews:{latest:{},history:[]},drafts:new Map(),filters:{query:'',market:'all',origin:'screening',frequency:'all',point:'all',review:'all',stage:'all',sector:'',
        point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'},
      frequency:'5m',sectorExpanded:false,queueLimit:100,chartKey:'',chartVisible:typeof window==='undefined'||typeof IntersectionObserver==='undefined',quoteBusy:false,watchSerial:0,
      mutationPending:false,refreshPending:false,settingsInitialized:false,settingsEdited:false,
      reviewsLoadedSource:null,reviewSerial:0,reviewWriteEpoch:0,savingReview:false,
      stopChartSync:null,chartPeriods:[],fullChartPreviousList:null};
    function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(className)el.className=className;return el; }
    function message(text) {$('page-message').textContent=text;$('page-message').hidden=!text;$('page-message').classList.toggle('is-loading',text===RESULTS_LOADING);}
    async function api(url, body, form=false) {
      const headers = body === undefined ? {} : {'X-CSRFToken':document.querySelector('meta[name=csrf-token]').content};
      if (body !== undefined && !form) headers['Content-Type']='application/json';
      try {
        const response = await fetch(url,{credentials:'same-origin',cache:'no-store',headers,
          ...(body!==undefined ? {method:'POST',body:form?new URLSearchParams(body):JSON.stringify(body)} : {}),signal:AbortSignal.timeout(20000)});
        if (response.redirected || response.status===401) throw new Error('登录已失效，请在行情页登录后刷新。');
        const value=await response.json();
        if (!response.ok || value.ok===false) throw new Error(value.error?.message || value.error || value.msg || '读取失败，请重试。');
        return value;
      } catch (error) {
        if (error.name === 'TimeoutError') throw new Error(body !== undefined
          ? '请求响应超时，操作可能已受理，请刷新状态后再试。'
          : ['/screening/workbench','/screening/status'].includes(url) ? '读取选股数据超时，页面会自动重试。' : '请求超时，请稍后重试。');
        throw error;
      }
    }
    const version = s => [s.run_id,s.status,s.completed,s.cancel_requested,s.source_current,s.cutoff_current,s.freshness_state,s.evidence_version,s.selection_policy_version,s.higher_context_version].join(':');
    function pointLabel(p={}) {
      const label=POINTS[p.point_type]||'人工关注';
      if(p.point_type==='1buy'||p.point_type==='1sell')return `${label} · 本级趋势背驰`;
      if(p.point_type==='2buy'||p.point_type==='2sell')return `${label} · ${p.variant==='weak_divergence'?'破前极值后盘整背驰':p.variant==='strict'?'守前极值':'变体待核对'}`;
      return label;
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
      renderRunControls();
      const incompleteRun=status.status==='completed'&&status.total>0
        &&status.error_count/status.total>=.2;
      $('run-status').textContent=(STATES[status.status] || status.status)
        +(status.results_loading?' · 正在读取结果…':incompleteRun?' · 结果不完整':'');
      const settings=status.settings||{};
      $('run-detail').textContent=[status.total ? `已处理 ${status.completed||0} / ${status.total} 只` : '',
        status.effective_workers?`并行 ${status.effective_workers} 进程`:settings.workers?`设定 ${settings.workers} 进程`:'',
        settings.strategy===STRATEGY?'5m 主信号 · 1m 区间套确认':settings.frequencies?.join(' / '),settings.point_types?.map(p=>POINTS[p]||p).join(' / '),
        settings.recent_sessions ? `最近 ${settings.recent_sessions} 个交易日确认 / 观测` : '',
        status.cancel_requested&&activeRun(status.status)?'正在取消选股计算':''].filter(Boolean).join(' · ');
      $('freshness').className='notice'+(!incompleteRun&&status.source_current===true&&status.cutoff_current===true?' current':'');
      $('freshness').textContent=[incompleteRun?`本轮 ${status.error_count}/${status.total} 只标的存在数据或计算错误，候选数量不能代表完整市场；请检查行情服务后重新选股。`:'',
        status.source_current===false&&status.run_id?(status.source_revision?'本次结果使用旧版算法，请重新选股。':'本次结果的算法版本尚不可核对。'):'',
        status.run_id&&settings.strategy!==STRATEGY?'这轮是原独立周期结果，尚未经过 1m 区间套确认。新一轮将按 5m 主信号筛选。':'',
        status.freshness_message||'尚无可核对的数据截止时刻。',
        status.semantic_exclusion_count?`已排除 ${status.semantic_exclusion_count} 个混用背驰类型或缺少来源证据的旧候选；需按当前规则重算。`:'',
        status.selection_policy_exclusion_count?`按首中枢偏好排除 ${status.selection_policy_exclusion_count} 条三买线索。`:'',
        state.data.diagnostics?.rejection_counts?.CONFIRMING_SEGMENT_COMPLETED?`已排除 ${state.data.diagnostics.rejection_counts.CONFIRMING_SEGMENT_COMPLETED} 个确认线段已完成的买点。`:'',
        state.data.diagnostics?.rejection_counts?.CONFIRMING_SEGMENT_MISSING?`有 ${state.data.diagnostics.rejection_counts.CONFIRMING_SEGMENT_MISSING} 个旧买点缺少确认线段证据，暂不纳入候选；需重新选股核对。`:'',
        state.data.diagnostics?.rejection_counts?.CONFIRMING_SEGMENT_UNRESOLVED?`有 ${state.data.diagnostics.rejection_counts.CONFIRMING_SEGMENT_UNRESOLVED} 个买卖点的后续线段仍待判定，已排除尾部投影作为确认依据。`:'',
        Object.entries(status.cutoffs||{}).map(([f,t])=>`${f} 截止 ${date(t)}`).join(' / ')].filter(Boolean).join(' ');
      $('symbol-count').textContent=status.results_loading?'—':new Set(candidates.map(r=>r.market+':'+r.code)).size;
      $('signal-count').textContent=status.results_loading?'—':candidates.length;
      const waiting=candidates.filter(isWaiting).length;
      const higher=state.data.higher_context||{};
      $('higher-context-status').textContent=higher.status==='completed'?
        `30m 独立背景已完成 ${higher.completed}/${higher.total} 只；按本轮 5m 截止时刻核对。方向筛选只影响页面，不改变买卖点、监听或通知。`:
        higher.status==='running'||higher.status==='starting'?
        `30m 独立背景计算中 ${higher.completed||0}/${higher.total||0} 只；缺少的证据暂列“未知”。`:
        higher.status==='failed'?`30m 背景计算失败：${higher.error||'请查看任务日志'}`:
        status.source_current===false?'本轮为旧版选股结果；30m 独立背景将在新一轮选股完成后计算。':'30m 独立背景等待本轮选股完成。';
      $('stage-counts').textContent=status.results_loading?'正在读取候选…':`筛选已通过 ${candidates.length-waiting} · 条件待齐 ${waiting}`;
      $('signal-breakdown').textContent=status.results_loading?'正在读取候选…':`筛选通过 ${candidates.length-waiting} · 待齐 ${waiting}`;
      $('sector-count').textContent=sectors.filter(s=>s.signal_count>0).length;
      $('review-count').textContent=Object.keys(state.reviews.latest).length;
      $('error-count').textContent=status.error_count ?? '—';
      $('point-counts').replaceChildren(...Object.entries(POINTS).map(([p,label])=>{
        const rows=candidates.filter(row=>row.point?.point_type===p);
        const confirmed=rows.filter(row=>pointStage(row)==='confirmed').length;
        return node('span',`${label} ${rows.length} · 5m 已确认 ${confirmed}`);
      }));
      const third=candidates.filter(row=>['3buy','3sell'].includes(row.point?.point_type));
      const both=third.filter(row=>pointStage(row)==='confirmed'&&row.nested_confirmation?.state==='confirmed').length;
      const lowerWaiting=third.filter(row=>pointStage(row)==='confirmed'&&row.nested_confirmation?.state!=='confirmed').length;
      const forming=third.filter(row=>pointStage(row)!=='confirmed').length;
      $('third-breakdown').textContent=status.results_loading?'正在校验三类点证据…':
        `三类点线索 ${third.length} 条：5m 与 1m 均确认 ${both} · 5m 已确认、1m 待齐 ${lowerWaiting} · 5m 尚在形成 ${forming}。当前列表默认只看 5m 已确认，可展开候选筛选切换。`;
      $('diagnostics').textContent=[`任务：${state.data.source}`,`行情截止：${Object.values(status.cutoffs||{}).map(date).join(' / ')||'—'}`,
        `行情截止：${status.cutoff_current===true?'最新收盘时刻':status.cutoff_current===false?'历史或待更新':'待核对'}；历史行情修订未重新核对`,
        `代码版本：${status.source_current===true?'当前':status.source_current===false?'历史':'待核对'}`,
        `数据问题：${status.error_count??'—'} 个周期组合`,status.error||''].filter(Boolean).join('；');
      const coverage=status.universe_coverage,markets={SH:'沪市',SZ:'深市',BJ:'北交所'};
      $('coverage-detail').textContent=coverage?`实际股票池：${Object.entries(coverage.markets||coverage.exchanges).map(([e,n])=>`${markets[e]||e} ${n} 只`).join('，')}`+
        (['all_a','all_a_watchlist'].includes(settings.scope)&&coverage.absent_exchanges.length?`；本次股票池未覆盖：${coverage.absent_exchanges.map(e=>markets[e]||e).join('、')}`:''):'';
      const diagnostics=state.data.diagnostics||{},labels=diagnostics.reason_labels||{};
      $('diagnostic-reasons').textContent=Object.entries(diagnostics.rejection_counts||{}).map(([r,n])=>`${labels[r]||r}：${n}`).join('；');
      $('diagnostic-errors').replaceChildren(...(diagnostics.errors||[]).map(r=>node('li',`${r.code} ${r.frequency||''}：${(r.reasons||[]).map(k=>labels[k]||k).join('；')} ${r.error||''}${r.data_quality?.missing_bars?`；缺口 ${r.data_quality.missing_bars} 根，始于 ${date(r.data_quality.first_missing_at)}`:''}`)),
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
      $('visible-count').textContent=`${state.rows.length} 条`;
      $('signal-list').replaceChildren();
      const names=sectorNames();
      for (const row of state.rows.slice(0,state.queueLimit)) {
        const b=node('button',undefined,'signal-card'+(state.selected?.id===row.id?' active':''));b.type='button';b.dataset.id=row.id;
        const title=node('div',undefined,'row');title.append(node('strong',row.name||row.code),node('span',pointLabel(row.point),'badge'));
        const quote=state.quotes.get(`${row.market}:${row.code}`);
        b.append(title,node('small',`${row.code} · ${row.frequency} · ${STAGES[pointStage(row)]||pointStage(row)}`),
          node('small',row.origin==='watchlist'?'人工关注 · 点击查看图表':`拐点 ${date(row.point.anchor_at)}`),
          node('small',row.origin==='watchlist'?'':`${pointStage(row)==='confirmed'?'确认':'观测'} ${date(row.point.available_at)}`),
          node('small',(row.sectors||[]).map(id=>names[id]).filter(Boolean).slice(-2).join(' / ')||'板块暂缺'),
          node('small',quote?quoteText(quote):`截止收盘 ${price(row.latest_price)}`,'queue-quote'),
          node('small',state.reviews.latest[row.id]?'已保存人工复核':'','badge'));
        if(row.nested_confirmation)b.append(node('small',`${row.nested_confirmation.frequency||'1m'} 区间套：${row.nested_confirmation.state==='confirmed'?'已确认':'等待确认'}`));
        if(row.origin!=='watchlist')b.append(node('small',higherContextText(row.higher_context)));
        b.addEventListener('click',()=>{selectRow(row);renderQueue();});$('signal-list').append(b);
      }
      $('queue-more').hidden=state.queueLimit>=state.rows.length;
      $('queue-empty').hidden=!!state.rows.length;
      $('queue-empty').textContent=state.data?.status.results_loading?RESULTS_LOADING:'最新一轮结果中没有符合当前过滤条件的候选。可重置筛选、切换关注组，或查看上方选股进度及下方数据诊断。';
      if (state.selected && !state.rows.some(r=>r.id===state.selected.id)) {
        clearSelection();
      }
      if (!state.selected && state.rows.length) selectRow(state.rows.find(r=>r.id===previousSelection())||state.rows[0]);
      else if(state.selected)selectRow(state.rows.find(r=>r.id===state.selected.id));
    }
    function list(id, values) {$(id).replaceChildren(...values.map(v=>node('li',v)));}
    function selectRow(row) {
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
      $('selected-tags').textContent=[row.frequency,pointLabel(p),STAGES[pointStage(row)]||pointStage(row)].filter(Boolean).join(' · ');
      if(nested)$('selected-tags').textContent+=` · ${nested.frequency||'1m'} 区间套${nested.state==='confirmed'?'已确认':'等待确认'}`;
      const current=state.data.status.source_current===true&&state.data.status.cutoff_current===true;
      $('decision-title').textContent=row.origin==='watchlist'?'人工关注 · 查看当前结构':isWaiting(row)?(pointStage(row)==='confirmed'?'5m 买卖点已确认 · 1m 区间套等待确认':'形成与等待 · 5m 买点尚未确认'):current?(nested?'5m 买卖点与 1m 区间套确认通过':'本次筛选通过 · 待人工判断'):'本次结果待更新';
      $('decision-detail').textContent=row.origin==='watchlist'?'关注组不代表买卖点确认。':
        `拐点 ${date(p.anchor_at)}；${pointStage(row)==='confirmed'?'确认':'观测'} ${date(p.available_at)}。${!isWaiting(row)&&row.confirmation_delay_sessions!==undefined?`确认间隔 ${row.confirmation_delay_sessions} 个交易日。`:''}`+
        (nested?` 5m ${p.status==='confirmed'?'已确认':'待确认'}；1m ${nested.state==='confirmed'?'已确认':'等待确认'}。`:'')+
        (isWaiting(row)?` 待满足：${missing.map(c=>MISSING[c]||c).join('；')}。`:'');
      if(row.confirmation_segment?.state==='in_progress')$('decision-detail').textContent+=` 后继${{up:'向上',down:'向下'}[row.confirmation_segment.segment?.direction]||''}线段进行中，尚未完成。`;
      if(higherDetail)$('decision-detail').textContent+=` ${higherDetail}。`;
      $('anchor-price').textContent=price(p.anchor_price);$('invalidation-price').textContent=price(p.invalidation_price);$('close-price').textContent=price(row.latest_price);
      const exits=globalThis.PointExitInfo?.describe(row.exit_plan,state.data.point_exit_sources)
        || {stop:'依据不足',profit:'待结构证据',lines:[],sources:[]};
      $('stop-loss-price').textContent=exits.stop;$('take-profit-price').textContent=exits.profit;
      list('exit-price-detail',exits.lines);
      list('exit-price-sources',exits.sources);
      const lowerExits=globalThis.PointExitInfo?.describe(row.confirmation_exit_plan,state.data.point_exit_sources);
      list('confirmation-exit-detail',nested?['1m 区间套点独立参考：'+(lowerExits?`止损 ${lowerExits.stop}；止盈 / 退出 ${lowerExits.profit}`:'等待 1m 结构证据。'),
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
        ...missing.map(c=>MISSING[c]||c),...(row.decision_reasons||[]),
        ...(row.observation_validation==='legacy_not_rechecked'?['本条来自本轮保留的未确认记录，尚未按新检查重新选股；请结合当前图表观察。']:[]),
        '人工核对中枢区间、背驰依据与组合边界；程序复算通过不等于人工规则已经确认。',
        nested?'日线和 30m 用于背景观察；入选依据为 5m 主信号及其对应区间内的 1m 确认。':'日线、30m、5m、1m 按独立物理周期观察；本轮旧结果没有区间套确认。']);
      list('evidence-risk',row.origin==='watchlist'?['以图中实际结构确认风险边界。']:[
        `结构失效边界 ${price(p.invalidation_price)}；原拐点 ${price(p.anchor_price)}。`,
        '本次任务只评价截止时刻之前的 K 线；新行情出现后需重新确认有效性。',
        p.point_type==='3buy'?'三买回试触及中枢上沿不视为有效三买。':p.point_type==='3sell'?'三卖回试触及中枢下沿不视为有效三卖。':'继续核对后续反向结构与背驰。']);
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
        ?state.chartPeriods:chartPeriods(row,state.frequency,layout);
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
      $('review-fields').disabled=state.selected.origin==='watchlist';
      for(const select of $('review-form').querySelectorAll('select[name]'))select.value=values?.judgements?.[select.name]|| (select.name==='disposition'?'WATCH':'UNCERTAIN');
      $('review-notes').value=values?.notes||'';
      $('review-state').textContent=stored?`最近保存 ${date(stored.saved_at)}`:'待人工复核';
      $('review-message').textContent=state.selected.origin==='watchlist'?'关注标的需先产生正式候选，才能保存该候选的复核记录。':draft?'未保存的草稿已保留':'';
      const history=state.reviews.history.filter(r=>r.candidate_id===id);
      $('review-history-count').textContent=history.length;
      $('review-history').replaceChildren(...history.map(r=>{const a=node('article');a.append(node('strong',date(r.saved_at)),node('p',r.notes||'未填写笔记'),node('small',`处置：${{WATCH:'继续观察',REJECT:'拒绝',NEEDS_MORE_DATA:'需要更多数据'}[r.judgements.disposition]}`));return a;}));
    }
    function clearSelection() {
      stopChartSync();
      state.selected=null;$('selected-detail').hidden=true;$('selected-title').textContent='当前筛选未选中标的';
      $('selected-tags').textContent='';
      $('chart-frame').src='about:blank';state.chartKey='';state.chartPeriods=[];$('open-chart').href='/';
    }
    function showResults(data) {
      const changed=state.data?.source!==data.source;
      state.data=data;
      if(changed){clearSelection();state.reviews={latest:{},history:[]};state.reviewsLoadedSource=null;state.reviewSerial++;state.filters.sector='';}
      renderSummary();renderSectors();renderQueue();
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
          if(s.strategy===STRATEGY)for(const input of document.querySelectorAll('#screen-form [name=point]'))input.checked=s.point_types.includes(input.value);
          $('recent').value=s.recent_sessions;$('anchor').value=s.max_anchor_sessions;
          $('workers').value=status.source_current===false?12:(s.workers??12);
          $('max-gain').value=s.max_anchor_gain_pct??10;$('exclude-st').checked=s.exclude_st!==false;
          $('weak-second').checked=s.include_weak_second===true;
          updateScope();
        }
      }
    }
    function updateScope() {$('codes-label').hidden=$('scope').value!=='codes';$('codes').required=$('scope').value==='codes';}
    function screeningSettings() {
      const scope=$('scope').value;
      return {scope,codes:scope==='codes'?$('codes').value.toUpperCase().split(/[\s,，;；]+/).filter(Boolean):[],
        frequencies:['5m'],
        point_types:Array.from(document.querySelectorAll('#screen-form [name=point]:checked'),i=>i.value),
        recent_sessions:Number($('recent').value),max_anchor_sessions:Number($('anchor').value),workers:Number($('workers').value),
        max_anchor_gain_pct:Number($('max-gain').value),exclude_st:$('exclude-st').checked,
        include_weak_second:$('weak-second').checked};
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
      try {
        if(!force) {
          const status=await api('/screening/status');
          if(serial!==state.serial)return;
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
        const data=await api('/screening/workbench');
        if(serial!==state.serial)return;
        showResults(data);state.version=data.status.results_loading?'':version(data.status);
        message(data.status.results_loading?RESULTS_LOADING:'');
        if(data.status.results_loading)return;
        state.reviewsLoadedSource=null;
        await loadReviews(data.source,serial);
      }catch(error){if(serial===state.serial)message(error.message);}
      finally{state.refreshPending=false;}
    }
    function renderWatch() {
      const marketNames={futures:'国内期货',ny_futures:'纽约期货',fx:'外汇',currency:'数字货币合约',currency_spot:'数字货币现货'};
      for(const market of new Set(state.watch.map(row=>row.market))){
        if(!Array.from($('market-filter').options).some(o=>o.value===market)){
          const option=node('option',marketNames[market]||market);option.value=market;$('market-filter').append(option);
        }
      }
      $('watch-count').textContent=`${state.watch.length} 只 · 美股 ${state.watch.filter(s=>s.market==='us').length} 只`;
      $('watch-list').replaceChildren(...state.watch.map(row=>{
        const button=node('button');button.type='button';const title=node('span',row.name||row.code);title.append(node('small',`${row.market} · ${row.code}`));
        button.append(title,node('span',quoteText(state.quotes.get(`${row.market}:${row.code}`)),'watch-price'));
        button.addEventListener('click',()=>{state.filters.origin='all';$('origin-filter').value='all';state.filters.market='all';$('market-filter').value='all';state.filters.point='all';state.filters.sector='';state.filters.query='';$('signal-search').value='';state.filters.frequency='all';$('frequency-filter').value='all';state.filters.stage='all';$('stage-filter').value='all';state.filters.review='all';$('review-filter').value='all';syncPoints();
          selectRow(watchRows().find(r=>r.id===`watch:${row.market}:${row.code}`));renderQueue();renderSectors();});return button;
      }));
    }
    async function refreshWatch() {
      const serial=++state.watchSerial;
      try{
        const group=$('watch-group').value;if(!group)return;
        const data=await api('/get_zixuan_stocks/a/'+encodeURIComponent(group));
        if(serial!==state.watchSerial)return;
        state.watch=data.data||[];$('watch-status').textContent=state.watch.length?'点击标的进入右侧图表。':'当前分组暂无标的，可在自选分组管理中添加。';renderWatch();if(state.data){renderQueue();if(!state.data.status?.results_loading)await refreshQuotes();}
      }catch(error){if(serial===state.watchSerial)$('watch-status').textContent=error.message;}
    }
    async function initWatch() {
      try{const groups=await api('/get_zixuan_groups/a');$('watch-group').replaceChildren(...groups.map(g=>{const o=node('option',g.name);o.value=g.name;return o;}));
        if(groups.some(g=>g.name==='我的关注'))$('watch-group').value='我的关注';await refreshWatch();
      }catch(error){$('watch-status').textContent=error.message;}
    }
    async function refreshQuotes() {
      if(state.quoteBusy||!$('auto-quotes').checked||document.hidden)return;
      state.quoteBusy=true;
      try{
        const targets=[...(state.selected?[state.selected]:[]),...state.watch.slice(0,80),...state.rows.slice(0,15)];
        const groups=new Map();for(const row of targets){if(!groups.has(row.market))groups.set(row.market,new Set());groups.get(row.market).add(row.code);}
        const results=await Promise.allSettled(Array.from(groups,async([market,codes])=>{
          const data=await api('/ticks',{market,codes:JSON.stringify([...codes])},true);
          if(data.quote_state==='deferred')return;
          for(const tick of data.ticks||[])state.quotes.set(`${market}:${tick.code}`,{...tick,received_at:Date.now()});
          // Show each completed market immediately; a slow remote provider must
          // not hold back A-share or other already available prices.
          renderQuoteDisplays();
        }));
        const failures=results.filter(r=>r.status==='rejected');
        if(failures.length)$('watch-status').textContent=failures.map(r=>r.reason.message).join('；');
        else if(groups.size)$('watch-status').textContent=`行情更新 ${new Date().toLocaleTimeString('zh-CN')}；暂无行情的标的保留缺失提示。`;
        renderQuoteDisplays();
      }finally{state.quoteBusy=false;}
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
    $('screen-form').addEventListener('input',()=>{state.settingsEdited=true;});
    $('screen-form').addEventListener('invalid',()=>{$('screen-settings').open=true;},true);
    $('screen-form').addEventListener('submit',event=>{event.preventDefault();return changeRun('start');});
    $('cancel').addEventListener('click',()=>changeRun('cancel'));
    document.querySelectorAll('[data-point]').forEach(b=>b.addEventListener('click',()=>{state.filters.point=b.dataset.point;syncPoints();renderQueue();}));
    let chartObserver=null;
    function activateChart(){state.chartVisible=true;if(chartObserver){chartObserver.disconnect();chartObserver=null;}}
    document.querySelectorAll('[data-frequency]').forEach(b=>b.addEventListener('click',()=>{activateChart();state.frequency=b.dataset.frequency;renderChart();}));
    for(const [id,key] of [['market-filter','market'],['origin-filter','origin'],['frequency-filter','frequency'],['review-filter','review'],['stage-filter','stage'],
      ['point-status-filter','point_status'],['lower-status-filter','lower_status'],['higher-context-filter','higher'],['center-order-filter','center_order'],['age-filter','age'],['gain-filter','gain'],['sort-filter','sort']])
      $(id).addEventListener('change',()=>{state.filters[key]=$(id).value;state.queueLimit=100;renderQueue();});
    $('signal-search').addEventListener('input',()=>{state.filters.query=$('signal-search').value;state.queueLimit=100;renderQueue();});
    $('sector-search').addEventListener('input',renderSectors);
    $('sector-more').addEventListener('click',()=>{state.sectorExpanded=!state.sectorExpanded;renderSectors();});
    $('queue-more').addEventListener('click',()=>{state.queueLimit+=100;renderQueue();});
    $('reset').addEventListener('click',()=>{
      Object.assign(state.filters,{query:'',market:'all',origin:'screening',frequency:'all',point:'all',review:'all',stage:'all',sector:'',
        point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'});
      $('signal-search').value='';$('sector-search').value='';
      for(const id of ['market-filter','frequency-filter','review-filter','stage-filter','lower-status-filter','higher-context-filter','center-order-filter','age-filter','gain-filter'])$(id).value='all';
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
    $('export').addEventListener('click',()=>{if(!state.data)return;const url=URL.createObjectURL(new Blob([JSON.stringify({source:state.data.source,status:state.data.status,filters:state.filters,candidates:state.rows,reviews:state.reviews.latest},null,2)],{type:'application/json'}));
      const a=node('a');a.href=url;a.download=`screening-${state.data.source}.json`;a.click();URL.revokeObjectURL(url);});
    void refresh(true);void initWatch();
    const poll=async()=>{if(!document.hidden)await refresh(false);setTimeout(poll,5000);};setTimeout(poll,5000);
    const quotes=async()=>{await refreshQuotes();setTimeout(quotes,15000);};setTimeout(quotes,15000);
  }
  return {boot,filterRows,chartUrl,evidenceUrl,quoteText};
});
