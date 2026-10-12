'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'screening_workbench.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../../../templates/early_screening.html'), 'utf8').replace(/<!--[\s\S]*?-->/g, '');
const templateIds = new Set([...template.matchAll(/\bid=["']([^"']+)["']/g)].map(match=>match[1]));
const {filterRows,normalizeViewState} = require('../screening_workbench.js');
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
  click() {
    if(this.disabled)return;
    if(this.type==='checkbox'){this.checked=!this.checked;this.indeterminate=false;this.listeners.change?.({target:this});}
    if(this.type==='radio'&&!this.checked){for(const sibling of this.radioGroup||[])sibling.checked=false;this.checked=true;this.listeners.change?.({target:this});}
    this.listeners.click?.({target:this});
  }
  setAttribute(name,value) {this.attributes[name]=value;}
  getAttribute(name) {return this.attributes[name]??null;}
  closest(selector) {
    let element=this;
    while(element){
      if(selector.startsWith('.')&&String(element.className||'').split(/\s+/).includes(selector.slice(1)))return element;
      element=element.parentElement;
    }
    return null;
  }
  querySelectorAll(selector) {
    if (!selector.startsWith('.')) return [];
    const name=selector.slice(1);
    return this.children.flatMap(child=>child instanceof Element?
      [...(String(child.className||'').split(/\s+/).includes(name)?[child]:[]),...child.querySelectorAll(selector)]:[]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}
function page(fetch, preferences, autoQuotes=false, strictTemplate=false, phone=false, sessionStorage=null, user='tester') {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  for (const [id, value] of Object.entries({scope:'all_a', codes:'', recent:'5', anchor:'20', workers:'12', 'max-gain':'10','chart-layout':'single',
    'screen-strategy':'5m_with_1m_confirmation','screen-frequency':'5m','screen-grade':'0'})) get(id).value = value;
  get('exclude-st').checked = true;
  get('auto-quotes').checked = autoQuotes;
  const frequency = ['1m','5m','30m'].map(value => ({value,checked:value!=='1m'}));
  const point = ['1buy','2buy','3buy','1sell','2sell','3sell'].map(value => ({value,checked:true}));
  const attribute=(tag,name)=>tag.match(new RegExp('\\b'+name+'=["\']([^"\']*)["\']'))?.[1];
  const signalCells=[...template.matchAll(/<input\b[^>]*>/g)].map(match=>match[0]).filter(tag=>attribute(tag,'data-signal-point')).map(tag=>{
    const element=new Element();element.type='checkbox';element.checked=/\bchecked\b/.test(tag);
    element.dataset.signalPoint=attribute(tag,'data-signal-point');
    element.id=attribute(tag,'id');
    element.setAttribute('aria-label',attribute(tag,'aria-label'));
    element.parentElement=new Element();element.parentElement.className='signal-choice';
    if(element.id)elements.set(element.id,element);
    return element;
  });
  const signalPeriods=[...template.matchAll(/<input\b[^>]*>/g)].map(match=>match[0]).filter(tag=>attribute(tag,'data-signal-period')).map(tag=>{
    const element=new Element();element.type=attribute(tag,'type');element.checked=/\bchecked\b/.test(tag);
    element.dataset.signalPeriod=attribute(tag,'data-signal-period');element.id=attribute(tag,'id');
    if(element.id)elements.set(element.id,element);return element;
  });
  for(const radio of signalPeriods)radio.radioGroup=signalPeriods;
  const signalPresets=[...template.matchAll(/<button\b[^>]*>/g)].map(match=>match[0]).filter(tag=>attribute(tag,'data-signal-preset')).map(tag=>{
    const element=new Element();element.dataset.signalPreset=attribute(tag,'data-signal-preset');element.id=attribute(tag,'id');
    if(element.id)elements.set(element.id,element);return element;
  });
  get('advanced-filters').open=false;
  get('sector-level').value='industry';
  get('sector-matches-only').type='checkbox';get('sector-matches-only').checked=true;
  get('include-previous').type='checkbox';get('include-previous').checked=true;
  const periods = ['1m','5m','30m','d'].map(frequency => {
    const element = new Element(); element.dataset.frequency = frequency; return element;
  });
  get('chart-periods').append(...periods);
  const timers = [], timerDelays=[];
  const clock={now:Date.now()};
  const exports=[];
  class PageURL extends URL {
    static createObjectURL(blob){exports.push(blob);return 'blob:screening-test';}
    static revokeObjectURL(){}
  }
  class PageDate extends Date { static now(){return clock.now;} }
  const body = new Element(), documentEvents = {};
  const sandbox={
    PointExitInfo: require('../point_exit_info.js'),
    document: {
      body, getElementById:id=>strictTemplate&&!templateIds.has(id)?null:get(id), createElement:() => new Element(),
      addEventListener(name, fn) { (documentEvents[name] ||= []).push(fn); },
      querySelector:s => s==='main' ? {dataset:{user}} : {content:'test-csrf'},
      querySelectorAll:s => {
        if(s==='[data-frequency]')return periods;
        if(s.includes('[data-signal-')) {
          const attributes=[...s.matchAll(/\[data-([\w-]+)(?:=["']([^"']*)["'])?\]/g)];
          return [...signalCells,...signalPeriods,...signalPresets].filter(element=>attributes.every(([,key,value])=>{
            const property=key.replace(/-([a-z])/g,(_match,letter)=>letter.toUpperCase());
            return element.dataset[property]!==undefined&&(value===undefined||element.dataset[property]===value);
          })&&(!s.includes(':checked')||element.checked));
        }
        const inputs=s.includes('#screen-form [name=frequency]')?frequency:s.includes('#screen-form [name=point]')?point:[];
        return s.endsWith(':checked')?inputs.filter(i=>i.checked):inputs;
      },
    },
    fetch, URL:PageURL, URLSearchParams, Date:PageDate, Blob, AbortSignal, location:{origin:'http://local'},
    AccountPreferences:preferences,WorkspaceDevice:{isPhone:phone},...(sessionStorage?{sessionStorage}:{}),
    setTimeout:(fn,delay) => {timerDelays.push(delay);return timers.push(fn);}, clearTimeout:id=>{timers[id-1]=()=>{};},
  };
  vm.runInNewContext(source,sandbox);
  return {get, timers, timerDelays, exports, advance:ms=>{clock.now+=ms;}, periods, signalCells,signalPeriods,signalPresets,body, documentEvents,
    setHidden:value=>{sandbox.document.hidden=value;for(const fn of documentEvents.visibilitychange||[])fn();}};
}
const response = (value, status=200) => ({ok:status>=200&&status<300, status, redirected:false, json:async () => value});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

test('noon time budget shows a partial stop and keeps already verified candidates visible',async()=>{
  const latest=withCandidate();
  Object.assign(latest.status,{status:'cancelled',stop_reason:'time_budget',deadline_kind:'noon',completed:6000,total:10000});
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.match(ui.get('run-status').textContent,/午间限时结束.*保留部分结果/);
  assert.doesNotMatch(ui.get('run-status').textContent,/本次筛选完成/);
  assert.match(ui.get('run-detail').textContent,/6000.*10000/);
  assert.match(ui.get('run-detail').textContent,/20 分钟.*共用预算/);
  assert.ok(displayedIds(ui).length>0);
});
const SIGNAL_PERIODS=['1m','5m','30m'];
const SIGNAL_POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const defaultSignals=()=>({signal_period:'5m',signal_points:[...SIGNAL_POINTS]});
const selectedSignals=ui=>({signal_period:ui.signalPeriods.find(radio=>radio.checked)?.dataset.signalPeriod,
  signal_points:ui.signalCells.filter(cell=>cell.checked).map(cell=>cell.dataset.signalPoint)});
const signalFields=filters=>({signal_period:filters.signal_period,signal_points:filters.signal_points});
const displayedIds=ui=>ui.get('signal-list').children.map(row=>row.dataset.id);
const sectorIds=ui=>ui.get('sector-list').children.map(card=>card.dataset.sectorId);
const elementText=element=>[element.textContent,...element.children.map(elementText)].filter(Boolean).join(' ');
function sectorCard(ui,id){const card=ui.get('sector-list').children.find(row=>row.dataset.sectorId===id);assert.ok(card,`sector card ${id} is not on the current page`);return card;}
function choose(ui,id,value){ui.get(id).value=value;ui.get(id).listeners.change?.({target:ui.get(id)});}
function setSignal(ui,point,checked=true){
  const cell=ui.signalCells.find(item=>item.dataset.signalPoint===point);
  assert.ok(cell,`missing actual template checkbox ${point}`);
  cell.checked=checked;cell.listeners.change({target:cell});
}
function chooseSignal(ui,period,points){
  setPeriod(ui,period);
  ui.get('signal-clear').click();
  for(const point of points)setSignal(ui,point);
}
function preset(ui,value){
  const button=ui.signalPresets.find(item=>item.dataset.signalPreset===value);
  assert.ok(button,`missing actual preset ${value}`);button.click();
}
function setPeriod(ui,frequency){
  const radio=ui.signalPeriods.find(item=>item.dataset.signalPeriod===frequency);
  assert.ok(radio,`missing actual period control ${frequency}`);radio.click();
}

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

test('memory throttling updates the running status without clearing displayed results',async()=>{
  let latest=withCandidate();latest.status={...latest.status,status:'running',resource_mode:'normal',effective_workers:14};
  const ui=page(async url=>dispatch(latest,url));await settle();
  latest={...latest,status:{...latest.status,resource_mode:'waiting',effective_workers:0}};
  await ui.timers[0]();await settle();
  assert.match(ui.get('run-detail').textContent,/内存保护.*暂缓新计算/);
  assert.equal(ui.get('signal-list').children.length,1);
  assert.equal(ui.get('cancel').disabled,false);
  latest={...latest,status:{...latest.status,resource_mode:'normal',effective_workers:4}};
  await ui.timers[0]();await settle();
  assert.doesNotMatch(ui.get('run-detail').textContent,/内存保护/);
  assert.equal(ui.get('signal-list').children.length,1);
});

test('an empty page continues checking a late previous cache even without new scan progress',async()=>{
  let latest=result('next','5m','running'),reads=0;
  latest.status.previous_results_loading=true;
  const ui=page(async url=>{
    if(url.startsWith('/screening/workbench'))reads++;
    return dispatch(latest,url);
  });await settle();
  const count=reads;
  latest={...latest,candidates:[{...withCandidate().candidates[0],refresh_state:'pending',result_source:'old'}],
    status:{...latest.status,previous_results_loading:false,previous_result_count:1}};
  await ui.timers[0]();await settle();
  assert.equal(reads,count+1);
  assert.equal(ui.get('signal-list').children.length,1);
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

for(const [field,before,after] of [['live_recheck_version','before','after'],['current_source_revision','old','new'],
  ['result_reader_revision','old-reader','new-reader'],['source_current',false,true]])
test('a review failure cannot clear successfully refreshed candidates after '+field+' changes',async()=>{
  let latest=withCandidate(),failReviews=false,resultReads=0,reviewReads=0;
  latest.status[field]=before;
  const ui=page(async url=>{
    if(url.startsWith('/screening/workbench'))resultReads++;
    if(url.startsWith('/screening/reviews')){
      reviewReads++;
      if(failReviews)return response({error:'review notes temporarily unavailable'},503);
    }
    return dispatch(latest,url);
  });await settle();
  const chart=ui.get('chart-frame').src;
  ui.get('review-notes').value='unsaved local note';ui.get('review-form').listeners.input();
  latest={...latest,status:{...latest.status,[field]:after},
    candidates:[{...latest.candidates[0],name:'freshly verified candidate'}]};
  failReviews=true;
  await ui.timers[0]();await settle();
  assert.equal(resultReads,2,'the new verified result response was installed');
  assert.equal(reviewReads,2,'only the following optional notes request failed');
  assert.equal(ui.get('signal-list').children.length,1);
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(ui.get('selected-detail').hidden,false);
  assert.match(ui.get('selected-title').textContent,/freshly verified candidate/);
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('review-notes').value,'unsaved local note');
  assert.equal(ui.get('page-message').textContent,'review notes temporarily unavailable');
  failReviews=false;
  await ui.timers[0]();await settle();
  assert.equal(resultReads,2,'unchanged verified results need no replacement fetch');
  assert.equal(reviewReads,3);
  assert.equal(ui.get('page-message').textContent,'');
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('review-notes').value,'unsaved local note');
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
  assert.equal(submitted.workers,16);
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

function crossPeriodCandidates() {
  const data=withCandidate(),base=data.candidates[0];
  data.status.settings={...data.status.settings,frequencies:['1m','5m','30m'],point_types:['1buy','2buy','3buy','1sell','2sell','3sell']};
  data.candidates=[['a1','SH.600000','1m','1buy'],['a5','SH.600000','5m','3sell'],['a30','SH.600000','30m','3buy'],
    ['b1','SH.600001','1m','1buy'],['b30','SH.600001','30m','3buy']].map(([id,code,frequency,point_type])=>
    ({...base,id,code,frequency,point:{...base.point,point_id:id,point_type,status:'confirmed'}}));
  return data;
}

for(const phone of [false,true])test('single period and six point controls apply immediately without hidden modes '+(phone?'phone':'desktop'),async()=>{
  const latest=crossPeriodCandidates(),calls=[];
  const ui=page(async(url,options)=>{calls.push({url,method:options?.method||'GET'});return dispatch(latest,url);},undefined,false,true,phone);
  await settle();
  assert.equal(ui.signalCells.length,6);assert.equal(ui.signalPeriods.length,3);assert.equal(ui.signalPresets.length,4);
  assert.deepEqual(selectedSignals(ui),defaultSignals());assert.deepEqual(displayedIds(ui),['a5']);
  assert.ok(ui.signalPeriods.every(radio=>radio.type==='radio'));
  assert.match(template,/<section\b[^>]*class="[^"]*chart-filters/);
  assert.equal(ui.get('include-previous').checked,true);
  setPeriod(ui,'1m');assert.deepEqual(displayedIds(ui),['a1','b1']);
  preset(ui,'none');setSignal(ui,'1buy');
  assert.deepEqual(selectedSignals(ui),{signal_period:'1m',signal_points:['1buy']});
  setPeriod(ui,'30m');assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:['1buy']});
  assert.deepEqual(displayedIds(ui),[],'changing period does not silently reselect point types');
  setSignal(ui,'3buy');assert.deepEqual(displayedIds(ui),['a30','b30']);
  assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:['1buy','3buy']});
  preset(ui,'sell');assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:['1sell','2sell','3sell']});
  setPeriod(ui,'5m');assert.deepEqual(displayedIds(ui),['a5']);
  ui.get('reset').click();assert.deepEqual(selectedSignals(ui),defaultSignals());assert.deepEqual(displayedIds(ui),['a5']);
  ui.get('include-previous').click();preset(ui,'none');ui.get('reset').click();
  assert.deepEqual(selectedSignals(ui),defaultSignals());assert.equal(ui.get('include-previous').checked,true);
  assert.ok(calls.every(call=>call.method==='GET'),'display controls never launch scans or mutate server data');
  assert.doesNotMatch(template,/data-signal-frequency=|data-signal-period-all=|data-signal-period-clear=/);
  for(const id of ['signal-all-1m','signal-all-5m','signal-all-30m','signal-period-filter','signal-type-filter',
    'signal-period-custom','signal-type-custom','signal-custom-details','signal-preset-period'])
    assert.equal(templateIds.has(id),false,`retired control ${id} remains`);
});

