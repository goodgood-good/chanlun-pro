'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'screening_workbench.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../../../templates/early_screening.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
const templateIds = new Set([...template.matchAll(/\bid=["']([^"']+)["']/g)].map(match=>match[1]));
const {filterRows} = require('../screening_workbench.js');
class Element {
  constructor() {
    this.children = []; this.listeners = {}; this.textContent = ''; this.value = '';
    this.dataset = {}; this.options = []; this.attributes = {};
    const classes=new Set();
    this.classList = {toggle(name,on) {if(on)classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)};
  }
  append(...children) {
    for(const child of children){this.children=this.children.filter(c=>c!==child);this.children.push(child);}
  }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name,value) {this.attributes[name]=value;}
  querySelectorAll(selector) {
    if (!selector.startsWith('.')) return [];
    const name=selector.slice(1);
    return this.children.flatMap(child=>child instanceof Element?
      [...(String(child.className||'').split(/\s+/).includes(name)?[child]:[]),...child.querySelectorAll(selector)]:[]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}
function page(fetch, preferences, autoQuotes=false, strictTemplate=false) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const [id, value] of Object.entries({scope:'all_a', codes:'', recent:'5', anchor:'20', workers:'12', 'max-gain':'10','chart-layout':'single',
    'screen-strategy':'5m_with_1m_confirmation','screen-frequency':'5m','screen-grade':'0'})) get(id).value = value;
  get('exclude-st').checked = true;
  get('auto-quotes').checked = autoQuotes;
  const frequency = ['1m','5m','30m'].map(value => ({value,checked:value!=='1m'}));
  const point = ['1buy','2buy','3buy','1sell','2sell','3sell'].map(value => ({value,checked:true}));
  const periods = ['1m','5m','30m','d'].map(frequency => {
    const element = new Element(); element.dataset.frequency = frequency; return element;
  });
  get('chart-periods').append(...periods);
  const timers = [], timerDelays=[];
  const clock={now:Date.now()};
  class PageDate extends Date { static now(){return clock.now;} }
  const body = new Element(), documentEvents = {};
  const sandbox={
    PointExitInfo: require('../point_exit_info.js'),
    document: {
      body, getElementById:id=>strictTemplate&&!templateIds.has(id)?null:get(id), createElement:() => new Element(),
      addEventListener(name, fn) { (documentEvents[name] ||= []).push(fn); },
      querySelector:s => s==='main' ? {dataset:{user:'tester'}} : {content:'test-csrf'},
      querySelectorAll:s => {
        if(s==='[data-frequency]')return periods;
        const inputs=s.includes('#screen-form [name=frequency]')?frequency:s.includes('#screen-form [name=point]')?point:[];
        return s.endsWith(':checked')?inputs.filter(i=>i.checked):inputs;
      },
    },
    fetch, URL, URLSearchParams, Date:PageDate, Blob, AbortSignal, location:{origin:'http://local'},
    AccountPreferences:preferences,
    setTimeout:(fn,delay) => {timerDelays.push(delay);return timers.push(fn);}, clearTimeout:id=>{timers[id-1]=()=>{};},
  };
  vm.runInNewContext(source,sandbox);
  return {get, timers, timerDelays, advance:ms=>{clock.now+=ms;}, periods, body, documentEvents,
    setHidden:value=>{sandbox.document.hidden=value;for(const fn of documentEvents.visibilitychange||[])fn();}};
}
const response = (value, status=200) => ({ok:status>=200&&status<300, status, redirected:false, json:async () => value});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

test('a missing quote becomes an explicit reference while other symbols stay current',async()=>{
  const latest=withCandidate(),stocks=[{market:'a',code:'SH.600000'},{market:'a',code:'SH.600001'}];
  latest.candidates[0].code=stocks[0].code;
  let partial=false;
  const ui=page(async(url)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:stocks});
    if(url==='/ticks')return response({ticks:[...(partial?[]:[{code:stocks[0].code,price:12,rate:1}]),{code:stocks[1].code,price:20,rate:-2}]});
    return dispatch(latest,url);
  },undefined,true);await settle();
  partial=true;ui.advance(15000);await ui.timers[1]();await settle();
  const old=ui.get('watch-list').children[0].children[1],current=ui.get('watch-list').children[1].children[1];
  assert.match(old.children[0].textContent,/上次报价/);
  assert.equal(old.children[1].className,'quote-neutral');
  assert.equal(current.children[1].className,'quote-down');
  assert.match(ui.get('live-quote').textContent,/上次报价/);
  assert.match(ui.get('signal-list').querySelector('.queue-quote').textContent,/上次报价/);
  assert.match(ui.get('watch-quote-status').textContent,/部分行情/);
});

test('a quote failure labels old data and a successful retry restores normal colors',async()=>{
  const latest=result();let fail=false;
  const ui=page(async(url)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'currency_spot',code:'BTC/USDT'}]});
    if(url==='/ticks')return fail?response({error:{message:'provider timeout',retry_after_seconds:30}},503):response({ticks:[{code:'BTC/USDT',price:100,rate:1}]});
    return dispatch(latest,url);
  },undefined,true);await settle();
  fail=true;ui.advance(15000);await ui.timers[1]();await settle();
  assert.match(ui.get('watch-list').children[0].children[1].children[0].textContent,/上次报价/);
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].className,'quote-neutral');
  fail=false;ui.advance(30000);await ui.timers[1]();await settle();
  assert.doesNotMatch(ui.get('watch-list').children[0].children[1].children[0].textContent,/上次报价/);
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].className,'quote-up');
});

test('hidden pages stop fetching and old prices are labeled immediately on returning',async()=>{
  const latest=result();let calls=0,release;
  const ui=page(async(url)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'a',code:'SH.600000'}]});
    if(url==='/ticks'){
      if(++calls>1)return new Promise(resolve=>{release=resolve;});
      return response({ticks:[{code:'SH.600000',price:12,rate:1}]});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  ui.setHidden(true);ui.advance(60000);await ui.timers[1]();await settle();
  assert.equal(calls,1);
  ui.setHidden(false);await settle();
  assert.equal(calls,2);
  assert.match(ui.get('watch-list').children[0].children[1].children[0].textContent,/上次报价/);
  release(response({ticks:[{code:'SH.600000',price:13,rate:0}]}));await settle();
  assert.doesNotMatch(ui.get('watch-list').children[0].children[1].children[0].textContent,/上次报价/);
});
function result(id='a', frequency='30m', status='completed') {
  return {source:id, candidates:[],sectors:[],point_counts:{},status:{
    run_id:id,status,total:1,completed:status==='completed'?1:0,
    source_current:true,cutoff_current:true,data_current:null,freshness_state:'cutoff_current',
    settings:{scope:'codes',codes:['SZ.000001'],frequencies:[frequency],point_types:['3buy'],
      recent_sessions:5,max_anchor_sessions:20,max_anchor_gain_pct:8,exclude_st:true},
  }};
}
function dispatch(latest, url) {
  if(url.startsWith('/screening/workbench')) return response(latest);
  if(url==='/screening/status') return response(latest.status);
  if(url.startsWith('/screening/reviews')) return response({latest:{},history:[]});
  if(url==='/get_zixuan_groups/a') return response([]);
  throw new Error('Unexpected endpoint: '+url);
}

test('pending evidence loads show progress and poll until the same run is ready', async () => {
  let latest=result(); latest.status.results_loading=true;
  const ui=page(async url=>url.startsWith('/screening/workbench')?response(latest,latest.status.results_loading?202:200):dispatch(latest,url));
  await settle();
  assert.match(ui.get('run-status').textContent,/本次筛选完成.*正在读取结果/);
  assert.match(ui.get('queue-empty').textContent,/正在读取并校验/);
  assert.equal(ui.get('signal-count').textContent,'—');
  latest=withCandidate();
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(ui.get('page-message').textContent,'');
  assert.equal(ui.get('queue-empty').hidden,true);
});

