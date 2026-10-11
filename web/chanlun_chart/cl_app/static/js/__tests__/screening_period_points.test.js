'use strict';
// Period selectors are row-level (frequency, point) alternatives, never symbol expansion.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,pageRows,periodPointConditions}=require('../screening_workbench.js');

const SOURCE='current-run';
const POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const PERIODS=['1m','5m','30m'];
const allSelection=()=>Object.fromEntries(PERIODS.map(f=>[f,[...POINTS]]));
const defaults={market:'all',origin:'screening',sector:'',review:'all',stage:'all',
  risk:'all',point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'};
function row(id,frequency,pointType,extra={}) {
  return {id,market:'a',code:'SH.600088',name:'fixture',frequency,origin:'screening',stage:'confirmed',
    point:{point_id:id,point_type:pointType,status:'confirmed'},evidence_available:true,...extra};
}
const ids=(rows,filters={},source=SOURCE,reviewed={},sectorNames={})=>
  filterRows(rows,{...defaults,...filters},reviewed,sectorNames,source).map(r=>r.id);


test('a selected period returns its matching rows without expanding the whole symbol',()=>{
  const rows=[row('one','1m','1buy'),row('five','5m','2buy'),row('thirty','30m','3sell')];
  assert.deepEqual(ids(rows,{signal_selection:{'30m':['3sell']}}),['thirty']);
  assert.deepEqual(ids(rows,{signal_selection:{'30m':['3buy']}}),[]);
});

test('different period choices are row alternatives across different symbols',()=>{
  const rows=[row('a','1m','1buy',{code:'SH.600001'}),row('b','5m','2buy',{code:'SH.600002'}),
    row('c','30m','3buy',{code:'SH.600003'}),row('wrong','30m','1buy',{code:'SH.600004'})];
  assert.deepEqual(ids(rows,{signal_selection:{'1m':['1buy'],'5m':['2buy'],'30m':['3buy']}}),['a','b','c']);
});

test('legacy controls cannot suppress or extend canonical row alternatives',()=>{
  const rows=[row('one','1m','1buy'),row('five','5m','2buy'),row('thirty','30m','3sell')];
  const signal_selection={'1m':['1buy'],'30m':['3sell']};
  for(const period_match of ['any','all'])
    assert.deepEqual(ids(rows,{signal_selection,point:'2buy',frequency:'5m',period_points:{'5m':'2buy'},period_match}),['one','thirty']);
  assert.deepEqual(ids(rows,{point:'1buy',frequency:'1m',period_points:{'1m':'1buy'}}),['one','five','thirty']);
});

test('stale quick point and frequency values cannot intersect active period choices',()=>{
  const rows=[row('one','1m','1buy'),row('thirty','30m','3sell')];
  assert.deepEqual(ids(rows,{point:'1buy',frequency:'1m',signal_selection:{'30m':['3sell']}}),['thirty']);
  assert.deepEqual(ids(rows,{point:'3buy',frequency:'5m',signal_selection:{'1m':['1buy'],'30m':['3sell']}}),['one','thirty']);
});

test('every one of six point types matches only its selected physical period and type',()=>{
  const rows=PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));
  for(const frequency of PERIODS)for(const point of POINTS)
    assert.deepEqual(ids(rows,{signal_selection:{[frequency]:[point]}}),[frequency+':'+point]);
});

test('buy and sell choices are explicit cells within each physical period',()=>{
  const rows=PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));
  for(const frequency of PERIODS)
    assert.deepEqual(ids(rows,{signal_selection:{[frequency]:['3buy','3sell']}}),[frequency+':3buy',frequency+':3sell']);
});

test('empty and absent period rows do not become wildcard branches',()=>{
  const rows=[row('one','1m','3buy'),row('five','5m','3buy'),row('thirty','30m','3buy')];
  assert.deepEqual(ids(rows,{signal_selection:{'5m':['3buy']}}),['five']);
  assert.deepEqual(ids(rows,{signal_selection:{'1m':[],'5m':['3buy'],'30m':[]}}),['five']);
});

