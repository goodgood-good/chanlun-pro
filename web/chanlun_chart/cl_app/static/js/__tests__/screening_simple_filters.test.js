'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {projectSignalSelection,changeSignalPeriod,changeSignalType,filterRows}=require('../screening_workbench.js');

const PERIODS=['1m','5m','30m'];
const POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const BUY=POINTS.filter(point=>point.endsWith('buy'));
const SELL=POINTS.filter(point=>point.endsWith('sell'));
const matrix=(periods=PERIODS,points=POINTS)=>Object.fromEntries(
  PERIODS.map(period=>[period,periods.includes(period)?[...points]:[]]));
const pointTypes=type=>type==='all'?POINTS:type==='buy'?BUY:type==='sell'?SELL:[type];
const frozen=value=>{
  if(value&&typeof value==='object') {
    Object.values(value).forEach(frozen);Object.freeze(value);
  }
  return value;
};
const filters={market:'all',origin:'screening',point_status:'confirmed',stage:'all',review:'all',sector:'',sort:'recent'};
const candidate=(id,frequency,point_type,extra={})=>({id,market:'a',code:'SH.600088',frequency,
  origin:'screening',stage:'confirmed',point:{point_type,status:'confirmed'},evidence_available:true,...extra});
const matched=(rows,selection,extra={})=>filterRows(rows,{...filters,signal_selection:selection,...extra},{},{},'run').map(row=>row.id);

test('undefined selection projects the eighteen selected cells and explicit empty stays empty',()=>{
  assert.deepEqual(projectSignalSelection(undefined),{
    period:'all',point:'all',periods:PERIODS,points:POINTS,empty:false});
  for(const selection of [{},[],matrix([],[])])
    assert.deepEqual(projectSignalSelection(selection),{
      period:'custom',point:'custom',periods:[],points:[],empty:true});
});

test('every ordinary period and type combination projects to its exact controls',()=>{
  for(const period of ['all',...PERIODS])for(const point of ['all','buy','sell',...POINTS]) {
    const periods=period==='all'?PERIODS:[period],points=pointTypes(point);
    assert.deepEqual(projectSignalSelection(matrix(periods,points)),{period,point,periods,points,empty:false},`${period}/${point}`);
  }
});

test('two participating periods are represented by custom period with their ordered membership',()=>{
  for(const periods of [['1m','5m'],['1m','30m'],['5m','30m']])
    assert.deepEqual(projectSignalSelection(matrix(periods,SELL)),{
      period:'custom',point:'sell',periods,points:SELL,empty:false});
});

test('uniform uncommon type subsets project as custom while retaining exact points',()=>{
  const points=['1buy','3sell'];
  assert.deepEqual(projectSignalSelection(matrix(['1m','30m'],points)),{
    period:'custom',point:'custom',periods:['1m','30m'],points,empty:false});
});

test('nonrectangular selections never claim all buy or sell from their union',()=>{
  for(const [selection,points] of [
    [{'1m':BUY,'5m':SELL,'30m':POINTS},POINTS],
    [{'1m':['1buy'],'5m':['2buy'],'30m':['3buy']},BUY],
    [{'1m':['1sell'],'5m':['2sell'],'30m':['3sell']},SELL],
  ]) assert.deepEqual(projectSignalSelection(selection),{
    period:'all',point:'custom',periods:PERIODS,points,empty:false});
});

test('projection normalizes duplicates ordering missing periods and unknown cells without mutation',()=>{
  const selection=frozen({'30m':['3sell','1buy','3sell','either'],'1m':['2buy','1buy'],'5m':'3buy',d:['3sell']});
  const before=JSON.stringify(selection);
  assert.deepEqual(projectSignalSelection(selection),{
    period:'custom',point:'custom',periods:['1m','30m'],points:['1buy','2buy','3sell'],empty:false});
  assert.equal(JSON.stringify(selection),before);
});

test('changing period immediately keeps the selected type union in the new physical periods',()=>{
  const selection=frozen({'1m':['1buy'],'5m':[],'30m':['3sell']});
  assert.deepEqual(changeSignalPeriod(selection,'5m'),matrix(['5m'],['1buy','3sell']));
  assert.deepEqual(changeSignalPeriod(selection,'all'),matrix(PERIODS,['1buy','3sell']));
  assert.deepEqual(changeSignalPeriod(selection,'30m'),matrix(['30m'],['1buy','3sell']));
  assert.deepEqual(selection,{'1m':['1buy'],'5m':[],'30m':['3sell']});
});

test('setting the same participating periods preserves nonrectangular cells instead of expanding them',()=>{
  const selection=frozen({'30m':['3buy'],'1m':['1buy','1buy'],'5m':['2buy']});
  assert.deepEqual(changeSignalPeriod(selection,'all'),{'1m':['1buy'],'5m':['2buy'],'30m':['3buy']});
  assert.deepEqual(changeSignalPeriod(frozen({'5m':['3sell','1buy']}),'5m'),matrix(['5m'],['1buy','3sell']));
});

