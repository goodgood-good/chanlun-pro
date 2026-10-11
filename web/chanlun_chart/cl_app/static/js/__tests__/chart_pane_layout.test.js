'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const modulePath=path.join(__dirname,'../chart_pane_layout.js');
const {normalizeRatios,defaultRatios,clampRatio}=require(modulePath);

test('layout defaults describe all six layouts and return independent objects',()=>{
  const expected={single:{},'horizontal-2':{rows:.5},'horizontal-7-3':{rows:.7},'vertical-2':{columns:.5},
    three:{rows:.5,top:.5},four:{rows:.5,top:.5,bottom:.5}};
  for(const [layout,ratios] of Object.entries(expected)){
    assert.deepEqual(defaultRatios(layout),ratios);const copy=defaultRatios(layout);copy.rows=.123;
    assert.deepEqual(defaultRatios(layout),ratios);
  }
  assert.deepEqual(defaultRatios('unknown'),{});
});

test('stored ratios retain only known layouts splits and finite numbers in the allowed range',()=>{
  const input={'four':{rows:.7,top:.1,bottom:.9,unknown:.5},'three':{rows:.2,top:.8,bottom:.4},
    'vertical-2':{columns:'0.4'},'horizontal-2':{rows:true},'horizontal-7-3':{rows:Infinity},single:{rows:.3},unknown:{rows:.5}};
  const before=JSON.stringify(input);
  assert.deepEqual(normalizeRatios(input),{four:{rows:.7,top:.1,bottom:.9},three:{rows:.2,top:.8}});
  assert.equal(JSON.stringify(input),before);
  for(const value of [null,undefined,[],42,'{}'])assert.deepEqual(normalizeRatios(value),{});
  for(const value of [NaN,Infinity,-Infinity,-.01,0,.099,.901,1,2,'0.5',false,null])
    assert.deepEqual(normalizeRatios({'horizontal-2':{rows:value}}),{});
});

test('normalization detaches persisted objects so edits cannot mutate another layout or caller',()=>{
  const input={four:{rows:.6,top:.7,bottom:.3},three:{rows:.4,top:.2}},output=normalizeRatios(input);
  output.four.top=.9;output.three.rows=.8;
  assert.deepEqual(input,{four:{rows:.6,top:.7,bottom:.3},three:{rows:.4,top:.2}});
});

test('ratio bounds protect minimum pixel sizes and converge to halves when there is no room',()=>{
  assert.equal(clampRatio(.01,1000,200),.2);assert.equal(clampRatio(.99,1000,200),.8);
  assert.equal(clampRatio(.4,1000,200),.4);assert.equal(clampRatio(.8,1000,140),.8);
  assert.equal(clampRatio(.01,4000,200),.1);assert.equal(clampRatio(.99,4000,200),.9);
  assert.equal(clampRatio(.1,300,200),.5);assert.equal(clampRatio(.9,200,140),.5);
});