test('each preset replaces only point types and precisely matches its pressed state in every period',async()=>{
  const latest=crossPeriodCandidates(),ui=page(async url=>dispatch(latest,url));await settle();
  for(const period of SIGNAL_PERIODS)for(const type of ['all','buy','sell','none']){
    chooseSignal(ui,period,['1buy','3sell']);preset(ui,type);
    const points=type==='all'?SIGNAL_POINTS:type==='none'?[]:SIGNAL_POINTS.filter(point=>point.endsWith(type));
    assert.deepEqual(selectedSignals(ui),{signal_period:period,signal_points:points});
    for(const button of ui.signalPresets)assert.equal(button.getAttribute('aria-pressed'),String(button.dataset.signalPreset===type));
    assert.deepEqual(displayedIds(ui),latest.candidates.filter(row=>row.frequency===period&&points.includes(row.point.point_type)).map(row=>row.id));
  }
});

test('individual type adjustments preserve one active radio and clear unmatched preset highlights',async()=>{
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url));await settle();
  preset(ui,'buy');setSignal(ui,'2buy',false);
  assert.ok(ui.signalPresets.every(button=>button.getAttribute('aria-pressed')==='false'));
  assert.deepEqual(selectedSignals(ui),{signal_period:'5m',signal_points:['1buy','3buy']});
  for(const period of ['30m','1m','5m']){
    setPeriod(ui,period);assert.equal(ui.signalPeriods.filter(radio=>radio.checked).length,1);
    assert.deepEqual(selectedSignals(ui),{signal_period:period,signal_points:['1buy','3buy']});
  }
  setSignal(ui,'3sell');assert.deepEqual(displayedIds(ui),['a5']);
  setSignal(ui,'1buy',false);setSignal(ui,'3buy',false);setSignal(ui,'3sell',false);
  assert.equal(ui.get('signal-clear').getAttribute('aria-pressed'),'true');
});

test('switching physical periods preserves explicit empty points until a point or preset is chosen',async()=>{
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url));await settle();preset(ui,'none');
  for(const period of ['1m','30m','5m']){
    setPeriod(ui,period);assert.deepEqual(selectedSignals(ui),{signal_period:period,signal_points:[]});
    assert.deepEqual(displayedIds(ui),[]);
  }
  setSignal(ui,'3sell');assert.deepEqual(displayedIds(ui),['a5']);
  setPeriod(ui,'30m');assert.deepEqual(displayedIds(ui),[]);preset(ui,'buy');assert.deepEqual(displayedIds(ui),['a30','b30']);
  assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:['1buy','2buy','3buy']});
});

test('current, retained and watchlist rows retain independent visibility and count labels',async()=>{
  const latest=withCandidate(),current=latest.candidates[0];
  latest.candidates=[current,
    {...current,id:'old-pending',refresh_state:'pending',result_source:'older',evidence_available:false},
    {...current,id:'old-limited',refresh_state:'limited',result_source:'older',evidence_available:false},
    {...current,id:'unverified',evidence_available:false},
    {...current,id:'wrong-source',result_source:'different'},
    {...current,id:'inactive',stage:'closed'}];
  const watches=[{market:'a',code:current.code,name:'watch same symbol'},{market:'us',code:'AAPL.US',name:'watch only'}];
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:watches});
    return dispatch(latest,url);
  });await settle();
  assert.deepEqual(displayedIds(ui),['candidate','old-pending','old-limited']);
  ui.get('include-previous').click();assert.deepEqual(displayedIds(ui),['candidate']);
  ui.get('signal-clear').click();assert.deepEqual(displayedIds(ui),[]);
  choose(ui,'origin-filter','all');
  assert.deepEqual(displayedIds(ui),['watch:a:SH.600000','watch:us:AAPL.US'],'watch placeholders never borrow signal predicates');
  setSignal(ui,'3buy');ui.get('include-previous').click();
  assert.deepEqual(displayedIds(ui),['candidate','old-pending','old-limited','watch:a:SH.600000','watch:us:AAPL.US']);
  const counts=ui.get('visible-breakdown').textContent;
  assert.match(counts,/(?:本轮|当前)[^0-9]*1|1[^0-9]*(?:本轮|当前)/);
  assert.match(counts,/(?:历史|上次|旧结果)[^0-9]*2|2[^0-9]*(?:历史|上次|旧结果)/);
  assert.match(counts,/关注[^0-9]*2|2[^0-9]*关注/);
  choose(ui,'origin-filter','watchlist');assert.deepEqual(displayedIds(ui),['watch:a:SH.600000','watch:us:AAPL.US']);
});

test('watch-only disables signal controls without discarding their canonical state and watch navigation resets all',async()=>{
  const latest=crossPeriodCandidates(),ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:'AAPL.US',name:'Apple'}]});
    return dispatch(latest,url);
  },undefined,false,true);await settle();
  ui.get('signal-clear').click();setSignal(ui,'3sell');ui.get('include-previous').click();
  choose(ui,'origin-filter','watchlist');
  const before=selectedSignals(ui);
  assert.deepEqual(displayedIds(ui),['watch:us:AAPL.US']);
  assert.ok(ui.signalCells.every(control=>control.disabled));
  for(const control of [...ui.signalPeriods,...ui.signalPresets,ui.get('include-previous')])assert.equal(control.disabled,true);
  assert.equal(ui.get('reset').disabled,false,'full reset remains available for a nondefault watch-only scope');
  for(const control of [...ui.signalPeriods,...ui.signalPresets])control.click();
  assert.deepEqual(selectedSignals(ui),before,'disabled bulk commands must not mutate saved choices');
  choose(ui,'origin-filter','all');
  assert.deepEqual(selectedSignals(ui),before);assert.equal(ui.get('include-previous').checked,false);
  assert.ok(ui.signalCells.every(control=>!control.disabled));
  assert.deepEqual(displayedIds(ui),['a5','watch:us:AAPL.US']);
  ui.get('watch-list').children[0].click();await settle();
  assert.deepEqual(selectedSignals(ui),defaultSignals());
  assert.equal(ui.get('include-previous').checked,true);assert.equal(ui.signalPeriods.filter(radio=>radio.checked).length,1);
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
});

test('the always-visible summary describes selected pairs and advanced filters',async()=>{
  const latest=crossPeriodCandidates();
  latest.sectors=[{id:'technology',name:'科技',symbol_count:2,signal_count:5,member_count:10}];
  latest.candidates=latest.candidates.map(row=>({...row,sectors:['technology'],confirmation_age_sessions:1,gain_pct:1,
    point:{...row.point,center_ordinal:1}}));
  const ui=page(async url=>dispatch(latest,url),undefined,false,true);await settle();
  const summary=()=>ui.get('active-filter-summary').textContent;
  assert.ok(templateIds.has('active-filter-summary'));
  assert.ok(template.indexOf('id="active-filter-summary"')<template.indexOf('id="signal-list"'));
  assert.doesNotMatch(template.match(/<[^>]*\bid="active-filter-summary"[^>]*>/)[0],/\bhidden\b/);
  ui.get('signal-clear').click();setPeriod(ui,'30m');setSignal(ui,'3buy');setSignal(ui,'3sell');
  for(const text of ['三卖','30m','三买'])assert.ok(summary().includes(text));
  assert.doesNotMatch(summary(),/5m/);
  choose(ui,'market-filter','a');choose(ui,'center-order-filter','first');choose(ui,'age-filter','3');
  choose(ui,'gain-filter','3');choose(ui,'review-filter','pending');choose(ui,'sort-filter','distance');
  ui.get('signal-search').value='SH.600000';ui.get('signal-search').listeners.input();
  sectorCard(ui,'technology').click();
  for(const text of ['A 股','SH.600000','科技','首中枢','3 个交易日','3%','待复核','靠近拐点'])
    assert.ok(summary().includes(text),`active summary must retain ${text}`);
  assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:['3buy','3sell']});
  ui.get('reset').click();
  assert.deepEqual(selectedSignals(ui),defaultSignals());
  assert.doesNotMatch(summary(),/SH\.600000|科技|首中枢|3%|待复核|靠近拐点/);
});

for(const action of ['period','preset','cell','include-previous','reset','sector','all-sectors'])
test(`${action} returns the queue to page one and selects a card on that page`,async()=>{
  const latest=withCandidate(),base=latest.candidates[0];
  latest.status.settings={...latest.status.settings,frequencies:['1m','5m','30m']};
  latest.candidates=Array.from({length:121},(_,index)=>({...base,id:String(index),name:'Stock '+index,sectors:['technology']}));
  if(action==='period')latest.candidates.push(...latest.candidates.map(row=>({...row,id:'one'+row.id,frequency:'1m'})));
  latest.sectors=[{id:'technology',name:'科技',symbol_count:121,signal_count:121,member_count:121}];
  const ui=page(async url=>dispatch(latest,url));await settle();
  if(action==='reset')choose(ui,'market-filter','a');
  ui.get('queue-next').click();ui.get('queue-next').click();await settle();
  assert.match(ui.get('queue-page').textContent,/3 \/ 3/);
  assert.match(ui.get('selected-title').textContent,/Stock 100/);
  if(action==='period')setPeriod(ui,'1m');
  else if(action==='preset')preset(ui,'buy');
  else if(action==='cell')setSignal(ui,'1buy',false);
  else if(action==='include-previous')ui.get('include-previous').click();
  else if(action==='reset')ui.get('reset').click();
  else if(action==='sector')sectorCard(ui,'technology').click();
  else ui.get('sector-all').click();
  await settle();
  assert.match(ui.get('queue-page').textContent,/1 \/ 3/);
  assert.equal(ui.get('signal-list').children[0].dataset.id,action==='period'?'one0':'0');
  const active=ui.get('signal-list').children.filter(card=>card.classList.contains('active'));
  assert.equal(active.length,1);
  const activeIndex=active[0].dataset.id.replace(/^one/,'');
  assert.ok(Number(activeIndex)<50,'selected card must belong to the rendered page');
  assert.match(ui.get('selected-title').textContent,new RegExp(`Stock ${activeIndex} ·`));
});

