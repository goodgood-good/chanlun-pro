/* Persistent automation controls; no page timer launches a market scan. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const points = {'1buy':'一买','2buy':'二买','3buy':'三买','1sell':'一卖','2sell':'二卖','3sell':'三卖',
    consolidation_up:'向上盘整背驰',consolidation_down:'向下盘整背驰',risk_stop:'结构止损触发',risk_reverse:'反向信号确认'};
  const stages = {warning:'提前预警（未确认）',invalidated:'预警已失效',confirmed:'已确认',approaching:'形成等待',formed:'形成等待',observed:'形成等待',left:'退出本轮候选',triggered:'持仓风险触发'};
  const active = job => ['starting','running'].includes(job?.status);
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
  let data, busy = false, polling = false, initialized = false, serial = 0;
  let refreshAfterControl=false;
  let refreshController, searchTimer;
  let statusCache=null;
  let riskRevision=null,riskRetryAt=0;
  const pages = {signals:1,pool:1,events:1};
  function node(tag, text, className) { const el = document.createElement(tag); if(text !== undefined)el.textContent = text; if(className)el.className = className; return el; }
  function chart(row) {
    const link = node('a', `${row.name || row.code} · ${row.code}`);
    link.href = '/?' + new URLSearchParams({market:row.market||'a',code:row.code,frequency:row.frequency||'5m',layout:'single',workspace_layout:'1'});
    link.target = '_blank'; link.rel = 'noopener'; return link;
  }
  async function api(path, body, controller) {
    const options = {credentials:'same-origin',headers:{Accept:'application/json'}};
    const isStatus=body===undefined&&path.startsWith('/monitor/status');
    if(isStatus&&statusCache?.path===path)options.headers['If-None-Match']=statusCache.etag;
    if(body!==undefined)statusCache=null;
    controller=controller||(typeof AbortController==='function'?new AbortController():null);
    if(controller)options.signal=controller.signal;
    const deadline=controller?setTimeout(()=>controller.abort(),15000):null;
    if(body !== undefined)Object.assign(options,{method:'POST',headers:{...options.headers,'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name=csrf-token]').content},body:JSON.stringify(body)});
    try {
    const response = await fetch(path, options);
    if(response.status===304)return {unchanged:true};
    if(response.redirected || response.status === 401)throw Error('登录已过期，请重新登录后打开监听页面。');
    const result = await response.json();
    if(!response.ok)throw Error(result.error || result.errmsg || '请求失败');
    const etag=isStatus&&response.headers?.get?.('ETag');
    return isStatus?{payload:result,cache:etag?{path,etag}:null}:result;
    } finally { if(deadline!==null)clearTimeout(deadline); }
  }
  function message(text) { $('monitor-message').textContent=text;$('monitor-message').hidden=!text; }
  function pointStatus(row) {
    if(row.stage==='left')return '退出候选';
    return row.point_status ? stages[row.point_status]||row.point_status : row.stage==='confirmed'?'已确认':'筛选条件待齐';
  }
  function statusDetails(row) {
    const details=[];
    if(row.signal_type==='holding_risk')details.push(`持仓参考 ${points[row.reference_point_type]||'买点'} · 结构止损 ${row.stop_price??'—'}`);
    if(row.stage==='left'&&row.exit_reasons?.length){
      const reasons={STRUCTURAL_STOP_TRIGGERED:'触发结构止损参考',ANCHOR_DISTANCE_EXCEEDED:'距拐点超过筛选上限',
        CONFIRMING_SEGMENT_COMPLETED:'后继线段已完成',FIRST_ADVANCED_TO_SECOND:'已进入二类点阶段',LATER_SELL:'出现更晚的卖点',LATER_BUY:'出现更晚的买点',SELECTION_WINDOW_ENDED:'退出当前筛选窗口'};
      details.push(row.exit_reasons.map(reason=>reasons[reason]||reason).join('；'));
    }
    if(row.signal_type==='consolidation_divergence'){
      if(row.warning_recovery_pending)details.push('恢复后首次复核通过，等待下一收盘复核后推送');
      details.push(`持仓标的 · ${row.core_position==='breakout'?'突破中枢':'中枢内'}`);
      if(row.stage!=='invalidated'&&Number.isFinite(row.macd_area_ratio))details.push(`MACD 同向柱面积比 C/A ${(row.macd_area_ratio*100).toFixed(1)}%`);
      else if(row.stage!=='invalidated'&&Number.isFinite(row.macd_area_a)&&Number.isFinite(row.macd_area_c))details.push(`MACD 同向柱面积 A ${row.macd_area_a} / C ${row.macd_area_c}`);
      if(row.stage==='warning')details.push('线段尚未完成，预警可能被后续走势推翻');
      if(row.stage==='invalidated')details.push(row.invalidation_reason||'最新复核不再满足预警条件');
      if(row.first_warned_at)details.push(`对应预警 ${date(row.first_warned_at)}`);
    }
    if(row.lower_confirmation_state)details.push(`${row.lower_frequency||'1m'} 区间套：${row.lower_confirmation_state==='confirmed'?'已确认':'等待确认'}`);
    if(row.following_segment_state==='in_progress')details.push(`后继${{up:'向上',down:'向下'}[row.following_segment_direction]||''}线段：进行中`);
    return details;
  }
  function progress(id, job) { const el=$(id);el.hidden=!active(job);el.max=Math.max(1,job.total||0);el.value=job.completed||0; }
  function listPage(id, rows) {
    const remote=data?.pagination?.[id];
    if(remote){
      pages[id]=remote.page;
      const start=(remote.page-1)*remote.size,end=Math.min(start+rows.length,remote.total);
      $(id+'-page').textContent=remote.total?`${remote.page} / ${remote.pages} 页 · ${start+1}–${end} / ${remote.total} 条`:'0 条';
      $(id+'-prev').disabled=remote.page<=1;$(id+'-next').disabled=remote.page>=remote.pages;
      return {rows,total:remote.total,filtered:!!remote.query};
    }
    const query=$(id+'-search').value.trim().toLocaleLowerCase();
    const matching=query?rows.filter(row=>[row.code,row.name,row.frequency,points[row.point_type]].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)):rows;
    const size=Number($(id+'-page-size').value)||50;
    const total=matching.length, count=Math.max(1,Math.ceil(total/size));
    pages[id]=Math.max(1,Math.min(pages[id],count));
    const start=(pages[id]-1)*size,end=Math.min(start+size,total);
    const label=total?`${pages[id]} / ${count} 页 · ${start+1}–${end} / ${total} 条`:'0 条';
    if($(id+'-page').textContent!==label)$(id+'-page').textContent=label;
    $(id+'-prev').disabled=pages[id]===1;
    $(id+'-next').disabled=pages[id]===count;
    return {rows:matching.slice(start,end),total,filtered:!!query};
  }
  function events() {
    const page=listPage('events',data?.events||[]);
    $('events').replaceChildren(...page.rows.map(row=>{
      const item=node('article',undefined,'monitor-event');item.append(chart(row),node('span',` · ${row.frequency||'5m'} ${points[row.point_type]||row.point_type} · ${pointStatus(row)}`));
      for(const detail of statusDetails(row))item.append(node('small',detail));
      item.append(node('small',`${date(row.recorded_at)} 记录 · 行情截止 ${date(row.source_closed_at)} · ${row.change==='discovered'?'首次发现':row.change==='left'?'退出候选':'状态变化'}`));return item;
    }));
    $('events-empty').textContent=page.total?'':page.filtered?'没有匹配的监听记录。':'尚无监听变化记录；完成首轮检查后在这里显示。';
  }
  function signals() {
    const page=listPage('signals',data?.signals||[]);
    $('signals').replaceChildren(...page.rows.map(row=>{
      const tr=node('tr'),name=node('td');name.append(chart(row));tr.append(name);
      tr.append(node('td',`${row.frequency} ${points[row.point_type]||row.point_type}`));
      const status=node('td');status.append(node('div',pointStatus(row)));
      for(const detail of statusDetails(row))status.append(node('div',detail,'muted'));
      tr.append(status);
      for(const value of [row.anchor_price??'—',date(row.available_at),date(row.source_closed_at)])tr.append(node('td',String(value)));return tr;
    }));
    $('signals-empty').textContent=page.total?'':page.filtered?'没有匹配的当前信号。':data?.last_check_at?'最近完成的一轮没有可展示信号；有行情问题时请查看诊断。':'尚待首轮监听完成。';
  }
  function pool() {
    const page=listPage('pool',data?.seed?.symbols||[]);
    $('pool').replaceChildren(...page.rows.map(row=>{
      const link=chart(row),label={limited:'数据受限',pending:'待核验'}[row.coverage_state];
      if(label)link.append(node('small',' · '+label));return link;
    }));
    $('pool-empty').textContent=page.total?'':page.filtered?'没有匹配的监听标的。':'尚无选股候选或自选标的。';
  }
  function render(result) {
    data=result; const manual=result.screening_job||{},live=result.monitor_job||{},seed=result.seed;
    const poolCount=seed?.count??seed?.codes?.length??0;
    if(!initialized){$('morning-after-close').value=result.settings.morning_after_close||'11:35';$('after-close').value=result.settings.after_close;$('daily-workers').value=String(result.settings.screening?.workers||12);$('interval').value=String(result.settings.interval_seconds);$('daily-scope').value=result.settings.daily_scope;initialized=true;}
    $('enabled-label').textContent=result.enabled?(result.runtime_running?'自动任务已开启':'后台服务尚未就绪'):'自动任务已暂停';
    $('enable').disabled=busy;$('enable').textContent=result.enabled?'保存并保持开启':'启动自动任务';
    $('pause').disabled=busy||!result.enabled;$('check-now').disabled=busy||!result.enabled||!poolCount||active(live)||active(manual);
    $('daily-status').textContent=result.daily_schedule?.title||'定时选股状态待核验';
    $('daily-detail').textContent=result.daily_schedule?.detail||'正在读取交易日历和任务记录。';
    $('live-status').textContent=!result.enabled?'监听已暂停':!seed?'等待选股候选或自选标的':!poolCount?'监听名单为空':active(live)?`检查中 · ${live.completed||0} / ${live.total||'—'}${active(manual)?' · 全量选股同时进行':''}`:active(manual)?'全量选股中 · 实时监听等待下一次检查':result.last_error?'部分标的需要检查':'等待各市场新的已收盘 K 线';
    $('live-detail').textContent=`新 K 线收盘后优先检查；常规检查间隔 ${result.settings.interval_seconds} 秒，已完成标的即时更新。最近处理 ${date(result.last_check_at)}，最近整批完成 ${date(result.last_batch_completed_at)}。${result.pending_symbols_count?`仍有 ${result.pending_symbols_count} 只等待检查。`:''}${live.error||''}`;
    const markets={a:'A 股',us:'美股',hk:'港股',fx:'外汇',ny_futures:'国际期货',futures:'国内期货',currency_spot:'数字货币现货',currency:'数字货币合约'};
    const coverage=Object.entries(result.pool_markets||{}).map(([m,n])=>`${markets[m]||m} ${n} 只`).join('，');
    $('pool-source').textContent=seed?`选股候选 ${seed.screening_count??seed.screening_symbols?.length??poolCount} 只${seed.screening_recheck_count?` + 待复核 ${seed.screening_recheck_count} 只`:''} + 自选 ${seed.watchlist_count||0} 只，去重共 ${poolCount} 只，其中持仓 ${seed.holding_count||0} 只。${coverage}。${result.coverage?`检查通过 ${result.coverage.ready} 只 · 数据受限 ${result.coverage.limited} 只 · 待核验 ${result.coverage.pending} 只。`:''}${seed.error_count?'盘后选股存在数据问题，详见选股诊断。':''}`:'自选标的无需先出现买卖点即可进入监听名单。';
    progress('daily-progress',manual);progress('live-progress',live);
    $('pool-count').textContent=poolCount;$('signal-count').textContent=result.pagination?.signals.overall??result.signals.length;
    $('event-count').textContent=result.pagination?.events.overall??result.events.length;$('error-count').textContent=result.errors?.length||0;
    const marketCutoffs=Object.entries(result.last_cutoffs_by_market||{}).map(([m,t])=>`${markets[m]||m} ${date(t)}`).join(' / ');
    $('signal-cutoff').textContent=marketCutoffs?`各市场最近检查截止：${marketCutoffs}`:`最近检查行情截止 ${date(result.last_cutoffs?.['5m'])}`;
    signals();pool();
    $('errors').replaceChildren(...(result.errors||[]).map(row=>node('li',`${row.code} · ${row.error||(row.reasons||[]).join('、')||'行情或计算异常'}`)));
    $('errors-panel').hidden=!result.errors?.length;events();message(result.last_error);
    const dt=result.dingtalk||{};
    if(result.notification_activity&&$('notification-activity-status')){
      const activity=result.notification_activity;
      $('notification-activity-status').textContent=activity.message;
      $('notification-activity-times').textContent=`最近复核 ${date(activity.last_check_at)} · 最近新事件 ${date(activity.last_event_at)} · 最近通知成功 ${date(activity.last_sent_at)} · 最近一次处理新增 ${activity.new_events??'—'} 条`;
    }
    $('dingtalk-enabled').checked=!!dt.enabled;$('dingtalk-enabled').disabled=busy||!dt.configured;
    $('dingtalk-status').textContent=!dt.configured?'尚未配置机器人':!dt.enabled?'钉钉通知已关闭':dt.last_error&&!dt.last_error.startsWith('等待')?'通知发送遇到问题':dt.pending?'通知处理中':'钉钉通知已开启';
    const charts=dt.chart_delivery||{},generation=dt.chart_generation||{};
    $('dingtalk-detail').textContent=dt.configuration_error||`通知待发 ${dt.pending||0} 条 · 已发送 ${dt.sent||0} 条 · 失败 ${dt.failed||0} 条 · 最近成功 ${date(dt.last_sent_at)}；图表已发 ${charts.sent||0} 张 · 待处理 ${(charts.pending||0)+(generation.pending||0)} 张 · 失败 ${(charts.failed||0)+(generation.failed||0)} 张${dt.last_error?'；'+dt.last_error:''}${generation.last_error?'；'+generation.last_error:''}`;
    if(dt.storage_available===false)$('dingtalk-detail').textContent='通知记录暂不可读取，发送数量尚无法核对；行情复核继续运行。';
    const archive=generation.evidence_archive;
    if(archive&&(archive.saved||archive.pending||archive.unavailable||archive.error))
      $('dingtalk-detail').textContent+=archive.error?'；'+archive.error:
        `；原始证据留存 ${archive.saved||0} 条（${archive.keep_days||7} 天）${archive.pending?' · 待保存 '+archive.pending+' 条':''}${archive.unavailable?' · 无法留存 '+archive.unavailable+' 条':''}`;
    const delivery={pending:'待发送',sending:'发送中',retry:'等待重试',sent:'已发送',failed:'发送失败',expired:'已过期，未补发'};
    $('dingtalk-history').replaceChildren(...(dt.history||[]).map(row=>node('p',`${date(row.created)} · ${row.kind==='screening'?'选股完成汇总':row.kind==='chart'?'信号图表':'监听信号'} · ${delivery[row.status]||row.status}${row.error?' · '+row.error:''}`)));
    if($('business-health')&&result.business_health){
      const health=result.business_health;
      const labels={running:'调度正常',paused:'已暂停',stopped:'调度未运行',starting:'启动检查中',delayed:'调度检查有延迟'};
      $('business-health').textContent=`${labels[health.scheduler]||health.scheduler} · 最近成功处理 ${date(health.last_check_at)} · 待检查 ${health.pending} 只 · 已绑定风险参考 ${health.risk_bound} 个${health.risk_attention?' · '+health.risk_attention+' 个参考需要查看':''}`;
      refreshRisksIfNeeded();
    }
  }
  function query(){
    const params=new URLSearchParams({view:'paged'});
    for(const id of ['signals','pool','events']){
      params.set(id+'_page',String(pages[id]));params.set(id+'_size',$(id+'-page-size').value||'50');params.set(id+'_q',$(id+'-search').value.trim());
    }
    return '?'+params.toString();
  }
  async function refresh(force=false) {
    if(busy){refreshAfterControl=true;return;}
    if(polling&&!force)return;
    refreshController?.abort();refreshController=typeof AbortController==='function'?new AbortController():null;
    polling=true;const request=++serial;
    try{
      const result=await api('/monitor/status'+query(),undefined,refreshController);
      if(request===serial){
        if(!result.unchanged){
          // Cache only a representation this request actually displayed.
          // Superseded responses and render failures cannot authorize a 304.
          statusCache=null;
          render(result.payload);
          statusCache=result.cache;
        }else{message(data?.last_error||'');refreshRisksIfNeeded();}
      }
    }
    catch(error){if(request===serial)message(error.name==='AbortError'?'状态读取超时，稍后自动重试。':error.message);}
    finally{if(request===serial)polling=false;}
  }
  async function control(action,body={}) {
    if(busy)return;
    const requestedView=query(),requestedPages={...pages};
    busy=true;refreshAfterControl=false;++serial;refreshController?.abort();polling=false;
    let failure='';
    try{
      if(data){try{render(data);}finally{Object.assign(pages,requestedPages);}}
      message('');
      const result=await api('/monitor/'+action+requestedView,body);
      const wantedPages={...pages},viewChanged=query()!==requestedView;
      try{render(result);}finally{
        if(viewChanged){Object.assign(pages,wantedPages);refreshAfterControl=true;}
      }
    }catch(error){
      failure=error.name==='AbortError'?'操作响应超时，请刷新核对状态。':error.message;
      message(failure);
      refreshAfterControl=refreshAfterControl||query()!==requestedView;
    }finally{
      busy=false;
      if(data){$('enable').disabled=false;$('pause').disabled=!data.enabled;$('check-now').disabled=!data.enabled||!(data.seed?.count??data.seed?.codes?.length)||active(data.monitor_job)||active(data.screening_job);$('dingtalk-enabled').checked=!!data.dingtalk?.enabled;$('dingtalk-enabled').disabled=!data.dingtalk?.configured;}
      if(refreshAfterControl){
        refreshAfterControl=false;clearTimeout(searchTimer);
        const followUp=refresh(true),request=serial;
        if(failure)void followUp.then(()=>{if(request===serial)message(failure);});
      }
    }
  }
  $('monitor-form').addEventListener('submit',event=>{event.preventDefault();void control('start',{morning_after_close:$('morning-after-close').value,after_close:$('after-close').value,screening_workers:Number($('daily-workers').value),interval_seconds:Number($('interval').value),daily_scope:$('daily-scope').value});});
  $('pause').addEventListener('click',()=>void control('pause'));
  $('dingtalk-enabled').addEventListener('change',()=>void control('dingtalk',{enabled:$('dingtalk-enabled').checked}));
  $('check-now').addEventListener('click',()=>void control('check'));
  $('reload').addEventListener('click',()=>void refresh());
  for(const id of ['signals','pool','events']){
    $(id+'-search').addEventListener('input',()=>{pages[id]=1;clearTimeout(searchTimer);searchTimer=setTimeout(()=>void refresh(true),180);});
    $(id+'-page-size').addEventListener('change',()=>{pages[id]=1;void refresh(true);});
    $(id+'-prev').addEventListener('click',()=>{pages[id]--;void refresh(true);});
    $(id+'-next').addEventListener('click',()=>{pages[id]++;void refresh(true);});
  }
  let riskBusy=false,analyticsBusy=false,riskActionKey=null;
  const riskActions=[],riskSelections=new Map();
  function refreshRisksIfNeeded(){
    const wanted=data?.business_health?.risk_revision;
    if($('holding-risk-panel')?.open&&!riskBusy&&wanted&&wanted!==riskRevision&&Date.now()>=riskRetryAt&&document.activeElement?.tagName!=='SELECT'){
      void riskView();
    }
  }
  const money=value=>Number.isFinite(value)?Number(value.toFixed(8)).toLocaleString('en-US',{maximumFractionDigits:8}):'—';
  async function riskView(body){
    const actionKey=body?JSON.stringify(body):null;
    if(riskBusy){
      const previousKey=riskActions.length?riskActions[riskActions.length-1].key:riskActionKey;
      if(body&&actionKey!==previousKey){
        riskActions.push({key:actionKey,body:{...body}});
        $('holding-risk-message').textContent='正在更新持仓信息，已记录操作，完成后提交。';
      }
      return;
    }
    riskBusy=true;riskActionKey=actionKey;
    const requestedRevision=data?.business_health?.risk_revision??riskRevision;
    try{
      const result=await api('/monitor/holding-risk',body);
      if(body?.action==='bind')for(const [key,identity] of riskSelections)if(identity===body.signal_id)riskSelections.delete(key);
      const holdingKeys=new Set((result.holdings||[]).map(row=>`${row.market}:${row.code}`));
      for(const key of riskSelections.keys())if(!holdingKeys.has(key))riskSelections.delete(key);
      const cards=(result.holdings||[]).map(row=>{
        const selectionKey=`${row.market}:${row.code}`;
        const card=node('article',undefined,'monitor-risk-card');card.append(node('strong',`${row.name||row.code} · ${row.code}`));
        for(const period of row.periods||[])card.append(node('small',`${period.frequency} · ${{ready:'已核验',limited:'部分检查受限',pending:'等待核验'}[period.status]||period.status} · 行情截止 ${date(period.cutoff)}`,'muted'));
        for(const plan of row.plans||[]){
          const labels={active:'跟踪中',waiting:'等待复核',requires_review:'需要重新核验',stop_triggered:'结构止损已触发',reverse_confirmed:'反向信号已确认'};
          card.append(node('p',`${plan.frequency} ${points[plan.point_type]} · 止损参考 ${plan.stop_label||money(plan.stop_price)} · ${labels[plan.status]||plan.status}`));
          if(plan.window_exited_at)card.append(node('small','已退出选股窗口，风险参考继续跟踪。','muted'));
          if(plan.detail)card.append(node('small',plan.detail,'muted'));
          card.append(node('small','检查截止 '+date(plan.checked_cutoff),'muted'));
          const remove=node('button','解除参考');remove.type='button';remove.addEventListener('click',()=>void riskView({action:'unbind',binding_id:plan.binding_id}));card.append(remove);
        }
        const options=row.choices||[];
        if(options.length){
          const label=node('label','绑定参考买点 '),select=node('select');select.setAttribute('aria-label',`${row.code} 参考买点`);
          select.append(node('option','请选择参考买点'));
          options.forEach((choice,index)=>{const option=node('option',`${choice.frequency} ${points[choice.point_type]} · ${date(choice.available_at)} · 拐点 ${choice.anchor_label||money(choice.anchor_price)} / 止损 ${choice.stop_label||money(choice.stop_price)}`);option.value=String(index);select.append(option);});
          select.children[0].value='';
          const selected=options.findIndex(choice=>choice.signal_id===riskSelections.get(selectionKey));
          select.value=selected<0?'':String(selected);
          if(selected<0)riskSelections.delete(selectionKey);
          select.addEventListener('change',()=>{
            const choice=select.value===''?null:options[Number(select.value)];
            if(choice)riskSelections.set(selectionKey,choice.signal_id);else riskSelections.delete(selectionKey);
          });
          label.append(select);card.append(label);
          const bind=node('button','绑定并跟踪');bind.type='button';bind.addEventListener('click',()=>{
            if(select.value===''){message('请先选择参考买点。');return;}
            const choice=options[Number(select.value)];
            if(!choice){message('参考买点已更新，请重新选择。');return;}
            riskSelections.set(selectionKey,choice.signal_id);
            void riskView({action:'bind',signal_id:choice.signal_id,source_closed_at:choice.source_closed_at});
          });card.append(bind);
        }else{if(!result.retry_needed)riskSelections.delete(selectionKey);card.append(node('p','当前暂无可核验且止损尚未触发的参考买点。','muted'));}
        if(row.message)card.append(node('p',row.message,'muted'));
        return card;
      });
      $('holding-risk-list').replaceChildren(...cards);
      $('holding-risk-message').textContent=!result.holding_source_current?'持仓分组暂不可核验，等待恢复。':!result.monitor_enabled?'参考已保留，启动自动任务后继续跟踪。':cards.length?'参考价由所选信号确定；使用绑定后新收盘K线持续跟踪。':'我的持仓分组为空。';
      riskRevision=result.retry_needed?null:requestedRevision;
      riskRetryAt=result.retry_needed?Date.now()+5000:0;
      if(body)void refresh(true);
    }catch(error){riskRevision=null;riskRetryAt=Date.now()+5000;$('holding-risk-message').textContent=error.message;}finally{
      riskBusy=false;riskActionKey=null;
      if(riskActions.length)void riskView(riskActions.shift().body);else refreshRisksIfNeeded();
    }
  }
  async function analyticsView(){
    if(analyticsBusy)return;analyticsBusy=true;
    try{
      const result=await api('/monitor/analytics');
      $('analytics-description').textContent=result.error||`自 ${date(result.since)} 起记录 ${result.events} 条事件（保留最近一年），其中持仓风险 ${result.risk_events||0} 条。以下统计范围为提醒窗口；历史未完整记录的预警不计入转确认比例。`;
      $('analytics-groups').replaceChildren(...(result.groups||[]).map(row=>{
        const tr=node('tr');for(const value of [`${row.market} ${row.frequency}`,points[row.point_type]||row.point_type,row.samples,
          row.warnings?`${row.warning_confirmed} 确认 / ${row.warning_invalidated} 失效 / ${row.warnings} 次预警`:'—',
          `${row.window_exits} 退出 / ${row.window_stop_hits} 触发止损参考`,Number.isFinite(row.confirmation_delay_bars)?row.confirmation_delay_bars.toFixed(1)+' 根K线':'—'])tr.append(node('td',String(value)));return tr;
      }));
      $('analytics-timings').replaceChildren(...(result.timings||[]).map(row=>node('p',`${row.market} ${row.frequency} · ${row.samples} 次成功计算 · 标的内部平均 ${(row.total_ms/1000).toFixed(2)} 秒：取数 ${(row.fetch_ms/1000).toFixed(2)} / 结构 ${(row.structure_ms/1000).toFixed(2)} / 核验 ${(row.validation_ms/1000).toFixed(2)} / 保存 ${(row.persistence_ms/1000).toFixed(2)}；复用 ${row.proofs_reused} 个确认记录`)));
      const delivery=result.delivery;
      if(delivery){
        $('analytics-timings').append(node('p',delivery.error||`近 ${delivery.days} 天通知送达统计；以平台成功回执为准，文字、图文与独立图片分别统计。历史记录缺少的阶段时间不作估算。`));
        const labels={cutoff_to_sent:'行情截止至送达',schedule_wait:'截止至调度',worker_wait:'等待计算',fetch:'取数',compute:'计算与保存',result_wait:'结果读取',detection_to_sent:'发现至送达',queue:'发送排队',provider:'发送请求'};
        for(const row of delivery.groups||[]){
          const parts=Object.entries(row.measures||{}).map(([name,v])=>`${labels[name]||name} ${v.average_seconds.toFixed(2)} 秒（P95 ${v.p95_seconds.toFixed(2)}，${v.samples} 次）`);
          $('analytics-timings').append(node('p',`${row.market} ${row.frequency} · ${row.kind==='combined'?'图文':row.kind==='chart'?'图片':'文字'} · ${row.samples} 条：${parts.join('；')}`));
        }
      }
    }catch(error){$('analytics-description').textContent=error.message;}finally{analyticsBusy=false;}
  }
  $('holding-risk-panel')?.addEventListener('toggle',()=>{if($('holding-risk-panel').open)void riskView();});
  $('holding-risk-refresh')?.addEventListener('click',()=>void riskView());
  $('analytics-panel')?.addEventListener('toggle',()=>{if($('analytics-panel').open)void analyticsView();});
  $('analytics-refresh')?.addEventListener('click',()=>void analyticsView());
  void refresh();const poll=async()=>{if(!document.hidden)await refresh();setTimeout(poll,5000);};setTimeout(poll,5000);
})();
