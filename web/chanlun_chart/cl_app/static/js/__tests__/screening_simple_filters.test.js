'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {applySignalPreset,setSignalPeriod,filterRows,normalizeViewState,advancedFilterCount}=require('../screening_workbench.js');
const PERIODS=['1m','5m','30m'],POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const BUY=POINTS.filter(point=>point.endsWith('buy')),SELL=POINTS.filter(point=>point.endsWith('sell'));
const matrix=(periods=PERIODS,points=POINTS)=>Object.fromEntries(PERIODS.map(period=>[period,periods.includes(period)?[...points]:[]]));
const frozen=value=>{if(value&&typeof value==='object'){Object.values(value).forEach(frozen);Object.freeze(value);}return value;};
const filters={market:'all',origin:'screening',point_status:'confirmed',stage:'all',review:'all',sector:'',sort:'recent'};
const candidate=(id,frequency,point_type,extra={})=>({id,market:'a',code:'SH.600088',frequency,origin:'screening',stage:'confirmed',point:{point_type,status:'confirmed'},evidence_available:true,...extra});
const matched=(rows,selection,extra={})=>filterRows(rows,{...filters,signal_selection:selection,...extra},{},{},'run').map(row=>row.id);

test('four global presets replace all periods regardless of previous shape',()=>{
  for(const selection of [undefined,{},matrix(),matrix([],[]),{'1m':['1buy'],'5m':[],'30m':['3sell']}])
    for(const [preset,points] of [['all',POINTS],['buy',BUY],['sell',SELL],['none',[]]])
      assert.deepEqual(applySignalPreset(frozen(selection),preset),matrix(PERIODS,points),preset);
});
test('each preset retains row OR semantics across all physical periods',()=>{
  const rows=PERIODS.flatMap(period=>POINTS.map(point=>candidate(period+':'+point,period,point)));
  for(const [preset,points] of [['all',POINTS],['buy',BUY],['sell',SELL],['none',[]]])
    assert.deepEqual(matched(rows,applySignalPreset({'1m':['1buy']},preset)),rows.filter(row=>points.includes(row.point.point_type)).map(row=>row.id));
});
test('period checking changes only its own row for every possible six-cell subset',()=>{
  for(const period of PERIODS)for(let mask=0;mask<64;mask++){
    const selection=frozen({'1m':['1buy'],'5m':['2buy','3sell'],'30m':['1sell'],[period]:POINTS.filter((_point,index)=>mask&(1<<index))});
    for(const checked of [true,false]){
      const changed=setSignalPeriod(selection,period,checked);assert.deepEqual(changed[period],checked?POINTS:[]);
      for(const other of PERIODS.filter(value=>value!==period))assert.deepEqual(changed[other],selection[other]);
    }
  }
});
test('partial choices remain exact after edits to sibling periods',()=>{
  const selection=frozen({'1m':['1buy'],'5m':[],'30m':['3sell']}),checked=setSignalPeriod(selection,'5m',true);
  assert.deepEqual(checked,{'1m':['1buy'],'5m':POINTS,'30m':['3sell']});
  assert.deepEqual(setSignalPeriod(checked,'5m',false),selection);
  assert.deepEqual(setSignalPeriod(checked,'1m',false),{'1m':[],'5m':POINTS,'30m':['3sell']});
});
test('preset then row then cell edits keep one canonical state without preset residue',()=>{
  const sell=applySignalPreset(undefined,'sell'),limited=setSignalPeriod(setSignalPeriod(sell,'1m',false),'30m',false);
  const fine={...limited,'30m':['3buy']};assert.deepEqual(fine,{'1m':[],'5m':SELL,'30m':['3buy']});
  assert.deepEqual(applySignalPreset(frozen(fine),'buy'),matrix(PERIODS,BUY));assert.deepEqual(fine,{'1m':[],'5m':SELL,'30m':['3buy']});
});
test('empty rows never become wildcard branches and explicit period checks have bounded scope',()=>{
  const empty=frozen(matrix([],[]));assert.deepEqual(setSignalPeriod(empty,'5m',true),matrix(['5m']));
  assert.deepEqual(setSignalPeriod(empty,'5m',false),empty);assert.deepEqual(applySignalPreset(empty,'none'),empty);
  const rows=[candidate('one','1m','3buy'),candidate('five','5m','3buy'),candidate('thirty','30m','3sell')];
  assert.deepEqual(matched(rows,empty),[]);assert.deepEqual(matched(rows,setSignalPeriod(empty,'5m',true)),['five']);
});
test('absent canonical input defaults to eighteen cells while explicit empty remains empty',()=>{
  assert.deepEqual(applySignalPreset(undefined,'invalid'),matrix());
  assert.deepEqual(setSignalPeriod(undefined,'5m',false),matrix(['1m','30m']));
  for(const empty of [{},[],matrix([],[])]){
    assert.deepEqual(applySignalPreset(empty,'invalid'),matrix([],[]));assert.deepEqual(setSignalPeriod(empty,'invalid',true),matrix([],[]));
  }
});
test('unknown presets and periods normalize without changing valid saved membership',()=>{
  const selection=frozen({'30m':['3sell','1buy','3sell'],'1m':['2buy'],'5m':'3buy',d:['1buy']});
  const expected={'1m':['2buy'],'5m':[],'30m':['1buy','3sell']};
  for(const value of ['custom','unknown','',null,undefined,'1buy','5m'])assert.deepEqual(applySignalPreset(selection,value),expected);
  for(const period of ['all','unknown','',null,undefined,'d'])for(const checked of [true,false])assert.deepEqual(setSignalPeriod(selection,period,checked),expected);
});
test('period updates accept only booleans without interpreting strings or numbers',()=>{
  const selection=frozen({'1m':['1buy'],'5m':['3sell'],'30m':[]});
  for(const checked of [undefined,null,'true','false','',0,1,[],{}])assert.deepEqual(setSignalPeriod(selection,'5m',checked),selection);
});
test('all helper outputs contain independent arrays and never share mutable rows with input',()=>{
  const selection=matrix(['1m','30m'],BUY),before=JSON.stringify(selection);
  for(const changed of [applySignalPreset(selection,'buy'),applySignalPreset(selection,'invalid'),setSignalPeriod(selection,'5m',true),setSignalPeriod(selection,'d',false)]){
    assert.notEqual(changed['1m'],changed['5m']);assert.notEqual(changed['5m'],changed['30m']);
    for(const period of PERIODS)assert.notEqual(changed[period],selection[period]);
    const untouched=[...changed['30m']];changed['1m'].push('3sell');assert.deepEqual(changed['30m'],untouched);
    changed['5m'].push('2sell');assert.equal(JSON.stringify(selection),before);
  }
});
test('repeating a preset or an identical period assignment is canonically idempotent',()=>{
  for(const preset of ['all','buy','sell','none']){const once=applySignalPreset(undefined,preset);assert.deepEqual(applySignalPreset(once,preset),once);}
  for(const period of PERIODS)for(const checked of [true,false]){
    const once=setSignalPeriod({'1m':['1buy'],'30m':['3sell']},period,checked);assert.deepEqual(setSignalPeriod(once,period,checked),once);
  }
});
test('matrix controls retain current evidence history and watch eligibility without widening identities',()=>{
  const rows=[candidate('one','1m','3buy'),candidate('five','5m','3buy'),candidate('sell','5m','3sell'),
    candidate('pending','5m','3buy',{refresh_state:'pending',result_source:'older'}),candidate('limited','5m','3buy',{refresh_state:'limited',result_source:'older',evidence_available:false}),
    candidate('unverified','5m','3buy',{evidence_available:false}),candidate('wrong-source','5m','3buy',{result_source:'older'}),
    {id:'watch',origin:'watchlist',market:'a',code:'SH.600088',frequency:'30m',stage:'monitoring',point:{}}];
  const selection=setSignalPeriod(setSignalPeriod(applySignalPreset(undefined,'buy'),'1m',false),'30m',false);
  assert.deepEqual(matched(rows,selection),['five','pending','limited']);assert.deepEqual(matched(rows,selection,{include_previous:false}),['five']);
  assert.deepEqual(matched(rows,selection,{origin:'all'}),['five','pending','limited','watch']);
  assert.deepEqual(matched(rows,applySignalPreset(selection,'buy')),['one','five','pending','limited']);
  assert.deepEqual(matched(rows,applySignalPreset(selection,'none'),{origin:'all'}),['watch']);
});
test('version one views restore their exact saved matrix without migration',()=>{
  const saved=frozen({version:1,filters:{signal_selection:{'1m':['1buy'],'30m':['3sell']},query:'keep',include_previous:false},watch_group:'观察'});
  const normalized=normalizeViewState(saved);assert.deepEqual(normalized.filters.signal_selection,{'1m':['1buy'],'5m':[],'30m':['3sell']});
  assert.equal(normalized.filters.query,'keep');assert.equal(normalized.filters.include_previous,false);assert.equal(normalized.watch_group,'观察');
});
test('advanced badge counts folded conditions and excludes exposed query and matrix',()=>{
  const defaults=normalizeViewState(null).filters;assert.equal(advancedFilterCount(defaults),0);
  assert.equal(advancedFilterCount({...defaults,query:'SH.600000',signal_selection:matrix([],[])}),0);
  const advanced={market:'a',origin:'all',review:'pending',sector:'tech',center_order:'first',age:'3',gain:'3',sort:'distance',include_previous:false};
  for(const [key,value] of Object.entries(advanced))assert.equal(advancedFilterCount({...defaults,[key]:value}),1,key);
  assert.equal(advancedFilterCount({...defaults,...advanced,query:'visible'}),9);
});