for(const repeated of ['preset','period','cell'])test('repeating an unchanged '+repeated+' preserves the later page and selected chart',async()=>{
  const latest=withCandidate(),row=latest.candidates[0];
  latest.candidates=Array.from({length:121},(_,index)=>({...row,id:String(index),name:'Stock '+index}));
  const ui=page(async url=>dispatch(latest,url));await settle();
  if(repeated==='preset')preset(ui,'buy');
  else chooseSignal(ui,'5m',['1buy','3buy']);
  ui.get('queue-next').click();ui.get('queue-next').click();await settle();
  const chart=ui.get('chart-frame').src,selected=ui.get('selected-title').textContent,checks=selectedSignals(ui);
  if(repeated==='preset')preset(ui,'buy');
  else if(repeated==='period')setPeriod(ui,'5m');
  else setSignal(ui,'3buy',true);
  assert.deepEqual(selectedSignals(ui),checks);assert.match(ui.get('queue-page').textContent,/3 \/ 3/);
  assert.equal(ui.get('selected-title').textContent,selected);assert.equal(ui.get('chart-frame').src,chart);
  ui.get('advanced-filters').open=true;ui.get('advanced-filters').open=false;
  assert.deepEqual(selectedSignals(ui),checks);assert.match(ui.get('queue-page').textContent,/3 \/ 3/);
});

test('uncomputed choices identify absent coverage while retained rows are separately opt-in context',async()=>{
  let latest=crossPeriodCandidates();
  latest.status.settings.frequencies=['1m','5m'];latest.candidates=latest.candidates.filter(row=>row.frequency!=='30m');
  const ui=page(async url=>dispatch(latest,url));await settle();
  ui.get('signal-clear').click();setPeriod(ui,'30m');setSignal(ui,'3buy');
  assert.deepEqual(displayedIds(ui),[]);
  assert.match(ui.get('signal-coverage-message').textContent,/30m 三买/);
  assert.match(ui.get('active-filter-summary').textContent,/三买/);
  latest={...latest,candidates:[...latest.candidates,{...crossPeriodCandidates().candidates[2],refresh_state:'pending',result_source:'older'}]};
  await ui.get('refresh').listeners.click();await settle();
  assert.deepEqual(displayedIds(ui),['a30']);
  assert.match(ui.get('visible-breakdown').textContent,/历史|上次|旧结果/);
  ui.get('include-previous').click();assert.deepEqual(displayedIds(ui),[]);
});

test('coverage follows the single selected period and its point choices',async()=>{
  const latest=crossPeriodCandidates(),calls=[];
  latest.status.settings.frequencies=['30m'];latest.status.settings.point_types=['3buy'];
  latest.candidates=latest.candidates.filter(row=>row.frequency==='30m');
  const ui=page(async(url,options)=>{calls.push({url,method:options?.method||'GET'});return dispatch(latest,url);});await settle();
  ui.get('signal-clear').click();setPeriod(ui,'30m');setSignal(ui,'3buy');setPeriod(ui,'30m');setSignal(ui,'3sell');
  assert.deepEqual(displayedIds(ui),['a30','b30']);
  const coverage=ui.get('signal-coverage-message').textContent;
  assert.match(coverage,/30m 三卖/);assert.doesNotMatch(coverage,/30m 三买/);
  chooseSignal(ui,'1m',['3buy','3sell']);
  assert.deepEqual(selectedSignals(ui),{signal_period:'1m',signal_points:['3buy','3sell']});
  assert.deepEqual(displayedIds(ui),[]);
  assert.match(ui.get('signal-coverage-message').textContent,/1m/);
  assert.doesNotMatch(ui.get('signal-coverage-message').textContent,/30m/);
  setSignal(ui,'3sell',false);
  assert.match(ui.get('signal-coverage-message').textContent,/1m 三买/);
  assert.match(ui.get('active-filter-summary').textContent,/三买/);
  setPeriod(ui,'5m');preset(ui,'all');assert.match(ui.get('signal-coverage-message').textContent,/5m 全部类型/);
  setPeriod(ui,'30m');preset(ui,'buy');assert.match(ui.get('signal-coverage-message').textContent,/30m.*一买.*二买/);
  assert.ok(calls.every(call=>call.method==='GET'));
});

test('export freezes the canonical period points and history while subsequent controls keep working',async()=>{
  const latest=crossPeriodCandidates();latest.compact=true;let release;
  const ui=page(async url=>url==='/screening/workbench?compact=0'?new Promise(resolve=>{release=resolve;}):dispatch(latest,url));await settle();
  ui.get('signal-clear').click();setPeriod(ui,'30m');setSignal(ui,'3buy');
  const exportJob=ui.get('export').listeners.click();await settle();
  setSignal(ui,'3sell');preset(ui,'buy');chooseSignal(ui,'1m',['1buy']);ui.get('include-previous').click();
  assert.equal(ui.get('export').disabled,true,'changing displayed filters cannot reopen an inflight export');
  release(response({...latest,compact:false}));await exportJob;
  const exported=JSON.parse(await ui.exports[0].text());
  assert.deepEqual(signalFields(exported.filters),{signal_period:'30m',signal_points:['3buy']});
  assert.equal(exported.filters.include_previous,true);
  for(const old of ['signal_selection','period_points','period_match','frequency','point'])assert.equal(Object.hasOwn(exported.filters,old),false);
  assert.deepEqual(exported.candidates.map(row=>row.id),['a30','b30']);
  assert.deepEqual(displayedIds(ui),['a1','b1']);
});

test('export rechecks selected rows against fresh full results of the same run',async()=>{
  const latest=crossPeriodCandidates();latest.compact=true;
  const full={...latest,compact:false,candidates:latest.candidates.filter(row=>row.id!=='a30')};
  const ui=page(async url=>url==='/screening/workbench?compact=0'?response(full):dispatch(latest,url));await settle();
  ui.get('signal-clear').click();setPeriod(ui,'30m');setSignal(ui,'3buy');
  assert.deepEqual(displayedIds(ui),['a30','b30']);
  await ui.get('export').listeners.click();
  const exported=JSON.parse(await ui.exports[0].text());
  assert.deepEqual(exported.candidates.map(row=>row.id),['b30']);
});

test('export includes every filtered page and rejects a new run rather than mixing snapshots',async()=>{
  const latest=withCandidate(),row=latest.candidates[0];latest.compact=true;
  latest.candidates=Array.from({length:121},(_,index)=>({...row,id:String(index),name:'Stock '+index}));
  let source=latest.source;
  const ui=page(async url=>url==='/screening/workbench?compact=0'?response({...latest,source,compact:false}):dispatch(latest,url));await settle();
  ui.get('queue-next').click();ui.get('queue-next').click();
  await ui.get('export').listeners.click();
  assert.equal(JSON.parse(await ui.exports[0].text()).candidates.length,121);
  source='changed-run';await ui.get('export').listeners.click();
  assert.equal(ui.exports.length,1);assert.match(ui.get('page-message').textContent,/批次已更新/);
  assert.equal(ui.get('export').disabled,false);
});

test('a new scan keeps the previous candidate and chart while its response is pending',async()=>{
  let latest=withCandidate(),release;
  latest.status.settings={...latest.status.settings,scope:'all_a',frequencies:['5m']};
  let started=false;
  const pending=new Promise(resolve=>{release=resolve;});
  const ui=page(async (url)=>{
    if(url==='/screening/start'){
      started=true;latest={...latest,source:'next',status:{...latest.status,run_id:'next',status:'running',completed:0}};
      return response(latest.status);
    }
    if(started&&url.startsWith('/screening/workbench'))return pending;
    return dispatch(latest,url);
  });await settle();
  const chart=ui.get('chart-frame').src;
  const starting=ui.get('screen-form').listeners.submit({preventDefault(){}});await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.match(ui.get('signal-list').querySelector('.queue-refresh').textContent,/上次结果.*待本轮更新/);
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('signal-count').textContent,0);
  assert.equal(ui.get('review-fields').disabled,true);
  const old={...latest.candidates[0],refresh_state:'pending',result_source:'a'};
  const fresh={...old,id:'fresh',code:'SH.600001',name:'new result',refresh_state:undefined,result_source:'next'};
  release(response({...latest,candidates:[fresh,old],status:{...latest.status,completed:1,previous_result_count:1,current_result_count:1}}));
  await starting;await settle();
  assert.equal(ui.get('signal-list').children.length,2);
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(ui.get('signal-list').querySelectorAll('.queue-refresh').length,1);
});

test('an automatic new run and failed fetch retain labelled previous results until replacement',async()=>{
  let latest=withCandidate(),fail=false;
  latest.status.settings={...latest.status.settings,scope:'all_a',frequencies:['5m']};
  const ui=page(async url=>fail&&url.startsWith('/screening/workbench')
    ?response({error:'temporary result read failure'},503):dispatch(latest,url));await settle();
  latest={...latest,source:'next',status:{...latest.status,run_id:'next',status:'running',completed:0}};fail=true;
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.match(ui.get('decision-title').textContent,/上次结果/);
  assert.equal(ui.get('review-fields').disabled,true);
  fail=false;latest={...latest,candidates:[],status:{...latest.status,status:'completed',completed:1}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('signal-list').children.length,0);
  assert.equal(ui.get('selected-detail').hidden,true);
});

test('reopening a running scan displays server-retained rows without requesting current evidence',async()=>{
  const latest=withCandidate();latest.status={...latest.status,status:'running',previous_result_count:1,current_result_count:0,results_loading:true,results_available:false};
  latest.candidates[0]={...latest.candidates[0],refresh_state:'pending',result_source:'old',detail_loaded:true};
  const ui=page(async url=>dispatch(latest,url));await settle();
  assert.equal(ui.get('signal-list').children.length,1);
  assert.match(ui.get('selected-tags').textContent,/上次结果/);
  assert.equal(ui.get('chart-source').disabled,true);
  assert.equal(ui.get('review-fields').disabled,true);
  assert.match(ui.get('page-message').textContent,/逐步替换/);
});

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
  assert.equal(ui.get('visible-count').textContent,'4 条');
  ui.get('lower-status-filter').value='confirmed';ui.get('lower-status-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'2 条');
  ui.get('center-order-filter').value='first';ui.get('center-order-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'1 条');
  ui.get('point-status-filter').value='all';ui.get('point-status-filter').listeners.change();
  ui.get('lower-status-filter').value='all';ui.get('lower-status-filter').listeners.change();
  ui.get('center-order-filter').value='all';ui.get('center-order-filter').listeners.change();
  assert.equal(ui.get('visible-count').textContent,'4 条');
});

