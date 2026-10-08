'use strict';

const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const symbolA={ticker:'us:AAA.US',name:'us:AAA.US',exchange:'us'};
const symbolB={ticker:'us:BBB.US',name:'us:BBB.US',exchange:'us'};
const keyA='us:aaa.us5',keyB='us:bbb.us5';
const bar=(time,close)=>({time,open:close,high:close,low:close,close,volume:1});
const result=(...bars)=>({bars,meta:{noData:false}});
const raw=(time,close)=>({s:'ok',update:false,bar_time_label:'start',t:[time],o:[close],h:[close],l:[close],c:[close],v:[1],
  macd_dif:[close],macd_dea:[close-1],macd_hist:[1],fxs:[],bis:[],xds:[]});
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(setImmediate);};

async function fixture(realHistory=false){
  const timers=new Map(),requests=[];let timerId=0;
  const sandbox={console,Math,JSON,Array,Object,String,Number,Boolean,Promise,Error,Map,Set,Date,
    fetch:async()=>({ok:true,status:200,text:async()=>JSON.stringify({supports_search:true,supported_resolutions:['5','30']})}),
    setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),
    setInterval:()=>0,clearInterval:()=>{}};
  sandbox.globalThis=sandbox;sandbox.self=sandbox;sandbox.window=sandbox;
  vm.createContext(sandbox);
  const bundle=path.join(__dirname,'../../datafeeds/udf/dist/bundle.js');
  vm.runInContext(fs.readFileSync(bundle,'utf8'),sandbox,{filename:'bundle.js'});
  const datafeed=new sandbox.Datafeeds.UDFCompatibleDatafeed('http://isolated.test',1000);
  await flush();
  const history=datafeed._historyProvider,provider=datafeed._dataPulseProvider;
  const deferred=(...args)=>new Promise((resolve,reject)=>requests.push({args,resolve,reject}));
  if(realHistory)history._requester.sendRequest=deferred;else history.getBars=deferred;
  return {datafeed,history,provider,requests,expire(){
    const timer=[...timers.values()].find(t=>t.delay===provider._requestTimeoutMs);
    assert.ok(timer,'a pulse timeout must exist');timer.fn();
  }};
}

test('an old request cannot deliver bars to a replacement subscription using the same guid',async()=>{
  const {provider,requests}=await fixture(),old=[],current=[];
  provider.subscribeBars(symbolA,'5',b=>old.push(b),'shared');
  const pending=provider._updateDataForSubscriber('shared');
  provider.unsubscribeBars('shared');provider.subscribeBars(symbolB,'30',b=>current.push(b),'shared');
  requests[0].resolve(result(bar(60000,10)));await pending;
  assert.equal(old.length,0);assert.equal(current.length,0);
});

test('replacement subscriptions refresh immediately and an old completion cannot unlock their pending request',async()=>{
  const {provider,requests}=await fixture(),old=[],current=[];
  provider.subscribeBars(symbolA,'5',b=>old.push(b),'shared');provider._updateData();await flush();
  provider.unsubscribeBars('shared');provider.subscribeBars(symbolB,'5',b=>current.push(b),'shared');
  provider._updateData();await flush();assert.equal(requests.length,2);
  requests[0].resolve(result(bar(60000,10)));await flush();
  provider._updateData();await flush();assert.equal(requests.length,2,'old cleanup unlocked a new pending request');
  requests[1].resolve(result(bar(60000,20)));await flush();
  assert.equal(old.length,0);assert.deepEqual(current.map(b=>b.close),[20]);
});

test('a timed-out response is ignored even before another request starts',async()=>{
  const {provider,requests,expire}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider._updateData();await flush();
  expire();await flush();requests[0].resolve(result(bar(60000,10)));await flush();
  assert.equal(received.length,0);
  provider._updateData();await flush();requests[1].resolve(result(bar(60000,20)));await flush();
  assert.deepEqual(received.map(b=>b.close),[20]);
});

test('a timed-out response cannot overwrite a newer price within the same candle',async()=>{
  const {provider,requests,expire}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider._updateData();await flush();
  expire();await flush();provider._updateData();await flush();
  requests[1].resolve(result(bar(60000,20)));await flush();
  requests[0].resolve(result(bar(60000,10)));await flush();
  assert.deepEqual(received.map(b=>b.close),[20]);
});

test('an accepted SSE price supersedes an older poll without blocking the next poll',async()=>{
  const {provider,requests}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider.feedBar(keyA,bar(60000,5));
  provider._updateData();await flush();provider.feedBar(keyA,bar(60000,20),true);
  requests[0].resolve(result(bar(60000,10)));await flush();
  assert.deepEqual(received.map(b=>b.close),[5,20]);
  provider._updateData();await flush();requests[1].resolve(result(bar(60000,30)));await flush();
  assert.deepEqual(received.map(b=>b.close),[5,20,30]);
});

