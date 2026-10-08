'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.textContent='';this.handlers={};}
  append(...rows){this.children.push(...rows);}
  replaceChildren(...rows){this.children=rows;}
  addEventListener(event,fn){this.handlers[event]=fn;}
  setAttribute(name,value){this[name]=value;}
}
const source=fs.readFileSync(path.join(__dirname,'../signal_monitor.js'),'utf8');
const wait=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(fetcher,overrides={}){
  const elements=new Map(),timers=[],requests=[];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const context={document:{getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>({content:'csrf'})},
    URLSearchParams,Date,AbortController,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
    fetch:(url,options)=>{requests.push({url,options});return fetcher(url,options);},...overrides};
  vm.runInNewContext(source,context);return {get,timers,requests};
}
function payload(){
  const rows=Array.from({length:25},(_,i)=>({id:String(i),code:'SH.'+String(i).padStart(6,'0'),market:'a',name:'test',
    frequency:'5m',point_type:'3buy',stage:'confirmed'}));
  return {enabled:true,runtime_running:true,settings:{interval_seconds:60},seed:{count:125,symbols:rows,screening_count:120,holding_count:5},
    signals:rows,events:rows,errors:[],pool_markets:{a:125},pagination:Object.fromEntries(['signals','pool','events'].map(k=>
      [k,{page:2,pages:5,size:25,total:125,overall:125,query:''}])),coverage:{total:125,ready:121,limited:4,pending:0}};
}
test('remote pages are not sliced twice and full totals and layout links survive',async()=>{
  const app=fixture(async()=>({ok:true,json:async()=>payload()}));await wait();
  assert.equal(app.get('signals').children.length,25);
  assert.equal(app.get('signal-count').textContent,125);
  assert.match(app.get('signals-page').textContent,/2 \/ 5/);
  assert.match(app.get('pool-source').textContent,/数据受限 4/);
  assert.match(app.get('signals').children[0].children[0].children[0].href,/workspace_layout=1/);
  app.get('signals-search').value='目标';app.get('signals-search').handlers.input();
  app.timers.find(t=>t.ms===180).fn();await wait();
  const url=new URL(app.requests.at(-1).url,'http://local');
  assert.equal(url.searchParams.get('signals_q'),'目标');assert.equal(url.searchParams.get('signals_page'),'1');
});

test('notification activity explains quiet scans without treating existing signals as new',async()=>{
  const data={...payload(),notification_activity:{state:'quiet',message:'监听运行中，最近复核没有新增可发送事件',
    last_check_at:300,last_event_at:100,last_sent_at:105,new_events:0}};
  const app=fixture(async()=>({ok:true,json:async()=>data}));await wait();
  assert.match(app.get('notification-activity-status').textContent,/没有新增/);
  assert.match(app.get('notification-activity-times').textContent,/最近一次处理新增 0 条/);
  assert.equal(app.get('signal-count').textContent,125);
});
test('a hung read times out and the next refresh can recover',async()=>{
  let fail=true;
  const app=fixture((url,{signal})=>fail?new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})))):
    Promise.resolve({ok:true,json:async()=>payload()}));
  app.timers.find(t=>t.ms===15000).fn();await wait();
  assert.match(app.get('monitor-message').textContent,/超时/);
  fail=false;app.get('reload').handlers.click();await wait();
  assert.equal(app.get('signals').children.length,25);
});

test('an unchanged status response preserves the existing rows',async()=>{
  let count=0;
  const unchanged=payload();for(const meta of Object.values(unchanged.pagination))meta.page=1;
  const app=fixture(async()=>++count===1?{ok:true,headers:{get:()=> '"same"'},json:async()=>unchanged}:
    {ok:true,status:304,json:async()=>{throw Error('304 has no JSON body');}});
  await wait();const rows=app.get('signals').children;
  app.get('reload').handlers.click();await wait();
  assert.equal(app.get('signals').children,rows);
  assert.equal(app.requests[1].options.headers['If-None-Match'],'"same"');
});

