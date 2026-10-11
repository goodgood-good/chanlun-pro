'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');

function page(){
  const elements=new Map(),requests=[],notifications=[];
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',props:{},events:{},text:'',children:[],
    replaceChildren(){this.children=[];},appendChild(child){this.children.push(child);},click(){this.events.click?.();}});return elements.get(id);};
  function $(selector){
    if(typeof selector==='function'){selector();return;}
    const nodes=String(selector).split(',').map(id=>get(id.trim().replace(/^#/,''))),item=nodes[0];
    const chain={on(name,arg,handler){item.events[name]=handler||arg;return chain;},val(value){if(!arguments.length)return item.value;nodes.forEach(node=>node.value=value);return chain;},
      text(value){nodes.forEach(node=>node.text=value);return chain;},prop(name,value){nodes.forEach(node=>node.props[name]=value);return chain;},
      toggleClass(){return chain;},data(){return '';}};return chain;
  }
  get('export_zx_group').value='original-group';get('zixuan_import_market').value='hk';
  const sandbox={$,FormData:class {constructor(){this.values=new Map();}append(key,value){this.values.set(key,value);}},
    document:{getElementById:get,createElement(){return {textContent:''};}},
    AppRequest:{ajax(options){requests.push(options);}},
    layui:{use(_names,callback){callback();},form:{render(){}},layer:{msg(){},confirm(){},close(){}}},
    window:{parent:{ZiXuan:{load_groups(group){notifications.push(group);},render_zixuan_opts(){}}},location:{reload(){}},open(){}},
    encodeURIComponent};
  const template=fs.readFileSync(path.join(__dirname,'../../../templates/zixuan.html'),'utf8');
  const start=template.indexOf('$(function () {');
  const script=template.slice(start,template.indexOf('</script>',start)).replace('{{ market | tojson }}','"a"');
  vm.runInNewContext(script,sandbox);
  return {get,requests,notifications};
}

test('an import pins its group and legacy market, blocks duplicates, and shows skipped reasons as text',()=>{
  const ui=page(),file={name:'fixture.csv'};
  const input=ui.get('zixuan_import_file');input.files=[file];input.events.change.call(input);
  assert.equal(ui.requests.length,1);
  assert.equal(ui.requests[0].data.values.get('zx_group'),'original-group');
  assert.equal(ui.requests[0].data.values.get('market'),'hk');
  assert.equal(ui.get('export_zx_group').props.disabled,true);
  input.events.change.call(input);assert.equal(ui.requests.length,1);
  ui.get('export_zx_group').value='changed-later';
  ui.requests[0].success({ok:true,msg:'新增 1，跳过 1',issues:[{line:3,market:'us',code:'<bad>',reason:'代码无效'}]});
  assert.match(ui.get('zixuan_import_status').text,/original-group.*跳过 1/);
  assert.equal(ui.get('zixuan_import_issues').children[0].textContent,'第 3 行 · us <bad>：代码无效');
  assert.deepEqual(ui.notifications,['original-group']);
  ui.requests[0].complete();
  assert.equal(ui.get('export_zx_group').props.disabled,false);
  assert.equal(input.value,'');
});

test('validation errors retain the server explanation and restore the import controls',()=>{
  const ui=page(),input=ui.get('zixuan_import_file');input.files=[{}];input.events.change.call(input);
  ui.requests[0].error({responseJSON:{msg:'CSV 格式错误，请检查引号与列分隔'}});
  ui.requests[0].complete();
  assert.match(ui.get('zixuan_import_status').text,/CSV 格式错误/);
  assert.equal(ui.get('zixuan_opt_import_file').props.disabled,false);
  assert.deepEqual(ui.notifications,[]);
});
