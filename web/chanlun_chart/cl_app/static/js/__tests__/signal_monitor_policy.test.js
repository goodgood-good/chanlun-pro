'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../signal_monitor.js'),'utf8');
const template=fs.readFileSync(path.join(__dirname,'../../../templates/signal_monitor.html'),'utf8');
const defaults={frequencies:['1m','5m','30m'],other_frequencies:['5m'],holdings:['1buy','2buy','3buy','1sell','2sell','3sell'],others:['1buy','2buy','3buy']};
const status=policy=>({enabled:true,runtime_running:true,settings:{interval_seconds:60,after_close:'15:10'},
  signals:[],events:[],errors:[],seed:{count:0,symbols:[]},dingtalk:{enabled:false,configured:true},point_notifications:policy});
const response=(value,code=200)=>({ok:code<400,status:code,json:async()=>value});
const settle=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
class Element{
  constructor(){this.children=[];this.handlers={};this.dataset={};this.style={};this.value='';this.textContent='';this.checked=false;}
  append(...rows){this.children.push(...rows);}
  replaceChildren(...rows){this.children=rows;}
  addEventListener(type,fn){this.handlers[type]=fn;}
  setAttribute(key,value){this[key]=String(value);}
}
function fixture(fetcher){
  const elements=new Map(),timers=[],requests=[],inputs=[];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  for(const match of template.matchAll(/<input\s+([^>]*name="(?:notification-frequency|ordinary-frequency|holding-point|ordinary-point)"[^>]*)>/g)){
    const attrs=Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(value=>value.slice(1)));
    const input=get(attrs.id);Object.assign(input,attrs);inputs.push(input);
  }
  const context={document:{getElementById:get,createElement:()=>new Element(),body:new Element(),
    querySelector:query=>query.startsWith('meta')?{content:'test-only-csrf'}:null,
    querySelectorAll:query=>query.startsWith('#notification-policy-form')?inputs.filter(input=>query.includes(`name="${input.name}"`)):[]},
    URLSearchParams,Date,AbortController,WorkspaceDevice:{isPhone:false},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
    fetch:(url,options)=>{requests.push({url,options});return fetcher(url,options);}};
  vm.runInNewContext(source,context);
  return {get,inputs,requests,
    poll:async()=>{await timers.find(value=>value.ms===5000).fn();await settle();},
    change:(id,checked)=>{const input=get(id);input.checked=checked;input.handlers.change();},
    submit:()=>get('notification-policy-form').handlers.submit({preventDefault(){}})};
}
const serve=(url,body=defaults)=>url.startsWith('/monitor/status')?response(status(body)):
  url.startsWith('/monitor/notifications?')?response({items:[],pagination:{page:1,size:25,total:0,pages:1},summary:{}}):
  url.startsWith('/monitor/holding-risk')?response({holdings:[]}):response({groups:[]});

test('real template separates holding points from ordinary buy-only delivery',async()=>{
  const ui=fixture(async url=>serve(url));await settle();
  assert.equal(ui.inputs.length,13);
  assert.equal(ui.inputs.filter(input=>input.name==='ordinary-point'&&input.value.endsWith('sell')).length,0);
  assert.deepEqual(ui.inputs.filter(input=>input.name==='ordinary-frequency').map(input=>input.value),['5m']);
  assert.equal(ui.get('notification-policy-fields').disabled,false);
  assert.equal(ui.get('notification-policy-save').disabled,true);
  assert.ok(ui.inputs.every(input=>input.checked));
  assert.equal(ui.get('dingtalk-enabled').checked,false,'master off does not prevent editing preferences');
});

test('failed initial read stays disabled, later saved empty settings remain empty',async()=>{
  let data=null;
  const ui=fixture(async url=>serve(url,data));await settle();
  assert.equal(ui.get('notification-policy-fields').disabled,true);
  ui.submit();assert.equal(ui.requests.filter(row=>row.options?.method==='POST').length,0);
  data={frequencies:[],holdings:[],others:[]};await ui.poll();
  assert.equal(ui.get('notification-policy-fields').disabled,false);
  assert.ok(ui.inputs.every(input=>!input.checked));
});