test('an SSE update only invalidates the matching symbol and period',async()=>{
  const {provider,requests}=await fixture(),a=[],b=[];
  provider.subscribeBars(symbolA,'5',v=>a.push(v),'a');provider.subscribeBars(symbolB,'5',v=>b.push(v),'b');
  provider._updateData();await flush();provider.feedBar(keyB,bar(60000,20));
  requests[0].resolve(result(bar(60000,10)));requests[1].resolve(result(bar(60000,15)));await flush();
  assert.deepEqual(a.map(v=>v.close),[10]);assert.deepEqual(b.map(v=>v.close),[20]);
});

test('a rejected older SSE candle does not cancel a valid pending poll',async()=>{
  const {provider,requests}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider.feedBar(keyA,bar(120000,10));
  provider._updateData();await flush();provider.feedBar(keyA,bar(60000,99));
  requests[0].resolve(result(bar(120000,20)));await flush();
  assert.deepEqual(received.map(b=>b.close),[10,20]);
});

test('SSE initialization guards still allow the first successful poll',async()=>{
  const {provider,requests}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider._updateData();await flush();
  provider.feedBar(keyA,bar(60000,20),true);requests[0].resolve(result(bar(60000,10)));await flush();
  assert.deepEqual(received.map(b=>b.close),[10]);
});

test('a previous candle older than the chart tail is not replayed backwards',async()=>{
  const {provider}=await fixture(),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider.feedBar(keyA,bar(120000,20));
  provider._onSubscriberDataReceived('a',result(bar(60000,10),bar(180000,30)));
  assert.deepEqual(received.map(b=>b.time),[120000,180000]);
});

test('switching subscriptions inside the previous-bar callback stops the old final update',async()=>{
  const {provider,requests}=await fixture(),old=[],current=[];
  provider.subscribeBars(symbolA,'5',b=>{
    old.push(b);
    if(b.close===11){provider.unsubscribeBars('shared');provider.subscribeBars(symbolB,'5',v=>current.push(v),'shared');}
  },'shared');
  provider.feedBar(keyA,bar(60000,10));provider._updateData();await flush();
  requests[0].resolve(result(bar(60000,11),bar(120000,12)));await flush();
  assert.deepEqual(old.map(b=>b.close),[10,11]);assert.equal(current.length,0);
});

test('a superseded real history response cannot replace cached candles or indicators',async()=>{
  const {provider,history,requests}=await fixture(true),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider.feedBar(keyA,bar(60000,5));
  provider._updateData();await flush();
  history.applyChanlunUpdate(raw(60,20),{symbol:symbolA.ticker,resolution:'5'});
  provider.feedBar(keyA,bar(60000,20),true);
  requests[0].resolve(raw(60,10));await flush();
  assert.equal(history.bars_result.get(keyA).bars.at(-1).close,20);
  assert.equal(history.bars_result.get(keyA).macd_dif.at(-1),20);
  assert.deepEqual(received.map(b=>b.close),[5,20]);
  assert.equal(history._activeHistoryRequests,0);
});

test('timeout ownership expires before a same-turn network response can write the cache',async()=>{
  const {provider,history,requests,expire}=await fixture(true),received=[];
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');
  history.applyChanlunUpdate(raw(60,20),{symbol:symbolA.ticker,resolution:'5'});
  provider.feedBar(keyA,bar(60000,20));provider._updateData();await flush();
  expire();requests[0].resolve(raw(60,10));await flush();
  assert.equal(history.bars_result.get(keyA).bars.at(-1).close,20);
  assert.deepEqual(received.map(b=>b.close),[20]);
  assert.equal(history._activeHistoryRequests,0);
});

test('a late truncated-history follow-up cannot overwrite SSE or request another chunk',async()=>{
  const {provider,history,requests}=await fixture(true),received=[];
  history._limitedServerResponse={maxResponseLength:1,expectedOrder:'earliestFirst'};
  provider.subscribeBars(symbolA,'5',b=>received.push(b),'a');provider.feedBar(keyA,bar(60000,5));
  provider._updateData();await flush();requests[0].resolve(raw(60,10));await flush();
  assert.equal(requests.length,2);
  history.applyChanlunUpdate(raw(120,20),{symbol:symbolA.ticker,resolution:'5'});
  provider.feedBar(keyA,bar(120000,20),true);
  requests[1].resolve(raw(120,99));await flush();
  assert.equal(requests.length,2);
  assert.equal(history.bars_result.get(keyA).bars.at(-1).close,20);
  assert.deepEqual(received.map(b=>b.close),[5,20]);
  assert.equal(history._activeHistoryRequests,0);
});