test('chart template initializes widgets through the layout controller without changing navigation extraction',()=>{
  const template=fs.readFileSync(path.join(__dirname,'../../../templates/index.html'),'utf8');
  assert.match(template,/chart_pane_layout\.js/);assert.match(template,/ChartPaneLayout\.create\(/);
  assert.match(template,/Charts\.show_tv_chart\(/);assert.match(template,/chart_pane_ratios/);
  assert.ok(template.match(/\(function applyUrlSymbolParams\(\) \{[\s\S]*?\n    \}\)\(\);/));
  assert.ok(template.match(/dropdown\.render\((\{[\s\S]*?\n        \})\);/));
});

function fixture(layout='four',stored={}){
  class Target{
    constructor(){this.listeners=new Map();}
    addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
    removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
    fire(type,values={}){const event={preventDefault(){this.prevented=true;},...values};for(const fn of [...this.listeners.get(type)||[]])fn(event);return event;}
  }
  class Node extends Target{
    constructor(doc){super();this.ownerDocument=doc;this.children=[];this.className='';this.style={};this.dataset={};this.attrs={};this.captured=new Set();
      this.classList={contains:name=>this.className.split(/\s+/).includes(name),
        add:(...names)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...names])].join(' ');},
        remove:(...names)=>{this.className=this.className.split(/\s+/).filter(name=>!names.includes(name)).join(' ');}};}
    append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node);}}
    appendChild(node){this.append(node);return node;}
    replaceChildren(...nodes){this.replacements=(this.replacements||0)+1;for(const node of this.children)node.parentElement=null;this.children=[];this.append(...nodes);}
    remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(node=>node!==this);this.parentElement=null;}}
    setAttribute(name,value){this.attrs[name]=String(value);}
    getAttribute(name){return this.attrs[name]??null;}
    querySelectorAll(selector){return this.children.flatMap(node=>[...(node.classList.contains(selector.slice(1))?[node]:[]),...node.querySelectorAll(selector)]);}
    focus(){this.ownerDocument.activeElement=this;}
    setPointerCapture(id){this.captured.add(id);}
    hasPointerCapture(id){return this.captured.has(id);}
    releasePointerCapture(id){this.captured.delete(id);this.fire('lostpointercapture',{pointerId:id});}
    getBoundingClientRect(){
      if(this.rect)return {...this.rect};
      const parent=this.parentElement;if(!parent)return {left:0,top:0,width:1200,height:800};
      const rect=parent.getBoundingClientRect(),axis=parent.classList.contains('cp-pane-split-x')?'x':parent.classList.contains('cp-pane-split-y')?'y':null;
      if(!axis)return {...rect};
      const length=axis==='x'?'width':'height',position=axis==='x'?'left':'top',available=rect[length]-6;
      if(this.classList.contains('cp-pane-handle'))return {...rect,[length]:6,[position]:rect[position]+available*parseFloat(parent.children[0].style.flex)};
      const ratio=parseFloat(this.style.flex)||.5,index=parent.children.indexOf(this);
      return {...rect,[length]:available*ratio,[position]:rect[position]+(index===2?available*parseFloat(parent.children[0].style.flex)+6:0)};
    }
  }
  const view=new Target(),doc=new Target(),frames=new Map(),observers=[];let next=1,reads=0,writes=[],notifications=0;
  doc.defaultView=view;doc.createElement=()=>new Node(doc);doc.body=new Node(doc);
  view.requestAnimationFrame=fn=>{const id=next++;frames.set(id,fn);return id;};view.cancelAnimationFrame=id=>frames.delete(id);
  view.ResizeObserver=class{constructor(fn){this.fn=fn;observers.push(this);}observe(node){this.node=node;}disconnect(){this.disconnected=true;}};
  const container=new Node(doc);container.rect={left:20,top:30,width:1200,height:800};doc.body.append(container);
  const sb={window:view,globalThis:view};vm.runInNewContext(fs.readFileSync(modulePath,'utf8'),sb);
  const api=view.ChartPaneLayout,options={read(){reads++;if(stored instanceof Error)throw stored;return stored;},
    write(value){writes.push(JSON.parse(JSON.stringify(value)));stored=value;},onResize(){notifications++;}};
  const controller=api.create(container,layout,options);
  const handle=key=>container.querySelectorAll('.cp-pane-handle').find(node=>node.dataset.splitKey===key);
  const flush=()=>{for(const [id,fn] of [...frames]){frames.delete(id);fn();}};
  const begin=(key='top',id=7)=>{const h=handle(key),r=h.getBoundingClientRect();h.fire('pointerdown',{button:0,pointerId:id,isPrimary:true,clientX:r.left+3,clientY:r.top+3});return {handle:h,x:r.left+3,y:r.top+3,id};};
  const move=(drag,delta)=>{view.fire('pointermove',{pointerId:drag.id,clientX:drag.x+(drag.handle.dataset.axis==='x'?delta:0),clientY:drag.y+(drag.handle.dataset.axis==='y'?delta:0)});};
  return {api,controller,container,doc,view,handle,begin,move,flush,frames,observers,options,
    writes:()=>writes,reads:()=>reads,notifications:()=>notifications,setStored:value=>{stored=value;},
    finish:drag=>view.fire('pointerup',{pointerId:drag.id})};
}

