'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const Analysis=require('../chart_analysis.js');

function nativeCenter(overrides={}) {
 const ticks=[90,120,100,110,90,120];
 const roles=ticks.slice(1).map((end,i)=>({unit_id:`u${i+1}`, direction:end>ticks[i]?'up':'down',
  start_tick:ticks[i],end_tick:end,low_tick:Math.min(ticks[i],end),high_tick:Math.max(ticks[i],end),
  start_time:100+i*100,end_time:200+i*100,locked:true,confirmed_at:200+i*100}));
 return {center_id:'c1',source_kind:'segment',formation_rule:'physical_five',render_kind:'formal_center',
  core:{zd_tick:100,zg_tick:110,zd_price:10,zg_price:11},available_at:600,
  core_component_count:3,lifecycle_role_count:5,overlap_component_count:5,
  minimum_lifecycle_role_count:5,establishment_component_count:5,
  entry_unit_id:'u1',establishment_leave_unit_id:'u5',initial_exit_unit_id:'u5',
  core_unit_ids:['u2','u3','u4'],initial_unit_ids:['u2','u3','u4'],
  establishment_segment_ids:roles.map(unit=>unit.unit_id),establishment_segments:roles,...overrides};
}
test('empty and pending charts distinguish missing structures from zero centers',()=>{
 assert.equal(Analysis.summarizeChartData(null).ready,false);
 const pending=Analysis.summarizeChartData({bars:[{}],strict_structure_mode:'unavailable',strict_structure_error:{code:'strict_structure_pending'}});
 assert.equal(pending.loading,true);assert.equal(pending.bars,1);assert.equal(pending.ready,false);
 const empty=Analysis.summarizeChartData({strict_structure_mode:'replace',strict_structure:{analysis_scope:'native_centers',levels:[{centers:[]}]}});
 assert.equal(empty.ready,true);assert.equal(empty.centers.length,0);
});
test('native overview reads only the current native snapshot',()=>{
 const centers=[nativeCenter()];
 const payload={bars:[{},{},{}],bis:[{},{}],xds:[{}],strict_structure_mode:'replace',strict_structure:{analysis_scope:'native_centers',levels:[{centers}]}};
 assert.deepEqual(Analysis.summarizeChartData(payload),{bars:3,strokes:2,segments:1,centers,previews:[],ready:true,loading:false});
 assert.equal(Analysis.summarizeChartData({...payload,strict_structure_mode:'unavailable'}).centers.length,0);
});

test('native overview rejects three/four roles and unconfirmed formal centers, but keeps a fifth-role preview',()=>{
 const good=nativeCenter();
 const old=nativeCenter({formation_rule:'three_core'});
 const incomplete=nativeCenter({establishment_leave_unit_id:null,establishment_component_count:4,
  establishment_segments:good.establishment_segments.slice(0,4)});
 const pending=nativeCenter({render_kind:'center_preview'});
 pending.establishment_segments[4].locked=false;
 const summarize=(centers,previews=[])=>Analysis.summarizeChartData({strict_structure_mode:'replace',
  strict_structure:{analysis_scope:'native_centers',levels:[{centers,center_previews:previews}]}});
 assert.deepEqual(summarize([good,old,incomplete,pending],[old,incomplete,pending]).centers,[good]);
 assert.deepEqual(summarize([],[old,incomplete,pending]).previews,[pending]);
 delete good.establishment_segments;
 assert.deepEqual(summarize([good]).centers,[]);
});

