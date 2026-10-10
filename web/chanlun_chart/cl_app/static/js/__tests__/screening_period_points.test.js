'use strict';
// Period selectors are row-level (frequency, point) alternatives, never symbol expansion.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,pageRows,periodPointConditions}=require('../screening_workbench.js');

const SOURCE='current-run';
const POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const PERIODS=['1m','5m','30m'];
const defaults={market:'all',origin:'screening',frequency:'all',point:'all',sector:'',review:'all',stage:'all',
  risk:'all',point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'};
function row(id,frequency,pointType,extra={}) {
  return {id,market:'a',code:'SH.600088',name:'fixture',frequency,origin:'screening',stage:'confirmed',
    point:{point_id:id,point_type:pointType,status:'confirmed'},evidence_available:true,...extra};
}
const ids=(rows,filters={},source=SOURCE,reviewed={},sectorNames={})=>
  filterRows(rows,{...defaults,...filters},reviewed,sectorNames,source).map(r=>r.id);


test('a selected period returns its matching rows without expanding the whole symbol',()=>{
  const rows=[row('one','1m','1buy'),row('five','5m','2buy'),row('thirty','30m','3sell')];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),['thirty']);
  assert.deepEqual(ids(rows,{period_points:{'30m':'3buy'}}),[]);
});

test('different period choices are row alternatives across different symbols',()=>{
  const rows=[row('a','1m','1buy',{code:'SH.600001'}),row('b','5m','2buy',{code:'SH.600002'}),
    row('c','30m','3buy',{code:'SH.600003'}),row('wrong','30m','1buy',{code:'SH.600004'})];
  assert.deepEqual(ids(rows,{period_points:{'1m':'1buy','5m':'2buy','30m':'3buy'}}),['a','b','c']);
});

test('legacy period_match all cannot restore same-symbol AND semantics',()=>{
  const rows=[row('one','1m','1buy',{code:'SH.600001'}),row('five','5m','2buy',{code:'SH.600002'})];
  const period_points={'1m':'1buy','5m':'2buy','30m':'3buy'};
  assert.deepEqual(ids(rows,{period_points}),['one','five']);
  assert.deepEqual(ids(rows,{period_points,period_match:'any'}),['one','five']);
  assert.deepEqual(ids(rows,{period_points,period_match:'all'}),['one','five']);
});

test('stale quick point and frequency values cannot intersect active period choices',()=>{
  const rows=[row('one','1m','1buy'),row('thirty','30m','3sell')];
  assert.deepEqual(ids(rows,{point:'1buy',frequency:'1m',period_points:{'30m':'3sell'}}),['thirty']);
  assert.deepEqual(ids(rows,{point:'3buy',frequency:'5m',period_points:{'1m':'1buy','30m':'3sell'}}),['one','thirty']);
});

test('every one of six point types matches only its selected physical period and type',()=>{
  const rows=PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));
  for(const frequency of PERIODS)for(const point of POINTS)
    assert.deepEqual(ids(rows,{period_points:{[frequency]:point}}),[frequency+':'+point]);
});

test('either remains specifically third buy or third sell in that period',()=>{
  const rows=PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));
  for(const frequency of PERIODS)
    assert.deepEqual(ids(rows,{period_points:{[frequency]:'either'}}),[frequency+':3buy',frequency+':3sell']);
});

test('unselected all periods do not become wildcard branches when another condition is active',()=>{
  const rows=[row('one','1m','3buy'),row('five','5m','3buy'),row('thirty','30m','3buy')];
  assert.deepEqual(ids(rows,{period_points:{'1m':'all','5m':'3buy','30m':'all'}}),['five']);
});

test('identical codes in another market neither prove nor suppress another row',()=>{
  const rows=[row('a-wrong','1m','1buy'),row('us-match','30m','3sell',{market:'us'})];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),['us-match']);
  assert.deepEqual(ids(rows,{market:'a',period_points:{'30m':'3sell'}}),[]);
});

test('separate valid signals in a selected period remain individual candidate rows',()=>{
  const rows=[row('a','5m','3buy'),row('b','5m','3buy'),row('c','5m','3sell')];
  assert.deepEqual(ids(rows,{period_points:{'5m':'3buy'}}),['a','b']);
});

test('pending limited and explicitly unavailable evidence cannot be displayed as matches',()=>{
  const rows=[row('good','30m','3sell'),row('pending','30m','3sell',{refresh_state:'pending'}),
    row('limited','30m','3sell',{refresh_state:'limited'}),row('missing','30m','3sell',{evidence_available:false})];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),['good']);
});

test('invalid or unfinished row stages cannot match even with an old confirmed point',()=>{
  for(const stage of ['invalidated','closed','left','approaching','formed','observed','monitoring','active'])
    assert.deepEqual(ids([row(stage,'30m','3sell',{stage})],{period_points:{'30m':'3sell'}}),[],stage);
});

test('nonconfirmed point status cannot match even when its containing row says confirmed',()=>{
  for(const status of ['invalidated','closed','approaching','formed','observed','unknown']) {
    const candidate=row(status,'30m','3sell');candidate.point.status=status;
    assert.deepEqual(ids([candidate],{period_points:{'30m':'3sell'}}),[],status);
  }
});