test('running partial results retain counts and candidate detail while more evidence is loading',async()=>{
  let latest=withCandidate(),details=0;
  latest.status={...latest.status,status:'running',total:5000,completed:10,results_loading:true,
    results_available:true,results_through:8,results_partial:true,evidence_version:'prefix-8',result_reader_revision:'reader'};
  latest.candidates[0]={...latest.candidates[0],detail_loaded:false,candidate_evidence_version:'point-proof'};
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')){
      details++;
      return response({source:latest.source,evidence_version:'newer-unrelated-prefix',result_reader_revision:'reader',
        candidate:{...latest.candidates[0],detail_loaded:true}});
    }
    return dispatch(latest,url);
  });await settle();
  assert.equal(ui.get('signal-count').textContent,1);
  assert.match(ui.get('run-detail').textContent,/已处理 10 \/ 5000.*已核验 8/);
  assert.match(ui.get('page-message').textContent,/已展示 8/);
  assert.equal(ui.get('review-fields').disabled,false);
  const chart=ui.get('chart-frame').src;
  latest={...latest,status:{...latest.status,completed:20,results_through:16,evidence_version:'prefix-16'}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(details,1);
  assert.equal(ui.get('chart-frame').src,chart);
  latest={...latest,status:{...latest.status,status:'completed',completed:5000,results_through:5000,
    results_partial:false,results_loading:false,evidence_version:'final'}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('run-status').textContent,'本次筛选完成');
  assert.equal(ui.get('page-message').textContent,'');
  assert.equal(ui.get('signal-count').textContent,1);
});

test('changed per-candidate evidence is rejected even while its batch is still running',async()=>{
  const latest=withCandidate();latest.status.status='running';
  latest.candidates[0]={...latest.candidates[0],detail_loaded:false,candidate_evidence_version:'old-proof'};
  const ui=page(async url=>url.startsWith('/screening/candidate?')?response({source:latest.source,
    candidate:{...latest.candidates[0],detail_loaded:true,candidate_evidence_version:'replaced-proof'}}):dispatch(latest,url));
  await settle();
  assert.match(ui.get('candidate-detail-state').textContent,/依据已更新/);
  assert.equal(ui.get('raw-evidence').textContent,'');
  assert.equal(ui.get('review-fields').disabled,true);
});

for(const [field,before,after] of [['live_recheck_version','before','after'],['current_source_revision','old','new'],
  ['result_reader_revision','old-reader','new-reader'],['source_current',true,false]])
test('a failed refresh cannot leave candidates from a superseded '+field+' active',async()=>{
  let latest=withCandidate(),fail=false,reads=0;
  latest.status[field]=before;
  const ui=page(async url=>{
    if(url.startsWith('/screening/workbench')){
      reads++;if(fail)return response({error:'isolated result read failure'},503);
    }
    return dispatch(latest,url);
  });await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  latest={...latest,status:{...latest.status,[field]:after}};fail=true;
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-list').children.length,0);
  assert.equal(ui.get('selected-detail').hidden,true);
  assert.match(ui.get('queue-empty').textContent,/读取|核验/);
  assert.equal(reads,2);
  fail=false;latest={...latest,candidates:[{...latest.candidates[0],id:'newly-verified',name:'new fixture'}]};
  await ui.timers[0]();await settle();
  assert.equal(reads,3);
  assert.equal(ui.get('signal-list').children.length,1);
  assert.match(ui.get('selected-title').textContent,/new fixture/);
});