test('native overview requires common five-role intersection while retaining the wider middle-three core',()=>{
 const value=nativeCenter();
 value.establishment_segments[0].start_tick=105;
 value.establishment_segments[0].low_tick=105;
 const summarize=()=>Analysis.summarizeChartData({strict_structure_mode:'replace',
  strict_structure:{analysis_scope:'native_centers',levels:[{centers:[value]}]}});
 assert.deepEqual(summarize().centers,[value]);
 assert.equal(value.core.zd_tick,100,'frame must retain the three middle segments core');
 value.establishment_segments[0].start_tick=115;
 value.establishment_segments[0].low_tick=115;
 assert.deepEqual(summarize().centers,[]);
});
test('archived selected center fields cannot override formal centers and previews',()=>{
 const formal={center_id:'formal'},preview={center_id:'preview'},old={center_id:'old',formation_rule:'three_core'};
 const oldPreview={center_id:'old-preview',formation_rule:'three_core'};
 const summary=Analysis.summarizeChartData({strict_structure_mode:'replace',strict_structure:{
  analysis_scope:'centers_and_signals',levels:[{centers:[formal,old],center_previews:[preview,oldPreview]}],
  user_same_level_type_view:{schema:'chanlun-user-same-level-structure-view-v1',formal_point_issued:false},
  selected_user_center_policy:{levels:[{structural_level:0,observed_center_ids:[]}]},
  selected_five_segment_centers:{schema:'chanlun-user-five-segment-centers-v3',
   centers:[{center_id:'experimental',core_ticks:[100,110]}]},
  selected_center_type_partition:{schema:'chanlun-user-center-type-partition-v1'},
 }});
 assert.deepEqual(summary.centers,[formal,old]);
 assert.deepEqual(summary.previews,[preview,oldPreview]);
 assert.equal(summary.userView,undefined);
 assert.equal(summary.directFive,undefined);
});
test('forming-only payload is ready even before any center becomes formal',()=>{
 const previews=[{center_id:'p1',owner_center_id:null,preview_status:'awaiting_leave'}];
 const summary=Analysis.summarizeChartData({strict_structure_mode:'replace',strict_structure:{
  analysis_scope:'centers_and_signals',levels:[{centers:[],center_previews:previews}]}});
 assert.equal(summary.ready,true);assert.equal(summary.centers.length,0);assert.deepEqual(summary.previews,previews);
});
test('center rows keep confirmed endings separate from pending returns and extensions', (t) => {
 const previousDocument = globalThis.document, previousManagers = globalThis.__cm;
 t.after(() => {globalThis.document = previousDocument; globalThis.__cm = previousManagers;});
 const element = () => ({children: [], textContent: '',
  replaceChildren() {this.children = [];}, appendChild(child) {this.children.push(child);}});
 const rows = element();
 globalThis.document = {getElementById: id => id === 'ca-native-centers' ? rows : null,
  createElement: element, querySelectorAll: () => []};
 const ended = {center_id: 'ended', third_class_confirmed: true};
 const divided = {center_id: 'divided', state: 'divergence_closed', third_class_confirmed: false};
 const waiting = {center_id: 'waiting'}, extending = {center_id: 'extending'};
 const previews = [{owner_center_id: 'waiting', preview_status: 'awaiting_completion_confirmation'},
  {owner_center_id: 'extending', preview_status: 'extending'},
  {center_id: 'forming', render_kind: 'center_preview', preview_status: 'awaiting_completion_confirmation'}];
 const data = {strict_structure_mode: 'replace', strict_structure: {analysis_scope: 'centers_and_signals',
  levels: [{centers: [ended, waiting, extending, divided], center_previews: previews}]}};
 globalThis.__cm = {one: {id: 'one', getChartData: () => ({barsResult: data})}};
 Analysis.refresh();
 assert.deepEqual(rows.children.map(row => row.children.map(cell => cell.textContent).filter((_, i) => i !== 1)),
  [['Z1', '已结束'], ['Z2', '回试待确认'], ['Z3', '延伸 / 观察'], ['Z4', '背驰分界'], ['P1', '回试待确认']]);
 waiting.third_class_confirmed = true;
 data.strict_structure.levels[0].center_previews = previews.slice(1);
 Analysis.refresh();
 assert.equal(rows.children[1].children[2].textContent, '已结束');
 assert.equal(rows.children[2].children[2].textContent, '延伸 / 观察');
});
test('layer changes redraw without enabling removed analysis',()=>{
 let draws=0;const manager={cl_show_config:{bi:true},debouncedDrawChanlun(){draws++;}};
 assert.equal(Analysis.setLayerVisibility(manager,'bi',false),true);assert.equal(manager.cl_show_config.bi,false);
 assert.equal(Analysis.setLayerVisibility(manager,'point_all',true),false);assert.equal(draws,1);
});

test('unresolved segment coverage is visible without being counted as an extra segment',()=>{
 const state={status:'unresolved',confirmed_segments:70,preview_segments:0,awaiting_confirmation_segments:0,
  tail:{pen_count:283}};
 const summary=Analysis.summarizeChartData({xds:Array(70).fill({locked:true}),strict_structure_mode:'replace',
  strict_structure:{analysis_scope:'centers_and_signals',levels:[{centers:[]}],segment_construction:state}});
 assert.equal(summary.segments,70);
 assert.equal(summary.ready,true);
 assert.match(Analysis.segmentStatusText(summary),/283 笔的线段分界待判定/);
 assert.match(Analysis.segmentStatusText(summary),/不作为选股确认段/);
});
