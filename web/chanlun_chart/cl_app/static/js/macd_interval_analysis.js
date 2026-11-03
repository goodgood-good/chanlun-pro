(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MacdIntervalAnalysis = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const valid = value => typeof value === 'number' && Number.isFinite(value);
  const seconds = value => valid(value) ? (value >= 1e11 ? value / 1000 : value) : null;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const format = value => !valid(value) ? '—' : (value !== 0 && Math.abs(value) < 1e-7 ? value.toExponential(4) : value.toLocaleString('zh-CN', {maximumFractionDigits: 8}));

  function computeStats(times, hist, startIdx, endIdx, opts = {}) {
    const r = {startIdx, endIdx, rangeCount:0, barCount:0, missingBars:0, posCount:0, negCount:0, zeroCount:0,
      posArea:0, negArea:0, netArea:0, absoluteArea:0, posAreaX2:0, negAreaX2:0, netAreaX2:0,
      histMax:null, histMin:null, histMean:null, histAbsMean:null, difMax:null, difMin:null, difMean:null,
      deaMax:null, deaMin:null, deaMean:null, difCount:0, deaCount:0,
      posMax:0, negMin:0, posMaxTime:null, negMinTime:null, posMaxIdx:-1, negMinIdx:-1,
      posSegments:[], negSegments:[], segmentCount:0, excludedLast:false};
    if (!times || !hist || !Number.isInteger(startIdx) || !Number.isInteger(endIdx) || startIdx < 0 || endIdx < startIdx) return r;
    let end = Math.min(endIdx, times.length - 1), difSum = 0, deaSum = 0, segment = null;
    // This option means the last bar of the whole chart, never a historical
    // interval's endpoint. Single-bar intervals follow the same rule.
    if (opts.excludeLast && end === times.length - 1) { end--; r.excludedLast = true; }
    r.endIdx = end;
    const flush = () => { if (segment) (segment.sign > 0 ? r.posSegments : r.negSegments).push(segment); segment = null; };
    for (let i = startIdx; i <= end; i++) {
      if (!valid(times[i])) { flush(); continue; }
      r.rangeCount++;
      const dif = opts.difArr?.[i], dea = opts.deaArr?.[i], value = hist[i];
      if (valid(dif)) { difSum += dif; r.difCount++; r.difMax = r.difMax === null ? dif : Math.max(r.difMax, dif); r.difMin = r.difMin === null ? dif : Math.min(r.difMin, dif); }
      if (valid(dea)) { deaSum += dea; r.deaCount++; r.deaMax = r.deaMax === null ? dea : Math.max(r.deaMax, dea); r.deaMin = r.deaMin === null ? dea : Math.min(r.deaMin, dea); }
      if (!valid(value)) { r.missingBars++; flush(); continue; }
      r.barCount++;
      r.histMax = r.histMax === null ? value : Math.max(r.histMax, value);
      r.histMin = r.histMin === null ? value : Math.min(r.histMin, value);
      const sign = Math.sign(value);
      if (!segment || segment.sign !== sign) { flush(); if (sign) segment = {startIdx:i, endIdx:i, area:0, peak:value, peakIdx:i, sign}; }
      if (segment) { segment.endIdx=i; segment.area+=Math.abs(value); if (Math.abs(value)>Math.abs(segment.peak)) {segment.peak=value; segment.peakIdx=i;} }
      if (value > 0) { r.posCount++; r.posArea += value; if (value > r.posMax) {r.posMax=value; r.posMaxTime=times[i]; r.posMaxIdx=i;} }
      else if (value < 0) { r.negCount++; r.negArea -= value; if (value < r.negMin) {r.negMin=value; r.negMinTime=times[i]; r.negMinIdx=i;} }
      else r.zeroCount++;
    }
    flush();
    r.netArea = r.posArea-r.negArea; r.absoluteArea = r.posArea+r.negArea;
    r.histMean = r.barCount ? r.netArea/r.barCount : null;
    r.histAbsMean = r.barCount ? r.absoluteArea/r.barCount : null;
    r.difMean = r.difCount ? difSum/r.difCount : null; r.deaMean = r.deaCount ? deaSum/r.deaCount : null;
    r.posAreaX2=r.posArea*2; r.negAreaX2=r.negArea*2; r.netAreaX2=r.netArea*2;
    r.segmentCount=r.posSegments.length+r.negSegments.length;
    return r;
  }

  function rangeIndices(times, from, to) {
    from=seconds(from); to=seconds(to);
    if (from===null || to===null || !times?.length) return null;
    if (from>to) [from,to]=[to,from];
    let start=0, end=times.length;
    while(start<end) {const mid=(start+end)>>1; if(times[mid]<from)start=mid+1;else end=mid;}
    const first=start; end=times.length;
    while(start<end) {const mid=(start+end)>>1; if(times[mid]<=to)start=mid+1;else end=mid;}
    return first<start ? {startIdx:first,endIdx:start-1,from,to,clipped:from<times[0] || to>times[times.length-1]} : null;
  }

  function parseExport(exported, studies) {
    if (!Array.isArray(exported?.schema) || !Array.isArray(exported?.data)) throw new Error('图表尚未提供可统计的指标数据');
    const schema=exported.schema, timeIndex=schema.findIndex(field=>field.type==='time');
    if (timeIndex<0) throw new Error('图表指标数据缺少时间');
    const rows=exported.data.filter(row=>valid(row?.[timeIndex])).map(row=>Array.from(row));
    rows.sort((a,b)=>a[timeIndex]-b[timeIndex]);
    const times=rows.map(row=>seconds(row[timeIndex]));
    if (times.some((time,index)=>index>0 && time<=times[index-1])) throw new Error('图表时间重复，暂时无法统计');
    const metrics=studies.map(study=>{
      const indices={};let label=study.name;
      schema.forEach((field,index)=>{
        if (field.sourceType!=='study' || String(field.sourceId)!==String(study.id)) return;
        label=field.sourceTitle||label;
        const title=String(field.plotTitle||'').trim().toLowerCase();
        if (['histogram','hist','柱状图','柱状线'].includes(title)) indices.hist=index;
        else if (['macd','dif'].includes(title)) indices.dif=index;
        else if (['signal','dea','信号线'].includes(title)) indices.dea=index;
      });
      const recognized=['hist','dif','dea'].every(key=>Number.isInteger(indices[key]));
      return {...study, label, recognized, hist:rows.map(row=>row[indices.hist]), dif:rows.map(row=>row[indices.dif]), dea:rows.map(row=>row[indices.dea])};
    });
    return {times,metrics};
  }

  function rawHtfInput(cm) {
    const identity=cm.getCurrentChartIdentity?.()||cm.widget.symbolInterval();
    const key=String(identity.symbol).toLowerCase()+String(identity.interval).toLowerCase();
    const data=cm.udf_datafeed?._historyProvider?.bars_result?.get(key);
    const times=data?.chart_times;
    if(!Array.isArray(times)||!times.length||times.some((t,i)=>!valid(t)||i>0&&t<=times[i-1]))
      throw new Error('MACD HTF 原始数据尚未就绪。请刷新当前行情图；旧冻结批次可能未保存原始值。');
    const metric={name:'MACD_HTF',label:'MACD HTF · 原始值（非平滑图形）',recognized:true};
    for(const field of ['hist','dif','dea']){
      const values=data['higher_macd_raw_'+field];
      if(!Array.isArray(values)||values.length!==times.length)
        throw new Error('MACD HTF 原始数据未对齐，暂不使用平滑值或普通 MACD 代替。');
      metric[field]=values.slice();
    }
    if(!metric.hist.some(valid))throw new Error('当前周期没有可统计的 MACD HTF 原始值。');
    return {times:times.slice(),metrics:[metric]};
  }

  function localInput(time) {
    if (!valid(time)) return '';
    const d=new Date(seconds(time)*1000), pad=value=>String(value).padStart(2,'0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function inputTime(value) {const time=new Date(value).getTime()/1000;return Number.isFinite(time)?time:null;}
  let nextId=0;

  class Controller {
    constructor(cm) {
      this.cm=cm; this.id=`macd-stats-panel-${++nextId}`; this.startTime=null; this.endTime=null;
      this.pickMode=null; this.serial=0; this.shapeIds=[]; this.snapshots=[]; this.disposed=false;
      this.subscriptions=[]; this.status='选择起止区间，统计 MACD HTF 原始值；平滑图形仅供参考。';
      this.results=null; this.excludeLast=false; this.busy=false;
    }
    identity() {const value=this.cm.widget.symbolInterval();return `${value.symbol}|${value.interval}`;}
    attach() {
      this.cm.widget.headerReady().then(()=>{
        if(this.disposed)return;
        const button=this.cm.widget.createButton({align:'right',useTradingViewStyle:false});
        if(!button)return;
        button.textContent='MACD HTF 区间统计'; button.title='使用原始 HTF 值统计面积、柱高和 DIF / DEA 线高';
        button.setAttribute('role','button');button.setAttribute('tabindex','0');button.setAttribute('aria-label','打开 MACD HTF 区间统计');
        const accessible=()=>{if(this.disposed)return;for(const [key,value] of [['role','button'],['tabindex','0'],['aria-disabled','false']])if(button.getAttribute(key)!==value)button.setAttribute(key,value);};
        accessible();
        const Observer=button.ownerDocument?.defaultView?.MutationObserver;
        if(Observer){const observer=new Observer(accessible);observer.observe(button,{attributes:true,attributeFilter:['role','tabindex','aria-disabled']});this.buttonCleanup=()=>observer.disconnect();}
        button.style.cssText='cursor:pointer;padding:0 10px;color:#FFD54F;';
        button.addEventListener('click',()=>this.open());
        button.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();this.open();}});
        this.button=button;
      }).catch(()=>{});
      this.cm.widget.onContextMenu?.(time=>this.disposed || !valid(time) ? [] : [
        {position:'top',text:'MACD：设为区间起点',click:()=>{this.setStartTime(time);this.open(false);}},
        {position:'top',text:'MACD：设为区间终点并统计',click:()=>{this.open(false);this.setEndTime(time,true);}},
      ]);
      for(const method of ['onSymbolChanged','onIntervalChanged']) {
        const sub=this.cm.chart?.[method]?.(), handler=()=>this.clearRange();
        if(sub?.subscribe){sub.subscribe(null,handler);this.subscriptions.push([sub,handler]);}
      }
    }
    open(useVisible=true) {
      if(this.disposed)return;
      if(useVisible && (this.startTime===null || this.endTime===null)) this.useVisible(false);
      this.ensurePanel(); this.panel.hidden=false; this.render();
    }
    useVisible(compute=true) {
      const range=this.cm.chart.getVisibleRange?.();
      if(!range)return;
      this.startTime=seconds(range.from);this.endTime=seconds(range.to);
      if(compute)void this.computeAndRender();
    }
    setStartTime(time) {this.serial++;this.busy=false;this.results=null;this.startTime=seconds(time);this.status='起点已设，请选择终点。';this.render();}
    setEndTime(time,compute) {this.serial++;this.busy=false;this.results=null;this.endTime=seconds(time);this.render();if(compute)void this.computeAndRender();}
    beginPickRange() {
      this.stopPicker();this.serial++;this.startTime=null;this.endTime=null;this.results=null;this.busy=false;this.pickMode='start';
      this.status='请在图表上单击起点，再单击终点；Esc 取消。';this.open(false);
      let lastTime=null;
      const cross=this.cm.chart.crossHairMoved?.();
      const moved=value=>{lastTime=seconds(value?.time);};
      const down=()=>{
        if(lastTime===null)return;
        const selectedTime=lastTime;
        if(this.pickMode==='start'){this.setStartTime(selectedTime);this.pickMode='end';}
        else if(this.pickMode==='end'){this.stopPicker();this.setEndTime(selectedTime,true);}
      };
      const key=event=>{if(event.key==='Escape'){this.stopPicker();this.status='已取消图上选点，可输入时间或重新选择。';this.render();}};
      cross?.subscribe?.(null,moved);this.cm.widget.subscribe('mouse_down',down);
      const docs=[document];
      const frameDocument=this.button?.ownerDocument;
      if(frameDocument && frameDocument!==document)docs.push(frameDocument);
      docs.forEach(doc=>doc.addEventListener('keydown',key,true));
      this.pickerCleanup=()=>{cross?.unsubscribe?.(null,moved);this.cm.widget.unsubscribe('mouse_down',down);docs.forEach(doc=>doc.removeEventListener('keydown',key,true));};
    }
    stopPicker() {try{this.pickerCleanup?.();}catch(_error){}this.pickerCleanup=null;this.pickMode=null;}
    clearRange() {
      this.serial++;this.stopPicker();this.removeShapes();this.results=null;this.startTime=null;this.endTime=null;this.busy=false;
      if(this.panel)this.panel.hidden=true;
    }
    studies() {
      return (this.cm.chart.getAllStudies?.()||[]).filter(study=>['MACD','MACD_HTF'].includes(study.name));
    }
    studySignature(studies) {
      return JSON.stringify(studies.map(study=>{let inputs=null;try{inputs=this.cm.chart.getStudyById(study.id).getInputValues?.();}catch(_error){}return [study.id,study.name,inputs];}));
    }
    async addIndicators() {
      const token=++this.serial, identity=this.identity();this.busy=true;this.status='正在添加指标…';this.render();
      try {
        for(const name of ['MACD','MACD_HTF']) {
          if(token!==this.serial || identity!==this.identity())return;
          if(!this.studies().some(study=>study.name===name))await this.cm.chart.createStudy(name,false,false);
        }
        this.status='指标已添加，选定区间后点击统计。';
      } catch(_error) {this.status='指标尚未加载完成，请稍后重试。';}
      finally {if(token===this.serial){this.busy=false;this.render();}}
    }
    async computeAndRender() {
      if(this.startTime===null || this.endTime===null){this.status='请设置有效的起点和终点。';this.render();return;}
      this.stopPicker();const token=++this.serial, identity=this.identity(), studies=this.studies();
      const signature=this.studySignature(studies), from=Math.min(this.startTime,this.endTime), to=Math.max(this.startTime,this.endTime);
      this.startTime=from;this.endTime=to;
      this.busy=true;this.results=null;this.status='正在读取 MACD HTF 原始值并统计…';this.render();
      try {
        const parsed=rawHtfInput(this.cm);
        if(this.disposed || token!==this.serial || identity!==this.identity())return;
        if(signature!==this.studySignature(this.studies()))throw new Error('指标参数已改变，请重新统计。');
        const range=rangeIndices(parsed.times,from,to);
        if(!range)throw new Error('所选区间没有已加载的 K 线，请先定位加载该区间。');
        const metrics=parsed.metrics.map(metric=>({...metric,stats:metric.recognized ? computeStats(parsed.times,metric.hist,range.startIdx,range.endIdx,
          {difArr:metric.dif,deaArr:metric.dea,excludeLast:this.excludeLast}) : null}));
        const includedEnd=range.endIdx-(this.excludeLast && range.endIdx===parsed.times.length-1?1:0);
        this.results={identity,from:parsed.times[range.startIdx],to:parsed.times[range.endIdx],
          includedFrom:includedEnd>=range.startIdx?parsed.times[range.startIdx]:null,
          includedTo:includedEnd>=range.startIdx?parsed.times[includedEnd]:null,range,metrics,computedAt:Date.now()};
        this.status=range.clipped?'所选区间含无 K 线的部分，下方按已加载的 HTF 原始数据覆盖范围统计。':'已按 MACD HTF 原始值统计，包含区间首尾；与平滑图形可能不同。';
        this.drawRange(this.results.from,this.results.to,token);
      } catch(error) {if(token===this.serial){this.results=null;this.status=error.message||'统计失败，请稍后重试。';}}
      finally {if(token===this.serial){this.busy=false;this.render();}}
    }
    async loadRange() {
      if(this.startTime===null||this.endTime===null)return;
      const token=++this.serial;this.busy=true;this.results=null;this.status='正在定位并加载所选区间…';this.render();
      try {await this.cm.chart.setVisibleRange({from:Math.min(this.startTime,this.endTime),to:Math.max(this.startTime,this.endTime)});
        if(token===this.serial){this.busy=false;await this.computeAndRender();}}
      catch(_error){if(token===this.serial){this.busy=false;this.status='无法完整加载该区间，请检查可用历史范围后重试。';this.render();}}
    }
    drawRange(from,to,token) {
      this.removeShapes();
      for(const time of new Set([from,to])) {
        try {
          Promise.resolve(this.cm.chart.createShape({time},{shape:'vertical_line',lock:true,disableSelection:true,
            disableSave:true,disableUndo:true,showInObjectsTree:false,overrides:{linecolor:'#FFD54F',linewidth:2,linestyle:2}})).then(id=>{
              if(this.disposed||token!==this.serial)this.cm.chart.removeEntity(id);else if(id)this.shapeIds.push(id);
            }).catch(()=>{});
        }catch(_error){}
      }
    }
    removeShapes(){for(const id of this.shapeIds){try{this.cm.chart.removeEntity(id);}catch(_error){}}this.shapeIds=[];}
    ensurePanel() {
      if(this.panel)return;
      const panel=document.createElement('section');panel.id=this.id;panel.className='macd-interval-panel';panel.setAttribute('aria-label','MACD HTF 区间统计');
      panel.style.cssText='position:fixed;right:18px;top:72px;width:min(520px,calc(100vw - 32px));max-height:calc(100vh - 90px);overflow:auto;z-index:9999;background:#1e222d;color:#e2e8f0;border:1px solid #475569;border-radius:10px;box-shadow:0 12px 40px #0008;padding:16px;font:13px/1.5 system-ui;box-sizing:border-box;';
      document.body.appendChild(panel);this.panel=panel;
      const move=event=>{if(!this.drag)return;panel.style.left=Math.max(0,Math.min(innerWidth-panel.offsetWidth,event.clientX-this.drag.x))+'px';panel.style.top=Math.max(0,event.clientY-this.drag.y)+'px';panel.style.right='auto';};
      const up=()=>{this.drag=null;};
      panel.addEventListener('pointerdown',event=>{if(event.target.closest('[data-drag]')&&!event.target.closest('button'))this.drag={x:event.clientX-panel.offsetLeft,y:event.clientY-panel.offsetTop};});
      document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);
      this.dragCleanup=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);};
    }
    renderMetric(metric) {
      const r=metric.stats;
      if(!r)return `<article class="macd-metric"><b>${escape(metric.label||metric.name)}</b><p>指标数据尚未就绪，请稍后重新统计。</p></article>`;
      const row=(label,max,min,mean)=>`<tr><th>${label}</th><td>${format(max)}</td><td>${format(min)}</td><td>${format(mean)}</td></tr>`;
      return `<article class="macd-metric"><b>${escape(metric.label||metric.name)}</b><span class="macd-muted"> · 有效柱 ${r.barCount} / ${r.rangeCount}${r.excludedLast?' · 已排除图表末根':''}</span>
        <table><thead><tr><th>相对零轴的值</th><th>最大</th><th>最小</th><th>均值</th></tr></thead><tbody>
        ${row('DIF（快线）',r.difMax,r.difMin,r.difMean)}${row('DEA（信号线）',r.deaMax,r.deaMin,r.deaMean)}${row('柱高',r.histMax,r.histMin,r.histMean)}</tbody></table>
        <div class="macd-area"><span>正柱面积 <strong>${r.barCount?format(r.posArea):'—'}</strong></span><span>负柱面积（绝对值） <strong>${r.barCount?format(r.negArea):'—'}</strong></span>
        <span>净面积 <strong>${r.barCount?format(r.netArea):'—'}</strong></span><span>绝对面积 <strong>${r.barCount?format(r.absoluteArea):'—'}</strong></span></div>
        <div class="macd-muted">正柱 ${r.posCount} · 负柱 ${r.negCount} · 零柱 ${r.zeroCount} · 缺失柱 ${r.missingBars}<br>DIF 有效 ${r.difCount} · DEA 有效 ${r.deaCount} · 柱高绝对值均值 ${format(r.histAbsMean)}</div></article>`;
    }
    render() {
      if(!this.panel || this.panel.hidden || this.disposed)return;
      this.panel.style.pointerEvents=this.pickMode?'none':'';
      if(this.pickMode){
        this.panel.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;"><b>MACD 区间选择</b><button data-cancel style="pointer-events:auto;cursor:pointer;color:#fff;background:#334155;border:0;border-radius:4px;padding:5px 10px;">取消选点</button></div><p role="status" style="margin-bottom:0;color:#fcd34d;">${escape(this.status)}</p>`;
        this.panel.querySelector('[data-cancel]').addEventListener('click',()=>{this.stopPicker();this.status='已取消图上选点，可输入时间或重新选择。';this.render();});
        return;
      }
      const disabled=this.busy?'disabled':'', zone=Intl.DateTimeFormat().resolvedOptions().timeZone;
      this.panel.innerHTML=`<style>#${this.id} button,#${this.id} input{font:inherit;color:inherit;background:#2b3445;border:1px solid #536078;border-radius:5px;padding:5px 8px;}#${this.id} button{cursor:pointer;}#${this.id} button:disabled{opacity:.5;cursor:wait;}#${this.id} label{display:flex;flex-direction:column;gap:4px;}#${this.id} input{min-width:0;box-sizing:border-box;}#${this.id} .macd-muted{color:#abb8ca;font-size:11px;}#${this.id} .macd-controls{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0;}#${this.id} .macd-metric{background:#252d3c;padding:12px;border-radius:7px;margin-top:12px;overflow-x:auto;}#${this.id} table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12px;}#${this.id} th,#${this.id} td{text-align:right;padding:7px 3px;border-bottom:1px solid #3b4658;}#${this.id} th:first-child{text-align:left;}#${this.id} .macd-area{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px 0;}#${this.id} strong{display:block;color:#fcd34d;font-variant-numeric:tabular-nums;}</style>
        <div data-drag style="display:flex;justify-content:space-between;align-items:center;cursor:move;"><b style="font-size:16px">MACD HTF 区间统计</b><button data-act="close" aria-label="关闭区间统计">×</button></div>
        <p class="macd-muted">${escape(this.identity())} · 输入时间：${escape(zone)}</p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;"><label>起点<input data-field="start" aria-label="统计起点" type="datetime-local" step="1" value="${localInput(this.startTime)}"></label><label>终点<input data-field="end" aria-label="统计终点" type="datetime-local" step="1" value="${localInput(this.endTime)}"></label></div>
        <div class="macd-controls"><button data-act="calculate" ${disabled} style="background:#2459be">统计区间</button><button data-act="pick">图上选两点</button><button data-act="visible" ${disabled}>当前可视范围</button><button data-act="load" ${disabled}>定位并加载</button><button data-act="add" ${disabled}>添加缺少的指标</button></div>
        <label style="display:block;font-size:12px;"><input data-field="exclude" type="checkbox" ${this.excludeLast?'checked':''}> 排除图表最后一根 K 线（区间含该根时生效）</label>
        <p data-status role="status" style="color:#fcd34d">${escape(this.status)}</p>
        <div data-results>${this.results?`<div class="macd-muted">${this.results.includedFrom===null?'该区间没有纳入统计的 K 线。':`实际统计：${escape(localInput(this.results.includedFrom).replace('T',' '))} → ${escape(localInput(this.results.includedTo).replace('T',' '))}`}</div>${this.results.metrics.map(metric=>this.renderMetric(metric)).join('')}<div class="macd-controls"><button data-act="snapshot">保存对比</button></div>`:''}</div>
        <p class="macd-muted">面积、DIF／DEA 线高和柱高统一使用 MACD HTF 原始值，与自动判定一致。图中平滑插值只供目视参考。面积逐根累加，每根来源 K 线宽度记为 1；负柱面积取绝对值，净面积保留正负，缺失值不当作 0。</p>
        ${this.snapshots.length?`<details><summary>区间对比（${this.snapshots.length}）</summary>${this.snapshots.map(snapshot=>`<p class="macd-muted">${escape(snapshot.label)}</p>${snapshot.metrics.map(metric=>this.renderMetric(metric)).join('')}`).join('')}</details>`:''}`;
      const action=(name,fn)=>this.panel.querySelector(`[data-act="${name}"]`)?.addEventListener('click',fn);
      action('close',()=>this.clearRange());action('pick',()=>this.beginPickRange());action('visible',()=>this.useVisible());
      action('calculate',()=>this.computeAndRender());action('load',()=>this.loadRange());action('add',()=>this.addIndicators());
      action('snapshot',()=>{if(!this.results)return;this.snapshots.unshift({label:`${this.results.identity} ${localInput(this.results.from)} → ${localInput(this.results.to)}`,metrics:this.results.metrics.map(metric=>({name:metric.name,label:metric.label,stats:metric.stats?{...metric.stats}:null}))});this.snapshots.length=Math.min(5,this.snapshots.length);this.render();});
      for(const [field,key] of [['start','startTime'],['end','endTime']])this.panel.querySelector(`[data-field="${field}"]`).addEventListener('change',event=>{this[key]=inputTime(event.target.value);this.serial++;this.busy=false;this.results=null;this.status='区间已修改，请点击统计区间。';this.removeShapes();this.panel.querySelector('[data-results]')?.replaceChildren();this.panel.querySelector('[data-status]').textContent=this.status;this.panel.querySelectorAll('button').forEach(button=>{button.disabled=false;});});
      this.panel.querySelector('[data-field="exclude"]').addEventListener('change',event=>{this.excludeLast=event.target.checked;void this.computeAndRender();});
    }
    dispose(){if(this.disposed)return;this.clearRange();this.disposed=true;this.buttonCleanup?.();this.dragCleanup?.();this.subscriptions.forEach(([sub,fn])=>{try{sub.unsubscribe?.(null,fn);}catch(_error){}});this.panel?.remove();this.button?.remove();}
  }
  function attach(cm){if(!cm)return null;if(cm._macdStats)return cm._macdStats;const controller=new Controller(cm);cm._macdStats=controller;controller.attach();return controller;}
  return {attach,computeStats,parseExport,rawHtfInput,rangeIndices,Controller};
});