test('current-run statistics exclude both pending and limited retained third-class rows',async()=>{
  const latest=withCandidate(),current=latest.candidates[0];
  latest.status.settings.alert_mode='native_center_signals';
  latest.status.previous_result_count=2;
  latest.candidates=[current,
    {...current,id:'old-pending',code:'SH.600001',refresh_state:'pending',result_source:'previous'},
    {...current,id:'old-limited',code:'SH.600002',refresh_state:'limited',result_source:'previous',point:{...current.point,point_type:'3sell'}}];
  const ui=page(async url=>dispatch(latest,url),undefined,false,true);await settle();
  assert.equal(ui.get('signal-list').children.length,3,'retained context may still appear in the default queue');
  assert.equal(ui.get('symbol-count').textContent,1);
  assert.equal(ui.get('signal-count').textContent,1);
  assert.match(ui.get('third-breakdown').textContent,/三类买卖点提醒 1 条/);
  assert.match(ui.get('signal-breakdown').textContent,/2 条上次结果/);
  const stats=template.match(/<section class="summary"[\s\S]*?<\/section>/)[0];
  assert.match(stats,/本轮/);
  chooseSignal(ui,'5m',['1buy']);
  assert.equal(ui.get('signal-list').children.length,0);
  assert.equal(ui.get('signal-count').textContent,1,'display filters do not rewrite whole-run statistics');
  assert.match(ui.get('third-breakdown').textContent,/三类买卖点提醒 1 条/);
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
  const base={market:'all',origin:'screening',...defaultSignals(),include_previous:true,stage:'all',review:'all',sector:'',query:'',
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
  assert.equal(ui.get('selected-detail').hidden,false); // Unconfirmed candidates are included by default.
  ui.get('point-status-filter').value='all';ui.get('point-status-filter').listeners.change();
  const chart=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(chart.pathname,'/');assert.equal(chart.searchParams.get('code'),row.code);
  assert.equal(chart.searchParams.get('layout'),'single');assert.equal(chart.searchParams.get('intervals'),'5');
  assert.equal(ui.get('chart-source').value,'live');assert.equal(ui.get('chart-source').disabled,true);
  assert.match(ui.get('decision-title').textContent,/未确认预警.*三买尚未确认/);
  assert.match(ui.get('decision-detail').textContent,/预警/);
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

test('phone queue uses 25 rows and returning from its chart preserves the selected page',async()=>{
  const latest=withCandidate(),row=latest.candidates[0];
  latest.candidates=Array.from({length:60},(_,i)=>({...row,id:String(i),name:'Stock '+i}));
  const ui=page(async url=>dispatch(latest,url),{getItem:()=>'{"chart_layout_type":"four"}'},false,false,true);
  await settle();assert.equal(ui.get('chart-layout').value,'single');
  assert.equal(ui.get('signal-list').children.length,25);
  ui.get('queue-next').listeners.click();await settle();
  assert.equal(ui.get('signal-list').children[0].dataset.id,'25');
  ui.get('signal-list').children[0].listeners.click();await settle();
  assert.equal(ui.get('workspace').attributes['data-mobile-view'],'chart');
  assert.equal(ui.get('mobile-chart-tab').attributes['aria-selected'],'true');
  const chart=ui.get('chart-frame').src;
  ui.get('mobile-back-to-list').listeners.click();
  assert.equal(ui.get('workspace').attributes['data-mobile-view'],'list');
  assert.equal(ui.get('signal-list').children[0].dataset.id,'25');
  assert.equal(ui.get('chart-frame').src,chart);
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

test('same-group watch refresh follows the selected target when eighty new rows move it to another page',async()=>{
  const latest=result(),target={market:'a',code:'SH.600099',name:'Selected target'};
  let stocks=[target],groupReads=0;
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/')){groupReads++;return response({data:stocks});}
    return dispatch(latest,url);
  });await settle();
  ui.get('watch-list').children[0].click();await settle();
  const id='watch:a:SH.600099',chart=ui.get('chart-frame').src;
  assert.match(ui.get('queue-page').textContent,/1 \/ 1/);
  assert.equal(ui.get('signal-list').children.find(row=>row.dataset.id===id).classList.contains('active'),true);
  stocks=[...Array.from({length:80},(_,index)=>({market:'a',code:'SH.'+String(600000+index),name:'Added '+index})),target];
  ui.get('watch-refresh').click();await settle();
  assert.equal(groupReads,2);
  assert.equal(ui.get('watch-group').value,'我的关注');
  assert.match(ui.get('queue-page').textContent,/2 \/ 2/);
  assert.equal(ui.get('selected-title').textContent,'Selected target · SH.600099');
  assert.equal(ui.get('chart-frame').src,chart);
  assert.deepEqual(ui.get('signal-list').children.filter(row=>row.classList.contains('active')).map(row=>row.dataset.id),[id]);
});

test('failed same-group refresh retains loaded members and counts with an explicit stale label',async()=>{
  const data=withCandidate(),session=sessionFixture();let fail=false;
  const stocks=[{market:'a',code:'SH.600099',name:'Target'},{market:'us',code:'AAPL.US',name:'Apple'}];
  saveView(session,{origin:'watchlist'},'我的关注');session.setItem('screening-selection:tester:a','watch:a:SH.600099');
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return fail?response({error:'temporary group failure'},503):response({data:stocks});
    return dispatch(data,url);
  },undefined,false,true,false,session);await settle();
  assert.deepEqual(displayedIds(ui),['watch:a:SH.600099','watch:us:AAPL.US']);
  assert.equal(ui.get('watch-market-counts').textContent,'A 股 1 · 美股 1');
  const selected=ui.get('selected-title').textContent,view=session.getItem(viewKey('tester'));
  fail=true;ui.get('watch-refresh').click();await settle();
  assert.equal(ui.get('watch-status').textContent,'temporary group failure');
  assert.match(ui.get('watch-count').textContent,/读取失败.*上次 2 个标的/);
  assert.equal(ui.get('watch-market-counts').textContent,'A 股 1 · 美股 1');
  assert.equal(ui.get('watch-list').children.length,2);assert.deepEqual(displayedIds(ui),['watch:a:SH.600099','watch:us:AAPL.US']);
  assert.equal(ui.get('selected-title').textContent,selected);assert.equal(session.getItem(viewKey('tester')),view);
  fail=false;ui.get('watch-refresh').click();await settle();
  assert.equal(ui.get('watch-count').textContent,'我的关注 · 2 个标的');assert.doesNotMatch(ui.get('watch-status').textContent,/failure/);
});

test('watchlist navigation clears stale age gain lower-confirmation and point-status refinements',async()=>{
  const latest=withCandidate();
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:'AAPL.US',name:'Apple'}]});
    return dispatch(latest,url);
  });await settle();
  for(const [id,value] of [['age-filter','0'],['gain-filter','1'],['lower-status-filter','confirmed'],['point-status-filter','forming']]) {
    ui.get(id).value=value;ui.get(id).listeners.change();
  }
  assert.equal(ui.get('signal-list').children.length,0);
  ui.get('watch-list').children[0].click();await settle();
  for(const id of ['age-filter','gain-filter','lower-status-filter'])assert.equal(ui.get(id).value,'all');
  assert.equal(ui.get('point-status-filter').value,'all');
  assert.equal(ui.get('origin-filter').value,'all');
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  assert.equal(ui.get('signal-list').children.find(row=>row.dataset.id==='watch:us:AAPL.US').classList.contains('active'),true);
});

for(const phone of [false,true])
for(const filters of [[['risk-filter','avoid']],[['higher-context-filter','aligned']],[['risk-filter','avoid'],['higher-context-filter','aligned']]])
test(`${phone?'phone':'desktop'} watchlist clicks clear ${filters.map(([id])=>id).join(' and ')} and keep the requested stock`,async()=>{
  const latest=withCandidate();
  latest.candidates[0].operational_macd_risk={risk_filter_state:'basic_avoid_on_selected_period'};
  latest.candidates[0].higher_context={schema:'native-period-link-v1',category:'aligned'};
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:'AAPL.US',name:'Apple'}]});
    return dispatch(latest,url);
  },undefined,false,true,phone);await settle();
  for(const [id,value] of filters){ui.get(id).value=value;ui.get(id).listeners.change();}
  assert.match(ui.get('selected-title').textContent,/SH\.600000/);
  ui.get('watch-list').children[0].listeners.click();await settle();
  for(const [id] of filters)assert.equal(ui.get(id).value,'all');
  assert.equal(ui.get('origin-filter').value,'all');
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  const chart=new URL(ui.get('chart-frame').src,'http://local');
  assert.equal(chart.searchParams.get('code'),'AAPL.US');
  assert.equal(chart.searchParams.get('market'),'us');
  assert.equal(ui.get('signal-list').children.find(row=>row.dataset.id==='watch:us:AAPL.US').classList.contains('active'),true);
  if(phone)assert.equal(ui.get('workspace').attributes['data-mobile-view'],'chart');
  ui.get('refresh').listeners.click();await settle();
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  assert.equal(ui.get('chart-frame').src,chart.pathname+chart.search);
});

for(const phone of [false,true])
for(const initial of ['pending','failed'])
test(`${phone?'phone':'desktop'} watchlist chart opens while initial results are ${initial} and survives their arrival`,async()=>{
  let latest=withCandidate(),reads=0,release;
  const ui=page(async url=>{
    if(url.startsWith('/screening/workbench')&&++reads===1)
      return initial==='pending'?new Promise(resolve=>{release=resolve;}):response({error:'results unavailable'},503);
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'us',code:'AAPL.US',name:'Apple'}]});
    return dispatch(latest,url);
  },undefined,false,true,phone);await settle();
  if(initial==='failed')assert.equal(ui.get('page-message').textContent,'results unavailable');
  assert.doesNotThrow(()=>ui.get('watch-list').children[0].listeners.click());await settle();
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  assert.equal(ui.get('selected-detail').hidden,false);
  assert.equal(ui.get('review-fields').disabled,true);
  assert.equal(ui.get('chart-source').value,'live');
  assert.equal(ui.get('chart-source').disabled,true);
  assert.equal(ui.get('exit-price-sources').children.length,0);
  assert.equal(ui.get('confirmation-exit-detail').children.length,0);
  ui.periods.find(button=>button.dataset.frequency==='30m').listeners.click();
  const chart=ui.get('chart-frame').src,params=new URL(chart,'http://local').searchParams;
  assert.equal(params.get('code'),'AAPL.US');
  assert.equal(params.get('market'),'us');
  assert.equal(params.get('frequency'),'30m');
  if(initial==='pending')release(response(latest));
  else await ui.timers[0]();
  await settle();
  assert.equal(ui.get('page-message').textContent,'');
  assert.equal(ui.get('signal-count').textContent,1);
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(ui.get('signal-list').children.find(row=>row.dataset.id==='watch:us:AAPL.US').classList.contains('active'),true);
  if(phone)assert.equal(ui.get('workspace').attributes['data-mobile-view'],'chart');
  latest={...latest,source:'next',status:{...latest.status,run_id:'next'}};
  await ui.timers[0]();await settle();
  assert.equal(ui.get('selected-title').textContent,'Apple · AAPL.US');
  assert.equal(ui.get('chart-frame').src,chart);
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

function compactDetailCase(){
  const data=withCandidate();data.compact=true;
  data.status={...data.status,evidence_version:'evidence',result_reader_revision:'reader',live_recheck_version:'live'};
  data.candidates[0]={...data.candidates[0],detail_loaded:false,candidate_evidence_version:'proof'};
  return data;
}
const detailResult=data=>({source:data.source,evidence_version:data.status.evidence_version,
  result_reader_revision:data.status.result_reader_revision,candidate:{...data.candidates[0],detail_loaded:true}});
function sessionFixture(){
  const values=new Map(),reads=[],writes=[];
  return {values,reads,writes,getItem:key=>{reads.push(key);return values.has(key)?values.get(key):null;},
    setItem:(key,value)=>{writes.push({key,value:String(value)});values.set(key,String(value));},removeItem:key=>values.delete(key)};
}
function pagedCandidates(){
  const data=withCandidate(),base=data.candidates[0];
  data.candidates=Array.from({length:120},(_,i)=>({...base,id:'n'+i,name:'n'+i,code:'SH.'+(600000+i),
    point:{...base.point,point_id:'p'+i,status:'confirmed'}}));
  return data;
}

test('temporary compact detail failure retries after backoff on unchanged status and restores review',async()=>{
  const data=compactDetailCase();let online=false,reads=0;
  const calls=[];
  const ui=page(async(url,options)=>{
    calls.push({url,method:options?.method});
    if(url.startsWith('/screening/candidate?')){reads++;return online?response(detailResult(data)):response({error:'temporary offline'},503);}
    return dispatch(data,url);
  },undefined,false,true);await settle();
  assert.equal(reads,1);assert.equal(ui.get('review-fields').disabled,true);
  assert.match(ui.get('candidate-detail-state').textContent,/temporary offline.*自动重试/);
  online=true;ui.advance(1999);await ui.timers[0]();await settle();
  assert.equal(reads,1,'unchanged polling respects the retry deadline');
  ui.advance(1);await ui.timers[0]();await settle();
  assert.equal(reads,2);assert.equal(ui.get('review-fields').disabled,false);
  assert.equal(ui.get('candidate-detail-state').textContent,'');
  assert.equal(calls.filter(r=>r.method==='POST').length,0);
});

