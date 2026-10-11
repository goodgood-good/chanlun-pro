'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {normalizeSignalFilter,setSignalPeriod,applySignalPreset,normalizeViewState,filterRows}=require('../screening_workbench.js');
const PERIODS=['1m','5m','30m'],POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const BUY=POINTS.filter(p=>p.endsWith('buy')),SELL=POINTS.filter(p=>p.endsWith('sell'));
const selected=(period='5m',points=POINTS)=>({signal_period:period,signal_points:[...points]});
const frozen=value=>{if(value&&typeof value==='object'){Object.values(value).forEach(frozen);Object.freeze(value);}return value;};
const legacy=matrix=>({version:1,filters:{signal_selection:matrix}});

test('normalization defaults to 5m six types and preserves explicit empty types',()=>{
  for(const input of [undefined,null,{}])assert.deepEqual(normalizeSignalFilter(input),selected());
  for(const period of PERIODS){
    assert.deepEqual(normalizeSignalFilter({signal_period:period}),selected(period));
    assert.deepEqual(normalizeSignalFilter(selected(period,[])),selected(period,[]));
  }
});

test('normalization orders and deduplicates real types without accepting old selector fields',()=>{
  const input=frozen({signal_period:'30m',signal_points:['3sell','1buy','3sell','unknown','either'],
    signal_selection:{'1m':['2buy']},point:'2buy',frequency:'1m',period_points:{'1m':'2buy'}});
  assert.deepEqual(normalizeSignalFilter(input),selected('30m',['1buy','3sell']));
  assert.deepEqual(normalizeSignalFilter({signal_selection:{'1m':['2buy']},point:'2buy',frequency:'1m'}),selected());
  assert.deepEqual(normalizeSignalFilter({signal_period:'d',signal_points:['3buy']}),selected('5m',['3buy']));
  assert.deepEqual(normalizeSignalFilter({signal_period:'1m',signal_points:['either','invalid']}),selected('1m',[]));
});

test('non-array point values use all six types while an array with no valid type stays empty',()=>{
  for(const signal_points of [null,undefined,'3buy',false,0,{}, {length:1,0:'3buy'}])
    assert.deepEqual(normalizeSignalFilter({signal_period:'30m',signal_points}),selected('30m'));
  assert.deepEqual(normalizeSignalFilter({signal_period:'30m',signal_points:['unknown']}),selected('30m',[]));
});

test('changing period preserves each exact type subset including empty',()=>{
  for(const previous of PERIODS)for(const target of PERIODS)for(let mask=0;mask<64;mask++){
    const points=POINTS.filter((_p,index)=>mask&(1<<index));
    const input=frozen(selected(previous,points));
    assert.deepEqual(setSignalPeriod(input,target),selected(target,points));
    assert.deepEqual(input,selected(previous,points));
  }
});

test('invalid period changes normalize without changing the valid selected period',()=>{
  const input=frozen({signal_period:'30m',signal_points:['3sell','1buy','3sell']});
  for(const value of ['all','custom','none','d','',null,undefined,1])
    assert.deepEqual(setSignalPeriod(input,value),selected('30m',['1buy','3sell']));
  assert.deepEqual(setSignalPeriod(undefined,'1m'),selected('1m'));
});

test('four presets replace types only and never change the selected physical period',()=>{
  for(const period of PERIODS)for(const points of [[],['1buy'],['1buy','3sell'],POINTS])
    for(const [preset,expected] of [['all',POINTS],['buy',BUY],['sell',SELL],['none',[]]])
      assert.deepEqual(applySignalPreset(frozen(selected(period,points)),preset),selected(period,expected));
  assert.deepEqual(applySignalPreset(undefined,'buy'),selected('5m',BUY));
});

test('unknown presets normalize without replacing the chosen period or types',()=>{
  const input=frozen({signal_period:'1m',signal_points:['3sell','1buy','3sell']});
  for(const value of ['custom','3buy','all-periods','',null,undefined])
    assert.deepEqual(applySignalPreset(input,value),selected('1m',['1buy','3sell']));
  assert.deepEqual(applySignalPreset(selected('30m',[]),'unknown'),selected('30m',[]));
});

test('helpers return independent arrays without mutating frozen inputs',()=>{
  const input=frozen(selected('1m',['1buy','3sell']));
  const outputs=[normalizeSignalFilter(input),setSignalPeriod(input,'1m'),setSignalPeriod(input,'30m'),
    applySignalPreset(input,'unknown'),applySignalPreset(input,'buy')];
  for(const output of outputs){assert.notEqual(output.signal_points,input.signal_points);output.signal_points.push('2sell');}
  assert.deepEqual(input,selected('1m',['1buy','3sell']));
  const one=applySignalPreset(input,'all'),two=applySignalPreset(input,'all');
  one.signal_points.pop();assert.deepEqual(two,selected('1m'));
});

test('period changes and type presets keep one canonical selection through empty and fine adjustments',()=>{
  const initial=frozen(selected('1m',['1buy','3sell']));
  const period=setSignalPeriod(initial,'30m');assert.deepEqual(period,selected('30m',['1buy','3sell']));
  const buy=applySignalPreset(period,'buy');assert.deepEqual(buy,selected('30m',BUY));
  const fine={...buy,signal_points:buy.signal_points.filter(p=>p!=='2buy')};
  assert.deepEqual(setSignalPeriod(fine,'5m'),selected('5m',['1buy','3buy']));
  assert.deepEqual(setSignalPeriod(applySignalPreset(fine,'none'),'5m'),selected('5m',[]));
  assert.deepEqual(applySignalPreset(selected('5m',[]),'sell'),selected('5m',SELL));
});