test('ordinary notification period saves independently from all holding periods',async()=>{
  const ui=fixture(async(url,options)=>url.startsWith('/monitor/notification-policy')?
    response(status(JSON.parse(options.body))):serve(url));await settle();
  ui.change('notification-ordinary-frequency-5m',false);await ui.poll();
  assert.equal(ui.get('notification-ordinary-frequency-5m').checked,false);
  for(const period of defaults.frequencies)assert.equal(ui.get('notification-frequency-'+period).checked,true);
  ui.submit();await settle();
  const saved=JSON.parse(ui.requests.find(row=>row.options?.method==='POST').options.body);
  assert.deepEqual(saved,{...defaults,other_frequencies:[]});
  ui.change('notification-frequency-1m',false);
  assert.equal(ui.get('notification-ordinary-frequency-5m').checked,false);
});

test('the master switch can still be turned off after both destination addresses are cleared',async()=>{
  const ui=fixture(async url=>url.startsWith('/monitor/status')?
    response({...status(defaults),dingtalk:{enabled:true,configured:false}}):serve(url));
  await settle();
  assert.equal(ui.get('dingtalk-enabled').checked,true);
  assert.equal(ui.get('dingtalk-enabled').disabled,false);
});

test('poll cannot overwrite a dirty draft and save uses only independent point preferences',async()=>{
  const ui=fixture(async(url,options)=>url.startsWith('/monitor/notification-policy')?
    response(status(JSON.parse(options.body))):serve(url));await settle();
  ui.change('notification-ordinary-3buy',false);await ui.poll();
  assert.equal(ui.get('notification-ordinary-3buy').checked,false);
  ui.submit();await settle();
  const request=ui.requests.find(row=>row.options?.method==='POST');
  assert.match(request.url,/^\/monitor\/notification-policy\?view=paged/);
  assert.deepEqual(JSON.parse(request.options.body),{...defaults,others:defaults.others.filter(point=>point!=='3buy')});
  assert.equal(ui.get('notification-policy-save').disabled,true);
  assert.match(ui.get('notification-policy-message').textContent,/已保存/);
  assert.equal(ui.get('dingtalk-enabled').checked,false);
});

test('edits made during save survive its late response and duplicate submits do not send',async()=>{
  let finish,posted;
  const ui=fixture(async(url,options)=>url.startsWith('/monitor/notification-policy')?
    new Promise(resolve=>{finish=resolve;posted=JSON.parse(options.body);}):serve(url));await settle();
  ui.change('notification-ordinary-3buy',false);ui.submit();ui.submit();
  assert.equal(ui.get('notification-policy-fields').disabled,false);
  ui.change('notification-frequency-1m',false);
  assert.equal(ui.requests.filter(row=>row.options?.method==='POST').length,1);
  finish(response(status(posted)));await settle();
  assert.equal(ui.get('notification-frequency-1m').checked,false);
  assert.equal(ui.get('notification-policy-save').disabled,false);
  assert.match(ui.get('notification-policy-message').textContent,/之后的修改尚未保存/);
  await ui.poll();assert.equal(ui.get('notification-frequency-1m').checked,false);
});

test('failed save keeps the exact draft and allows retry including empty scope',async()=>{
  let fail=true;
  const ui=fixture(async(url,options)=>url.startsWith('/monitor/notification-policy')?
    fail?response({error:'fixture save error'},409):response(status(JSON.parse(options.body))):serve(url));await settle();
  for(const point of defaults.others)ui.change('notification-ordinary-'+point,false);
  ui.submit();await settle();
  assert.match(ui.get('notification-policy-message').textContent,/保存失败/);
  assert.equal(ui.get('notification-policy-save').disabled,false);
  await ui.poll();assert.ok(ui.inputs.filter(input=>input.name==='ordinary-point').every(input=>!input.checked));
  fail=false;ui.submit();await settle();
  assert.deepEqual(JSON.parse(ui.requests.findLast(row=>row.options?.method==='POST').options.body).others,[]);
  assert.match(ui.get('notification-policy-message').textContent,/已保存/);
});

test('corrupt stored preferences show the server diagnostic and can be repaired',async()=>{
  const ui=fixture(async url=>url.startsWith('/monitor/status')?response({...status({frequencies:[],holdings:[],others:[]}),
    point_notification_error:'已暂停买卖点通知：保存的通知条件无效，请重新设置。'}):serve(url));await settle();
  assert.match(ui.get('notification-policy-message').textContent,/已暂停买卖点通知/);
  assert.equal(ui.get('notification-policy-save').disabled,false,'saving an explicit all-off policy repairs malformed stored settings');
  ui.change('notification-frequency-5m',true);
  assert.equal(ui.get('notification-policy-save').disabled,false);
});