test('consecutive detail errors increase backoff and ordinary rerenders cannot bypass it',async()=>{
  const data=compactDetailCase();let reads=0;
  const ui=page(async url=>url.startsWith('/screening/candidate?')?(reads++,response({error:'unavailable'},503)):dispatch(data,url));await settle();
  ui.advance(2000);await ui.timers[0]();await settle();assert.equal(reads,2);
  ui.advance(4999);ui.get('signal-search').value='fixture';ui.get('signal-search').listeners.input();await settle();
  await ui.timers[0]();await settle();assert.equal(reads,2);
  ui.advance(1);await ui.timers[0]();await settle();assert.equal(reads,3);
  ui.advance(14999);await ui.timers[0]();await settle();assert.equal(reads,3);
  ui.advance(1);await ui.timers[0]();await settle();assert.equal(reads,4);
});

test('a terminal missing detail never loops automatically but explicit selection can retry',async()=>{
  const data=compactDetailCase();let missing=true,reads=0;
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')){reads++;return missing?response({error:'candidate missing'},404):response(detailResult(data));}
    return dispatch(data,url);
  });await settle();
  for(let i=0;i<3;i++){ui.advance(60000);await ui.timers[0]();await settle();}
  assert.equal(reads,1);assert.match(ui.get('candidate-detail-state').textContent,/不会自动重复请求/);
  missing=false;ui.get('signal-list').children[0].listeners.click();await settle();
  assert.equal(reads,2);assert.equal(ui.get('review-fields').disabled,false);
});

test('a new candidate evidence identity is not held behind an old terminal detail error',async()=>{
  let data=compactDetailCase(),reads=0;
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?'))return ++reads===1?response({error:'candidate missing'},404):response(detailResult(data));
    return dispatch(data,url);
  });await settle();
  data={...data,status:{...data.status,evidence_version:'new-evidence'},
    candidates:[{...data.candidates[0],candidate_evidence_version:'new-proof'}]};
  await ui.timers[0]();await settle();
  assert.equal(reads,2);assert.equal(ui.get('review-fields').disabled,false);
});

test('same-key detail retry remains singular while pending and hidden pages do not retry',async()=>{
  const data=compactDetailCase();let reads=0,release;
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?'))return ++reads===1?response({error:'offline'},503):new Promise(resolve=>{release=resolve;});
    return dispatch(data,url);
  });await settle();
  ui.advance(60000);ui.setHidden(true);await ui.timers[0]();await settle();assert.equal(reads,1);
  ui.setHidden(false);await ui.timers[0]();await settle();assert.equal(reads,2);
  ui.advance(60000);await ui.timers[0]();await settle();assert.equal(reads,2);
  ui.get('signal-list').children[0].listeners.click();await settle();assert.equal(reads,2);
  release(response(detailResult(data)));await settle();assert.equal(ui.get('review-fields').disabled,false);
});

test('a late old-source detail error cannot poison the new selection retry state',async()=>{
  let data=compactDetailCase(),release;
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')) {
      if(new URL(url,'http://local').searchParams.get('source')==='a')return new Promise(resolve=>{release=resolve;});
      return response(detailResult(data));
    }
    return dispatch(data,url);
  });await settle();
  data={...data,source:'next',status:{...data.status,run_id:'next'},candidates:[{...data.candidates[0],id:'next-point',name:'next'}]};
  await ui.timers[0]();await settle();
  assert.match(ui.get('selected-title').textContent,/next/);assert.equal(ui.get('review-fields').disabled,false);
  release(response({error:'late old error'},503));await settle();
  assert.equal(ui.get('candidate-detail-state').textContent,'');assert.equal(ui.get('review-fields').disabled,false);
});

for(const phone of [false,true])test('same-run reload restores a saved later-page candidate then respects active filtering '+(phone?'phone':'desktop'),async()=>{
  const data=pagedCandidates(),session=sessionFixture();
  const first=page(async url=>dispatch(data,url),undefined,false,true,phone,session);await settle();
  first.get('queue-next').click();first.get('queue-next').click();await settle();
  const saved=phone?'n50':'n100';
  assert.equal(session.getItem('screening-selection:tester:a'),saved);
  const ui=page(async url=>dispatch(data,url),undefined,false,true,phone,session);await settle();
  assert.match(ui.get('queue-page').textContent,/^3 \/ /);
  assert.ok(ui.get('selected-title').textContent.startsWith(saved+' ·'));
  assert.equal(ui.get('signal-list').children.find(card=>card.classList.contains('active')).dataset.id,saved);
  ui.get('queue-prev').click();await settle();assert.match(ui.get('queue-page').textContent,/^2 \/ /);
  ui.get('signal-search').value='SH.600000';ui.get('signal-search').listeners.input();await settle();
  assert.match(ui.get('queue-page').textContent,/^1 \/ 1/);assert.match(ui.get('selected-title').textContent,/^n0 ·/);
  ui.get('reset').click();await settle();assert.match(ui.get('queue-page').textContent,/^1 \/ /);
  assert.match(ui.get('selected-title').textContent,/^n0 ·/);
});

test('late committed prefixes can restore the saved candidate without overwriting its identity early',async()=>{
  const all=pagedCandidates(),session=sessionFixture();session.setItem('screening-selection:tester:a','n100');
  let data={...all,candidates:all.candidates.slice(0,20),status:{...all.status,status:'running',results_loading:true,results_available:true,completed:20}};
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.equal(session.getItem('screening-selection:tester:a'),'n100');
  data=all;await ui.timers[0]();await settle();
  assert.match(ui.get('queue-page').textContent,/^3 \/ 3/);assert.match(ui.get('selected-title').textContent,/^n100 ·/);
});

test('a user filter during prefix loading cancels deferred selection restoration',async()=>{
  const all=pagedCandidates(),session=sessionFixture();session.setItem('screening-selection:tester:a','n100');
  let data={...all,candidates:all.candidates.slice(0,20),status:{...all.status,status:'running',results_loading:true,results_available:true,completed:20}};
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  ui.get('signal-search').value='SH.600000';ui.get('signal-search').listeners.input();await settle();
  assert.match(ui.get('selected-title').textContent,/^n0 ·/);
  data=all;await ui.timers[0]();await settle();
  assert.match(ui.get('queue-page').textContent,/^1 \/ 1/);assert.match(ui.get('selected-title').textContent,/^n0 ·/);
});

test('expired saved IDs and another run never redirect a new page to an unrelated later row',async()=>{
  const data=pagedCandidates(),session=sessionFixture();session.setItem('screening-selection:tester:a','missing');
  session.setItem('screening-selection:tester:other','n100');
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.match(ui.get('queue-page').textContent,/^1 \/ 3/);assert.match(ui.get('selected-title').textContent,/^n0 ·/);
});

test('HTML 404 detail response stops automatic retries and explicit refresh can recover',async()=>{
  const data=compactDetailCase();let missing=true,reads=0;
  const ui=page(async url=>{
    if(url.startsWith('/screening/candidate?')) {
      reads++;
      return missing?{ok:false,status:404,redirected:false,json:async()=>{throw SyntaxError('HTML error page');}}:response(detailResult(data));
    }
    return dispatch(data,url);
  });await settle();
  assert.match(ui.get('candidate-detail-state').textContent,/不会自动重复请求/);
  for(let i=0;i<3;i++){ui.advance(60000);await ui.timers[0]();await settle();}
  assert.equal(reads,1);
  missing=false;ui.get('refresh').click();await settle();
  assert.equal(reads,2);assert.equal(ui.get('review-fields').disabled,false);
});

test('manual selection is saved even while its compact detail is unavailable',async()=>{
  const data=compactDetailCase(),session=sessionFixture();
  data.candidates.push({...data.candidates[0],id:'second',name:'Second'});
  const ui=page(async url=>url.startsWith('/screening/candidate?')?response({error:'offline'},503):dispatch(data,url),
    undefined,false,true,false,session);await settle();
  ui.get('signal-list').children[1].click();await settle();
  assert.equal(session.getItem('screening-selection:tester:a'),'second');
  const restored=page(async url=>url.startsWith('/screening/candidate?')?response({error:'offline'},503):dispatch(data,url),
    undefined,false,true,false,session);await settle();
  assert.match(restored.get('selected-title').textContent,/Second/);
  assert.equal(restored.get('review-fields').disabled,true);
});

test('a saved watchlist selection excluded by default scope cannot block a running candidate prefix',async()=>{
  const all=pagedCandidates(),session=sessionFixture();session.setItem('screening-selection:tester:a','watch:us:TSLA.US');
  const data={...all,candidates:all.candidates.slice(0,20),status:{...all.status,status:'running',results_loading:true,results_available:true,completed:20}};
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.match(ui.get('selected-title').textContent,/^n0 ·/);
  assert.equal(session.getItem('screening-selection:tester:a'),'n0');
});

const viewKey=user=>'screening-view:v4:'+encodeURIComponent(user);
function saveView(session,filters={},watch_group='',user='tester'){
  session.setItem(viewKey(user),JSON.stringify({version:4,filters,watch_group}));
}
const readView=(session,user='tester')=>JSON.parse(session.getItem(viewKey(user)));

test('unconfirmed native signals are visible by default and their status filter survives reload',async()=>{
  const latest=withCandidate(),preview=JSON.parse(JSON.stringify(latest.candidates[0]));
  preview.id='native-preview';preview.stage='approaching';preview.point.status='approaching';
  preview.point.confirmed_at=null;preview.point.formal_point_issued=false;preview.observation_validation='checked';
  preview.point.evidence_scope='native_center_signal';
  latest.candidates.push(preview);
  latest.status.settings={alert_mode:'native_center_signals',frequencies:['5m']};
  const session=sessionFixture(),ui=page(async url=>dispatch(latest,url),undefined,false,true,false,session);
  await settle();assert.equal(ui.get('point-status-filter').value,'all');
  assert.equal(displayedIds(ui).length,2);assert.match(ui.get('stage-counts').textContent,/未确认预警 1 条/);
  choose(ui,'point-status-filter','forming');
  assert.deepEqual(displayedIds(ui),['native-preview']);assert.match(ui.get('active-filter-summary').textContent,/仅未确认/);
  const restored=page(async url=>dispatch(latest,url),undefined,false,true,false,session);await settle();
  assert.equal(restored.get('point-status-filter').value,'forming');
  assert.deepEqual(displayedIds(restored),['native-preview']);
  restored.get('reset').click();assert.equal(restored.get('point-status-filter').value,'all');
  assert.equal(displayedIds(restored).length,2);
});

test('the old hidden confirmation-only default migrates to all while preserving period and point types',()=>{
  const migrated=normalizeViewState({version:2,filters:{signal_period:'30m',signal_points:['3buy'],point_status:'confirmed'}});
  assert.equal(migrated.filters.point_status,'all');
  assert.deepEqual(signalFields(migrated.filters),{signal_period:'30m',signal_points:['3buy']});
  const explicit=normalizeViewState({version:3,filters:{point_status:'confirmed'}});
  assert.equal(explicit.filters.point_status,'confirmed');
});

test('stored views normalize only visible filters and distinguish an empty selection from a missing one',()=>{
  const defaults=normalizeViewState(null);
  for(const invalid of [null,{},[],{version:9,filters:{}},{version:2,filters:[]},{version:2,filters:'all'}])
    assert.deepEqual(normalizeViewState(invalid),defaults);
  const view=normalizeViewState({version:2,watch_group:'  长期关注  ',filters:{query:'  SH.600000  ',sector:' tech ',market:'bad',
    origin:'watchlist',review:'reviewed',center_order:'first',age:'3',gain:'1',sort:'distance',include_previous:false,
    signal_period:'30m',signal_points:['3sell','1buy','1buy','bad'],signal_selection:{'5m':['3buy']},
    stage:'closed',risk:'blocked',point_status:'all',lower_status:'missing',higher:'missing',point:'1sell',frequency:'d'}});
  assert.equal(view.watch_group,'长期关注');assert.equal(view.filters.query,'SH.600000');assert.equal(view.filters.sector,'tech');
  assert.equal(view.filters.market,'all');assert.equal(view.filters.origin,'watchlist');assert.equal(view.filters.review,'reviewed');
  assert.deepEqual(signalFields(view.filters),{signal_period:'30m',signal_points:['1buy','3sell']});
  assert.equal('signal_selection' in view.filters,false);
  for(const key of ['stage','risk','point_status','lower_status','higher'])assert.equal(view.filters[key],defaults.filters[key]);
  assert.equal(view.filters.include_previous,false);assert.equal('point' in view.filters,false);
  assert.deepEqual(signalFields(normalizeViewState({version:2,filters:{}}).filters),defaultSignals());
  assert.deepEqual(signalFields(normalizeViewState({version:2,filters:{signal_points:[]}}).filters),{signal_period:'5m',signal_points:[]});
});

