'use strict';
// Cross-period predicates use verified independent candidates of the same symbol.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterRows,pageRows}=require('../screening_workbench.js');

const SOURCE='current-run';
const defaults={market:'all',origin:'screening',frequency:'all',point:'all',sector:'',review:'all',stage:'all',
  risk:'all',point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'};
function row(id,frequency,pointType,extra={}) {
  return {id,market:'a',code:'SH.600088',name:'fixture',frequency,origin:'screening',stage:'confirmed',
    point:{point_id:id,point_type:pointType,status:'confirmed'},evidence_available:true,...extra};
}
const base=()=>row('base','1m','1buy');
const anchorFilters=(extra={})=>({...defaults,frequency:'1m',point:'1buy',...extra});
const ids=(rows,filters={},source=SOURCE)=>filterRows(rows,anchorFilters(filters),{}, {},source).map(r=>r.id);


test('period condition searches all candidates before main period and point filtering',()=>{
  const rows=[base(),row('thirty-sell','30m','3sell')];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),['base']);
  assert.deepEqual(ids(rows,{period_points:{'30m':'3buy'}}),[]);
  assert.deepEqual(rows.map(r=>r.id),['base','thirty-sell']);
});

test('any is the default and all requires each configured independent period',()=>{
  const rows=[base(),row('five-buy','5m','3buy')];
  const period_points={'5m':'3buy','30m':'3sell'};
  assert.deepEqual(ids(rows,{period_points}),['base']);
  assert.deepEqual(ids(rows,{period_points,period_match:'any'}),['base']);
  assert.deepEqual(ids(rows,{period_points,period_match:'all'}),[]);
  rows.push(row('thirty-sell','30m','3sell'));
  assert.deepEqual(ids(rows,{period_points,period_match:'all'}),['base']);
});

test('each of 1m 5m and 30m accepts third buy or third sell independently',()=>{
  for(const frequency of ['1m','5m','30m']) {
    for(const point of ['3buy','3sell']) {
      const rows=[base(),row('proof',frequency,point)];
      assert.deepEqual(ids(rows,{period_points:{[frequency]:point}}),['base']);
      assert.deepEqual(ids(rows,{period_points:{[frequency]:'either'}}),['base']);
      assert.deepEqual(ids(rows,{period_points:{[frequency]:point==='3buy'?'3sell':'3buy'}}),[]);
    }
  }
});

test('either requires a third-class point rather than any first or second point',()=>{
  for(const point of ['1buy','2buy','1sell','2sell'])
    assert.deepEqual(ids([base(),row('other','30m',point)],{period_points:{'30m':'either'}}),[]);
});

test('all six types are selectable as an independent period condition',()=>{
  for(const point of ['1buy','2buy','3buy','1sell','2sell','3sell']) {
    const rows=[base(),row('proof','30m',point)];
    assert.deepEqual(ids(rows,{period_points:{'30m':point}}),['base']);
    for(const other of ['1buy','2buy','3buy','1sell','2sell','3sell'].filter(p=>p!==point))
      assert.deepEqual(ids(rows,{period_points:{'30m':other}}),[]);
  }
});

test('first second and third classes can form one all-period combination',()=>{
  const rows=[base(),row('second','5m','2buy'),row('third','30m','3buy')];
  assert.deepEqual(ids(rows,{period_points:{'1m':'1buy','5m':'2buy','30m':'3buy'},period_match:'all'}),['base']);
  assert.deepEqual(ids(rows.filter(r=>r.id!=='second'),{period_points:{'1m':'1buy','5m':'2buy','30m':'3buy'},period_match:'all'}),[]);
});

test('same code in another market cannot supply the requested period',()=>{
  const rows=[base(),row('foreign','30m','3sell',{market:'us'})];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),[]);
  rows.push(row('local','30m','3sell'));
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'}}),['base']);
});

test('multiple signals in one period do not replace another required period',()=>{
  const rows=[base(),row('five-1','5m','3buy'),row('five-2','5m','3buy'),row('five-sell','5m','3sell')];
  assert.deepEqual(ids(rows,{period_points:{'5m':'either','30m':'either'},period_match:'all'}),[]);
});

test('pending limited and unavailable evidence never qualify as period proof',()=>{
  const filters={period_points:{'30m':'3sell'}};
  for(const extra of [{refresh_state:'pending'},{refresh_state:'limited'},{evidence_available:false}])
    assert.deepEqual(ids([base(),row('stale','30m','3sell',extra)],filters),[]);
});

test('watchlist placeholders cannot supply third-class evidence',()=>{
  const placeholder=row('watch','30m','3sell',{origin:'watchlist'});
  assert.deepEqual(ids([base(),placeholder],{period_points:{'30m':'3sell'}}),[]);
});

test('invalid or unfinished stages cannot count even if an old point status says confirmed',()=>{
  for(const stage of ['invalidated','closed','left','approaching','formed','observed','monitoring','active'])
    assert.deepEqual(ids([base(),row(stage,'30m','3sell',{stage})],{period_points:{'30m':'3sell'}}),[],stage);
  for(const status of ['invalidated','closed','approaching','formed','observed','unknown']) {
    const proof=row(status,'30m','3sell');proof.point.status=status;
    assert.deepEqual(ids([base(),proof],{period_points:{'30m':'3sell'}}),[],status);
  }
});

