/* Resize the notification list without reloading its chart or selected record. */
(function () {
  'use strict';
  const columns=document.getElementById('notification-columns');
  const handle=document.getElementById('notification-list-resizer');
  if(!columns||!handle)return;
  const defaultWidth=320,minWidth=220,minDetailWidth=400;
  const account=window.__CHANLUN_ACCOUNT_PREFERENCES__?.account_key||'local';
  const storageKey='chanlun.monitor.list-width.'+account;
  let preferred=defaultWidth,active=null,frame=0,lastContainerWidth=0;
  try{
    const saved=Number(localStorage.getItem(storageKey));
    if(Number.isFinite(saved)&&saved>=minWidth&&saved<=10000)preferred=saved;
  }catch(_error){/* Dragging still works when browser storage is unavailable. */}
  const desktop=()=>window.matchMedia('(min-width:761px)').matches;
  const maximum=()=>Math.max(minWidth,columns.clientWidth-handle.getBoundingClientRect().width-minDetailWidth);
  function apply(){
    if(!desktop()||!columns.clientWidth)return;
    const max=maximum(),width=Math.round(Math.min(max,Math.max(minWidth,preferred)));
    columns.style.setProperty('--notification-list-width',width+'px');
    handle.setAttribute('aria-valuemin',String(minWidth));handle.setAttribute('aria-valuemax',String(Math.floor(max)));
    handle.setAttribute('aria-valuenow',String(width));handle.setAttribute('aria-valuetext',width+' 像素');
  }
  function persist(){try{localStorage.setItem(storageKey,String(preferred));}catch(_error){}}
  function renderDrag(){
    frame=0;if(!active)return;
    preferred=Math.min(maximum(),Math.max(minWidth,active.startWidth+active.position-active.startX));
    apply();
  }
  function finish(commit,event){
    if(!active||event&&event.pointerId!==active.pointerId)return;
    if(frame){cancelAnimationFrame(frame);frame=0;}
    if(commit){if(Number.isFinite(event?.clientX))active.position=event.clientX;renderDrag();}
    const drag=active;active=null;
    if(commit)persist();else{preferred=drag.previous;apply();}
    columns.classList.remove('is-resizing');document.body.classList.remove('notification-resizing');drag.shield.remove();
    try{if(handle.hasPointerCapture(drag.pointerId))handle.releasePointerCapture(drag.pointerId);}catch(_error){}
  }
  handle.addEventListener('pointerdown',event=>{
    if(active||event.button!==0||event.isPrimary===false||!desktop())return;
    event.preventDefault();apply();
    const shield=document.createElement('div');shield.className='notification-resize-shield';
    shield.setAttribute('aria-hidden','true');document.body.append(shield);
    active={pointerId:event.pointerId,startX:event.clientX,position:event.clientX,
      startWidth:Number(handle.getAttribute('aria-valuenow')),previous:preferred,shield};
    columns.classList.add('is-resizing');document.body.classList.add('notification-resizing');
    handle.focus({preventScroll:true});
    try{handle.setPointerCapture(event.pointerId);}catch(_error){}
  });
  window.addEventListener('pointermove',event=>{
    if(!active||event.pointerId!==active.pointerId)return;
    event.preventDefault();active.position=event.clientX;
    if(!frame)frame=requestAnimationFrame(renderDrag);
  },{passive:false});
  window.addEventListener('pointerup',event=>finish(true,event));
  window.addEventListener('pointercancel',event=>finish(false,event));
  handle.addEventListener('lostpointercapture',event=>finish(false,event));
  window.addEventListener('blur',()=>finish(false));
  window.addEventListener('pagehide',()=>finish(false));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)finish(false);});
  window.addEventListener('keydown',event=>{if(active&&event.key==='Escape'){event.preventDefault();finish(false);}});
  function setWidth(width){preferred=Math.min(maximum(),Math.max(minWidth,width));apply();persist();}
  handle.addEventListener('dblclick',event=>{event.preventDefault();finish(false);setWidth(defaultWidth);});
  handle.addEventListener('keydown',event=>{
    if(active||event.altKey||event.ctrlKey||event.metaKey||!desktop())return;
    const current=Number(handle.getAttribute('aria-valuenow')),step=event.shiftKey?48:16;
    const width={ArrowLeft:current-step,ArrowRight:current+step,Home:minWidth,End:maximum(),Enter:defaultWidth}[event.key];
    if(width===undefined)return;
    event.preventDefault();setWidth(width);
  });
  function resize(){finish(false);apply();}
  window.addEventListener('resize',resize);
  if(window.ResizeObserver)new ResizeObserver(()=>{
    const width=columns.clientWidth;
    if(width!==lastContainerWidth){lastContainerWidth=width;resize();}
  }).observe(columns);
  apply();
})();