test('view persistence is account scoped and background refresh does not rewrite it',async()=>{
  const session=sessionFixture(),data=crossPeriodCandidates(),alice='alice/甲',bob='bob';
  const first=page(async url=>dispatch(data,url),undefined,false,true,false,session,alice);await settle();
  chooseSignal(first,'1m',['1buy']);choose(first,'market-filter','a');
  first.get('include-previous').click();
  assert.deepEqual(signalFields(readView(session,alice).filters),{signal_period:'1m',signal_points:['1buy']});
  assert.equal(readView(session,alice).version,4);
  const saved=session.getItem(viewKey(alice)),writes=session.writes.filter(write=>write.key===viewKey(alice)).length;
  await first.timers[0]();await settle();first.get('refresh').click();await settle();
  assert.equal(session.getItem(viewKey(alice)),saved);
  assert.equal(session.writes.filter(write=>write.key===viewKey(alice)).length,writes,'background reads must not persist a second snapshot');
  const other=page(async url=>dispatch(data,url),undefined,false,true,false,session,bob);await settle();
  assert.deepEqual(selectedSignals(other),defaultSignals());assert.equal(other.get('market-filter').value,'all');
  assert.equal(session.getItem(viewKey(bob)),null);
  const restored=page(async url=>dispatch(data,url),undefined,false,true,false,session,alice);await settle();
  assert.deepEqual(displayedIds(restored),['a1','b1']);assert.equal(restored.get('market-filter').value,'a');
  assert.equal(restored.get('include-previous').checked,false);assert.equal(restored.get('advanced-filter-summary').textContent,'更多条件 · 2');
});

for(const [label,legacy,expected,ids] of [
  ['single',{'1m':['1buy']},{signal_period:'1m',signal_points:['1buy']},['a1','b1']],
  ['prefer five',{'1m':['1buy'],'5m':['3sell'],'30m':['3buy']},{signal_period:'5m',signal_points:['3sell']},['a5']],
  ['prefer thirty',{'1m':['1buy'],'30m':['3buy']},{signal_period:'30m',signal_points:['3buy']},['a30','b30']],
  ['empty',{}, {signal_period:'5m',signal_points:[]},[]],
  ['missing',undefined,defaultSignals(),['a5']],
])test('a version one view migrates to one period: '+label,async()=>{
  const session=sessionFixture(),oldKey='screening-view:v1:tester';
  const old=JSON.stringify({version:1,filters:{...(legacy===undefined?{}:{signal_selection:legacy}),market:'a',include_previous:false},watch_group:''});
  session.setItem(oldKey,old);
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(ui),expected);assert.deepEqual(displayedIds(ui),ids);
  assert.equal(ui.get('market-filter').value,'a');assert.equal(ui.get('include-previous').checked,false);
  assert.equal(session.reads.filter(key=>key===oldKey).length,1);assert.equal(session.getItem(viewKey('tester')),null,'background restoration never rewrites preferences');
  ui.get('include-previous').click();assert.equal(readView(session).version,4);
  assert.deepEqual(signalFields(readView(session).filters),expected);assert.equal('signal_selection' in readView(session).filters,false);
  assert.equal(session.getItem(oldKey),old,'migration must not destroy the old snapshot');
  session.reads.length=0;
  const reopened=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(reopened),expected);assert.equal(session.reads.includes(oldKey),false,'a saved v2 takes precedence without consulting v1');
});

for(const raw of ['', '{broken',JSON.stringify({version:9,filters:{}})])
test('an existing invalid v2 view falls back to defaults without reviving an old v1: '+raw,async()=>{
  const session=sessionFixture(),oldKey='screening-view:v1:tester';
  session.setItem(oldKey,JSON.stringify({version:1,filters:{signal_selection:{'1m':['1buy']}}}));session.setItem(viewKey('tester'),raw);
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(ui),defaultSignals());assert.deepEqual(displayedIds(ui),['a5']);
  assert.equal(session.reads.includes(oldKey),false);
});

test('switching an empty filter still saves its physical period and restores the empty selection',async()=>{
  const session=sessionFixture(),data=crossPeriodCandidates();
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  preset(ui,'none');setPeriod(ui,'30m');
  assert.deepEqual(signalFields(readView(session).filters),{signal_period:'30m',signal_points:[]});
  const reopened=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(reopened),{signal_period:'30m',signal_points:[]});assert.deepEqual(displayedIds(reopened),[]);
  preset(reopened,'all');assert.deepEqual(displayedIds(reopened),['a30','b30']);
});

test('anonymous pages neither restore nor persist browser storage',async()=>{
  const session=sessionFixture();saveView(session,{signal_points:[]},'','');session.reads.length=0;session.writes.length=0;
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session,'');await settle();
  setPeriod(ui,'1m');ui.get('reset').click();
  assert.deepEqual(session.reads,[]);assert.deepEqual(session.writes,[]);
  assert.deepEqual(displayedIds(ui),['a5']);
});

for(const stored of ['{broken',JSON.stringify({version:9,filters:{signal_points:[]}}),JSON.stringify({version:2,filters:[]})])
test('a malformed saved view safely restores defaults: '+stored,async()=>{
  const session=sessionFixture();session.setItem(viewKey('tester'),stored);
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(ui),defaultSignals());assert.equal(ui.get('reset').disabled,true);
  assert.equal(ui.get('advanced-filter-summary').textContent,'更多条件');assert.equal(ui.get('market-filter').value,'all');
  preset(ui,'none');preset(ui,'all');assert.deepEqual(displayedIds(ui),['a5']);
});

for(const storage of [null,{getItem(){throw Error('denied');},setItem(){throw Error('quota');}}])
test('filtering and reset survive '+(storage?'throwing':'missing')+' session storage',async()=>{
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,storage);await settle();
  chooseSignal(ui,'30m',['3buy']);assert.deepEqual(displayedIds(ui),['a30','b30']);
  ui.get('reset').click();assert.deepEqual(selectedSignals(ui),defaultSignals());assert.equal(ui.get('reset').disabled,true);
});

test('explicitly empty restored point types stay empty until the shared empty-state reset is used',async()=>{
  const session=sessionFixture();saveView(session,{signal_period:'30m',signal_points:[]});
  const ui=page(async url=>dispatch(crossPeriodCandidates(),url),undefined,false,true,false,session);await settle();
  assert.deepEqual(displayedIds(ui),[]);assert.deepEqual(selectedSignals(ui),{signal_period:'30m',signal_points:[]});
  assert.equal(ui.get('queue-reset-filters').hidden,false);assert.equal(ui.get('advanced-filter-summary').textContent,'更多条件');
  ui.get('queue-reset-filters').click();assert.deepEqual(displayedIds(ui),['a5']);
  assert.equal(ui.get('queue-reset-filters').hidden,true);assert.equal(ui.get('reset').disabled,true);
  assert.deepEqual(signalFields(readView(session).filters),defaultSignals());
});

test('genuinely empty unfiltered data has no misleading reset action',async()=>{
  const data=withCandidate();data.candidates=[];
  const ui=page(async url=>dispatch(data,url),undefined,false,true);await settle();
  assert.deepEqual(displayedIds(ui),[]);assert.equal(ui.get('queue-reset-filters').hidden,true);assert.equal(ui.get('reset').disabled,true);
  choose(ui,'market-filter','us');assert.equal(ui.get('queue-reset-filters').hidden,false);
  ui.get('queue-reset-filters').click();assert.equal(ui.get('queue-reset-filters').hidden,true);
});

for(const phone of [false,true])test('restored filters apply before selecting the correct later page '+(phone?'phone':'desktop'),async()=>{
  const data=pagedCandidates(),base=data.candidates[0],session=sessionFixture();
  data.candidates=Array.from({length:240},(_,i)=>({...base,id:'n'+i,name:'n'+i,code:'SH.'+(600000+i),
    frequency:i%2?'1m':'5m',sectors:['tech'],confirmation_age_sessions:1,gain_pct:1}));
  data.sectors=[{id:'tech',name:'科技',symbol_count:240,signal_count:240}];
  saveView(session,{signal_period:'5m',signal_points:['3buy'],market:'a',sector:'tech',age:'3',query:'SH.'});
  session.setItem('screening-selection:tester:a','n200');
  const ui=page(async url=>dispatch(data,url),undefined,false,true,phone,session);await settle();
  assert.match(ui.get('queue-page').textContent,phone?/^5 \/ 5/:/^3 \/ 3/);
  assert.match(ui.get('selected-title').textContent,/^n200 ·/);
  assert.equal(ui.get('signal-list').children.find(card=>card.classList.contains('active')).dataset.id,'n200');
  assert.equal(ui.get('signal-search').value,'SH.');assert.equal(ui.get('market-filter').value,'a');assert.equal(ui.get('age-filter').value,'3');
  assert.match(ui.get('active-filter-summary').textContent,/科技/);assert.equal(ui.get('advanced-filter-summary').textContent,'更多条件 · 3');
});

test('reset clears visible and hidden refinements, closes advanced conditions, and survives reload',async()=>{
  const session=sessionFixture(),data=crossPeriodCandidates();
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  for(const [id,value] of [['market-filter','us'],['origin-filter','all'],['review-filter','reviewed'],['center-order-filter','first'],
    ['age-filter','3'],['gain-filter','3'],['sort-filter','distance'],['risk-filter','blocked'],['point-status-filter','all'],['lower-status-filter','missing'],['higher-context-filter','unverified']])choose(ui,id,value);
  ui.get('signal-search').value='missing';ui.get('signal-search').listeners.input();ui.get('include-previous').click();
  ui.get('signal-clear').click();ui.get('advanced-filters').open=true;
  assert.equal(ui.get('advanced-filter-summary').textContent,'更多条件 · 9','the visible linkage condition counts; hidden legacy predicates do not');
  ui.get('reset').click();
  assert.equal(ui.get('advanced-filters').open,false);
  assert.equal(ui.get('reset').disabled,true);assert.equal(ui.get('advanced-filter-summary').textContent,'更多条件');
  assert.deepEqual(readView(session).filters,normalizeViewState(null).filters);
  assert.deepEqual(displayedIds(ui),['a5']);
  const reopened=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.deepEqual(selectedSignals(reopened),defaultSignals());assert.deepEqual(displayedIds(reopened),displayedIds(ui));
  assert.equal(reopened.get('reset').disabled,true);assert.equal(reopened.get('include-previous').checked,true);
});

for(const review of ['reviewed','pending'])test('saved '+review+' filtering waits for reviews before resolving a saved candidate',async()=>{
  const data=pagedCandidates(),session=sessionFixture();let release;
  saveView(session,{review});session.setItem('screening-selection:tester:a','n100');
  const ui=page(async url=>url.startsWith('/screening/reviews')?new Promise(resolve=>{release=resolve;}):dispatch(data,url),
    undefined,false,true,false,session);await settle();
  assert.equal(session.getItem('screening-selection:tester:a'),'n100');assert.equal(ui.get('selected-title').textContent,'当前筛选未选中标的');
  const latest=Object.fromEntries(data.candidates.filter(row=>review==='reviewed'||row.id==='n0').map(row=>[row.id,{decision:'watch'}]));
  release(response({latest,history:[]}));await settle();
  assert.match(ui.get('selected-title').textContent,/^n100 ·/);
  assert.match(ui.get('queue-page').textContent,review==='reviewed'?/^3 \/ 3/:/^2 \/ 3/);
});