test('monitor revalidation removes the active candidate and restores it after verification',async()=>{
  let latest=withCandidate();
  latest.status.live_recheck_version='verified';
  const savedCandidate=latest.candidates[0];
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  latest={...latest,candidates:[],status:{...latest.status,live_recheck_version:'pending',live_unverified_count:1}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-list').children.length,0);
  assert.equal(ui.get('selected-detail').hidden,true);
  assert.match(ui.get('diagnostic-errors').children[0].textContent,/尚未通过最新复核/);
  latest={...latest,candidates:[savedCandidate],status:{...latest.status,live_recheck_version:'recovered',live_unverified_count:0}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.match(ui.get('selected-title').textContent,/fixture/);
  assert.equal(ui.get('diagnostic-errors').children.length,0);
});

test('an ordinary network failure keeps the previously verified selection and review draft',async()=>{
  const latest=withCandidate();let fail=false;
  latest.status.live_recheck_version='same-proof';
  const ui=page(async url=>url.startsWith('/screening/workbench')&&fail
    ?response({error:'temporary network failure'},503):dispatch(latest,url));
  await settle();
  ui.get('review-notes').value='unsaved review';ui.get('review-form').listeners.input();
  fail=true;await ui.get('refresh').listeners.click();await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.equal(ui.get('selected-detail').hidden,false);
  assert.equal(ui.get('review-notes').value,'unsaved review');
});

test('polling and refresh clicks do not duplicate an inflight workbench read', async () => {
  const latest=result();let release, reads=0;
  const pending=new Promise(resolve=>{release=resolve;});
  const ui=page(async url=>{
    if(url.startsWith('/screening/workbench')){reads++;return pending;}
    return dispatch(latest,url);
  });
  await settle();
  await ui.timers[0]();
  await ui.get('refresh').listeners.click();
  assert.equal(reads,1);
  release(response(latest));await settle();
  assert.equal(ui.get('run-status').textContent,'本次筛选完成');
});

test('a timeout is readable and clears after a successful unchanged status poll', async () => {
  const latest=result();let timeout=true;
  const ui=page(async url=>{
    if(url==='/screening/status'&&timeout){
      const error=new Error('signal timed out');error.name='TimeoutError';throw error;
    }
    return dispatch(latest,url);
  });
  await settle();await ui.timers[0]();
  assert.equal(ui.get('page-message').textContent,'读取选股数据超时，页面会自动重试。');
  timeout=false;await ui.timers[0]();
  assert.equal(ui.get('page-message').textContent,'');
});

test('full-chart mode preserves the chart and review draft and restores list visibility', async () => {
  const data=withCandidate();
  const ui=page(async url=>dispatch(data,url));await settle();
  const chart=ui.get('chart-frame').src;
  ui.get('review-notes').value='keep this review';ui.get('review-form').listeners.input();
  ui.get('theater').listeners.click();
  assert.equal(ui.body.classList.contains('theater'),true);
  assert.equal(ui.get('workspace').classList.contains('list-hidden'),true);
  assert.equal(ui.get('theater').textContent,'退出满屏');
  ui.get('toggle-list').listeners.click();
  assert.equal(ui.get('workspace').classList.contains('list-hidden'),false);
  for(const listener of ui.documentEvents.keydown)listener({key:'Escape'});
  assert.equal(ui.body.classList.contains('theater'),false);
  assert.equal(ui.get('workspace').classList.contains('list-hidden'),false);
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('review-notes').value,'keep this review');
  ui.get('toggle-list').listeners.click();
  ui.get('theater').listeners.click();ui.get('theater').listeners.click();
  assert.equal(ui.get('workspace').classList.contains('list-hidden'),true);
});

test('multichart layouts expose their row count to chart sizing without changing a full-chart toggle', async () => {
  const data=withCandidate();const ui=page(async url=>dispatch(data,url));await settle();
  assert.equal(ui.get('chart-stage').dataset.chartLayout,'single');
  ui.get('chart-layout').value='four';ui.get('chart-layout').listeners.change();
  assert.equal(ui.get('chart-stage').dataset.chartLayout,'four');
  const chart=ui.get('chart-frame').src;
  ui.get('theater').listeners.click();ui.get('theater').listeners.click();
  assert.equal(ui.get('chart-frame').src,chart);
});

test('screening inherits account layout and secondary periods while pinning the selected candidate',async()=>{
  const latest=withCandidate();
  const saved={chart_layout_type:'four',a_interval_1:'30',a_interval_2:'1D',a_interval_3:'30',a_interval_4:'1'};
  const ui=page(async url=>dispatch(latest,url),{getItem:key=>key==='tv_chart'?JSON.stringify(saved):null});
  await settle();
  assert.equal(ui.get('chart-layout').value,'four');
  const target=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(target.searchParams.get('layout'),'four');
  assert.equal(target.searchParams.get('code'),'SH.600000');
  assert.equal(target.searchParams.get('intervals'),'5,1D,30,1');
  assert.equal(target.searchParams.get('workspace_layout'),'1');
});

test('a delayed poll cannot overwrite a run started from the latest-results page', async () => {
  let latest=result(), resolveOld, submitted;
  const oldPoll=new Promise(resolve=>{resolveOld=resolve;});
  const ui=page(async (url, options) => {
    if(url==='/screening/start') {
      submitted=JSON.parse(options.body);
      assert.equal(options.headers['X-CSRFToken'],'test-csrf');
      latest=result('new','5m','starting');
      return response(latest.status);
    }
    if(url==='/screening/status') return oldPoll;
    return dispatch(latest,url);
  });
  await settle();
  assert.equal(ui.get('max-gain').value,8);
  const pendingPoll=ui.timers[0]();
  await ui.get('screen-form').listeners.submit({preventDefault() {}});
  resolveOld(response(result('old','30m').status));await pendingPoll;await settle();
  assert.equal(submitted.max_anchor_gain_pct,8);
  assert.equal(submitted.workers,12);
  assert.deepEqual(submitted.frequencies,['5m']);
  assert.deepEqual(submitted.point_types,['1buy','2buy','3buy','1sell','2sell','3sell']);
  assert.match(ui.get('diagnostics').textContent,/new/);
  assert.match(ui.get('run-detail').textContent,/5m/);
  assert.doesNotMatch(ui.get('run-detail').textContent,/30m/);
  assert.equal(ui.get('start').disabled,true);
  assert.equal(ui.get('cancel').hidden,false);
  assert.equal(ui.get('run-progress').hidden,false);
  assert.equal(ui.get('chart-frame').src,'about:blank');
});

test('native center screening submits one physical period and no recursive grade', async () => {
  const latest=result();let submitted;
  const ui=page(async (url, options) => {
    if(url==='/screening/start'){
      submitted=JSON.parse(options.body);
      return response({...latest.status,run_id:'next',status:'starting'});
    }
    return dispatch(latest,url);
  });
  await settle();
  ui.get('screen-strategy').value='direct_structural';
  ui.get('screen-strategy').listeners.change();
  ui.get('screen-frequency').value='1m';
  ui.get('screen-grade').value='1';
  ui.get('screen-risk-period').value='1m';
  assert.equal(ui.get('direct-frequency-label').hidden,false);
  assert.equal(ui.get('nested-period-note').hidden,true);
  assert.equal(ui.get('direct-explanation').hidden,false);
  assert.equal(ui.get('nested-explanation').hidden,true);
  await ui.get('screen-form').listeners.submit({preventDefault(){}});
  assert.equal(submitted.strategy,'direct_structural');
  assert.deepEqual(submitted.frequencies,['1m']);
  assert.deepEqual(submitted.structural_levels,[0]);
  assert.equal(submitted.minimum_operating_frequency,undefined);

  let nestedSubmitted;
  const nested=page(async (url,options)=>{
    if(url==='/screening/start'){
      nestedSubmitted=JSON.parse(options.body);
      return response({...latest.status,run_id:'nested',status:'starting'});
    }
    return dispatch(latest,url);
  });
  await settle();
  nested.get('screen-risk-period').value='1m';
  await nested.get('screen-form').listeners.submit({preventDefault(){}});
  assert.equal(nestedSubmitted.strategy,'direct_structural');
  assert.deepEqual(nestedSubmitted.frequencies,['5m']);
  assert.equal(nestedSubmitted.minimum_operating_frequency,undefined);
});

test('native center screening submits every selected K-line period', async () => {
  const latest=result();let submitted;
  const ui=page(async (url,options)=>{
    if(url==='/screening/start'){
      submitted=JSON.parse(options.body);
      return response({...latest.status,run_id:'next',status:'starting'});
    }
    return dispatch(latest,url);
  });
  await settle();
  ui.get('screen-frequency').selectedOptions=[
    {value:'1m'},{value:'5m'},{value:'30m'},
  ];
  await ui.get('screen-form').listeners.submit({preventDefault(){}});
  assert.deepEqual(submitted.frequencies,['1m','5m','30m']);
  assert.deepEqual(submitted.structural_levels,[0]);
});

test('selected minimum-period MACD risk is visible without renaming a formal point', async () => {
  const data=withCandidate(),row=data.candidates[0];
  row.point.status='confirmed';
  row.operational_macd_risk={
    rule_id:'U176',physical_frequency:'5m',declared_minimum_frequency:'5m',
    risk_filter_state:'basic_avoid_on_selected_period',known_at:200,
    formal_structure_independent:true,tradable:false,
  };
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.match(ui.get('decision-title').textContent,/MACD基础避险/);
  assert.match(ui.get('selected-tags').textContent,/三买.*已确认/);
  assert.ok(ui.get('evidence-risk').children.some(e=>/MACD 黄白线均在零轴下/.test(e.textContent)));
  assert.match(ui.get('stage-counts').textContent,/筛选已通过 1/);
  ui.get('risk-filter').value='not_triggered';ui.get('risk-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'0 条');
  ui.get('risk-filter').value='avoid';ui.get('risk-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'1 条');
});

test('duplicate starts are suppressed and failed starts preserve the latest results', async () => {
  const latest=result();let release, starts=0, body;
  const pending=new Promise(resolve=>{release=resolve;});
  const ui=page(async (url,options) => {
    if(url==='/screening/start'){starts++;body=JSON.parse(options.body);return pending;}
    return dispatch(latest,url);
  });
  await settle();
  ui.get('recent').value='7';ui.get('screen-form').listeners.input();
  const first=ui.get('screen-form').listeners.submit({preventDefault() {}});
  await ui.get('screen-form').listeners.submit({preventDefault() {}});
  assert.equal(starts,1);assert.equal(body.recent_sessions,7);
  assert.equal(ui.get('start').disabled,true);
  assert.equal(ui.get('refresh').disabled,true);
  release(response({error:'test failure'},400));await first;
  assert.match(ui.get('diagnostics').textContent,/a/);
  assert.equal(ui.get('page-message').textContent,'test failure');
  assert.equal(ui.get('start').disabled,false);
  assert.equal(ui.get('recent').value,'7');
});

test('cancel stays available while running and becomes disabled after the request', async () => {
  let latest=result('running','5m','running'), cancels=0;
  const ui=page(async (url,options) => {
    if(url==='/screening/cancel') {
      cancels++;assert.equal(options.headers['X-CSRFToken'],'test-csrf');
      assert.equal(options.body,'{}');
      latest={...latest,status:{...latest.status,cancel_requested:true}};
      return response(latest.status);
    }
    return dispatch(latest,url);
  });
  await settle();
  assert.equal(ui.get('cancel').hidden,false);assert.equal(ui.get('cancel').disabled,false);
  await ui.get('cancel').listeners.click();await ui.get('cancel').listeners.click();
  assert.equal(cancels,1);
  assert.equal(ui.get('cancel').disabled,true);assert.equal(ui.get('start').disabled,true);
  assert.match(ui.get('run-detail').textContent,/取消/);
});

function withCandidate() {
  const data=result();
  data.candidates=[{id:'candidate',code:'SH.600000',name:'fixture',market:'a',frequency:'5m',
    origin:'screening',stage:'confirmed',sectors:[],point:{point_id:'P',point_type:'3buy',anchor_at:100,available_at:200,anchor_price:12,invalidation_price:11}}];
  return data;
}

test('the actual template supports rendering a completed result without invented elements',async()=>{
  const latest=withCandidate();
  const ui=page(async url=>dispatch(latest,url),undefined,false,true);
  await settle();
  assert.equal(ui.get('page-message').textContent,'');
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(ui.get('signal-list').children.length,1);
  assert.equal(ui.get('run-status').textContent,'本次筛选完成');
});

test('third-class counts distinguish confirmed points from incomplete lower confirmation', async () => {
  const data=withCandidate(), first=data.candidates[0];
  first.point.status='confirmed';first.stage='approaching';first.nested_confirmation={state:'waiting',interval:{start_after:100,end_at:200}};
  first.point.center_ordinal=1;first.confirmation_age_sessions=1;first.gain_pct=1.5;
  const forming={...first,id:'forming',code:'SH.600001',point:{...first.point,status:'approaching'},stage:'approaching'};
  const complete={...first,id:'complete',code:'SH.600002',point:{...first.point},stage:'confirmed',nested_confirmation:{state:'confirmed',interval:{start_after:100,end_at:200}}};
  const laterSell={...complete,id:'sell',code:'SH.600003',point:{...complete.point,point_type:'3sell',center_ordinal:2}};
  data.candidates=[first,forming,complete,laterSell];
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.match(ui.get('third-breakdown').textContent,/三类点线索 4 条：5m 与 1m 均确认 2 · 5m 已确认、1m 待齐 1 · 5m 尚在形成 1/);
  assert.equal(ui.get('visible-count').textContent,'3 条');
  ui.get('lower-status-filter').value='confirmed';ui.get('lower-status-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'2 条');
  ui.get('center-order-filter').value='first';ui.get('center-order-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'1 条');
  ui.get('point-status-filter').value='all';ui.get('point-status-filter').listeners.change();
  ui.get('lower-status-filter').value='all';ui.get('lower-status-filter').listeners.change();
  ui.get('center-order-filter').value='all';ui.get('center-order-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'4 条');
});

test('confirmed higher third buy keeps an entry-proof waiting stage', async () => {
  const data=withCandidate(), row=data.candidates[0];
  data.status.settings.strategy='direct_structural';
  data.status.settings.structural_levels=[1];
  data.diagnostics={reason_labels:{THIRD_ENTRY_PRECISION_PENDING:'次次级入场证仍待核验'}};
  row.point.status='confirmed';row.point.structural_level=1;
  row.stage='entry_pending';row.entry_waiting=true;
  row.decision_reasons=['THIRD_ENTRY_PRECISION_PENDING'];
  row.evidence_chart_available=true;
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.match(ui.get('selected-tags').textContent,/正式三买已确认 · 入场证待核/);
  assert.match(ui.get('decision-title').textContent,/次次级入场证待核/);
  assert.match(ui.get('decision-detail').textContent,/不能列为已选入场候选/);
  assert.match(ui.get('stage-counts').textContent,/筛选已通过 0 · 条件待齐 1/);
  assert.match(ui.get('third-breakdown').textContent,/入场证待核 1/);
  assert.ok(ui.get('evidence-missing').children.some(e=>e.textContent==='次次级入场证仍待核验'));
  ui.get('stage-filter').value='forming';ui.get('stage-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'1 条');
});

test('age, price distance and sort refine confirmed evidence without changing the point', () => {
  const rows=[
    {id:'older',code:'SH.600001',market:'a',origin:'screening',frequency:'5m',stage:'confirmed',sectors:[],
      point:{point_type:'3buy',status:'confirmed',center_ordinal:1},nested_confirmation:{state:'confirmed'},confirmation_age_sessions:3,gain_pct:4},
    {id:'near',code:'SH.600002',market:'a',origin:'screening',frequency:'5m',stage:'approaching',sectors:[],
      point:{point_type:'3buy',status:'confirmed',center_ordinal:1},nested_confirmation:{state:'waiting'},confirmation_age_sessions:1,gain_pct:1.2},
    {id:'next',code:'SH.600003',market:'a',origin:'screening',frequency:'5m',stage:'confirmed',sectors:[],
      point:{point_type:'3sell',status:'confirmed',center_ordinal:2},nested_confirmation:{state:'confirmed'},confirmation_age_sessions:0,gain_pct:2},
  ];
  const base={market:'all',origin:'screening',frequency:'all',point:'all',stage:'all',review:'all',sector:'',query:'',
    point_status:'confirmed',lower_status:'all',center_order:'all',age:'all',gain:'all',sort:'distance'};
  assert.deepEqual(filterRows(rows,base).map(r=>r.id),['near','next','older']);
  assert.deepEqual(filterRows(rows,{...base,age:'1',gain:'3',center_order:'first'}).map(r=>r.id),['near']);
  assert.deepEqual(filterRows(rows,{...base,lower_status:'confirmed'}).map(r=>r.id),['next','older']);
  assert.equal(rows[1].point.status,'confirmed');
});

test('native stops and the waiting exit condition are visible before lazy detail resolves',async()=>{
  const data=withCandidate(),row=data.candidates[0];
  row.detail_loaded=false;
  row.exit_plan={evidence_scope:'native_center_signal',point_status:'confirmed',side:'buy',
    stop_loss:{price:11.25,trigger:'lt',basis:'center_edge'},take_profit:{price:null,basis:'opposite_point',status:'waiting'}};
  const ui=page(async url=>url.startsWith('/screening/candidate?')?new Promise(()=>{}):dispatch(data,url));
  await settle();
  assert.equal(ui.get('stop-loss-price').textContent,'跌破 11.25');
  assert.equal(ui.get('take-profit-price').textContent,'等待本周期已确认反向点');
  const card=ui.get('signal-list').children[0];
  assert.ok(card.children.some(child=>child.textContent==='止损 跌破 11.25'));
  assert.ok(card.children.some(child=>child.textContent==='止盈 等待本周期已确认反向点'));
});

test('selected 5m and confirming 1m points display separate exit references', async () => {
  const data=withCandidate(), row=data.candidates[0];
  row.exit_plan={point_status:'confirmed',side:'buy',stop_loss:{price:11.001,trigger:'lte',basis:'center_edge'},take_profit:{price:null}};
  row.nested_confirmation={state:'confirmed',interval:{start_after:100,end_at:200},point:{point_type:'3buy',anchor_at:150,available_at:180}};
  row.confirmation_exit_plan={point_status:'confirmed',side:'buy',stop_loss:{price:11.2,trigger:'lte'},take_profit:{price:12.8,basis:'opposite_point',point_type:'1sell',available_at:230}};
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.equal(ui.get('stop-loss-price').textContent,'≤ 11.001');
  assert.equal(ui.get('take-profit-price').textContent,'待同级反向点');
  assert.match(ui.get('confirmation-exit-detail').children.map(x=>x.textContent).join(' '),/11.20.*12.80/);
  assert.equal(row.point.invalidation_price,11);
});

function chartWidget(resolution='5',ready=true) {
  const listeners=new Set(), pending=[], requests=[];
  const subscription={subscribe(_owner,fn){listeners.add(fn);},unsubscribe(_owner,fn){listeners.delete(fn);}};
  const chart={resolution:()=>resolution,onIntervalChanged:()=>subscription,
    setResolution(value){requests.push(value);resolution=value;for(const fn of listeners)fn(value);}};
  return {activeChart:()=>chart,onChartReady:fn=>{if(ready)fn();else pending.push(fn);},
    ready(){ready=true;for(const fn of pending.splice(0))fn();},listeners,requests};
}
function loadChart(ui,widgets) {
  const frame=ui.get('chart-frame');
  frame.contentDocument={};
  frame.contentWindow={chart_widgets:widgets,change_chart_ticker(){}};
  frame.listeners.load();
  return frame;
}
function assertPeriod(ui,frequency) {
  assert.deepEqual(ui.periods.filter(p=>p.classList.contains('active')).map(p=>p.dataset.frequency),
    ['1m','5m','30m','d'].includes(frequency)?[frequency]:[]);
  for(const p of ui.periods)assert.equal(p.attributes['aria-pressed'],String(p.dataset.frequency===frequency));
  assert.equal(new URL(ui.get('open-chart').href,'http://local').searchParams.get('frequency'),frequency);
}

test('chart toolbar and workbench periods stay synchronized across result refreshes', async()=>{
  let data=withCandidate();
  const ui=page(async url=>dispatch(data,url));await settle();
  const widget=chartWidget(),frame=loadChart(ui,[widget]),src=frame.src;
  widget.activeChart().setResolution('1');
  assertPeriod(ui,'1m');assert.equal(ui.get('chart-frequency').textContent,'图表周期：1m');
  assert.match(ui.get('selected-tags').textContent,/^5m/); // Signal level is immutable.
  data={...data,status:{...data.status,cutoff_current:false,freshness_state:'stale'}};
  await ui.timers[0]();await settle();
  assertPeriod(ui,'1m');assert.equal(widget.activeChart().resolution(),'1');assert.equal(frame.src,src);
  ui.periods.find(p=>p.dataset.frequency==='5m').listeners.click();
  assert.equal(widget.activeChart().resolution(),'5');assertPeriod(ui,'5m');
  assert.equal(frame.src,src);assert.equal(widget.listeners.size,1);
});

test('asynchronous iframe bootstrap and widget readiness both synchronize the actual period', async()=>{
  const ui=page(async url=>dispatch(withCandidate(),url));await settle();
  const frame=loadChart(ui,[]),widget=chartWidget('1',false);
  const retry=ui.timers.at(-1);
  frame.contentWindow.chart_widgets.push(widget);retry();
  assertPeriod(ui,'5m');assert.equal(widget.listeners.size,0);
  widget.ready();assertPeriod(ui,'1m');assert.equal(widget.listeners.size,1);
  frame.listeners.load();assert.equal(widget.listeners.size,1);
});

test('multichart synchronization follows the primary chart and represents non-button periods', async()=>{
  const ui=page(async url=>dispatch(withCandidate(),url));await settle();
  ui.get('chart-layout').value='horizontal-2';ui.get('chart-layout').listeners.change();
  const first=chartWidget(),second=chartWidget('1');loadChart(ui,[first,second]);
  assert.deepEqual(ui.get('chart-periods').children.map(b=>b.dataset.frequency),['1m','5m','30m','d']);
  second.activeChart().setResolution('30');assertPeriod(ui,'5m');
  assert.deepEqual(ui.get('chart-periods').children.map(b=>b.dataset.frequency),['1m','5m','30m','d']);
  first.activeChart().setResolution('1');assertPeriod(ui,'1m');
  assert.equal(second.activeChart().resolution(),'30');
  assert.equal(ui.get('chart-frequency').textContent,'首图周期：1m');
  first.activeChart().setResolution('15');assertPeriod(ui,'15m');
  assert.equal(new URL(ui.get('open-chart').href,'http://local').searchParams.get('intervals').split(',')[0],'15');
  first.activeChart().setResolution('D');assertPeriod(ui,'d');
});

test('period navigation stays fixed while full-workbench links track every pane through refresh and reload', async()=>{
  let data=withCandidate();
  data.candidates[0].nested_confirmation={state:'waiting',frequency:'1m',interval:{start_after:60,end_at:100}};
  const ui=page(async url=>dispatch(data,url));await settle();
  ui.get('chart-layout').value='four';ui.get('chart-layout').listeners.change();
  const widgets=['5','1','30','1D'].map(interval=>chartWidget(interval));
  const frame=loadChart(ui,widgets),src=frame.src;
  const order=()=>ui.get('chart-periods').children.map(b=>b.dataset.frequency);
  assert.deepEqual(order(),['1m','5m','30m','d']);
  widgets[1].activeChart().setResolution('30');widgets[2].activeChart().setResolution('1');
  assert.deepEqual(order(),['1m','5m','30m','d']);assertPeriod(ui,'5m');
  data={...data,status:{...data.status,cutoff_current:false,freshness_state:'stale'}};
  await ui.timers[0]();await settle();
  assert.deepEqual(order(),['1m','5m','30m','d']);assert.equal(frame.src,src);
  assert.equal(new URL(ui.get('open-chart').href,'http://local').searchParams.get('intervals'),'5,30,1,1D');
  ui.get('chart-reload').listeners.click();
  assert.equal(new URL(frame.src,'http://local').searchParams.get('intervals'),'5,30,1,1D');
  assert.ok(widgets.every(w=>w.listeners.size===0));
  ui.get('chart-layout').value='single';ui.get('chart-layout').listeners.change();
  loadChart(ui,[chartWidget()]);
  assert.deepEqual(order(),['1m','5m','30m','d']);
  ui.periods.find(b=>b.dataset.frequency==='1m').listeners.click();assertPeriod(ui,'1m');
  assert.deepEqual(order(),['1m','5m','30m','d']);
});

test('old interval events and delayed ready callbacks cannot change a replacement chart', async()=>{
  const ui=page(async url=>dispatch(withCandidate(),url));await settle();
  const old=chartWidget();loadChart(ui,[old]);
  const lateEvent=Array.from(old.listeners)[0];
  ui.get('chart-reload').listeners.click();assert.equal(old.listeners.size,0);
  const pending=chartWidget('1',false);loadChart(ui,[pending]);
  ui.periods.find(p=>p.dataset.frequency==='30m').listeners.click();
  // An iframe replacement must stop callbacks already queued by the old document.
  ui.get('chart-reload').listeners.click();
  const replacement=chartWidget('30');loadChart(ui,[replacement]);
  pending.ready();old.activeChart().setResolution('1');lateEvent('1');
  assertPeriod(ui,'30m');assert.equal(pending.listeners.size,0);assert.equal(replacement.listeners.size,1);
  ui.get('stage-filter').value='forming';ui.get('stage-filter').listeners.change();
  assert.equal(replacement.listeners.size,0);assert.equal(ui.get('chart-frame').src,'about:blank');
});

test('switching to frozen evidence detaches live synchronization and pins allowed periods', async()=>{
  const data=withCandidate();
  data.candidates[0].nested_confirmation={state:'waiting',frequency:'1m',interval:{start_after:60,end_at:100}};
  const ui=page(async url=>dispatch(data,url));await settle();
  const widget=chartWidget();loadChart(ui,[widget]);widget.activeChart().setResolution('1');
  ui.get('chart-source').value='evidence';ui.get('chart-source').listeners.change();
  assert.equal(widget.listeners.size,0);
  const frozen=chartWidget('1');loadChart(ui,[frozen]);assert.equal(frozen.listeners.size,0);
  widget.activeChart().setResolution('30');assertPeriod(ui,'1m');
  assert.equal(ui.periods.find(p=>p.dataset.frequency==='30m').disabled,true);
  ui.periods.find(p=>p.dataset.frequency==='5m').listeners.click();assertPeriod(ui,'5m');
  ui.get('chart-source').value='live';ui.get('chart-source').listeners.change();
  const live=chartWidget();loadChart(ui,[live]);live.activeChart().setResolution('1');assertPeriod(ui,'1m');
});

test('completed or unverified confirmation segments explain exclusions without starting a scan', async () => {
  const data=result(), requests=[];
  data.diagnostics={rejection_counts:{CONFIRMING_SEGMENT_COMPLETED:3,CONFIRMING_SEGMENT_MISSING:2}};
  const ui=page(async url=>{requests.push(url);return dispatch(data,url);});
  await settle();
  assert.match(ui.get('diagnostic-reasons').textContent,/CONFIRMING_SEGMENT_COMPLETED：3/);
  assert.match(ui.get('diagnostic-reasons').textContent,/CONFIRMING_SEGMENT_MISSING：2/);
  assert.match(ui.get('stage-counts').textContent,/筛选已通过 0/);
  assert.equal(requests.includes('/screening/start'),false);
});

test('waiting candidates use the shared analysis chart and never claim confirmation', async () => {
  const data=withCandidate(), row=data.candidates[0];
  row.stage='approaching'; row.observation_validation='legacy_not_rechecked'; row.evidence_chart_available=false;
  row.point.confirmed_at=null; row.point.missing_conditions=['terminal_unit_locked'];
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.equal(ui.get('selected-detail').hidden,true); // The default list is primary-confirmed only.
  ui.get('point-status-filter').value='all';ui.get('point-status-filter').listeners.change();
  const chart=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(chart.pathname,'/');assert.equal(chart.searchParams.get('code'),row.code);
  assert.equal(chart.searchParams.get('layout'),'single');assert.equal(chart.searchParams.get('intervals'),'5');
  assert.equal(ui.get('chart-source').value,'live');assert.equal(ui.get('chart-source').disabled,true);
  assert.match(ui.get('decision-title').textContent,/买点尚未确认/);
  assert.match(ui.get('decision-detail').textContent,/观测/);
  assert.doesNotMatch(ui.get('decision-detail').textContent,/确认间隔/);
  assert.match(ui.get('stage-counts').textContent,/筛选已通过 0 · 条件待齐 1/);
  assert.ok(ui.get('evidence-missing').children.some(e=>e.textContent==='等待末段完成并锁定'));
  ui.get('stage-filter').value='confirmed';ui.get('stage-filter').listeners.change();
  assert.equal(ui.get('selected-detail').hidden,true);
  ui.get('stage-filter').value='forming';ui.get('stage-filter').listeners.change();
  assert.equal(ui.get('selected-detail').hidden,false);
});

test('a confirmed 5m chart point still waits when 1m confirmation is missing', async () => {
  const data=withCandidate(), row=data.candidates[0];
  row.stage='approaching'; row.point.status='confirmed';
  row.selection_missing_conditions=['lower_1m_confirmation'];
  row.nested_confirmation={state:'waiting',frequency:'1m',interval:{start_after:60,end_at:100},point:null};
  row.confirmation_segment={state:'in_progress',segment:{direction:'up'}};
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.match(ui.get('decision-title').textContent,/5m 买卖点已确认 · 区间内 1m 结构确认待齐/);
  assert.match(ui.get('decision-detail').textContent,/5m 已确认；1m 等待确认/);
  assert.match(ui.get('selected-tags').textContent,/已确认/);
  assert.match(ui.get('decision-detail').textContent,/后继向上线段进行中/);
  assert.doesNotMatch(ui.get('decision-title').textContent,/买点尚未确认/);
  assert.match(ui.get('stage-counts').textContent,/筛选已通过 0 · 条件待齐 1/);
  assert.ok(ui.get('evidence-missing').children.some(e=>e.textContent==='等待对应区间内的 1m 买卖点确认'));
});

test('nested evidence switches physical periods while retaining the main candidate identity', async () => {
  const data=withCandidate(), row=data.candidates[0];
  row.evidence_chart_available=true;
  row.nested_confirmation={state:'confirmed',frequency:'1m',interval:{start_after:60,end_at:100},
    point:{point_id:'lower',point_type:'1buy',anchor_at:90,available_at:150},audit:{confirmation_replay:true}};
  const ui=page(async url=>dispatch(data,url));await settle();
  ui.get('chart-source').value='evidence';ui.get('chart-source').listeners.change();
  assert.equal(new URL(ui.get('chart-frame').src,'http://local').searchParams.get('frequency'),'5m');
  const one=ui.periods.find(p=>p.dataset.frequency==='1m');
  assert.equal(one.disabled,false);
  assert.equal(ui.periods.find(p=>p.dataset.frequency==='30m').disabled,true);
  one.listeners.click();
  const url=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(url.searchParams.get('frequency'),'1m');
  assert.equal(url.searchParams.get('point'),'P');
  assert.equal(url.searchParams.get('source'),data.source);
});

test('unavailable markets do not label a current A-share candidate stale and period changes refresh it',async()=>{
  let latest=withCandidate();
  latest.status={...latest.status,cutoff_current:null,freshness_state:'partial',
    market_cutoffs:{a:{'5m':1000},fx:{'5m':null}},
    market_freshness:{a:{'5m':{state:'cutoff_current'}},fx:{'5m':{state:'unknown'}}},
    freshness_message:'已分别核对有有效截止时间的市场。'};
  latest.diagnostics={rejection_counts:{CONFIRMING_SEGMENT_COMPLETED:193}};
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.match(ui.get('decision-title').textContent,/本次筛选通过/);
  assert.match(ui.get('diagnostic-reasons').textContent,/CONFIRMING_SEGMENT_COMPLETED：193/);
  latest={...latest,status:{...latest.status,market_freshness:{...latest.status.market_freshness,a:{'5m':{state:'unknown',message:'calendar unavailable'}}}}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('decision-title').textContent,'本次结果待更新');
});

test('same-run freshness changes update selected details without resetting chart or review draft', async () => {
  let latest=withCandidate();
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.match(ui.get('decision-title').textContent,/本次筛选通过/);
  const chart=ui.get('chart-frame').src;
  ui.get('review-notes').value='unsaved notes';ui.get('review-form').listeners.input();
  latest={...latest,status:{...latest.status,cutoff_current:false,data_current:false,freshness_state:'stale'}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('decision-title').textContent,'本次结果待更新');
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('review-notes').value,'unsaved notes');
});

test('failed reviews retry on unchanged status without downloading the result again', async () => {
  let reads=0,results=0;
  const latest=withCandidate();
  const ui=page(async url=>{
    if(url.startsWith('/screening/reviews')){
      if(++reads===1)return response({error:'transient review error'},503);
      return response({latest:{candidate:{notes:'stored',saved_at:200,judgements:{disposition:'WATCH'}}},history:[]});
    }
    if(url.startsWith('/screening/workbench'))results++;
    return dispatch(latest,url);
  });await settle();
  assert.equal(reads,1);assert.equal(ui.get('page-message').textContent,'transient review error');
  await ui.timers[0]();await settle();
  assert.equal(reads,2);assert.equal(results,1);
  assert.equal(ui.get('review-notes').value,'stored');
  assert.equal(ui.get('page-message').textContent,'');
});

test('compact candidates load detail on selection and ignore late replies from another selection',async()=>{
  const latest=withCandidate();
  const first={...latest.candidates[0],id:'first',name:'First',detail_loaded:false};
  const second={...first,id:'second',name:'Second'};
  latest.candidates=[first,second];
  const replies=new Map();
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')){
      const id=new URL(url,'http://local').searchParams.get('id');
      return new Promise(resolve=>replies.set(id,resolve));
    }
    return dispatch(latest,url);
  });
  await settle();
  assert.match(ui.get('candidate-detail-state').textContent,/正在读取/);
  assert.equal(ui.get('review-fields').disabled,true);
  ui.get('signal-list').children[1].listeners.click();await settle();
  replies.get('second')(response({source:latest.source,candidate:{...second,detail_loaded:true}}));await settle();
  assert.match(ui.get('selected-title').textContent,/Second/);
  assert.equal(ui.get('review-fields').disabled,false);
  replies.get('first')(response({source:latest.source,candidate:{...first,detail_loaded:true}}));await settle();
  assert.match(ui.get('selected-title').textContent,/Second/);
  ui.get('signal-list').children[0].listeners.click();await settle();
  assert.match(ui.get('selected-title').textContent,/First/);
  assert.equal(ui.get('candidate-detail-state').textContent,'');
});

test('page navigation caps rendered cards and search still covers later pages',async()=>{
  const latest=withCandidate();
  const row=latest.candidates[0];
  latest.candidates=Array.from({length:121},(_,i)=>({...row,id:String(i),name:i===120?'UniqueTarget':'Stock '+i}));
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.equal(ui.get('signal-list').children.length,50);
  assert.match(ui.get('queue-page').textContent,/1 \/ 3/);
  ui.get('queue-next').listeners.click();await settle();
  assert.equal(ui.get('signal-list').children[0].dataset.id,'50');
  ui.get('queue-next').listeners.click();await settle();
  assert.equal(ui.get('signal-list').children.length,21);
  ui.get('signal-search').value='UniqueTarget';ui.get('signal-search').listeners.input();await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.equal(ui.get('signal-list').children[0].dataset.id,'120');
});

test('live rechecks invalidate only the affected cached detail and preserve review drafts',async()=>{
  let latest=withCandidate(),reads=0;
  latest.status.evidence_version='evidence';latest.status.result_reader_revision='reader';
  latest.status.live_recheck_version='pool-1';
  latest.candidates[0]={...latest.candidates[0],detail_loaded:false,selection_check_version:'check-1',latest_recheck_closed_at:300,latest_recheck_at:400};
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')){
      reads++;return response({source:latest.source,evidence_version:'evidence',result_reader_revision:'reader',candidate:{...latest.candidates[0],detail_loaded:true}});
    }
    return dispatch(latest,url);
  });await settle();
  assert.equal(reads,1);
  ui.get('review-notes').value='keep draft';ui.get('review-form').listeners.input();
  latest={...latest,status:{...latest.status,live_recheck_version:'another-period'}};
  await ui.timers[0]();await settle();
  assert.equal(reads,1);
  latest={...latest,status:{...latest.status,live_recheck_version:'pool-2'},candidates:[{...latest.candidates[0],selection_check_version:'check-2',latest_recheck_closed_at:600}]};
  await ui.timers[0]();await settle();
  assert.equal(reads,2);
  assert.equal(JSON.parse(ui.get('raw-evidence').textContent).latest_recheck_closed_at,600);
  assert.equal(ui.get('review-notes').value,'keep draft');
});