function statusValue(count){
  const value=payload();for(const meta of Object.values(value.pagination))meta.page=1;
  value.pagination.signals.overall=count;return value;
}
const taggedStatus=(value,etag)=>({ok:true,headers:{get:()=>etag},json:async()=>value});

test('a superseded status body cannot install an ETag for data the page never rendered',async()=>{
  let reads=0,release;
  const app=fixture(async(url,options)=>{
    reads++;
    if(reads===1)return taggedStatus(statusValue(125),'"initial"');
    if(reads===2)return new Promise(resolve=>{release=()=>resolve(taggedStatus(statusValue(127),'"late-state"'));});
    if(reads===3)return taggedStatus(statusValue(126),'"accepted"');
    if(options.headers['If-None-Match']==='"late-state"')return {ok:true,status:304};
    return taggedStatus(statusValue(127),'"late-state"');
  });await settle();
  app.get('reload').handlers.click();await settle();
  app.get('signals-page-size').handlers.change();await settle();
  assert.equal(app.get('signal-count').textContent,126);
  release();await settle();
  assert.equal(app.get('signal-count').textContent,126);
  app.get('reload').handlers.click();await settle();
  assert.equal(app.get('signal-count').textContent,127,'304 incorrectly kept the older rendered state');
  assert.equal(app.requests.at(-1).options.headers['If-None-Match'],'"accepted"');
});

test('a late status response cannot restore a cache invalidated by a user control',async()=>{
  let reads=0,release;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST')return taggedStatus({...statusValue(130),enabled:false},null);
    reads++;
    if(reads===1)return taggedStatus(statusValue(125),'"initial"');
    if(reads===2)return new Promise(resolve=>{release=()=>resolve(taggedStatus(statusValue(129),'"unused"'));});
    return taggedStatus({...statusValue(130),enabled:false},'"current"');
  });await settle();
  app.get('reload').handlers.click();await settle();
  app.get('pause').handlers.click();await settle();
  release();await settle();
  assert.equal(app.get('signal-count').textContent,130);
  app.get('reload').handlers.click();await settle();
  assert.equal(app.requests.at(-1).options.headers['If-None-Match'],undefined,
    'an ignored pre-control response restored an invalidated cache');
});

test('a response without an ETag clears the previous conditional token',async()=>{
  let reads=0;
  const app=fixture(async()=>taggedStatus(statusValue(++reads+124),reads===1?'"old"':null));
  await settle();app.get('reload').handlers.click();await settle();
  assert.equal(app.get('signal-count').textContent,126);
  app.get('reload').handlers.click();await settle();
  assert.equal(app.requests.at(-1).options.headers['If-None-Match'],undefined);
});

test('an unrenderable status body is not accepted as a cached representation',async()=>{
  let reads=0;
  const app=fixture(async(url,options)=>{
    reads++;
    if(reads===1)return taggedStatus(statusValue(125),'"initial"');
    if(reads===2)return taggedStatus({},'"broken"');
    if(options.headers['If-None-Match']==='"broken"')return {ok:true,status:304};
    return taggedStatus(statusValue(126),'"recovered"');
  });await settle();
  app.get('reload').handlers.click();await settle();
  assert.ok(app.get('monitor-message').textContent);
  app.get('reload').handlers.click();await settle();
  assert.equal(app.get('signal-count').textContent,126);
  assert.equal(app.requests.at(-1).options.headers['If-None-Match'],undefined);
});

function statusForQuery(url,enabled=true){
  const value=payload(),query=new URL(url,'http://local').searchParams;value.enabled=enabled;value.last_error='';
  for(const id of ['signals','pool','events']){
    const meta=value.pagination[id];meta.size=Number(query.get(id+'_size'))||50;meta.query=query.get(id+'_q')||'';
    meta.total=meta.query?1:125;meta.pages=Math.ceil(meta.total/meta.size);
    meta.page=Math.min(meta.pages,Number(query.get(id+'_page'))||1);
  }
  return value;
}

