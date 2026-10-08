'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tag=tag;this.children=[];this.textContent='';this.value=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener() {}
}

async function render(direction, stage='confirmed') {
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const pan={id:'pan',market:'a',code:'SH.600088',name:'held',frequency:'30m',
    point_type:'consolidation_'+direction,signal_type:'consolidation_divergence',stage,
    point_status:stage,macd_area_a:10,macd_area_c:5,core_position:'inside',
    anchor_price:12.3,available_at:1000,source_closed_at:1000,recorded_at:1000,change:'discovered'};
  const point={...pan,id:'point',signal_type:'buy_sell_point',point_type:'3buy',stage:'confirmed',point_status:'confirmed'};
  const data={enabled:true,runtime_running:true,today:'2026-09-30',
    settings:{morning_after_close:'11:35',after_close:'15:10',interval_seconds:60,daily_scope:'all_a_watchlist'},
    seed:{codes:['a:SH.600088'],symbols:[pan],screening_symbols:[],watchlist_count:1,holding_count:1},
    signals:[pan,point],events:[pan],errors:[],pool_markets:{a:1}};
  const context={document:{getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>({content:'csrf'})},
    fetch:async()=>({ok:true,json:async()=>data}),URLSearchParams,Date,setTimeout:()=>0};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../signal_monitor.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return elements;
}

for(const [stage,label] of [['warning','提前预警（未确认）'],['invalidated','预警已失效']]){
  test(`consolidation ${stage} is never labeled as a confirmed buy or sell`,async()=>{
    const elements=await render('up',stage);
    assert.equal(elements.get('signals').children[0].children[2].children[0].textContent,label);
    assert.match(elements.get('events').children[0].children[1].textContent,new RegExp(label.replace(/[（）]/g,'.')));
    assert.equal(elements.get('signals').children[1].children[2].children[0].textContent,'已确认');
  });
}

for (const direction of ['up','down']) {
  test(`held ${direction} consolidation renders separately and opens its actual period`,async()=>{
    const elements=await render(direction);
    const rows=elements.get('signals').children;
    assert.equal(rows.length,2);
    assert.match(rows[0].children[1].textContent,/盘整背驰/);
    assert.match(rows[1].children[1].textContent,/三买/);
    assert.equal(rows[0].children[2].children[0].textContent,'已确认');
    assert.match(rows[0].children[2].children[1].textContent,/持仓标的/);
    assert.match(rows[0].children[2].children[2].textContent,/MACD 同向柱面积 A 10 \/ C 5/);
    const url=new URL(rows[0].children[0].children[0].href,'http://local');
    assert.equal(url.searchParams.get('frequency'),'30m');
    assert.match(elements.get('pool-source').textContent,/持仓 1 只/);
    assert.match(elements.get('events').children[0].children[1].textContent,/盘整背驰/);
  });
}