test('a delayed detail from the same point before recheck cannot overwrite its newer detail',async()=>{
  let latest=withCandidate();const replies=[];
  latest.status.live_recheck_version='pool-1';
  latest.candidates[0]={...latest.candidates[0],detail_loaded:false,selection_check_version:'check-1'};
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')){
      const candidate={...latest.candidates[0],detail_loaded:true};
      return new Promise(resolve=>replies.push(()=>resolve(response({source:latest.source,candidate}))));
    }
    return dispatch(latest,url);
  });await settle();
  latest={...latest,status:{...latest.status,live_recheck_version:'pool-2'},candidates:[{...latest.candidates[0],selection_check_version:'check-2'}]};
  await ui.timers[0]();await settle();
  assert.equal(replies.length,2);
  replies[1]();await settle();replies[0]();await settle();
  assert.equal(JSON.parse(ui.get('raw-evidence').textContent).selection_check_version,'check-2');
  assert.equal(ui.get('review-fields').disabled,false);
});

for(const field of ['evidence_version','result_reader_revision','selection_check_version']){
  test(`detail with a mismatched ${field} is never presented as the selected evidence`,async()=>{
    const latest=withCandidate();latest.candidates[0].detail_loaded=false;
    latest.status.evidence_version='old';latest.status.result_reader_revision='old';
    latest.candidates[0].selection_check_version='old';
    const ui=page(async url=>{
      if(url.startsWith('/screening/candidate?')){
        const payload={source:latest.source,evidence_version:'old',result_reader_revision:'old',candidate:{...latest.candidates[0],detail_loaded:true}};
        if(field==='selection_check_version')payload.candidate[field]='new';else payload[field]='new';
        return response(payload);
      }
      return dispatch(latest,url);
    });await settle();
    assert.match(ui.get('candidate-detail-state').textContent,/依据已更新/);
    assert.equal(ui.get('raw-evidence').textContent,'');
    assert.equal(ui.get('review-fields').disabled,true);
  });
}

