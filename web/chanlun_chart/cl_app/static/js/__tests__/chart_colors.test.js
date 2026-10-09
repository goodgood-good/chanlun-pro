'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const colors=require('../chart_colors.js');
const palette=values=>({colors:Object.fromEntries(values.map((color,i)=>[i,{color,width:2,style:1}]))});
test('candle bodies, borders, wicks and native volume share red-up green-down defaults',()=>{
  for(const style of ['candleStyle','hollowCandleStyle','haStyle','volCandlesStyle']){
    for(const field of ['upColor','borderUpColor','wickUpColor'])assert.equal(colors.seriesOverrides[`mainSeriesProperties.${style}.${field}`],colors.palette.up);
    for(const field of ['downColor','borderDownColor','wickDownColor'])assert.equal(colors.seriesOverrides[`mainSeriesProperties.${style}.${field}`],colors.palette.down);
  }
  assert.equal(colors.studyOverrides['volume.volume.color.0'],colors.palette.down);
  assert.equal(colors.studyOverrides['volume.volume.color.1'],colors.palette.up);
});
test('all three MACD defaults and custom volume use the same direction and line palettes',()=>{
  for(const [file,name] of [['chart_idx_macd_backend.js','TvIdxMACDBackend'],['chart_idx_macdbl.js','TvIdxMACDBL'],['chart_idx_vol.js','TvIdxVOL']]){
    const sandbox={};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../tv_indicators',file),'utf8'),sandbox);
    const study=colors.customDefaults(sandbox[name].idx({})),defaults=study.metainfo.defaults;
    if(name==='TvIdxVOL'){
      assert.equal(defaults.styles.plot_vol_up.color,colors.palette.up);assert.equal(defaults.styles.plot_vol_down.color,colors.palette.down);
    }else{
      for(let i=0;i<4;i++)assert.equal(defaults.palettes.paletteHist.colors[i].color,colors.studyOverrides['macd.histogram.color.'+i]);
      assert.equal((defaults.styles.plot_dif||defaults.styles.plot_macd).color,colors.studyOverrides['macd.macd.color']);
      assert.equal((defaults.styles.plot_dea||defaults.styles.plot_signal).color,colors.studyOverrides['macd.signal.color']);
      assert.equal(defaults.styles.plot_hist.color,colors.palette.up);
    }
  }
});
test('restored legacy native MACD and Volume palettes migrate without changing inputs or line widths',()=>{
  const macd={type:'Study',metaInfo:{id:'Moving Average Convergence/Divergence@tv-basicstudies-1'},state:{
    inputs:{in_0:18,in_1:36,in_2:9},palettes:{palette_0:palette(['#26a69a','#b2dfdb','#ffcdd2','#ff5252'])},styles:{plot_1:{color:'#2196F3',linewidth:3}}}};
  const volume={type:'study_Volume',metaInfo:{id:'Volume@tv-basicstudies-1'},state:{
    palettes:{volumePalette:palette(['#ef5350','#26a69a'])},styles:{vol_ma:{color:'#2196f3'}}}};
  colors.normalizeSavedSource(macd);colors.normalizeSavedSource(volume);
  assert.deepEqual(Object.values(macd.state.palettes.palette_0.colors).map(v=>v.color),colors.histogram);
  assert.deepEqual(macd.state.inputs,{in_0:18,in_1:36,in_2:9});assert.equal(macd.state.styles.plot_1.linewidth,3);
  assert.equal(volume.state.palettes.volumePalette.colors[0].color,colors.palette.down);
  assert.equal(volume.state.palettes.volumePalette.colors[1].color,colors.palette.up);
});
test('custom saved palettes and unrelated studies survive migration unchanged',()=>{
  const study={type:'Study',metaInfo:{id:'Moving Average Convergence/Divergence@tv-basicstudies-1'},state:{
    palettes:{palette_0:palette(['#123456','#b2dfdb','#ffcdd2','#ff5252'])},styles:{plot_1:{color:'#abcdef'}}}};
  const original=JSON.stringify(study);colors.normalizeSavedSource(study);assert.equal(JSON.stringify(study),original);
  const candle={type:'MainSeries',state:{candleStyle:{upColor:'#112233',downColor:'#445566',wickUpColor:'#778899',wickDownColor:'#aabbcc'}}};
  const originalCandle=JSON.stringify(candle);colors.normalizeSavedSource(candle);assert.equal(JSON.stringify(candle),originalCandle);
});
test('study templates update recognized defaults while keeping sources and malformed content intact',()=>{
  const template={version:3,panes:[{sources:[{id:'saved-macd',type:'Study',
    metaInfo:{id:'Moving Average Convergence/Divergence@tv-basicstudies-1',defaults:{palettes:{palette_0:palette(['#26a69a','#b2dfdb','#ffcdd2','#ff5252'])}}},
    state:{palettes:{palette_0:palette(['#26a69a','#b2dfdb','#ffcdd2','#ff5252'])},inputs:{in_0:17}}}]}]};
  const saved=JSON.parse(colors.normalizeStudyTemplate(JSON.stringify(template))).panes[0].sources[0];
  assert.equal(saved.id,'saved-macd');assert.equal(saved.state.inputs.in_0,17);
  assert.equal(saved.state.palettes.palette_0.colors[0].color,colors.palette.up);
  assert.equal(saved.metaInfo.defaults.palettes.palette_0.colors[0].color,colors.palette.up);
  assert.equal(colors.normalizeStudyTemplate('invalid'),'invalid');
});

for(const old of [
  ['#22AB94','#ACE5DC','#FCCBCD','#FF5252'],
  ['rgba(34, 171, 148, 1)','rgb(172, 229, 220)','rgba(252, 203, 205, 1.0)','#FF5252'],
])test('the palette found in saved production MACD layouts converts above-zero green to red: '+old[0],()=>{
  const source={type:'Study',metaInfo:{id:'Moving Average Convergence/Divergence@tv-basicstudies-1'},
    state:{palettes:{palette_0:palette(old)},inputs:{in_0:17},styles:{plot_1:{color:'#abcdef'}}}};
  const overrides=colors.macdHistogramOverrides('MACD',source.state);
  assert.deepEqual(Object.values(overrides),colors.histogram);
  colors.normalizeSavedSource(source);
  assert.deepEqual(Object.values(source.state.palettes.palette_0.colors).map(row=>row.color),colors.histogram);
  assert.equal(source.state.inputs.in_0,17);assert.equal(source.state.styles.plot_1.color,'#abcdef');
  assert.equal(colors.macdHistogramOverrides('MACD',source.state),null);
});
test('the runtime correction leaves custom, transparent and unrelated palettes alone',()=>{
  const values={palettes:{palette_0:palette(['#123456','#ACE5DC','#FCCBCD','#FF5252'])}};
  assert.equal(colors.macdHistogramOverrides('MACD',values),null);
  values.palettes.palette_0.colors[0].color='rgba(34,171,148,0.5)';
  assert.equal(colors.macdHistogramOverrides('MACD',values),null);
  values.palettes.palette_0.colors[0].color='#22AB94';
  assert.equal(colors.macdHistogramOverrides('RSI',values),null);
});
