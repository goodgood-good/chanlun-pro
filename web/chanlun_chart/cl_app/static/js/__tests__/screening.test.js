'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'screening_workbench.js'), 'utf8');
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
  querySelectorAll() { return []; }
}
function page(fetch) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const [id, value] of Object.entries({scope:'all_a', codes:'', recent:'5', anchor:'20', 'max-gain':'10','chart-layout':'single'})) get(id).value = value;
  get('exclude-st').checked = true;
  const frequency = ['1m','5m','30m'].map(value => ({value,checked:value!=='1m'}));
  const point = ['1buy','2buy','3buy','1sell','2sell','3sell'].map(value => ({value,checked:true}));
  const periods = ['1m','5m','30m','d'].map(frequency => {
    const element = new Element(); element.dataset.frequency = frequency; return element;
  });
  get('chart-periods').append(...periods);
  const timers = [];
  vm.runInNewContext(source, {
    PointExitInfo: require('../point_exit_info.js'),
    document: {
      getElementById:get, createElement:() => new Element(), addEventListener() {},
      querySelector:s => s==='main' ? {dataset:{user:'tester'}} : {content:'test-csrf'},
      querySelectorAll:s => {
        if(s==='[data-frequency]')return periods;
        const inputs=s.includes('#screen-form [name=frequency]')?frequency:s.includes('#screen-form [name=point]')?point:[];
        return s.endsWith(':checked')?inputs.filter(i=>i.checked):inputs;
      },
    },
    fetch, URL, URLSearchParams, Date, Blob, AbortSignal, location:{origin:'http://local'},
    setTimeout:fn => timers.push(fn), clearTimeout:id=>{timers[id-1]=()=>{};},
  });
  return {get, timers, periods};
}
const response = (value, status=200) => ({ok:status===200, status, redirected:false, json:async () => value});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
function result(id='a', frequency='30m', status='completed') {
  return {source:id, candidates:[],sectors:[],point_counts:{},status:{
    run_id:id,status,total:1,completed:status==='completed'?1:0,
    source_current:true,cutoff_current:true,data_current:null,freshness_state:'cutoff_current',
    settings:{scope:'codes',codes:['SZ.000001'],frequencies:[frequency],point_types:['3buy'],
      recent_sessions:5,max_anchor_sessions:20,max_anchor_gain_pct:8,exclude_st:true},
  }};
}
function dispatch(latest, url) {
  if(url==='/screening/workbench') return response(latest);
  if(url==='/screening/status') return response(latest.status);
  if(url.startsWith('/screening/reviews')) return response({latest:{},history:[]});
  if(url==='/get_zixuan_groups/a') return response([]);
  throw new Error('Unexpected endpoint: '+url);
}

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
  assert.match(ui.get('freshness').textContent,/3 个确认线段已完成/);
  assert.match(ui.get('freshness').textContent,/2 个旧买点缺少确认线段证据/);
  assert.match(ui.get('stage-counts').textContent,/已确认 0/);
  assert.equal(requests.includes('/screening/start'),false);
});

test('waiting candidates use the shared analysis chart and never claim confirmation', async () => {
  const data=withCandidate(), row=data.candidates[0];
  row.stage='approaching'; row.observation_validation='legacy_not_rechecked'; row.evidence_chart_available=false;
  row.point.confirmed_at=null; row.point.missing_conditions=['terminal_unit_locked'];
  const ui=page(async url=>dispatch(data,url));await settle();
  const chart=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(chart.pathname,'/');assert.equal(chart.searchParams.get('code'),row.code);
  assert.equal(chart.searchParams.get('layout'),'single');assert.equal(chart.searchParams.get('intervals'),'5');
  assert.equal(ui.get('chart-source').value,'live');assert.equal(ui.get('chart-source').disabled,true);
  assert.match(ui.get('decision-title').textContent,/买点尚未确认/);
  assert.match(ui.get('decision-detail').textContent,/观测/);
  assert.doesNotMatch(ui.get('decision-detail').textContent,/确认间隔/);
  assert.match(ui.get('stage-counts').textContent,/已确认 0 · 形成与等待 1/);
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
  const ui=page(async url=>dispatch(data,url));await settle();
  assert.match(ui.get('decision-title').textContent,/5m 或 1m 尚缺确认/);
  assert.match(ui.get('decision-detail').textContent,/5m 已确认；1m 等待确认/);
  assert.match(ui.get('stage-counts').textContent,/已确认 0 · 形成与等待 1/);
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
    if(url==='/screening/workbench')results++;
    return dispatch(latest,url);
  });await settle();
  assert.equal(reads,1);assert.equal(ui.get('page-message').textContent,'transient review error');
  await ui.timers[0]();await settle();
  assert.equal(reads,2);assert.equal(results,1);
  assert.equal(ui.get('review-notes').value,'stored');
  assert.equal(ui.get('page-message').textContent,'');
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
