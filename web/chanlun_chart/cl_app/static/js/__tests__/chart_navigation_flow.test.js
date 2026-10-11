'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../../templates/index.html'), 'utf8');
const bootstrap = template.match(/\(function applyUrlSymbolParams\(\) \{[\s\S]*?\n    \}\)\(\);/)[0];
const menuObject = template.match(/dropdown\.render\((\{[\s\S]*?\n        \})\);/)[1];

function page({search='?market=a&code=SH.600088&frequency=5m&layout=single&intervals=5&workspace_layout=1', phone=false, parentMode='top'}={}) {
  const values={market:'a',a_code:'SH.600088',chart_layout_type:'single',a_interval_2:'1D',a_interval_3:'30',a_interval_4:'1'};
  const elements=new Map(), navigations=[], opened=[], messages=[], events={};
  const element=()=>({children:[],textContent:'',value:'',listeners:{},
    replaceChildren(...children){this.children=children;},appendChild(child){this.children.push(child);},
    addEventListener(name,fn){this.listeners[name]=fn;},setAttribute(){}});
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const registry={activeManagerId:null};
  const sb={console,URLSearchParams,initial_market:'a',__cm:{},chart_widgets:[],
    location:{origin:'http://local',pathname:'/',hash:'',search,assign:url=>navigations.push(url)},
    WorkspaceDevice:{isPhone:phone,layout:value=>phone?'single':value},
    Utils:{get_market:()=>values.market,get_code:()=>values[values.market+'_code'],
      get_local_data:key=>values[key],set_local_data:(key,value)=>values[key]=value},
    AccountPreferences:{navigateAfterSave:(url,replace)=>{assert.equal(replace,false);navigations.push(url);}},
    layer:{open:options=>opened.push(options),msg:message=>messages.push(message)},getTVRegistry:()=>registry,
    document:{readyState:'loading',addEventListener(){},getElementById:get,querySelectorAll:()=>[],createElement:element},
    addEventListener:(name,fn)=>events[name]=fn,setInterval:()=>0};
  sb.window=sb;sb.globalThis=sb;sb.parent=sb;
  if(parentMode!=='top') {
    const frame={contentWindow:parentMode==='managed'?sb:{}};
    sb.parent={location:{origin:'http://local'},document:{getElementById:id=>id==='chart-layout'?{}:id==='chart-frame'?frame:null}};
    if(parentMode==='cross-origin')Object.defineProperty(sb.parent,'document',{get(){throw Error('cross-origin');}});
  }
  for(const filename of ['chart_analysis.js','chart_navigation.js'])
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'..',filename),'utf8'),sb);
  if(parentMode==='managed')vm.runInNewContext('parent.document.getElementById("chart-frame").contentWindow=window;',sb);
  vm.runInNewContext(bootstrap,sb);
  vm.runInNewContext('this.menu='+menuObject,sb);
  function installCharts(symbols) {
    sb.__cm={};
    const periods=Array.from(sb.__chanlunUrlBootstrap.intervals);
    sb.chart_widgets=periods.map((interval,index)=>{
      const identity={symbol:symbols[index]||symbols[0],interval};
      const id=String(index+1);
      sb.__cm[id]={id,instanceId:'pane-'+id,getCurrentChartIdentity:()=>identity,getChartData:()=>null};
      return {activeChart:()=>({resolution:()=>identity.interval})};
    });
    sb.ChartAnalysis.init();sb.ChartAnalysis.refresh();
  }
  function followNavigation() {
    const target=new URL(navigations.at(-1),'http://local');
    sb.location.search=target.search;sb.location.pathname=target.pathname;sb.location.hash=target.hash;
    vm.runInNewContext(bootstrap,sb);
  }
  return {sb,values,get,opened,messages,navigations,installCharts,followNavigation,
    chooseTarget(id){get('ca-chart-target').value='pane-'+id;get('ca-chart-target').listeners.change({target:get('ca-chart-target')});}};
}

