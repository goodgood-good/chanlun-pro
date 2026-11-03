'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterDiagnostics,normalizeViewState,emptyResultsMessage}=require('../screening_workbench.js');
const defaults=normalizeViewState(null).filters;
const row=(id,type='3buy',extra={})=>({id,code:id,market:'a',frequency:'5m',origin:'screening',stage:'confirmed',
  point:{point_type:type,status:'confirmed'},filter_facts:{ages:{structure:2}},...extra});

test('empty diagnostics distinguish excluded current candidates from retained previous rows',()=>{
  const filters={...defaults,signal_points:['1buy']},before=JSON.stringify(filters);
  const result=filterDiagnostics([row('now'),row('prior','1buy',{refresh_state:'pending'})],filters);
  assert.equal(result.available,1);assert.equal(result.matching,0);
  assert.equal(result.previousPeriods['5m'],1);
  assert.deepEqual(result.steps,[{key:'signal_points',label:'买卖点类型',before:1,after:0}]);
  assert.equal(JSON.stringify(filters),before);
});
test('retained rows remain recoverable while a new batch has produced no candidates',()=>{
  const result=filterDiagnostics([row('previous','1buy',{refresh_state:'pending',result_source:'old'})],
    {...defaults,signal_period:'1m',include_previous:false});
  assert.equal(result.available,0);assert.equal(result.periods['5m'],0);
  assert.equal(result.previousPeriods['5m'],1);
});
test('combined filters show the condition that removes the final candidate',()=>{
  const result=filterDiagnostics([row('buy'),row('sell','3sell')],{...defaults,signal_points:['3buy'],age:'0'});
  assert.deepEqual(result.steps.map(s=>[s.key,s.before,s.after]),[['signal_points',2,1],['age',1,0]]);
  assert.equal(result.matching,0);
});
test('unscanned 1m view identifies available 5m rows and is not described as still calculating 1m',()=>{
  const result=filterDiagnostics([row('five')],{...defaults,signal_period:'1m'});
  assert.equal(result.available,0);assert.equal(result.periods['5m'],1);
  const message=emptyResultsMessage({status:'running',settings:{frequencies:['5m','30m']}},1,{...defaults,signal_period:'1m'});
  assert.match(message,/未扫描 1m/);
});
test('already published candidates excluded by filters are explained during a running scan',()=>{
  assert.match(emptyResultsMessage({status:'running'},8,defaults),/过滤条件/);
  assert.match(emptyResultsMessage({status:'running'},0,defaults),/正在执行/);
});
