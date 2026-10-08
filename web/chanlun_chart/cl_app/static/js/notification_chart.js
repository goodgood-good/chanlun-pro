/* Frozen notification view; all candles and structures use the normal renderer. */
(function () {
    const data = window.__notificationChart;
    window.Utils = {get_market: () => data.market, get_code: () => data.code,
        get_local_data: key => key.includes('interval') ? data.resolution : key === 'theme' ? 'light' : null,
        set_local_data() {}};
    window.__CHANLUN_SSE_ENABLED = false;
    window.__CHANLUN_SCREENING_EVIDENCE = {source: 'notification', point: 'notification'};
    const cm = window.notificationChartManager = new ChartManager('notification');
    cm.getCustomIndicators = function (PineJS) {
        const study = TvIdxMACDBackend.idx(PineJS);
        study.metainfo.shortDescription = 'MACD · ' + data.frequency;
        return Promise.resolve([study]);
    };
    cm.init();
    function complete() {
        const chartData = cm.chart && cm.getChartData();
        return cm._initialLoadDone && chartData && cm._strictStructureStatus?.state === 'ready'
            && !cm._automaticShapeCreateCount && cm._strictReconcileComplete()
            && cm._baseStructureEntitiesMatch(chartData, data.resolution);
    }
    async function waitForReady() {
        const until = performance.now() + 24000;
        while (!complete()) {
            if (performance.now() > until) throw new Error('chart geometry is not ready');
            await new Promise(resolve => setTimeout(resolve, 100));
        }
    }
    const coord = time => cm._centerChartTimeCoordinate(time, data.resolution, 'end');
    async function prepare() {
        await waitForReady();
        await cm.ensureRequestedDefaultStudies();
        if (!cm.chart.getAllStudies().length) throw new Error('MACD study is missing');
        cm._cancelDefaultViewWork();
        cm._hasSavedViewport = true;
        await cm.chart.setVisibleRange({from: coord(data.from), to: coord(data.cutoff)}, {percentRightMargin: 8});
        const panes = cm.chart.getPanes();
        if (panes.length !== 2) throw new Error('MACD pane is missing');
        panes[1].setHeight(205);
        await waitForReady();
        const options = {lock: true, disableSelection: true, disableSave: true, disableUndo: true};
        if (data.marker) {
            await cm.chart.createShape({time: coord(data.anchor_at), price: data.anchor_price}, {
                ...options, shape: data.up ? 'arrow_up' : 'arrow_down', text: data.label,
                overrides: {color: data.invalidation_reason ? '#DC2626' : '#7C3AED',
                    arrowColor: data.invalidation_reason ? '#DC2626' : '#7C3AED',fontsize: 16, showLabel: true},
            });
        }
        if(data.reference_anchor_at){
            await cm.chart.createShape({time:coord(data.reference_anchor_at),price:data.reference_anchor_price},{
                ...options,shape:data.up?'arrow_up':'arrow_down',text:'原预警参考（已失效）',
                overrides:{color:'#64748B',arrowColor:'#64748B',fontsize:13,showLabel:true},
            });
        }
        if (data.stop_price !== null) {
            await cm.chart.createShape({time: coord(data.anchor_at), price: data.stop_price}, {
                ...options, shape: 'horizontal_line', text: data.stop_label,
                overrides: {linecolor: '#DC2626', linewidth: 1, linestyle: 2, showLabel: true, textcolor: '#DC2626'},
            });
        }
        await cm.chart.createAnchoredShape({x: 0.01, y: 0.09}, {...options, shape: 'anchored_text',
            text: data.title + '\n' + data.caption + (data.invalidation_reason ? '\n失效原因：'+data.invalidation_reason : ''),
            overrides: {color: '#334155', fontsize: 14, backgroundColor: '#ffffff', fillBackground: true}});
        await document.fonts.ready;
        // Two animation frames let the native canvas finish its last paint.
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        await waitForReady();
        window.__notificationReady = true;
    }
    window.exportNotificationChart = async function () {
        if (window.devicePixelRatio !== 1) throw new Error('native export requires a 1:1 pixel ratio');
        if (!window.__notificationReady || !complete()) throw new Error('chart is not ready for export');
        const history = cm.getChartData().barsResult;
        if (history.strict_structure?.source_closed_at !== data.cutoff) throw new Error('snapshot cutoff mismatch');
        const canvas = await cm.widget.takeClientScreenshot({hideStudiesFromLegend: false});
        return canvas.toDataURL('image/png');
    };
    prepare().catch(error => { window.__notificationError = error.message; });
})();
