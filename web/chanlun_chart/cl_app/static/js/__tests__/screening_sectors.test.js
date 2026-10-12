'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {sectorLevel,sectorSummary,pageRows}=require('../screening_workbench.js');

const SOURCE='run';
const INDUSTRY='qmt-gics3:industry',SUBINDUSTRY='qmt-gics4:child',OTHER='external:other';
const POINTS=['1buy','2buy','3buy','1sell','2sell','3sell'];
const filters={signal_period:'5m',signal_points:POINTS,market:'all',origin:'screening',query:'',sector:'',
  review:'all',stage:'all',risk:'all',point_status:'confirmed',lower_status:'all',higher:'all',center_order:'all',age:'all',gain:'all',sort:'recent'};
const sector=(id=INDUSTRY,name='Industry',extra={})=>({id,name,member_count:100,historical_rank:1,...extra});
const candidate=(id,code='SH.600001',point_type='3buy',extra={})=>({id,code,name:code,market:'a',frequency:'5m',origin:'screening',stage:'confirmed',
  point:{point_id:id,point_type,status:'confirmed',available_at:100},selection_available_at:100,evidence_available:true,sectors:[INDUSTRY],...extra});
const watched=(code='SH.600001',extra={})=>({id:'watch:a:'+code,code,name:code,market:'a',frequency:'5m',origin:'watchlist',stage:'monitoring',point:{},sectors:[INDUSTRY],...extra});
const summary=(sectors,rows,extra={},reviewed={})=>sectorSummary(sectors,rows,{...filters,...extra},reviewed,SOURCE);
const counts=value=>Object.fromEntries(['current_symbols','current_signals','buy_symbols','sell_symbols','retained_symbols','watch_symbols','latest_at'].map(key=>[key,value[key]]));
const expected=(extra={})=>({current_symbols:0,current_signals:0,buy_symbols:0,sell_symbols:0,retained_symbols:0,watch_symbols:0,latest_at:0,...extra});
const freeze=value=>{if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;};

test('sector level uses the exact known ID prefixes and leaves other IDs separate',()=>{
  assert.equal(sectorLevel(sector(INDUSTRY)),'industry');
  assert.equal(sectorLevel(sector(SUBINDUSTRY)),'subindustry');
  for(const id of [OTHER,'qmt-gics30:fake','qmt-gics4','other-qmt-gics3:fake',''])
    assert.equal(sectorLevel(sector(id)),'other',id);
  assert.equal(sectorLevel({name:'unknown'}),'other');
});

test('empty summaries retain real catalog cards with zero counters and fresh levels',()=>{
  const result=summary([sector(INDUSTRY,'B',{current_symbols:999}),sector(SUBINDUSTRY,'A')],[]);
  assert.deepEqual(result.sectors.map(s=>s.id),[SUBINDUSTRY,INDUSTRY]);
  assert.deepEqual(result.sectors.map(s=>s.level),['subindustry','industry']);
  for(const item of result.sectors)assert.deepEqual(counts(item),expected());
  assert.deepEqual(counts(result.totals),expected());
  assert.deepEqual(summary([],[]).sectors,[]);
});

test('counts distinguish unique symbols individual signals and buy or sell risk',()=>{
  const rows=[candidate('a1'),candidate('a2','SH.600001','2buy'),candidate('a3','SH.600001','3sell'),candidate('b1','SH.600002','1sell')];
  const result=summary([sector()],rows);
  const wanted=expected({current_symbols:2,current_signals:4,buy_symbols:1,sell_symbols:2,latest_at:100});
  assert.deepEqual(counts(result.sectors[0]),wanted);assert.deepEqual(counts(result.totals),wanted);
});

test('symbol identity includes market while a symbol may have both buy and sell signals',()=>{
  const rows=[candidate('a','SAME','3buy'),candidate('us','SAME','3sell',{market:'us'})];
  const result=summary([sector()],rows);
  assert.deepEqual(counts(result.totals),expected({current_symbols:2,current_signals:2,buy_symbols:1,sell_symbols:1,latest_at:100}));
});

test('duplicate row IDs and repeated sector memberships count only once',()=>{
  const first=candidate('same','SH.600001','3buy',{sectors:[INDUSTRY,INDUSTRY]});
  const result=summary([sector()],[first,{...first},candidate('second','SH.600001','2buy')]);
  assert.deepEqual(counts(result.sectors[0]),expected({current_symbols:1,current_signals:2,buy_symbols:1,latest_at:100}));
  assert.deepEqual(counts(result.totals),counts(result.sectors[0]));
});