test('identical codes in another market neither prove nor suppress another row',()=>{
  const rows=[row('a-wrong','1m','1buy'),row('us-match','30m','3sell',{market:'us'})];
  assert.deepEqual(ids(rows,{signal_selection:{'30m':['3sell']}}),['us-match']);
  assert.deepEqual(ids(rows,{market:'a',signal_selection:{'30m':['3sell']}}),[]);
});

test('separate valid signals in a selected period remain individual candidate rows',()=>{
  const rows=[row('a','5m','3buy'),row('b','5m','3buy'),row('c','5m','3sell')];
  assert.deepEqual(ids(rows,{signal_selection:{'5m':['3buy']}}),['a','b']);
});

test('labelled pending and limited history uses the same cells while current missing evidence is excluded',()=>{
  const rows=[row('good','30m','3sell'),row('pending','30m','3sell',{refresh_state:'pending',result_source:'old'}),
    row('limited','30m','3sell',{refresh_state:'limited',result_source:'old',evidence_available:false,evidence_chart_available:false}),
    row('missing','30m','3sell',{evidence_available:false})];
  const before=JSON.stringify(rows),signal_selection={'30m':['3sell']};
  assert.deepEqual(ids(rows,{signal_selection}),['good','pending','limited']);
  assert.deepEqual(ids(rows,{signal_selection,include_previous:true}),['good','pending','limited']);
  assert.deepEqual(ids(rows,{signal_selection,include_previous:false}),['good']);
  assert.equal(JSON.stringify(rows),before);
});

test('invalidated closed and left rows never qualify even through history or all status',()=>{
  for(const stage of ['invalidated','closed','left']) {
    const current=row('current','30m','3sell',{stage});
    const previous={...current,id:'old',refresh_state:'pending',result_source:'old'};
    const invalidPoint=row('point','30m','3sell');invalidPoint.point.status=stage;
    assert.deepEqual(ids([current,previous,invalidPoint],{point_status:'all',signal_selection:allSelection()}),[],stage);
  }
});

test('nonconfirmed point status cannot match even when its containing row says confirmed',()=>{
  for(const status of ['invalidated','closed','approaching','formed','observed','unknown']) {
    const candidate=row(status,'30m','3sell');candidate.point.status=status;
    assert.deepEqual(ids([candidate],{signal_selection:{'30m':['3sell']}}),[],status);
  }
});

test('formal confirmed entry-pending point keeps its existing meaning',()=>{
  const candidate=row('entry','30m','3buy',{stage:'entry_pending',nested_confirmation:{state:'waiting',frequency:'1m'}});
  assert.deepEqual(ids([candidate],{signal_selection:{'30m':['3buy']}}),['entry']);
  assert.deepEqual(ids([candidate],{signal_selection:{'30m':['3buy']},point_status:'forming'}),[]);
  assert.equal(candidate.stage,'entry_pending');
  assert.equal(candidate.nested_confirmation.state,'waiting');
});

test('compatible compact records use the existing confirmed stage fallback',()=>{
  const candidate=row('compact','30m','3sell');delete candidate.point.status;delete candidate.evidence_available;
  assert.deepEqual(ids([candidate],{signal_selection:{'30m':['3sell']}}),['compact']);
});

test('explicit result source must match and cannot be assumed without the current source',()=>{
  const candidate=row('current','30m','3sell',{result_source:SOURCE});
  const filters={signal_selection:{'30m':['3sell']}};
  assert.deepEqual(ids([candidate],filters,SOURCE),['current']);
  assert.deepEqual(ids([candidate],filters,'other-run'),[]);
  assert.deepEqual(ids([candidate],filters,null),[]);
  assert.deepEqual(ids([row('implicit','30m','3sell')],filters,null),['implicit']);
});

