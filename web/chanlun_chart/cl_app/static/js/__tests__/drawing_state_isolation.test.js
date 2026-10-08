'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadChartManager(overrides = {}) {
  const sandbox = {
    console, Math, JSON, Intl, Array, Object, String, Number, Boolean, RegExp,
    parseInt, parseFloat, isFinite, isNaN, Set, Map, WeakMap, WeakSet, Symbol,
    Promise, Error, URLSearchParams, Date,
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: (callback) => { callback(); return 0; },
    clearTimeout() {},
    performance: { now: () => 0 },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    EventSource: function EventSource() {},
    Utils: { get_market: () => 'us', get_local_data: () => '30' },
    getTVRegistry: () => ({ chartManagers: new Map(), datafeeds: new Map(), widgets: new Map() }),
    Datafeeds: { UDFCompatibleDatafeed: function UDFCompatibleDatafeed() {} },
    TradingView: { widget: function widget() {} },
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
    navigator: { onLine: true },
    location: { search: '', reload() {}, assign() {} },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    createElement: () => ({
      style: {}, dataset: {}, classList: { add() {}, remove() {} },
      appendChild() {}, addEventListener() {}, setAttribute() {}, remove() {},
    }),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { appendChild() {} },
  };
  Object.assign(sandbox,overrides);
  vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '..', 'charts.js'), 'utf8');
  source += '\n;globalThis.__DRAWING_CM = ChartManager;';
  vm.runInContext(source, sandbox, { filename: 'charts.js' });
  return sandbox.__DRAWING_CM;
}

test('only explicit valid user drawings are persisted', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._userDrawingIds = new Set(['manual-1']);

  const saved = manager.serializeUserDrawingsState({
    sources: new Map([
      ['manual-1', { type: 'LineToolTrendLine', state: { points: [1, 2] } }],
      ['auto-1', { type: 'LineToolTrendLine', state: { points: [3, 4] } }],
      ['null-entry', null],
    ]),
    groups: new Map([['polluted-group', { lineTools: ['manual-1', 'auto-1'] }]]),
  });

  assert.equal(saved.schema, 'chanlun-user-drawings');
  assert.deepEqual(Object.keys(saved.sources), ['manual-1']);
  assert.deepEqual(Object.keys(saved.groups), []);
});

function periodDrawing(id, interval, type='LineToolTrendLine') {
  return {id,ownerSource:'_seriesId',symbol:'A:SH.600088',state:{type,id,
    state:{interval,visible:true},points:[{time_t:1000,price:10},{time_t:2000,price:12}]}};
}

test('drawing-period choices follow the chart period and selected presets own newly drawn shapes', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype),properties=new Map();
  let interval='1';
  manager.chart={resolution:()=>interval,getShapeById:id=>({
    getProperties:()=>properties.get(id)||{},setProperties:value=>properties.set(id,value)})};
  manager.setDrawColor=()=>{};
  for(const [view,expected]of [['1',['1','5','30']],['5',['5','30']],['30',['30']]]){
    assert.deepEqual(Array.from(manager._levelBarItems(view).items,item=>item.period),expected);
  }
  assert.equal(manager.setManualDrawingToolPeriod('30'),true);
  manager.configureManualDrawing('higher');
  assert.equal(manager._userDrawingOrigins.get('higher'),'30');
  assert.equal(properties.get('higher').intervalsVisibilities.minutesTo,30);
  manager._userDrawingIds=new Set(['higher']);
  const saved=manager.serializeUserDrawingsState({sources:new Map([['higher',periodDrawing('higher','1')]])});
  assert.equal(saved.sources.higher._cl_origin_resolution,'30');
  manager.setManualDrawingToolPeriod('1');
  manager.configureManualDrawing('higher');
  manager.configureManualDrawing('current');
  assert.equal(manager._userDrawingOrigins.get('higher'),'30','changing the preset must not relabel existing drawings');
  assert.equal(manager._userDrawingOrigins.get('current'),'1');
  interval='5';
  assert.equal(manager.getManualDrawingToolPeriod(),'5');
  assert.equal(manager.setManualDrawingToolPeriod('1'),false);
  interval='30';
  assert.equal(manager.getManualDrawingToolPeriod(),'30');
  assert.equal(manager.setManualDrawingToolPeriod('5'),false);
});