for(const [layout,count,keys] of [['single',1,[]],['vertical-2',2,['columns']],['horizontal-2',2,['rows']],
  ['horizontal-7-3',2,['rows']],['three',3,['top','rows']],['four',4,['top','bottom','rows']]])
test(layout+' creates stable pane IDs and accessible split handles once',()=>{
  const f=fixture(layout);assert.equal(f.container.replacements,1);assert.deepEqual(Array.from(f.controller.ids),Array.from({length:count},(_,i)=>String(i+1)));
  assert.deepEqual(new Set(f.container.querySelectorAll('.cp-pane-handle').map(node=>node.dataset.splitKey)),new Set(keys));
  for(const key of keys){const h=f.handle(key);assert.equal(h.getAttribute('role'),'separator');assert.equal(h.tabIndex,0);
    assert.equal(h.getAttribute('aria-orientation'),h.dataset.axis==='x'?'vertical':'horizontal');assert.match(h.getAttribute('aria-controls'),/tv_chart_container_/);}
  assert.equal(f.writes().length,0);assert.equal(f.notifications(),0);
});

test('pointer moves coalesce through RAF and commit once without replacing pane or iframe identity',()=>{
  const f=fixture(),panes=[...f.controller.panes],iframe=f.doc.createElement('iframe');panes[0].append(iframe);
  const drag=f.begin('top'),initial=f.handle('top').getAttribute('aria-valuenow');
  f.move(drag,20);f.move(drag,80);assert.equal(f.frames.size,1);assert.equal(f.handle('top').getAttribute('aria-valuenow'),initial);
  f.flush();assert.notEqual(f.handle('top').getAttribute('aria-valuenow'),initial);assert.equal(f.writes().length,0);
  f.finish(drag);assert.equal(f.writes().length,1);assert.equal(f.notifications(),1);
  assert.equal(f.container.replacements,1);assert.equal(f.controller.panes[0],panes[0]);assert.equal(panes[0].children[0],iframe);
  assert.equal(f.doc.body.querySelectorAll('.cp-pane-drag-shield').length,0);assert.equal(f.doc.body.classList.contains('chart-pane-dragging'),false);
  assert.equal(drag.handle.hasPointerCapture(drag.id),false);
});

for(const reason of ['pointercancel','lostpointercapture','blur','Escape','visibilitychange'])
test(reason+' cancels a drag restores the previous ratio and removes its overlay without saving',()=>{
  const f=fixture('horizontal-7-3'),h=f.handle('rows'),before=h.getAttribute('aria-valuenow'),drag=f.begin('rows');
  f.move(drag,80);f.flush();assert.notEqual(h.getAttribute('aria-valuenow'),before);
  if(reason==='lostpointercapture')h.fire(reason,{pointerId:drag.id});
  else if(reason==='Escape')f.view.fire('keydown',{key:'Escape'});
  else if(reason==='visibilitychange'){f.doc.hidden=true;f.doc.fire(reason);}
  else f.view.fire(reason,{pointerId:drag.id});
  f.finish(drag);f.flush();assert.equal(h.getAttribute('aria-valuenow'),before);assert.equal(f.writes().length,0);
  assert.equal(f.doc.body.querySelectorAll('.cp-pane-drag-shield').length,0);assert.equal(f.doc.body.classList.contains('chart-pane-dragging'),false);
});

test('wrong pointers and nonprimary input do not hijack an active drag',()=>{
  const f=fixture('vertical-2'),h=f.handle('columns');
  h.fire('pointerdown',{button:2,pointerId:1,clientX:400,clientY:300});assert.equal(f.doc.body.querySelectorAll('.cp-pane-drag-shield').length,0);
  h.fire('pointerdown',{button:0,isPrimary:false,pointerId:1,clientX:400,clientY:300});assert.equal(f.doc.body.querySelectorAll('.cp-pane-drag-shield').length,0);
  const drag=f.begin('columns');f.move({...drag,id:99},100);f.view.fire('pointerup',{pointerId:99});f.flush();assert.equal(f.writes().length,0);
  f.move(drag,100);f.finish(drag);assert.equal(f.writes().length,1);
});

