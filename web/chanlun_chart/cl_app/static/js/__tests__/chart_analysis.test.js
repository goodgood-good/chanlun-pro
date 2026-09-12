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
 assert.deepEqual(Analysis.summarizeChartData(payload),{bars:3,strokes:2,segments:1,centers,ready:true,loading:false});
 assert.equal(Analysis.summarizeChartData({...payload,strict_structure_mode:'unavailable'}).centers.length,0);
});
test('layer changes redraw without enabling removed analysis',()=>{
 let draws=0;const manager={cl_show_config:{bi:true},debouncedDrawChanlun(){draws++;}};
 assert.equal(Analysis.setLayerVisibility(manager,'bi',false),true);assert.equal(manager.cl_show_config.bi,false);
 assert.equal(Analysis.setLayerVisibility(manager,'point_all',true),false);assert.equal(draws,1);
});