test('drawing picker focus suspends only its toolbar auto-blur and restores it on an outside click', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype),listeners=new Map();
  const attribute='data-mouse-click-auto-blur';
  function parent(value,ancestor=null){
    const attributes=new Map([[attribute,value]]);
    return {parentElement:ancestor,hasAttribute:key=>attributes.has(key),getAttribute:key=>attributes.get(key),
      setAttribute:(key,value)=>attributes.set(key,value),removeAttribute:key=>attributes.delete(key)};
  }
  const outer=parent(''),inner=parent('native',outer);
  const doc={addEventListener:(event,handler)=>listeners.set(event,handler),
    removeEventListener:(event,handler)=>{if(listeners.get(event)===handler)listeners.delete(event);}};
  const select={parentElement:inner,ownerDocument:doc,contains:node=>node===select};
  manager._protectDrawingPickerFocus(select);
  assert.equal(inner.hasAttribute(attribute),false);
  assert.equal(outer.hasAttribute(attribute),false);
  listeners.get('pointerdown')({target:select});
  assert.equal(inner.hasAttribute(attribute),false,'interaction inside the picker retains focus protection');
  listeners.get('pointerdown')({target:{}});
  assert.equal(inner.getAttribute(attribute),'native');
  assert.equal(outer.getAttribute(attribute),'');
  assert.equal(listeners.size,0);
  manager._protectDrawingPickerFocus(select);
  inner.setAttribute(attribute,'updated-by-native-ui');
  manager._releaseDrawingPickerFocus(select);
  assert.equal(inner.getAttribute(attribute),'updated-by-native-ui');
  assert.equal(outer.getAttribute(attribute),'');
  assert.equal(listeners.size,0);
});

test('palette refresh does not reset a focused or unchanged period selector', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  let value='5',assignments=0;
  const doc={activeElement:null};
  const select={ownerDocument:doc,get value(){return value;},set value(next){value=next;assignments++;}};
  doc.activeElement=select;
  const group={getAttribute:()=> '1',querySelectorAll:selector=>selector==='.cl-draw-period'?[select]:[]};
  manager.chart={resolution:()=> '1'};
  manager._paintDrawPalette(group);
  assert.equal(assignments,0);
  assert.equal(value,'5','a tentative keyboard choice is not reset by market refreshes');
  doc.activeElement=null;
  value='1';
  manager._paintDrawPalette(group);
  assert.equal(assignments,0);
  value='5';
  manager._paintDrawPalette(group);
  assert.equal(assignments,1);
  assert.equal(value,'1');
});

test('manual period visibility keeps every saved source while showing only the requested hierarchy', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  let interval='1';
  manager.chart={resolution:()=>interval};
  manager.cl_independent_drawings=true;
  const state={schema:'chanlun-user-drawings',groups:{},sources:{
    one:periodDrawing('one','1'),five:periodDrawing('five','5','LineToolRectangle'),
    thirty:periodDrawing('thirty','30'),}};
  const keys=new Set();
  for(const [view,expected] of [['1',['one','five','thirty']],['5',['five','thirty']],['30',['thirty']]]) {
    interval=view;
    keys.add(manager.getDrawingsCacheKey('A:SH.600088',view));
    const loaded=manager.deserializeUserDrawingsState(state);
    const visible=[...loaded.sources].filter(([,item])=>{
      const visibility=item.state.state.intervalsVisibilities;
      return visibility.minutes&&Number(view)>=visibility.minutesFrom&&Number(view)<=visibility.minutesTo;
    }).map(([id])=>id);
    assert.deepEqual(visible,expected);
    const saved=manager.serializeUserDrawingsState(loaded);
    assert.deepEqual(Object.keys(saved.sources),['one','five','thirty']);
    assert.equal(saved.sources.thirty._cl_origin_resolution,'30');
  }
  assert.equal(keys.size,1,'changing supported periods keeps one complete storage state');
  assert.equal(manager.getDrawingStorageResolution('5'),'hierarchy');
});

