'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../workspace_device.js'),'utf8');
function profile(extra={}){
  const root={innerWidth:1440,location:{origin:'http://local'},...extra};root.window=root;
  vm.runInNewContext(source,root);return root;
}
test('phone portrait and landscape share one profile without reclassifying a narrow desktop iframe',()=>{
  const portrait=profile({innerWidth:393});
  const landscape=profile({innerWidth:852,screen:{width:393,height:852},matchMedia:()=>({matches:true})});
  assert.equal(portrait.WorkspaceDevice.isPhone,true);assert.equal(landscape.WorkspaceDevice.isPhone,true);
  const parent=profile();const embedded=profile({innerWidth:360,top:parent});
  assert.equal(embedded.WorkspaceDevice.isPhone,false);
  assert.equal(embedded.WorkspaceDevice.layout('four'),'four');
  assert.equal(landscape.WorkspaceDevice.layout('four'),'single');
  portrait.innerWidth=852;vm.runInNewContext(source,portrait);assert.equal(portrait.WorkspaceDevice.isPhone,true);
});
test('phone scopes only view preferences and native layouts, keeping structure preferences shared',()=>{
  const {WorkspaceDevice:device}=profile({innerWidth:393});
  assert.equal(device.preferenceKey('tv_chart'),'phone_tv_chart');
  assert.equal(device.preferenceKey('cl_show_config_1_5'),'cl_show_config_1_5');
  assert.equal(device.preferenceKey('cl_independent_drawings_1'),'cl_independent_drawings_1');
  assert.equal(device.layoutClient('chanlun_pro_us_1'),'chanlun_pro_us_1_phone');
});
