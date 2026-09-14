'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,chartUrl,evidenceUrl,quoteText}=require('../screening_workbench.js');
const filters={market:'all',origin:'screening',frequency:'all',point:'all',sector:'',review:'all',stage:'all'};
const rows=[
  {id:'a',market:'a',code:'SH.600088',name:'中视传媒',frequency:'5m',point:{point_type:'3buy'},stage:'confirmed',origin:'screening',sectors:['media']},
  {id:'b',market:'us',code:'AAPL',name:'Apple',frequency:'5m',point:{},stage:'monitoring',origin:'watchlist'},
  {id:'c',market:'a',code:'SZ.301004',name:'另一候选',frequency:'30m',point:{point_type:'2buy'},stage:'confirmed',origin:'screening',sectors:['other']},
];
test('all-market watchlist stays distinct from confirmed screening signals',()=>{
  assert.deepEqual(filterRows(rows,filters).map(r=>r.id),['a','c']);
  assert.deepEqual(filterRows(rows,{...filters,origin:'watchlist',market:'us'}).map(r=>r.id),['b']);
  assert.equal(filterRows(rows,{...filters,origin:'all',point:'3buy'}).length,1);
});
test('sector, six point types, periods and review filters compose',()=>{
  assert.deepEqual(filterRows(rows,{...filters,query:'传媒'}, {}, {media:'传媒行业'}).map(r=>r.id),['a']);
  assert.equal(filterRows(rows,{...filters,sector:'media',point:'3sell'}).length,0);
  assert.deepEqual(filterRows(rows,{...filters,stage:'confirmed',frequency:'30m'}).map(r=>r.id),['c']);
  assert.deepEqual(filterRows(rows,{...filters,review:'reviewed'},{a:{}}).map(r=>r.id),['a']);
  assert.deepEqual(filterRows(rows,{...filters,review:'pending',origin:'all'},{a:{}}).map(r=>r.id),['c']);
});

test('forming results filter independently from confirmed candidates and watchlists',()=>{
  const waiting={...rows[0],id:'waiting',stage:'approaching',point:{point_type:'3buy',confirmed_at:null}};
  const mixed=[...rows,waiting];
  assert.deepEqual(filterRows(mixed,{...filters,stage:'forming',frequency:'5m',point:'3buy'}).map(r=>r.id),['waiting']);
  assert.deepEqual(filterRows(mixed,{...filters,stage:'confirmed'}).map(r=>r.id),['a','c']);
  assert.deepEqual(filterRows(mixed,{...filters,stage:'forming',point:'1buy'}),[]);
});
test('chart URLs explicitly pin exact market, symbol and physical layout periods',()=>{
  const single=new URL(chartUrl(rows[0],'1m'),'http://local');
  assert.equal(single.searchParams.get('code'),'SH.600088');
  assert.equal(single.searchParams.get('layout'),'single');
  assert.equal(single.searchParams.get('intervals'),'1');
  const four=new URL(chartUrl(rows[1],'d','four'),'http://local');
  assert.equal(four.searchParams.get('market'),'us');
  assert.equal(four.searchParams.get('intervals'),'1D,30,5,1');
  assert.equal(four.searchParams.get('chart_sidebar'),'collapsed');
});
test('missing quote change is not rendered as zero, while real zero is retained',()=>{
  assert.match(quoteText({price:100,rate:0}),/0\.00%/);
  assert.match(quoteText({price:100,rate:null}),/暂缺/);
  assert.match(quoteText({price:100,rate:1.25}),/\+1\.25%/);
});

test('evidence URL pins the run and selected point as well as its physical bars',()=>{
  const url=new URL(evidenceUrl({...rows[0],point:{point_id:'proof+id'}},'e'.repeat(32)),'http://local');
  assert.equal(url.pathname,'/screening/evidence');
  assert.equal(url.searchParams.get('source'),'e'.repeat(32));
  assert.equal(url.searchParams.get('point'),'proof+id');
  assert.equal(url.searchParams.get('frequency'),'5m');
  assert.equal(url.searchParams.get('code'),'SH.600088');
});

test('nested layouts put the 1m confirmation next to the 5m main chart',()=>{
  const row={...rows[0],nested_confirmation:{frequency:'1m'},point:{point_id:'main-five-minute'}};
  const paired=new URL(chartUrl(row,'5m','horizontal-2'),'http://local');
  assert.equal(paired.searchParams.get('intervals'),'5,1');
  const evidence=new URL(evidenceUrl(row,'run','1m'),'http://local');
  assert.equal(evidence.searchParams.get('frequency'),'1m');
  assert.equal(evidence.searchParams.get('point'),'main-five-minute');
});