test('editing a higher-period drawing on a lower chart retains its origin and deleting it preserves hidden drawings', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  let interval='1';
  manager.chart={resolution:()=>interval};
  const state={schema:'chanlun-user-drawings',groups:{},sources:{
    one:periodDrawing('one','1'),five:periodDrawing('five','5'),thirty:periodDrawing('thirty','30')}};
  manager.deserializeUserDrawingsState(state);
  const edited=periodDrawing('thirty','1');
  edited.state.points[1].price=15;
  const saved=manager.serializeUserDrawingsState({sources:new Map([
    ['one',state.sources.one],['five',state.sources.five],['thirty',edited]])});
  assert.equal(saved.sources.thirty._cl_origin_resolution,'30');
  assert.equal(saved.sources.thirty.state.state.intervalsVisibilities.minutesTo,30);
  assert.equal(saved.sources.thirty.state.points[1].price,15);
  interval='30';
  const loaded=manager.deserializeUserDrawingsState(saved);
  loaded.sources.delete('thirty');
  manager._userDrawingIds.delete('thirty');
  assert.deepEqual(Object.keys(manager.serializeUserDrawingsState(loaded).sources),['one','five']);
});

test('a saved response for a previous chart does not replace current manual ownership', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  manager.chart={resolution:()=> '1'};
  manager.deserializeUserDrawingsState({schema:'chanlun-user-drawings',groups:{},sources:{current:periodDrawing('current','30')}});
  manager.deserializeUserDrawingsState({schema:'chanlun-user-drawings',groups:{},sources:{previous:periodDrawing('previous','5')}},{track:false});
  assert.deepEqual([...manager._userDrawingIds],['current']);
  const saved=manager.serializeUserDrawingsState({sources:new Map([['current',periodDrawing('current','1')]])});
  assert.equal(saved.sources.current._cl_origin_resolution,'30');
});

test('a deferred drawing save keeps its original symbol, period and ownership during a chart switch', async () => {
  const timers=[],ChartManager=loadChartManager({setTimeout:callback=>{timers.push(callback);return timers.length;}});
  const manager=Object.create(ChartManager.prototype),written=[];
  let interval='1',symbol='A:SH.600088';
  manager.chart={resolution:()=>interval,symbol:()=>symbol,getLineToolsState:()=>({sources:new Map([
    ['manual',periodDrawing('manual','1')],['automatic',periodDrawing('automatic','1')]])})};
  manager._activeDrawingMutations=new Set();
  manager._userDrawingIds=new Set(['manual','automatic']);
  manager._userDrawingOrigins=new Map([['manual','1']]);
  manager.save_load_adapter={saveLineToolsAndGroups:async(_layout,_chart,_state,options)=>written.push(options)};
  const saving=manager.scheduleDrawingsSave('drawing_event');
  manager._markAutomaticShapeId('automatic');
  interval='30';symbol='A:SZ.000001';
  manager._is_switching_interval=true;
  manager._userDrawingIds=new Set(['another-symbol']);
  manager._userDrawingOrigins=new Map();
  await timers.shift()();
  await saving;
  assert.equal(written.length,1);
  assert.equal(written[0].symbol,'A:SH.600088');
  assert.equal(written[0].resolution,'1');
  assert.equal(written[0].storageResolution,'hierarchy');
  assert.deepEqual(Object.keys(written[0].capturedState.sources),['manual']);
  assert.equal(written[0].capturedState.sources.manual._cl_origin_resolution,'1');
});

