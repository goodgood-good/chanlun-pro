'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template=fs.readFileSync(path.join(__dirname,'../../../templates/options.html'),'utf8');
const source=template.match(/<script nonce=[^>]+>([\s\S]*?)<\/script>/)[1];
function page() {
  const values={config_use_type:'custom',chart_show_fx:'1',chart_show_bi:'1',chart_show_xd:'1'};
  const nodes=new Map(), formEvents={}, requests=[], toasts=[], reloads=[];
  const get=selector=>{if(!nodes.has(selector))nodes.set(selector,{listeners:{},properties:{},attributes:{},textContent:'',
    on(name,fn){this.listeners[name]=fn;return this;},prop(name,value){this.properties[name]=value;return this;},
    attr(name,value){this.attributes[name]=value;return this;},text(value){this.textContent=value;return this;}});return nodes.get(selector);};
  const sandbox={$:get,AppRequest:{ajax:options=>requests.push(options)},
    layui:{form:{val:()=>({...values}),render(){},on:(name,fn)=>formEvents[name]=fn},
      layer:{msg:(message,_options,done)=>toasts.push({message,done})}},
    location:{reload:()=>reloads.push({...values})}};
  sandbox.layui.use=(_names,fn)=>fn();
  vm.runInNewContext(source,sandbox);
  return {values,get,formEvents,requests,toasts,reloads,sandbox,
    click(which='save'){get('#'+which+'_cl_config_btn').listeners.click();},
    edit(name,value,event='checkbox'){values[name]=value;formEvents[event]();},
    success(index=0){requests[index].success({ok:true});requests[index].complete();}};
}

test('save and delete share one bounded request and controls recover after completion',()=>{
  const ui=page();ui.click();ui.click();ui.click('delete');
  assert.equal(ui.requests.length,1);
  assert.equal(ui.requests[0].timeout,15000);
  assert.equal(ui.requests[0].data.is_del,'false');
  assert.equal(ui.get('#save_cl_config_btn, #delete_cl_config_btn').properties.disabled,true);
  ui.success();
  assert.equal(ui.get('#save_cl_config_btn, #delete_cl_config_btn').properties.disabled,false);
  ui.toasts[0].done();assert.equal(ui.reloads.length,1);
});

for(const when of ['during request','during success toast'])
test(`edits ${when} survive the saved version acknowledgement and delayed reload`,()=>{
  const ui=page();ui.click();
  if(when==='during request')ui.edit('chart_show_xd','0');
  ui.success();
  if(when==='during success toast')ui.edit('chart_show_xd','0');
  ui.toasts[0].done();
  assert.equal(ui.requests[0].data.chart_show_xd,'1');
  assert.equal(ui.values.chart_show_xd,'0');
  assert.equal(ui.reloads.length,0);
  assert.match(ui.get('#config-save-status').textContent,/后续修改尚未保存/);
});

test('an older success toast cannot reload while a newer request is pending',()=>{
  const ui=page();ui.click();ui.success();ui.click('delete');
  ui.toasts[0].done();assert.equal(ui.reloads.length,0);
  assert.equal(ui.requests.length,2);assert.equal(ui.requests[1].data.is_del,'true');
  ui.success(1);ui.toasts[1].done();assert.equal(ui.reloads.length,1);
});

test('unchecked fields, scope edits and programmatic form changes are preserved',()=>{
  const ui=page();ui.values.chart_show_fx=undefined;ui.click();
  assert.equal(ui.requests[0].data.chart_show_fx,'0');
  ui.edit('config_use_type','common','select');ui.success();ui.toasts[0].done();
  assert.equal(ui.reloads.length,0);
  ui.click();ui.success(1);
  ui.values.chart_show_bi='0'; // Equality guard also covers libraries that emit no native change event.
  ui.toasts[1].done();assert.equal(ui.reloads.length,0);
});

test('timeout and rejected saves keep edits and allow retry with explicit feedback',()=>{
  const ui=page();ui.click();ui.edit('chart_show_bi','0');
  ui.requests[0].error({},'timeout');ui.requests[0].complete();
  assert.equal(ui.get('#save_cl_config_btn, #delete_cl_config_btn').properties.disabled,false);
  assert.match(ui.get('#config-save-status').textContent,/超时.*结果未确认.*修改已保留/);
  ui.click();assert.equal(ui.requests[1].data.chart_show_bi,'0');
  ui.requests[1].success({ok:false,msg:'rejected by server'});ui.requests[1].complete();
  assert.equal(ui.get('#config-save-status').textContent,'rejected by server');
  assert.equal(ui.reloads.length,0);
});

test('synchronous request failures release the action guard',()=>{
  const ui=page();ui.sandbox.AppRequest.ajax=()=>{throw new Error('unavailable');};
  ui.click();assert.equal(ui.get('#save_cl_config_btn, #delete_cl_config_btn').properties.disabled,false);
  assert.match(ui.get('#config-save-status').textContent,/失败.*修改已保留/);
});
