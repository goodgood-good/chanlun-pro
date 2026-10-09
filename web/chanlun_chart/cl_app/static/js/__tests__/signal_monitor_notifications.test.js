'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.textContent='';this.handlers={};this.dataset={};this.style={};this.hidden=false;}
  append(...rows){this.children.push(...rows);}
  replaceChildren(...rows){this.children=rows;}
  addEventListener(type,fn){this.handlers[type]=fn;}
  setAttribute(key,value){this[key]=String(value);}
  scrollIntoView(){this.scrolled=true;}
  focus(){this.focused=true;}
}
const source=fs.readFileSync(path.join(__dirname,'../signal_monitor.js'),'utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const response=(value,status=200)=>({ok:status<400,status,json:async()=>value});
const statusPayload={enabled:true,runtime_running:true,settings:{interval_seconds:60,after_close:'15:10'},
  signals:[],events:[],errors:[],seed:{count:0,symbols:[]},dingtalk:{enabled:true,configured:true}};
const row=(id='notice-1',overrides={})=>({id,name:'测试持仓',code:'SH.513100',market:'a',frequency:'5m',
  kind:'signal',point_type:'3buy',holding:true,status:'sent',delivery_status:'sent',created_at:100,sent_at:110,
  title:'买卖点通知',summary:'回抽守住中枢上沿',image_count:1,...overrides});
const listing=(items=[row()],extra={})=>({items,pagination:{page:1,size:25,pages:1,total:items.length,has_more:false},
  summary:{sent:1,pending:2,failed:0,holdings:1,last_sent_at:110},...extra});
const detail=(id='notice-1',overrides={})=>({...row(id),body:'已确认三买。',evidence_lines:['回抽守住中枢上沿'],
  images:[{frequency:'1m/5m/30m',periods:['1m','5m','30m'],url:'/monitor/notifications/'+id+'/images/0.png?version='+'a'.repeat(64),status:'sent'}],...overrides});
function fixture(fetcher,phone=false){
  const elements=new Map(),requests=[],timers=[];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('notification-workspace').dataset.view='notifications';
  for(const id of ['status','kind','frequency','scope'])get('notifications-'+id).value='all';
  get('notifications-hours').value='24';
  const tabs=['notifications','signals','settings','diagnostics'].map(name=>{const el=new Element('button');el.dataset.monitorTab=name;return el;});
  const panels=tabs.map(tab=>{const el=new Element('section');el.dataset.monitorPanel=tab.dataset.monitorTab;return el;});
  const context={document:{getElementById:get,createElement:tag=>new Element(tag),body:new Element('body'),
    querySelector:query=>query.startsWith('meta')?{content:'csrf'}:null,
    querySelectorAll:query=>query==='[data-monitor-tab]'?tabs:query==='[data-monitor-panel]'?panels:[]},
    URLSearchParams,Date,AbortController,WorkspaceDevice:{isPhone:phone},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
    fetch:(url,options)=>{requests.push({url,options});return fetcher(url,options);}};
  vm.runInNewContext(source,context);return {get,requests,timers,tabs,panels};
}
function dispatch(url,data=listing()){
  if(url.startsWith('/monitor/status'))return response(statusPayload);
  if(url.startsWith('/monitor/notifications?'))return response(data);
  if(url.startsWith('/monitor/notifications/'))return response(detail(decodeURIComponent(url.split('/').at(-1))));
  throw Error('unexpected URL '+url);
}
const text=element=>element.textContent+element.children.map(text).join(' ');
const byTag=(element,tag)=>[...(element.tag===tag?[element]:[]),...element.children.flatMap(child=>byTag(child,tag))];

test('notifications open on a rolling 24-hour range and render the actual composite chart once',async()=>{
  const ui=fixture(async url=>dispatch(url));await settle();await settle();
  const query=new URL(ui.requests.find(r=>r.url.startsWith('/monitor/notifications?')).url,'http://local');
  assert.equal(query.searchParams.get('hours'),'24');assert.equal(query.searchParams.get('status'),'all');
  assert.equal(ui.get('notifications-list').children.length,1);
  assert.match(text(ui.get('notifications-list')),/我的持仓/);
  assert.match(text(ui.get('notification-detail-content')),/保存的信号要点/);
  const imgs=byTag(ui.get('notification-detail-content'),'img');assert.equal(imgs.length,1);
  assert.match(imgs[0].src,/\?version=a{64}$/);
  assert.match(imgs[0].alt,/1m \/ 5m \/ 30m 合成图/);
  assert.equal(byTag(ui.get('notification-detail-content'),'button').filter(b=>b.textContent.includes('合成图')).length,0,
    'a single composite image does not invent separate period tabs');
  assert.equal(ui.get('notification-sent-count').textContent,1);
});

test('background refresh preserves the selected detail, page and chart element',async()=>{
  let data=listing([row('a'),row('b')],{pagination:{page:2,size:25,pages:3,total:60,has_more:true}});
  const ui=fixture(async url=>dispatch(url,data));await settle();await settle();
  ui.get('notifications-list').children[1].handlers.click();await settle();
  const content=ui.get('notification-detail-content').children,details=ui.requests.filter(r=>/notifications\/b$/.test(r.url)).length;
  data=listing([row('new'),row('b')],{pagination:{page:2,size:25,pages:3,total:61,has_more:true}});
  await ui.timers.find(timer=>timer.ms===5000).fn();await settle();
  assert.equal(ui.get('notification-detail-content').children,content);
  assert.equal(ui.requests.filter(r=>/notifications\/b$/.test(r.url)).length,details);
  assert.match(ui.get('notifications-page').textContent,/2 \/ 3/);
  assert.equal(ui.get('notifications-list').children[1]['aria-pressed'],'true');
});

for(const change of ['new delivery','previous-page status changed'])test('notification pagination keeps its boundary after '+change,async()=>{
  let now=2000;
  const records=Array.from({length:36},(_,index)=>row('record-'+index,{name:'record-'+index,sort_at:1900-index,sent_at:1900-index}));
  const cursors=new Map();
  const ui=fixture(async url=>{
    if(!url.startsWith('/monitor/notifications?'))return dispatch(url);
    const query=new URL(url,'http://local').searchParams,size=Number(query.get('size')),page=Number(query.get('page'));
    const cursor=cursors.get(query.get('cursor'));
    const asOf=cursor?.asOf??(query.has('as_of')?Number(query.get('as_of')):now);
    const within=records.filter(value=>value.sort_at<=asOf).sort((a,b)=>b.sort_at-a.sort_at);
    const remaining=cursor?within.filter(value=>value.sort_at<cursor.at):within.slice((page-1)*size);
    const items=remaining.slice(0,size),hasMore=remaining.length>size;
    const next=hasMore?'after-'+items.at(-1).id:null;
    if(next)cursors.set(next,{at:items.at(-1).sort_at,asOf});
    return response(listing(items,{pagination:{page,size,total:within.length,pages:Math.ceil(within.length/size),has_more:hasMore,as_of:asOf,next_cursor:next}}));
  },true);
  await settle();await settle();
  const visible=()=>ui.get('notifications-list').children.map(button=>button.children[0].children[0].textContent);
  assert.equal(visible()[0],'record-0');assert.equal(visible().at(-1),'record-14');
  if(change==='new delivery')records.unshift(row('new',{name:'new',sort_at:2100,sent_at:2100}));
  else records[0]={...records[0],sort_at:2100,sent_at:2100};
  now=2200;
  ui.get('notifications-next').handlers.click();await settle();await settle();
  assert.equal(visible()[0],'record-15','the cursor, rather than a shifted offset, determines the next page');
  assert.equal(visible().at(-1),'record-29');
  const nextRequest=new URL(ui.requests.findLast(request=>request.url.startsWith('/monitor/notifications?')).url,'http://local');
  assert.equal(nextRequest.searchParams.get('as_of'),'2000');
  assert.equal(nextRequest.searchParams.get('cursor'),'after-record-14');
  ui.get('notifications-prev').handlers.click();await settle();await settle();
  assert.equal(visible()[0],change==='new delivery'?'record-0':'record-1');
});

test('historical refresh keeps its cursor, advertises newer delivery and only the latest action starts a new window',async()=>{
  let changed=false;
  const ui=fixture(async url=>{
    if(!url.startsWith('/monitor/notifications?'))return dispatch(url);
    const q=new URL(url,'http://local').searchParams,page=Number(q.get('page'));
    return response(listing(page===1?[row('a'),...(changed?[row('new')]:[])]:[row('b')],{
      pagination:{page,size:25,total:26,pages:2,has_more:page===1,as_of:q.has('as_of')?Number(q.get('as_of')):changed?2200:2000,
        next_cursor:page===1?'first-boundary':null,has_newer:changed&&q.has('as_of')},
    }));
  });await settle();await settle();
  ui.get('notifications-next').handlers.click();await settle();await settle();
  changed=true;await ui.timers.find(timer=>timer.ms===5000).fn();await settle();
  let request=new URL(ui.requests.findLast(value=>value.url.startsWith('/monitor/notifications?')).url,'http://local');
  assert.equal(request.searchParams.get('page'),'2');assert.equal(request.searchParams.get('as_of'),'2000');
  assert.equal(request.searchParams.get('cursor'),'first-boundary');
  assert.equal(ui.get('notifications-newer').hidden,false);
  assert.match(ui.get('notifications-snapshot-note').textContent,/发送状态仍会更新/);
  ui.get('notifications-newer').handlers.click();await settle();await settle();
  request=new URL(ui.requests.findLast(value=>value.url.startsWith('/monitor/notifications?')).url,'http://local');
  assert.equal(request.searchParams.get('page'),'1');assert.equal(request.searchParams.has('as_of'),false);
  assert.equal(request.searchParams.has('cursor'),false);assert.equal(ui.get('notifications-snapshot').hidden,true);
  assert.equal(ui.get('notifications-list').children[0]['aria-pressed'],'true','the inspected record remains selected');
});

test('a failed next-page read retries that page rather than skipping another page',async()=>{
  let fail=true;
  const ui=fixture(async url=>{
    if(!url.startsWith('/monitor/notifications?'))return dispatch(url);
    const q=new URL(url,'http://local').searchParams,page=Number(q.get('page'));
    if(page===2&&fail)return response({error:'temporary page read failure'},503);
    return response(listing([row(page===1?'a':'b')],{pagination:{page,size:25,total:51,pages:3,has_more:true,
      as_of:2000,next_cursor:page===1?'first-boundary':'second-boundary'}}));
  });await settle();await settle();
  ui.get('notifications-next').handlers.click();await settle();
  assert.match(ui.get('notifications-message').textContent,/保留上次记录/);
  assert.match(ui.get('notifications-page').textContent,/1 \/ 3/);
  fail=false;ui.get('notifications-next').handlers.click();await settle();await settle();
  const requested=ui.requests.filter(value=>value.url.startsWith('/monitor/notifications?')).map(value=>new URL(value.url,'http://local').searchParams.get('page'));
  assert.deepEqual(requested,['1','2','2']);
  assert.match(ui.get('notifications-page').textContent,/2 \/ 3/);
});

test('editing a filter on a historical page cannot send its old cursor with the new query',async()=>{
  const ui=fixture(async url=>{
    if(!url.startsWith('/monitor/notifications?'))return dispatch(url);
    const q=new URL(url,'http://local').searchParams,page=Number(q.get('page'));
    if(q.get('cursor')&&q.get('q'))return response({error:'cursor filter mismatch'},400);
    return response(listing([row('a')],{pagination:{page,size:25,total:76,pages:4,has_more:true,
      as_of:2000,next_cursor:'after-page-'+page}}));
  });await settle();await settle();
  ui.get('notifications-next').handlers.click();await settle();
  const reads=()=>ui.requests.filter(value=>value.url.startsWith('/monitor/notifications?'));
  assert.equal(reads().length,2);
  ui.get('notifications-search').value='BABA';ui.get('notifications-search').handlers.input();
  await ui.timers.find(timer=>timer.ms===5000).fn();await settle();
  ui.get('notifications-next').handlers.click();await settle();
  assert.equal(reads().length,2,'polling and navigation wait for the filter debounce to commit');
  ui.timers.findLast(timer=>timer.ms===200).fn();await settle();await settle();
  const request=new URL(reads().at(-1).url,'http://local');
  assert.equal(request.searchParams.get('q'),'BABA');assert.equal(request.searchParams.get('page'),'1');
  assert.equal(request.searchParams.has('cursor'),false);assert.equal(request.searchParams.has('as_of'),false);
  assert.doesNotMatch(ui.get('notifications-message').textContent,/mismatch/);
});

test('filters reset to page one and stale detail replies cannot overwrite the selected notification',async()=>{
  let release;const pending=new Promise(resolve=>{release=resolve;});
  const ui=fixture(async url=>url.endsWith('/notice-1')?pending:dispatch(url,listing([row(),row('b')])));
  await settle();ui.get('notifications-list').children[1].handlers.click();await settle();
  const rendered=ui.get('notification-detail-content').children;
  release(response(detail('notice-1',{name:'stale detail'})));await settle();
  assert.equal(ui.get('notification-detail-content').children,rendered);
  ui.get('notifications-status').value='failed';ui.get('notifications-status').handlers.change();await settle();
  const query=new URL(ui.requests.findLast(r=>r.url.startsWith('/monitor/notifications?')).url,'http://local');
  assert.equal(query.searchParams.get('page'),'1');assert.equal(query.searchParams.get('status'),'failed');
});

test('a newer delivery state for the same selected id supersedes its delayed old detail',async()=>{
  let release,reads=0,delivered=false;
  const old=new Promise(resolve=>{release=resolve;});
  const pending=row('same',{status:'pending',delivery_status:'pending',sent_at:null});
  const sent=row('same',{status:'sent',delivery_status:'sent',sent_at:140});
  const ui=fixture(async url=>{
    if(url.endsWith('/same'))return ++reads===1?old:response(detail('same',{...sent,body:'最新已发送内容'}));
    return dispatch(url,listing([delivered?sent:pending]));
  });await settle();
  assert.equal(reads,1);
  delivered=true;await ui.timers.find(timer=>timer.ms===5000).fn();await settle();await settle();
  assert.equal(reads,2,'the changed list evidence must start a new detail read without waiting for the old one');
  assert.match(text(ui.get('notification-detail-content')),/最新已发送内容/);
  release(response(detail('same',{...pending,body:'旧待发送内容'})));await settle();
  assert.doesNotMatch(text(ui.get('notification-detail-content')),/旧待发送内容|待发送/);
  assert.match(text(ui.get('notification-detail-content')),/已发送/);
});

test('silent events never claim delivery and absent images are not invented',async()=>{
  const item=row('silent',{status:'suppressed',delivery_status:'suppressed',sent_at:null,image_count:0});
  const ui=fixture(async url=>url.endsWith('/silent')?response(detail('silent',{...item,images:[],body:'仅重分类'})):dispatch(url,listing([item])));
  await settle();await settle();
  const shown=text(ui.get('notification-detail-content'));
  assert.match(shown,/静默 · 未发送/);assert.match(shown,/没有作为通知发送/);assert.match(shown,/尚未发送/);
  assert.equal(byTag(ui.get('notification-detail-content'),'img').length,0);
});

test('a delivered consolidation warning remains explicitly unconfirmed and batches retain their members',async()=>{
  const item=row('warning',{point_type:'consolidation_up',kind:'consolidation',signal_stage:'warning',
    event_count:2,title:'两只持仓的盘整背驰预警',symbols:[{name:'纳指100ETF',code:'QQQ.US'},{name:'特斯拉',code:'TSLA.US'}]});
  const ui=fixture(async url=>url.endsWith('/warning')?response(detail('warning',{...item,images:[],signals:[
    {name:'纳指100ETF',code:'QQQ.US',frequency:'5m',point_type:'consolidation_up',stage:'warning'},
    {name:'特斯拉',code:'TSLA.US',frequency:'5m',point_type:'consolidation_up',stage:'warning'},
  ]})):dispatch(url,listing([item])));
  await settle();await settle();
  const list=text(ui.get('notifications-list')),shown=text(ui.get('notification-detail-content'));
  assert.match(list,/已发送/);assert.match(list,/提前预警（未确认）/);
  assert.match(list,/QQQ.US/);assert.match(list,/TSLA.US/);
  assert.match(shown,/本条通知包含的信号/);assert.match(shown,/提前预警（未确认）/);
});

test('a multi-signal notification never promotes the first member price or confirmation to message-wide facts',async()=>{
  const signals=[
    {name:'阿里巴巴',code:'BABA.US',frequency:'1m',point_type:'3buy',stage:'confirmed',point_confirmed_at:1000,anchor_price:109.77,anchor_at:900,source_closed_at:1200},
    {name:'特斯拉',code:'TSLA.US',frequency:'5m',point_type:'2sell',stage:'confirmed',point_confirmed_at:2000,anchor_price:255.5,anchor_at:1700,source_closed_at:2400},
  ];
  const item=row('batch',{event_count:2,confirmed_at:null,frequency:'',point_type:'',title:'两条信号通知',event_at:2500});
  const ui=fixture(async url=>url.endsWith('/batch')?response(detail('batch',{
    ...item,signal:signals[0],signals,evidence_lines:[],images:[],attempts:1,
  })):dispatch(url,listing([item])));
  await settle();await settle();
  const content=ui.get('notification-detail-content'),facts=byTag(content,'dl')[0];
  const tags=content.children.find(el=>el.className==='notification-detail-tags');
  assert.doesNotMatch(text(tags),/已确认/,'missing aggregate stage must not fall back to the first signal');
  assert.deepEqual(byTag(facts,'dt').map(el=>el.textContent),['事件发生','通知生成','实际发送','发送尝试']);
  assert.doesNotMatch(text(facts),/109\.77|255\.5|信号确认|拐点|行情截止/);
  const members=byTag(content,'section').find(section=>byTag(section,'h4').some(el=>el.textContent==='本条通知包含的信号'));
  const entries=byTag(members,'li');assert.equal(entries.length,2);
  const fmt=value=>new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  for(let index=0;index<2;index++){
    const displayed=text(entries[index]),signal=signals[index],other=signals[1-index];
    assert.ok(displayed.includes(signal.code));assert.ok(displayed.includes(signal.frequency));
    assert.ok(displayed.includes('信号确认：'+fmt(signal.point_confirmed_at)));
    assert.ok(displayed.includes('信号拐点：'+signal.anchor_price));
    assert.ok(displayed.includes('拐点时间：'+fmt(signal.anchor_at)));
    assert.ok(displayed.includes('行情截止：'+fmt(signal.source_closed_at)));
    assert.ok(!displayed.includes(String(other.anchor_price)));
  }
});

test('read failures keep the previous delivery records and phone selection scrolls to detail',async()=>{
  let fail=false;
  const ui=fixture(async url=>fail&&url.startsWith('/monitor/notifications?')?response({error:'台账暂不可读'},503):dispatch(url),true);
  await settle();await settle();
  const query=new URL(ui.requests[0].url,'http://local');assert.equal(query.searchParams.get('size'),'15');
  ui.get('notifications-list').children[0].handlers.click();await settle();
  assert.equal(ui.get('notification-detail').scrolled,true);
  fail=true;ui.get('notifications-reload').handlers.click();await settle();
  assert.equal(ui.get('notifications-list').children.length,1);
  assert.match(ui.get('notifications-message').textContent,/保留上次记录/);
  ui.tabs[2].handlers.click();assert.equal(ui.panels[2].hidden,false);assert.equal(ui.panels[0].hidden,true);
});
