'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page() {
  const elements = new Map(), events = {}, draws = [], saved = [];
  const element = () => ({children:[],textContent:'',value:'',listeners:{},attributes:{},
    appendChild(child){this.children.push(child);},replaceChildren(...children){this.children=children;},
    addEventListener(name, fn){this.listeners[name]=fn;},setAttribute(name,value){this.attributes[name]=value;}});
  const get = id => {if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const layer = get('layer'); layer.dataset = {chartLayer:'bi'};
  const registry = {activeManagerId:'second-instance'};
  const manager = (id,symbol,interval) => ({id,instanceId:id+'-instance',cl_show_config:{bi:true},
    identity:{symbol,interval},_curResolution:interval,
    getCurrentChartIdentity(){return this.identity;},
    getChartData:()=>({barsResult:{bars:[{}],strict_structure_mode:'replace',
      strict_structure:{analysis_scope:'native_centers',levels:[{centers:[]}]}}}),
    debouncedDrawChanlun(){draws.push(id);}});
  const first=manager('first','a:SH.600000','5'), second=manager('second','a:SZ.000001','30');
  const sandbox={console,__cm:{first,second},getTVRegistry:()=>registry,
    saveClShowConfig:(...args)=>saved.push(args),setInterval:()=>0,
    document:{readyState:'loading',addEventListener(){},getElementById:get,
      querySelectorAll:()=>[layer],createElement:element},
    addEventListener:(name,fn)=>{events[name]=fn;}};
  sandbox.globalThis=sandbox;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'..','chart_analysis.js'),'utf8'),sandbox);
  sandbox.ChartAnalysis.init();
  return {get,first,second,registry,draws,saved,events,sandbox,
    choose(value){get('ca-chart-target').value=value;get('ca-chart-target').listeners.change({target:get('ca-chart-target')});}};
}

test('the explicit sidebar target owns labels and layer controls despite other chart readiness events',()=>{
  const ui=page();
  assert.equal(ui.get('ca-chart-target').disabled,false);
  assert.equal(ui.get('ca-chart-target').children.length,2);
  assert.match(ui.get('ca-current-symbol').textContent,/SH\.600000.*5m/);
  ui.choose('second-instance');
  assert.equal(ui.registry.activeManagerId,'second-instance');
  assert.match(ui.get('ca-current-symbol').textContent,/SZ\.000001.*30m/);
  ui.registry.activeManagerId='first-instance'; // Another onChartReady callback cannot steal selection.
  ui.events['chanlun-bars-ready']({detail:{managerId:'first-instance'}});
  assert.equal(ui.registry.activeManagerId,'second-instance');
  assert.equal(ui.get('ca-chart-target').value,'second-instance');
  ui.get('layer').listeners.click();
  assert.equal(ui.first.cl_show_config.bi,true);
  assert.equal(ui.second.cl_show_config.bi,false);
  assert.deepEqual(ui.draws,['second']);
  assert.equal(ui.saved[0][0],'second');
  assert.equal(ui.saved[0][1],'30');
});

test('target labels follow symbol and period updates while disposal safely picks a remaining chart',()=>{
  const ui=page();ui.choose('second-instance');
  ui.second.identity={symbol:'a:SZ.000002',interval:'1'};
  ui.events['chanlun-chart-context-changed']();
  assert.match(ui.get('ca-chart-target').children[1].textContent,/SZ\.000002.*1m/);
  assert.match(ui.get('ca-current-symbol').textContent,/SZ\.000002.*1m/);
  ui.second._disposed=true;
  ui.sandbox.ChartAnalysis.refresh();
  assert.equal(ui.get('ca-chart-target').value,'first-instance');
  assert.equal(ui.get('ca-chart-target').children.length,1);
  assert.equal(ui.get('ca-chart-target').disabled,true);
  assert.equal(ui.registry.activeManagerId,'first-instance');
  ui.first._disposed=true;ui.sandbox.ChartAnalysis.refresh();
  assert.equal(ui.get('ca-current-symbol').textContent,'等待行情');
  assert.equal(ui.get('ca-chart-target').children.length,0);
  assert.equal(ui.get('layer').disabled,true);
  assert.equal(ui.registry.activeManagerId,null);
});

test('an invalid or removed target value cannot move the sidebar away from its live target',()=>{
  const ui=page();ui.choose('second-instance');ui.choose('removed');
  assert.equal(ui.get('ca-chart-target').value,'second-instance');
  assert.match(ui.get('ca-current-symbol').textContent,/SZ\.000001/);
});

test('settings identity is captured from the selected instance and never guessed for an unqualified symbol',()=>{
  const ui=page();ui.choose('second-instance');
  const captured=ui.sandbox.ChartAnalysis.selectedIdentity();
  assert.equal(captured.market,'a');assert.equal(captured.code,'SZ.000001');
  assert.equal(captured.interval,'30');assert.equal(Object.isFrozen(captured),true);
  ui.second.identity={symbol:'US:AAPL.US',interval:'5'};
  assert.equal(captured.code,'SZ.000001');
  assert.equal(ui.sandbox.ChartAnalysis.selectedIdentity().market,'us');
  ui.second.identity={symbol:'AAPL.US',interval:'5'};
  assert.equal(ui.sandbox.ChartAnalysis.selectedIdentity(),null);
});
