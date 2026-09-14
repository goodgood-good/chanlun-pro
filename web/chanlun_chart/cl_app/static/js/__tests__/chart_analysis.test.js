'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const Analysis=require('../chart_analysis.js');
test('empty and pending charts distinguish missing structures from zero centers',()=>{
 assert.equal(Analysis.summarizeChartData(null).ready,false);
 const pending=Analysis.summarizeChartData({bars:[{}],strict_structure_mode:'unavailable',strict_structure_error:{code:'strict_structure_pending'}});
 assert.equal(pending.loading,true);assert.equal(pending.bars,1);assert.equal(pending.ready,false);
 const empty=Analysis.summarizeChartData({strict_structure_mode:'replace',strict_structure:{analysis_scope:'native_centers',levels:[{centers:[]}]}});
 assert.equal(empty.ready,true);assert.equal(empty.centers.length,0);
});
test('native overview reads only the current native snapshot',()=>{
 const centers=[{center_id:'c1',core:{zd_price:10,zg_price:11}}];
 const payload={bars:[{},{},{}],bis:[{},{}],xds:[{}],strict_structure_mode:'replace',strict_structure:{analysis_scope:'native_centers',levels:[{centers}]}};
 assert.deepEqual(Analysis.summarizeChartData(payload),{bars:3,strokes:2,segments:1,centers,previews:[],ready:true,loading:false});
 assert.equal(Analysis.summarizeChartData({...payload,strict_structure_mode:'unavailable'}).centers.length,0);
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