test('pending-review restoration exposes no speculative rows or export before all-reviewed evidence arrives',async()=>{
  const data=pagedCandidates(),session=sessionFixture();let release;
  saveView(session,{review:'pending'});session.setItem('screening-selection:tester:a','n100');
  const ui=page(async url=>url.startsWith('/screening/reviews')?new Promise(resolve=>{release=resolve;}):dispatch(data,url),
    undefined,false,true,false,session);await settle();
  assert.deepEqual(displayedIds(ui),[],'an uninitialized review map does not establish unreviewed membership');
  assert.equal(ui.get('export').disabled,true);assert.match(ui.get('queue-empty').textContent,/正在读取复核状态/);
  await ui.get('export').listeners.click();assert.equal(ui.exports.length,0,'the handler also rejects an unresolved review filter');
  release(response({latest:Object.fromEntries(data.candidates.map(row=>[row.id,{decision:'watch'}])),history:[]}));await settle();
  assert.deepEqual(displayedIds(ui),[]);assert.doesNotMatch(ui.get('queue-empty').textContent,/正在读取复核状态/);
  assert.equal(ui.get('queue-reset-filters').hidden,false);
});

test('a failed pending-review dependency blocks export until its retry establishes actual membership',async()=>{
  const data=pagedCandidates(),session=sessionFixture();let failed=true;
  saveView(session,{review:'pending'});session.setItem('screening-selection:tester:a','n100');
  const latest=Object.fromEntries(data.candidates.filter(row=>row.id!=='n100').map(row=>[row.id,{decision:'watch'}]));
  const ui=page(async url=>url.startsWith('/screening/reviews')?failed?response({error:'review unavailable'},503):
    response({latest,history:[]}):dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.deepEqual(displayedIds(ui),[]);assert.equal(ui.get('export').disabled,true);
  await ui.get('export').listeners.click();assert.equal(ui.exports.length,0);
  assert.equal(session.getItem('screening-selection:tester:a'),'n100');
  failed=false;await ui.timers[0]();await settle();
  assert.deepEqual(displayedIds(ui),['n100']);assert.equal(ui.get('export').disabled,false);
  await ui.get('export').listeners.click();assert.equal(ui.exports.length,1);
  const exported=JSON.parse(await ui.exports[0].text());assert.deepEqual(exported.candidates.map(row=>row.id),['n100']);
  assert.equal(exported.filters.review,'pending');
});

test('a failed review dependency keeps its saved identity until unchanged-source retry succeeds',async()=>{
  const data=pagedCandidates(),session=sessionFixture();let failed=true;
  saveView(session,{review:'reviewed'});session.setItem('screening-selection:tester:a','n100');
  const ui=page(async url=>url.startsWith('/screening/reviews')?failed?response({error:'review unavailable'},503):
    response({latest:{n100:{decision:'watch'}},history:[]}):dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.equal(session.getItem('screening-selection:tester:a'),'n100');
  failed=false;await ui.timers[0]();await settle();assert.match(ui.get('selected-title').textContent,/^n100 ·/);
});

test('slow group discovery and stock loading restore the saved group without selecting a default-group stock',async()=>{
  const data=pagedCandidates(),session=sessionFixture(),calls=[];let groups,stocks;
  saveView(session,{origin:'all'},'长期关注');session.setItem('screening-selection:tester:a','watch:us:TSLA.US');
  const ui=page(async url=>{
    calls.push(url);
    if(url==='/get_zixuan_groups/a')return new Promise(resolve=>{groups=resolve;});
    if(url.startsWith('/get_zixuan_stocks/a/'))return new Promise(resolve=>{stocks=resolve;});
    return dispatch(data,url);
  },undefined,false,true,false,session);await settle();
  assert.equal(session.getItem('screening-selection:tester:a'),'watch:us:TSLA.US');
  groups(response([{name:'我的关注'},{name:'长期关注'}]));await settle();
  assert.equal(ui.get('watch-group').value,'长期关注');assert.equal(session.getItem('screening-selection:tester:a'),'watch:us:TSLA.US');
  assert.deepEqual(calls.filter(url=>url.startsWith('/get_zixuan_stocks/a/')),['/get_zixuan_stocks/a/'+encodeURIComponent('长期关注')]);
  stocks(response({data:[{market:'us',code:'TSLA.US',name:'Tesla'}]}));await settle();
  assert.equal(ui.get('selected-title').textContent,'Tesla · TSLA.US');assert.match(ui.get('queue-page').textContent,/^3 \/ 3/);
  assert.equal(readView(session).watch_group,'长期关注');
  const count=session.writes.filter(write=>write.key===viewKey('tester')).length;
  ui.get('watch-refresh').click();await settle();stocks(response({data:[{market:'us',code:'TSLA.US',name:'Tesla'}]}));await settle();
  assert.equal(session.writes.filter(write=>write.key===viewKey('tester')).length,count);
  choose(ui,'watch-group','我的关注');await settle();assert.equal(readView(session).watch_group,'我的关注');
  stocks(response({data:[]}));await settle();
});

test('a successfully empty group list releases a missing watch selection even while screening is running',async()=>{
  const data=pagedCandidates(),session=sessionFixture();data.status.status='running';data.status.results_loading=true;
  saveView(session,{origin:'all'},'removed group');session.setItem('screening-selection:tester:a','watch:us:TSLA.US');
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.match(ui.get('selected-title').textContent,/^n0 ·/);assert.equal(session.getItem('screening-selection:tester:a'),'n0');
});

test('a saved dynamic watch market survives slow group discovery and later reload',async()=>{
  const session=sessionFixture(),data=withCandidate();let release;
  saveView(session,{origin:'watchlist',market:'fx'},'外汇关注');
  session.setItem('screening-selection:tester:a','watch:fx:EURUSD');
  const fetch=async url=>{
    if(url==='/get_zixuan_groups/a')return new Promise(resolve=>{release=resolve;});
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'fx',code:'EURUSD',name:'欧元美元'},
      {market:'us',code:'AAPL.US',name:'Apple'}]});
    return dispatch(data,url);
  };
  const ui=page(fetch,undefined,false,true,false,session);await settle();
  assert.equal(ui.get('market-filter').value,'fx');assert.equal(ui.get('origin-filter').value,'watchlist');
  assert.equal(session.getItem('screening-selection:tester:a'),'watch:fx:EURUSD');
  release(response([{name:'外汇关注'}]));await settle();
  assert.deepEqual(displayedIds(ui),['watch:fx:EURUSD']);assert.equal(ui.get('market-filter').value,'fx');
  assert.match(ui.get('active-filter-summary').textContent,/外汇/);
  ui.get('signal-search').value='EUR';ui.get('signal-search').listeners.input();
  assert.equal(readView(session).filters.market,'fx');
  const reopened=page(fetch,undefined,false,true,false,session);await settle();
  assert.equal(reopened.get('market-filter').value,'fx');release(response([{name:'外汇关注'}]));await settle();
  assert.deepEqual(displayedIds(reopened),['watch:fx:EURUSD']);assert.equal(reopened.get('signal-search').value,'EUR');
});

test('a new source preserves saved view refinements but cannot reuse the previous source selection',async()=>{
  let data=pagedCandidates();const session=sessionFixture();
  saveView(session,{signal_period:'5m',signal_points:['3buy'],query:'n',market:'a',include_previous:false});
  session.setItem('screening-selection:tester:a','n100');
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.match(ui.get('selected-title').textContent,/^n100 ·/);
  const saved=session.getItem(viewKey('tester'));
  data={...data,source:'new-run',status:{...data.status,run_id:'new-run'}};
  await ui.timers[0]();await settle();
  assert.match(ui.get('queue-page').textContent,/^1 \/ 3/);assert.match(ui.get('selected-title').textContent,/^n0 ·/);
  assert.equal(session.getItem('screening-selection:tester:a'),'n100');assert.equal(session.getItem('screening-selection:tester:new-run'),'n0');
  assert.equal(session.getItem(viewKey('tester')),saved);assert.equal(ui.get('signal-search').value,'n');
  assert.deepEqual(selectedSignals(ui),{signal_period:'5m',signal_points:['3buy']});assert.equal(ui.get('include-previous').checked,false);
});

const industryId=index=>'qmt-gics3:'+String(index).padStart(2,'0');
function sectorPageCase(){
  const data=withCandidate(),base=data.candidates[0];
  data.status.settings.frequencies=['1m','5m','30m'];data.status.settings.point_types=SIGNAL_POINTS;
  data.sectors=Array.from({length:25},(_,index)=>({id:industryId(index),name:'行业 '+String(index).padStart(2,'0'),
    symbol_count:999,signal_count:999,member_count:10000,historical_rank:25-index,historical_regime:'up'}));
  data.sectors.push(...Array.from({length:6},(_,index)=>({id:'qmt-gics4:'+index,name:'细分 '+index,member_count:100})),
    {id:'legacy-sector',name:'旧目录',member_count:10},{id:'empty-sector',name:'空目录',member_count:10});
  data.candidates=Array.from({length:25},(_,index)=>['5m','1m'].map(frequency=>({...base,
    id:frequency+'-'+index,name:'标的 '+index,code:'SH.'+(601000+index),frequency,sectors:[industryId(index)],
    result_source:data.source,evidence_available:true,selection_available_at:frequency==='5m'?1000+index:2000+25-index,
    point:{...base.point,point_id:frequency+'-'+index,status:'confirmed',available_at:100+index}}))).flat();
  data.candidates.push(...Array.from({length:121},(_,index)=>({...base,id:'detail-'+index,name:'同标的信号 '+index,
    code:'SH.601014',sectors:[industryId(14)],result_source:data.source,evidence_available:true,selection_available_at:900,
    point:{...base.point,point_id:'detail-'+index,status:'confirmed',available_at:900}})),
    ...Array.from({length:6},(_,index)=>({...base,id:'sub-'+index,code:'SH.'+(602000+index),sectors:['qmt-gics4:'+index],
      result_source:data.source,evidence_available:true,selection_available_at:800-index,point:{...base.point,status:'confirmed'}})),
    {...base,id:'legacy-row',code:'SH.603000',sectors:['legacy-sector'],result_source:data.source,evidence_available:true,
      selection_available_at:700,point:{...base.point,status:'confirmed'}});
  return data;
}
function sectorSearch(ui,value){ui.get('sector-search').value=value;ui.get('sector-search').listeners.input();}

for(const phone of [false,true])test('sector pages contain six globally ranked cards and browsing leaves the candidate page intact '+(phone?'phone':'desktop'),async()=>{
  const data=sectorPageCase(),calls=[],ui=page(async url=>{calls.push(url);return dispatch(data,url);},undefined,false,true,phone);await settle();
  assert.equal(ui.get('sector-level').value,'industry');assert.equal(ui.get('sector-matches-only').checked,true);
  assert.deepEqual(sectorIds(ui),[24,23,22,21,20,19].map(industryId));assert.equal(templateIds.has('sector-more'),false);
  assert.equal(ui.get('sector-list').children.length,6);assert.ok(sectorIds(ui).every(Boolean),'all is outside the card list');
  ui.get('queue-next').click();ui.get('queue-next').click();await settle();
  const candidatePage=ui.get('queue-page').textContent,title=ui.get('selected-title').textContent,chart=ui.get('chart-frame').src;
  const requests=calls.length;
  ui.get('sector-next').click();assert.deepEqual(sectorIds(ui),[18,17,16,15,14,13].map(industryId));
  assert.match(ui.get('sector-page').textContent,/2\s*\/\s*5/);
  for(let index=0;index<3;index++)ui.get('sector-next').click();
  assert.deepEqual(sectorIds(ui),[industryId(0)]);assert.equal(ui.get('sector-next').disabled,true);
  assert.equal(ui.get('queue-page').textContent,candidatePage);assert.equal(ui.get('selected-title').textContent,title);assert.equal(ui.get('chart-frame').src,chart);
  assert.equal(calls.length,requests,'board paging must not refetch candidates or evidence');
  ui.get('sector-prev').click();assert.deepEqual(sectorIds(ui),[6,5,4,3,2,1].map(industryId));
});

