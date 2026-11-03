/* Current market data with immutable annotations from the selected receipt. */
(function () {
  'use strict';
  function barAtOrBefore(times,time){
    if(!Number.isFinite(time)||!times.length||time<times[0])return null;
    let left=0,right=times.length;
    while(left<right){const middle=(left+right)>>1;if(times[middle]<=time)left=middle+1;else right=middle;}
    return times[left-1];
  }
  function annotationPoints(data,times,resolution,barTimeLabel,coordinate){
    const result=[];
    if(Number.isFinite(data.notification_at)){
      // Delivery is a wall-clock instant, not a candle's close timestamp.
      const time=barAtOrBefore(times,coordinate(data.notification_at,resolution,'start'));
      if(time!==null)result.push({kind:'notice',time,at:data.notification_at});
    }
    if(Number.isFinite(data.anchor_at)&&Number.isFinite(data.anchor_price)){
      // Preserve the original period's reference candle when changing periods.
      const reference=coordinate(data.anchor_at,data.resolution,barTimeLabel);
      const bucket=coordinate(reference,resolution,'start');
      const lastBarEnd=times[times.length-1]+(/^\d+$/.test(resolution)?Number(resolution)*60:1);
      const time=bucket<lastBarEnd?barAtOrBefore(times,bucket):null;
      if(time!==null)result.push({kind:'point',time,price:data.anchor_price,at:data.anchor_at});
    }
    return result;
  }
  if(typeof module==='object'&&module.exports)module.exports={barAtOrBefore,annotationPoints};
  if(typeof window==='undefined'||!window.__notificationCurrentChart)return;
  const data=window.__notificationCurrentChart,preferences=new Map(),local=new Map();
  window.AccountPreferences={getItem:key=>preferences.get(key)??null,
    setItem:(key,value)=>{preferences.set(key,value);return true;},removeItem:key=>preferences.delete(key)};
  window.Utils={get_market:()=>data.market,get_code:()=>data.code,
    get_local_data:key=>local.get(key)??(key.includes('interval')?data.resolution:key==='theme'?'light':null),
    set_local_data:(key,value)=>local.set(key,value)};
  const url=new URL(window.location.href);url.searchParams.set('default_study','MACD');
  window.history.replaceState(null,'',url);
  const status=document.getElementById('notification-current-status');
  const cm=window.notificationChartManager=new ChartManager('notification');
  cm.readOnlySnapshot=true; // Isolate layouts/drawings; the /tv feed remains live.
  cm.datafeedUrl='/tv';
  cm.exportMinBarSpacing=0.01;
  cm.getCustomIndicators=PineJS=>Promise.resolve([window.ChartColors.customDefaults(TvIdxMACDBackend.idx(PineJS))]);
  cm.init();
  let entities=[],drawKey=null,viewportKey=null,busy=false,disposed=false;
  const timestamp=at=>new Date(at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间';
  const noticeText=data.notification_label+' · '+timestamp(data.notification_at);
  const pointText=['通知参考点',data.frequency,data.point_label,data.stage_label].filter(Boolean).join(' · ');
  async function draw(){
    if(disposed||busy||!cm.chart||!cm._initialLoadDone||cm._is_switching_interval||cm.isApplyingDrawingState)return;
    const chartData=cm.getChartData(),bars=chartData?.barsResult?.bars;
    if(!bars?.length)return;
    const resolution=String(chartData.chartInterval),identity=chartData.symbolKey;
    if(String(chartData.chartSymbol).toLowerCase()!==(data.market+':'+data.code).toLowerCase())return;
    const times=bars.map(bar=>bar.time/1000);
    const points=annotationPoints(data,times,resolution,chartData.barsResult.bar_time_label,
      (time,period,label)=>cm._centerChartTimeCoordinate(time,period,label));
    const key=JSON.stringify([identity,points]);
    const existing=new Set(cm.chart.getAllShapes().map(shape=>shape.id));
    if(drawKey===key&&entities.every(id=>existing.has(id))&&viewportKey===identity)return;
    busy=true;
    try{
      for(const id of entities)try{cm.chart.removeEntity(id);}catch(_error){}
      entities=[];drawKey=null;
      if(viewportKey!==identity){
        await cm.ensureRequestedDefaultStudies();
        cm._cancelDefaultViewWork();cm._hasSavedViewport=true;
        const oldest=Math.min(...points.map(point=>point.time));
        const index=times.indexOf(oldest);
        const start=index<0?Math.max(0,times.length-180):Math.max(0,Math.min(index-40,times.length-180));
        await cm.chart.setVisibleRange({from:times[start],to:times[times.length-1]},{percentRightMargin:8});
        const height=document.getElementById('tv_chart_container_notification').clientHeight;
        for(const pane of cm.chart.getPanes().slice(1))pane.setHeight(Math.max(70,Math.min(160,Math.floor(height*.22))));
      }
      if(disposed||identity!==cm.getChartData()?.symbolKey)return;
      const options={lock:true,disableSelection:true,disableSave:true,disableUndo:true};
      for(const point of points){
        const id=await cm.chart.createShape(point.kind==='notice'?{time:point.time}:{time:point.time,price:point.price},
          point.kind==='notice'?{...options,shape:'vertical_line',text:noticeText,
            overrides:{linecolor:'#7c3aed',linewidth:2,linestyle:2,showLabel:true,textcolor:'#7c3aed',fontsize:12}}:
          {...options,shape:/(buy|_down)$/.test(data.point_type||'')?'arrow_up':'arrow_down',
            text:pointText+' · '+data.anchor_price,
            overrides:{color:'#7c3aed',arrowColor:'#7c3aed',fontsize:13,showLabel:true}});
        if(disposed||identity!==cm.getChartData()?.symbolKey){try{cm.chart.removeEntity(id);}catch(_error){}return;}
        entities.push(id);
      }
      drawKey=key;viewportKey=identity;
      window.__notificationMarkers=points.map((point,index)=>({...point,id:entities[index],
        text:point.kind==='notice'?noticeText:pointText}));
      window.__notificationReady=true;
      status.hidden=points.some(point=>point.kind==='notice');
      if(!status.hidden)status.textContent=data.notification_at?'通知时间早于已加载的历史，可向左拖动继续查看。':'该条记录未保存通知时间。';
    }catch(error){
      window.__notificationError=error.message;
      status.hidden=false;status.textContent='通知标记暂未加载，正在重试…';
    }finally{busy=false;}
  }
  const timer=setInterval(()=>void draw(),500);
  window.addEventListener('pagehide',()=>{disposed=true;clearInterval(timer);});
})();
