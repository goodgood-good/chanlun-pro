'use strict';

const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(setImmediate);};

async function fixture(){
  const requests=[];
  const sb={console,Math,JSON,Array,Object,String,Number,Boolean,Promise,Error,Map,Set,Date,
    fetch:async()=>({ok:true,text:async()=>JSON.stringify({supports_search:true,supported_resolutions:['5']})}),
    setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,clearInterval:()=>{}};
  sb.window=sb;sb.self=sb;sb.globalThis=sb;vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../../datafeeds/udf/dist/bundle.js'),'utf8'),sb);
  const feed=new sb.Datafeeds.UDFCompatibleDatafeed('http://isolated.test');await flush();
  feed._quotesProvider.getQuotes=symbols=>new Promise((resolve,reject)=>requests.push({symbols:[...symbols],resolve,reject}));
  return {feed,provider:feed._quotesPulseProvider,requests};
}

test('a slow quote subscription does not block a completed subscription from refreshing',async()=>{
  const {provider,requests}=await fixture(),received=[];
  provider.subscribeQuotes(['slow'],['slow'],()=>{},'slow');
  provider.subscribeQuotes(['fast'],['fast'],v=>received.push(v),'fast');
  provider._updateQuotes(1);await flush();requests[1].resolve([{n:'fast',v:{lp:20}}]);await flush();
  provider._updateQuotes(1);await flush();
  assert.equal(requests.length,3);assert.deepEqual(requests[2].symbols,['fast']);
  assert.equal(requests.filter(r=>r.symbols[0]==='slow').length,1);
  requests[2].resolve([{n:'fast',v:{lp:21}}]);requests[0].resolve([]);await flush();
  assert.deepEqual(received.map(v=>v[0].v.lp),[20,21]);assert.equal(provider._requestsPending,0);
});

for(const firstType of [0,1])test('overlapping quote refresh types are serialized without losing type '+(1-firstType),async()=>{
  const {provider,requests}=await fixture();
  provider.subscribeQuotes(['all'],['priority'],()=>{},'one');
  provider._updateQuotes(firstType);provider._updateQuotes(1-firstType);provider._updateQuotes(1-firstType);await flush();
  assert.equal(requests.length,1);requests[0].resolve([]);await flush();
  assert.equal(requests.length,2,'the other refresh type was silently dropped');
  assert.deepEqual(requests[1].symbols,firstType===1?['all']:['priority']);
  requests[1].resolve([]);await flush();assert.equal(provider._requestsPending,0);
});

test('ordinary updates still run when fast and general timers repeatedly coincide',async()=>{
  const {provider,requests}=await fixture();provider.subscribeQuotes(['all'],['priority'],()=>{},'one');
  for(let minute=0;minute<3;minute++){
    const start=requests.length;provider._updateQuotes(1);provider._updateQuotes(0);await flush();
    requests[start].resolve([]);await flush();assert.equal(requests.length,start+2);
    requests[start+1].resolve([]);await flush();
  }
  assert.equal(requests.filter(r=>r.symbols[0]==='all').length,3);
});

test('quote pulse skips empty symbol lists without blocking a nonempty update',async()=>{
  const {provider,requests}=await fixture();provider.subscribeQuotes(['all'],[],()=>{},'one');
  provider._updateQuotes(1);await flush();assert.equal(requests.length,0);
  provider._updateQuotes(0);await flush();assert.deepEqual(requests[0].symbols,['all']);
  requests[0].resolve([]);await flush();
});

test('a replacement quote subscription starts without waiting for a retired request',async()=>{
  const {provider,requests}=await fixture(),old=[],current=[];
  provider.subscribeQuotes(['old-all'],['old-fast'],v=>old.push(v),'shared');
  provider._updateQuotes(1);provider._updateQuotes(0);await flush();
  provider.unsubscribeQuotes('shared');provider.subscribeQuotes(['new-all'],['new-fast'],v=>current.push(v),'shared');
  provider._updateQuotes(1);await flush();assert.equal(requests.length,2);
  requests[0].resolve([]);await flush();provider._updateQuotes(1);await flush();
  assert.equal(requests.length,2,'old completion unlocked or scheduled work for a new subscription');
  requests[1].resolve([{n:'new-fast',v:{lp:22}}]);await flush();
  assert.equal(old.length,0);assert.equal(current.length,1);assert.equal(provider._requestsPending,0);
});

test('a failed request releases its queued refresh type',async()=>{
  const {provider,requests}=await fixture();provider.subscribeQuotes(['all'],['priority'],()=>{},'one');
  provider._updateQuotes(1);provider._updateQuotes(0);await flush();
  requests[0].reject(Error('isolated failure'));await flush();assert.equal(requests.length,2);
  assert.deepEqual(requests[1].symbols,['all']);requests[1].resolve([]);await flush();
  assert.equal(provider._requestsPending,0);
});

test('same-kind pulses coalesce and disposal cancels deferred quote work',async()=>{
  const {feed,provider,requests}=await fixture();provider.subscribeQuotes(['all'],['priority'],()=>{},'one');
  provider._updateQuotes(1);provider._updateQuotes(1);provider._updateQuotes(0);await flush();
  assert.equal(requests.length,1);feed.dispose();requests[0].resolve([]);await flush();
  assert.equal(requests.length,1);assert.equal(provider._requestsPending,0);
});
