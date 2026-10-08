'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,chartUrl,evidenceUrl,quoteText,cutoffSummary,resultVersionMessage,emptyResultsMessage,pageRows}=require('../screening_workbench.js');
const filters={market:'all',origin:'screening',frequency:'all',point:'all',sector:'',review:'all',stage:'all'};
const rows=[
  {id:'a',market:'a',code:'SH.600088',name:'中视传媒',frequency:'5m',point:{point_type:'3buy'},stage:'confirmed',origin:'screening',sectors:['media']},
  {id:'b',market:'us',code:'AAPL',name:'Apple',frequency:'5m',point:{},stage:'monitoring',origin:'watchlist'},
  {id:'c',market:'a',code:'SZ.301004',name:'另一候选',frequency:'30m',point:{point_type:'2buy'},stage:'confirmed',origin:'screening',sectors:['other']},
];
test('candidate pages are bounded and retain global filters beyond the first page',()=>{
  const all=Array.from({length:121},(_,i)=>({...rows[0],id:String(i),name:i===120?'target':'ordinary'}));
  assert.equal(pageRows(all,1).rows.length,50);
  assert.equal(pageRows(all,2).rows[0].id,'50');
  assert.equal(pageRows(all,99).page,3);
  assert.equal(pageRows(all,3).rows.length,21);
  assert.equal(pageRows([],5).total,0);
  const matches=filterRows(all,{...filters,query:'target'});
  assert.deepEqual(pageRows(matches,1).rows.map(row=>row.id),['120']);
});
test('cross-market cutoff labels retain separate clocks for each period',()=>{
  const summary=cutoffSummary({cutoffs:{'5m':1000},market_cutoffs:{a:{'5m':2000},us:{'1m':3000,'30m':null}}});
  assert.equal(summary.length,3);
  assert.match(summary[0],/^A 股 5m 截止 /);
  assert.match(summary[1],/^美股 1m 截止 /);
  assert.equal(summary[2],'美股 30m：未取得有效截止时间');
  assert.equal(cutoffSummary({cutoffs:{'5m':1000}}).length,1);
});
test('market cutoff labels distinguish current, stale and unavailable periods',()=>{
  const summary=cutoffSummary({status:'completed',market_cutoffs:{a:{'5m':1000},us:{'5m':2000},fx:{'5m':null}},
    market_freshness:{a:{'5m':{state:'cutoff_current'}},us:{'5m':{state:'stale'}},fx:{'5m':{state:'unknown'}}}});
  assert.match(summary[0],/A 股.*截止时间已核对/);
  assert.match(summary[1],/美股.*已有更新/);
  assert.equal(summary[2],'外汇 5m：未取得有效截止时间');
  assert.equal(cutoffSummary({status:'running',market_cutoffs:{fx:{'5m':null}}})[0],'外汇 5m：等待行情截止记录');
});
test('frozen native results show their historical proof status after a source update',()=>{
  const status={run_id:'a'.repeat(32),status:'completed',source_current:false,source_revision:'0'.repeat(64),frozen_native_review:true};
  assert.match(resultVersionMessage(status),/原截止时刻.*证据核验.*历史结果/);
  assert.match(emptyResultsMessage(status,5),/过滤条件/);
  assert.match(emptyResultsMessage(status,0),/没有通过核验/);
  assert.match(resultVersionMessage({...status,frozen_native_review:false}),/不兼容/);
  assert.equal(resultVersionMessage({...status,source_current:true}),'');
});
test('empty queue explains loading, idle, incompatible versions and view filters separately',()=>{
  assert.match(emptyResultsMessage({results_loading:true}),/正在读取并校验/);
  assert.match(emptyResultsMessage({status:'idle'}),/尚未运行选股/);
  assert.match(emptyResultsMessage({status:'running'}),/正在执行/);
  assert.match(emptyResultsMessage({run_id:'a',source_current:false,source_revision:'old'}),/不兼容/);
  assert.match(emptyResultsMessage({status:'completed',source_current:true},3),/过滤条件/);
});
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
test('operational MACD risk filters only the view and keeps formal point rows',()=>{
  const candidates=[
    {...rows[0],id:'avoid',operational_macd_risk:{risk_filter_state:'basic_avoid_on_selected_period'}},
    {...rows[0],id:'clear',operational_macd_risk:{risk_filter_state:'basic_zero_axis_risk_not_triggered'}},
    {...rows[0],id:'pending',operational_macd_risk:{risk_filter_state:'pending_frozen_evidence'}},
  ];
  assert.deepEqual(filterRows(candidates,{...filters,risk:'all'}).map(r=>r.id),['avoid','clear','pending']);
  assert.deepEqual(filterRows(candidates,{...filters,risk:'not_triggered'}).map(r=>r.id),['clear']);
  assert.deepEqual(filterRows(candidates,{...filters,risk:'avoid'}).map(r=>r.id),['avoid']);
  assert.deepEqual(filterRows(candidates,{...filters,risk:'pending'}).map(r=>r.id),['pending']);
  assert.equal(candidates[0].stage,'confirmed');
});
test('30m context is an optional page filter and preserves unknown evidence',()=>{
  const candidates=[
    {...rows[0],id:'trend',higher_context:{category:'up_trend_forming'}},
    {...rows[0],id:'early',higher_context:{category:'up_segment'}},
    {...rows[0],id:'opposing',higher_context:{category:'down_segment'}},
    {...rows[0],id:'unknown',higher_context:{category:'unknown',reason:'DATA_GAPS'}},
  ];
  assert.deepEqual(filterRows(candidates,{...filters,higher:'all'}).map(r=>r.id),
    ['trend','early','opposing','unknown']);
  assert.deepEqual(filterRows(candidates,{...filters,higher:'up'}).map(r=>r.id),['trend','early']);
  assert.deepEqual(filterRows(candidates,{...filters,higher:'up_trend_forming'}).map(r=>r.id),['trend']);
  assert.deepEqual(filterRows(candidates,{...filters,higher:'unknown'}).map(r=>r.id),['unknown']);
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
  assert.match(quoteText({price:100,rate:''}),/暂缺/);
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