for(const table of ['signals','pool','events'])test('a control response preserves the requested '+table+' page and refreshes once',async()=>{
  let release,reads=0;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST')return new Promise(resolve=>{release=()=>resolve(taggedStatus(statusForQuery(url,false),null));});
    reads++;return taggedStatus(statusForQuery(url),null);
  });await settle();
  app.get('pause').handlers.click();await settle();
  app.get(table+'-next').handlers.click();app.get(table+'-next').handlers.click();await settle();
  assert.equal(reads,1,'view reads must wait for the active control');
  release();await settle();
  assert.equal(reads,2,'the requested view was never loaded after the control completed');
  const query=new URL(app.requests.at(-1).url,'http://local').searchParams;
  assert.equal(query.get(table+'_page'),'3');
  assert.match(app.get(table+'-page').textContent,/^3 \/ 3/);
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,1);
});

test('search and page size changes during a control are coalesced into the latest view',async()=>{
  let release,reads=0;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST')return new Promise(resolve=>{release=()=>resolve(taggedStatus(statusForQuery(url,false),null));});
    reads++;return taggedStatus(statusForQuery(url),null);
  });await settle();
  app.get('pause').handlers.click();await settle();
  app.get('signals-search').value='first';app.get('signals-search').handlers.input();
  app.get('signals-page-size').value='25';app.get('signals-page-size').handlers.change();
  app.get('signals-search').value='latest';app.get('signals-search').handlers.input();
  app.timers.filter(t=>t.ms===180).at(-1).fn();await settle();
  release();await settle();
  assert.equal(reads,2);
  const query=new URL(app.requests.at(-1).url,'http://local').searchParams;
  assert.equal(query.get('signals_q'),'latest');assert.equal(query.get('signals_size'),'25');
  assert.equal(query.get('signals_page'),'1');assert.match(app.get('signals-page').textContent,/\/ 1 条$/);
});

test('a failed control still refreshes the requested page without losing its error',async()=>{
  let release,reads=0;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST')return new Promise(resolve=>{release=()=>resolve({ok:false,status:409,json:async()=>({error:'pause was rejected'})});});
    reads++;return taggedStatus(statusForQuery(url),null);
  });await settle();
  app.get('pause').handlers.click();await settle();app.get('signals-next').handlers.click();await settle();
  release();await settle();
  assert.equal(reads,2);assert.match(app.get('signals-page').textContent,/^2 \/ 3/);
  assert.equal(app.get('monitor-message').textContent,'pause was rejected');
});

test('a superseded follow-up read cannot restore an older control error',async()=>{
  let releaseControl,releaseStatus,posts=0,reads=0;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST'){
      if(++posts===1)return new Promise(resolve=>{releaseControl=()=>resolve({ok:false,status:409,json:async()=>({error:'older control failure'})});});
      return taggedStatus(statusForQuery(url),null);
    }
    if(++reads===2)return new Promise(resolve=>{releaseStatus=()=>resolve(taggedStatus(statusForQuery(url),null));});
    return taggedStatus(statusForQuery(url),null);
  });await settle();
  app.get('pause').handlers.click();await settle();app.get('signals-next').handlers.click();
  releaseControl();await settle();assert.equal(typeof releaseStatus,'function');
  app.get('check-now').handlers.click();await settle();releaseStatus();await settle();
  assert.equal(posts,2);assert.equal(app.get('monitor-message').textContent,'');
});

test('a control started during a page read uses the selected page rather than the last rendered one',async()=>{
  let release,reads=0;
  const app=fixture(async(url,options)=>{
    if(options.method==='POST')return taggedStatus(statusForQuery(url,false),null);
    if(++reads===2)return new Promise(resolve=>{release=()=>resolve(taggedStatus(statusForQuery(url),null));});
    return taggedStatus(statusForQuery(url),null);
  });await settle();
  app.get('signals-next').handlers.click();await settle();
  app.get('pause').handlers.click();await settle();
  const post=app.requests.find(r=>r.options.method==='POST');
  assert.equal(new URL(post.url,'http://local').searchParams.get('signals_page'),'2');
  assert.match(app.get('signals-page').textContent,/^2 \/ 3/);
  release();await settle();
  assert.match(app.get('signals-page').textContent,/^2 \/ 3/);assert.equal(reads,2);
});

