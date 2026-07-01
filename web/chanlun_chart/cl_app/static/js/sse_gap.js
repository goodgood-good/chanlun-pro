// SSE 断档检测：纯逻辑，无 DOM/widget 依赖，可被 node:test 直接 require。
//
// 背景：实时 K 线有两条更新通道——SSE 推送(feedRealtimeBar 只喂最新一根)与
// TradingView 30s 轮询(DataPulseProvider 只喂最近 2 根)。两条都只补"最近 1~2 根"，
// 都补不齐"断网期间错过的整段 bar"。唯一能整段补齐的是 TV 的 resetData(清空+重新
// firstDataRequest 全量拉取)。本模块负责判断"是否出现了单根 feed 补不齐的断档"，
// 由 charts.js 据此在 SSE onmessage / 网络恢复 / 切回前台时触发一次 resetData。
//
// 判据：正常实时推进时，SSE 推送的最新 bar 只会比 TV 图表已知的最新 bar 领先
// 0 根(同根更新)或 1 根(刚生成的新 bar，feedBar 能补)。一旦领先 ≥2 根，说明中间
// 有 bar 缺失(断网/长时间无更新)，单根 feed 无法补齐 → 需要 resetData 全量补齐。
//
// 依赖方: charts.js (浏览器经 window.SseGap 使用)。

