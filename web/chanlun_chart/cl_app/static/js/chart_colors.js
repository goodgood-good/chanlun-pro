(function (root) {
    'use strict';
    const palette = Object.freeze({up:'#ef5350', upSoft:'#ffcdd2', downSoft:'#b2dfdb', down:'#26a69a',
        dif:'#2962FF', dea:'#FF6D00', neutral:'#64748B'});
    const histogram = [palette.up, palette.upSoft, palette.downSoft, palette.down];
    const seriesOverrides = {};
    for (const style of ['candleStyle','hollowCandleStyle','haStyle','volCandlesStyle']) {
        for (const part of ['', 'border', 'wick']) {
            const up = part ? part+'UpColor' : 'upColor', down = part ? part+'DownColor' : 'downColor';
            seriesOverrides['mainSeriesProperties.'+style+'.'+up] = palette.up;
            seriesOverrides['mainSeriesProperties.'+style+'.'+down] = palette.down;
        }
    }
    for (const style of ['barStyle','columnStyle']) {
        seriesOverrides['mainSeriesProperties.'+style+'.upColor'] = palette.up;
        seriesOverrides['mainSeriesProperties.'+style+'.downColor'] = palette.down;
    }
    const studyOverrides = {'macd.macd.color':palette.dif, 'macd.signal.color':palette.dea,
        'volume.volume.color.0':palette.down, 'volume.volume.color.1':palette.up,
        'volume.volume ma:plot.color':palette.dif};
    histogram.forEach((color,i) => { studyOverrides['macd.histogram.color.'+i] = color; });
    function customDefaults(study) {
        const defaults=study?.metainfo?.defaults, styles=defaults?.styles;
        if (!styles) return study;
        if (['MACD_HTF','MACD背离版+金死叉'].includes(study.name)) {
            if(styles.plot_hist)styles.plot_hist.color=palette.up;
            const colors=defaults.palettes?.paletteHist?.colors;
            if (colors) histogram.forEach((color,i) => {if(colors[i]) colors[i].color=color;});
            for (const id of ['plot_dif','plot_macd']) if(styles[id])styles[id].color=palette.dif;
            for (const id of ['plot_dea','plot_signal']) if(styles[id])styles[id].color=palette.dea;
            for (const id of ['plot_crossGold','plot_bullShape']) if(styles[id])styles[id].color=palette.up;
            for (const id of ['plot_crossDead','plot_bearShape']) if(styles[id])styles[id].color=palette.down;
        }
        if (study.name==='成交量') {
            if(styles.plot_vol_up)styles.plot_vol_up.color=palette.up;
            if(styles.plot_vol_down)styles.plot_vol_down.color=palette.down;
        }
        return study;
    }
    const same = (left,right) => String(left).toLowerCase()===String(right).toLowerCase();
    function legacyPair(style, up, down) {
        if(!style)return;
        if ((same(style[up],'#26a69a') && same(style[down],'#ef5350'))
            || (same(style[up],'#22ab94') && same(style[down],'#f23645'))) {
            style[up]=palette.up;style[down]=palette.down;
        }
    }
    function migratePalette(colors, oldSets, desired) {
        if(!colors)return;
        // Only recognized complete default palettes are migrated. Arbitrary
        // saved user colors and every non-color field are retained.
        if(oldSets.some(old=>old.every((color,i)=>same(colors[i]?.color,color)))) {
            desired.forEach((color,i)=>{colors[i].color=color;});
        }
    }
    function normalizeSavedSource(source) {
        const state=source?.state;if(!state)return;
        if(source.type==='MainSeries') {
            for(const name of ['candleStyle','hollowCandleStyle','haStyle','volCandlesStyle','barStyle','columnStyle']) {
                const style=state[name];
                legacyPair(style,'upColor','downColor');legacyPair(style,'borderUpColor','borderDownColor');
                legacyPair(style,'wickUpColor','wickDownColor');
            }
        }
        if(source.type!=='Study'&&source.type!=='study_Volume')return;
        const id=String(source.metaInfo?.id || state.metaInfo?.id || state.inputs?.id || state.id || '');
        normalizeStudyState(state,id);
        if(source.metaInfo?.defaults)normalizeStudyState(source.metaInfo.defaults,id);
    }
    function normalizeStudyState(state,id) {
        if(id.startsWith('Moving Average Convergence/Divergence@')) {
            migratePalette(state.palettes?.palette_0?.colors,
                [['#26a69a','#b2dfdb','#ffcdd2','#ff5252'],['#26a69a','#b2dfdb','#ffcdd2','#ef5350']],histogram);
            if(same(state.styles?.plot_1?.color,'#2196F3'))state.styles.plot_1.color=palette.dif;
            if(same(state.styles?.plot_0?.color,'#FF5252'))state.styles.plot_0.color=palette.up;
        }
        if(id.startsWith('MACD_HTF@') && state.styles?.plot_hist
            && (state.styles.plot_hist.color===undefined||same(state.styles.plot_hist.color,'#0496ff')))state.styles.plot_hist.color=palette.up;
        if(id.startsWith('Volume@')) {
            migratePalette(state.palettes?.volumePalette?.colors,
                [['#ef5350','#26a69a'],['#f23645','#22ab94']], [palette.down,palette.up]);
            if(same(state.styles?.vol_ma?.color,'#2196F3'))state.styles.vol_ma.color=palette.dif;
        }
        if(id.startsWith('CustomIndicatorsVOL@')) {
            if(same(state.styles?.plot_vol_up?.color,'#FF3232'))state.styles.plot_vol_up.color=palette.up;
            if(same(state.styles?.plot_vol_down?.color,'#00A843'))state.styles.plot_vol_down.color=palette.down;
        }
    }
    function normalizeStudyTemplate(content) {
        try {
            const value=JSON.parse(content);
            if(!Array.isArray(value.panes))return content;
            for(const pane of value.panes)for(const source of pane.sources||[])normalizeSavedSource(source);
            return JSON.stringify(value);
        } catch (_) { return content; }
    }
    const api={palette, histogram:Object.freeze(histogram), seriesOverrides:Object.freeze(seriesOverrides),
        studyOverrides:Object.freeze(studyOverrides), customDefaults, normalizeSavedSource, normalizeStudyTemplate};
    root.ChartColors=Object.freeze(api);
    if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
