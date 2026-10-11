'use strict';
// Only the selected physical period participates; chosen types are alternatives within it.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,pageRows,periodPointConditions}=require('../screening_workbench.js');
const SOURCE='current-run',PERIODS=['1m','5m','30m'],POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const defaults={market:'all',origin:'screening',sector:'',review:'all',stage:'all',risk:'all',point_status:'confirmed',
  lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'};
const pick=(signal_period='5m',signal_points=POINTS)=>({signal_period,signal_points});
const row=(id,frequency,pointType,extra={})=>({id,market:'a',code:'SH.600088',name:'fixture',frequency,origin:'screening',stage:'confirmed',
  point:{point_id:id,point_type:pointType,status:'confirmed'},evidence_available:true,...extra});
const ids=(rows,filters={},source=SOURCE,reviewed={},names={})=>filterRows(rows,{...defaults,...filters},reviewed,names,source).map(r=>r.id);
const grid=()=>PERIODS.flatMap(f=>POINTS.map(p=>row(f+':'+p,f,p)));

test('missing new fields default to six types only on 5m',()=>{
  assert.deepEqual(ids(grid()),POINTS.map(p=>'5m:'+p));
  assert.deepEqual(periodPointConditions({}),POINTS.map(point=>({frequency:'5m',point})));
  assert.deepEqual(ids(grid(),pick()),ids(grid()));
});

test('each selected period and type matches only its own physical row',()=>{
  for(const period of PERIODS)for(const point of POINTS)
    assert.deepEqual(ids(grid(),pick(period,[point])),[period+':'+point]);
});

test('several types use OR inside one period without adding sibling periods or symbol rows',()=>{
  const rows=[row('one','1m','1buy'),row('five-buy','5m','1buy'),row('five-sell','5m','3sell'),
    row('five-other','5m','2buy'),row('thirty','30m','3sell')];
  assert.deepEqual(ids(rows,pick('5m',['1buy','3sell'])),['five-buy','five-sell']);
  assert.deepEqual(ids(rows,pick('30m',['1buy','3sell'])),['thirty']);
});

test('empty types remain empty rather than defaulting to all or taking legacy conditions',()=>{
  for(const period of PERIODS){
    assert.deepEqual(ids(grid(),pick(period,[])),[]);
    assert.deepEqual(periodPointConditions(pick(period,[])),[]);
  }
  assert.deepEqual(ids(grid(),{...pick('5m',[]),signal_selection:{'1m':['1buy']},point:'3buy',frequency:'30m',period_points:{'30m':'3buy'}}),[]);
});

test('all legacy selector fields are ignored by the new canonical filter',()=>{
  const legacy={signal_selection:{'1m':['1buy']},point:'1buy',frequency:'1m',period_points:{'30m':'3sell'},period_match:'all'};
  assert.deepEqual(ids(grid(),legacy),POINTS.map(p=>'5m:'+p));
  assert.deepEqual(ids(grid(),{...legacy,...pick('30m',['3buy','3sell'])}),['30m:3buy','30m:3sell']);
});

test('normalization exposes unique ordered real conditions and rejects virtual types',()=>{
  const filters=pick('30m',['3sell','1buy','3sell','either','unknown']),before=JSON.stringify(filters);
  assert.deepEqual(periodPointConditions(filters),[{frequency:'30m',point:'1buy'},{frequency:'30m',point:'3sell'}]);
  assert.equal(JSON.stringify(filters),before);
  assert.deepEqual(ids(grid(),pick('30m',['either','unknown'])),[]);
  assert.deepEqual(ids([row('daily','d','3buy'),row('unknown','5m','unknown')]),[]);
});

test('market restrictions do not borrow evidence from matching codes in another market',()=>{
  const rows=[row('a','5m','3buy'),row('us','5m','3buy',{market:'us'}),row('other-period','30m','3buy')];
  assert.deepEqual(ids(rows,pick('5m',['3buy'])),['a','us']);
  assert.deepEqual(ids(rows,{...pick('5m',['3buy']),market:'a'}),['a']);
});

test('separate valid signals remain separate rows within the selected period',()=>{
  const rows=[row('a','5m','3buy'),row('b','5m','3buy'),row('c','5m','3sell')];
  assert.deepEqual(ids(rows,pick('5m',['3buy'])),['a','b']);
});

test('matching retained history remains context and current unavailable evidence is excluded',()=>{
  const rows=[row('current','30m','3sell'),row('pending','30m','3sell',{refresh_state:'pending',result_source:'old'}),
    row('limited','30m','3sell',{refresh_state:'limited',result_source:'old',evidence_available:false,evidence_chart_available:false}),
    row('unavailable','30m','3sell',{evidence_available:false}),row('different-period','5m','3sell',{refresh_state:'pending',result_source:'old'})];
  const before=JSON.stringify(rows),selection=pick('30m',['3sell']);
  assert.deepEqual(ids(rows,selection),['current','pending','limited']);
  assert.deepEqual(ids(rows,{...selection,include_previous:true}),['current','pending','limited']);
  assert.deepEqual(ids(rows,{...selection,include_previous:false}),['current']);
  assert.equal(JSON.stringify(rows),before);
});

test('all types and a matching type subset have identical lifecycle eligibility',()=>{
  const rows=[row('good','5m','3buy'),row('pending','5m','3buy',{refresh_state:'pending',result_source:'old'}),
    row('limited','5m','3buy',{refresh_state:'limited',result_source:'old'}),row('bad','5m','3buy',{evidence_available:false}),
    row('wrong-source','5m','3buy',{result_source:'old'}),row('closed','5m','3buy',{stage:'closed'})];
  for(const include_previous of [true,false])for(const signal_points of [POINTS,['3buy']])
    assert.deepEqual(ids(rows,{...pick('5m',signal_points),include_previous}),include_previous?['good','pending','limited']:['good']);
});

test('current sources are checked and compact absent evidence fields remain compatible',()=>{
  const candidate=row('explicit','30m','3sell',{result_source:SOURCE});
  const filters=pick('30m',['3sell']);
  assert.deepEqual(ids([candidate],filters),['explicit']);
  assert.deepEqual(ids([candidate],filters,'other'),[]);assert.deepEqual(ids([candidate],filters,null),[]);
  const compact=row('compact','30m','3sell');delete compact.point.status;delete compact.evidence_available;
  assert.deepEqual(ids([compact],filters,null),['compact']);
});

test('invalidated closed and left records cannot return through history or all-status selection',()=>{
  for(const status of ['invalidated','closed','left']){
    const stage=row('stage','30m','3sell',{stage:status}),point=row('point','30m','3sell');point.point.status=status;
    const previous={...stage,id:'previous',refresh_state:'pending',result_source:'old'};
    assert.deepEqual(ids([stage,point,previous],{...pick('30m'),point_status:'all'}),[]);
  }
});

test('confirmed forming and entry-pending meanings stay independent of period and type controls',()=>{
  const confirmed=row('confirmed','5m','3buy'),forming=row('forming','5m','3buy',{stage:'approaching'});forming.point.status='approaching';
  const entry=row('entry','5m','3buy',{stage:'entry_pending',nested_confirmation:{state:'waiting',frequency:'1m'}});
  for(const signal_points of [POINTS,['3buy']]){
    const selection=pick('5m',signal_points);
    assert.deepEqual(ids([confirmed,forming,entry],selection),['confirmed','entry']);
    assert.deepEqual(ids([confirmed,forming,entry],{...selection,point_status:'forming'}),['forming']);
    assert.deepEqual(ids([confirmed,forming,entry],{...selection,point_status:'all'}),['confirmed','forming','entry']);
  }
});

test('watchlist context follows origin independently of selected period or empty types',()=>{
  const watch={id:'watch',market:'us',code:'AAPL',name:'Apple',origin:'watchlist',stage:'monitoring',frequency:'30m',point:{}};
  const proof=row('proof','5m','3buy');
  assert.deepEqual(ids([watch,proof],{...pick('1m',[]),origin:'all'}),['watch']);
  assert.deepEqual(ids([watch,proof],{...pick('5m',['3buy']),origin:'all'}),['watch','proof']);
  assert.deepEqual(ids([watch,proof],{...pick('5m',['3buy']),origin:'watchlist'}),['watch']);
  assert.deepEqual(ids([watch],{...pick('1m',[]),origin:'all',market:'a'}),[]);
  assert.deepEqual(ids([watch],{...pick('1m',[]),origin:'all',query:'apple'}),['watch']);
  assert.deepEqual(watch.point,{});
});

test('nested confirmation and higher context cannot substitute for the selected physical period',()=>{
  const main=row('five','5m','3buy',{nested_confirmation:{frequency:'1m',state:'confirmed',point:{point_type:'3buy',status:'confirmed'}},
    higher_context:{frequency:'30m',category:'up_segment',point_type:'3sell'}});
  assert.deepEqual(ids([main],pick('1m',['3buy'])),[]);
  assert.deepEqual(ids([main],pick('30m',['3sell'])),[]);
});

test('single-period matching precedes pagination across the whole candidate set',()=>{
  const rows=Array.from({length:121},(_,i)=>row('row'+i,i===120?'30m':'1m',i===120?'3sell':'1buy'));
  const matches=filterRows(rows,{...defaults,...pick('30m',['3sell'])},{},{},SOURCE);
  assert.deepEqual(pageRows(matches,1).rows.map(r=>r.id),['row120']);assert.equal(matches.length,1);
});

test('advanced refinements remain AND constraints on the selected period and types',()=>{
  const candidate=row('match','30m','3sell',{sectors:['media'],confirmation_age_sessions:3,gain_pct:4});
  const selection=pick('30m',['3sell']);assert.deepEqual(ids([candidate],selection),['match']);
  for(const extra of [{market:'us'},{sector:'other'},{query:'missing'},{review:'reviewed'},{age:'1'},{gain:'3'}])
    assert.deepEqual(ids([candidate],{...selection,...extra}),[]);
  assert.deepEqual(ids([candidate],{...selection,review:'reviewed'},SOURCE,{match:{}}),['match']);
  assert.deepEqual(ids([candidate],{...selection,query:'media'},SOURCE,{}, {media:'media sector'}),['match']);
});

test('progressive updates retain only matching historical rows until real withdrawal',()=>{
  const rows=[row('buy','5m','3buy'),row('sell','5m','3sell'),row('other','30m','3buy')],before=JSON.stringify(rows);
  const selection=pick('5m',['3buy','3sell']),limited=rows.map(r=>r.id==='buy'?{...r,refresh_state:'limited',result_source:'old'}:r);
  assert.deepEqual(ids(limited,selection),['buy','sell']);
  assert.deepEqual(ids(limited,{...selection,include_previous:false}),['sell']);
  assert.deepEqual(ids(rows.filter(r=>r.id!=='buy'),selection),['sell']);assert.equal(JSON.stringify(rows),before);
});