test('formal confirmed entry-pending point remains valid without pretending entry is ready',()=>{
  const proof=row('entry','30m','3buy',{stage:'entry_pending',nested_confirmation:{state:'waiting',frequency:'1m'}});
  assert.deepEqual(ids([base(),proof],{period_points:{'30m':'3buy'}}),['base']);
  assert.equal(proof.stage,'entry_pending');
  assert.equal(proof.nested_confirmation.state,'waiting');
});

test('point status falls back to a confirmed row stage for compatible compact records',()=>{
  const proof=row('compact','30m','3sell');delete proof.point.status;delete proof.evidence_available;
  assert.deepEqual(ids([base(),proof],{period_points:{'30m':'3sell'}}),['base']);
});

test('explicit result source must match and cannot be assumed when source is unavailable',()=>{
  const proof=row('scoped','30m','3sell',{result_source:SOURCE});
  const filters={period_points:{'30m':'3sell'}};
  assert.deepEqual(ids([base(),proof],filters,SOURCE),['base']);
  assert.deepEqual(ids([base(),proof],filters,'other-run'),[]);
  assert.deepEqual(ids([base(),proof],filters,null),[]);
  assert.deepEqual(ids([base(),row('implicit','30m','3sell')],filters,null),['base']);
});

test('old main rows are not displayed as satisfied by a current counterpart',()=>{
  const proof=row('current','30m','3sell');
  const filters={period_points:{'30m':'3sell'}};
  for(const extra of [{refresh_state:'pending'},{refresh_state:'limited'},{result_source:'previous-run'}])
    assert.deepEqual(ids([base(),row('old-base','1m','1buy',extra),proof],filters),['base']);
});

test('a watchlist row can be selected from real same-symbol signal evidence',()=>{
  const watch={id:'watch',market:'a',code:'SH.600088',name:'watched',frequency:'1m',origin:'watchlist',stage:'monitoring',point:{}};
  const proof=row('true-proof','30m','3sell');
  const filters={...defaults,origin:'watchlist',period_points:{'30m':'3sell'}};
  assert.deepEqual(filterRows([watch,proof],filters,{}, {},SOURCE).map(r=>r.id),['watch']);
  assert.deepEqual(filterRows([watch,{...proof,refresh_state:'limited'}],filters,{}, {},SOURCE),[]);
});

test('nested confirmation and higher context do not invent independent timeframe rows',()=>{
  const main=row('five','5m','3buy',{nested_confirmation:{frequency:'1m',state:'confirmed',point:{point_type:'3buy',status:'confirmed'}},
    higher_context:{frequency:'30m',category:'up_segment',point_type:'3sell'}});
  for(const frequency of ['1m','30m']) {
    const filters={...defaults,period_points:{[frequency]:'either'}};
    assert.deepEqual(filterRows([main],filters,{}, {},SOURCE),[]);
  }
});

test('different valid completed closes remain independent and need not be equal',()=>{
  const rows=[{...base(),source_closed_at:180},{...row('thirty','30m','3buy'),source_closed_at:60}];
  assert.deepEqual(ids(rows,{period_points:{'30m':'3buy'},period_match:'all'}),['base']);
});

test('period evidence on later pages is found before pagination',()=>{
  const rows=Array.from({length:121},(_,i)=>row('base'+i,'1m','1buy',{code:'SH.'+String(600000+i)}));
  rows.push(row('late-proof','30m','3sell',{code:'SH.600120'}));
  const matched=filterRows(rows,anchorFilters({period_points:{'30m':'3sell'}}),{}, {},SOURCE);
  assert.deepEqual(pageRows(matched,1).rows.map(r=>r.id),['base120']);
  assert.equal(pageRows(matched,1).total,1);
});

test('default unlimited conditions preserve previous rows and all existing filters',()=>{
  const rows=[base(),row('old','1m','1buy',{refresh_state:'pending'}),row('thirty','30m','3sell')];
  const before=JSON.stringify(rows);
  const ordinary=ids(rows);
  assert.deepEqual(ordinary,['base','old']);
  assert.deepEqual(ids(rows,{period_points:{'1m':'all','5m':'all','30m':'all'},period_match:'all'}),ordinary);
  assert.deepEqual(ids(rows,{period_points:{},period_match:'any'}),ordinary);
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'},query:'no-such-stock'}),[]);
  assert.deepEqual(ids(rows,{period_points:{'30m':'3sell'},review:'reviewed'}),[]);
  assert.equal(JSON.stringify(rows),before);
});

test('new result updates remove expired combinations without changing row identity or input',()=>{
  const rows=[base(),row('five','5m','3buy'),row('thirty','30m','3sell')];
  const filters={period_points:{'5m':'3buy','30m':'3sell'},period_match:'all'};
  const before=JSON.stringify(rows);
  assert.deepEqual(ids(rows,filters),['base']);
  assert.deepEqual(ids(rows.filter(r=>r.id!=='thirty'),filters),[]);
  assert.deepEqual(ids(rows.map(r=>r.id==='thirty'?{...r,refresh_state:'limited'}:r),filters),[]);
  assert.equal(JSON.stringify(rows),before);
});