var SseGap = (function () {

    // TV resolution → 单根周期的秒数。复刻 datafeed 的 periodLengthSeconds 单根口径
    // (data-pulse-provider.ts)。无法解析的周期返回 0，调用方据此退回原行为(不 reset)。
    function resolutionToPeriodSeconds(resolution) {
        const r = String(resolution == null ? '' : resolution).trim();
        if (r === '') return 0;
        if (r === 'D' || r === '1D') return 86400;
        if (r === 'W' || r === '1W') return 7 * 86400;
        if (r === 'M' || r === '1M') return 31 * 86400; // 月按近似 31 天，仅用于 gap 量级判断
        // 其余必须是"纯分钟数"(如 '1'/'5'/'30'/'60'/'120')。带字母后缀的(季线 '3M' 等)
        // String(n)!==r 会被判为无效 → 0：这些周期无实时 gap 问题，退回原行为最安全。
        const n = parseInt(r, 10);
        if (!isFinite(n) || n <= 0 || String(n) !== r) return 0;
        return n * 60;
    }

    // 取 t 数组的末根时间(秒)。t 来自 /tv/history 同构响应(后端为秒)；防御性处理
    // 毫秒量级(≥1e12)输入，归一到秒，避免单位混用导致 gap 误判。
    function latestBarSec(arrT) {
        if (!Array.isArray(arrT) || arrT.length === 0) return null;
        let v = arrT[arrT.length - 1];
        if (typeof v !== 'number' || !isFinite(v) || v <= 0) return null;
        if (v >= 1e12) v = Math.round(v / 1000);
        return v;
    }

    // 断档判定：sse 最新 bar 比 tv 已知最新 bar 领先超过 1.5 个周期(即 ≥2 根，
    // 中间确有缺失)→ true。两边时间戳必须同为"秒"。任一无效 → false(安全退化，
    // 不误触发 reset)。tvLatestSec 为空/0 表示图表尚无数据(首次加载)，走正常 getBars。
    function detectGap(sseLatestSec, tvLatestSec, periodSec) {
        if (!periodSec || periodSec <= 0) return false;
        if (sseLatestSec == null || !isFinite(sseLatestSec) || sseLatestSec <= 0) return false;
        if (tvLatestSec == null || !isFinite(tvLatestSec) || tvLatestSec <= 0) return false;
        const gap = sseLatestSec - tvLatestSec;
        return gap > periodSec * 1.5;
    }

    // 数 SSE 完整快照 data.t 中, 严格落在 (tvLatestSec, sseLatestSec) 开区间内的成根数,
    // 即"断网期间确实生成、但前端没收到"的 K线根数。交易时段边界(午休/隔夜/周末)墙钟差大
    // 但中间无成根 → 计数=0; 真断网期间有成根 → 计数≥1。逐元素 ms 归一防单位混用。
    function countMissingBars(arrT, tvLatestSec, sseLatestSec) {
        if (!Array.isArray(arrT)) return 0;
        let cnt = 0;
        for (let i = 0; i < arrT.length; i++) {
            let v = arrT[i];
            if (typeof v !== 'number' || !isFinite(v)) continue;
            if (v >= 1e12) v = Math.round(v / 1000);
            if (v > tvLatestSec && v < sseLatestSec) cnt++;
        }
        return cnt;
    }

    // charts.js 一行入口：两道判定。
    // 第一道(墙钟粗筛 detectGap): 领先 ≤1 根直接排除(正常实时推进), 省去遍历。
    // 第二道(精判 countMissingBars): 完整快照 data.t 中 (tvLatest, sseLatest) 之间真实成根数 ≥1
    //   才补——干净区分"真断网(有缺根)"与"交易时段边界(午休/隔夜/周末, 墙钟差大但无缺根)",
    //   消除时段边界的无谓 resetData 闪烁; 跨时段的真断网(中间确有成根)仍能触发。
    function shouldResetForGap(responseT, tvLatestSec, resolution) {
        const sseLatestSec = latestBarSec(responseT);
        const periodSec = resolutionToPeriodSeconds(resolution);
        if (!detectGap(sseLatestSec, tvLatestSec, periodSec)) return false;
        return countMissingBars(responseT, tvLatestSec, sseLatestSec) >= 1;
    }

    // ── 第二轮(治本)：墙钟看门狗 + 防抖/退避 + 交易时段门控 ──
    // onmessage 断档检测只在"SSE 真送帧且参照系未被轮询污染"时工作。对 SSE 半开静默、
    // 被中间层缓冲转 fallback、不推帧的标的等场景结构性失效。墙钟看门狗独立于 SSE 帧
    // 周期自查"画布末根 vs 当下应有末根",落后即补。交易时段门控避免收盘/周末误闪;
    // 退避防抖避免冷缓存/数据源停滞下的 8s reset 风暴。

    // 各市场本地时区(仅需精确门控的主市场;其余按 24x5 近似)。
    var _MARKET_TZ = { a: 'Asia/Shanghai', hk: 'Asia/Hong_Kong', us: 'America/New_York' };
    // 各市场本地交易时段(自午夜分钟数, 闭开区间 [start, end))。
    var _MARKET_SESSIONS = {
        a:  [[570, 690], [780, 900]],   // 9:30-11:30, 13:00-15:00
        hk: [[570, 720], [780, 960]],   // 9:30-12:00, 13:00-16:00
        us: [[570, 960]],               // 9:30-16:00 (ET, DST 由 Intl 自动处理)
    };
    var _WATCHDOG_LAG_BARS = 3;   // 画布末根落后超过 N 根判失速(留容差防数据源延迟/边界)
    var _BACKOFF_CAP = 6;         // 退避封顶: 基础间隔 × 2^6 = 64 倍

    // 当前是否大概率在该市场交易时段。用于看门狗门控, 防止收盘/周末/午休误触发 reset。
    // 数字货币 24x7 恒 true; 主市场(a/hk/us)用 Intl 取本地星期+分钟精确判定(自动含 DST);
    // 其余(fx/futures/ny_futures)按 24x5 近似(仅排周末), 残留 off-hour 由退避兜底。
    // 任何异常/无效输入一律放行(true): 宁可多查一次, 不可漏掉真失速。
    function marketLikelyTrading(market, nowSec) {
        var mk = String(market || '').toLowerCase();
        if (mk === 'currency' || mk === 'currency_spot') return true;
        if (typeof nowSec !== 'number' || !isFinite(nowSec) || nowSec <= 0) return true;
        var tz = _MARKET_TZ[mk];
        if (!tz) {
            var wd = new Date(nowSec * 1000).getUTCDay();
            return wd !== 0 && wd !== 6;
        }
        var wdStr, hh, mm;
        try {
            var parts = new Intl.DateTimeFormat('en-US', {
                timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
            }).formatToParts(new Date(nowSec * 1000));
            for (var i = 0; i < parts.length; i++) {
                var p = parts[i];
                if (p.type === 'weekday') wdStr = p.value;
                else if (p.type === 'hour') hh = parseInt(p.value, 10);
                else if (p.type === 'minute') mm = parseInt(p.value, 10);
            }
        } catch (e) {
            return true;
        }
        if (wdStr === 'Sat' || wdStr === 'Sun') return false;
        if (hh === 24) hh = 0; // 部分实现子夜返回 '24', 归一到 0
        var minutes = hh * 60 + mm;
        var sessions = _MARKET_SESSIONS[mk] || [];
        for (var j = 0; j < sessions.length; j++) {
            if (minutes >= sessions[j][0] && minutes < sessions[j][1]) return true;
        }
        return false;
    }

    // 看门狗失速判定: 交易时段内画布末根落后当下超过 _WATCHDOG_LAG_BARS 根 → 该 reset。
    // viewLatestSec = TV 画布实际渲染到的末根秒数(由 charts.js 从订阅者 lastBarTime / bars_result 取)。
    function computeWatchdogReset(market, viewLatestSec, nowSec, periodSec) {
        if (!periodSec || periodSec <= 0) return false;
        if (viewLatestSec == null || !isFinite(viewLatestSec) || viewLatestSec <= 0) return false;
        if (typeof nowSec !== 'number' || !isFinite(nowSec) || nowSec <= 0) return false;
        if (!marketLikelyTrading(market, nowSec)) return false;
        return (nowSec - viewLatestSec) > periodSec * _WATCHDOG_LAG_BARS;
    }

    // 防抖 + 指数退避: 距上次 reset 是否已达"基础间隔 × 2^backoffLevel"。
    function resetAllowed(nowSec, lastResetSec, minIntervalSec, backoffLevel) {
        if (lastResetSec == null) return true;
        var lvl = Math.max(0, Math.min(backoffLevel || 0, _BACKOFF_CAP));
        var interval = minIntervalSec * Math.pow(2, lvl);
        return (nowSec - lastResetSec) >= interval;
    }

    // reset 生效(画布末根真前进)→ 退避归零; 无效(末根没动: 收盘/冷缓存/数据源停)→ +1 封顶。
    function nextBackoffLevel(prevLevel, wasEffective) {
        if (wasEffective) return 0;
        return Math.min((prevLevel || 0) + 1, _BACKOFF_CAP);
    }

    // 综合防抖+退避决策(纯函数, 可测)。返回 {allow, state}:
    //   allow = 本次是否应执行 reset; state = 更新后的 reset 记账(调用方仅在"真执行了 reset"后持久化)。
    // 关键(M-2): 先用"当前 backoffLevel"判防抖间隔, 被拒直接返回且**不抬退避**(被拒调用不消耗退避);
    //   仅放行时才基于"画布末根较上次 reset 是否前进"更新退避(上次生效→归零, 无效→+1)。
    function computeResetGate(prevState, nowSec, viewLatestSec, minIntervalSec) {
        var st = prevState || { lastResetSec: null, backoffLevel: 0, lastViewLatestSec: null };
        if (!resetAllowed(nowSec, st.lastResetSec, minIntervalSec, st.backoffLevel)) {
            return { allow: false, state: st };
        }
        var level = st.backoffLevel || 0;
        if (st.lastResetSec != null && st.lastViewLatestSec != null) {
            var advanced = (viewLatestSec != null && viewLatestSec > st.lastViewLatestSec);
            level = nextBackoffLevel(level, advanced);
        }
        return {
            allow: true,
            state: {
                lastResetSec: nowSec,
                backoffLevel: level,
                lastViewLatestSec: (viewLatestSec != null ? viewLatestSec : st.lastViewLatestSec),
            },
        };
    }

    return {
        _internal: {
            resolutionToPeriodSeconds,
            latestBarSec,
            detectGap,
            countMissingBars,
            shouldResetForGap,
            marketLikelyTrading,
            computeWatchdogReset,
            resetAllowed,
            nextBackoffLevel,
            computeResetGate,
        },
        shouldResetForGap,
        marketLikelyTrading,
        computeWatchdogReset,
        resetAllowed,
        nextBackoffLevel,
        computeResetGate,
    };
})();

if (typeof window !== 'undefined') {
    window.SseGap = SseGap;
}
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SseGap;
}