test('switching to a pending detail immediately replaces the chart and clears previous evidence',async()=>{
  const latest=withCandidate();const first=latest.candidates[0];
  const second={...first,id:'second',code:'SH.600001',detail_loaded:false,point:{...first.point,anchor_price:21}};
  latest.candidates.push(second);
  const ui=page(async url=>url.startsWith('/screening/candidate?')?new Promise(()=>{}):dispatch(latest,url));
  await settle();assert.match(ui.get('chart-frame').src,/SH.600000/);
  assert.notEqual(ui.get('decision-detail').textContent,'');
  ui.get('signal-list').children[1].listeners.click();await settle();
  assert.match(ui.get('chart-frame').src,/SH.600001/);
  assert.equal(ui.get('anchor-price').textContent,'21.00');
  assert.equal(ui.get('decision-detail').textContent,'');
  assert.equal(ui.get('raw-evidence').textContent,'');
  assert.equal(ui.get('exit-price-detail').children.length,0);
  assert.equal(ui.get('review-fields').disabled,true);
});

test('quote refresh follows the visible page instead of the first page',async()=>{
  const latest=withCandidate(),row=latest.candidates[0],requested=[];
  latest.candidates=Array.from({length:80},(_,i)=>({...row,id:String(i),code:'SH.'+String(600000+i)}));
  const ui=page(async (url,options)=>{
    if(url==='/ticks'){
      requested.push(...JSON.parse(new URLSearchParams(options.body).get('codes')));
      return response({ticks:[]});
    }
    return dispatch(latest,url);
  });await settle();
  ui.get('queue-next').listeners.click();await settle();
  ui.get('auto-quotes').checked=true;
  await ui.timers[1]();await settle();
  assert.ok(requested.includes('SH.600050'));
  assert.ok(requested.includes('SH.600064'));
  assert.ok(!requested.includes('SH.600000'));
});