test('missing row IDs use signal identity without collapsing different points',()=>{
  const first=candidate('point-one');delete first.id;
  const second={...first,point:{...first.point,point_id:'point-two'}};
  const result=summary([sector()],[first,JSON.parse(JSON.stringify(first)),second]);
  assert.deepEqual(counts(result.totals),expected({current_symbols:1,current_signals:2,buy_symbols:1,latest_at:100}));
});

test('cross-layer membership increments each actual card once but totals are not summed across cards',()=>{
  const row=candidate('a','SH.600001','3buy',{sectors:[INDUSTRY,SUBINDUSTRY]});
  const result=summary([sector(INDUSTRY),sector(SUBINDUSTRY)],[row]);
  for(const item of result.sectors)assert.equal(item.current_symbols,1);
  assert.deepEqual(counts(result.totals),expected({current_symbols:1,current_signals:1,buy_symbols:1,latest_at:100}));
});

test('unknown or absent sector membership cannot manufacture cards but remains in valid totals',()=>{
  const rows=[candidate('unknown','SH.600001','3buy',{sectors:['missing']}),candidate('none','SH.600002','3sell',{sectors:[]})];
  const result=summary([sector()],rows);
  assert.equal(result.sectors.length,1);assert.equal(result.sectors[0].id,INDUSTRY);
  assert.deepEqual(counts(result.sectors[0]),expected());
  assert.deepEqual(counts(result.totals),expected({current_symbols:2,current_signals:2,buy_symbols:1,sell_symbols:1,latest_at:100}));
});

test('single period and type choices apply before aggregation while selected sector is ignored',()=>{
  const rows=[candidate('a'),candidate('other-sector','SH.600002','3buy',{sectors:[SUBINDUSTRY]}),
    candidate('other-period','SH.600003','3buy',{frequency:'30m'}),candidate('sell','SH.600004','3sell')];
  const result=summary([sector(),sector(SUBINDUSTRY)],rows,{sector:INDUSTRY,signal_points:['3buy']});
  assert.equal(result.totals.current_symbols,2);assert.equal(result.totals.current_signals,2);
  assert.ok(result.sectors.every(s=>s.current_symbols===1));
  assert.equal(summary([sector()],rows,{signal_points:[]}).totals.current_symbols,0);
});

test('market search review and other independent refinements remain effective',()=>{
  const good=candidate('good','SH.600001','3buy',{name:'target',confirmation_age_sessions:0,gain_pct:1});
  const rows=[good,candidate('not-reviewed','SH.600002','3buy',{name:'target',confirmation_age_sessions:0,gain_pct:1}),
    candidate('wrong-market','AAPL','3buy',{market:'us',name:'target',confirmation_age_sessions:0,gain_pct:1}),
    candidate('old','SH.600003','3buy',{name:'target',confirmation_age_sessions:5,gain_pct:1})];
  const result=summary([sector()],rows,{market:'a',query:'target',review:'reviewed',age:'1',gain:'3'},
    {good:{},'wrong-market':{},old:{}});
  assert.deepEqual(counts(result.totals),expected({current_symbols:1,current_signals:1,buy_symbols:1,latest_at:100}));
  assert.equal(summary([sector()],rows,{query:'missing'}).totals.current_symbols,0);
});

test('current summary includes verified unconfirmed points when the status filter allows them',()=>{
  const forming=candidate('forming','SH.600002','3buy',{stage:'approaching'});forming.point.status='approaching';
  const closed=candidate('closed','SH.600003','3buy',{stage:'closed'});
  const rows=[candidate('good'),forming,closed,candidate('bad','SH.600004','3buy',{evidence_available:false}),
    candidate('stale-source','SH.600005','3buy',{result_source:'other'})];
  assert.deepEqual(counts(summary([sector()],rows,{point_status:'all'}).totals),
    expected({current_symbols:2,current_signals:2,buy_symbols:2,latest_at:100}));
});