test('changing type acts on precisely the currently participating periods',()=>{
  const selection=frozen({'1m':['1buy'],'5m':[],'30m':['3sell']});
  for(const type of ['all','buy','sell',...POINTS])
    assert.deepEqual(changeSignalType(selection,type),matrix(['1m','30m'],pointTypes(type)),type);
  assert.deepEqual(changeSignalType(matrix(['5m'],BUY),'3buy'),matrix(['5m'],['3buy']));
});

test('changing type uniformly replaces differing per-period types without activating other periods',()=>{
  const selection=frozen({'1m':['1buy'],'5m':['2buy'],'30m':[]});
  const changed=changeSignalType(selection,'sell');
  assert.deepEqual(changed,matrix(['1m','5m'],SELL));
  assert.deepEqual(projectSignalSelection(changed),{
    period:'custom',point:'sell',periods:['1m','5m'],points:SELL,empty:false});
});

test('period and type controls each restore a useful selection from explicit empty state',()=>{
  const empty=frozen(matrix([],[]));
  for(const period of ['all',...PERIODS])
    assert.deepEqual(changeSignalPeriod(empty,period),matrix(period==='all'?PERIODS:[period]));
  for(const type of ['all','buy','sell',...POINTS])
    assert.deepEqual(changeSignalType(empty,type),matrix(PERIODS,pointTypes(type)));
  assert.deepEqual(empty,matrix([],[]));
});

test('default selections update directly without a separate pending scope',()=>{
  assert.deepEqual(changeSignalPeriod(undefined,'1m'),matrix(['1m']));
  assert.deepEqual(changeSignalType(undefined,'3buy'),matrix(PERIODS,['3buy']));
  assert.deepEqual(changeSignalType(changeSignalPeriod(undefined,'5m'),'sell'),matrix(['5m'],SELL));
});

test('invalid and display-only custom values are no-ops with normalized fresh output',()=>{
  const selection=frozen({'30m':['3sell','1buy','3sell'],'1m':['2buy'],'d':['1buy']});
  const expected={'1m':['2buy'],'5m':[],'30m':['1buy','3sell']};
  for(const value of ['custom','unknown','',null,undefined,'none']) {
    assert.deepEqual(changeSignalPeriod(selection,value),expected);
    assert.deepEqual(changeSignalType(selection,value),expected);
  }
  assert.deepEqual(changeSignalPeriod(selection,'buy'),expected);
  assert.deepEqual(changeSignalType(selection,'5m'),expected);
  assert.deepEqual(changeSignalPeriod(undefined,'custom'),matrix());
  assert.deepEqual(changeSignalType(matrix([],[]),'custom'),matrix([],[]));
});

test('custom to period to type to individual-cell adjustment remains one canonical selection',()=>{
  const initial=frozen({'1m':['1buy'],'5m':[],'30m':['3buy']});
  const period=changeSignalPeriod(initial,'5m');
  assert.deepEqual(period,matrix(['5m'],['1buy','3buy']));
  const type=changeSignalType(frozen(period),'sell');
  assert.deepEqual(type,matrix(['5m'],SELL));
  const fine={...type,'30m':['3buy']};
  assert.deepEqual(projectSignalSelection(fine),{
    period:'custom',point:'custom',periods:['5m','30m'],points:['3buy',...SELL],empty:false});
  const uniform=changeSignalType(frozen(fine),'3buy');
  assert.deepEqual(uniform,matrix(['5m','30m'],['3buy']));
  assert.deepEqual(projectSignalSelection(uniform),{
    period:'custom',point:'3buy',periods:['5m','30m'],points:['3buy'],empty:false});
  assert.deepEqual(initial,{'1m':['1buy'],'5m':[],'30m':['3buy']});
});

test('projection and no-op period choices never mutate matrix or share mutable output with input',()=>{
  const selection=matrix(['1m'],BUY),before=JSON.stringify(selection);
  const view=projectSignalSelection(selection),changed=changeSignalPeriod(selection,'1m');
  view.periods.push('30m');view.points.push('3sell');changed['1m'].push('3sell');changed['30m'].push('3buy');
  assert.equal(JSON.stringify(selection),before);
});

test('immediate controls retain row OR and the same current historical and watchlist eligibility',()=>{
  const rows=[candidate('one','1m','3buy'),candidate('five','5m','3buy'),candidate('sell','5m','3sell'),
    candidate('pending','5m','3buy',{refresh_state:'pending',result_source:'older'}),
    candidate('limited','5m','3buy',{refresh_state:'limited',result_source:'older',evidence_available:false}),
    candidate('unverified','5m','3buy',{evidence_available:false}),candidate('wrong-source','5m','3buy',{result_source:'older'}),
    {id:'watch',origin:'watchlist',market:'a',code:'SH.600088',frequency:'30m',stage:'monitoring',point:{}}];
  const selection=changeSignalType(changeSignalPeriod(undefined,'5m'),'3buy');
  assert.deepEqual(matched(rows,selection),['five','pending','limited']);
  assert.deepEqual(matched(rows,selection,{include_previous:false}),['five']);
  assert.deepEqual(matched(rows,selection,{origin:'all'}),['five','pending','limited','watch']);
  assert.deepEqual(matched(rows,changeSignalPeriod(selection,'all')),['one','five','pending','limited']);
  const fine={...selection,'1m':['3buy']};
  assert.deepEqual(matched(rows,fine),['one','five','pending','limited']);
});
