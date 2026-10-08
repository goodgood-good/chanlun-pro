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
        study.metainfo.shortDescription = 'MACD_HTF · ' + ({'1m':'5m','5m':'30m','30m':'日线'}[data.frequency] || data.frequency);
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
        const studies = cm.chart.getAllStudies();
        if (!studies.some(study => study.name === 'MACD') || !studies.some(study => study.name === 'MACD_HTF')) {
            throw new Error('MACD / MACD_HTF study is missing');
        }
        cm._cancelDefaultViewWork();
        cm._hasSavedViewport = true;
        await cm.chart.setVisibleRange({from: coord(data.from), to: coord(data.cutoff)}, {percentRightMargin: 8});
        const panes = cm.chart.getPanes();
        if (panes.length !== 3) throw new Error('MACD / MACD_HTF pane is missing');
        panes[1].setHeight(175);
        panes[2].setHeight(175);
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
    window.composeNotificationCharts = async function (panels) {
        const canvas = document.createElement('canvas');
        const rows = await Promise.all(panels.map(async panel => {
            if (panel.error) return {...panel, height: 150};
            const image = new Image();
            image.src = panel.data;
            await image.decode();
            return {...panel, image, height: image.height};
        }));
        canvas.width = Math.max(...rows.map(row => row.image?.width || 0));
        if (!canvas.width) throw new Error('no chart image to compose');
        canvas.height = rows.reduce((height, row) => height + row.height + 12, 0) - 12;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#e2e8f0';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        let top = 0;
        for (const row of rows) {
            if (row.image) ctx.drawImage(row.image, 0, top);
            else {
                ctx.fillStyle = '#f8fafc'; ctx.fillRect(0, top, canvas.width, row.height);
                ctx.fillStyle = '#334155'; ctx.font = '24px sans-serif';
                ctx.fillText(row.frequency + ' · ' + row.error, 32, top + 80);
            }
            top += row.height + 12;
        }
        return canvas.toDataURL('image/png');
    };
    prepare().catch(error => { window.__notificationError = error.message; });
})();
