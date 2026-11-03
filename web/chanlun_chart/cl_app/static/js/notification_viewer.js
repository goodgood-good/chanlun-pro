/* Read-only controls for a notification's isolated frozen chart. */
(function () {
  'use strict';
  const data=window.__notificationChart;
  const preferences=new Map();
  window.AccountPreferences={getItem:key=>preferences.get(key)??null,
    setItem:(key,value)=>{preferences.set(key,value);return true;},removeItem:key=>preferences.delete(key)};
  const status=document.getElementById('notification-view-status');
  const focus=document.getElementById('notification-focus'),all=document.getElementById('notification-all-history');
  function message(text,error=false){status.textContent=text;status.dataset.error=String(error);}
  async function range(from){
    const cm=window.notificationChartManager;
    if(!cm?.chart||!window.__notificationReady)return;
    try{
      await cm.chart.setVisibleRange({from:cm._centerChartTimeCoordinate(from,data.resolution,'end'),
        to:cm._centerChartTimeCoordinate(data.cutoff,data.resolution,'end')},{percentRightMargin:8});
    }catch(_error){message('暂时无法调整显示范围，请重新打开图表。',true);}
  }
  focus.addEventListener('click',()=>void range(data.from));
  all.addEventListener('click',()=>void range(data.history_from));
  window.addEventListener('notification-chart-ready',()=>{
    focus.disabled=all.disabled=false;
    message('当时图表已加载 · K线、MACD 与 MACD HTF · 不随当前行情刷新');
  });
  window.addEventListener('notification-chart-error',()=>message('图表加载未完成，请重新打开或查看已保存附图。',true));
})();
