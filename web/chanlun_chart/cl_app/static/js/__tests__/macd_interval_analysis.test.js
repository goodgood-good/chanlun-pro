'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {computeStats,parseExport,rawHtfInput,rangeIndices,Controller}=require('../macd_interval_analysis.js');

test('signed line statistics and histogram areas use each plotted bar exactly once',()=>{
  const r=computeStats([100,200,300,400],[2,3,-1,-4],0,3,{difArr:[-2,-1,1,4],deaArr:[-3,-1,0,2]});
  assert.deepEqual([r.posArea,r.negArea,r.netArea,r.absoluteArea],[5,5,0,10]);
  assert.deepEqual([r.histMax,r.histMin,r.histMean,r.histAbsMean],[3,-4,0,2.5]);
  assert.deepEqual([r.difMax,r.difMin,r.difMean],[4,-2,.5]);
  assert.deepEqual([r.deaMax,r.deaMin,r.deaMean],[2,-3,-.5]);
});

test('missing values are not zero and each line retains its independent valid samples',()=>{
  const r=computeStats([1,2,3,4],[null,undefined,NaN,0],0,3,{difArr:[1,2,Infinity,null],deaArr:[null,4,6,8]});
  assert.equal(r.barCount,1);assert.equal(r.missingBars,3);assert.equal(r.zeroCount,1);
  assert.equal(r.difMean,1.5);assert.equal(r.deaMean,6);
  assert.equal(computeStats([1],[null],0,0).histMax,null);
});

test('historical endpoints and one-bar ranges are included; exclusion applies only at the chart tail',()=>{
  assert.equal(computeStats([1,2,3],[2,4,8],0,1,{excludeLast:true}).posArea,6);
  assert.equal(computeStats([1,2,3],[2,4,8],1,1).posArea,4);
  const tail=computeStats([1,2,3],[2,4,8],2,2,{excludeLast:true});
  assert.equal(tail.barCount,0);assert.equal(tail.excludedLast,true);
});

test('exact range bounds do not snap to a bar outside the interval',()=>{
  assert.deepEqual(rangeIndices([100,200,300],290,110),{startIdx:1,endIdx:1,from:110,to:290,clipped:false});
  assert.equal(rangeIndices([100,200,300],400,500),null);
  assert.equal(rangeIndices([100,200,300],200,200).startIdx,1);
  assert.equal(rangeIndices([1700000000,1700000300],1700000000000,1700000300000).endIdx,1);
});

function exported(values=[2,-4]) {
  return {schema:[{type:'time'},...['Histogram','MACD','Signal'].map(plotTitle=>({type:'value',sourceType:'study',sourceId:'own',plotTitle}))],
    data:[new Float64Array([100,values[0],-2,-3]),new Float64Array([200,values[1],4,2])]};
}
test('export fields are bound to the selected study, including typed rows and reordered columns',()=>{
  const data=exported();data.schema.push({type:'value',sourceType:'study',sourceId:'other',plotTitle:'Histogram'});
  const r=parseExport(data,[{id:'own',name:'MACD_HTF'},{id:'missing',name:'MACD'}]);
  assert.equal(r.metrics[0].recognized,true);assert.equal(r.metrics[1].recognized,false);
  assert.deepEqual(r.metrics[0].hist,[2,-4]);
  assert.throws(()=>parseExport({...data,data:[data.data[0],data.data[0]]},[]),/时间重复/);
});

function controller(exporter) {
  let identity={symbol:'us:AAPL.US',interval:'5'},inputs=[{id:'fast',value:12}];
  const cm={widget:{symbolInterval:()=>identity},chart:{getAllStudies:()=>[{id:'own',name:'MACD'}],
    getStudyById:()=>({getInputValues:()=>inputs}),exportData:exporter,removeEntity:()=>{},createShape:()=>null}};
  cm.udf_datafeed={_historyProvider:{bars_result:new Map([['us:aapl.us5',{
    chart_times:[100,200],higher_macd_raw_hist:[2,-4],higher_macd_raw_dif:[-2,4],higher_macd_raw_dea:[-3,2],
    higher_macd_hist:[200,-400],macd_hist:[20,-40]}]])}};
  const c=new Controller(cm);c.startTime=100;c.endTime=200;c.render=()=>{};
  return {c,cm,changeSymbol:()=>{identity={symbol:'us:QQQ.US',interval:'5'};},changeInputs:()=>{inputs=[{id:'fast',value:20}];}};
}
test('statistics use raw HTF instead of smooth exports or native MACD without rounding or doubling',async()=>{
  const {c}=controller(async()=>{throw new Error('smooth export must not provide measurements');});await c.computeAndRender();
  assert.equal(c.results.metrics[0].stats.posArea,2);assert.equal(c.results.metrics[0].stats.negArea,4);
});
test('reported coverage ends at the last included bar when the live tail is excluded',async()=>{
  const {c}=controller(async()=>exported());c.excludeLast=true;await c.computeAndRender();
  assert.equal(c.results.to,200);assert.equal(c.results.includedTo,100);
  c.startTime=200;await c.computeAndRender();
  assert.equal(c.results.includedFrom,null);assert.equal(c.results.metrics[0].stats.barCount,0);
});
test('unavailable or mismatched raw HTF never uses a previous symbol or either visual indicator',async()=>{
  const ctx=controller(()=>exported());ctx.changeSymbol();await ctx.c.computeAndRender();
  assert.equal(ctx.c.results,null);assert.match(ctx.c.status,/原始数据/);
  const own=controller(()=>exported());delete own.cm.udf_datafeed._historyProvider.bars_result.get('us:aapl.us5').higher_macd_raw_hist;
  assert.throws(()=>rawHtfInput(own.cm),/原始数据未对齐/);
  await own.c.computeAndRender();assert.equal(own.c.results,null);
});