test('queued writes for different drawing contexts are retained while each context still coalesces', async () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype),writes=[];
  let release;
  const first=manager.enqueueLatestDrawingSave(async()=>{writes.push('first');await new Promise(resolve=>release=resolve);},'A');
  const earlier=manager.enqueueLatestDrawingSave(async()=>writes.push('old-A'),'A');
  const other=manager.enqueueLatestDrawingSave(async()=>writes.push('B'),'B');
  const newer=manager.enqueueLatestDrawingSave(async()=>writes.push('new-A'),'A');
  release();
  await Promise.all([first,earlier,other,newer]);
    assert.deepEqual(writes,['first','new-A','B']);
});

test('a late event from a cancelled manual tool does not interrupt drawing persistence', () => {
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  manager.chart={resolution:()=> '1',getShapeById(){throw new Error('There is no such shape');}};
  assert.doesNotThrow(()=>manager.configureManualDrawing('cancelled'));
});

test('unsupported drawing states are rejected and current sources are normalized', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._userDrawingIds = new Set(['stale']);

  assert.throws(
    () => manager.deserializeUserDrawingsState({
      sources: { GiRd26: { type: 'LineToolTrendLine' } },
    }),
    /drawing_state_schema_invalid/,
  );
  assert.equal(manager._userDrawingIds.size, 0);

  const current = manager.deserializeUserDrawingsState({
    schema: 'chanlun-user-drawings',
    sources: {
      manual: { type: 'LineToolRectangle', state: {} },
      invalid: null,
    },
    groups: {},
  });
  assert.deepEqual(Array.from(current.sources.keys()), ['manual']);
  assert.deepEqual(Array.from(manager._userDrawingIds), ['manual']);
});

test('settled automatic ownership revokes a racing user classification', () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  manager._reconcileOwnedIds = new Set();
  manager._userDrawingIds = new Set(['GiRd26']);
  manager._coloredDrawings = new Set(['GiRd26']);

  manager._markAutomaticShapeId('GiRd26');

  assert.equal(manager._reconcileOwnedIds.has('GiRd26'), true);
  assert.equal(manager._userDrawingIds.has('GiRd26'), false);
  assert.equal(manager._coloredDrawings.has('GiRd26'), false);
});

test('manual reload clears overlays through the storage boundary before resetting K line data', async () => {
  const ChartManager = loadChartManager();
  const manager = Object.create(ChartManager.prototype);
  const calls = [];
  manager._disposed = false;
  manager.widget = {
    resetCache() { calls.push('reset-cache'); },
  };
  manager.chart = {
    resetData() { calls.push('reset-data'); },
  };
  manager.udf_datafeed = { _historyProvider: {} };
  manager.scheduleDrawingsSave = async (reason) => {
    calls.push(`save:${reason}`);
  };
  manager._resetDataReadyContext = () => {
    calls.push('reset-data-ready-context');
  };
  manager.reloadDrawingsForCurrentContext = async (reason, options) => {
    calls.push(`reload-drawings:${reason}`);
    assert.equal(options.bypassCache, true);
    assert.equal(options.redrawAutomatic, false);
  };
  manager._requestChanlunDrawWhenReady = () => {
    calls.push('wait-for-kline-before-redraw');
  };

  const first = manager.manualReloadData();
  const racingClick = manager.manualReloadData();
  assert.equal(racingClick, first, '并发点击必须复用同一轮重载');
  assert.equal(await first, true);

  assert.deepEqual(calls, [
    'save:manual-data-reload',
    'reset-data-ready-context',
    'reload-drawings:manual-data-reload',
    'reset-cache',
    'reset-data',
    'wait-for-kline-before-redraw',
  ]);
  assert.equal(manager.udf_datafeed._historyProvider._forceRefreshOnce, true);
  assert.equal(manager._manualReloadInFlight, null);
});