test('watchlist context is independent of signal cells and controlled by origin',()=>{
  const watch={id:'watch',market:'a',code:'SH.600088',name:'watched',frequency:'30m',origin:'watchlist',stage:'monitoring',point:{}};
  const proof=row('proof','30m','3sell');
  for(const signal_selection of [{},{'1m':['1buy']},{'30m':['3sell']}]) {
    assert.deepEqual(ids([watch,proof],{origin:'watchlist',signal_selection}),['watch']);
    assert.deepEqual(ids([watch,proof],{origin:'all',signal_selection}),signal_selection['30m']?['watch','proof']:['watch']);
  }
  assert.deepEqual(ids([watch,proof],{origin:'screening',signal_selection:{}}),[]);
  assert.deepEqual(ids([watch],{origin:'all',signal_selection:{},market:'us'}),[]);
  assert.deepEqual(ids([watch],{origin:'all',signal_selection:{},query:'watched'}),['watch']);
  assert.deepEqual(ids([watch],{origin:'all',signal_selection:{},query:'missing'}),[]);
  assert.deepEqual(watch.point,{});assert.equal(watch.origin,'watchlist');
});

test('nested confirmation and higher context do not manufacture independent period matches',()=>{
  const main=row('five','5m','3buy',{nested_confirmation:{frequency:'1m',state:'confirmed',point:{point_type:'3buy',status:'confirmed'}},
    higher_context:{frequency:'30m',category:'up_segment',point_type:'3sell'}});
  assert.deepEqual(ids([main],{signal_selection:{'1m':['3buy'],'30m':['3sell']}}),[]);
});

test('independent matching rows do not require equal completed bar cutoffs',()=>{
  const rows=[row('one','1m','1buy',{source_closed_at:180}),row('thirty','30m','3buy',{source_closed_at:60})];
  assert.deepEqual(ids(rows,{signal_selection:{'1m':['1buy'],'30m':['3buy']}}),['one','thirty']);
});

test('row matching occurs before pagination and a last-page match reaches the first result page',()=>{
  const rows=Array.from({length:121},(_,i)=>row('row'+i,i===120?'30m':'1m',i===120?'3sell':'1buy',{code:'SH.'+(600000+i)}));
  const matched=filterRows(rows,{...defaults,signal_selection:{'30m':['3sell']}},{},{},SOURCE);
  assert.deepEqual(pageRows(matched,1).rows.map(r=>r.id),['row120']);
  assert.equal(pageRows(matched,1).total,1);
});

test('omitted selection means eighteen cells but explicit empty selection means no signals',()=>{
  const rows=PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));
  assert.deepEqual(ids(rows),rows.map(r=>r.id));
  assert.deepEqual(ids(rows,{signal_selection:allSelection()}),rows.map(r=>r.id));
  assert.deepEqual(periodPointConditions({}),PERIODS.flatMap(frequency=>POINTS.map(point=>({frequency,point}))));
  for(const signal_selection of [{},[],{'1m':[],'5m':[],'30m':[]}]) {
    assert.deepEqual(ids(rows,{signal_selection}),[]);
    assert.deepEqual(ids(rows,{signal_selection,point:'3buy',frequency:'5m',period_points:{'5m':'3buy'}}),[]);
    assert.deepEqual(periodPointConditions({signal_selection}),[]);
  }
});