test('watchlist counts identify the selected group and every market and ignore late group replies',async()=>{
  const latest=withCandidate();let release;
  const stocks=Array.from({length:13},(_,i)=>({market:i<8?'a':'us',code:'stock-'+i,name:'Stock '+i}));
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'},{name:'我的持仓'},{name:'跨市场'}]);
    if(url.startsWith('/get_zixuan_stocks/a/')){
      const group=decodeURIComponent(url.split('/').pop());
      if(group==='我的关注')return response({data:stocks});
      if(group==='我的持仓')return new Promise(resolve=>{release=resolve;});
      return response({data:[{market:'currency_spot',code:'BTC/USDT',name:'Bitcoin'},{market:'fx',code:'FX.USD',name:'FX'}]});
    }
    return dispatch(latest,url);
  });await settle();
  assert.equal(ui.get('watch-count').textContent,'我的关注 · 13 个标的');
  assert.equal(ui.get('watch-market-counts').textContent,'A 股 8 · 美股 5');
  ui.get('watch-group').value='我的持仓';ui.get('watch-group').listeners.change();await settle();
  assert.equal(ui.get('watch-count').textContent,'我的持仓 · 正在读取…');
  assert.equal(ui.get('watch-list').children.length,0);
  ui.get('watch-group').value='跨市场';ui.get('watch-group').listeners.change();await settle();
  release(response({data:stocks}));await settle();
  assert.equal(ui.get('watch-count').textContent,'跨市场 · 2 个标的');
  assert.equal(ui.get('watch-market-counts').textContent,'外汇 1 · 数字货币现货 1');
  assert.doesNotMatch(ui.get('watch-count').textContent,/美股/);
  assert.match(ui.get('watch-list').children[0].children[0].children[0].textContent,/数字货币现货/);
});

