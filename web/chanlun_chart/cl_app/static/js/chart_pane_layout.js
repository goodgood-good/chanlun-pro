(function(root, factory) {
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.ChartPaneLayout=api;
})(typeof window!=='undefined'?window:globalThis, function() {
  'use strict';
  const DEFAULTS={
    single:{},'horizontal-2':{rows:0.5},'horizontal-7-3':{rows:0.7},
    'vertical-2':{columns:0.5},three:{rows:0.5,top:0.5},four:{rows:0.5,top:0.5,bottom:0.5}
  };
  const own=(object,key)=>Object.prototype.hasOwnProperty.call(object,key);
  function defaultRatios(layout){return own(DEFAULTS,layout)?{...DEFAULTS[layout]}:{};}
  function normalizeRatios(value){
    const result={};
    if(!value||typeof value!=='object'||Array.isArray(value))return result;
    for(const [layout,defaults] of Object.entries(DEFAULTS)){
      const raw=value[layout];
      if(!raw||typeof raw!=='object'||Array.isArray(raw))continue;
      const ratios={};
      for(const key of Object.keys(defaults)){
        const ratio=raw[key];
        if(typeof ratio==='number'&&Number.isFinite(ratio)&&ratio>=0.1&&ratio<=0.9)ratios[key]=ratio;
      }
      if(Object.keys(ratios).length)result[layout]=ratios;
    }
    return result;
  }
  function clampRatio(value, available, minPixels){
    const minimum=Number.isFinite(available)&&available>0&&Number.isFinite(minPixels)&&minPixels>0
      ?Math.min(0.5,Math.max(0.1,minPixels/available)):0.1;
    return Math.min(1-minimum,Math.max(minimum,Number.isFinite(value)?value:0.5));
  }
  function create(container, requestedLayout, options={}){
    const doc=container.ownerDocument,view=doc.defaultView;
    const layout=own(DEFAULTS,requestedLayout)?requestedLayout:'single';
    container.__chartPaneLayout?.destroy();
    const read=()=>{try{return normalizeRatios(options.read?.());}catch(_){return {};}};
    const ratios={...defaultRatios(layout),...read()[layout]};
    const panes=[],splits=[],listeners=[];
    let active=null,dragFrame=0,resizeFrame=0,observer=null,destroyed=false;
    const raf=callback=>view.requestAnimationFrame(callback);
    const cancel=id=>{if(id)view.cancelAnimationFrame(id);};
    const listen=(target,type,handler,settings)=>{
      target.addEventListener(type,handler,settings);
      listeners.push(()=>target.removeEventListener(type,handler,settings));
    };
    const notify=()=>{try{options.onResize?.();}catch(_){/* autosize remains active */}};
    const persist=key=>{
      const values=read();values[layout]={...values[layout],[key]:ratios[key]};
      try{options.write?.(values);}catch(_){/* resizing works with unavailable storage */}
    };
    const dimension=split=>{
      const rect=split.node.getBoundingClientRect(),handle=split.handle.getBoundingClientRect();
      return Math.max(0,split.axis==='x'?rect.width-handle.width:rect.height-handle.height);
    };
    const minimum=split=>split.axis==='x'?200:140;
    function apply(split){
      const available=dimension(split),ratio=clampRatio(ratios[split.key],available,minimum(split));
      split.applied=ratio;
      split.first.style.flex=`${ratio} 1 0px`;
      split.second.style.flex=`${1-ratio} 1 0px`;
      split.handle.setAttribute('aria-valuenow',String(Math.round(ratio*100)));
      split.handle.setAttribute('aria-valuemin',String(Math.round(clampRatio(0,available,minimum(split))*100)));
      split.handle.setAttribute('aria-valuemax',String(Math.round(clampRatio(1,available,minimum(split))*100)));
      split.handle.setAttribute('aria-valuetext',`${Math.round(ratio*100)}% / ${Math.round((1-ratio)*100)}%`);
    }
    function resize(){
      if(destroyed)return;
      // Apply outer rows before the nested columns so all measurements are current.
      for(const split of [...splits].reverse())apply(split);
    }
    const coordinate=(event,split)=>split.axis==='x'?event.clientX:event.clientY;
    function renderDrag(){
      dragFrame=0;
      if(!active||!Number.isFinite(active.position))return;
      const delta=active.position-active.startPosition;
      if(Math.abs(delta)<1&&!active.changed)return;
      ratios[active.split.key]=clampRatio(active.startApplied+delta/active.available,active.available,minimum(active.split));
      active.changed=ratios[active.split.key]!==active.startRatio;
      apply(active.split);
    }
    function finish(commit,event){
      if(!active||event&&event.pointerId!==active.pointerId)return;
      if(commit){
        const finalPosition=event&&coordinate(event,active.split);
        if(Number.isFinite(finalPosition))active.position=finalPosition;
        cancel(dragFrame);renderDrag();
      } else {cancel(dragFrame);dragFrame=0;}
      const drag=active;active=null;
      if(!commit){ratios[drag.split.key]=drag.startRatio;apply(drag.split);}
      drag.split.handle.classList.remove('is-dragging');
      doc.body.classList.remove('chart-pane-dragging','chart-pane-dragging-x','chart-pane-dragging-y');
      drag.shield.remove();
      try{if(drag.split.handle.hasPointerCapture?.(drag.pointerId))drag.split.handle.releasePointerCapture(drag.pointerId);}catch(_){}
      if(commit&&drag.changed)persist(drag.split.key);
      if(drag.changed)notify();
    }
    function begin(event,split){
      if(destroyed||active||event.button!==0||event.isPrimary===false)return;
      const available=dimension(split),startPosition=coordinate(event,split);
      if(available<=0||!Number.isFinite(startPosition))return;
      event.preventDefault();
      apply(split);
      const shield=doc.createElement('div');shield.className='cp-pane-drag-shield';shield.dataset.axis=split.axis;
      shield.setAttribute('aria-hidden','true');doc.body.appendChild(shield);
      active={split,shield,available,pointerId:event.pointerId,startPosition,position:startPosition,
        startRatio:ratios[split.key],startApplied:split.applied,changed:false};
      doc.body.classList.add('chart-pane-dragging',`chart-pane-dragging-${split.axis}`);
      split.handle.classList.add('is-dragging');
      try{split.handle.focus({preventScroll:true});split.handle.setPointerCapture(event.pointerId);}catch(_){}
    }
    function setRatio(split,value){
      if(value===ratios[split.key])return;
      ratios[split.key]=value;apply(split);persist(split.key);notify();
    }
    function splitNode(axis,key,first,second){
      const node=doc.createElement('div'),handle=doc.createElement('div');
      node.className=`cp-pane-split cp-pane-split-${axis}`;
      handle.className='cp-pane-handle';handle.dataset.axis=axis;handle.dataset.splitKey=key;handle.tabIndex=0;
      const label=key==='top'?'调整上排左右图表大小':key==='bottom'?'调整下排左右图表大小':axis==='x'?'调整左右图表大小':'调整上下图表大小';
      handle.title=`拖动${label}，双击恢复默认`;
      handle.setAttribute('role','separator');handle.setAttribute('aria-label',label);
      handle.setAttribute('aria-orientation',axis==='x'?'vertical':'horizontal');
      handle.setAttribute('aria-controls',[...first.querySelectorAll('.cp-chart-pane'),...second.querySelectorAll('.cp-chart-pane'),
        ...(first.classList.contains('cp-chart-pane')?[first]:[]),...(second.classList.contains('cp-chart-pane')?[second]:[])].map(pane=>pane.id).join(' '));
      const split={node,handle,axis,key,first,second,applied:ratios[key]};splits.push(split);
      first.style.flex=`${ratios[key]} 1 0px`;second.style.flex=`${1-ratios[key]} 1 0px`;
      node.append(first,handle,second);
      listen(handle,'pointerdown',event=>begin(event,split));
      listen(handle,'lostpointercapture',event=>finish(false,event));
      listen(handle,'dblclick',event=>{event.preventDefault();setRatio(split,DEFAULTS[layout][key]);});
      listen(handle,'keydown',event=>{
        if(active||event.altKey||event.ctrlKey||event.metaKey)return;
        const delta=event.shiftKey?0.1:0.02;
        const negative=axis==='x'?'ArrowLeft':'ArrowUp',positive=axis==='x'?'ArrowRight':'ArrowDown';
        let value;
        if(event.key===negative)value=split.applied-delta;
        else if(event.key===positive)value=split.applied+delta;
        else if(event.key==='Home')value=0;
        else if(event.key==='End')value=1;
        else if(event.key==='Enter'){event.preventDefault();setRatio(split,DEFAULTS[layout][key]);return;}
        else return;
        event.preventDefault();setRatio(split,clampRatio(value,dimension(split),minimum(split)));
      });
      return node;
    }
    function pane(id){
      const node=doc.createElement('div');node.id=`tv_chart_container_${id}`;node.className='cp-chart-pane';
      panes.push(node);return node;
    }
    container.replaceChildren();container.classList.add('cp-pane-layout');
    container.style.display='flex';container.style.flexDirection='column';container.style.flexWrap='nowrap';
    let content;
    if(layout==='vertical-2')content=splitNode('x','columns',pane('1'),pane('2'));
    else if(layout==='horizontal-2'||layout==='horizontal-7-3')content=splitNode('y','rows',pane('1'),pane('2'));
    else if(layout==='three')content=splitNode('y','rows',splitNode('x','top',pane('1'),pane('2')),pane('3'));
    else if(layout==='four')content=splitNode('y','rows',splitNode('x','top',pane('1'),pane('2')),splitNode('x','bottom',pane('3'),pane('4')));
    else content=pane('1');
    content.style.flex='1 1 0px';container.appendChild(content);
    const scheduleResize=()=>{
      if(active)finish(false);
      if(!destroyed&&!resizeFrame)resizeFrame=raf(()=>{resizeFrame=0;resize();});
    };
    if(splits.length){
      listen(view,'pointermove',event=>{
        if(!active||event.pointerId!==active.pointerId)return;
        event.preventDefault();active.position=coordinate(event,active.split);
        if(!dragFrame)dragFrame=raf(renderDrag);
      },{passive:false});
      listen(view,'pointerup',event=>finish(true,event));
      listen(view,'pointercancel',event=>finish(false,event));
      listen(view,'blur',()=>finish(false));
      listen(doc,'visibilitychange',()=>{if(doc.hidden)finish(false);});
      listen(view,'keydown',event=>{if(active&&event.key==='Escape'){event.preventDefault();finish(false);}});
      listen(view,'resize',scheduleResize);
      if(view.ResizeObserver){observer=new view.ResizeObserver(scheduleResize);observer.observe(container);}
    }
    const controller={layout,ids:panes.map(node=>node.id.replace('tv_chart_container_','')),panes,resize,
      destroy(){
        if(destroyed)return;
        finish(false);destroyed=true;cancel(dragFrame);cancel(resizeFrame);observer?.disconnect();
        for(const remove of listeners)remove();
        if(container.__chartPaneLayout===controller)delete container.__chartPaneLayout;
      }};
    container.__chartPaneLayout=controller;resize();
    return controller;
  }
  return {defaultRatios,normalizeRatios,clampRatio,create};
});
