'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');

function page(){
  const elements=new Map(),forms={},requests=[],tables=[],errors=[],renders=[];
  const get=key=>{if(!elements.has(key))elements.set(key,{value:key==='#market_select'?'a':'',text:'',props:{},events:{}});return elements.get(key);};
  function $(key){
    if(typeof key==='function'){key();return;}
    const item=get(key),chain={
      val(value){if(arguments.length){item.value=value;return chain;}return item.value;},
      text(value){item.text=value;return chain;},prop(name,value){item.props[name]=value;return chain;},
      click(handler){item.events.click=handler;return chain;},keydown(handler){item.events.keydown=handler;return chain;},
      trigger(name){item.events[name]?.();return chain;}
    };return chain;
  }
  function apply(options){
    const instance={options,disposed:false,rows:[]};tables.push(instance);
    instance.load=()=>options.ajax({data:{...options.where,page:1,page_size:50},success(response){
      if(instance.disposed){errors.push('disposed callback');throw new Error('disposed callback');}
      instance.rows=response.items;options.done(response);
    }});
    instance.load();
  }
  const table={render(options){renders.push('render');apply(options);},reload(id,options){
    assert.equal(id,'symbols_table');renders.push('reload');apply(options);
  }};
  const sandbox={$,layui:{form:{on(event,callback){forms[event]=callback;}},table,layer:{},use(_names,callback){callback();}},
    document:{querySelector(){return null;},createElement(){return {setAttribute(){},get outerHTML(){return this.href;}};}},
    AppRequest:{ajax(options){const request={options,aborted:false,abort(){this.aborted=true;options.error({status:0},'abort');options.complete();}};requests.push(request);return request;}},
    encodeURIComponent,console};sandbox.window=sandbox;
  const template=fs.readFileSync(path.join(__dirname,'../../../templates/symbols.html'),'utf8');
  const start=template.indexOf('$(function () {');
  vm.runInNewContext(template.slice(start,template.indexOf('</script>',start)),sandbox);
  const finish=(index,body)=>{requests[index].options.success(body);requests[index].options.complete();};
  return {get,forms,requests,tables,errors,finish,renders};
}

test('market switching aborts the old request and ignores its late response before vendor callbacks',()=>{
  const ui=page();
  ui.forms['select(market_select)']({value:'us'});
  assert.equal(ui.requests[0].aborted,true);
  ui.finish(1,{ok:true,market:'us',total:1,items:[{code:'AAPL.US'}]});
  ui.finish(0,{ok:true,market:'a',total:999,items:[{code:'SH.600519'}]});
  assert.equal(ui.get('#header_total_tip').text,'1 条');
  assert.equal(ui.tables[1].rows[0].code,'AAPL.US');
  assert.deepEqual(ui.errors,[]);
  assert.deepEqual(ui.renders,['render','reload']);
  assert.match(ui.tables[1].options.cols[0][0].templet({code:'AAPL.US'}),/market=us&code=AAPL.US/);
  assert.match(ui.tables[0].options.cols[0][0].templet({code:'SH.600519'}),/market=a&code=SH.600519/);
});

test('not-ready and errors remain visibly distinct from a successfully empty search, with manual retry',()=>{
  const ui=page();
  ui.finish(0,{ok:false,status:'not_ready',market:'a',total:null,msg:'目录加载中'});
  assert.equal(ui.get('#header_total_tip').text,'—');
  assert.equal(ui.get('#total_tip').text,'目录未就绪');
  assert.equal(ui.get('#symbols_retry_wrap').props.hidden,false);
  ui.get('#symbols_retry').events.click();
  ui.requests[1].options.error({status:503,responseJSON:{msg:'目录读取失败'}},'error');
  assert.equal(ui.get('#total_tip').text,'读取失败');
  ui.get('#symbols_retry').events.click();
  ui.finish(2,{ok:true,market:'a',total:0,items:[]});
  assert.equal(ui.get('#header_total_tip').text,'0 条');
  assert.match(ui.get('#symbols_load_status').text,/没有匹配/);
  assert.equal(ui.get('#symbols_retry_wrap').props.hidden,true);
});

test('rapid searches and a stale pagination response cannot overwrite the latest query',()=>{
  const ui=page();
  ui.get('#search_input').value='new query';ui.get('#btn_search').events.click();
  assert.equal(ui.requests[0].aborted,true);
  assert.equal(ui.tables[1].options.where.q,'new query');
  ui.tables[1].load();
  assert.equal(ui.requests[1].aborted,true);
  ui.finish(2,{ok:true,market:'a',total:2,items:[{code:'new'}]});
  ui.finish(1,{ok:true,market:'a',total:20,items:[{code:'old'}]});
  assert.equal(ui.get('#header_total_tip').text,'2 条');
  assert.equal(ui.tables[1].rows[0].code,'new');
  assert.equal(ui.requests[2].options.timeout,10000);
  assert.equal(ui.requests[2].options.cache,false);
});
