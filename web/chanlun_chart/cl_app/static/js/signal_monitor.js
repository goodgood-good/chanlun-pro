/* Persistent automation controls; no page timer launches a market scan. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const points = {'1buy':'一买','2buy':'二买','3buy':'三买','1sell':'一卖','2sell':'二卖','3sell':'三卖'};
  const stages = {confirmed:'已确认',approaching:'形成等待',formed:'形成等待',observed:'形成等待',left:'退出本轮候选'};
  const active = job => ['starting','running'].includes(job?.status);
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
  let data, busy = false, polling = false, initialized = false, eventLimit = 40, serial = 0;
  function node(tag, text, className) { const el = document.createElement(tag); if(text !== undefined)el.textContent = text; if(className)el.className = className; return el; }
  function chart(row) {
    const link = node('a', `${row.name || row.code} · ${row.code}`);
    link.href = '/?' + new URLSearchParams({market:row.market||'a',code:row.code,frequency:'5m',layout:'single'});
    link.target = '_blank'; link.rel = 'noopener'; return link;
  }
  async function api(path, body) {
    const options = {credentials:'same-origin',headers:{Accept:'application/json'}};
    if(body !== undefined)Object.assign(options,{method:'POST',headers:{...options.headers,'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name=csrf-token]').content},body:JSON.stringify(body)});
    const response = await fetch(path, options);
    if(response.redirected || response.status === 401)throw Error('登录已过期，请重新登录后打开监听页面。');
    const result = await response.json();
    if(!response.ok)throw Error(result.error || result.errmsg || '请求失败');
    return result;
  }
  function message(text) { $('monitor-message').textContent=text;$('monitor-message').hidden=!text; }
  function pointStatus(row) {
    if(row.stage==='left')return '退出候选';
    return row.point_status ? stages[row.point_status]||row.point_status : row.stage==='confirmed'?'已确认':'筛选条件待齐';
  }
  function statusDetails(row) {
    const details=[];
    if(row.lower_confirmation_state)details.push(`${row.lower_frequency||'1m'} 区间套：${row.lower_confirmation_state==='confirmed'?'已确认':'等待确认'}`);
    if(row.following_segment_state==='in_progress')details.push(`后继${{up:'向上',down:'向下'}[row.following_segment_direction]||''}线段：进行中`);
    return details;
  }
  function progress(id, job) { const el=$(id);el.hidden=!active(job);el.max=Math.max(1,job.total||0);el.value=job.completed||0; }
  function events() {
    const rows=data?.events||[];
    $('events').replaceChildren(...rows.slice(0,eventLimit).map(row=>{
      const item=node('article',undefined,'monitor-event');item.append(chart(row),node('span',` · ${row.frequency||'5m'} ${points[row.point_type]||row.point_type} · ${pointStatus(row)}`));
      for(const detail of statusDetails(row))item.append(node('small',detail));
      item.append(node('small',`${date(row.recorded_at)} 记录 · 行情截止 ${date(row.source_closed_at)} · ${row.change==='discovered'?'首次发现':row.change==='left'?'退出候选':'状态变化'}`));return item;
    }));
    $('events-empty').textContent=rows.length?'':'尚无监听变化记录；完成首轮检查后在这里显示。';
    $('more-events').hidden=eventLimit>=rows.length;
  }
  function render(result) {
    data=result; const manual=result.screening_job||{},live=result.monitor_job||{},seed=result.seed;
    if(!initialized){$('morning-after-close').value=result.settings.morning_after_close||'11:35';$('after-close').value=result.settings.after_close;$('daily-workers').value=String(result.settings.screening?.workers||12);$('interval').value=String(result.settings.interval_seconds);$('daily-scope').value=result.settings.daily_scope;initialized=true;}
    $('enabled-label').textContent=result.enabled?(result.runtime_running?'自动任务已开启':'后台服务尚未就绪'):'自动任务已暂停';
    $('enable').disabled=busy;$('enable').textContent=result.enabled?'保存并保持开启':'启动自动任务';
    $('pause').disabled=busy||!result.enabled;$('check-now').disabled=busy||!result.enabled||!seed?.codes.length||active(live)||active(manual);
    const daily=result.daily||{};
    const today=daily.session===result.today;
    const phase=(key)=>today?daily[key]:null;
    $('daily-status').textContent=active(manual)?`选股中 · ${manual.completed||0} / ${manual.total||'—'}`:
      !today?'今日待运行':phase('afternoon')?.status==='completed'?'今日两轮已完成':
      phase('morning')?.status==='completed'?'上午已完成，下午待运行':
      phase('morning')?.status==='failed'||phase('afternoon')?.status==='failed'?'本日选股需要检查':'今日待运行';
    const phaseText=(key,label,time)=>`${label} ${time}：${phase(key)?.status==='completed'?'已完成':phase(key)?.status==='started'?'计算中':phase(key)?.status==='failed'?'失败，待重试':'待运行'}`;
    $('daily-detail').textContent=`${phaseText('morning','上午',result.settings.morning_after_close||'11:35')}；${phaseText('afternoon','下午',result.settings.after_close)}。应用需保持运行。${manual.error||''}${['morning','afternoon'].some(key=>phase(key)?.attempts>=3&&phase(key)?.status!=='completed')?'本时段已尝试 3 次，请检查行情后手动重新选股。':''}`;
    $('live-status').textContent=!result.enabled?'监听已暂停':active(manual)?'等待本轮盘后选股完成':!seed?'等待选股候选或自选标的':!seed.codes.length?'监听名单为空':active(live)?`检查中 · ${live.completed||0} / ${live.total||'—'}`:result.last_error?'部分标的需要检查':'等待各市场新的已收盘 K 线';
    $('live-detail').textContent=`每 ${result.settings.interval_seconds} 秒调度；分批检查，已完成标的即时处理。最近处理 ${date(result.last_check_at)}，最近整批完成 ${date(result.last_batch_completed_at)}。${result.pending_symbols_count?`仍有 ${result.pending_symbols_count} 只等待检查。`:''}${live.error||''}`;
    const markets={a:'A 股',us:'美股',hk:'港股',fx:'外汇',ny_futures:'国际期货',futures:'国内期货',currency_spot:'数字货币现货',currency:'数字货币合约'};
    const coverage=Object.entries(result.pool_markets||{}).map(([m,n])=>`${markets[m]||m} ${n} 只`).join('，');
    $('pool-source').textContent=seed?`选股候选 ${seed.screening_symbols?.length??seed.codes.length} 只 + 自选 ${seed.watchlist_count||0} 只，去重共 ${seed.codes.length} 只。${coverage}。${seed.error_count?'盘后选股存在数据问题，详见选股诊断。':''}`:'自选标的无需先出现买卖点即可进入监听名单。';
    progress('daily-progress',manual);progress('live-progress',live);
    $('pool-count').textContent=seed?.codes.length||0;$('signal-count').textContent=result.signals.length;
    $('event-count').textContent=result.events.length;$('error-count').textContent=result.errors?.length||0;
    const marketCutoffs=Object.entries(result.last_cutoffs_by_market||{}).map(([m,t])=>`${markets[m]||m} ${date(t)}`).join(' / ');
    $('signal-cutoff').textContent=marketCutoffs?`各市场最近检查截止：${marketCutoffs}`:`最近检查行情截止 ${date(result.last_cutoffs?.['5m'])}`;
    $('signals').replaceChildren(...result.signals.map(row=>{
      const tr=node('tr'),name=node('td');name.append(chart(row));tr.append(name);
      tr.append(node('td',`${row.frequency} ${points[row.point_type]||row.point_type}`));
      const status=node('td');status.append(node('div',pointStatus(row)));
      for(const detail of statusDetails(row))status.append(node('div',detail,'muted'));
      tr.append(status);
      for(const value of [row.anchor_price??'—',date(row.available_at),date(row.source_closed_at)])tr.append(node('td',String(value)));return tr;
    }));
    $('signals-empty').textContent=result.signals.length?'':result.last_check_at?'最近完成的一轮没有可展示信号；有行情问题时请查看诊断。':'尚待首轮监听完成。';
    $('pool').replaceChildren(...(seed?.symbols||[]).map(chart));
    $('pool-empty').textContent=seed?.codes.length?'':'尚无选股候选或自选标的。';
    $('errors').replaceChildren(...(result.errors||[]).map(row=>node('li',`${row.code} · ${row.error||(row.reasons||[]).join('、')||'行情或计算异常'}`)));
    $('errors-panel').hidden=!result.errors?.length;events();message(result.last_error);
    const dt=result.dingtalk||{};
    $('dingtalk-enabled').checked=!!dt.enabled;$('dingtalk-enabled').disabled=busy||!dt.configured;
    $('dingtalk-status').textContent=!dt.configured?'尚未配置机器人':!dt.enabled?'钉钉通知已关闭':dt.last_error?'通知发送遇到问题':'钉钉通知已开启';
    $('dingtalk-detail').textContent=dt.configuration_error||`待发送 ${dt.pending||0} 条 · 已发送 ${dt.sent||0} 条 · 失败 ${dt.failed||0} 条 · 最近成功 ${date(dt.last_sent_at)}${dt.last_error?'；'+dt.last_error:''}`;
    const delivery={pending:'待发送',sending:'发送中',retry:'等待重试',sent:'已发送',failed:'发送失败',expired:'已过期，未补发'};
    $('dingtalk-history').replaceChildren(...(dt.history||[]).map(row=>node('p',`${date(row.created)} · ${row.kind==='screening'?'选股完成汇总':'监听信号'} · ${delivery[row.status]||row.status}${row.error?' · '+row.error:''}`)));
  }
  async function refresh() {
    if(polling||busy)return;polling=true;const request=++serial;
    try{const result=await api('/monitor/status');if(request===serial)render(result);}catch(error){if(request===serial)message(error.message);}finally{polling=false;}
  }
  async function control(action,body={}) {
    if(busy)return;busy=true;++serial;if(data)render(data);message('');
    try{render(await api('/monitor/'+action,body));}catch(error){message(error.message);}finally{busy=false;if(data){$('enable').disabled=false;$('pause').disabled=!data.enabled;$('check-now').disabled=!data.enabled||!data.seed?.codes.length||active(data.monitor_job)||active(data.screening_job);$('dingtalk-enabled').checked=!!data.dingtalk?.enabled;$('dingtalk-enabled').disabled=!data.dingtalk?.configured;}}
  }
  $('monitor-form').addEventListener('submit',event=>{event.preventDefault();void control('start',{morning_after_close:$('morning-after-close').value,after_close:$('after-close').value,screening_workers:Number($('daily-workers').value),interval_seconds:Number($('interval').value),daily_scope:$('daily-scope').value});});
  $('pause').addEventListener('click',()=>void control('pause'));
  $('dingtalk-enabled').addEventListener('change',()=>void control('dingtalk',{enabled:$('dingtalk-enabled').checked}));
  $('check-now').addEventListener('click',()=>void control('check'));
  $('reload').addEventListener('click',()=>void refresh());
  $('more-events').addEventListener('click',()=>{eventLimit+=100;events();});
  void refresh();const poll=async()=>{if(!document.hidden)await refresh();setTimeout(poll,5000);};setTimeout(poll,5000);
})();