test('risk binding requires an explicit reference and submits its frozen cutoff',async()=>{
  const risk={holding_source_current:true,monitor_enabled:true,holdings:[{market:'a',code:'SH.600000',name:'held',plans:[],choices:[
    {signal_id:'reference',frequency:'5m',point_type:'3buy',available_at:900,source_closed_at:1000,anchor_price:12,stop_price:10}]}]};
  const app=fixture(async url=>({ok:true,json:async()=>url.startsWith('/monitor/holding-risk')?risk:payload()}));
  await wait();app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await wait();
  const card=app.get('holding-risk-list').children[0];
  const select=card.children.find(el=>el.tag==='label').children[0];
  const button=card.children.find(el=>el.tag==='button');
  assert.equal(select.value,'');button.handlers.click();await wait();
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,0);
  select.value='0';button.handlers.click();await wait();
  const post=app.requests.find(r=>r.options.method==='POST');
  assert.deepEqual(JSON.parse(post.options.body),{action:'bind',signal_id:'reference',source_closed_at:1000});
});

const riskResponse=value=>({ok:true,json:async()=>value});
const settle=async()=>{for(let i=0;i<4;i++)await wait();};
const riskFixture=()=>({holding_source_current:true,monitor_enabled:true,holdings:[{
  market:'a',code:'SH.600000',name:'held',plans:[{binding_id:'old-binding',frequency:'5m',point_type:'3buy',stop_price:10,status:'active'}],
  choices:[{signal_id:'first',frequency:'5m',point_type:'1buy',available_at:800,source_closed_at:1000,anchor_price:11,stop_price:9},
    {signal_id:'second',frequency:'5m',point_type:'3buy',available_at:900,source_closed_at:1000,anchor_price:12,stop_price:10}]}]});
const riskSelect=card=>card.children.find(el=>el.tag==='label').children[0];
const riskButton=(card,label)=>card.children.find(el=>el.tag==='button'&&el.textContent===label);

for(const action of ['bind','unbind'])test('a '+action+' click during an automatic risk refresh is submitted once',async()=>{
  const status={...payload(),business_health:{risk_revision:'first'}};let risk=riskFixture(),release;
  let delay=false;
  const app=fixture(async(url,options)=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(status);
    if(options.method==='POST')return riskResponse(risk);
    if(delay)return new Promise(resolve=>{release=()=>resolve(riskResponse(risk));});
    return riskResponse(risk);
  });await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const card=app.get('holding-risk-list').children[0],select=riskSelect(card);
  select.value='1';select.handlers.change?.();
  delay=true;status.business_health.risk_revision='second';app.get('reload').handlers.click();await settle();
  assert.equal(typeof release,'function');
  const button=riskButton(card,action==='bind'?'绑定并跟踪':'解除参考');
  button.handlers.click();button.handlers.click();await settle();
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,0);
  delay=false;release();await settle();
  const posts=app.requests.filter(r=>r.options.method==='POST');
  assert.equal(posts.length,1,'a user action was dropped or duplicated while a GET was pending');
  assert.deepEqual(JSON.parse(posts[0].options.body),action==='bind'
    ?{action:'bind',signal_id:'second',source_closed_at:1000}:{action:'unbind',binding_id:'old-binding'});
});