test('advanced market sector review query and age filters still intersect the matching rows',()=>{
  const candidate=row('match','30m','3sell',{sectors:['media'],confirmation_age_sessions:3,gain_pct:4});
  const filters={signal_selection:{'30m':['3sell']}};
  assert.deepEqual(ids([candidate],filters),['match']);
  assert.deepEqual(ids([candidate],{...filters,market:'us'}),[]);
  assert.deepEqual(ids([candidate],{...filters,sector:'other'}),[]);
  assert.deepEqual(ids([candidate],{...filters,query:'missing'}),[]);
  assert.deepEqual(ids([candidate],{...filters,review:'reviewed'}),[]);
  assert.deepEqual(ids([candidate],{...filters,review:'reviewed'},SOURCE,{match:{}}),['match']);
  assert.deepEqual(ids([candidate],{...filters,age:'1'}),[]);
  assert.deepEqual(ids([candidate],{...filters,gain:'3'}),[]);
  assert.deepEqual(ids([candidate],{...filters,query:'media'},SOURCE,{}, {media:'media sector'}),['match']);
});

test('progressive updates retain limited context and remove genuinely withdrawn rows without mutation',()=>{
  const rows=[row('one','1m','1buy'),row('five','5m','2buy'),row('thirty','30m','3sell')];
  const filters={signal_selection:{'1m':['1buy'],'5m':['2buy'],'30m':['3sell']}};
  const before=JSON.stringify(rows),limited=rows.map(r=>r.id==='five'?{...r,refresh_state:'limited',result_source:'old'}:r);
  assert.deepEqual(ids(rows,filters),['one','five','thirty']);
  assert.deepEqual(ids(limited,filters),['one','five','thirty']);
  assert.deepEqual(ids(limited,{...filters,include_previous:false}),['one','thirty']);
  assert.deepEqual(ids(rows.filter(r=>r.id!=='one'),filters),['five','thirty']);
  assert.equal(JSON.stringify(rows),before);
});

test('normalization flattens unique real cells and rejects unknown types periods and non-array values',()=>{
  const signal_selection={'1m':['1buy','1buy','unknown','either'],'5m':['3sell'],'30m':'3buy','d':['3sell']};
  const before=JSON.stringify(signal_selection);
  assert.deepEqual(periodPointConditions({signal_selection}),[{frequency:'1m',point:'1buy'},{frequency:'5m',point:'3sell'}]);
  assert.equal(JSON.stringify(signal_selection),before);
  assert.deepEqual(ids([row('daily','d','3buy'),row('unknown','5m','either')]),[]);
  assert.deepEqual(ids([row('third','5m','3buy')],{signal_selection:{'5m':['either']}}),[]);
});


test('all selected and a matching subset apply identical evidence and history eligibility',()=>{
  const rows=[row('good','5m','3buy'),row('pending','5m','3buy',{refresh_state:'pending',result_source:'old'}),
    row('limited','5m','3buy',{refresh_state:'limited',result_source:'old'}),
    row('unavailable','5m','3buy',{evidence_available:false}),row('wrong-source','5m','3buy',{result_source:'old'}),
    row('closed','5m','3buy',{stage:'closed'})];
  for(const include_previous of [true,false]) {
    const expected=include_previous?['good','pending','limited']:['good'];
    assert.deepEqual(ids(rows,{include_previous}),expected);
    assert.deepEqual(ids(rows,{include_previous,signal_selection:allSelection()}),expected);
    assert.deepEqual(ids(rows,{include_previous,signal_selection:{'5m':['3buy']}}),expected);
  }
});

test('confirmed forming and all advanced statuses apply consistently to full and partial cell selection',()=>{
  const confirmed=row('confirmed','5m','3buy');
  const forming=row('forming','5m','3buy',{stage:'approaching'});forming.point.status='approaching';
  for(const signal_selection of [allSelection(),{'5m':['3buy']}]) {
    assert.deepEqual(ids([confirmed,forming],{signal_selection,point_status:'confirmed'}),['confirmed']);
    assert.deepEqual(ids([confirmed,forming],{signal_selection,point_status:'forming'}),['forming']);
    assert.deepEqual(ids([confirmed,forming],{signal_selection,point_status:'all'}),['confirmed','forming']);
    assert.deepEqual(ids([confirmed,forming],{signal_selection,point_status:'all',stage:'forming'}),['forming']);
  }
});