test('watchlist fetch failure remains explicit when quotes refresh and differs from an empty group',async()=>{
  const latest=withCandidate();let fail=false;
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return fail?response({error:'group unavailable'},503):response({data:[]});
    if(url==='/ticks')return response({ticks:[]});
    return dispatch(latest,url);
  });await settle();
  assert.equal(ui.get('watch-count').textContent,'我的关注 · 0 个标的');
  assert.match(ui.get('watch-status').textContent,/暂无标的/);
  fail=true;ui.get('watch-refresh').listeners.click();await settle();
  assert.match(ui.get('watch-count').textContent,/读取失败/);
  ui.get('auto-quotes').checked=true;await ui.timers[1]();await settle();
  assert.equal(ui.get('watch-status').textContent,'group unavailable');
  assert.match(ui.get('watch-count').textContent,/读取失败/);
  assert.equal(ui.get('watch-quote-status').textContent,'');
});

test('watchlist quotes start before screening results and one slow market cannot block another',async()=>{
  const latest=withCandidate(),calls=[];let releaseUs;
  const ui=page(async(url,options)=>{
    if(url.startsWith('/screening/workbench'))return new Promise(()=>{});
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'a',code:'SH.600000'},{market:'us',code:'AAPL.US'}]});
    if(url==='/ticks'){
      const market=new URLSearchParams(options.body).get('market');calls.push(market);
      if(market==='us')return new Promise(resolve=>{releaseUs=resolve;});
      return response({ticks:[{code:'SH.600000',price:12,rate:1}]});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  assert.deepEqual(calls,['a','us']);
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].textContent,'+1.00%');
  ui.advance(15000);await ui.timers[1]();await settle();
  assert.deepEqual(calls,['a','us','a']);
  releaseUs(response({ticks:[{code:'AAPL.US',price:200,rate:-1}]}));await settle();
  assert.equal(ui.get('watch-list').children[1].children[1].children[1].textContent,'-1.00%');
});