test('new version two views restore only new signal fields alongside independent refinements',()=>{
  const input=frozen({version:2,filters:{signal_period:'30m',signal_points:['3sell','1buy'],
    signal_selection:{'1m':['2buy']},query:'keep',include_previous:false,review:'reviewed'},watch_group:'saved'});
  const result=normalizeViewState(input);
  assert.deepEqual(normalizeSignalFilter(result.filters),selected('30m',['1buy','3sell']));
  assert.equal(result.filters.query,'keep');assert.equal(result.filters.include_previous,false);
  assert.equal(result.filters.review,'reviewed');assert.equal(result.watch_group,'saved');
  assert.equal(Object.hasOwn(result.filters,'signal_selection'),false);
  assert.deepEqual(normalizeSignalFilter(normalizeViewState({version:2,filters:{signal_selection:{'1m':['2buy']}}}).filters),selected());
  assert.deepEqual(normalizeSignalFilter(normalizeViewState({version:2,filters:selected('1m',[])}).filters),selected('1m',[]));
});

test('legacy version one single nonempty row keeps that exact period and types',()=>{
  for(const period of PERIODS){
    const matrix=Object.fromEntries(PERIODS.map(p=>[p,p===period?['3sell','1buy','1buy']:[]]));
    assert.deepEqual(normalizeSignalFilter(normalizeViewState(frozen(legacy(matrix))).filters),selected(period,['1buy','3sell']));
  }
});

test('legacy multirow migration prefers 5m then 30m then 1m without taking a union',()=>{
  const cases=[
    [{'1m':['1buy'],'5m':['2sell'],'30m':['3buy']},selected('5m',['2sell'])],
    [{'1m':['1buy'],'30m':['3sell']},selected('30m',['3sell'])],
    [{'5m':[],'1m':['1buy'],'30m':['3sell']},selected('30m',['3sell'])],
    [{'1m':['1buy'],'5m':['2buy']},selected('5m',['2buy'])],
    [{'1m':['1sell']},selected('1m',['1sell'])],
  ];
  for(const [matrix,expected] of cases)assert.deepEqual(normalizeSignalFilter(normalizeViewState(frozen(legacy(matrix))).filters),expected);
});

test('legacy migration chooses a row only after validating its actual point types',()=>{
  assert.deepEqual(normalizeSignalFilter(normalizeViewState(legacy({
    '1m':['1buy'],'5m':['either','unknown'],'30m':['3sell']})).filters),selected('30m',['3sell']));
  assert.deepEqual(normalizeSignalFilter(normalizeViewState(legacy({
    '1m':['1buy'],'5m':'3buy','30m':[]})).filters),selected('1m',['1buy']));
  assert.deepEqual(normalizeSignalFilter(normalizeViewState(legacy({
    '5m':['unknown'],d:['3buy']})).filters),selected('5m',[]));
});

test('legacy explicit empty stays empty while absence of a matrix gets the new defaults',()=>{
  for(const matrix of [{},{'1m':[],'5m':[],'30m':[]}])
    assert.deepEqual(normalizeSignalFilter(normalizeViewState(legacy(matrix)).filters),selected('5m',[]));
  assert.deepEqual(normalizeSignalFilter(normalizeViewState({version:1,filters:{}}).filters),selected());
  assert.deepEqual(normalizeSignalFilter(normalizeViewState(null).filters),selected());
  assert.deepEqual(normalizeSignalFilter(normalizeViewState({version:999,filters:selected('1m',[])}).filters),selected());
});

test('new helper results select one period and preserve current historical and watchlist eligibility',()=>{
  const base={market:'a',code:'SH.600088',origin:'screening',stage:'confirmed',evidence_available:true};
  const row=(id,frequency,point_type,extra={})=>({...base,id,frequency,point:{point_type,status:'confirmed'},...extra});
  const rows=[row('one','1m','3buy'),row('five','5m','3buy'),row('sell','5m','3sell'),
    row('pending','5m','3buy',{refresh_state:'pending',result_source:'old'}),
    row('limited','5m','3buy',{refresh_state:'limited',result_source:'old',evidence_available:false}),
    row('bad','5m','3buy',{evidence_available:false}),row('wrong-source','5m','3buy',{result_source:'old'}),
    {id:'watch',market:'a',code:'SH.600088',origin:'watchlist',stage:'monitoring',frequency:'30m',point:{}}];
  const filters={market:'all',origin:'screening',point_status:'confirmed',...applySignalPreset(selected('5m'),'buy')};
  const ids=extra=>filterRows(rows,{...filters,...extra},{},{},'run').map(r=>r.id);
  assert.deepEqual(ids(),['five','pending','limited']);
  assert.deepEqual(ids({include_previous:false}),['five']);
  assert.deepEqual(ids({origin:'all'}),['five','pending','limited','watch']);
  assert.deepEqual(ids(setSignalPeriod(filters,'1m')),['one']);
  assert.deepEqual(ids({...applySignalPreset(filters,'none'),origin:'all'}),['watch']);
});