test('risk refresh preserves a selected identity when choices change order',async()=>{
  let risk=riskFixture();const status={...payload(),business_health:{risk_revision:'first'}};
  const app=fixture(async url=>riskResponse(url.startsWith('/monitor/holding-risk')?risk:status));await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const select=riskSelect(app.get('holding-risk-list').children[0]);select.value='1';select.handlers.change?.();
  risk={...risk,holdings:[{...risk.holdings[0],choices:[...risk.holdings[0].choices].reverse().map(c=>({...c,source_closed_at:1300}))}]};
  status.business_health.risk_revision='second';app.get('reload').handlers.click();await settle();
  const card=app.get('holding-risk-list').children[0];
  assert.equal(riskSelect(card).value,'0','refresh lost the selected identity after reordering');
  riskButton(card,'绑定并跟踪').handlers.click();await settle();
  const post=app.requests.find(r=>r.options.method==='POST');
  assert.deepEqual(JSON.parse(post.options.body),{action:'bind',signal_id:'second',source_closed_at:1300});
});

test('an unavailable risk choice returns to the explicit placeholder instead of selecting another point',async()=>{
  let risk=riskFixture();const status={...payload(),business_health:{risk_revision:'first'}};
  const app=fixture(async url=>riskResponse(url.startsWith('/monitor/holding-risk')?risk:status));await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const select=riskSelect(app.get('holding-risk-list').children[0]);select.value='1';select.handlers.change?.();
  risk={...risk,holdings:[{...risk.holdings[0],choices:risk.holdings[0].choices.slice(0,1)}]};
  status.business_health.risk_revision='second';app.get('reload').handlers.click();await settle();
  const card=app.get('holding-risk-list').children[0];
  assert.equal(riskSelect(card).value,'');
  riskButton(card,'绑定并跟踪').handlers.click();await settle();
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,0);
});

test('distinct risk edits retain click order while consecutive duplicate clicks are merged',async()=>{
  const risk=riskFixture(),status=payload();let release,posts=0,active=0,maximum=0;
  const app=fixture(async(url,options)=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(status);
    if(options.method==='POST'){
      posts++;active++;maximum=Math.max(maximum,active);
      if(posts===1)await new Promise(resolve=>{release=resolve;});
      active--;
    }
    return riskResponse(risk);
  });await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const card=app.get('holding-risk-list').children[0],select=riskSelect(card),button=riskButton(card,'绑定并跟踪');
  select.value='0';select.handlers.change();button.handlers.click();await settle();
  select.value='1';select.handlers.change();button.handlers.click();
  select.value='0';select.handlers.change();button.handlers.click();button.handlers.click();await settle();
  assert.equal(posts,1);release();await settle();
  assert.deepEqual(app.requests.filter(r=>r.options.method==='POST').map(r=>JSON.parse(r.options.body).signal_id),['first','second','first']);
  assert.equal(maximum,1,'risk edits must never execute concurrently');
});

test('a failed automatic read still submits a queued unbind exactly once',async()=>{
  const risk=riskFixture(),status={...payload(),business_health:{risk_revision:'first'}};let fail=false,release;
  const app=fixture(async(url,options)=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(status);
    if(options.method!=='POST'&&fail)return new Promise(resolve=>{release=()=>resolve({ok:false,status:503,json:async()=>({error:'temporary read failure'})});});
    return riskResponse(risk);
  });await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const button=riskButton(app.get('holding-risk-list').children[0],'解除参考');
  fail=true;status.business_health.risk_revision='second';app.get('reload').handlers.click();await settle();
  button.handlers.click();fail=false;release();await settle();
  const posts=app.requests.filter(r=>r.options.method==='POST');
  assert.equal(posts.length,1);assert.equal(JSON.parse(posts[0].options.body).binding_id,'old-binding');
});

test('a binding response preserves a different unsubmitted selection made during its request',async()=>{
  const risk=riskFixture();let release;
  const app=fixture(async(url,options)=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(payload());
    if(options.method==='POST')await new Promise(resolve=>{release=resolve;});
    return riskResponse(risk);
  });await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const card=app.get('holding-risk-list').children[0],select=riskSelect(card);
  select.value='0';select.handlers.change();riskButton(card,'绑定并跟踪').handlers.click();await settle();
  select.value='1';select.handlers.change();release();await settle();
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(riskSelect(app.get('holding-risk-list').children[0]).value,'1');
});