test('all watchlist quotes are drained in bounded chunks and an old group cannot keep fetching',async()=>{
  const latest=result(),requested=[];let release,group='large';
  const stocks=Array.from({length:180},(_,i)=>({market:'a',code:'SH.'+String(600000+i)}));
  const ui=page(async(url,options)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'},{name:'新分组'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:group==='large'?stocks:[{market:'a',code:'SZ.000001'}]});
    if(url==='/ticks'){
      const codes=JSON.parse(new URLSearchParams(options.body).get('codes'));requested.push(codes);
      if(requested.length===2)return new Promise(resolve=>{release=resolve;});
      return response({ticks:codes.map(code=>({code,price:10,rate:1}))});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  assert.equal(requested.length,2);assert.equal(requested[0].length,80);assert.equal(requested[1].length,80);
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].textContent,'+1.00%');
  group='new';ui.get('watch-group').value='新分组';ui.get('watch-group').listeners.change();await settle();
  release(response({ticks:[]}));await settle();
  assert.deepEqual(requested[2],['SZ.000001']);assert.equal(requested.length,3);
});

test('watchlists beyond eighty receive quotes without clicking each stock',async()=>{
  const latest=result(),requested=[];
  const stocks=Array.from({length:100},(_,i)=>({market:'a',code:'SH.'+String(600000+i)}));
  const ui=page(async(url,options)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:stocks});
    if(url==='/ticks'){
      const codes=JSON.parse(new URLSearchParams(options.body).get('codes'));requested.push(codes);
      return response({ticks:codes.map(code=>({code,price:10,rate:-1}))});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  assert.deepEqual(requested.map(c=>c.length),[80,20]);
  assert.equal(new Set(requested.flat()).size,100);
  assert.equal(ui.get('watch-list').children[99].children[1].children[1].textContent,'-1.00%');
});

test('a failing middle quote batch cannot starve the tail and can recover independently',async()=>{
  const latest=result(),requested=[];let broken=true;
  const stocks=Array.from({length:180},(_,i)=>({market:'a',code:'SH.'+String(600000+i)}));
  const ui=page(async(url,options)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:stocks});
    if(url==='/ticks'){
      const codes=JSON.parse(new URLSearchParams(options.body).get('codes'));requested.push(codes);
      if(broken&&codes.includes('SH.600080'))return response({error:'this batch failed'},503);
      return response({ticks:codes.map(code=>({code,price:10,rate:1}))});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  assert.deepEqual(requested.map(c=>c.length),[80,80,20]);
  assert.equal(ui.get('watch-list').children[179].children[1].children[1].textContent,'+1.00%');
  assert.match(ui.get('watch-quote-status').textContent,/部分行情/);
  broken=false;ui.advance(15000);await ui.timers[1]();await settle();
  assert.equal(ui.get('watch-list').children[99].children[1].children[1].textContent,'+1.00%');
  assert.equal(ui.get('watch-quote-status').textContent,'');
});

test('provider rate limits pause a market without resetting its completed quote chunks',async()=>{
  const latest=result(),requested=[];let blocked=true;
  const stocks=Array.from({length:100},(_,i)=>({market:'a',code:'SH.'+String(600000+i)}));
  const ui=page(async(url,options)=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:stocks});
    if(url==='/ticks'){
      const codes=JSON.parse(new URLSearchParams(options.body).get('codes'));requested.push(codes);
      if(blocked&&codes.includes('SH.600080'))return response({error:{message:'rate limit',retry_after_seconds:5}},429);
      return response({ticks:codes.map(code=>({code,price:10,rate:1}))});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  await ui.timers[1]();await settle();assert.equal(requested.length,2);
  blocked=false;ui.advance(5000);await ui.timers[1]();await settle();
  assert.deepEqual(requested.map(c=>c.length),[80,20,20]);
  assert.equal(ui.get('watch-list').children[99].children[1].children[1].textContent,'+1.00%');
});

test('deferred quotes retry on the server clock and failure backoff cannot be bypassed',async()=>{
  const latest=withCandidate();let calls=0;
  const ui=page(async(url)=>{
    if(url.startsWith('/screening/workbench'))return new Promise(()=>{});
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:'AAPL.US'}]});
    if(url==='/ticks'){
      calls++;
      if(calls===1)return response({quote_state:'deferred',retry_after_seconds:2,ticks:[]});
      if(calls===2)return response({ok:false,error:{message:'provider cooldown',retry_after_seconds:30}},503);
      return response({ticks:[{code:'AAPL.US',price:100,rate:0}]});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  assert.equal(calls,1);
  assert.equal(ui.get('watch-quote-status').textContent,'行情加载中…');
  const retry=ui.timerDelays.indexOf(2000);assert.ok(retry>=0);
  ui.advance(2000);await ui.timers[retry]();await settle();
  assert.equal(calls,2);
  await ui.timers[1]();await settle();assert.equal(calls,2);
  const cooldown=ui.timerDelays.indexOf(30000);assert.ok(cooldown>=0);
  ui.advance(30000);await ui.timers[cooldown]();await settle();
  assert.equal(calls,3);
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].textContent,'0.00%');
});

test('new watchlist targets are fetched as soon as an old request completes without duplicates',async()=>{
  const latest=withCandidate(),requested=[];let release;
  const ui=page(async(url,options)=>{
    if(url.startsWith('/screening/workbench'))return new Promise(()=>{});
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'},{name:'新分组'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:decodeURIComponent(url).endsWith('我的关注')?'AAPL.US':'MSFT.US'}]});
    if(url==='/ticks'){
      const codes=JSON.parse(new URLSearchParams(options.body).get('codes'));requested.push(codes);
      if(requested.length===1)return new Promise(resolve=>{release=resolve;});
      return response({ticks:[{code:codes[0],price:100,rate:2}]});
    }
    return dispatch(latest,url);
  },undefined,true);await settle();
  ui.get('watch-group').value='新分组';ui.get('watch-group').listeners.change();await settle();
  assert.equal(requested.length,1);
  release(response({ticks:[{code:'AAPL.US',price:200,rate:-3}]}));await settle();
  assert.deepEqual(requested,[['AAPL.US'],['MSFT.US']]);
  assert.equal(ui.get('watch-count').textContent,'新分组 · 1 个标的');
  assert.equal(ui.get('watch-list').children[0].children[1].children[1].textContent,'+2.00%');
});

test('a review GET started before a save cannot overwrite the saved review', async () => {
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const latest=withCandidate();
  const ui=page(async (url, options)=>{
    if(url.startsWith('/screening/reviews')){
      if(options.method==='POST')return response({...JSON.parse(options.body),saved_at:300});
      return pending;
    }
    return dispatch(latest,url);
  });await settle();
  ui.get('review-notes').value='new notes';ui.get('review-form').listeners.input();
  await ui.get('review-form').listeners.submit({preventDefault(){}});
  release(response({latest:{},history:[]}));await settle();
  assert.equal(ui.get('review-notes').value,'new notes');
  assert.equal(ui.get('review-count').textContent,1);
});

test('editing a review while its previous version is saving preserves the newer draft', async () => {
  let release,posted;
  const pending=new Promise(resolve=>{release=resolve;});
  const latest=withCandidate();
  const ui=page(async (url,options)=>{
    if(url.startsWith('/screening/reviews')&&options.method==='POST'){
      posted=JSON.parse(options.body);return pending;
    }
    return dispatch(latest,url);
  });await settle();
  ui.get('review-notes').value='submitted observation';ui.get('review-form').listeners.input();
  const saving=ui.get('review-form').listeners.submit({preventDefault(){}});
  await settle();
  ui.get('review-notes').value='new observation during save';ui.get('review-form').listeners.input();
  release(response({...posted,saved_at:300}));await saving;await settle();
  assert.equal(ui.get('review-notes').value,'new observation during save');
  assert.match(ui.get('review-message').textContent,/未保存草稿/);
  await ui.get('refresh').listeners.click();await settle();
  assert.equal(ui.get('review-notes').value,'new observation during save');
});