test('sector selection and all change candidate membership without resetting the board page or filtering its own statistics',async()=>{
  const data=sectorPageCase(),session=sessionFixture(),ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  ui.get('sector-next').click();const board=sectorIds(ui);
  sectorCard(ui,industryId(14)).click();
  assert.match(ui.get('queue-page').textContent,/^1\s*\/\s*3/);assert.equal(ui.get('visible-count').textContent,'122 条');
  assert.deepEqual(sectorIds(ui),board);assert.match(ui.get('sector-page').textContent,/2\s*\/\s*5/);
  assert.match(ui.get('sector-active-selection').textContent,/行业 14/);assert.equal(readView(session).filters.sector,industryId(14));
  ui.get('sector-next').click();assert.ok(!sectorIds(ui).includes(industryId(14)));
  assert.match(ui.get('sector-active-selection').textContent,/行业 14/);assert.equal(ui.get('visible-count').textContent,'122 条');
  ui.get('sector-all').click();assert.equal(readView(session).filters.sector,'');assert.match(ui.get('sector-page').textContent,/3\s*\/\s*5/);
  assert.match(ui.get('queue-page').textContent,/^1\s*\//);assert.equal(ui.get('visible-count').textContent,'153 条');
});

test('board search level and matches controls leave filters and candidate selection intact',async()=>{
  const data=sectorPageCase(),session=sessionFixture(),ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  ui.get('sector-next').click();sectorCard(ui,industryId(14)).click();ui.get('queue-next').click();await settle();
  const before=readView(session),selected=ui.get('selected-title').textContent,candidatePage=ui.get('queue-page').textContent;
  sectorSearch(ui,'行业 00');assert.deepEqual(sectorIds(ui),[industryId(0)]);assert.match(ui.get('sector-page').textContent,/1\s*\/\s*1/);
  assert.match(ui.get('sector-active-selection').textContent,/行业 14/);assert.equal(ui.get('selected-title').textContent,selected);
  sectorSearch(ui,'');choose(ui,'sector-level','subindustry');assert.equal(sectorIds(ui).length,6);assert.ok(sectorIds(ui).every(id=>id.startsWith('qmt-gics4:')));
  choose(ui,'sector-level','other');assert.deepEqual(sectorIds(ui),['legacy-sector']);
  ui.get('sector-matches-only').click();assert.deepEqual(new Set(sectorIds(ui)),new Set(['legacy-sector','empty-sector']));
  assert.equal(ui.get('queue-page').textContent,candidatePage);assert.equal(ui.get('selected-title').textContent,selected);assert.deepEqual(readView(session),before);
  assert.match(ui.get('sector-active-selection').textContent,/行业 14/);
});

test('candidate period point and advanced changes recompute sector ranks from page one',async()=>{
  const data=sectorPageCase(),ui=page(async url=>dispatch(data,url),undefined,false,true);await settle();
  ui.get('sector-next').click();setPeriod(ui,'1m');assert.deepEqual(sectorIds(ui),[0,1,2,3,4,5].map(industryId));
  assert.match(ui.get('sector-page').textContent,/1\s*\/\s*5/);
  ui.get('sector-next').click();preset(ui,'sell');assert.deepEqual(sectorIds(ui),[]);assert.equal(ui.get('sector-empty').hidden,false);
  preset(ui,'all');ui.get('sector-next').click();choose(ui,'market-filter','us');assert.deepEqual(sectorIds(ui),[]);
  choose(ui,'market-filter','all');assert.deepEqual(sectorIds(ui),[0,1,2,3,4,5].map(industryId));
  ui.get('sector-next').click();ui.get('signal-search').value='SH.601024';ui.get('signal-search').listeners.input();
  assert.deepEqual(sectorIds(ui),[industryId(24)]);assert.match(ui.get('sector-page').textContent,/1\s*\/\s*1/);
});

test('same-source polling preserves or clamps board pagination while a new source returns to page one',async()=>{
  let data=sectorPageCase();const ui=page(async url=>dispatch(data,url),undefined,false,true);await settle();
  for(let index=0;index<4;index++)ui.get('sector-next').click();assert.match(ui.get('sector-page').textContent,/5\s*\/\s*5/);
  data={...data,status:{...data.status,completed:2}};await ui.timers[0]();await settle();assert.match(ui.get('sector-page').textContent,/5\s*\/\s*5/);
  data={...data,candidates:data.candidates.filter(row=>!row.sectors.includes(industryId(0))),status:{...data.status,completed:3}};
  await ui.timers[0]();await settle();assert.match(ui.get('sector-page').textContent,/4\s*\/\s*4/);assert.equal(ui.get('sector-next').disabled,true);
  data={...data,source:'other-run',status:{...data.status,run_id:'other-run'},candidates:data.candidates.map(row=>({...row,result_source:'other-run'}))};
  await ui.timers[0]();await settle();assert.match(ui.get('sector-page').textContent,/1\s*\/\s*4/);
});

test('a stored sector opens its own level and remains selected when board search or newer catalog omits it',async()=>{
  let data=sectorPageCase();const session=sessionFixture();saveView(session,{sector:'qmt-gics4:4'});
  const ui=page(async url=>dispatch(data,url),undefined,false,true,false,session);await settle();
  assert.equal(ui.get('sector-level').value,'subindustry');assert.match(ui.get('sector-active-selection').textContent,/细分 4/);
  assert.deepEqual(displayedIds(ui),['sub-4']);sectorSearch(ui,'no matches');assert.deepEqual(sectorIds(ui),[]);
  assert.equal(readView(session).filters.sector,'qmt-gics4:4');assert.match(ui.get('sector-active-selection').textContent,/细分 4/);
  data={...data,sectors:data.sectors.filter(sector=>sector.id!=='qmt-gics4:4'),status:{...data.status,completed:2}};
  await ui.timers[0]();await settle();assert.equal(readView(session).filters.sector,'qmt-gics4:4');
  assert.doesNotMatch(ui.get('sector-active-selection').textContent,/当前：全部板块/);
  ui.get('sector-all').click();assert.equal(readView(session).filters.sector,'');assert.equal(ui.get('visible-count').textContent,'153 条');
});

test('only other catalog IDs select the compatible other level automatically',async()=>{
  const data=withCandidate();data.candidates[0].sectors=['legacy'];data.candidates[0].point.status='confirmed';
  data.sectors=[{id:'legacy',name:'旧目录'}];
  const ui=page(async url=>dispatch(data,url),undefined,false,true);await settle();
  assert.equal(ui.get('sector-level').value,'other');assert.deepEqual(sectorIds(ui),['legacy']);
});

test('slow reviewed membership keeps sector loading explicit until the source-specific review response arrives',async()=>{
  const data=sectorPageCase(),session=sessionFixture();let release;
  saveView(session,{review:'reviewed'});
  const ui=page(async url=>url.startsWith('/screening/reviews')?new Promise(resolve=>{release=resolve;}):dispatch(data,url),
    undefined,false,true,false,session);await settle();
  assert.deepEqual(sectorIds(ui),[]);assert.match(ui.get('sector-status').textContent+' '+ui.get('sector-empty').textContent,/复核.*读取|读取.*复核|复核.*加载/);
  assert.doesNotMatch(ui.get('sector-status').textContent,/0 个匹配|0 个板块/);
  release(response({latest:{'5m-3':{decision:'watch'},'5m-18':{decision:'watch'}},history:[]}));await settle();
  assert.deepEqual(sectorIds(ui),[industryId(18),industryId(3)]);
  assert.deepEqual(displayedIds(ui),['5m-18','5m-3'],'equal linkage verdicts retain the newest signal first, independently of board priority');
});

test('sector ordering uses current time and distinct market symbols before retained watch and empty sectors',async()=>{
  const data=withCandidate(),base=data.candidates[0],id=name=>'qmt-gics3:'+name;
  data.sectors=['newest','fallback','distinct','duplicates','retained','watch','none'].map((name,index)=>({id:id(name),name,
    historical_rank:7-index,signal_count:name==='duplicates'?99999:0,symbol_count:9999,member_count:100000}));
  const row=(identity,sector,code,available,extra={})=>({...base,id:identity,code,sectors:[id(sector)],result_source:data.source,
    evidence_available:true,selection_available_at:available,point:{...base.point,status:'confirmed',available_at:available},...extra});
  data.candidates=[row('new','newest','SH.600010',500,{point:{...base.point,status:'confirmed',available_at:50}}),
    row('fallback','fallback','SH.600011',400,{selection_available_at:undefined}),
    row('a-same','distinct','SAME',300),row('us-same','distinct','SAME',300,{market:'us'}),
    ...Array.from({length:20},(_,index)=>row('duplicate-'+index,'duplicates','SH.600012',300)),
    row('old','retained','SH.600013',99999,{refresh_state:'pending',result_source:'old',evidence_available:false}),
    row('bad-proof','retained','SH.600013',999999,{evidence_available:false}),
    row('wrong-source','none','SH.600014',999999,{result_source:'wrong'}),
    row('closed','none','SH.600015',999999,{stage:'closed'}),
    row('watch-map','watch','SH.600016',999999,{frequency:'30m'})];
  const ui=page(async url=>{
    if(url==='/get_zixuan_groups/a')return response([{name:'我的关注'}]);
    if(url.startsWith('/get_zixuan_stocks/a/'))return response({data:[{market:'a',code:'SH.600016',name:'watch'}]});
    return dispatch(data,url);
  },undefined,false,true);await settle();choose(ui,'origin-filter','all');
  assert.deepEqual(sectorIds(ui),['newest','fallback','distinct','duplicates','retained','watch'].map(id));
  const distinct=elementText(sectorCard(ui,id('distinct'))),duplicates=elementText(sectorCard(ui,id('duplicates')));
  assert.match(distinct,/2\s*只/,'cross-market identical codes are two symbols');
  assert.match(duplicates,/1\s*只/,'many point records from the same symbol count once');
  assert.doesNotMatch(distinct+' '+duplicates,/9999|99999/,'stale catalog counts cannot override live metrics');
  assert.match(elementText(sectorCard(ui,id('retained'))),/上次|待更新|旧/);assert.match(elementText(sectorCard(ui,id('watch'))),/关注/);
  ui.get('include-previous').click();assert.deepEqual(sectorIds(ui),['newest','fallback','distinct','duplicates','watch'].map(id));
  choose(ui,'origin-filter','screening');assert.deepEqual(sectorIds(ui),['newest','fallback','distinct','duplicates'].map(id));
  ui.get('sector-matches-only').click();ui.get('sector-next').click();assert.equal(sectorIds(ui).length,1);
});

test('a replacement source cannot reuse the preceding source review membership for sector priorities',async()=>{
  let data=sectorPageCase(),release;const session=sessionFixture();saveView(session,{review:'reviewed'});
  const ui=page(async url=>{
    if(url.startsWith('/screening/reviews')){
      const source=new URL(url,'http://local').searchParams.get('source');
      if(source==='a')return response({latest:{'5m-24':{decision:'watch'}},history:[]});
      return new Promise(resolve=>{release=resolve;});
    }
    return dispatch(data,url);
  },undefined,false,true,false,session);await settle();
  assert.deepEqual(sectorIds(ui),[industryId(24)]);
  data={...data,source:'replacement',status:{...data.status,run_id:'replacement'},
    candidates:data.candidates.map(row=>({...row,result_source:'replacement'}))};
  const poll=ui.timers[0]();await settle();assert.deepEqual(sectorIds(ui),[]);
  assert.match(ui.get('sector-status').textContent+' '+ui.get('sector-empty').textContent,/复核.*读取|读取.*复核|复核.*加载/);
  release(response({latest:{'5m-2':{decision:'watch'}},history:[]}));await poll;await settle();
  assert.deepEqual(sectorIds(ui),[industryId(2)]);assert.deepEqual(displayedIds(ui),['5m-2']);
});

test('saving a review invalidates pending-review sector membership even when the reviews object is reused',async()=>{
  const data=withCandidate(),base=data.candidates[0],session=sessionFixture(),writes=[];
  data.sectors=[{id:industryId(0),name:'最新板块'},{id:industryId(1),name:'另一板块'}];
  data.candidates=[{...base,id:'first',sectors:[industryId(0)],selection_available_at:300,point:{...base.point,status:'confirmed'}},
    {...base,id:'second',code:'SH.600001',sectors:[industryId(1)],selection_available_at:100,point:{...base.point,status:'confirmed'}}];
  saveView(session,{review:'pending'});
  const ui=page(async(url,options)=>{
    if(url.startsWith('/screening/reviews')&&options?.method==='POST'){
      const payload=JSON.parse(options.body);writes.push(payload);return response({...payload,saved_at:400});
    }
    return dispatch(data,url);
  },undefined,false,true,false,session);await settle();
  assert.deepEqual(sectorIds(ui),[industryId(0),industryId(1)]);
  await ui.get('review-form').listeners.submit({preventDefault(){}});await settle();
  assert.equal(writes.length,1);assert.equal(writes[0].candidate_id,'first');
  assert.deepEqual(sectorIds(ui),[industryId(1)]);assert.deepEqual(displayedIds(ui),['second']);
});