test('formal confirmed entry-pending point keeps its existing meaning',()=>{
  const candidate=row('entry','30m','3buy',{stage:'entry_pending',nested_confirmation:{state:'waiting',frequency:'1m'}});
  assert.deepEqual(ids([candidate],{period_points:{'30m':'3buy'}}),['entry']);
  assert.equal(candidate.stage,'entry_pending');
  assert.equal(candidate.nested_confirmation.state,'waiting');
});

test('compatible compact records use the existing confirmed stage fallback',()=>{
  const candidate=row('compact','30m','3sell');delete candidate.point.status;delete candidate.evidence_available;
  assert.deepEqual(ids([candidate],{period_points:{'30m':'3sell'}}),['compact']);
});

test('explicit result source must match and cannot be assumed without the current source',()=>{
  const candidate=row('current','30m','3sell',{result_source:SOURCE});
  const filters={period_points:{'30m':'3sell'}};
  assert.deepEqual(ids([candidate],filters,SOURCE),['current']);
  assert.deepEqual(ids([candidate],filters,'other-run'),[]);
  assert.deepEqual(ids([candidate],filters,null),[]);
  assert.deepEqual(ids([row('implicit','30m','3sell')],filters,null),['implicit']);
});

test('watchlist placeholders cannot qualify through signals on the same symbol',()=>{
  const watch={id:'watch',market:'a',code:'SH.600088',name:'watched',frequency:'30m',origin:'watchlist',stage:'monitoring',point:{}};
  const proof=row('proof','30m','3sell');
  assert.deepEqual(ids([watch,proof],{origin:'all',period_points:{'30m':'3sell'}}),['proof']);
  assert.deepEqual(ids([watch,proof],{origin:'watchlist',period_points:{'30m':'3sell'}}),[]);
  const fake=row('fake-watch','30m','3sell',{origin:'watchlist'});
  assert.deepEqual(ids([fake],{origin:'all',period_points:{'30m':'3sell'}}),[]);
});

test('nested confirmation and higher context do not manufacture independent period matches',()=>{
  const main=row('five','5m','3buy',{nested_confirmation:{frequency:'1m',state:'confirmed',point:{point_type:'3buy',status:'confirmed'}},
    higher_context:{frequency:'30m',category:'up_segment',point_type:'3sell'}});
  assert.deepEqual(ids([main],{period_points:{'1m':'3buy','30m':'3sell'}}),[]);
});

test('independent matching rows do not require equal completed bar cutoffs',()=>{
  const rows=[row('one','1m','1buy',{source_closed_at:180}),row('thirty','30m','3buy',{source_closed_at:60})];
  assert.deepEqual(ids(rows,{period_points:{'1m':'1buy','30m':'3buy'}}),['one','thirty']);
});

test('row matching occurs before pagination and a last-page match reaches the first result page',()=>{
  const rows=Array.from({length:121},(_,i)=>row('row'+i,i===120?'30m':'1m',i===120?'3sell':'1buy',{code:'SH.'+(600000+i)}));
  const matched=filterRows(rows,{...defaults,period_points:{'30m':'3sell'}},{},{},SOURCE);
  assert.deepEqual(pageRows(matched,1).rows.map(r=>r.id),['row120']);
  assert.equal(pageRows(matched,1).total,1);
});

test('no valid period conditions preserve the old quick filters and retained old results',()=>{
  const rows=[row('one','1m','1buy'),row('old','1m','1buy',{refresh_state:'pending'}),row('thirty','30m','3sell')];
  const filters={point:'1buy',frequency:'1m'};
  assert.deepEqual(ids(rows,filters),['one','old']);
  assert.deepEqual(ids(rows,{...filters,period_points:{'1m':'all','5m':'all','30m':'all'}}),['one','old']);
  assert.deepEqual(ids(rows,{...filters,period_points:{},period_match:'all'}),['one','old']);
  assert.deepEqual(ids(rows,{...filters,period_points:{'1m':'bad','d':'3buy'}}),['one','old']);
});

test('advanced market sector review query and age filters still intersect the matching rows',()=>{
  const candidate=row('match','30m','3sell',{sectors:['media'],confirmation_age_sessions:3,gain_pct:4});
  const filters={period_points:{'30m':'3sell'}};
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

test('a row becoming limited or nonmatching disappears without affecting unrelated matches',()=>{
  const rows=[row('one','1m','1buy'),row('five','5m','2buy'),row('thirty','30m','3sell')];
  const filters={period_points:{'1m':'1buy','5m':'2buy','30m':'3sell'}};
  const before=JSON.stringify(rows);
  assert.deepEqual(ids(rows,filters),['one','five','thirty']);
  assert.deepEqual(ids(rows.map(r=>r.id==='five'?{...r,refresh_state:'limited'}:r),filters),['one','thirty']);
  assert.deepEqual(ids(rows.filter(r=>r.id!=='one'),filters),['five','thirty']);
  assert.equal(JSON.stringify(rows),before);
});

test('condition normalization includes six types and either but excludes inactive or unknown selectors',()=>{
  assert.deepEqual(periodPointConditions({period_points:{'1m':'1buy','5m':'either','30m':'all','d':'3sell'}}),
    [{frequency:'1m',point:'1buy'},{frequency:'5m',point:'either'}]);
  assert.deepEqual(periodPointConditions({period_points:{'1m':'unknown'}}),[]);
});