test('a list link can become four charts and open settings for the subsequently selected analysis target',()=>{
  const ui=page();ui.installCharts(['a:SH.600088']);
  assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'single');
  ui.sb.menu.click({id:'four'});
  const target=new URL(ui.navigations[0],'http://local');
  assert.equal(target.searchParams.get('market'),'a');
  assert.equal(target.searchParams.get('code'),'SH.600088');
  assert.equal(target.searchParams.get('workspace_layout'),'1');
  assert.equal(target.searchParams.get('intervals'),'5,1D,30,1');
  ui.followNavigation();assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'four');
  ui.installCharts(['a:SH.600088','a:SZ.000001','a:SH.600519','a:BJ.920030']);
  ui.values.a_code='SZ.000001'; // A different chart was most recently switched.
  ui.chooseTarget('3');ui.sb.menu.click({id:1});
  assert.equal(ui.get('ca-current-symbol').textContent,'a:SH.600519 · 30m');
  assert.equal(ui.opened[0].content,'/get_cl_config/a/SH.600519');
  ui.chooseTarget('2');
  assert.equal(ui.opened[0].content,'/get_cl_config/a/SH.600519','the already opened dialog keeps its captured identity');
  ui.sb.menu.click({id:'single'});ui.followNavigation();
  assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'single');
  assert.equal(ui.sb.__chanlunUrlBootstrap.intervals.length,1);
  const currentUrl=new URL(ui.navigations.at(-1),'http://local');
  assert.equal(currentUrl.searchParams.get('code'),'SZ.000001','layout changes retain the currently selected chart symbol');
  assert.equal(currentUrl.searchParams.get('intervals'),'1D');
});

test('layout URLs retain frozen-evidence parameters and fragment instead of navigating to generic defaults',()=>{
  const ui=page({search:'?market=currency_spot&code=BTC%2FUSDT&frequency=5m&layout=single&screening_source=run-1&screening_point=p-1&chart_sidebar=collapsed'});
  ui.sb.location.hash='#saved-point';ui.installCharts(['currency_spot:BTC/USDT']);
  ui.sb.menu.click({id:'horizontal-2'});
  const target=new URL(ui.navigations[0],'http://local');
  for(const [key,value] of [['market','currency_spot'],['code','BTC/USDT'],['frequency','5m'],
    ['screening_source','run-1'],['screening_point','p-1'],['chart_sidebar','collapsed']])
    assert.equal(target.searchParams.get(key),value);
  assert.equal(target.hash,'#saved-point');
  assert.equal(target.searchParams.get('intervals'),'5,5','a layout choice cannot introduce unsaved evidence periods');
  ui.sb.menu.click({id:1});
  assert.equal(ui.opened[0].content,'/get_cl_config/currency_spot/BTC__USDT');
});

test('only a same-origin parent managing this exact chart iframe owns its layout',()=>{
  const managed=page({parentMode:'managed'});managed.installCharts(['a:SH.600088']);
  assert.equal(managed.sb.menu.data.some(item=>item.id===7),false);
  managed.sb.menu.click({id:'four'});
  assert.equal(managed.navigations.length,0);
  assert.equal(managed.values.chart_layout_type,'single');
  assert.match(managed.messages[0],/外层.*布局/);
  for(const parentMode of ['unrelated','cross-origin']) {
    const ui=page({parentMode});ui.installCharts(['a:SH.600088']);
    assert.equal(ui.sb.menu.data.some(item=>item.id===7),true);
    ui.sb.menu.click({id:'four'});ui.followNavigation();
    assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'four');
  }
});

test('phone policy remains single even if a stale desktop layout action is invoked',()=>{
  const ui=page({phone:true});ui.installCharts(['a:SH.600088']);
  assert.equal(ui.sb.menu.data.some(item=>item.id===7),false);
  ui.sb.menu.click({id:'four'});ui.followNavigation();
  assert.equal(ui.values.chart_layout_type,'single');
  assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'single');
  assert.equal(ui.sb.__chanlunUrlBootstrap.intervals.length,1);
});

test('settings never fall back to a previous global symbol while no selected chart is ready',()=>{
  const ui=page();ui.sb.menu.click({id:1});
  assert.equal(ui.opened.length,0);assert.match(ui.messages[0],/所选图表尚未就绪/);
});

test('current chart periods outrank obsolete link intervals and layout navigation works without preference hooks',()=>{
  const ui=page();ui.installCharts(['a:SH.600088']);
  ui.sb.chart_widgets[0]={activeChart:()=>({resolution:()=> '30'})};
  ui.sb.__cm['1'].getCurrentChartIdentity=()=>({symbol:'a:SH.600088',interval:'30'});
  delete ui.sb.AccountPreferences;
  ui.sb.menu.click({id:'three'});ui.followNavigation();
  assert.deepEqual(Array.from(ui.sb.__chanlunUrlBootstrap.intervals),['30','1D','30']);
  assert.equal(ui.sb.__chanlunUrlBootstrap.layout,'three');
});