test('retained and watch rows are separate context and never inflate current signals or latest time',()=>{
  const rows=[candidate('current'),candidate('pending','SH.600002','3buy',{refresh_state:'pending',result_source:'old',selection_available_at:900}),
    candidate('limited','SH.600002','3sell',{refresh_state:'limited',result_source:'old',evidence_available:false}),
    watched('SH.600003',{selection_available_at:1000})];
  const wanted=expected({current_symbols:1,current_signals:1,buy_symbols:1,retained_symbols:1,watch_symbols:1,latest_at:100});
  assert.deepEqual(counts(summary([sector()],rows,{origin:'all'}).totals),wanted);
  assert.deepEqual(counts(summary([sector()],rows,{origin:'all',include_previous:false}).totals),{...wanted,retained_symbols:0});
  assert.deepEqual(counts(summary([sector()],rows,{origin:'watchlist',signal_points:[]}).totals),expected({watch_symbols:1}));
});

test('latest time uses positive finite confirmation availability and not anchor or recheck times',()=>{
  const cases=[[200,100,200],[0,100,100],[-1,100,100],[NaN,100,100],[Infinity,100,100],[undefined,100,100],[null,100,100],
    [undefined,0,0],[undefined,-1,0],[undefined,Infinity,0]];
  for(const [selection_available_at,available_at,latest] of cases){
    const row=candidate('a','SH.600001','3buy',{selection_available_at,latest_recheck_at:999999,latest_recheck_closed_at:999999});
    row.point.available_at=available_at;row.point.anchor_at=999999;
    const result=summary([sector()],[row]);assert.equal(result.totals.latest_at,latest);assert.equal(result.sectors[0].latest_at,latest);
  }
});

test('tier ordering puts current ahead of historical then watch then empty regardless of dates or old rank',()=>{
  const catalog=[sector('empty','A',{historical_rank:1}),sector('watch','B'),sector('retained','C'),sector('current','Z',{historical_rank:999})];
  const rows=[candidate('current','SH.600001','3buy',{sectors:['current'],selection_available_at:1}),
    candidate('old','SH.600002','3buy',{sectors:['retained'],refresh_state:'pending',result_source:'old',selection_available_at:999999}),
    watched('SH.600003',{sectors:['watch']})];
  assert.deepEqual(summary(catalog,rows,{origin:'all'}).sectors.map(s=>s.id),['current','retained','watch','empty']);
});

test('current rank uses latest time then distinct symbols rather than signal count or historical strength',()=>{
  const catalog=[sector('many-signals','A',{member_count:10000,historical_rank:1,historical_strength:999}),
    sector('breadth','B',{member_count:2,historical_rank:999}),sector('fresh','Z')];
  const rows=[...Array.from({length:8},(_,i)=>candidate('multi'+i,'SH.600001','3buy',{sectors:['many-signals']})),
    candidate('b1','SH.600002','3buy',{sectors:['breadth']}),candidate('b2','SH.600003','3buy',{sectors:['breadth']}),
    candidate('fresh','SH.600004','3sell',{sectors:['fresh'],selection_available_at:101})];
  assert.deepEqual(summary(catalog,rows).sectors.map(s=>s.id),['fresh','breadth','many-signals']);
});

test('retained and watch tiers rank by their distinct symbols and fall back to name then stable ID',()=>{
  const catalog=[sector('r1','A'),sector('r2','Z'),sector('w1','A'),sector('w2','Z'),sector('z','Same'),sector('a','Same')];
  const rows=[candidate('r1','SH.600001','3buy',{sectors:['r1'],refresh_state:'pending'}),
    candidate('r21','SH.600002','3buy',{sectors:['r2'],refresh_state:'limited'}),candidate('r22','SH.600003','3buy',{sectors:['r2'],refresh_state:'pending'}),
    watched('SH.600004',{sectors:['w1']}),watched('SH.600005',{sectors:['w2']}),watched('SH.600006',{sectors:['w2']})];
  assert.deepEqual(summary(catalog,rows,{origin:'all'}).sectors.map(s=>s.id),['r2','r1','w2','w1','a','z']);
});

test('full ranking precedes pagination and no input catalog rows candidates or filters are mutated',()=>{
  const catalog=freeze(Array.from({length:31},(_,i)=>sector('s'+i,'Sector '+i)));
  const rows=freeze([candidate('last','SH.600001','3sell',{sectors:['s30'],selection_available_at:200})]);
  const active=freeze({...filters,sector:'s0'}),before=JSON.stringify({catalog,rows,active});
  const result=sectorSummary(catalog,rows,active,{},SOURCE),page=pageRows(result.sectors,1,12);
  assert.equal(page.rows[0].id,'s30');assert.equal(page.total,31);assert.equal(page.pages,3);
  assert.equal(new Set(result.sectors.map(s=>s.id)).size,31);
  assert.equal(JSON.stringify({catalog,rows,active}),before);
});