test('a failed risk read retries after backoff even when monitor status returns 304',async()=>{
  let now=1000,reads=0,statusReads=0,fail=true;
  class Clock extends Date{static now(){return now;}}
  const status={...payload(),business_health:{risk_revision:'unchanged'}};
  const app=fixture(async url=>{
    if(url.startsWith('/monitor/holding-risk')){
      reads++;return fail?{ok:false,status:503,json:async()=>({error:'temporary failure'})}:riskResponse(riskFixture());
    }
    return ++statusReads===1?taggedStatus(status,'"stable"'):{ok:true,status:304};
  },{Date:Clock});await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  assert.equal(reads,1);
  fail=false;app.get('reload').handlers.click();await settle();assert.equal(reads,1);
  now+=5001;app.get('reload').handlers.click();await settle();
  assert.equal(reads,2,'unchanged status suppressed recovery of the risk request');
  assert.equal(app.get('holding-risk-list').children.length,1);
});

test('temporary evidence errors retry and preserve an unsubmitted selection',async()=>{
  let now=1000,reads=0,risk=riskFixture();
  class Clock extends Date{static now(){return now;}}
  const status={...payload(),business_health:{risk_revision:'first'}};
  const app=fixture(async url=>{
    if(url.startsWith('/monitor/holding-risk')){reads++;return riskResponse(risk);}
    return riskResponse(status);
  },{Date:Clock});await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  const select=riskSelect(app.get('holding-risk-list').children[0]);select.value='1';select.handlers.change();
  risk={...risk,retry_needed:true,holdings:[{...risk.holdings[0],choices:[],message:'temporary evidence failure'}]};
  status.business_health.risk_revision='second';app.get('reload').handlers.click();await settle();
  assert.equal(reads,2);
  risk=riskFixture();now+=5001;app.get('reload').handlers.click();await settle();
  assert.equal(reads,3,'a 200 response with unavailable evidence was treated as final');
  assert.equal(riskSelect(app.get('holding-risk-list').children[0]).value,'1');
  assert.equal(app.requests.filter(row=>row.options.method==='POST').length,0);
});

test('failed risk edits are reconciled with a GET and are never automatically resubmitted',async()=>{
  let now=1000,reads=0,posts=0;
  class Clock extends Date{static now(){return now;}}
  const status={...payload(),business_health:{risk_revision:'stable'}};
  const app=fixture(async(url,options)=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(status);
    if(options.method==='POST'){posts++;return {ok:false,status:503,json:async()=>({error:'temporary edit response failure'})};}
    reads++;return riskResponse(riskFixture());
  },{Date:Clock});await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  riskButton(app.get('holding-risk-list').children[0],'解除参考').handlers.click();await settle();
  assert.equal(posts,1);now+=5001;app.get('reload').handlers.click();await settle();
  assert.equal(posts,1);assert.equal(reads,2);
});

test('persistent risk request failures remain limited to one attempt per backoff interval',async()=>{
  let now=1000,reads=0;
  class Clock extends Date{static now(){return now;}}
  const status={...payload(),business_health:{risk_revision:'stable'}};
  const app=fixture(async url=>{
    if(!url.startsWith('/monitor/holding-risk'))return riskResponse(status);
    reads++;return {ok:false,status:503,json:async()=>({error:'still unavailable'})};
  },{Date:Clock});await settle();
  app.get('holding-risk-panel').open=true;app.get('holding-risk-panel').handlers.toggle();await settle();
  for(let i=0;i<3;i++){app.get('reload').handlers.click();await settle();}
  assert.equal(reads,1);
  now+=5001;app.get('reload').handlers.click();await settle();assert.equal(reads,2);
  for(let i=0;i<3;i++){app.get('reload').handlers.click();await settle();}
  assert.equal(reads,2);
});