test('keyboard changes only the chosen split and respects axis modifiers and pixel limits',()=>{
  const f=fixture(),h=f.handle('top'),bottom=f.handle('bottom').getAttribute('aria-valuenow');
  h.fire('keydown',{key:'ArrowDown'});h.fire('keydown',{key:'ArrowRight',ctrlKey:true});assert.equal(f.writes().length,0);
  h.fire('keydown',{key:'ArrowRight'});assert.equal(h.getAttribute('aria-valuenow'),'52');
  h.fire('keydown',{key:'ArrowLeft',shiftKey:true});assert.equal(h.getAttribute('aria-valuenow'),'42');
  h.fire('keydown',{key:'Home'});assert.equal(h.getAttribute('aria-valuenow'),h.getAttribute('aria-valuemin'));
  h.fire('keydown',{key:'End'});assert.equal(h.getAttribute('aria-valuenow'),h.getAttribute('aria-valuemax'));
  assert.equal(f.handle('bottom').getAttribute('aria-valuenow'),bottom);assert.equal(f.writes().length,4);
  h.fire('keydown',{key:'Enter'});assert.equal(h.getAttribute('aria-valuenow'),'50');assert.equal(f.writes().length,5);
  h.fire('dblclick');assert.equal(f.writes().length,5,'resetting an already default ratio is a no-op');
});

test('doubleclick restores the specific 70/30 default and preserves freshly read other layouts',()=>{
  const f=fixture('horizontal-7-3',{'horizontal-7-3':{rows:.4},four:{top:.2}});
  f.setStored({four:{top:.8,bottom:.3},three:{rows:.6}});f.handle('rows').fire('dblclick');
  assert.equal(f.handle('rows').getAttribute('aria-valuenow'),'70');
  assert.deepEqual(f.writes()[0],{four:{top:.8,bottom:.3},three:{rows:.6},'horizontal-7-3':{rows:.7}});
});

test('editing one split preserves newer sibling splits saved by another window of the same layout',()=>{
  const f=fixture('four',{four:{rows:.5,top:.5,bottom:.5}});
  f.setStored({four:{rows:.7,top:.4,bottom:.3},three:{rows:.6}});
  f.handle('top').fire('keydown',{key:'ArrowRight'});
  assert.deepEqual(f.writes()[0],{four:{rows:.7,top:.52,bottom:.3},three:{rows:.6}});
});

test('viewport and observer clamps do not save or discard the original large-window intent',()=>{
  const f=fixture('vertical-2',{'vertical-2':{columns:.8}}),h=f.handle('columns');
  assert.equal(h.getAttribute('aria-valuenow'),'80');f.container.rect.width=300;
  f.view.fire('resize');f.flush();assert.equal(h.getAttribute('aria-valuenow'),'50');assert.equal(f.writes().length,0);
  f.container.rect.width=1200;f.observers[0].fn();f.flush();assert.equal(h.getAttribute('aria-valuenow'),'80');assert.equal(f.writes().length,0);
});

test('destroy cancels active work and a replacement controller does not retain obsolete listeners',()=>{
  const f=fixture(),drag=f.begin('bottom');f.move(drag,100);
  f.controller.destroy();f.flush();assert.equal(f.writes().length,0);assert.equal(f.doc.body.querySelectorAll('.cp-pane-drag-shield').length,0);
  assert.equal(f.observers[0].disconnected,true);f.view.fire('pointerup',{pointerId:drag.id});assert.equal(f.writes().length,0);
  const replacement=f.api.create(f.container,'single',f.options);assert.deepEqual(Array.from(replacement.ids),['1']);
  assert.equal(f.container.querySelectorAll('.cp-pane-handle').length,0);assert.equal(f.container.__chartPaneLayout,replacement);
});

test('unavailable storage and resize callbacks cannot disable layout interaction',()=>{
  const f=fixture('vertical-2',new Error('private storage'));
  f.options.write=()=>{throw Error('quota');};f.options.onResize=()=>{throw Error('callback unavailable');};
  assert.doesNotThrow(()=>f.handle('columns').fire('keydown',{key:'ArrowRight'}));
  assert.equal(f.handle('columns').getAttribute('aria-valuenow'),'52');
});