test('failed manual drawing save prevents reload from replacing unsaved edits',async()=>{
  const timers=[],ChartManager=loadChartManager({setTimeout:fn=>{timers.push(fn);return timers.length;}});
  const manager=Object.create(ChartManager.prototype),actions=[];
  manager.shouldSuppressDrawingSave=()=>false;manager._disposed=false;
  manager._userDrawingIds=new Set(['manual']);manager._userDrawingOrigins=new Map([['manual','5']]);
  manager.widget={resetCache(){actions.push('reset');}};
  manager.chart={resolution:()=> '5',symbol:()=> 'us:TSLA.US',resetData(){actions.push('reload');},
    getLineToolsState:()=>({sources:new Map([['manual',periodDrawing('manual','5')]])})};
  manager.save_load_adapter={saveLineToolsAndGroups:async()=>{throw new Error('save rejected');}};
  manager._resetDataReadyContext=()=>{};manager._requestChanlunDrawWhenReady=()=>{};
  manager.reloadDrawingsForCurrentContext=async()=>{actions.push('replace-unsaved-drawing');};
  const task=manager.manualReloadData();task.catch(()=>{});
  await timers.shift()();
  await assert.rejects(task,/save rejected/);
  assert.deepEqual(actions,[]);
  assert.equal(manager._manualReloadInFlight,null);
});

test('failed drawing draft survives switching away and can be saved after recovery',async()=>{
  const timers=[],ChartManager=loadChartManager({setTimeout:fn=>{timers.push(fn);return timers.length;}});
  const manager=Object.create(ChartManager.prototype);let fail=true,symbol='us:TSLA.US',visible,writes=0;
  manager.shouldSuppressDrawingSave=()=>false;manager._drawingsCache=new Map();
  manager._userDrawingIds=new Set(['manual']);manager._userDrawingOrigins=new Map([['manual','5']]);
  const original={sources:new Map([['manual',periodDrawing('manual','5')]])};
  manager.chart={resolution:()=> '5',symbol:()=>symbol,getLineToolsState:()=>original,
    removeAllShapes(){},applyLineToolsState:async value=>{visible=value;}};
  manager._clearAllStrictScopes=()=>{};manager._resetReconcileRetry=()=>{};
  manager.markDrawingMutationStart=()=>{};manager.markDrawingMutationEnd=()=>{};
  manager.isTokenCurrent=()=>true;manager.beginContextSwitch=()=>({});manager.finishContextSwitch=()=>{};
  manager.save_load_adapter={saveLineToolsAndGroups:async()=>{writes++;if(fail)throw new Error('offline');},
    loadLineToolsAndGroups:async()=>({sources:new Map(),groups:new Map()})};
  const saving=manager.scheduleDrawingsSave('drawing_event');await timers.shift()();await saving;
  assert.equal(manager._failedDrawingSnapshots.size,1);
  symbol='us:AAPL.US';await manager.reloadDrawingsForCurrentContext('symbol-change',{redrawAutomatic:false});
  assert.equal(visible.sources.size,0);
  symbol='us:TSLA.US';await manager.reloadDrawingsForCurrentContext('symbol-change',{redrawAutomatic:false});
  assert.ok(visible.sources.has('manual'));
  fail=false;const retry=manager.scheduleDrawingsSave('manual-data-reload');await timers.shift()();await retry;
  assert.equal(manager._failedDrawingSnapshots.size,0);assert.equal(writes,2);
});

test('manual reload cannot claim capture failure or an active drawing mutation was saved',async()=>{
  const ChartManager=loadChartManager(),manager=Object.create(ChartManager.prototype);
  manager.chart={getLineToolsState(){throw new Error('capture failed');}};
  manager.save_load_adapter={saveLineToolsAndGroups:async()=>{}};
  manager.shouldSuppressDrawingSave=()=>false;
  await assert.rejects(manager.scheduleDrawingsSave('manual-data-reload'),/capture failed/);
  manager.shouldSuppressDrawingSave=()=>true;
  await assert.rejects(manager.scheduleDrawingsSave('manual-data-reload'),/尚未确认保存/);
});
