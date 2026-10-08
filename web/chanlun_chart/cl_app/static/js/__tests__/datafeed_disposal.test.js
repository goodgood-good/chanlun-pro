'use strict';

const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(setImmediate);};
const symbol={ticker:'us:AAA.US',name:'AAA.US',exchange:'us'};
const raw={s:'ok',update:false,bar_time_label:'start',t:[1000],o:[10],h:[10],l:[10],c:[10],v:[1],fxs:[],bis:[],xds:[]};
const configuration={supports_search:true,supported_resolutions:['5'],supports_time:true};

async function fixture(holdConfig=false){
  const intervals=new Map(),timeouts=new Map(),requests=[],pending=[];let nextId=0;
  const reply=body=>({ok:true,status:200,text:async()=>JSON.stringify(body)});
  const sandbox={console,Math,JSON,Array,Object,String,Number,Boolean,Promise,Error,Map,Set,Date,AbortController,
    fetch:(url)=>{
      const name=String(url);requests.push(name);
      if(name.endsWith('/config')&&!holdConfig)return Promise.resolve(reply(configuration));
      return new Promise(resolve=>pending.push({url:name,resolve:body=>resolve(reply(body))}));
    },
    setInterval:(fn,ms)=>{const id=nextId++;intervals.set(id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id),
    setTimeout:(fn,ms)=>{const id=nextId++;timeouts.set(id,{fn,ms});return id;},clearTimeout:id=>timeouts.delete(id)};
  sandbox.window=sandbox;sandbox.self=sandbox;sandbox.globalThis=sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../datafeeds/udf/dist/bundle.js'),'utf8'),sandbox);
  const create=()=>new sandbox.Datafeeds.UDFCompatibleDatafeed('http://isolated.test',1000);
  const feed=create();await flush();
  return {feed,create,intervals,timeouts,requests,pending};
}

test('datafeed disposal stops all polling timers and cannot be restarted by late subscriptions',async()=>{
  const {feed,intervals,requests}=await fixture();
  feed.subscribeBars(symbol,'5',()=>{},'bars');feed.subscribeQuotes(['us:AAA.US'],['us:AAA.US'],()=>{},'quotes');
  assert.equal(intervals.size,3);
  const oldCallbacks=[...intervals.values()].map(t=>t.fn),before=requests.length;
  assert.equal(typeof feed.dispose,'function');feed.dispose();feed.dispose();
  assert.equal(intervals.size,0,'closed datafeeds retained repeating timers');
  feed.subscribeBars(symbol,'5',()=>{},'late-bars');feed.subscribeQuotes(['us:BBB.US'],[],()=>{},'late-quotes');
  for(const callback of oldCallbacks)callback();await flush();
  assert.equal(intervals.size,0);assert.equal(requests.length,before);
});

test('disposing one datafeed keeps another chart subscribed and updating',async()=>{
  const {feed,create,intervals,pending}=await fixture();const other=create();await flush();
  let updates=0;other.subscribeBars(symbol,'5',()=>updates++,'other');
  assert.equal(typeof feed.dispose,'function');feed.dispose();assert.equal(intervals.size,1);
  other._dataPulseProvider._updateData();await flush();pending.find(r=>r.url.includes('/history?')).resolve(raw);await flush();
  assert.equal(updates,1);other.dispose();assert.equal(intervals.size,0);
});

test('late history replies cannot repopulate caches or call a removed chart',async()=>{
  const {feed,pending}=await fixture();const history=feed._historyProvider;let delivered=0,errors=0;
  history.applyChanlunUpdate(raw,{symbol:symbol.ticker,resolution:'5'});assert.equal(history.bars_result.size,1);
  feed.getBars(symbol,'5',{from:0,to:2000,countBack:2,firstDataRequest:true},()=>delivered++,()=>errors++);await flush();
  assert.equal(typeof feed.dispose,'function');feed.dispose();assert.equal(history.bars_result.size,0);
  pending.find(r=>r.url.includes('/history?')).resolve(raw);await flush();
  history.applyChanlunUpdate(raw,{symbol:symbol.ticker,resolution:'5'});
  assert.equal(history.bars_result.size,0);assert.equal(delivered,0);assert.equal(errors,0);
  assert.equal(history._activeHistoryRequests,0);
});

test('late configuration and onReady callbacks do not revive a disposed datafeed',async()=>{
  const {feed,pending,intervals}=await fixture(true);let ready=0;
  feed.onReady(()=>ready++);assert.equal(typeof feed.dispose,'function');feed.dispose();
  pending.find(r=>r.url.endsWith('/config')).resolve(configuration);await flush();
  assert.equal(ready,0);assert.equal(intervals.size,0);
});

test('late direct quotes do not call a disposed chart',async()=>{
  const {feed,pending}=await fixture();let calls=0;
  feed.getQuotes(['us:AAA.US'],()=>calls++,()=>calls++);await flush();
  assert.equal(typeof feed.dispose,'function');feed.dispose();
  pending.find(r=>r.url.includes('/quotes?')).resolve({s:'ok',d:[]});await flush();assert.equal(calls,0);
});

test('closing one chart does not cancel a symbol lookup shared with another chart',async()=>{
  const {feed,create,pending}=await fixture(),other=create();await flush();let old=0,current=0,errors=0;
  feed.resolveSymbol(symbol.ticker,()=>old++,()=>errors++);other.resolveSymbol(symbol.ticker,()=>current++,()=>errors++);await flush();
  const lookups=pending.filter(r=>r.url.includes('/symbols?'));assert.equal(lookups.length,1);
  assert.equal(typeof feed.dispose,'function');feed.dispose();
  lookups[0].resolve({name:'AAA.US',ticker:symbol.ticker,exchange:'us',listed_exchange:'US',minmov:1,pricescale:100});await flush();
  assert.equal(old,0);assert.equal(current,1);assert.equal(errors,0);other.dispose();
});

test('disposed datafeeds do not issue fresh history, quote or symbol requests',async()=>{
  const {feed,requests}=await fixture();assert.equal(typeof feed.dispose,'function');feed.dispose();
  const before=requests.length;let callbacks=0;
  feed.getBars(symbol,'5',{from:0,to:2000,countBack:2,firstDataRequest:true},()=>callbacks++,()=>callbacks++);
  feed.getQuotes([symbol.ticker],()=>callbacks++,()=>callbacks++);
  feed.resolveSymbol(symbol.ticker,()=>callbacks++,()=>callbacks++);
  feed.searchSymbols('AAA','us','stock',()=>callbacks++);feed.getServerTime(()=>callbacks++);await flush();
  assert.equal(requests.length,before);assert.equal(callbacks,0);
});

test('a quote listener exception retires its request only once',async()=>{
  const {feed,pending}=await fixture();const provider=feed._quotesPulseProvider;
  provider.subscribeQuotes(['first'],['first'],()=>{throw Error('isolated listener failure');},'first');
  provider.subscribeQuotes(['second'],['second'],()=>{},'second');provider._updateQuotes(1);await flush();
  const quotes=pending.filter(r=>r.url.includes('/quotes?'));assert.equal(quotes.length,2);
  quotes[0].resolve({s:'ok',d:[]});await flush();assert.equal(provider._requestsPending,1);
  provider._updateQuotes(1);await flush();
  const current=pending.filter(r=>r.url.includes('/quotes?'));
  assert.equal(current.length,3);assert.equal(current.filter(r=>r.url.includes('symbols=second')).length,1);
  quotes[1].resolve({s:'ok',d:[]});current[2].resolve({s:'ok',d:[]});await flush();assert.equal(provider._requestsPending,0);
});

test('a replaced quote subscription cannot receive its old request callback',async()=>{
  const {feed,pending}=await fixture();const provider=feed._quotesPulseProvider;let old=0,current=0;
  provider.subscribeQuotes(['old'],['old'],()=>old++,'shared');provider._updateQuotes(1);await flush();
  provider.subscribeQuotes(['new'],['new'],()=>current++,'shared');
  pending.find(r=>r.url.includes('/quotes?')).resolve({s:'ok',d:[]});await flush();
  assert.equal(old,0);assert.equal(current,0);
});

test('disposal before queued quote work starts avoids issuing the request',async()=>{
  const {feed,pending}=await fixture(),provider=feed._quotesPulseProvider;
  provider.subscribeQuotes(['old'],['old'],()=>{},'queued');provider._updateQuotes(1);
  assert.equal(pending.filter(r=>r.url.includes('/quotes?')).length,0);
  feed.dispose();await flush();
  assert.equal(pending.filter(r=>r.url.includes('/quotes?')).length,0);
  assert.equal(provider._requestsPending,0);
});
