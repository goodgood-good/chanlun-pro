const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function view({frequency = '30m', shift = true, stuck = false} = {}) {
    const data = {market:'us',code:'AAPL.US',frequency,resolution:frequency==='30m'?'30':'1',
        from:1000,cutoff:2000,stop_price:null,marker:false,title:'signal',caption:'history'};
    let range = {from:1000,to:2000}, calls = 0, exports = 0;
    const chart = {
        getAllStudies: () => [{name:'MACD'},{name:'MACD_HTF'}],
        setVisibleRange: async requested => {calls++;range={...requested};if(stuck)range.from+=100;},
        getVisibleRange: () => ({...range}),
        getPanes: () => [{}, {setHeight(){if(shift)range.from+=100;}}, {setHeight(){}}],
        createAnchoredShape: async () => {},
    };
    class ChartManager {
        constructor() {
            this.chart=chart;this._initialLoadDone=true;this._strictStructureStatus={state:'ready'};
            this.widget={takeClientScreenshot:async()=>{exports++;return {toDataURL:()=> 'png'};}};
        }
        init() {}
        getChartData(){return {barsResult:{strict_structure:{source_closed_at:2000}}};}
        _strictReconcileComplete(){return true;}
        _baseStructureEntitiesMatch(){return true;}
        _centerChartTimeCoordinate(value){return value;}
        ensureRequestedDefaultStudies(){return Promise.resolve();}
        _cancelDefaultViewWork() {}
    }
    const window = {__notificationChart:data,devicePixelRatio:1};
    const sandbox={window,ChartManager,document:{fonts:{ready:Promise.resolve()}},performance,
        setTimeout,requestAnimationFrame:callback=>queueMicrotask(callback),Image:class{}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../notification_chart.js'),'utf8'),sandbox);
    return {window,chart,stats:()=>({calls,exports}),shiftAgain:()=>{range.from+=100;}};
}

async function prepared(result) {
    for(let i=0;i<10 && !result.window.__notificationReady && !result.window.__notificationError;i++) {
        await new Promise(resolve=>setImmediate(resolve));
    }
}

test('30m export repairs a range shifted by pane layout and retains complete history', async()=>{
    const result=view();await prepared(result);
    assert.equal(result.window.__notificationReady,true);
    assert.equal(result.chart.getVisibleRange().from,1000);
    assert.equal(result.window.__notificationViewport.beforeCorrectionFrom,1100);
    assert.equal(result.window.__notificationViewport.corrections,1);
    assert.equal(result.stats().calls,2);
});

test('the export checks range again if layout shifts after readiness', async()=>{
    const result=view({shift:false});await prepared(result);result.shiftAgain();
    assert.equal(await result.window.exportNotificationChart(),'png');
    assert.equal(result.chart.getVisibleRange().from,1000);
    assert.equal(result.stats().exports,1);
});

test('a persistent 30m truncation stops after two corrections and cannot export', async()=>{
    const result=view({stuck:true});await prepared(result);
    assert.match(result.window.__notificationError,/after layout retries/);
    assert.equal(result.stats().calls,3);
    await assert.rejects(result.window.exportNotificationChart(),/not ready/);
    assert.equal(result.stats().exports,0);
});

test('a stable range does not repeatedly reset the chart', async()=>{
    const result=view({shift:false});await prepared(result);
    await result.window.exportNotificationChart();
    assert.equal(result.stats().calls,1);
    assert.equal(result.window.__notificationViewport.corrections,0);
});

for(const frequency of ['1m','5m']) {
    test(frequency + ' expanded history also recovers layout shifts', async()=>{
        const result=view({frequency});await prepared(result);
        assert.equal(result.window.__notificationReady,true);
        assert.equal(result.chart.getVisibleRange().from,1000);
        assert.equal(result.window.__notificationViewport.corrections,1);
    });
}
