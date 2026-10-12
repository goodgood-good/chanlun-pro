'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,normalizeViewState,higherContextText,higherContextDetail}=require('../screening_workbench.js');
const schema='native-period-link-v1';
const priorities={aligned:0,turning:1,independent:2,mixed:3,countertrend:4,unverified:5};
const row=(id,category,when=10)=>({id,code:'SH.600000',market:'a',name:id,frequency:'5m',origin:'screening',
  stage:'approaching',evidence_available:true,source_closed_at:100,selection_available_at:when,
  point:{point_id:id,point_type:'3buy',status:'approaching',available_at:when},
  higher_context:{schema,category,priority:priorities[category],label:category,periods:[]}});

test('linkage is the default order while every unconfirmed category stays visible',()=>{
  const filters=normalizeViewState(null).filters;
  assert.equal(filters.sort,'linkage');
  const input=[row('risk','countertrend',30),row('unknown','unverified',40),row('turn','turning',20),row('support','aligned',10)];
  assert.deepEqual(filterRows(input,filters).map(r=>r.id),['support','turn','risk','unknown']);
  assert.equal(input[0].id,'risk');
  assert.deepEqual(filterRows(input,{...filters,point_status:'forming'}).map(r=>r.id),['support','turn','risk','unknown']);
});

test('one operating period remains selected when filtering linkage verdicts',()=>{
  const filters={...normalizeViewState(null).filters,higher:'aligned'};
  const input=[row('support','aligned'),row('pending','unverified'),{...row('other','aligned'),frequency:'1m'}];
  assert.deepEqual(filterRows(input,filters).map(r=>r.id),['support']);
  assert.deepEqual(filterRows(input,{...filters,higher:'unverified'}).map(r=>r.id),['pending']);
});

test('old views retain period and unconfirmed choice when adopting linkage order',()=>{
  const state=normalizeViewState({version:3,filters:{signal_period:'30m',signal_points:['1buy'],point_status:'forming',sort:'recent'}});
  assert.equal(state.filters.sort,'linkage');
  assert.equal(state.filters.point_status,'forming');
  assert.equal(state.filters.signal_period,'30m');
  assert.deepEqual(state.filters.signal_points,['1buy']);
  const modern=normalizeViewState({version:4,filters:{...state.filters,sort:'recent',higher:'turning'}});
  assert.equal(modern.filters.sort,'recent');assert.equal(modern.filters.higher,'turning');
});

test('historical retained rows never outrank current rows because of linkage',()=>{
  const filters=normalizeViewState(null).filters;
  const current=row('current','unverified');
  const previous={...row('previous','aligned'),refresh_state:'pending',result_source:'old'};
  assert.deepEqual(filterRows([previous,current],filters).map(r=>r.id),['current','previous']);
});

test('period text states background and timing without inventing a larger confirmed reversal',()=>{
  const context={schema,label:'转折候选',periods:[
    {frequency:'1m',role:'timing',label:'一买预警',source_closed_at:100},
    {frequency:'5m',role:'main',label:'二买预警',source_closed_at:100},
    {frequency:'30m',role:'background',label:'中枢下移',source_closed_at:90}],
    notes:['较大周期转折尚待结构确认。']};
  assert.match(higherContextText(context),/30m 中枢下移/);
  assert.match(higherContextDetail(context),/1m 细化定位/);
  assert.match(higherContextDetail(context),/5m 操作周期/);
});
