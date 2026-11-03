'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,normalizeViewState}=require('../screening_workbench.js');
const defaults=normalizeViewState(null).filters;
const item=(id,extra={})=>({id,code:id,market:'a',frequency:'5m',origin:'screening',stage:'confirmed',
  point:{point_type:'2buy',status:'confirmed',available_at:100},
  filter_facts:{times:{structure:100,trade:200,anchor:50},ages:{structure:4,trade:2,anchor:6},absolute_distance_pct:3.41},
  ...extra});

test('absolute distance does not admit a negative move outside the chosen limit',()=>{
  const row=item('far',{gain_pct:-3.41});
  assert.deepEqual(filterRows([row],{...defaults,gain:'1'}),[]);
  assert.equal(filterRows([row],{...defaults,gain:'5'}).length,1);
});

test('date filtering and sorting use the same explicitly chosen clock',()=>{
  const a=item('later-trade');
  const b=item('later-main',{filter_facts:{times:{structure:150,trade:180},ages:{structure:1,trade:3}}});
  assert.deepEqual(filterRows([a,b],{...defaults,sort:'recent',time_basis:'structure'}).map(r=>r.id),['later-main','later-trade']);
  assert.deepEqual(filterRows([a,b],{...defaults,sort:'recent',time_basis:'trade'}).map(r=>r.id),['later-trade','later-main']);
  assert.deepEqual(filterRows([a],{...defaults,age:'2',time_basis:'structure'}),[]);
  assert.equal(filterRows([a],{...defaults,age:'2',time_basis:'trade'}).length,1);
});

test('explicit missing facts do not fall back to an old rounded number',()=>{
  const row=item('missing',{gain_pct:0,confirmation_age_sessions:0,
    filter_facts:{times:{structure:null},ages:{structure:null},absolute_distance_pct:null}});
  assert.deepEqual(filterRows([row],{...defaults,gain:'1'}),[]);
  assert.deepEqual(filterRows([row],{...defaults,age:'1'}),[]);
});

test('history has an independent usable date and explicit sorting',()=>{
  const old=item('old',{origin:'history',historical_only:true,stage:'historical',point:{point_type:'1buy',status:'confirmed',available_at:10},
    filter_facts:{times:{structure:10},ages:{structure:4}}});
  const recent=item('new',{origin:'history',historical_only:true,stage:'historical',point:{point_type:'1buy',status:'confirmed',available_at:20},
    filter_facts:{times:{structure:20},ages:{structure:1}}});
  assert.deepEqual(filterRows([old,recent],{...defaults,origin:'history',age:'4',sort:'recent'}).map(r=>r.id),['new','old']);
});

test('30m risk filters do not redefine main or lower confirmation',()=>{
  const row=item('countertrend',{background_30m:{alignment:'countertrend'}});
  assert.equal(filterRows([row],defaults).length,1);
  assert.equal(filterRows([row],{...defaults,background:'countertrend'}).length,1);
  assert.deepEqual(filterRows([row],{...defaults,background:'aligned'}),[]);
  assert.equal(row.point.status,'confirmed');
});
