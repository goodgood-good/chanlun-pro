// 缠论显示配置（cl_show_config）与独立周期画线开关（cl_independent_drawings）
// 按 ChartManager 实例和图表周期独立维护，key 形如 cl_show_config_<chartId>_<resolution>。

// 默认的缠论显示项配置
const CL_SHOW_DEFAULT = {
    schema: "chanlun-chart-config-v2",
    // 首屏只展示可审计的正式结构。分型、笔、线段仍可在显示设置中
    // 随时打开，但不再用数百个基础图形遮住正式走势与中枢。
    fx: false,
    bi: false,
    xd: false,
    center_observation: false,
    center_all: true,
    center_provisional: false,
    trend_all: true,
    pending_movement: false,
    point_all: true,
    point_1buy: true,
    point_2buy: true,
    point_3buy: true,
    point_1sell: true,
    point_2sell: true,
    point_3sell: true,
    divergence_all: true,
};

function recursiveDisplayLevels(interval) {
    const key = String(interval || '').trim();
    const map = {
        '1': ['1m', '5m', '30m', '日线'],
        '1m': ['1m', '5m', '30m', '日线'],
        '5': ['5m', '30m', '日线'],
        '5m': ['5m', '30m', '日线'],
        '30': ['30m', '日线'],
        '30m': ['30m', '日线'],
        '1D': ['日线'],
        '1d': ['日线'],
        'D': ['日线'],
        'd': ['日线'],
    };
    const fallback = /^\d+$/.test(key) ? `${key}m` : (key || '当前周期');
    const labels = map[key] || [fallback];
    return labels.map((label, level) => ({ label, level }));
}

// 各市场的 TV 显示时区，与后端 services/constants.py:market_timezone 保持一致。
// 之前硬编码 "Asia/Shanghai" 导致美股 K 线按北京时区显示（NY 09:30 EDT → 21:30）。
// currency/currency_spot 在后端用 get_localzone()，前端改为读取浏览器 Intl IANA tz 等价回退。
const MARKET_TIMEZONE = {
    a: "Asia/Shanghai",
    hk: "Asia/Shanghai",
    fx: "Asia/Shanghai",
    us: "America/New_York",
    futures: "Asia/Shanghai",
    ny_futures: "Asia/Shanghai",
};
// Blob 文档会继承父页面的内容安全策略，导致图表库的启动内联脚本被拦截。
// 随包提供的 sameorigin.html 不使用该页面策略，并在下方组件选项中显式启用。
const CHART_DISABLED_FEATURES = Object.freeze([
    "go_to_date",
    "use_blob_for_iframe_loading",
]);
const USER_DRAWING_STATE_SCHEMA = "chanlun-user-drawings";
const DEFAULT_STUDY_ALLOWLIST = new Set(["MACD_HTF"]);
function requestedDefaultStudies(search) {
    try {
        const params = new URLSearchParams(search || "");
        const selected = ["MACD_HTF"];
        params.getAll("default_study").forEach((value) => {
            const name = String(value || "").trim();
            if (DEFAULT_STUDY_ALLOWLIST.has(name) && !selected.includes(name)) selected.push(name);
        });
        return selected;
    } catch (e) {
        return ["MACD_HTF"];
    }
}
function _browserLocalTz() {
    try {
        return (Intl.DateTimeFormat().resolvedOptions().timeZone) || "Asia/Shanghai";
    } catch (e) {
        return "Asia/Shanghai";
    }
}
function getMarketTimezone(market) {
    if (market === "currency" || market === "currency_spot") {
        return _browserLocalTz();
    }
    return MARKET_TIMEZONE[market] || "Asia/Shanghai";
}

// 高频调试日志统一走 clog：仅 window.__chanlunDebug=true 时输出，避免生产 console 刷屏。
// 错误诊断（console.warn / console.error）不受此开关控制。
function clog(...args) {
    if (window.__chanlunDebug) console.log(...args);
}

// 菜单在主文档中，TradingView 的可交互区域在同源 iframe 中；递归收集这些
// document，才能让任意层级的图表空白处都参与菜单的 outside-dismiss。
function _collectSameOriginDocuments(rootDocument) {
    const documents = [];
    const visited = new Set();
    const visit = (doc) => {
        if (!doc || visited.has(doc)) return;
        visited.add(doc);
        documents.push(doc);

        let frames = [];
        try {
            frames = typeof doc.querySelectorAll === 'function'
                ? Array.from(doc.querySelectorAll('iframe'))
                : [];
        } catch (e) {
            return;
        }
        frames.forEach((frame) => {
            try {
                if (frame.contentDocument) visit(frame.contentDocument);
            } catch (e) { /* 跨源 iframe 无法绑定，安全跳过 */ }
        });
    };
    visit(rootDocument);
    return documents;
}

function bindClDisplayMenuOutsideDismiss(rootDocument, menuElement, triggerElement, dismiss) {
    const documents = _collectSameOriginDocuments(rootDocument);
    const eventTypes = ['pointerdown', 'click', 'keydown'];
    let active = true;

    const cleanup = () => {
        if (!active) return;
        active = false;
        documents.forEach((doc) => {
            eventTypes.forEach((type) => doc.removeEventListener(type, closeHandler, true));
        });
    };
    const contains = (element, target) => {
        if (!element || !target) return false;
        try {
            return element === target
                || (typeof element.contains === 'function' && element.contains(target));
        } catch (e) {
            return false;
        }
    };
    const closeHandler = (event) => {
        if (!active) return;
        if (event && event.type === 'keydown') {
            if (event.key !== 'Escape') return;
            if (typeof event.preventDefault === 'function') event.preventDefault();
            if (typeof event.stopPropagation === 'function') event.stopPropagation();
            cleanup();
            if (typeof dismiss === 'function') dismiss(event);
            else if (menuElement && typeof menuElement.remove === 'function') menuElement.remove();
            return;
        }
        const target = event && event.target;
        if (contains(menuElement, target) || contains(triggerElement, target)) return;
        cleanup();
        if (typeof dismiss === 'function') dismiss(event);
        else if (menuElement && typeof menuElement.remove === 'function') menuElement.remove();
    };

    documents.forEach((doc) => {
        eventTypes.forEach((type) => doc.addEventListener(type, closeHandler, true));
    });
    return cleanup;
}

function bindClDisplayButtonAccessibility(buttonElement) {
    if (!buttonElement || typeof buttonElement.setAttribute !== 'function') return () => {};
    const ownerWindow = buttonElement.ownerDocument
        && buttonElement.ownerDocument.defaultView;
    const apply = () => {
        if (buttonElement.getAttribute('role') !== 'button') {
            buttonElement.setAttribute('role', 'button');
        }
        if (buttonElement.getAttribute('tabindex') !== '0') {
            buttonElement.setAttribute('tabindex', '0');
        }
        if (buttonElement.getAttribute('aria-disabled') !== 'false') {
            buttonElement.setAttribute('aria-disabled', 'false');
        }
    };
    apply();

    const timers = [];
    if (ownerWindow && typeof ownerWindow.setTimeout === 'function') {
        timers.push(ownerWindow.setTimeout(apply, 0));
        timers.push(ownerWindow.setTimeout(apply, 250));
    }
    const Observer = ownerWindow && ownerWindow.MutationObserver;
    const observer = typeof Observer === 'function'
        ? new Observer(apply)
        : null;
    if (observer) {
        observer.observe(buttonElement, {
            attributes: true,
            attributeFilter: ['role', 'tabindex', 'aria-disabled'],
        });
    }
    return () => {
        if (observer) observer.disconnect();
        if (ownerWindow && typeof ownerWindow.clearTimeout === 'function') {
            timers.forEach((timer) => ownerWindow.clearTimeout(timer));
        }
    };
}

function _topWindowOffset(sourceWindow, topWindow) {
    let left = 0;
    let top = 0;
    let current = sourceWindow;
    while (current && current !== topWindow) {
        try {
            const frameElement = current.frameElement;
            if (!frameElement) break;
            const frameRect = frameElement.getBoundingClientRect();
            left += frameRect.left;
            top += frameRect.top;
            current = frameElement.ownerDocument
                && frameElement.ownerDocument.defaultView;
        } catch (e) {
            break;
        }
    }
    return { left, top };
}

function _elementRectInTopWindow(element, topWindow) {
    const rect = element.getBoundingClientRect();
    const ownerWindow = element.ownerDocument
        && element.ownerDocument.defaultView;
    const offset = _topWindowOffset(ownerWindow, topWindow);
    return {
        top: rect.top + offset.top,
        left: rect.left + offset.left,
        bottom: rect.bottom + offset.top,
        right: rect.right + offset.left,
    };
}

function positionClDisplayMenuNearPointer(menuElement, event, triggerElement, topWindow) {
    const targetWindow = topWindow || window.top || window;
    const targetDocument = targetWindow.document;
    const triggerRect = _elementRectInTopWindow(triggerElement, targetWindow);
    const eventWindow = (event && event.view)
        || (event && event.target && event.target.ownerDocument
            && event.target.ownerDocument.defaultView)
        || (triggerElement.ownerDocument && triggerElement.ownerDocument.defaultView);
    const offset = _topWindowOffset(eventWindow, targetWindow);
    const keyboardLikeClick = event
        && event.type === 'click'
        && Number(event.detail) === 0
        && Number(event.clientX) === 0
        && Number(event.clientY) === 0;
    const hasPointer = event
        && Number.isFinite(event.clientX)
        && Number.isFinite(event.clientY)
        && !keyboardLikeClick;
    const pointer = hasPointer
        ? { left: event.clientX + offset.left, top: event.clientY + offset.top }
        : { left: triggerRect.left, top: triggerRect.bottom };
    const menuRect = menuElement.getBoundingClientRect();
    const viewportWidth = targetWindow.innerWidth
        || targetDocument.documentElement.clientWidth;
    const viewportHeight = targetWindow.innerHeight
        || targetDocument.documentElement.clientHeight;
    const scrollLeft = targetWindow.scrollX || targetWindow.pageXOffset || 0;
    const scrollTop = targetWindow.scrollY || targetWindow.pageYOffset || 0;
    const margin = 8;
    const gap = 12;
    const maxLeft = Math.max(margin, viewportWidth - menuRect.width - margin);
    const maxTop = Math.max(margin, viewportHeight - menuRect.height - margin);
    let clientLeft = pointer.left + gap;
    let clientTop = pointer.top + gap;
    if (clientLeft + menuRect.width > viewportWidth - margin) {
        clientLeft = pointer.left - menuRect.width - gap;
    }
    if (clientTop + menuRect.height > viewportHeight - margin) {
        clientTop = pointer.top - menuRect.height - gap;
    }
    clientLeft = Math.min(maxLeft, Math.max(margin, clientLeft));
    clientTop = Math.min(maxTop, Math.max(margin, clientTop));
    menuElement.style.left = (clientLeft + scrollLeft) + 'px';
    menuElement.style.top = (clientTop + scrollTop) + 'px';
    return {
        left: clientLeft + scrollLeft,
        top: clientTop + scrollTop,
        anchor: hasPointer ? 'pointer' : 'trigger',
    };
}

function clampClDisplayMenuToViewport(menuElement, topWindow) {
    if (!menuElement) return null;
    const targetWindow = topWindow || window.top || window;
    const targetDocument = targetWindow.document;
    const menuRect = menuElement.getBoundingClientRect();
    const viewportWidth = targetWindow.innerWidth
        || targetDocument.documentElement.clientWidth;
    const viewportHeight = targetWindow.innerHeight
        || targetDocument.documentElement.clientHeight;
    const scrollLeft = targetWindow.scrollX || targetWindow.pageXOffset || 0;
    const scrollTop = targetWindow.scrollY || targetWindow.pageYOffset || 0;
    const margin = 8;
    const maxLeft = Math.max(margin, viewportWidth - menuRect.width - margin);
    const maxTop = Math.max(margin, viewportHeight - menuRect.height - margin);
    const clientLeft = Math.min(maxLeft, Math.max(margin, menuRect.left));
    const clientTop = Math.min(maxTop, Math.max(margin, menuRect.top));
    menuElement.style.left = (clientLeft + scrollLeft) + 'px';
    menuElement.style.top = (clientTop + scrollTop) + 'px';
    return { left: clientLeft + scrollLeft, top: clientTop + scrollTop };
}

function bindClDisplayMenuViewportGuard(menuElement, topWindow) {
    if (!menuElement) return () => {};
    const targetWindow = topWindow || window.top || window;
    if (!targetWindow || typeof targetWindow.addEventListener !== 'function') return () => {};
    const clamp = () => clampClDisplayMenuToViewport(menuElement, targetWindow);
    targetWindow.addEventListener('resize', clamp);
    targetWindow.addEventListener('orientationchange', clamp);
    return () => {
        targetWindow.removeEventListener('resize', clamp);
        targetWindow.removeEventListener('orientationchange', clamp);
    };
}

function bindClDisplayMenuDrag(menuElement, handleElement, topWindow) {
    if (!menuElement || !handleElement) return () => {};
    const targetWindow = topWindow || window.top || window;
    const targetDocument = targetWindow.document;
    let dragState = null;

    const clamp = (value, minimum, maximum) => (
        Math.min(Math.max(minimum, value), Math.max(minimum, maximum))
    );
    const move = (event) => {
        if (!dragState) return;
        if (
            Number.isFinite(dragState.pointerId)
            && Number.isFinite(event.pointerId)
            && event.pointerId !== dragState.pointerId
        ) return;
        const viewportWidth = targetWindow.innerWidth
            || targetDocument.documentElement.clientWidth;
        const viewportHeight = targetWindow.innerHeight
            || targetDocument.documentElement.clientHeight;
        const scrollLeft = targetWindow.scrollX || targetWindow.pageXOffset || 0;
        const scrollTop = targetWindow.scrollY || targetWindow.pageYOffset || 0;
        const margin = 8;
        const left = clamp(
            dragState.menuLeft + event.clientX - dragState.clientX,
            margin,
            viewportWidth - dragState.width - margin,
        );
        const top = clamp(
            dragState.menuTop + event.clientY - dragState.clientY,
            margin,
            viewportHeight - dragState.height - margin,
        );
        menuElement.style.left = (left + scrollLeft) + 'px';
        menuElement.style.top = (top + scrollTop) + 'px';
        if (typeof event.preventDefault === 'function') event.preventDefault();
    };
    const restoreSelection = (state) => {
        if (!state || !state.selectionStyle) return;
        const rootStyle = targetDocument.documentElement.style;
        const previous = state.selectionStyle;
        if (previous.value) {
            rootStyle.setProperty('user-select', previous.value, previous.priority || '');
        } else {
            rootStyle.removeProperty('user-select');
        }
    };
    const finish = (event) => {
        if (!dragState) return;
        if (
            event
            && Number.isFinite(dragState.pointerId)
            && Number.isFinite(event.pointerId)
            && event.pointerId !== dragState.pointerId
        ) return;
        const completedState = dragState;
        dragState = null;
        const pointerId = event && Number.isFinite(event.pointerId)
            ? event.pointerId
            : completedState.pointerId;
        try {
            if (
                Number.isFinite(pointerId)
                && typeof handleElement.releasePointerCapture === 'function'
            ) handleElement.releasePointerCapture(pointerId);
        } catch (e) { /* pointer capture may already be released */ }
        handleElement.style.cursor = 'grab';
        restoreSelection(completedState);
    };
    const start = (event) => {
        if (event.button !== undefined && event.button !== 0) return;
        if (dragState) return;
        const rect = menuElement.getBoundingClientRect();
        const rootStyle = targetDocument.documentElement.style;
        dragState = {
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
            menuLeft: rect.left,
            menuTop: rect.top,
            width: rect.width,
            height: rect.height,
            selectionStyle: {
                value: typeof rootStyle.getPropertyValue === 'function'
                    ? rootStyle.getPropertyValue('user-select')
                    : '',
                priority: typeof rootStyle.getPropertyPriority === 'function'
                    ? rootStyle.getPropertyPriority('user-select')
                    : '',
            },
        };
        handleElement.style.cursor = 'grabbing';
        rootStyle.setProperty('user-select', 'none');
        try {
            if (
                Number.isFinite(event.pointerId)
                && typeof handleElement.setPointerCapture === 'function'
            ) handleElement.setPointerCapture(event.pointerId);
        } catch (e) { /* pointer capture is an enhancement, not a requirement */ }
        if (typeof event.preventDefault === 'function') event.preventDefault();
    };
    const moveWithKeyboard = (event) => {
        const step = event && event.shiftKey ? 40 : 10;
        const deltas = {
            ArrowLeft: [-step, 0],
            ArrowRight: [step, 0],
            ArrowUp: [0, -step],
            ArrowDown: [0, step],
        };
        const delta = event && deltas[event.key];
        if (!delta) return;
        const rect = menuElement.getBoundingClientRect();
        const scrollLeft = targetWindow.scrollX || targetWindow.pageXOffset || 0;
        const scrollTop = targetWindow.scrollY || targetWindow.pageYOffset || 0;
        menuElement.style.left = (rect.left + delta[0] + scrollLeft) + 'px';
        menuElement.style.top = (rect.top + delta[1] + scrollTop) + 'px';
        clampClDisplayMenuToViewport(menuElement, targetWindow);
        if (typeof event.preventDefault === 'function') event.preventDefault();
        if (typeof event.stopPropagation === 'function') event.stopPropagation();
    };

    handleElement.addEventListener('pointerdown', start);
    handleElement.addEventListener('keydown', moveWithKeyboard);
    handleElement.addEventListener('lostpointercapture', finish);
    targetDocument.addEventListener('pointermove', move, true);
    targetDocument.addEventListener('pointerup', finish, true);
    targetDocument.addEventListener('pointercancel', finish, true);
    if (typeof targetWindow.addEventListener === 'function') {
        targetWindow.addEventListener('blur', finish);
    }
    return () => {
        finish();
        handleElement.removeEventListener('pointerdown', start);
        handleElement.removeEventListener('keydown', moveWithKeyboard);
        handleElement.removeEventListener('lostpointercapture', finish);
        targetDocument.removeEventListener('pointermove', move, true);
        targetDocument.removeEventListener('pointerup', finish, true);
        targetDocument.removeEventListener('pointercancel', finish, true);
        if (typeof targetWindow.removeEventListener === 'function') {
            targetWindow.removeEventListener('blur', finish);
        }
    };
}

// 只接受当前 schema；调用方必须显式丢弃非当前配置。
function normalizeClShowConfig(config, interval) {
    const source = config === null || config === undefined
        ? CL_SHOW_DEFAULT
        : config;
    if (
        !source
        || typeof source !== 'object'
        || Array.isArray(source)
        || source.schema !== CL_SHOW_DEFAULT.schema
    ) {
        throw new TypeError('cl_show_config_current_schema_required');
    }
    const has = (key) => Object.prototype.hasOwnProperty.call(source, key);
    const output = Object.assign({}, CL_SHOW_DEFAULT);
    for (const key of [
        'fx', 'bi', 'xd', 'center_observation', 'center_all',
        'center_provisional', 'trend_all', 'pending_movement',
        'point_all', 'point_1buy', 'point_2buy', 'point_3buy',
        'point_1sell', 'point_2sell', 'point_3sell', 'divergence_all',
    ]) {
        if (has(key)) output[key] = source[key] !== false;
    }
    for (const { level } of recursiveDisplayLevels(interval)) {
        const centerKey = `center_L${level}`;
        const trendKey = `trend_L${level}`;
        const pointKey = `point_L${level}`;
        output[centerKey] = has(centerKey) ? source[centerKey] !== false : true;
        output[trendKey] = has(trendKey) ? source[trendKey] !== false : true;
        output[pointKey] = has(pointKey) ? source[pointKey] !== false : true;
        for (const kind of ['consolidation', 'trend']) {
            const divergenceKey = `divergence_${kind}_L${level}`;
            output[divergenceKey] = has(divergenceKey)
                ? source[divergenceKey] !== false
                : true;
        }
    }
    return output;
}

function strictItemEnabled(cfg, item) {
    const config = cfg || {};
    const level = item.structural_level;
    if (item.render_kind === 'center_observation') return config.center_observation === true;
    if (item.render_kind === 'formal_center') {
        return config.center_all === true && config[`center_L${level}`] !== false;
    }
    if (item.render_kind === 'center_preview' || item.render_kind === 'center_projection') {
        return config.center_all === true
            && config.center_provisional === true
            && config[`center_L${level}`] !== false;
    }
    if (item.render_kind === 'strict_trend') {
        return config.trend_all !== false && config[`trend_L${level}`] !== false;
    }
    if (item.render_kind === 'pending_movement') {
        return config.trend_all !== false
            && config.pending_movement === true
            && config[`trend_L${level}`] !== false;
    }
    if (item.render_kind === 'point_confirmed' || item.render_kind === 'point_approaching') {
        return config.point_all !== false
            && config[`point_L${level}`] !== false
            && config[`point_${item.point_type}`] !== false;
    }
    if (item.render_kind === 'strict_divergence') {
        return config.divergence_all !== false
            && config[`divergence_${item.kind}_L${level}`] !== false;
    }
    return false;
}

function strictStringArray(value) {
    return Array.isArray(value)
        && value.every((item) => typeof item === 'string' && item.length > 0);
}

function strictSameIds(left, right) {
    return left.length === right.length
        && left.every((item, index) => item === right[index]);
}

function validateStrictCenterRenderContract(item, level, allowPartialPhysical) {
    if (
        !item || item.schema !== 'chanlun-chart-center'
        || item.structural_level !== level
        || !['segment', 'stroke_observation', 'trend_type'].includes(item.source_kind)
        || !strictStringArray(item.core_unit_ids)
        || item.core_unit_ids.length !== 3
        || new Set(item.core_unit_ids).size !== 3
        || item.core_component_count !== 3
        || !strictStringArray(item.initial_unit_ids)
        || !strictSameIds(item.initial_unit_ids, item.core_unit_ids)
        || !strictStringArray(item.establishment_segment_ids)
    ) throw new Error('strict center core contract is invalid');

    if (item.source_kind === 'trend_type') {
        if (
            item.establishment_leave_unit_id !== null
            || item.initial_exit_unit_id !== null
            || item.minimum_lifecycle_role_count !== 3
            || item.lifecycle_role_count < 3
            || item.establishment_component_count !== 3
            || item.overlap_component_count < 3
            || !strictSameIds(item.establishment_segment_ids, item.core_unit_ids)
        ) throw new Error('recursive center contract is invalid');
        return;
    }

    const entryId = item.entry_unit_id;
    const leaveId = item.establishment_leave_unit_id;
    const partial = allowPartialPhysical === true
        && item.render_kind === 'center_preview'
        && item.state === 'forming'
        && leaveId === null;
    if (
        typeof entryId !== 'string' || !entryId
        || (!partial && (typeof leaveId !== 'string' || !leaveId))
        || (partial && item.initial_exit_unit_id !== null)
        || (!partial && item.initial_exit_unit_id !== leaveId)
        || item.minimum_lifecycle_role_count !== 5
    ) throw new Error('physical center entry/leave contract is invalid');
    const expected = [entryId, ...item.core_unit_ids];
    if (!partial) expected.push(leaveId);
    if (
        new Set(expected).size !== expected.length
        || !strictSameIds(item.establishment_segment_ids, expected)
        || item.establishment_component_count !== expected.length
        || item.overlap_component_count < expected.length
        || item.lifecycle_role_count < expected.length
    ) throw new Error('physical center five-role overlap contract is invalid');
}

function validatePendingMovementRenderContract(item, level, formalIds, pendingIds) {
    if (
        !item || item.schema !== 'chanlun-chart-pending-movement'
        || item.render_kind !== 'pending_movement'
        || item.structural_level !== level
        || item.state !== 'pending'
        || item.classification !== 'unresolved'
        || !['entire_stream', 'prefix', 'bridge', 'suffix'].includes(item.role)
        || !['up', 'down'].includes(item.direction)
        || item.geometric_direction !== item.direction
        || item.semantic_direction !== null
        || item.direction_status !== 'pending'
        || item.formal_direction_confirmed !== false
        || item.tradable !== false
        || item.recursive_eligible !== false
        || item.divergence_eligible !== false
        || !strictStringArray(item.constituent_unit_ids)
        || item.constituent_unit_ids.length === 0
        || new Set(item.constituent_unit_ids).size !== item.constituent_unit_ids.length
    ) throw new Error('pending movement contract is invalid');
    for (const unitId of item.constituent_unit_ids) {
        if (formalIds.has(unitId) || pendingIds.has(unitId)) {
            throw new Error('formal and pending movement ownership overlaps');
        }
        pendingIds.add(unitId);
    }
}

// resolution 归一为存储 key 后缀:去空白转小写(1D/1d 同一份),空/未知回退哨兵 '_'。
function _resolutionKey(resolution) {
    const r = (resolution === null || resolution === undefined) ? '' : String(resolution).trim();
    return r ? r.toLowerCase() : '_';
}

function loadClShowConfig(chartId, resolution) {
    const storageKey = 'cl_show_config_' + chartId + '_' + _resolutionKey(resolution);
    try {
        const raw = localStorage.getItem(storageKey);
        if (raw) {
            const parsed = JSON.parse(raw);
            return normalizeClShowConfig(parsed, resolution);
        }
    } catch (e) {
        localStorage.removeItem(storageKey);
        console.warn('[CHARTS] invalid cl_show_config removed', e);
    }
    // 该周期从未配置 → null 哨兵，由 resolveClConfigForResolution 决定继承。
    return null;
}

function saveClShowConfig(chartId, resolution, cfg) {
    const normalized = normalizeClShowConfig(cfg, resolution);
    try {
        localStorage.setItem(
            'cl_show_config_' + chartId + '_' + _resolutionKey(resolution),
            JSON.stringify(normalized),
        );
    } catch (e) {
        console.warn('[CHARTS] saveClShowConfig failed', e);
    }
}

// 按周期解析应用配置:该周期已配过 → 用存储值(persist=false);未配过 → 继承切换前当前配置的副本,
// 无当前配置则使用当前生产默认值；persist=true 表示需固化到该周期 key。
function resolveClConfigForResolution(chartId, resolution, currentCfg) {
    const loaded = loadClShowConfig(chartId, resolution);
    if (loaded !== null) {
        return { cfg: loaded, persist: false };
    }
    const base = currentCfg
        ? normalizeClShowConfig(currentCfg, resolution)
        : normalizeClShowConfig(null, resolution);
    return { cfg: base, persist: true };
}

function loadClIndependentDrawings(chartId) {
    try {
        const raw = localStorage.getItem('cl_independent_drawings_' + chartId);
        if (raw !== null) {
            return JSON.parse(raw) === true;
        }
    } catch (e) {
        console.warn('[CHARTS] loadClIndependentDrawings parse failed', e);
    }
    return false;
}

function saveClIndependentDrawings(chartId, val) {
    try {
        localStorage.setItem('cl_independent_drawings_' + chartId, JSON.stringify(!!val));
    } catch (e) {
        console.warn('[CHARTS] saveClIndependentDrawings failed', e);
    }
}

const CHART_CONFIG = {
    COLORS: {
        BI: "#C026D3", XD: "#2563EB",
        AREA_POS: "#ef5350", AREA_NEG: "#26a69a",
    },
    LINE_STYLES: { SOLID: 0, DOTTED: 1, DASHED: 2 },
    CHART_TYPES: ["fxs", "bis", "xds"],
};

// 方向性标记必须同时适配 TradingView 浅色、深色画布。结构级别继续使用下方
// LEVEL_COLOR_CHAIN；这里的红/蓝只表达“买/卖、顶/底”等方向语义，不能拿来表达级别。
const SIGNAL_COLOR_THEMES = Object.freeze({
    light: Object.freeze({
        fractalTop: "#B91C1C",
        fractalBottom: "#0369A1",
        buy: "#C2410C",
        sell: "#1D4ED8",
        neutralSurface: "#E2E8F0",
        neutralText: "#1E293B",
    }),
    dark: Object.freeze({
        fractalTop: "#FB7185",
        fractalBottom: "#38BDF8",
        buy: "#FB923C",
        sell: "#60A5FA",
        neutralSurface: "#334155",
        neutralText: "#F8FAFC",
    }),
});

// 视觉编码约束：颜色=绝对结构级别/交易方向，线宽=对象权重，线型=完成状态，
// 透明度=证据确定性。所有数值集中在这里，避免各绘制分支再次产生互相矛盾的魔法数。
const CHANLUN_VISUAL_STYLE = Object.freeze({
    fractal: Object.freeze({ linewidth: 2 }),
    center: Object.freeze({
        formal: Object.freeze({ linewidth: 1, completedTransparency: 92, ongoingTransparency: 96 }),
        preview: Object.freeze({ linewidth: 1, completedTransparency: 96, ongoingTransparency: 100 }),
        observation: Object.freeze({ linewidth: 1, completedTransparency: 100, ongoingTransparency: 100 }),
        projection: Object.freeze({ linewidth: 1, completedTransparency: 100, ongoingTransparency: 100 }),
    }),
    trend: Object.freeze({ linewidth: 1, completedTransparency: 12, formingTransparency: 30 }),
    point: Object.freeze({ fontsize: 12, higherFontsize: 13, approachingFontsize: 11 }),
    divergence: Object.freeze({ consolidationFontsize: 12, trendFontsize: 13 }),
});

function normalizeChartTheme(theme) {
    return String(theme || "").trim().toLowerCase() === "dark" ? "dark" : "light";
}

function currentChartTheme() {
    try {
        if (typeof Utils !== "undefined" && typeof Utils.get_local_data === "function") {
            const configured = Utils.get_local_data("theme");
            if (String(configured || "").trim().toLowerCase() === "dark") return "dark";
            if (String(configured || "").trim().toLowerCase() === "light") return "light";
        }
    } catch (e) { /* 回退到 TradingView 本地状态 */ }
    try {
        if (typeof localStorage !== "undefined") {
            const raw = typeof localStorage.getItem === "function"
                ? localStorage.getItem("tv_chart")
                : localStorage.tv_chart;
            const stored = raw ? JSON.parse(raw) : {};
            return normalizeChartTheme(stored.theme);
        }
    } catch (e) { /* 回退浅色 */ }
    return "light";
}

function getSignalColor(role, theme = currentChartTheme()) {
    const palette = SIGNAL_COLOR_THEMES[normalizeChartTheme(theme)];
    return palette[role] || palette.neutralText;
}

function _centerIsOngoing(item) {
    const state = String(item?.state || "").toLowerCase();
    const hasLineStyle = item?.linestyle !== undefined && item?.linestyle !== null;
    return state === "ongoing" || state === "forming"
        || (hasLineStyle && parseInt(item.linestyle) !== 0);
}

function getCenterVisualStyle(role, item = {}) {
    const spec = CHANLUN_VISUAL_STYLE.center[role] || CHANLUN_VISUAL_STYLE.center.formal;
    const ongoing = _centerIsOngoing(item);
    let linestyle = ongoing ? CHART_CONFIG.LINE_STYLES.DASHED : CHART_CONFIG.LINE_STYLES.SOLID;
    if (role === "projection") linestyle = CHART_CONFIG.LINE_STYLES.DOTTED;
    return {
        linewidth: spec.linewidth,
        transparency: ongoing ? spec.ongoingTransparency : spec.completedTransparency,
        linestyle,
    };
}

function getTrendVisualStyle(item = {}) {
    const forming = String(item.state || "").toLowerCase() === "forming";
    const directionStatus = String(item.direction_status || "").toLowerCase();
    if (directionStatus === "awaiting_reversal_support") {
        return {
            linewidth: CHANLUN_VISUAL_STYLE.trend.linewidth,
            transparency: 62,
            linestyle: CHART_CONFIG.LINE_STYLES.DOTTED,
        };
    }
    if (directionStatus === "consolidation") {
        return {
            linewidth: CHANLUN_VISUAL_STYLE.trend.linewidth,
            transparency: 70,
            linestyle: CHART_CONFIG.LINE_STYLES.DOTTED,
        };
    }
    if (directionStatus === "geometric_candidate") {
        return {
            linewidth: CHANLUN_VISUAL_STYLE.trend.linewidth,
            transparency: 50,
            linestyle: CHART_CONFIG.LINE_STYLES.DASHED,
        };
    }
    if (directionStatus === "ended") {
        // 历史走势只证明几何分解已经结束，不代表当前反转获得了一、二类点
        // 支撑。它必须弱于当前正式方向，避免实线让人误判为无点反转。
        return {
            linewidth: CHANLUN_VISUAL_STYLE.trend.linewidth,
            transparency: 46,
            linestyle: CHART_CONFIG.LINE_STYLES.DASHED,
        };
    }
    return {
        linewidth: CHANLUN_VISUAL_STYLE.trend.linewidth,
        transparency: forming
            ? CHANLUN_VISUAL_STYLE.trend.formingTransparency
            : CHANLUN_VISUAL_STYLE.trend.completedTransparency,
        linestyle: forming ? CHART_CONFIG.LINE_STYLES.DASHED : CHART_CONFIG.LINE_STYLES.SOLID,
    };
}

const POINT_TYPE_LABELS = Object.freeze({
    "1buy": "一买", "2buy": "二买", "3buy": "三买",
    "1sell": "一卖", "2sell": "二卖", "3sell": "三卖",
});
const STRICT_POINT_TYPES = new Set(Object.keys(POINT_TYPE_LABELS));

function pointTypeLabel(pointType) {
    const value = String(pointType || "");
    return POINT_TYPE_LABELS[value.toLowerCase()] || value;
}

function getStrictPointVisual(item = {}) {
    const pointType = String(item.point_type || "").toLowerCase();
    const isBuy = String(item.side || "").toLowerCase() === "buy" || pointType.includes("buy");
    const declaredFormation = ["forming", "geometry_ready", "formed", "confirmed"].includes(item.formation_state)
        ? (item.formation_state === "formed" ? "geometry_ready" : item.formation_state)
        : "";
    const confirmed = declaredFormation
        ? declaredFormation === "confirmed"
        : item.render_kind === "point_confirmed";
    const geometryCandidate = declaredFormation === "geometry_ready";
    const level = Number.isInteger(item.structural_level) ? item.structural_level : 0;
    const levelLabel = item.level_label || `L${level}`;
    const fontsize = confirmed
        ? (level > 0 ? CHANLUN_VISUAL_STYLE.point.higherFontsize : CHANLUN_VISUAL_STYLE.point.fontsize)
        : CHANLUN_VISUAL_STYLE.point.approachingFontsize;
    return {
        color: getSignalColor(isBuy ? "buy" : "sell"),
        fontsize,
        bold: confirmed,
        transparency: confirmed ? 0 : 45,
        text: `${isBuy ? "▲" : "▼"}${confirmed ? "" : geometryCandidate ? "候选待锁·" : "接近·"}${levelLabel}·${pointTypeLabel(pointType)}`,
    };
}

function getStrictDivergenceVisual(item = {}) {
    const bullish = item.direction === "down";
    const trend = item.kind === "trend";
    const level = Number.isInteger(item.structural_level) ? item.structural_level : 0;
    const levelLabel = item.level_label || `L${level}`;
    return {
        color: getSignalColor(bullish ? "buy" : "sell"),
        fontsize: trend
            ? CHANLUN_VISUAL_STYLE.divergence.trendFontsize
            : CHANLUN_VISUAL_STYLE.divergence.consolidationFontsize,
        bold: trend,
        text: `${bullish ? "▲" : "▼"}${levelLabel}·${trend ? "趋势背驰" : "盘整背驰"}`,
    };
}

// 基础结构保留“笔细、线段粗”的第二重视觉层级；颜色由下面的绝对递归级别色链决定，
// 因而 1m 线段与 5m 笔、5m 线段与 30m 笔始终同色。这里只影响显示，不改变结构计算。
const BASE_STRUCTURE_LINE_WIDTHS = Object.freeze({
    bis: 1,
    xds: 2,
});

function getBaseStructureStyle(interval, elementType) {
    return {
        color: getDynamicColor(interval, elementType),
        linewidth: BASE_STRUCTURE_LINE_WIDTHS[elementType] || 1,
    };
}

const DEFAULT_COLORS = {
    bis: CHART_CONFIG.COLORS.BI, xds: CHART_CONFIG.COLORS.XD,
};

// ─────────────────────────────────────────────────────────────────────────
// 绝对递归级别色链：每个绝对级别一个固定颜色，**同一绝对级别在任何周期图上恒同色**。
// 例如 1m 线段与 5m 笔都落在 index 2；5m 线段与 30m 笔都落在 index 3。
// 相邻级别采用跨色相、高饱和且兼顾明暗主题的颜色，避免旧橙/黄组合在密集 K 线上混淆。
//   index: 0=15秒(白) 1=1FB品红 2=1FC蓝 3=1F橙 4=5F青绿 5=30F紫
//          6=日琥珀 7=周青 8=月玫红 9=季橄榄绿
const LEVEL_COLOR_CHAIN = [
    "#FFFFFF", // 0  15秒(占位,基本不作基础色)
    "#C026D3", // 1  1FB  品红 —— 1分钟笔
    "#2563EB", // 2  1FC  皇家蓝 —— 1分钟线段 / 5分钟笔
    "#EA580C", // 3  1F   深橙 —— 5分钟线段 / 30分钟笔
    "#0F766E", // 4  5F   青绿 —— 30分钟线段 / 日线笔
    "#7C3AED", // 5  30F  紫 —— 日线线段 / 周线笔
    "#B45309", // 6  日线 琥珀 —— 周线线段 / 月线笔
    "#0891B2", // 7  周线 青
    "#BE185D", // 8  月线 玫红
    "#4D7C0F", // 9  季线 橄榄绿
];
// 按链索引取色:溢出(深递归 > 9)在 [1..9] 区间循环,既不 undefined 又仍可辨。
function chainColor(idx) {
    if (idx <= 0) return LEVEL_COLOR_CHAIN[0];
    if (idx < LEVEL_COLOR_CHAIN.length) return LEVEL_COLOR_CHAIN[idx];
    const span = LEVEL_COLOR_CHAIN.length - 1; // 9
    return LEVEL_COLOR_CHAIN[1 + ((idx - 1) % span)];
}

// 图周期 → 该图「笔」在链上的索引 p：1m=1、5m/15m=2、30m/60m=3、
// 日线=4、周线=5、月线=6。非标准周期按最接近的操作级别共享颜色锚点。
const CHART_BI_INDEX = { "1": 1, "5": 2, "15": 2, "30": 3, "60": 3, "1D": 4, "1W": 5, "1M": 6 };
function chartBiIndex(interval) {
    const p = CHART_BI_INDEX[interval];
    return (typeof p === "number") ? p : 1;
}

// 当前周期 → 各递归级别(L0/L1/L2/L3)的周期标签链。模块级:菜单与左侧级别快捷开关浮条共用。
const FREQ_CHAIN = {
    "1": ["1m", "5m", "30m", "日线"],
    "5": ["5m", "30m", "日线", "周线"],
    "15": ["15m", "60m", "日线", "周线"],
    "30": ["30m", "日线", "周线", "月线"],
    "60": ["60m", "日线", "周线", "月线"],
    "1D": ["日线", "周线", "月线", "年线"],
    "1W": ["周线", "月线", "年线", "10年"],
    "1M": ["月线", "年线", "10年", "30年"],
};

// 基础元素相对「笔」的链偏移。
const ELEMENT_CHAIN_OFFSET = { bis: 0, xds: 1 };

// 基础元素(笔/线段/笔中枢/线段中枢)按当前周期取链色。替代旧 DYNAMIC_CHART_COLORS。
function getDynamicColor(interval, elementType) {
    const off = ELEMENT_CHAIN_OFFSET[elementType];
    if (typeof off === "number") return chainColor(chartBiIndex(interval) + off);
    return DEFAULT_COLORS[elementType] || "#FFFFFF";
}

// 递归层级中枢 Lk(L0 = 本周期线段中枢) → 链色 C[p+2+k]。
// 1m 图：L0=深橙、L1=青绿、L2=紫、L3=琥珀。
function getRecursiveLevelColor(interval, level) {
    return chainColor(chartBiIndex(interval) + 2 + (level || 0));
}

// 递归层级走势类型与本级中枢同色；形状区分走势与中枢。

function debounce(func, wait) {
    let timeout;
    return function (...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}

function getTVRegistry() {
    if (!window.ChanlunTVRegistry) {
        window.ChanlunTVRegistry = {
            chartManagers: new Map(),
            datafeeds: new Map(),
            widgets: new Map(),
            activeManagerId: null,
        };
    }
    return window.ChanlunTVRegistry;
}

const ChartUtils = {
    createShape(chart, points, options = {}) {
        const defaults = {
            lock: true, disableSelection: true, disableSave: true, disableUndo: true,
            showInObjectsTree: false, overrides: {},
        };
        const config = { ...defaults, ...options };
        try {
            if (!chart) return Promise.reject("Chart object is null");

            return config.shape === "trend_line" || config.shape === "rectangle" || config.shape === "circle"
                ? chart.createMultipointShape(points, config)
                : chart.createShape(points, config);
        } catch (e) {
            console.error("[DEBUG-CHARTS] Shape create failed:", e);
            return Promise.reject(e);
        }
    },
    createFxShape(chart, fx, options = {}) {
        const { overrides = {}, ...shapeOptions } = options;
        const color = getSignalColor(fx.text === "ding" ? "fractalTop" : "fractalBottom");
        return this.createShape(chart, fx.points, {
            shape: "circle",
            ...shapeOptions,
            overrides: {
                backgroundColor: color,
                color,
                linecolor: color,
                linewidth: CHANLUN_VISUAL_STYLE.fractal.linewidth,
                transparency: 0,
                ...overrides,
            },
        });
    },
    createLineShape(chart, line, options = {}) {
        const {
            overrides = {},
            linewidth = 1,
            color = CHART_CONFIG.COLORS.BI,
            ...shapeOptions
        } = options;
        return this.createShape(chart, line.points, {
            shape: "trend_line",
            ...shapeOptions,
            overrides: {
                linestyle: parseInt(line.linestyle) || 0,
                linewidth,
                linecolor: color,
                transparency: 0,
                ...overrides,
            },
        });
    },
    createZhongshuShape(chart, zs, options = {}) {
        const { overrides = {}, ...shapeOptions } = options;
        const color = shapeOptions.color || CHART_CONFIG.COLORS.BI;
        const defaultStyle = getCenterVisualStyle("frequency", zs);
        const linewidth = shapeOptions.linewidth || defaultStyle.linewidth;
        return this.createShape(chart, zs.points, {
            shape: "rectangle",
            ...shapeOptions,
            overrides: {
                linestyle: defaultStyle.linestyle,
                linewidth,
                linecolor: color,
                backgroundColor: color,
                transparency: defaultStyle.transparency,
                color,
                "trendline.linecolor": color,
                fillBackground: true,
                filled: true,
                ...overrides,
            },
        });
    },
};

function getInitialChartInterval(chartId) {
    try {
        const bootstrap = window.__chanlunUrlBootstrap;
        const index = Number.parseInt(String(chartId), 10) - 1;
        if (bootstrap && Array.isArray(bootstrap.intervals) && index >= 0) {
            const interval = bootstrap.intervals[index];
            if (typeof interval === "string" && interval) return interval;
        }
    } catch (e) { /* 回退到当前用户保存的周期 */ }
    try {
        const market = Utils.get_market();
        return Utils.get_local_data(`${market}_interval_${chartId}`) || "1";
    } catch (e) {
        return "1";
    }
}

function shouldLoadLastChart() {
    try {
        return !window.__chanlunUrlBootstrap;
    } catch (e) {
        return true;
    }
}

class ChartManager {
    constructor(id) {
        this.id = id;
        this.instanceId = `chart-manager-${id}`;
        this.obj_charts = {};
        this.widget = null;
        this.udf_datafeed = null;
        this.chart = null;
        this.debouncedDrawChanlun = debounce(() => this.draw_chanlun(), 300);
        this.macdStudyId = null;
        this._defaultStudiesPromise = null;
        this._defaultStudyIds = new Map();
        // 每个图表面板独立维护缠论显示配置与独立周期画线开关
        // 初始 resolution:优先本地已记录(切过周期即有),回退 "1";据此载入该周期显示配置。
        let _initRes = '1';
        try {
            const _saved = getInitialChartInterval(this.id);
            if (_saved) _initRes = _saved;
        } catch (e) { /* 回退默认 '1' */ }
        this._curResolution = _initRes;
        const _r0 = resolveClConfigForResolution(this.id, _initRes, null);
        this.cl_show_config = _r0.cfg;
        if (_r0.persist) saveClShowConfig(this.id, _initRes, _r0.cfg);
        this.cl_independent_drawings = loadClIndependentDrawings(this.id);
        this._initialLoadDone = false; // 首次数据就绪前屏蔽 visibleRangeChange 重绘
        // 当前标的/周期的数据就绪代际。bars_result 更新早于 TradingView 接收 K 线，
        // 因此首次算法图形必须等当前代际 dataReady() 为 true 后才能创建。
        this._dataContextGeneration = 0;
        this._tvDataReadyGeneration = -1;
        this._tvDataReadyIdentity = null;
        this._pendingChanlunDrawGeneration = null;
        this._pendingChanlunDrawIdentity = null;
        this._dataReadyProbeGeneration = null;
        this._dataReadyProbeIdentity = null;
        this._intervalGeneration = 0;
        this._drawingsCache = new Map();  // 按 symbol+interval 缓存用户画线状态
        // 记录服务端已确认的画线状态，而不是仅记录当前画布状态。TradingView 会在
        // 初始化完成约 5 秒后触发 auto-save；只有和这份基线真正不同时才允许写入。
        this._persistedDrawingFingerprints = new Map();
        this._drawingLoadsInFlight = new Map();
        this._intervalSwitchSeq = 0;
        this._drawingsRequestSeq = 0;
        this._latestAppliedBarTime = null;
        this._is_switching_interval = false;
        this._is_drawing_chanlun = false;
        this._drawingStatePhase = null;
        this._activeDrawingMutations = new Set();
        this.isApplyingDrawingState = false;
        this._pendingSaveState = null;
        this._saveScheduled = false;
        this._saveInFlight = null;
        this._drawingSaveInFlight = false;
        this._drawingSavePending = null;
        this._barReadyHandler = null;
        this._sse = null;  // SSE 实时推送连接(每图表一个; flag 关/不支持时为 null)
        this._visibilityHandler = null;  // 页面可见性兜底监听句柄
        this._onlineHandler = null;      // 网络恢复(online)监听句柄: 断网后立即重连 SSE 补断档
        this._needResetOnNextData = false; // reconnect 后置位: 下一 SSE 帧无条件全量补齐(治轮询竞态/服务端重启)
        this._watchdogTimer = null;      // 墙钟新鲜度看门狗定时器(SSE 帧驱动检测的失速兜底)
        this._resetState = {};           // per-resKey reset 记账: {lastResetSec, backoffLevel, lastViewLatestSec}
        this._disconnectedSinceMs = null; // es.onerror 记录的断开时刻(ms); onmessage 按断流时长判真断档(M-3)
        // reconcile 失败自动重试状态：count 累计失败次数，timer 已排队的句柄（防重复）
        this._reconcileRetry = { count: 0, timer: null };
        this._disposed = false;
        this._sweepOrphanTimer = null;
        // reconcile 创建过的全部 entity id 集合，用于 sweep 时识别孤儿 shape。
        // safeRemove 静默失败或同一 key 两次 create 时 container 与 TV 会脱钩，
        // 孤儿 shape 残留为长斜线；sweep 强制 removeEntity 清除。
        // 用户手画的 shape 从未进入此 set，不会被误删。
        this._reconcileOwnedIds = new Set();
        // 只有从未被抑制的用户绘图事件中观察到的标识才能进入持久化存储。
        // TradingView 的 getLineToolsState() 可能包含 disableSave 自动图形，因此异步
        // 创建尚未稳定时，不能用“不是自动图形”的反向判断来认定用户绘图。
        this._userDrawingIds = new Set();
        this._automaticShapeCreateCount = 0;
        // removeEntity 可能不抛错却没有真正删除。删除确认前继续保留自动图形
        // 的所有权，避免它掉出追踪后被误当成用户手动画线永久残留。
        this._pendingRemovalIds = new Set();
        // reconcile 精确状态守卫：{ 'symbolKey__type': { from, keys, unfinishedKeys } }
        // 完整几何 key、可视区起点和未完成状态都相同才跳过；不截断，避免最新中枢
        // 边界修正被误判成无变化。Set 比较复用 reconcile 已生成的 newKeys，无需额外排序。
        this._reconcileGuard = {};
        // 每个 reconcile scope 的异步创建代际。旧 Promise 即使晚于新数据返回，也只能
        // 删除自己创建的实体，不能再写回当前容器形成重叠/旧范围中枢。
        this._reconcileEpochs = new Map();
        // full rebuild 后 500ms 补一次 verify-rebuild，让 TV 在稳定布局上重新落位 shape
        this._verifyRebuildTimer = null;
        this._verifyingUntil = null;  // performance.now() 时间戳，在此之前的 reconcile 属于 verify 内部
        // 严格结构使用独立、按图表实例隔离的 ownership 容器。状态在首次消费原子
        // strict_structure 时惰性初始化，避免 charts.js 单测或降级页面缺少辅助脚本时崩溃。
        this._strictContainers = new Map();
        this._strictScopes = new Set();
        this._strictDesiredByScope = new Map();
        this._strictDesiredItemsByScope = new Map();
        this._strictPendingCreates = new Map();
        this._strictReconcileEpoch = null;
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        this._strictStructureStatus = { state: 'waiting', code: null };
    }

    enqueueLatestDrawingSave(taskFactory) {
        if (typeof taskFactory !== "function") {
            return Promise.reject(new TypeError("drawing save task must be a function"));
        }

        return new Promise((resolve, reject) => {
            if (!this._drawingSavePending) {
                this._drawingSavePending = { taskFactory, waiters: [] };
            } else {
                // 当前写入之后排队的请求统一观察最新状态。
                this._drawingSavePending.taskFactory = taskFactory;
            }
            this._drawingSavePending.waiters.push({ resolve, reject });
            this._drainDrawingSaveQueue();
        });
    }

    async _drainDrawingSaveQueue() {
        if (this._drawingSaveInFlight || !this._drawingSavePending) return;
        const job = this._drawingSavePending;
        this._drawingSavePending = null;
        this._drawingSaveInFlight = true;
        try {
            await job.taskFactory();
            job.waiters.forEach((waiter) => waiter.resolve());
        } catch (error) {
            if (typeof layer !== "undefined" && layer && typeof layer.msg === "function") {
                layer.msg("画线保存失败，请检查网络后重试");
            }
            job.waiters.forEach((waiter) => waiter.reject(error));
        } finally {
            this._drawingSaveInFlight = false;
            if (this._drawingSavePending) {
                this._drainDrawingSaveQueue();
            }
        }
    }

    loadDrawingStateOnce(persistenceKey, taskFactory) {
        if (typeof taskFactory !== "function") {
            return Promise.reject(new TypeError("drawing load task must be a function"));
        }
        if (!(this._drawingLoadsInFlight instanceof Map)) {
            this._drawingLoadsInFlight = new Map();
        }
        const existing = this._drawingLoadsInFlight.get(persistenceKey);
        if (existing) return existing;

        let task;
        task = Promise.resolve()
            .then(taskFactory)
            .finally(() => {
                if (this._drawingLoadsInFlight.get(persistenceKey) === task) {
                    this._drawingLoadsInFlight.delete(persistenceKey);
                }
            });
        this._drawingLoadsInFlight.set(persistenceKey, task);
        return task;
    }

    getDrawingsCacheKey(symbol, interval) {
        const mode = this.cl_independent_drawings ? "ind" : "shared";
        const resolutionKey = this.cl_independent_drawings ? interval : "all";
        return `${symbol}_${resolutionKey}_${mode}`;
    }

    getDrawingPersistenceKey(layoutId, chartId, symbol, resolution) {
        // 后端存储名同时包含 layout/chart/symbol/resolution。不能只按图表缓存键
        // 记基线，否则 TradingView 自己的 chart=1 请求会污染 default/default 记录。
        return JSON.stringify([
            String(layoutId),
            String(chartId),
            String(symbol),
            String(resolution),
        ]);
    }

    drawingStateFingerprint(state) {
        const canonicalize = (value) => {
            if (Array.isArray(value)) return value.map(canonicalize);
            if (value && typeof value === "object") {
                const result = {};
                for (const key of Object.keys(value).sort()) {
                    result[key] = canonicalize(value[key]);
                }
                return result;
            }
            return value;
        };
        return JSON.stringify(canonicalize(state));
    }

    _setPersistedDrawingFingerprint(persistenceKey, fingerprint) {
        if (!(this._persistedDrawingFingerprints instanceof Map)) {
            this._persistedDrawingFingerprints = new Map();
        }
        // delete + set 维护简单的 LRU 顺序，限制多标的长时间运行的内存占用。
        this._persistedDrawingFingerprints.delete(persistenceKey);
        this._persistedDrawingFingerprints.set(persistenceKey, fingerprint);
        while (this._persistedDrawingFingerprints.size > 400) {
            const oldestKey = this._persistedDrawingFingerprints.keys().next().value;
            if (oldestKey === undefined) break;
            this._persistedDrawingFingerprints.delete(oldestKey);
        }
    }

    rememberPersistedDrawingState(persistenceKey, state) {
        const fingerprint = this.drawingStateFingerprint(state);
        this._setPersistedDrawingFingerprint(persistenceKey, fingerprint);
        return fingerprint;
    }

    enqueueDrawingStateSave(persistenceKey, state, taskFactory, options = {}) {
        if (typeof taskFactory !== "function") {
            return Promise.reject(new TypeError("drawing save task must be a function"));
        }
        const fingerprint = this.drawingStateFingerprint(state);
        return this.enqueueLatestDrawingSave(async () => {
            if (!(this._persistedDrawingFingerprints instanceof Map)) {
                this._persistedDrawingFingerprints = new Map();
            }
            const baselineKnown = this._persistedDrawingFingerprints.has(persistenceKey);
            if (!baselineKnown && options.requireKnownBaseline) {
                clog("[DEBUG-CHARTS] Skip drawings auto-save before baseline is loaded");
                return;
            }
            if (baselineKnown && this._persistedDrawingFingerprints.get(persistenceKey) === fingerprint) {
                clog("[DEBUG-CHARTS] Skip unchanged drawings save");
                return;
            }

            // 必须在任务真正执行时比较，而不是入队时比较。这样 A 正在写入、随后
            // 用户撤回到旧状态 B 时，B 会在 A 成功后再次写回，不会被旧基线误跳过。
            await taskFactory();
            this._setPersistedDrawingFingerprint(persistenceKey, fingerprint);
        });
    }

    getCurrentChartIdentity() {
        if (!this.chart) return null;
        return {
            symbol: this.chart.symbol(),
            interval: this.chart.resolution(),
        };
    }

    createContextToken(symbol, interval) {
        return `${this.instanceId}:${symbol}:${interval}:${++this._drawingsRequestSeq}`;
    }

    beginContextSwitch(reason, symbol, interval) {
        const token = this.createContextToken(symbol, interval);
        this._activeContextToken = token;
        this._drawingsLoadToken = token;
        this._is_switching_interval = true;
        this._drawingStatePhase = reason;
        return token;
    }

    isTokenCurrent(token) {
        return !!token && token === this._activeContextToken;
    }

    finishContextSwitch(token) {
        if (!this.isTokenCurrent(token)) return;
        this._is_switching_interval = false;
        this._drawingStatePhase = null;
    }

    markDrawingMutationStart(phase) {
        const mutationPhase = phase || 'unknown';
        this._activeDrawingMutations.add(mutationPhase);
        this._is_drawing_chanlun = this._activeDrawingMutations.size > 0;
        this._drawingStatePhase = mutationPhase;
    }

    markDrawingMutationEnd(phase) {
        const mutationPhase = phase || 'unknown';
        this._activeDrawingMutations.delete(mutationPhase);
        this._is_drawing_chanlun = this._activeDrawingMutations.size > 0;
        this._drawingStatePhase = this._is_drawing_chanlun
            ? Array.from(this._activeDrawingMutations)[this._activeDrawingMutations.size - 1]
            : null;
    }

    shouldSuppressDrawingSave() {
        return this._is_switching_interval
            || this._activeDrawingMutations.size > 0
            || this.isApplyingDrawingState
            || this._automaticShapeCreateCount > 0
            || (this._strictPendingCreates instanceof Map && this._strictPendingCreates.size > 0);
    }

    setDrawingsCache(key, state) {
        this._drawingsCache.set(key, state);
        if (this._drawingsCache.size > 200) {
            const oldestKey = this._drawingsCache.keys().next().value;
            if (oldestKey !== undefined) {
                this._drawingsCache.delete(oldestKey);
            }
        }
    }

    isDrawingStateEmpty(state) {
        if (!state || !state.sources) return true;
        if (state.sources instanceof Map) {
            return state.sources.size === 0;
        }
        return Object.keys(state.sources).length === 0;
    }

    emptyUserDrawingsState() {
        return { sources: new Map(), groups: new Map() };
    }

    _drawingStateEntries(collection) {
        if (!collection) return [];
        if (collection instanceof Map || typeof collection.entries === 'function') {
            try { return Array.from(collection.entries()); } catch (e) { return []; }
        }
        if (typeof collection === 'object' && !Array.isArray(collection)) {
            return Object.entries(collection);
        }
        return [];
    }

    _isValidDrawingSourceState(value) {
        return !!value && typeof value === 'object' && !Array.isArray(value);
    }

    serializeUserDrawingsState(state) {
        const allowed = new Set(
            Array.from(this._userDrawingIds || [], (value) => String(value)),
        );
        const sources = {};
        for (const [rawId, sourceState] of this._drawingStateEntries(state?.sources)) {
            const id = String(rawId);
            if (!allowed.has(id) || !this._isValidDrawingSourceState(sourceState)) continue;
            sources[id] = sourceState;
        }
        return {
            schema: USER_DRAWING_STATE_SCHEMA,
            sources,
            groups: {},
        };
    }

    deserializeUserDrawingsState(state) {
        if (!state || state.schema !== USER_DRAWING_STATE_SCHEMA) {
            this._userDrawingIds = new Set();
            throw new Error('drawing_state_schema_invalid');
        }
        const sources = new Map();
        for (const [rawId, sourceState] of this._drawingStateEntries(state.sources)) {
            if (!this._isValidDrawingSourceState(sourceState)) continue;
            sources.set(String(rawId), sourceState);
        }
        this._userDrawingIds = new Set(sources.keys());
        return { sources, groups: new Map() };
    }

    scheduleDrawingsSave(reason = 'unspecified') {
        if (!this.chart || this.shouldSuppressDrawingSave()) return Promise.resolve();
        if (typeof this.chart.getLineToolsState !== 'function') return Promise.resolve();
        if (!this.save_load_adapter || typeof this.save_load_adapter.saveLineToolsAndGroups !== 'function') {
            return Promise.resolve();
        }

        try {
            this._pendingSaveState = this.chart.getLineToolsState();
        } catch (e) {
            console.debug('[DEBUG-CHARTS] getLineToolsState failed', e);
            return Promise.resolve();
        }

        if (this._saveScheduled) {
            return this._saveInFlight || Promise.resolve();
        }

        this._saveScheduled = true;
        this._saveInFlight = new Promise((resolve) => {
            setTimeout(async () => {
                this._saveScheduled = false;
                const state = this._pendingSaveState;
                this._pendingSaveState = null;

                if (!state || this.shouldSuppressDrawingSave()) {
                    this._saveInFlight = null;
                    resolve();
                    return;
                }

                try {
                    await this.save_load_adapter.saveLineToolsAndGroups('default', 'default', state, { reason });
                } catch (e) {
                    console.debug('[DEBUG-CHARTS] drawing save skipped', e);
                }

                if (this._pendingSaveState && !this.shouldSuppressDrawingSave()) {
                    this._saveInFlight = null;
                    resolve(this.scheduleDrawingsSave(reason));
                    return;
                }

                this._saveInFlight = null;
                resolve();
            }, 300);
        });

        return this._saveInFlight;
    }

    async applyUserDrawingsState(state, token, cacheKey, options = {}) {
        if (!state || !this.chart || !this.isTokenCurrent(token)) {
            return false;
        }
        this.isApplyingDrawingState = true;
        this.markDrawingMutationStart('apply-user-drawings');
        try {
            // TradingView 会异步应用保存的绘图状态，因此等待前后都要使已追踪的自动实体
            // 失效。状态对象仍在应用时，K 线就绪事件可能重画缠论实体；若不这样处理，
            // 延迟提交的状态会让所有权容器继续引用几何已被替换的实体。
            this._clearAllStrictScopes('apply-user-drawings-start');
            this.chart.removeAllShapes();
            // removeAllShapes 清空画面后 obj_charts 仍保留旧 entity 记录，
            // 下次 reconcile 旧 key 命中 toKeep 分支不重建，导致图上空白只剩最新一段。
            // 同步置空 obj_charts，强制 reconcile 走全量重建路径。
            this.obj_charts = {};
            // removeAllShapes 已清掉所有用户图形 → 同步清空"已染色图形 id"集合,否则切标的/切周期
            // 长期累积陈旧 id(轻量内存泄漏,见审查 L2)。仅在用户图形确实被整块清除时清。
            if (this._coloredDrawings) this._coloredDrawings.clear();
            this._userDrawingIds = new Set(
                this._drawingStateEntries(state.sources).map(([id]) => String(id)),
            );
            this._resetReconcileRetry();
            if (!this.isTokenCurrent(token)) {
                return false;
            }
            await this.chart.applyLineToolsState(state);
            this._clearAllStrictScopes('apply-user-drawings-settled');
            if (cacheKey && this.isTokenCurrent(token)) {
                this.setDrawingsCache(cacheKey, state);
            }
            if (options.redrawAutomatic !== false) {
                this.debouncedDrawChanlun();
            }
            return this.isTokenCurrent(token);
        } finally {
            this.isApplyingDrawingState = false;
            this.markDrawingMutationEnd('apply-user-drawings');
        }
    }

    async reloadDrawingsForCurrentContext(reason, options = {}) {
        if (!this.chart || !this.save_load_adapter || typeof this.save_load_adapter.loadLineToolsAndGroups !== 'function') {
            return;
        }

        const identity = this.getCurrentChartIdentity();
        if (!identity) return;

        const { symbol, interval } = identity;
        const token = this.beginContextSwitch(reason, symbol, interval);
        const cacheKey = this.getDrawingsCacheKey(symbol, interval);
        const cachedDrawings = this._drawingsCache.get(cacheKey);

        try {
            if (!options.bypassCache && this._drawingsCache.has(cacheKey)) {
                await this.applyUserDrawingsState(
                    cachedDrawings,
                    token,
                    cacheKey,
                    options,
                );
                return;
            }

            const state = await this.save_load_adapter.loadLineToolsAndGroups('default', 'default', 'load', {
                resolution: interval,
                symbol,
                token,
            });

            if (!this.isTokenCurrent(token)) {
                return;
            }

            await this.applyUserDrawingsState(
                state || this.emptyUserDrawingsState(),
                token,
                cacheKey,
                options,
            );
        } catch (e) {
            console.error(`[DEBUG-CHARTS] Failed to reload drawings (${reason})`, e);
        } finally {
            this.finishContextSwitch(token);
        }
    }

    manualReloadData() {
        if (this._manualReloadInFlight) return this._manualReloadInFlight;

        const task = (async () => {
            if (!this.widget || !this.chart || this._disposed) return false;

            // 清空画布前先保存当前明确的用户绘图。serializeUserDrawingsState 会排除
            // 全部自动缠论实体，因此陈旧覆盖层不会随用户绘图一起保存。
            await this.scheduleDrawingsSave('manual-data-reload');

            this._resetDataReadyContext();
            this._drawRetryCount = 0;
            this._latestAppliedBarTime = null;

            // 始终通过服务端严格闸门读取。本地缓存可能来自应用升级前，或来自先前的
            // 标的/周期上下文。applyUserDrawingsState 会先删除全部线条工具，再只恢复
            // 明确的手工绘图。
            await this.reloadDrawingsForCurrentContext('manual-data-reload', {
                bypassCache: true,
                redrawAutomatic: false,
            });
            if (this._disposed || !this.chart) return false;

            try {
                const historyProvider = this.udf_datafeed?._historyProvider;
                if (historyProvider) historyProvider._forceRefreshOnce = true;
            } catch (e) { /* 可选的数据源优化 */ }
            this.widget.resetCache();

            this.chart.resetData();
            // K 线与自动结构的生命周期不同。主序列刷新完成后才重建缠论实体，
            // 防止空画布上残留悬浮图形。
            this._requestChanlunDrawWhenReady();
            return true;
        })();

        this._manualReloadInFlight = task.finally(() => {
            this._manualReloadInFlight = null;
        });
        return this._manualReloadInFlight;
    }

    _resetDataReadyContext() {
        this._dataContextGeneration = (this._dataContextGeneration || 0) + 1;
        this._tvDataReadyGeneration = -1;
        this._tvDataReadyIdentity = null;
        this._pendingChanlunDrawGeneration = null;
        this._pendingChanlunDrawIdentity = null;
        this._dataReadyProbeGeneration = null;
        this._dataReadyProbeIdentity = null;
        this._initialLoadDone = false;
        return this._dataContextGeneration;
    }

    _chartDataReadyNow() {
        try {
            return !!(
                this.chart &&
                typeof this.chart.dataReady === 'function' &&
                this.chart.dataReady() === true
            );
        } catch (e) {
            return false;
        }
    }

    _currentDataIdentityKey() {
        const identity = this.getCurrentChartIdentity();
        if (!identity || !identity.symbol || !identity.interval) return null;
        return `${String(identity.symbol).toLowerCase()}|${String(identity.interval).toLowerCase()}`;
    }

    _requestChanlunDrawWhenReady() {
        const contextGeneration = this._dataContextGeneration || 0;
        const contextIdentity = this._currentDataIdentityKey();
        if (!contextIdentity) return false;
        this._pendingChanlunDrawGeneration = contextGeneration;
        this._pendingChanlunDrawIdentity = contextIdentity;

        if (
            this._tvDataReadyGeneration === contextGeneration &&
            this._tvDataReadyIdentity === contextIdentity &&
            this._chartDataReadyNow()
        ) {
            this._pendingChanlunDrawGeneration = null;
            this._pendingChanlunDrawIdentity = null;
            this._initialLoadDone = true;
            this.debouncedDrawChanlun();
            return true;
        }

        if (!this.chart || typeof this.chart.dataReady !== 'function') return false;
        if (
            this._dataReadyProbeGeneration === contextGeneration &&
            this._dataReadyProbeIdentity === contextIdentity
        ) return false;
        this._dataReadyProbeGeneration = contextGeneration;
        this._dataReadyProbeIdentity = contextIdentity;
        try {
            const readyNow = this.chart.dataReady(
                () => this.handleDataReady(contextGeneration, contextIdentity)
            );
            if (readyNow === true) {
                return this.handleDataReady(contextGeneration, contextIdentity);
            }
        } catch (e) {
            if (
                this._dataReadyProbeGeneration === contextGeneration &&
                this._dataReadyProbeIdentity === contextIdentity
            ) {
                this._dataReadyProbeGeneration = null;
                this._dataReadyProbeIdentity = null;
            }
        }
        return false;
    }

    handleBarsReadyEvent(event) {
        const detail = event?.detail || {};
        if (detail.managerId && detail.managerId !== this.instanceId) {
            return;
        }
        const identity = this.getCurrentChartIdentity();
        if (!identity) return;
        // 守卫放宽: detail.symbol(getBars 的 requestParams.symbol=symbolInfo.ticker) 与
        // identity.symbol(chart.symbol())是 TV 两个不同 API, 搜索切标的后可能差一个 "market:"
        // 前缀。去前缀等价比较, 避免 ready 事件被误吞导致 draw_chanlun 永不触发(切标的卡死根因之一)。
        if (detail.symbol) {
            const _ev = detail.symbol.toLowerCase().replace(/^[a-z]+:/, '');
            const _id = identity.symbol.toLowerCase().replace(/^[a-z]+:/, '');
            if (_ev !== _id) return;
        }
        if (detail.resolution && detail.resolution !== identity.interval.toLowerCase()) {
            return;
        }
        const wasInitialLoad = (
            this._tvDataReadyGeneration !== (this._dataContextGeneration || 0) ||
            this._tvDataReadyIdentity !== this._currentDataIdentityKey()
        );
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleBarsReadyEvent ✓ symbol=${detail.symbol} res=${detail.resolution} bars=${detail.bars || '?'} fxs=${detail.fxs || '?'} bis=${detail.bis || '?'} xds=${detail.xds || '?'} wasInitialLoad=${wasInitialLoad}`);
        // 新传输响应提供了重新放置图形的机会。在 TradingView 装入新历史数据前，
        // 不能让上一画布状态的失败消耗本轮重试预算。
        if ((this._reconcileRetry?.count || 0) > 0) {
            this._clearReconcileRetryBudget();
        }
        // bars-ready 只表示 bars_result 已写入，TradingView 此时可能尚未接收 K 线。
        // 首次绘图必须等当前标的/周期 dataReady 后再执行；后续更新仍复用防抖入口。
        this._requestChanlunDrawWhenReady();
    }

    init() {
        // SSE 接管实时刷新(applyChanlunUpdate + feedRealtimeBar)后, TV 轮询降到 30s 仅作断线
        // 兜底, 大幅减少多标的 first=false 轮询撞长桥 6QPS 限流(实测轮询拉 10 根 6-24s)。
        const _sseOn = (typeof window !== 'undefined' && window.__CHANLUN_SSE_ENABLED === true);
        const _reviewLock = (typeof window !== 'undefined' && window.__chanlunReviewChartLock)
            ? window.__chanlunReviewChartLock
            : null;
        const _historyParams = _reviewLock ? {
            review_candidate_id: _reviewLock.candidate_id,
            review_source_sha256: _reviewLock.source_sha256,
            review_as_of: _reviewLock.review_as_of,
        } : {};
        this.udf_datafeed = new Datafeeds.UDFCompatibleDatafeed("/tv", _sseOn ? 30000 : 3000, undefined, {
            managerId: this.instanceId,
            historyParams: _historyParams,
        });

        const registry = getTVRegistry();
        registry.chartManagers.set(this.instanceId, this);
        registry.datafeeds.set(this.instanceId, this.udf_datafeed);
        registry.activeManagerId = this.instanceId;

        // 诊断工具挂在 window，生产环境默认静默；设 window.__chanlunDebug=true 可开启详细日志。
        // 在 console 跑 __chanlunDiag() 一键 dump 当前图表状态，__chanlunDumpLines() 打印 shape 端点。
        const _diagCm = this;
        window.__chanlunDiag = function () {
            try {
                const cm = _diagCm;
                const ch = cm.chart;
                if (!ch) { console.log('[CHANLUN-DIAG] chart 未就绪'); return; }
                const owned = cm._reconcileOwnedIds || new Set();
                const tvShapes = ch.getAllShapes ? (ch.getAllShapes() || []) : [];
                const tvIds = new Set(tvShapes.map(s => s.id));
                const containerByType = {};
                const containerIds = new Set();
                Object.entries(cm.obj_charts || {}).forEach(([sk, types]) => {
                    Object.entries(types || {}).forEach(([t, arr]) => {
                        containerByType[`${sk}/${t}`] = (arr || []).length;
                        (arr || []).forEach(item => {
                            if (item && item.id != null && typeof item.id !== 'object') containerIds.add(item.id);
                        });
                    });
                });
                const ghostsInTv = tvShapes.filter(s => !containerIds.has(s.id));
                const ghostsByName = {};
                ghostsInTv.forEach(g => { ghostsByName[g.name || '<noname>'] = (ghostsByName[g.name || '<noname>'] || 0) + 1; });
                const ownedNotInTv = [...owned].filter(id => !tvIds.has(id));
                console.log('========= [CHANLUN-DIAG] manual dump =========');
                console.log('TV.getAllShapes.length =', tvShapes.length);
                console.log('owned ids =', owned.size, ' container ids =', containerIds.size);
                console.log('container by type:', containerByType);
                console.log('TV 里有但 container 没有 (ghosts):', ghostsInTv.length, ' by name:', ghostsByName);
                console.log('owned 有但 TV 没 (已删 但 owned 未清):', ownedNotInTv.length);
                if (ghostsInTv.length > 0) {
                    console.log('--- ghosts 详情(前 10):');
                    ghostsInTv.slice(0, 10).forEach(g => {
                        let pts = null;
                        try { pts = ch.getShapeById(g.id)?.getPoints?.(); } catch (e) {}
                        console.log({ id: g.id, name: g.name, points: pts });
                    });
                }
                console.log('=============================================');
                return { tvShapes: tvShapes.length, owned: owned.size, container: containerIds.size, ghosts: ghostsInTv.length };
            } catch (e) {
                console.error('[CHANLUN-DIAG] dump failed:', e);
            }
        };

        // 打印当前所有 xds/bis 的端点坐标，用于定位错位长线段的起止时间和价格
        window.__chanlunDumpLines = function (type = 'xds') {
            try {
                const cm = _diagCm;
                if (!cm.chart) { console.log('[CHANLUN-DIAG] chart 未就绪'); return; }
                const allInContainer = [];
                Object.entries(cm.obj_charts || {}).forEach(([sk, types]) => {
                    (types[type] || []).forEach(entry => allInContainer.push({ sk, entry }));
                });
                console.log(`========= [CHANLUN-DIAG] dump ${type} (count=${allInContainer.length}) =========`);
                allInContainer.forEach(({ sk, entry }, i) => {
                    const pts = entry.points || [];
                    let geom = '';
                    if (pts.length >= 2) {
                        const t0 = pts[0].time, p0 = pts[0].price;
                        const t1 = pts[pts.length - 1].time, p1 = pts[pts.length - 1].price;
                        const dt = t1 - t0;
                        const dpct = p0 ? (Math.abs(p1 - p0) / p0 * 100).toFixed(2) : '?';
                        geom = `[${new Date(t0 * 1000).toISOString().slice(0,16)} → ${new Date(t1 * 1000).toISOString().slice(0,16)}] dt=${dt}s p=${p0}→${p1} (${dpct}%)`;
                    }
                    // 从 TV 取 shape 实际端点，与 entry.points 对比可定位漂移问题
                    let tvPts = null;
                    try { tvPts = cm.chart.getShapeById(entry.id)?.getPoints?.(); } catch (e) {}
                    const tvGeom = tvPts && tvPts.length >= 2
                        ? `TV=[${new Date(tvPts[0].time * 1000).toISOString().slice(0,16)}→${new Date(tvPts[tvPts.length-1].time * 1000).toISOString().slice(0,16)}] p=${tvPts[0].price}→${tvPts[tvPts.length-1].price}`
                        : 'TV=<no points>';
                    console.log(`#${i} id=${entry.id} linestyle=${entry.isUnfinished ? 1 : 0} ${geom} | ${tvGeom}`);
                });
                console.log('=============================================');
            } catch (e) {
                console.error('[CHANLUN-DIAG] dump lines failed:', e);
            }
        };

        this._barReadyHandler = this.handleBarsReadyEvent.bind(this);
        window.addEventListener('chanlun-bars-ready', this._barReadyHandler);

        // K线可见性兜底: 浏览器会节流后台标签的定时器(TV轮询), 切回前台时
        // K线可能停在旧值; 这里主动 resetData 让图表补刷(连带触发缠论重绘)。
        // 缠论侧在后台由 SSE 推送(onmessage 不受定时器节流)已保持最新。
        this._visibilityHandler = () => {
            if (document.visibilityState !== 'visible') return;
            // SSE 连通(readyState OPEN)时, K线由 feedRealtimeBar、缠论由 applyChanlunUpdate
            // 经 SSE onmessage 实时喂入(onmessage 不受后台标签定时器节流), 后台期间数据已保持
            // 最新 → 切回前台无需 resetData。resetData 会整块清空 K线+缠论、重新 getBars 重绘,
            // 正是"切换页面再切回缠论闪一下"的根因。仅当 SSE 未连通(退回被节流的轮询)时才
            // resetData 兜底补刷后台停滞的 K线。
            try {
                if (this._sse && this._sse.readyState === 1) return;  // SSE OPEN → 不补刷, 不闪
                if (this.widget && typeof this.widget.activeChart === 'function') {
                    const ch = this.widget.activeChart();
                    if (ch && typeof ch.resetData === 'function') ch.resetData();
                }
            } catch (e) { /* ignore */ }
        };
        document.addEventListener('visibilitychange', this._visibilityHandler);

        // 网络恢复兜底: 断网期间 EventSource 进入重连退避(可能数秒~数十秒才自动重连),
        // 'online' 事件一触发就立即重连 SSE, 缩短"恢复 → 补齐"延迟。重连后服务端推的首帧
        // (完整快照)会经上面 onmessage 的断档检测触发 resetData, 整段补齐断网期间缺失的 K线+缠论。
        this._onlineHandler = () => {
            clog('[SSE] 网络恢复(online), 标记重连后补齐 + 主动重连 SSE');
            this._needResetOnNextData = true;
            try { this._openSseStream(); } catch (e) { /* ignore */ }
        };
        if (typeof window !== 'undefined') {
            window.addEventListener('online', this._onlineHandler);
        }

        // 墙钟新鲜度看门狗: 独立于 SSE 帧, 每 20s 自查"画布末根 vs 当下"是否在交易时段内严重失速
        // (覆盖 SSE 半开静默/被中间层缓冲转 fallback/不推帧等帧驱动检测的盲区)。失速则触发一次
        // resetData(带防抖退避 + 交易时段门控, 收盘/周末不误闪)。
        this._watchdogTimer = setInterval(() => {
            try {
                if (typeof window === 'undefined' || !window.SseGap || !this.widget) return;
                let si = null;
                try { si = this.widget.symbolInterval(); } catch (e) { return; }
                if (!si || !si.symbol || !si.interval) return;
                const resKey = String(si.symbol).toLowerCase() + String(si.interval).toLowerCase();
                const sym = String(si.symbol || '');
                const market = (sym.indexOf(':') >= 0)
                    ? sym.split(':')[0].toLowerCase()
                    : ((typeof Utils !== 'undefined' && Utils.get_market) ? Utils.get_market() : '');
                const viewLatestSec = this._getViewLatestSec(resKey, si.interval);
                const nowSec = Math.floor(Date.now() / 1000);
                const periodSec = window.SseGap._internal.resolutionToPeriodSeconds(si.interval);
                if (window.SseGap.computeWatchdogReset(market, viewLatestSec, nowSec, periodSec)) {
                    this._doReset(resKey, 'watchdog', viewLatestSec);
                }
            } catch (e) { /* ignore */ }
        }, 20000);

        const self = this;
        const client_id = "chanlun_pro_" + Utils.get_market() + "_" + this.id;
        const user_id = "999";
        const save_load_adapter = {
            getAllCharts: function () {
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data : []);
            },
            removeChart: function (chartId) {
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + "&chart=" + chartId, { method: "DELETE" })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            saveChart: function (chartData) {
                clog("[DEBUG-CHARTS] saveChart called", chartData);
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + (chartData.id ? "&chart=" + chartData.id : ""), {
                    method: "POST",
                    body: new URLSearchParams({
                        name: chartData.name,
                        symbol: chartData.symbol,
                        resolution: chartData.resolution,
                        content: chartData.content
                    })
                })
                    .then(res => res.json())
                    .then(res => {
                        clog("[DEBUG-CHARTS] saveChart response", res);
                        return res.status === 'ok' ? (res.id || chartData.id || "default") : null;
                    })
                    .catch(err => {
                        console.error("[DEBUG-CHARTS] saveChart error", err);
                        return null;
                    });
            },
            getChartContent: function (chartId) {
                clog("[DEBUG-CHARTS] getChartContent called", chartId);
                return fetch("/tv/1.1/charts?client=" + client_id + "&user=" + user_id + "&chart=" + chartId)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data.content : null);
            },
            getAllStudyTemplates: function () {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data : []);
            },
            removeStudyTemplate: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id + "&template=" + templateData.name, { method: "DELETE" })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            saveStudyTemplate: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id, {
                    method: "POST",
                    body: new URLSearchParams({
                        name: templateData.name,
                        content: templateData.content
                    })
                })
                    .then(res => res.json())
                    .then(res => res.status === 'ok');
            },
            getStudyTemplateContent: function (templateData) {
                return fetch("/tv/1.1/study_templates?client=" + client_id + "&user=" + user_id + "&template=" + templateData.name)
                    .then(res => res.json())
                    .then(res => res.status === 'ok' ? res.data.content : null);
            },
            saveLineToolsAndGroups: function (layoutId, chartId, state, options = {}) {
                clog("[DEBUG-CHARTS] saveLineToolsAndGroups called", { layoutId, chartId, state, options });
                if (self.shouldSuppressDrawingSave()) {
                    clog("[DEBUG-CHARTS] Skip saveLineToolsAndGroups due to active drawing mutation");
                    return Promise.resolve();
                }
                if (!state || typeof state !== "object") {
                    const error = new TypeError("drawing state must be an object");
                    if (typeof layer !== "undefined" && layer && typeof layer.msg === "function") {
                        layer.msg("画线状态无效，已取消保存");
                    }
                    return Promise.reject(error);
                }
                const rawResolution = self.chart ? self.chart.resolution() : Utils.get_local_data(Utils.get_market() + "_interval_" + self.id);
                const resolution = self.cl_independent_drawings ? rawResolution : 'all';
                const symbol = self.chart ? self.chart.symbol() : Utils.get_market() + ":" + Utils.get_code();
                const cacheKey = self.getDrawingsCacheKey(symbol, rawResolution);
                const persistenceKey = self.getDrawingPersistenceKey(layoutId, chartId, symbol, resolution);

                const processedState = self.serializeUserDrawingsState(state);
                clog("[DEBUG-CHARTS] Evaluating drawings save", { symbol, resolution, reason: options.reason });
                const query = new URLSearchParams({
                    client: client_id,
                    user: user_id,
                    chart: String(chartId),
                    layout: String(layoutId),
                    symbol: String(symbol),
                    resolution: String(resolution),
                });
                return self.enqueueDrawingStateSave(persistenceKey, processedState, function () {
                    return fetch("/tv/1.1/drawings?" + query.toString(), {
                        method: "POST",
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ state: processedState })
                    }).then(function (response) {
                        if (!response.ok) {
                            throw new Error("Drawing save failed with HTTP " + response.status);
                        }
                        return response.json();
                    }).then(function (payload) {
                        if (!payload || payload.status !== 'ok') {
                            throw new Error((payload && (payload.message || payload.error)) || "Drawing save was rejected");
                        }
                        self.setDrawingsCache(
                            cacheKey,
                            self.deserializeUserDrawingsState(processedState),
                        );
                    });
                }, {
                    // auto-save 是 TradingView 初始化也会触发的事件。若读取尚未成功，
                    // 绝不能用本地空状态删除服务端已有画线；明确 drawing_event 则仍可写入。
                    requireKnownBaseline: options.reason === 'auto_save',
                });
            },
            loadLineToolsAndGroups: function (layoutId, chartId, requestType, requestContext = {}) {
                clog("[DEBUG-CHARTS] loadLineToolsAndGroups called", { layoutId, chartId, requestType, requestContext });
                return new Promise((resolve, reject) => {
                    const resolution = requestContext.resolution;
                    const symbol = requestContext.symbol;
                    const token = requestContext.token;

                    if (requestType !== 'mainSeriesLineTools' && requestType !== 'load') {
                        return resolve(null);
                    }
                    const loadSymbol = symbol || (self.chart ? self.chart.symbol() : '');
                    const rawResolution = resolution || (self.chart ? self.chart.resolution() : '');
                    const loadResolution = self.cl_independent_drawings ? rawResolution : 'all';
                    if (!loadSymbol || !loadResolution) {
                        return resolve(null);
                    }
                    const persistenceKey = self.getDrawingPersistenceKey(
                        layoutId,
                        chartId,
                        loadSymbol,
                        loadResolution,
                    );
                    const query = new URLSearchParams({
                        client: client_id,
                        user: user_id,
                        chart: String(chartId),
                        layout: String(layoutId),
                        symbol: String(loadSymbol),
                        resolution: String(loadResolution),
                    });

                    self.loadDrawingStateOnce(persistenceKey, () => (
                        fetch("/tv/1.1/drawings?" + query.toString())
                            .then(response => {
                                if (!response.ok) {
                                    throw new Error("Drawing load failed with HTTP " + response.status);
                                }
                                return response.json();
                            })
                    ))
                        .then(payload => {
                            if (token && !self.isTokenCurrent(token)) {
                                return resolve(null);
                            }
                            if (!payload || payload.status !== 'ok' || !payload.data) {
                                throw new Error(
                                    (payload && (payload.message || payload.error)) || "Drawing load was rejected",
                                );
                            }
                            const loadedState = self.deserializeUserDrawingsState(payload.data);
                            self.rememberPersistedDrawingState(persistenceKey, payload.data);
                            resolve(loadedState);
                        }).catch(err => {
                            console.error("[DEBUG-CHARTS] loadLineToolsAndGroups error:", err);
                            reject(err);
                        });
                });
            }
        };
        this.save_load_adapter = save_load_adapter;

        this.widget = new TradingView.widget({
            // loading_screen 让 widget 内部加载阶段显示 spinner 而非空白
            loading_screen: (function () {
                var isDark = false;
                try {
                    var t = JSON.parse(localStorage.tv_chart || '{}');
                    isDark = (t.theme === 'dark');
                } catch (e) {}
                return {
                    backgroundColor: isDark ? '#1e1e1e' : '#ffffff',
                    foregroundColor: '#1e9fff',
                };
            })(),
            debug: false, autosize: true, fullscreen: false,
            container: "tv_chart_container_" + this.id,
            symbol: Utils.get_market() + ":" + Utils.get_code(),
            interval: getInitialChartInterval(this.id),
            datafeed: this.udf_datafeed,
            library_path: "static/charting_library/",
            theme: Utils.get_local_data("theme"),
            numeric_formatting: { decimal_sign: "." },
            time_frames: [], timezone: getMarketTimezone(Utils.get_market()), locale: "zh",
            symbol_search_request_delay: 100, auto_save_delay: 5, study_count_limit: 100,
            disabled_features: CHART_DISABLED_FEATURES,
            enabled_features: ["study_templates", "seconds_resolution", "saveload_separate_drawings_storage", "iframe_loading_same_origin"],
            saved_data_meta_info: { uid: 1, name: "default", description: "default" },
            save_load_adapter: save_load_adapter,
            client_id: "chanlun_pro_" + Utils.get_market() + "_" + this.id,
            user_id: "999", load_last_chart: shouldLoadLastChart(),
            custom_indicators_getter: this.getCustomIndicators,
            time_scale: { min_bar_spacing: 0.05, max_bar_spacing: 800 },
        });
        this.setupEventListeners();
        return this;
    }

    getCustomIndicators(PineJS) {
        if (typeof TvIdxMACDBackend === 'undefined') {
            return Promise.resolve([]);
        }
        return Promise.resolve([
            TvIdxMACDBackend.idx(PineJS),
            TvIdxAMA.idx(PineJS), TvIdxATR.idx(PineJS), TvIdxCDBB.idx(PineJS),
            TvIdxCMCM.idx(PineJS), TvIdxFCX.idx(PineJS),
            TvIdxHDLY.idx(PineJS), TvIdxHeima.idx(PineJS), TvIdxHLBLW.idx(PineJS),
            TvIdxHLFTX.idx(PineJS), TvIdxKDJ.idx(PineJS), TvIdxLTQS.idx(PineJS),
            TvIdxMA.idx(PineJS), TvIdxMACDBL.idx(PineJS), TvIdxVegasMA.idx(PineJS),
            TvIdxVOL.idx(PineJS),
        ]);
    }

    ensureRequestedDefaultStudies() {
        if (this._defaultStudiesPromise) return this._defaultStudiesPromise;
        if (!this.chart) return Promise.resolve([]);
        if (!(this._defaultStudyIds instanceof Map)) this._defaultStudyIds = new Map();
        const requested = requestedDefaultStudies(
            window.location && typeof window.location.search === "string"
                ? window.location.search
                : "",
        );
        let createFailed = false;
        const pending = (async () => {
            if (!requested.length) return [];
            let existing = [];
            try {
                const studies = typeof this.chart.getAllStudies === "function"
                    ? this.chart.getAllStudies()
                    : [];
                existing = Array.isArray(studies) ? studies : [];
            } catch (e) {
                console.warn("[CHARTS] read default studies failed", e);
            }

            const studyIds = [];
            for (const name of requested) {
                if (this._defaultStudyIds.has(name)) {
                    const knownId = this._defaultStudyIds.get(name);
                    if (knownId !== null && knownId !== undefined) studyIds.push(knownId);
                    continue;
                }
                const current = existing.find((study) => study && study.name === name);
                if (current) {
                    if (current.id !== null && current.id !== undefined) studyIds.push(current.id);
                    this._defaultStudyIds.set(name, current.id);
                    if (name === "MACD_HTF") this.macdStudyId = current.id;
                    continue;
                }
                if (typeof this.chart.createStudy !== "function") {
                    createFailed = true;
                    continue;
                }
                try {
                    const studyId = await this.chart.createStudy(name, false, false);
                    if (studyId !== null && studyId !== undefined) studyIds.push(studyId);
                    this._defaultStudyIds.set(name, studyId);
                    if (name === "MACD_HTF") this.macdStudyId = studyId;
                } catch (e) {
                    createFailed = true;
                    console.warn(`[CHARTS] create default study ${name} failed`, e);
                }
            }
            return studyIds;
        })();
        this._defaultStudiesPromise = pending;
        pending.finally(() => {
            if (createFailed && this._defaultStudiesPromise === pending) {
                this._defaultStudiesPromise = null;
            }
        });
        return pending;
    }

    setupEventListeners() {
        const global_widget = this.widget;
        const self = this;

        this.widget.headerReady().then(function () {
            var btnDisplay = global_widget.createButton();
            btnDisplay.textContent = "缠论显示设置 ▾";
            btnDisplay.setAttribute('role', 'button');
            btnDisplay.setAttribute('tabindex', '0');
            btnDisplay.setAttribute('aria-disabled', 'false');
            btnDisplay.setAttribute('aria-label', '打开缠论显示设置');
            btnDisplay.setAttribute('aria-haspopup', 'dialog');
            btnDisplay.setAttribute('aria-expanded', 'false');
            btnDisplay.setAttribute('aria-controls', 'cl_display_menu_' + self.id);
            if (self._clDisplayButtonA11yCleanup) self._clDisplayButtonA11yCleanup();
            self._clDisplayButtonA11yCleanup = bindClDisplayButtonAccessibility(btnDisplay);
            btnDisplay.addEventListener("click", function (event) {
                // 每个图表面板独立一套菜单 DOM，防止多图布局下互相干扰
                const menuId = 'cl_display_menu_' + self.id;
                const cleanupOutsideDismiss = () => {
                    const cleanup = self._clDisplayMenuOutsideCleanup;
                    self._clDisplayMenuOutsideCleanup = null;
                    if (typeof cleanup === 'function') cleanup();
                };
                if ($('#' + menuId).length > 0) {
                    cleanupOutsideDismiss();
                    $('#' + menuId).remove();
                    btnDisplay.setAttribute('aria-expanded', 'false');
                    return;
                }
                cleanupOutsideDismiss();

                const cfg = self.cl_show_config;
                const cbId = (k) => 'cl_cb_' + k + '_' + self.id;
                const indCbId = 'cl_cb_independent_drawings_' + self.id;

                let _curInterval = "?";
                try { _curInterval = self.widget.symbolInterval().interval; } catch (e) {}
                const _displayLevels = recursiveDisplayLevels(_curInterval);
                const _centerLevels = _displayLevels.map((item) => ({
                    label: `${item.label} 中枢`, key: `center_L${item.level}`, level: item.level,
                }));
                const _trendLevels = _displayLevels.map((item) => ({
                    label: `${item.label} 走势类型`, key: `trend_L${item.level}`, level: item.level,
                }));
                const _pointLevels = _displayLevels.map((item) => ({
                    label: `${item.label} 买卖点`, key: `point_L${item.level}`, level: item.level,
                }));
                const _divergenceLevels = _displayLevels.flatMap((item) => ([
                    {
                        label: `${item.label} 盘整背驰`,
                        key: `divergence_consolidation_L${item.level}`,
                        level: item.level,
                    },
                    {
                        label: `${item.label} 趋势背驰`,
                        key: `divergence_trend_L${item.level}`,
                        level: item.level,
                    },
                ]));
                const _pointTypes = [
                    { key: 'point_1buy', label: '一买' },
                    { key: 'point_2buy', label: '二买' },
                    { key: 'point_3buy', label: '三买' },
                    { key: 'point_1sell', label: '一卖' },
                    { key: 'point_2sell', label: '二卖' },
                    { key: 'point_3sell', label: '三卖' },
                ];
                const _checked = (key, fallback = true) => (
                    cfg[key] === undefined ? fallback : cfg[key] !== false
                );
                const _cbRow = (key, label, fallback = true) => `
                    <label style="display:block; cursor:pointer; font-size:14px; line-height:24px;">
                        <input type="checkbox" id="${cbId(key)}" ${_checked(key, fallback) ? 'checked' : ''}
                            style="margin-right:6px; vertical-align:middle;">${label}
                    </label>`;
                const _grpTitle = (title, note = '') => `
                    <div style="font-size:14px; color:#2563a6; padding:8px 0 2px; font-weight:700;">
                        ${title}${note ? `<span style="font-size:13px; color:#687386; margin-left:6px; font-weight:400;">${note}</span>` : ''}
                    </div>`;
                const _swatch = (color) => `
                    <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};
                        margin-right:4px;vertical-align:middle;border:1px solid rgba(0,0,0,0.25);"></span>`;
                const _dualSwatch = (first, second, title) => `
                    <span title="${title}" style="display:inline-flex;width:12px;height:10px;border-radius:2px;
                        margin-right:4px;vertical-align:middle;overflow:hidden;border:1px solid rgba(0,0,0,0.25);">
                        <span style="width:6px;background:${first};"></span><span style="width:6px;background:${second};"></span>
                    </span>`;

                let html = `
                    <div id="${menuId}" role="dialog" aria-modal="false" aria-labelledby="${menuId}_title" tabindex="-1"
                        style="position:absolute;z-index:99999999;background:#fff;border:1px solid #cfd6df;box-sizing:border-box;
                        box-shadow:0 8px 28px rgba(0,0,0,0.2);border-radius:8px;padding:12px 14px;line-height:24px;
                        font-size:14px;color:#26313d;width:min(440px,calc(100vw - 16px));min-width:0;
                        max-width:calc(100vw - 16px);max-height:min(72vh,680px);overflow:auto;">
                        <div id="${menuId}_drag_handle" role="group" tabindex="0"
                            aria-label="拖动缠论显示设置；方向键移动" aria-roledescription="可拖动弹窗标题栏"
                            title="按住拖动弹窗，或使用方向键移动"
                            style="display:flex;align-items:center;justify-content:space-between;gap:12px;
                            margin:-12px -14px 8px;padding:9px 14px;border-bottom:1px solid #e3e8ef;
                            border-radius:8px 8px 0 0;background:#f7f9fc;cursor:grab;touch-action:none;
                            user-select:none;font-weight:700;color:#26313d;">
                            <span id="${menuId}_title">缠论显示设置</span>
                            <span style="font-size:12px;font-weight:400;color:#7a8797;">拖动移动</span>
                        </div>
                        <div id="${menuId}_lvl_toggle" style="cursor:pointer;font-size:14px;color:#596779;padding:2px 0;user-select:none;">
                            <span id="${menuId}_lvl_arrow">▸</span> 当前周期 <b>${_curInterval}</b> · 结构来源说明
                        </div>
                        <div id="${menuId}_lvl_detail" style="display:none;font-size:13px;color:#687386;line-height:20px;
                            padding:5px 0 5px 14px;border-left:2px solid #dce3eb;margin:3px 0 5px 4px;">
                            中枢、走势类型、背驰与三类买卖点均来自同一份严格结构快照。
                        </div>

                        ${_grpTitle('基础结构')}
                        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:14px;">
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('fx')}" ${_checked('fx') ? 'checked' : ''}>
                                ${_dualSwatch(getSignalColor('fractalTop'), getSignalColor('fractalBottom'), '顶分型 / 底分型')}分型</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('bi')}" ${_checked('bi') ? 'checked' : ''}> ${_swatch(getDynamicColor(_curInterval, 'bis'))}笔</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('xd')}" ${_checked('xd') ? 'checked' : ''}> ${_swatch(getDynamicColor(_curInterval, 'xds'))}线段</label>
                            <label style="cursor:pointer;"><input type="checkbox" id="${cbId('center_observation')}" ${_checked('center_observation') ? 'checked' : ''}> ${_swatch(getRecursiveLevelColor(_curInterval, 0))}笔中枢观察</label>
                        </div>

                        ${_grpTitle('中枢控制', '严格递归结构')}
                        ${_cbRow('center_all', '中枢总开关')}
                        ${_cbRow('center_provisional', '形成中 / 投影（非正式）', false)}
                        <div style="padding-left:14px;display:flex;gap:12px;flex-wrap:wrap;font-size:14px;">
                            ${_centerLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('走势类型', '由当前 K 线递归产生')}
                        ${_cbRow('trend_all', '走势类型总开关')}
                        ${_cbRow('pending_movement', '待定尾段（非正式）', false)}
                        <div style="padding-left:14px;display:flex;gap:12px;flex-wrap:wrap;font-size:14px;">
                            ${_trendLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('买卖点')}
                        ${_cbRow('point_all', '买卖点总开关')}
                        <div style="padding:2px 0 2px 14px;font-size:13px;color:#687386;">按周期</div>
                        <div style="padding-left:14px;display:flex;gap:12px;flex-wrap:wrap;font-size:14px;">
                            ${_pointLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getRecursiveLevelColor(_curInterval, item.level))}${item.label}</label>`).join('')}
                        </div>
                        <div style="padding:5px 0 2px 14px;font-size:13px;color:#687386;">按类型</div>
                        <div style="padding-left:14px;display:grid;grid-template-columns:repeat(3,1fr);gap:4px 10px;font-size:14px;">
                            ${_pointTypes.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_swatch(getSignalColor(item.key.endsWith('buy') ? 'buy' : 'sell'))}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('背驰', '由当前 K 线递归产生')}
                        ${_cbRow('divergence_all', '背驰总开关')}
                        <div style="padding-left:14px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 10px;font-size:14px;">
                            ${_divergenceLevels.map((item) => `
                                <label style="cursor:pointer;"><input type="checkbox" id="${cbId(item.key)}"
                                    ${_checked(item.key) ? 'checked' : ''}>
                                    ${_dualSwatch(getSignalColor('buy'), getSignalColor('sell'), '向上背驰 / 向下背驰')}${item.label}</label>`).join('')}
                        </div>

                        ${_grpTitle('画线设置')}
                        <label style="display:block;cursor:pointer;font-size:14px;">
                            <input type="checkbox" id="${indCbId}" ${self.cl_independent_drawings ? 'checked' : ''}
                                style="margin-right:6px;vertical-align:middle;">独立周期画线
                        </label>
                        <div style="display:flex;gap:8px;padding-top:8px;">
                            <button id="${menuId}_all" type="button" style="flex:1;font-size:14px;padding:5px 8px;cursor:pointer;
                                border:1px solid #c5ced8;background:#f7f9fb;border-radius:4px;">全选</button>
                            <button id="${menuId}_none" type="button" style="flex:1;font-size:14px;padding:5px 8px;cursor:pointer;
                                border:1px solid #c5ced8;background:#f7f9fb;border-radius:4px;">全清</button>
                        </div>
                    </div>
                `;
                $('body').append(html);
                const menuElement = document.getElementById(menuId);
                let menuPlacement = null;
                // 工具栏按钮位于 TradingView 同源内嵌框架中，菜单则挂载在当前
                // ChartManager 文档内。页面被嵌入时（例如预选股页），window.top 属于
                // 另一套坐标空间；菜单定位和拖动必须以其实际所属窗口为准。
                const menuWindow = menuElement?.ownerDocument?.defaultView || window;
                if (menuElement) {
                    menuPlacement = positionClDisplayMenuNearPointer(
                        menuElement,
                        event,
                        btnDisplay,
                        menuWindow,
                    );
                    btnDisplay.setAttribute('aria-expanded', 'true');
                }

                // 级别映射 折叠/展开
                $('#' + menuId + '_lvl_toggle').on('click', function () {
                    const $d = $('#' + menuId + '_lvl_detail');
                    const $a = $('#' + menuId + '_lvl_arrow');
                    if ($d.is(':visible')) { $d.hide(); $a.text('▸'); }
                    else { $d.show(); $a.text('▾'); }
                });
                // 全选/全清:trigger change 让现有 toggle handler 走完整 cfg 保存 + redraw 流程
                // 排除「独立周期画线」(indCbId): 它是画线存储模式切换非显示项, 误触会切数据源(R1-C8)。
                $('#' + menuId + '_all').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').not('#' + indCbId).each(function () {
                        if (!this.checked) { this.checked = true; $(this).trigger('change'); }
                    });
                });
                $('#' + menuId + '_none').on('click', function (e) {
                    e.stopPropagation();
                    $('#' + menuId + ' input[type="checkbox"]').not('#' + indCbId).each(function () {
                        if (this.checked) { this.checked = false; $(this).trigger('change'); }
                    });
                });

                const keys = [
                    'fx', 'bi', 'xd', 'center_observation', 'center_all',
                    'center_provisional', 'trend_all', 'pending_movement',
                    'point_all', 'divergence_all',
                    ..._centerLevels.map((item) => item.key),
                    ..._trendLevels.map((item) => item.key),
                    ..._pointLevels.map((item) => item.key),
                    ..._pointTypes.map((item) => item.key),
                    ..._divergenceLevels.map((item) => item.key),
                ];
                keys.forEach(k => {
                    $('#' + cbId(k)).change(function () {
                        const checked = $(this).is(':checked');
                        self.cl_show_config[k] = checked;
                        saveClShowConfig(self.id, self._curResolution, self.cl_show_config);
                        console.log(`[cl_show_config] toggle ${k}=${checked}`);
                        self.debouncedDrawChanlun();
                    });
                });

                $('#' + indCbId).change(function () {
                    self.cl_independent_drawings = $(this).is(':checked');
                    saveClIndependentDrawings(self.id, self.cl_independent_drawings);
                    self._drawingsCache.clear();
                    if (self.chart && self.save_load_adapter) {
                        self.reloadDrawingsForCurrentContext('toggle-drawing-mode');
                    }
                    layer.msg(self.cl_independent_drawings ? '已切换为独立周期画线' : '已切换为共享画线', { time: 1000 });
                });

                // 不使用全屏 backdrop，避免挡住 TV iframe。capture 阶段同时监听 pointerdown
                // 与 click：按下图表空白处即关闭，也兼容键盘触发的 click；递归覆盖嵌套 iframe。
                if (menuElement) {
                    const dragHandle = document.getElementById(menuId + '_drag_handle');
                    const dragCleanup = bindClDisplayMenuDrag(
                        menuElement,
                        dragHandle,
                        menuWindow,
                    );
                    const viewportCleanup = bindClDisplayMenuViewportGuard(
                        menuElement,
                        menuWindow,
                    );
                    let combinedCleanup = null;
                    const outsideCleanup = bindClDisplayMenuOutsideDismiss(
                        document,
                        menuElement,
                        btnDisplay,
                        (dismissEvent) => {
                            if (typeof combinedCleanup === 'function') combinedCleanup();
                            menuElement.remove();
                            btnDisplay.setAttribute('aria-expanded', 'false');
                            if (dismissEvent && dismissEvent.type === 'keydown') {
                                try { btnDisplay.focus({ preventScroll: true }); }
                                catch (e) { btnDisplay.focus(); }
                            }
                            if (self._clDisplayMenuOutsideCleanup === combinedCleanup) {
                                self._clDisplayMenuOutsideCleanup = null;
                            }
                        },
                    );
                    combinedCleanup = () => {
                        outsideCleanup();
                        dragCleanup();
                        viewportCleanup();
                    };
                    self._clDisplayMenuOutsideCleanup = combinedCleanup;
                    if (menuPlacement && menuPlacement.anchor === 'trigger') {
                        try { menuElement.focus({ preventScroll: true }); }
                        catch (e) { menuElement.focus(); }
                    }
                }
            });
            btnDisplay.addEventListener('keydown', function (event) {
                if (!event || (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar')) return;
                if (event.repeat) return;
                event.preventDefault();
                event.stopPropagation();
                btnDisplay.click();
            });

            var buttonReload = global_widget.createButton();
            buttonReload.textContent = "重新加载数据";
            buttonReload.addEventListener("click", function () {
                buttonReload.setAttribute('aria-busy', 'true');
                Promise.resolve(self.manualReloadData()).catch(function (error) {
                    console.error('[CHARTS] manual data reload failed', error);
                    if (typeof layer !== 'undefined' && layer && typeof layer.msg === 'function') {
                        layer.msg('重新加载失败，请稍后重试');
                    }
                }).finally(function () {
                    buttonReload.setAttribute('aria-busy', 'false');
                });
            });

        });
        this.widget.onChartReady(() => {
            // widget 就绪后移除首屏骨架占位
            var sk = document.getElementById('tv_charts_skeleton');
            if (sk) sk.remove();
            this.chart = this.widget.activeChart();
            if (!this.chart) return;
            this.ensureRequestedDefaultStudies().catch((e) => {
                console.warn("[CHARTS] initialize requested default studies failed", e);
            });
            this._alignResolutionOnReady();   // 按真实周期校正显示配置(构造 _curResolution 仅为猜测)
            this.chart.applyOverrides({ "mainSeriesProperties.candleStyle.upColor": "#ef5350", "mainSeriesProperties.candleStyle.downColor": "#26a69a" });
            const registry = getTVRegistry();
            registry.widgets.set(this.instanceId, this.widget);
            registry.activeManagerId = this.instanceId;
            this.widget._chanlunManagerId = this.instanceId;
            this.udf_datafeed._chanlunManagerId = this.instanceId;

            this.chart.onSymbolChanged().subscribe(null, (s) => this.handleSymbolChange(s));
            this.chart.onIntervalChanged().subscribe(null, (i) => this.handleIntervalChange(i));
            this.chart.onDataLoaded().subscribe(
                null,
                () => this.handleDataReady(
                    this._dataContextGeneration || 0,
                    this._currentDataIdentityKey()
                ),
                true
            );
            const initialDataContextGeneration = this._dataContextGeneration || 0;
            const initialDataContextIdentity = this._currentDataIdentityKey();
            const readyNow = this.chart.dataReady(
                () => this.handleDataReady(initialDataContextGeneration, initialDataContextIdentity)
            );
            if (readyNow === true) {
                this.handleDataReady(initialDataContextGeneration, initialDataContextIdentity);
            }
            this.widget.subscribe("onTick", () => this.handleTick());
            this.chart.onVisibleRangeChanged().subscribe(null, () => this.handleVisibleRangeChange());

            this.reloadDrawingsForCurrentContext('initial-load');
            this._openSseStream();
            this.updateDrawPalette();   // 画图调色板:优先原生注入 TV 工具栏,失败回退浮层
            this.applyOverscrollGuard();   // 给 TV iframe 注入 overscroll-behavior:none(防 macOS 横滑后退)
            // TV 左侧工具栏异步渲染:重试注入(成功即停,最多 ~4.2s)
            if (this._lvlbtnTimer) clearInterval(this._lvlbtnTimer);
            this._lvlbtnTries = 0;
            this._lvlbtnTimer = setInterval(() => {
                this._lvlbtnTries++;
                this.applyOverscrollGuard();   // iframe 异步加载,随重试一并注入(幂等)
                if (this.injectDrawPaletteIntoTVToolbar() || this._lvlbtnTries >= 12) { clearInterval(this._lvlbtnTimer); this._lvlbtnTimer = null; }
            }, 350);

            // 注入 MACD 区间统计（工具栏按钮 + 右键菜单 + 侧边面板），依赖 chart/widget 已就绪
            try {
                if (window.MacdStats && typeof window.MacdStats.attach === 'function') {
                    window.MacdStats.attach(this);
                }
            } catch (e) {
                console.warn("[DEBUG-CHARTS] MacdStats.attach failed", e);
            }

            this.widget.subscribe('drawing_event', (id, eventType) => {
                // 手画线段/矩形:首次出现(任意事件类型,排除 remove)且当前选了画图色 → 套该色。
                // (实测 chart.applyOverrides 的 linetool 默认色对手画不生效,故用此事件兜底上色。)
                // 记录已上色 id,避免覆盖用户之后手动改的色;add 在 setProperties 前,防 properties_changed 自触发循环。
                if (!this._coloredDrawings) this._coloredDrawings = new Set();
                // ⚠ 只给「用户手画」图形上色,排除缠论自动图形(在 _reconcileOwnedIds 中)——缠论的笔/线段
                // 也是 trend_line,点击会触发 click 事件,否则被误染成当前画图色(用户报:一点就变红)。
                const _isChanlunShape = !!(this._reconcileOwnedIds && this._reconcileOwnedIds.has(id));
                const _suppressSave = this.shouldSuppressDrawingSave();
                if (!_suppressSave && !_isChanlunShape) {
                    if (!(this._userDrawingIds instanceof Set)) this._userDrawingIds = new Set();
                    if (eventType === 'remove') this._userDrawingIds.delete(String(id));
                    else this._userDrawingIds.add(String(id));
                }
                if (this._drawColor && eventType !== 'remove' && !_isChanlunShape && !this._coloredDrawings.has(id)) {
                    try {
                        const sh = this.chart.getShapeById(id);
                        if (sh && sh.setProperties) {
                            const p = sh.getProperties() || {};
                            const ov = {};
                            if ('linecolor' in p) ov.linecolor = this._drawColor;
                            if ('color' in p) ov.color = this._drawColor;
                            if ('backgroundColor' in p) ov.backgroundColor = this._drawColor;
                            if (Object.keys(ov).length) { this._coloredDrawings.add(id); sh.setProperties(ov); }
                        }
                    } catch (e) {}
                }
                if (eventType === 'remove' && this._coloredDrawings) this._coloredDrawings.delete(id);
                if (_suppressSave) return;
                clog("[DEBUG-CHARTS] drawing_event", id, eventType);
                this.scheduleDrawingsSave('drawing_event');
            });
            this.widget.subscribe('onAutoSaveNeeded', () => {
                if (this.shouldSuppressDrawingSave()) return;
                clog("[DEBUG-CHARTS] onAutoSaveNeeded");
                this.scheduleDrawingsSave('auto_save');
            });
        });
    }

    // ===== 按级别颜色「手动画线段 / 矩形」调色板 =====
    // 需求:用 TV 画线/矩形工具在图上手动作图,可挑各级别(笔/段/1m/5m/30m/日…)颜色来画。
    // 机制:点色块 → setDrawColor 设 TV 趋势线/矩形工具默认色;点「线/框」激活对应工具;
    //       手画完成时 drawing_event=create 再兜底套色。位置优先注入 TV 左侧工具栏,失败回退浮层。

    // 设当前「画图颜色」:applyOverrides 设趋势线/矩形默认色,之后手画的线段/矩形即此色。
    setDrawColor(color) {
        this._drawColor = color;
        try {
            this.chart.applyOverrides({
                "linetooltrendline.linecolor": color,
                "linetooltrendline.linewidth": 2,
                "linetoolrectangle.color": color,
                "linetoolrectangle.backgroundColor": color,
                "linetoolrectangle.linecolor": color,
                "linetoolrectangle.transparency": 80,
            });
        } catch (e) { /* override 失败不致命,create 事件仍会兜底套色 */ }
        try {
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                const g = dd && dd.getElementById('cl_tv_drawpal_' + this.id);
                if (g) this._paintDrawPalette(g);
            }
            const ov = document.getElementById('cl_drawpal_' + this.id);
            if (ov) this._paintDrawPalette(ov);
        } catch (e) {}
    }

    // 高亮调色板中当前画图色对应的色块。
    _paintDrawPalette(grp) {
        try {
            grp.querySelectorAll('.cl-drawcol').forEach(b => {
                const c = b.getAttribute('data-color') || '';
                const active = this._drawColor && c.toUpperCase() === this._drawColor.toUpperCase();
                b.style.background = active ? c : 'transparent';
                b.style.color = active ? '#fff' : c;
                b.style.boxShadow = active ? '0 0 0 1.5px #333' : 'none';
            });
        } catch (e) {}
    }

    // 往容器 grp 构建调色板内容:标题 + 各级色块(点=设画图色) + 线段/矩形工具按钮(点=激活工具)。
    _buildDrawPaletteInto(grp, doc, interval) {
        const { items } = this._levelBarItems(interval);
        const hd = doc.createElement('div');
        hd.textContent = '一键画';
        hd.title = '点对应级别的「线/框」即可直接画(已含选色+激活工具)';
        hd.style.cssText = 'font-size:10px; color:#999; user-select:none; text-align:center;';
        grp.appendChild(hd);
        // 列头:左=线段、右=矩形
        const colhd = doc.createElement('div');
        colhd.style.cssText = 'display:flex; gap:2px; font-size:9px; color:#aaa; user-select:none;';
        ['线', '框'].forEach(t => { const c = doc.createElement('div'); c.textContent = t; c.style.cssText = 'width:23px; text-align:center;'; colhd.appendChild(c); });
        grp.appendChild(colhd);
        // 每级一行:左「线段」按钮(下划线样式) + 右「矩形」按钮(方框样式),均为该级颜色。
        // 点一下 = 设画图色 + 激活对应工具,直接画(用户要的「一键直画」)。
        items.forEach((it) => {
            const row = doc.createElement('div');
            row.style.cssText = 'display:flex; gap:2px;';
            const mk = (tool, isBox) => {
                const b = doc.createElement('div');
                b.className = 'cl-drawbtn';
                b.textContent = it.label;
                b.title = '画 ' + it.label + ' 级别' + (isBox ? '矩形' : '线段') + '(一键:选色+激活工具)';
                b.style.cssText = 'width:23px; height:17px; line-height:14px; text-align:center; font-size:9.5px; font-weight:700; cursor:pointer; box-sizing:border-box; color:' + it.color + '; '
                    + (isBox
                        ? 'border:1.5px solid ' + it.color + '; border-radius:3px;'
                        : 'border-bottom:2.5px solid ' + it.color + ';');
                b.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.setDrawColor(it.color);
                    try { this.widget.selectLineTool(tool); } catch (err) {}
                });
                return b;
            };
            row.appendChild(mk('trend_line', false));
            row.appendChild(mk('rectangle', true));
            grp.appendChild(row);
        });
    }

    // 回退:把调色板做成左侧浮层(TV 工具栏注入失败时)。
    renderDrawPaletteOverlay() {
        try {
            const container = document.getElementById("tv_chart_container_" + this.id);
            if (!container) return;
            if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
            let interval = '?';
            try { interval = this.widget.symbolInterval().interval; } catch (e) {}
            const { sig } = this._levelBarItems(interval);
            const barId = 'cl_drawpal_' + this.id;
            let bar = document.getElementById(barId);
            if (bar && bar.getAttribute('data-sig') === sig) { this._paintDrawPalette(bar); bar.style.display = 'flex'; return; }
            if (bar) bar.remove();
            bar = document.createElement('div');
            bar.id = barId;
            bar.setAttribute('data-sig', sig);
            bar.style.cssText = 'position:absolute; left:6px; top:64px; z-index:42; display:flex; flex-direction:column; align-items:center; gap:3px; background:rgba(255,255,255,0.85); padding:4px 3px; border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,0.18);';
            this._buildDrawPaletteInto(bar, document, interval);
            container.appendChild(bar);
        } catch (e) {
            console.warn('[CHARTS] renderDrawPaletteOverlay failed', e);
        }
    }

    // 当前周期 → 调色板级别颜色项(只各递归级别 1m/5m/30m/日线…,label + 链色;不含笔/段基础)。
    _levelBarItems(interval) {
        const levels = recursiveDisplayLevels(interval);
        const items = levels.map(({ label, level }) => ({
            label,
            color: getRecursiveLevelColor(interval, level),
        }));
        return { items, sig: `${interval}|${levels.map((item) => item.label).join('|')}` };
    }

    // 把「画图调色板」原生注入 TV 左侧画线工具栏列顶部。锚点用几何探测(窄<70+高>400+最左+含多个 group 子)
    // 而非哈希类名,较抗 TV 升级;找不到工具栏返回 false → 调用方回退浮层 renderDrawPaletteOverlay。
    injectDrawPaletteIntoTVToolbar() {
        try {
            let doc = null, inner = null;
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                if (!dd) continue;
                let cand = null;
                dd.querySelectorAll('div').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 70 && r.height > 400 && r.left < 12 && el.children.length >= 3) {
                        if (!cand || r.height > cand.getBoundingClientRect().height) cand = el;
                    }
                });
                if (cand) { doc = dd; inner = cand; break; }
            }
            if (!doc || !inner) return false;
            let interval = '?';
            try { interval = this.widget.symbolInterval().interval; } catch (e) {}
            const { sig } = this._levelBarItems(interval);
            const hideOverlay = () => { const ov = document.getElementById('cl_drawpal_' + this.id); if (ov) ov.style.display = 'none'; };
            const grpId = 'cl_tv_drawpal_' + this.id;
            let grp = doc.getElementById(grpId);
            if (grp && grp.isConnected && grp.getAttribute('data-sig') === sig) {
                this._paintDrawPalette(grp);
                hideOverlay();
                return true;
            }
            if (grp) grp.remove();
            grp = doc.createElement('div');
            grp.id = grpId;
            grp.setAttribute('data-sig', sig);
            grp.style.cssText = 'display:flex; flex-direction:column; align-items:center; gap:3px; padding:6px 0 5px; width:52px; border-bottom:1px solid rgba(120,120,120,0.3);';
            this._buildDrawPaletteInto(grp, doc, interval);
            inner.insertBefore(grp, inner.firstChild);
            hideOverlay();
            return true;
        } catch (e) {
            console.warn('[CHARTS] injectDrawPaletteIntoTVToolbar failed', e);
            return false;
        }
    }

    // macOS 触控板两指横向 swipe 会触发浏览器后退/前进:在图表上想左右平移却跳转到上/下一页。
    // 顶层 app.css 已设 overscroll-behavior:none,但 TV 图表在 iframe 内、iframe 是独立文档,横向
    // overscroll 可能在 iframe 层就被浏览器当成导航手势。这里给同源 TV iframe 的 html/body 也注入
    // overscroll-behavior:none(双保险)。跨源 iframe(contentDocument 抛错)跳过;幂等,可随
    // onChartReady / _lvlbtnTimer 重试多次调用(iframe 异步加载)。
    applyOverscrollGuard() {
        try {
            for (const f of document.querySelectorAll('iframe')) {
                let dd; try { dd = f.contentDocument; } catch (e) { continue; }
                if (!dd) continue;
                try {
                    if (dd.documentElement) dd.documentElement.style.overscrollBehavior = 'none';
                    if (dd.body) dd.body.style.overscrollBehavior = 'none';
                } catch (e) { /* 同源但访问受限,跳过 */ }
            }
        } catch (e) { /* querySelectorAll 失败极罕见,静默 */ }
    }

    // 画图调色板统一入口:优先原生注入 TV 工具栏;失败回退左侧浮层。
    updateDrawPalette() {
        if (!this.injectDrawPaletteIntoTVToolbar()) this.renderDrawPaletteOverlay();
    }

    handleSymbolChange(symbol) {
        if (!symbol?.ticker) return;
        const [marketRaw, code] = symbol.ticker.split(":");
        if (!marketRaw || !code) return;
        // ⚠ market 必须归一小写: symbol.ticker 来自 TV 是大写(如 "US:QQQ.US"，因 chart.symbol()
        // 返回大写)，而 Utils.get_market() 存的是小写 "us"。不归一 → "us" !== "US" 恒成立 →
        // 每次切标的都误判 market 变 → location.reload() 整页刷新 → 多标的切换卡死(浏览器实测复现+补丁验证)。
        const market = marketRaw.toLowerCase();
        if (Utils.get_market() !== market) { Utils.set_local_data("market", market); location.assign("/?market=" + encodeURIComponent(market)); return; }
        Utils.set_local_data("market", market); Utils.set_local_data(`${market}_code`, code);
        this._resetDataReadyContext();
        this._latestAppliedBarTime = null;
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('symbol-change');
        this._openSseStream();
        if (typeof ZiXuan.render_zixuan_opts === "function") ZiXuan.render_zixuan_opts();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 同市场切标的:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }
    // 切周期时的显示配置编排:把当前配置存回旧周期 key,解析并应用新周期配置(未配过则继承当前并固化),
    // 更新 _curResolution。纯前端 localStorage,不触发缠论重算;重绘由既有 handleDataReady→重绘链路负责。
    _applyResolutionConfig(newResolution) {
        const oldRes = this._curResolution;
        if (oldRes && oldRes !== newResolution && this.cl_show_config) {
            saveClShowConfig(this.id, oldRes, this.cl_show_config);
        }
        const r = resolveClConfigForResolution(this.id, newResolution, this.cl_show_config);
        this.cl_show_config = r.cfg;
        if (r.persist) {
            saveClShowConfig(this.id, newResolution, r.cfg);
        }
        this._curResolution = newResolution;
    }

    // Finding 1(审计 MED):chart 就绪后按真实周期校正 _curResolution。构造函数的 _curResolution 是猜测
    // (localStorage 或 '1'),可能与 TV 实际显示周期(load_last_chart 存档 / TV 默认日线)不符;就绪后以
    // chart.resolution() 为准,否则首次加载 toggle 会把显示配置存到错误周期 key。
    _alignResolutionOnReady() {
        try {
            const realRes = (this.chart && this.chart.resolution) ? this.chart.resolution() : null;
            if (realRes && realRes !== this._curResolution) {
                this._applyResolutionConfig(realRes);
            }
        } catch (e) { /* resolution() 异常不影响首绘 */ }
    }

    handleIntervalChange(interval) {
        if (!interval) return;
        const market = Utils.get_market(); if (!market) return;

        this._resetDataReadyContext();
        this._drawRetryCount = 0;
        this._latestAppliedBarTime = null;
        const currentSeq = ++this._intervalSwitchSeq;
        this._intervalGeneration++;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleIntervalChange → ${interval} (seq=${currentSeq}, generation=${this._intervalGeneration}) [_initialLoadDone reset to false]`);
        Utils.set_local_data(`${market}_interval_${this.id}`, interval);
        this._applyResolutionConfig(interval);   // 切周期:存旧周期配置、载入本周期配置(未配过继承当前)
        this.clear_draw_chanlun();
        this.reloadDrawingsForCurrentContext('interval-change');
        this._openSseStream();
        setTimeout(() => this._maybeWidenDefaultView(), 400);   // 切周期:缓存命中时 handleDataReady 不来,这里兜底拉宽默认视窗
    }

    handleDataReady(
        contextGeneration = this._dataContextGeneration || 0,
        expectedIdentity = this._currentDataIdentityKey()
    ) {
        if (contextGeneration !== (this._dataContextGeneration || 0)) return false;
        const currentIdentity = this._currentDataIdentityKey();
        if (!currentIdentity || expectedIdentity !== currentIdentity) return false;
        if (!this._chartDataReadyNow()) return false;

        const wasInitialLoad = (
            this._tvDataReadyGeneration !== contextGeneration ||
            this._tvDataReadyIdentity !== currentIdentity
        );
        const hasPendingDraw = (
            this._pendingChanlunDrawGeneration === contextGeneration &&
            this._pendingChanlunDrawIdentity === currentIdentity
        );
        const hadReconcileFailures = (this._reconcileRetry?.count || 0) > 0;
        if (hadReconcileFailures) this._resetReconcileRetry();
        this._tvDataReadyGeneration = contextGeneration;
        this._tvDataReadyIdentity = currentIdentity;
        this._dataReadyProbeGeneration = null;
        this._dataReadyProbeIdentity = null;
        this._pendingChanlunDrawGeneration = null;
        this._pendingChanlunDrawIdentity = null;
        this._initialLoadDone = true;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleDataReady ✓ context=${contextGeneration} initial=${wasInitialLoad} pending=${hasPendingDraw}`);
        if (!this._maybeApplyCausalFocus()) this._maybeWidenDefaultView();

        if (hasPendingDraw || wasInitialLoad || hadReconcileFailures) {
            if (wasInitialLoad) this.draw_chanlun();
            else this.debouncedDrawChanlun();
        }
        return true;
    }

    _ensureCausalAuditMarker(lock, si, key, focusAt) {
        if (this._causalAuditMarkerSetFor === key) return true;
        if (!this.chart || typeof this.chart.createMultipointShape !== 'function') {
            return false;
        }
        const point = { time: focusAt };
        const options = {
            shape: 'vertical_line',
            text: `${lock.point_type || 'AUDIT'} · 时间证据（无价格锚点）`,
            lock: true,
            disableSelection: true,
            disableSave: true,
            disableUndo: true,
            showInObjectsTree: false,
            overrides: {
                linecolor: '#f59e0b',
                linewidth: 2,
                linestyle: 2,
                showLabel: true,
            },
        };
        try {
            // 此处只提供时间坐标。RiskMappingPointEvidence 有意不记录价格；若从当前
            // 渲染 K 线虚构价格，会把时间审计标记错误地变成价格证据。
            this._causalAuditMarkerSetFor = key;
            const marker = this.chart.createMultipointShape([point], options);
            if (marker && typeof marker.then === 'function') {
                Promise.resolve(marker).then(
                    (id) => { this._causalAuditMarkerId = id; },
                    () => {
                        if (this._causalAuditMarkerSetFor === key) {
                            this._causalAuditMarkerSetFor = null;
                        }
                    },
                );
            } else {
                this._causalAuditMarkerId = marker;
            }
            return true;
        } catch (e) {
            if (this._causalAuditMarkerSetFor === key) {
                this._causalAuditMarkerSetFor = null;
            }
            return false;
        }
    }

    // 审计页的稳定买卖点链接带有服务端已复核的因果锁。首次数据就绪后把
    // 视窗移到点位锚定时间；review_as_of 仍由每次 /tv/history 请求强制截断，
    // 因此聚焦历史结构不会加载该次观察之后的 K 线。
    _maybeApplyCausalFocus() {
        try {
            const lock = (typeof window !== 'undefined')
                ? window.__chanlunReviewChartLock
                : null;
            const focusAt = Number(lock?.focus_at);
            const reviewAsOf = Number(lock?.review_as_of);
            if (
                !lock || lock.lock_kind !== 'RISK_POINT_AUDIT'
                || !Number.isFinite(focusAt) || !Number.isFinite(reviewAsOf)
                || focusAt <= 0 || focusAt > reviewAsOf
            ) return false;
            const si = this.widget && this.widget.symbolInterval
                ? this.widget.symbolInterval()
                : null;
            if (!si || !si.symbol || !si.interval || !this.chart) return false;
            const rawSymbol = String(si.symbol);
            const separator = rawSymbol.indexOf(':');
            const sourceSymbol = separator >= 0
                ? rawSymbol.slice(separator + 1)
                : rawSymbol;
            if (
                sourceSymbol !== String(lock.symbol)
                || String(si.interval) !== String(lock.chart_interval)
            ) return false;
            const key = `${lock.candidate_id}|${si.symbol}|${si.interval}|${focusAt}`;
            this._ensureCausalAuditMarker(lock, si, key, focusAt);
            if (this._causalFocusSetFor === key) return true;
            const spanDays = {
                '1': 2, '5': 6, '30': 45,
                '1D': 400, '1W': 1825, '1M': 5475,
            }[String(si.interval)];
            if (!spanDays) return false;
            const span = spanDays * 86400;
            const target = {
                from: focusAt - span * 0.45,
                to: Math.min(reviewAsOf, focusAt + span * 0.55),
            };
            if (target.from >= target.to) return false;
            this._causalFocusSetFor = key;
            setTimeout(() => {
                try {
                    const current = this.widget && this.widget.symbolInterval
                        ? this.widget.symbolInterval()
                        : null;
                    if (
                        !current
                        || `${lock.candidate_id}|${current.symbol}|${current.interval}|${focusAt}` !== key
                    ) return;
                    this.chart.setVisibleRange(target);
                } catch (e) { /* 聚焦失败不影响只读图表 */ }
            }, 150);
            return true;
        } catch (e) {
            return false;
        }
    }

    // 首次加载某 标的+周期 时,若默认可视窗过窄(实测外汇默认仅 ~4h/~43根 → 只见 1 笔,而数据有 438 笔),
    // 按周期拉宽到一个合理跨度,让图表一打开就显示更多历史与缠论。仅首次、且仅当过窄时设,
    // 不夺已够宽的图(A股默认已宽则跳过)或用户后续缩放。设更宽视窗会触发 TV 按需加载更早的 K 线。
    _maybeWidenDefaultView() {
        try {
            const si = this.widget && this.widget.symbolInterval ? this.widget.symbolInterval() : null;
            if (!si || !si.symbol || !si.interval) return;
            const key = si.symbol + '|' + si.interval;
            if (this._viewSetFor === key) return;
            // 当前 标的+周期 的 K 线必须已加载,否则数据/视窗未就绪 → 不设标记,等下次(handleDataReady/切换)再试。
            const resKey = String(si.symbol).toLowerCase() + String(si.interval).toLowerCase();
            const hp = this.udf_datafeed && this.udf_datafeed._historyProvider;
            const br = hp && hp.bars_result && hp.bars_result.get(resKey);
            if (!br || !br.bars || br.bars.length < 2) return;
            const vr = this.chart && this.chart.getVisibleRange ? this.chart.getVisibleRange() : null;
            if (!vr || !vr.from || !vr.to || vr.to <= vr.from) return;   // 数据/视窗未就绪,下次再试
            const toEpochSeconds = (value) => {
                const numeric = Number(value);
                return Number.isFinite(numeric) && Math.abs(numeric) >= 100000000000
                    ? numeric / 1000
                    : numeric;
            };
            const firstBarSec = toEpochSeconds(br.bars[0]?.time);
            const lastBarSec = toEpochSeconds(br.bars[br.bars.length - 1]?.time);
            if (!Number.isFinite(firstBarSec) || !Number.isFinite(lastBarSec) || firstBarSec > lastBarSec) return;
            const visibleOutsideLoadedBars = vr.to < firstBarSec || vr.from > lastBarSec;
            this._viewSetFor = key;
            // 各周期默认视窗跨度(日历天);跨度按天数,外汇 24h 连续→根数多,A股有夜盘缺口→根数少,均显示充足缠论。
            // 白名单只列分钟~月线;未列出的周期(秒线10S/30S、季线3M、年线12M等)直接跳过不拉宽——
            // 否则 6 天 fallback 对秒线会触发拉数万根、对季/年线又过窄;且这些周期 TV 默认视窗本就够宽。
            // 5m 是正式交易结构级别，6 天视窗通常容不下一条完整走势，导致严格线
            // 因端点在画面外而全部隐藏。默认展开到 90 天；正式结构优先配置会同时
            // 关闭高密度分型/笔/线段，因此不会把扩大视窗转化为数百个绘图实体。
            // 30m / 日线继续保留约 3 个月 / 2.2 年的审阅跨度。
            const SPAN_DAYS = { '1': 2, '2': 3, '3': 3, '5': 90, '10': 8, '15': 12, '30': 90, '60': 90, '120': 120, '180': 150, '240': 200, '1D': 800, '2D': 700, '1W': 1825, '1M': 5475 };
            const days = SPAN_DAYS[si.interval];
            if (!days) return;   // 周期不在白名单(秒/季/年等)→保持 TV 默认视窗
            const span = days * 86400;
            if (!visibleOutsideLoadedBars && (vr.to - vr.from) >= span * 0.7) return;   // 当前已够宽且与行情相交→不动
            setTimeout(() => {
                try {
                    // 竞态防护:延时期间用户若已切到别的标的/周期,放弃,避免把视窗设成上一周期的范围。
                    const si2 = this.widget && this.widget.symbolInterval ? this.widget.symbolInterval() : null;
                    if (!si2 || (si2.symbol + '|' + si2.interval) !== key) return;
                    const v = this.chart.getVisibleRange();
                    const targetTo = visibleOutsideLoadedBars ? lastBarSec : v?.to;
                    if (!targetTo) return;
                    this.chart.setVisibleRange({
                        from: visibleOutsideLoadedBars
                            ? Math.max(firstBarSec, targetTo - span)
                            : targetTo - span,
                        to: targetTo,
                    });
                } catch (e) {}
            }, 150);
        } catch (e) { /* 视窗调整失败不影响主流程 */ }
    }
    handleTick() {
        const identity = this.getCurrentChartIdentity();
        if (!identity) return;
        const symbolResKey = `${identity.symbol.toString().toLowerCase()}${identity.interval.toString().toLowerCase()}`;
        const barsResult = this.udf_datafeed?._historyProvider?.bars_result?.get(symbolResKey);
        const latestBar = barsResult?.bars?.[barsResult.bars.length - 1];
        if (!latestBar || latestBar.time === this._latestAppliedBarTime) {
            return;
        }
        clog("[DataVerify][Charts] handleTick newBar key=" + symbolResKey, {
            barTime: latestBar.time,
            prevTime: this._latestAppliedBarTime,
            barsCount: barsResult?.bars?.length || 0,
        });
        this._latestAppliedBarTime = latestBar.time;
        this.debouncedDrawChanlun();
    }
    handleVisibleRangeChange() {
        if (this._initialLoadDone) {
            // 加载更早或更晚的可视区间后，先前被拒绝的锚点可能已能落在画布上。
            if ((this._reconcileRetry?.count || 0) > 0) {
                this._clearReconcileRetryBudget();
            }
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleVisibleRangeChange → debouncedDrawChanlun (will fire 300ms later)`);
            this.debouncedDrawChanlun();
        } else {
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms handleVisibleRangeChange SKIPPED (_initialLoadDone=false)`);
        }
    }

    _autoEntityPresence(entityId) {
        if (!this.chart || typeof this.chart.getAllShapes !== 'function') return null;
        try {
            const shapes = this.chart.getAllShapes();
            if (!Array.isArray(shapes)) return undefined;
            return shapes.some((shape) => shape && shape.id === entityId);
        } catch (error) {
            console.warn(`[CHANLUN-DIAG][safeRemove] getAllShapes 抛错 id=${entityId}`, error);
            return undefined;
        }
    }

    _markAutomaticShapeId(entityId) {
        if (entityId == null) return entityId;
        if (!(this._reconcileOwnedIds instanceof Set)) this._reconcileOwnedIds = new Set();
        this._reconcileOwnedIds.add(entityId);
        // 异步创建事件可能先于其承诺完成，并短暂表现为用户绘图。所有权确定后必须在
        // 持久化前撤销这一临时用户分类。
        this._userDrawingIds?.delete(String(entityId));
        this._coloredDrawings?.delete(entityId);
        return entityId;
    }

    _finishAutoEntityRemoval(entityId) {
        const presence = this._autoEntityPresence(entityId);
        if (presence === false) {
            this._reconcileOwnedIds?.delete(entityId);
            this._pendingRemovalIds?.delete(entityId);
            return true;
        }
        if (presence === true) {
            console.warn(`[CHANLUN-DIAG][safeRemove] entity 仍存在，保留追踪等待重试 id=${entityId}`);
        }
        return false;
    }

    _removeAutoEntityId(entityId) {
        if (!entityId) return Promise.resolve(false);
        if (!(this._reconcileOwnedIds instanceof Set)) this._reconcileOwnedIds = new Set();
        if (!(this._pendingRemovalIds instanceof Set)) this._pendingRemovalIds = new Set();
        // late async create 可能尚未进入 container；先登记所有权，删除静默失败时
        // sweep 仍能识别它是自动图形。
        this._markAutomaticShapeId(entityId);
        this._pendingRemovalIds.add(entityId);

        let removalResult;
        try {
            removalResult = this.chart.removeEntity(entityId);
        } catch (error) {
            console.warn(`[CHANLUN-DIAG][safeRemove] removeEntity 抛错 id=${entityId}`, error);
            return Promise.resolve(false);
        }
        if (removalResult != null && typeof removalResult.then === 'function') {
            return Promise.resolve(removalResult).then(
                () => this._finishAutoEntityRemoval(entityId),
                (error) => {
                    console.warn(`[CHANLUN-DIAG][safeRemove] removeEntity promise rejected id=${entityId}`, error);
                    return false;
                },
            );
        }
        return Promise.resolve(this._finishAutoEntityRemoval(entityId));
    }

    safeRemove(entityId) {
        if (!entityId) return Promise.resolve(false);
        if (typeof entityId.then === 'function') {
            return Promise.resolve(entityId).then(
                (resolvedId) => this._removeAutoEntityId(resolvedId),
                (error) => {
                    console.warn('[CHANLUN-DIAG][safeRemove] entity promise rejected', error);
                    return false;
                },
            );
        }
        return this._removeAutoEntityId(entityId);
    }

    clear_draw_chanlun(clear_type) {
        this.markDrawingMutationStart('chanlun-clear');
        // 全量/末段清除都让 reconcile 走重新创建路径，重置失败重试计数
        // 避免上次首屏未 ready 攒到上限后影响切换/清除后的新一轮重试
        this._resetReconcileRetry();
        const removePromises = [];
        this._clearAllStrictScopes('clear-drawings');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        if (clear_type == "last") {
            for (const symbolKey in this.obj_charts) {
                for (const chartType in this.obj_charts[symbolKey]) {
                    if (this.obj_charts[symbolKey][chartType].length == 0) continue;
                    const maxTime = Math.max(...this.obj_charts[symbolKey][chartType].map((item) => item.time));
                    for (const _i in this.obj_charts[symbolKey][chartType]) {
                        const item = this.obj_charts[symbolKey][chartType][_i];
                        if (item.time == maxTime) {
                            removePromises.push(this.safeRemove(item.id));
                        }
                    }
                    this.obj_charts[symbolKey][chartType] = this.obj_charts[symbolKey][chartType].filter((item) => item.time != maxTime);
                }
            }
        } else {
            Object.values(this.obj_charts).forEach((symbolData) => {
                Object.values(symbolData).forEach((chartItems) => {
                    chartItems.forEach((item) => {
                        removePromises.push(this.safeRemove(item.id));
                    });
                });
            });
            this.obj_charts = {};
        }
        Promise.allSettled(removePromises).then(() => {
            this.markDrawingMutationEnd('chanlun-clear');
            this.sweepOrphanShapes();
        });
    }

    _strictApi() {
        const api = (typeof window !== 'undefined') ? window.ChartStructureReconcile : null;
        if (!api || typeof api.planReconcile !== 'function' || typeof api.ReconcileEpoch !== 'function') {
            throw new Error('strict chart reconcile helper is unavailable');
        }
        return api;
    }

    _ensureStrictReconcileState() {
        if (!(this._strictContainers instanceof Map)) this._strictContainers = new Map();
        if (!(this._strictScopes instanceof Set)) this._strictScopes = new Set();
        if (!(this._strictDesiredByScope instanceof Map)) this._strictDesiredByScope = new Map();
        if (!(this._strictDesiredItemsByScope instanceof Map)) this._strictDesiredItemsByScope = new Map();
        if (!(this._strictPendingCreates instanceof Map)) this._strictPendingCreates = new Map();
        if (!(this._reconcileOwnedIds instanceof Set)) this._reconcileOwnedIds = new Set();
        if (!(this._pendingRemovalIds instanceof Set)) this._pendingRemovalIds = new Set();
        if (!this._strictReconcileEpoch) {
            this._strictReconcileEpoch = new (this._strictApi().ReconcileEpoch)();
        }
    }

    _strictFrequencyFromResolution(resolution) {
        const value = String(resolution == null ? '' : resolution).trim();
        const fixed = {
            '10S': '10s', '30S': '30s',
            '120': '120m', '180': '3h', '240': '4h',
            '360': '6h', '480': '8h', '720': '12h',
            '1D': 'd', '2D': '2d', '3D': '3d', '1W': 'w', '1M': 'm',
            '3M': 'q', '12M': 'y',
        };
        if (fixed[value]) return fixed[value];
        if (/^[1-9][0-9]*$/.test(value)) return `${value}m`;
        return value.toLowerCase();
    }

    _strictNormalizeSymbol(symbol) {
        return String(symbol == null ? '' : symbol)
            .replace(/^[^:]+:/, '')
            .trim()
            .toUpperCase();
    }

    _setStrictStructureStatus(state, code = null) {
        this._strictStructureStatus = { state, code };
        try {
            const host = document.getElementById(`tv_chart_container_${this.id}`);
            if (!host) return;
            const statusId = `strict_structure_status_${this.id}`;
            let status = document.getElementById(statusId);
            if (state === 'ready') {
                if (status && typeof status.remove === 'function') status.remove();
                return;
            }
            if (!status) {
                status = document.createElement('div');
                status.id = statusId;
                status.className = 'strict-structure-status';
                status.setAttribute('role', 'status');
                host.appendChild(status);
            }
            status.dataset.state = state;
            status.textContent = state === 'unavailable'
                ? `严格缠论结构暂不可用（${code || 'unknown'}）`
                : state === 'stale'
                    ? `严格缠论结构正在同步，暂沿用最近有效结果（${code || 'unknown'}）`
                    : '正在同步严格缠论结构…';
        } catch (e) { /* 状态提示失败不能阻断 K 线和严格实体清理 */ }
    }

    _strictLoadedRange(bars) {
        if (!Array.isArray(bars) || bars.length === 0) {
            throw new Error('strict chart requires loaded bars');
        }
        const api = this._strictApi();
        const barTimes = bars.map((bar) => api.barTimeMsToEpochSeconds(bar.time));
        const from = barTimes[0];
        const to = barTimes[barTimes.length - 1];
        if (from > to) throw new Error('loaded bars must be ordered');
        for (let index = 1; index < barTimes.length; index += 1) {
            if (barTimes[index] < barTimes[index - 1]) {
                throw new Error('loaded bars must be ordered');
            }
        }
        return { from, to, barTimes };
    }

    _strictSourceClosedAt(barsResult) {
        const source = barsResult || {};
        if (Object.prototype.hasOwnProperty.call(source, 'times')) {
            if (!Array.isArray(source.times) || source.times.length === 0) {
                throw new Error('strict structure raw source close is invalid');
            }
            return this._strictApi().barTimeMsToEpochSeconds(
                source.times[source.times.length - 1],
            );
        }
        return this._strictLoadedRange(source.bars).to;
    }

    _validateStrictStructureSnapshot(snapshot, chartData, currentInterval) {
        if (!snapshot || snapshot.schema !== 'chanlun-chart-structure') {
            throw new Error('strict structure schema mismatch');
        }
        const requiredStrings = [
            'symbol', 'source_frequency', 'display_frequency',
            'price_basis_revision', 'structure_price_quantum',
            'strict_config_revision', 'structure_revision',
            'snapshot_revision', 'render_revision',
        ];
        for (const field of requiredStrings) {
            if (typeof snapshot[field] !== 'string' || snapshot[field].length === 0) {
                throw new Error(`strict structure ${field} is required`);
            }
        }
        if (!Number.isInteger(snapshot.source_closed_at)) {
            throw new Error('strict structure source_closed_at must be epoch seconds');
        }
        if (!Number.isFinite(Number(snapshot.structure_price_quantum)) || Number(snapshot.structure_price_quantum) <= 0) {
            throw new Error('strict structure price quantum is invalid');
        }
        if (
            !Array.isArray(snapshot.stroke_center_observations)
            || !Array.isArray(snapshot.levels)
        ) {
            throw new Error('strict structure collections are invalid');
        }
        const formalDirection = snapshot.formal_direction;
        if (
            !formalDirection
            || !['up', 'down', 'neutral'].includes(formalDirection.direction)
            || !Array.isArray(formalDirection.reason_codes)
            || formalDirection.reason_codes.length === 0
            || formalDirection.reason_codes.some(
                (value) => typeof value !== 'string' || !value,
            )
            || (
                formalDirection.structural_level !== null
                && !Number.isInteger(formalDirection.structural_level)
            )
        ) {
            throw new Error('strict structure formal direction is invalid');
        }

        const displayFrequency = this._strictFrequencyFromResolution(currentInterval);
        const chartSymbol = this._strictNormalizeSymbol(
            chartData.chartSymbol || this.widget?.symbolInterval?.()?.symbol,
        );
        if (
            this._strictNormalizeSymbol(snapshot.symbol) !== chartSymbol ||
            snapshot.display_frequency !== displayFrequency ||
            snapshot.source_frequency !== displayFrequency
        ) {
            throw new Error('strict structure chart context mismatch');
        }

        const loadedRange = this._strictLoadedRange(chartData.barsResult?.bars);
        const sourceClosedAt = this._strictSourceClosedAt(chartData.barsResult);
        if (snapshot.source_closed_at !== sourceClosedAt) {
            throw new Error('strict structure source close does not match loaded bars');
        }
        const rawVisible = chartData.visibleRange || { from: chartData.from, to: loadedRange.to };
        const visibleRange = {
            from: Math.floor(Number(rawVisible.from)),
            to: Math.ceil(Number(rawVisible.to)),
        };
        if (!Number.isInteger(visibleRange.from) || !Number.isInteger(visibleRange.to) || visibleRange.from > visibleRange.to) {
            throw new Error('strict structure visible range is invalid');
        }
        const context = {
            chartInstanceId: this.instanceId || `chart-manager-${this.id}`,
            symbol: this._strictNormalizeSymbol(snapshot.symbol),
            interval: displayFrequency,
            price_basis_revision: snapshot.price_basis_revision,
        };
        const contextToken = JSON.stringify([
            context.chartInstanceId,
            context.symbol,
            context.interval,
            context.price_basis_revision,
            snapshot.source_closed_at,
            snapshot.structure_revision,
            snapshot.snapshot_revision,
            snapshot.render_revision,
        ]);
        return { context, contextToken, loadedRange, visibleRange };
    }

    _strictItemEnabled(item) {
        return strictItemEnabled(this.cl_show_config || {}, item);
    }

    _strictPreviewSupersedesCenter(center, preview) {
        const centerUnitIds = Array.isArray(center?.body_unit_ids)
            ? center.body_unit_ids
            : center?.initial_unit_ids;
        const previewUnitIds = Array.isArray(preview?.body_unit_ids)
            ? preview.body_unit_ids
            : preview?.initial_unit_ids;
        if (!Array.isArray(centerUnitIds) || !Array.isArray(previewUnitIds)) return false;

        const previewUnits = new Set(previewUnitIds.filter(Boolean));
        const sharedUnitIds = centerUnitIds.filter((unitId) => previewUnits.has(unitId));
        // Optional entry legs are external evidence.  Only shared center-body
        // units can make a preview supersede the current formal rectangle.
        return sharedUnitIds.length > 0;
    }

    _strictRenderGroups(snapshot, context) {
        const api = this._strictApi();
        const groups = new Map();
        const add = (values, levelLabel = null) => {
            for (const rawItem of values || []) {
                const labeledItem = (
                    levelLabel && (
                        rawItem?.render_kind === 'strict_divergence'
                        || rawItem?.render_kind === 'point_confirmed'
                        || rawItem?.render_kind === 'point_approaching'
                    )
                        ? { ...rawItem, level_label: levelLabel }
                        : rawItem
                );
                if (!labeledItem || !Number.isInteger(labeledItem.structural_level) || !Array.isArray(labeledItem.points)) {
                    throw new Error('strict render item is invalid');
                }
                // 严格证据保留精确市场收盘时刻作为审计身份；绘图时使用与
                // TradingView 日历 K 线一致的 UTC 周期锚点。
                const item = api.itemToChartCoordinates(labeledItem, context.interval);
                if (!this._strictItemEnabled(item)) continue;
                const scope = api.scopeKey(context, item);
                if (!groups.has(scope)) groups.set(scope, []);
                groups.get(scope).push(item);
            }
        };
        for (const observation of snapshot.stroke_center_observations) {
            if (observation?.render_kind !== 'center_observation'
                || observation?.source_kind !== 'stroke_observation') {
                throw new Error('strict stroke center observation is invalid');
            }
            validateStrictCenterRenderContract(observation, 0, false);
        }
        add(snapshot.stroke_center_observations);
        for (const level of snapshot.levels) {
            if (
                !level || !Number.isInteger(level.structural_level)
                || typeof level.label !== 'string' || !level.label
                || level.origin !== 'current_chart_recursive'
            ) throw new Error('strict level is invalid');
            const levelDirection = level.formal_direction;
            if (
                !levelDirection
                || !['up', 'down', 'neutral'].includes(levelDirection.direction)
                || !Array.isArray(levelDirection.reason_codes)
                || levelDirection.reason_codes.length === 0
                || (
                    levelDirection.structural_level !== null
                    && levelDirection.structural_level !== level.structural_level
                )
            ) throw new Error('strict level formal direction is invalid');
            const requiredCollections = [
                'centers', 'center_previews', 'center_projections',
                'current_trends', 'pending_movements', 'completed_trend_snapshots',
                'confirmed_points', 'approaching_points', 'divergences',
            ];
            if (requiredCollections.some((field) => !Array.isArray(level[field]))) {
                throw new Error('strict level collections are invalid');
            }
            const expectedSourceKind = level.structural_level === 0 ? 'segment' : 'trend_type';
            for (const item of level.centers) {
                if (item?.render_kind !== 'formal_center'
                    || item?.source_kind !== expectedSourceKind) {
                    throw new Error('strict formal center source is invalid');
                }
                validateStrictCenterRenderContract(item, level.structural_level, false);
            }
            for (const item of level.center_previews) {
                if (item?.render_kind !== 'center_preview'
                    || item?.source_kind !== expectedSourceKind) {
                    throw new Error('strict center preview source is invalid');
                }
                validateStrictCenterRenderContract(item, level.structural_level, true);
            }
            for (const item of level.center_projections) {
                if (item?.render_kind !== 'center_projection'
                    || item?.source_kind !== expectedSourceKind) {
                    throw new Error('strict center projection source is invalid');
                }
                validateStrictCenterRenderContract(item, level.structural_level, false);
            }
            for (const trend of level.current_trends.concat(level.completed_trend_snapshots)) {
                if (
                    !trend || trend.render_kind !== 'strict_trend'
                    || trend.geometric_direction !== trend.direction
                    || !['up', 'down', null].includes(trend.semantic_direction)
                    || ![
                        'formal', 'awaiting_reversal_support', 'consolidation',
                        'ended', 'geometric_candidate',
                    ].includes(trend.direction_status)
                    || trend.formal_direction_confirmed !== (trend.direction_status === 'formal')
                    || !Array.isArray(trend.direction_reason_codes)
                ) throw new Error('strict trend direction qualification is invalid');
            }
            for (let index = 1; index < level.current_trends.length; index += 1) {
                const previous = level.current_trends[index - 1];
                const current = level.current_trends[index];
                const previousTail = previous.points?.[previous.points.length - 1];
                const currentHead = current.points?.[0];
                if (
                    previous.direction === current.direction
                    || !Number.isInteger(previousTail?.price_tick)
                    || !Number.isInteger(currentHead?.price_tick)
                    || previousTail.price_tick !== currentHead.price_tick
                    || !Number.isInteger(previousTail?.time)
                    || !Number.isInteger(currentHead?.time)
                    || currentHead.time < previousTail.time
                ) {
                    throw new Error('strict current trends must form an alternating causal chain');
                }
            }
            const formalUnitIds = new Set();
            for (const trend of level.current_trends) {
                if (!strictStringArray(trend.constituent_unit_ids)) {
                    throw new Error('strict trend source units are invalid');
                }
                for (const unitId of trend.constituent_unit_ids) formalUnitIds.add(unitId);
            }
            const pendingUnitIds = new Set();
            for (const item of level.pending_movements) {
                validatePendingMovementRenderContract(
                    item,
                    level.structural_level,
                    formalUnitIds,
                    pendingUnitIds,
                );
            }
            for (const [field, renderKind, status] of [
                ['confirmed_points', 'point_confirmed', 'confirmed'],
                ['approaching_points', 'point_approaching', 'approaching'],
            ]) {
                for (const point of level[field]) {
                    const pointType = String(point?.point_type || '').toLowerCase();
                    const expectedSide = pointType.endsWith('buy') ? 'buy' : 'sell';
                    const expectedConfirmed = status === 'confirmed';
                    const validFormation = expectedConfirmed
                        ? point?.formation_state === 'confirmed'
                        : ['forming', 'geometry_ready', 'formed'].includes(point?.formation_state);
                    const validLockState = expectedConfirmed
                        ? point?.lock_state === 'locked' || (
                            point?.lock_state === 'pending'
                            && point?.operational_confirmation === true
                            && point?.strict_status === 'approaching'
                            && point?.contains_forming_segment === false
                            && point?.contains_unlocked_segment === true
                        )
                        : point?.lock_state === 'pending';
                    if (
                        !point
                        || point.render_kind !== renderKind
                        || point.status !== status
                        || !validFormation
                        || !validLockState
                        || typeof point.contains_forming_segment !== 'boolean'
                        || typeof point.contains_unlocked_segment !== 'boolean'
                        || point.structural_level !== level.structural_level
                        || !STRICT_POINT_TYPES.has(pointType)
                        || point.side !== expectedSide
                        || !Array.isArray(point.points)
                        || point.points.length !== 1
                    ) throw new Error('strict point contract is invalid');
                }
            }
            // 后端 CenterLevelResult 保证只有一个未决归属。前端以同一规则独立
            // 防御陈旧缓存和竞态：共享边界或相互分离的形成中预览，不能与未决
            // 正式中枢共存，除非已完成预览提供了该中枢的因果三类点边界。
            const ongoingCenters = level.centers.filter((item) => item?.state === 'ongoing');
            const terminalOngoing = ongoingCenters.length
                ? ongoingCenters[ongoingCenters.length - 1]
                : null;
            const canonicalCenters = level.centers.filter((item) => (
                item?.state !== 'ongoing' || item === terminalOngoing
            ));
            const completedPreviews = level.center_previews.filter(
                (item) => item?.state === 'completed',
            );
            const activeCompletionObserved = ongoingCenters.some((center) => (
                completedPreviews.some(
                    (preview) => this._strictPreviewSupersedesCenter(center, preview),
                )
            ));
            let acceptedPreviews = level.center_previews.filter((preview) => (
                preview?.state !== 'forming'
                || ongoingCenters.length === 0
                || ongoingCenters.some(
                    (center) => this._strictPreviewSupersedesCenter(center, preview),
                )
                || activeCompletionObserved
            ));
            const formingPreviews = acceptedPreviews.filter(
                (item) => item?.state === 'forming',
            );
            if (formingPreviews.length > 1) {
                const latestForming = formingPreviews.reduce((latest, item) => {
                    const itemAt = Number(item?.available_at || item?.points?.[1]?.time || 0);
                    const latestAt = Number(latest?.available_at || latest?.points?.[1]?.time || 0);
                    return itemAt >= latestAt ? item : latest;
                });
                acceptedPreviews = acceptedPreviews.filter((item) => (
                    item?.state !== 'forming' || item === latestForming
                ));
            }
            const hasPreview = acceptedPreviews.length > 0;
            const projectedCenterIds = new Set(
                level.center_projections.map((item) => item?.center_id).filter(Boolean),
            );
            add(canonicalCenters.filter((item) => !(
                item?.state === 'ongoing'
                && (
                    // 共享一个首尾衔接段的相邻中枢可以同时显示；形成中预览
                    // 复用了更多活动中枢主体时，才选择更晚的预览作为唯一显示框。
                    acceptedPreviews.some(
                        (preview) => this._strictPreviewSupersedesCenter(item, preview),
                    )
                    // 没有预览时，开放投影替代同一活动中枢较短的正式框。
                    || (!hasPreview && projectedCenterIds.has(item.center_id))
                )
            )));
            add(acceptedPreviews);
            if (!hasPreview) add(level.center_projections);
            add(level.current_trends);
            add(level.pending_movements);
            // completed_trend_snapshots 是只读审计证据，不创建默认图形。
            add(level.confirmed_points, level.label);
            add(level.approaching_points, level.label);
            add(level.divergences, level.label);
        }
        return groups;
    }

    _createStrictShape(item, currentInterval, bars) {
        const level = item.structural_level || 0;
        const levelColor = getRecursiveLevelColor(currentInterval, level);
        if (item.render_kind === 'formal_center') {
            const style = getCenterVisualStyle('formal', item);
            return ChartUtils.createZhongshuShape(this.chart, item, {
                color: levelColor,
                linewidth: style.linewidth,
                overrides: {
                    linestyle: style.linestyle,
                    transparency: style.transparency,
                },
            });
        }
        if (item.render_kind === 'center_preview') {
            const style = getCenterVisualStyle('preview', item);
            return ChartUtils.createZhongshuShape(this.chart, {
                ...item,
                linestyle: style.linestyle,
            }, {
                color: levelColor,
                linewidth: style.linewidth,
                overrides: {
                    transparency: style.transparency,
                    linestyle: style.linestyle,
                },
            });
        }
        if (item.render_kind === 'center_observation') {
            const style = getCenterVisualStyle('observation', item);
            return ChartUtils.createZhongshuShape(this.chart, { ...item, linestyle: style.linestyle }, {
                color: getRecursiveLevelColor(currentInterval, 0),
                linewidth: style.linewidth,
                overrides: { transparency: style.transparency, linestyle: style.linestyle },
            });
        }
        if (item.render_kind === 'center_projection') {
            const style = getCenterVisualStyle('projection', item);
            return ChartUtils.createZhongshuShape(this.chart, { ...item, linestyle: style.linestyle }, {
                color: levelColor,
                linewidth: style.linewidth,
                overrides: { transparency: style.transparency, linestyle: style.linestyle },
            });
        }
        if (item.render_kind === 'strict_trend') {
            const style = getTrendVisualStyle(item);
            return ChartUtils.createLineShape(this.chart, {
                ...item,
                linestyle: style.linestyle,
            }, {
                color: levelColor,
                linewidth: style.linewidth,
                overrides: { transparency: style.transparency },
            });
        }
        if (item.render_kind === 'pending_movement') {
            return ChartUtils.createLineShape(this.chart, {
                ...item,
                linestyle: CHART_CONFIG.LINE_STYLES.DOTTED,
            }, {
                color: levelColor,
                linewidth: 1,
                overrides: { transparency: 82 },
            });
        }
        if (item.render_kind === 'point_confirmed' || item.render_kind === 'point_approaching') {
            const style = getStrictPointVisual(item);
            return ChartUtils.createShape(this.chart, item.points[0], {
                shape: 'text',
                text: style.text,
                overrides: {
                    color: style.color,
                    fontsize: style.fontsize,
                    bold: style.bold,
                    transparency: style.transparency,
                    'linetooltext.color': style.color,
                    'linetooltext.fontsize': style.fontsize,
                    'linetooltext.bold': style.bold,
                },
            });
        }
        if (item.render_kind === 'strict_divergence') {
            const style = getStrictDivergenceVisual(item);
            return ChartUtils.createShape(this.chart, item.points[0], {
                shape: 'text',
                text: style.text,
                overrides: {
                    color: style.color,
                    fontsize: style.fontsize,
                    bold: style.bold,
                    transparency: 0,
                    'linetooltext.color': style.color,
                    'linetooltext.fontsize': style.fontsize,
                    'linetooltext.bold': style.bold,
                },
            });
        }
        throw new Error(`unsupported strict render kind: ${item.render_kind}`);
    }

    _strictCreatedGeometryMatches(item, realId) {
        if (!this.chart || typeof this.chart.getShapeById !== 'function') return true;
        try {
            const shape = this.chart.getShapeById(realId);
            if (!shape || typeof shape.getPoints !== 'function') return false;
            const actualPoints = shape.getPoints();
            const expectedPoints = item?.points;
            if (!Array.isArray(actualPoints) || !Array.isArray(expectedPoints)) return false;
            if (actualPoints.length !== expectedPoints.length) return false;
            const quantum = Number(this._strictStructureSnapshot?.structure_price_quantum);
            return expectedPoints.every((point, index) => {
                const actual = actualPoints[index];
                const expectedPrice = Number(point?.price);
                const actualPrice = Number(actual?.price);
                if (
                    Number(actual?.time) !== Number(point?.time)
                    || !Number.isFinite(expectedPrice)
                    || !Number.isFinite(actualPrice)
                ) return false;
                const tolerance = Number.isFinite(quantum) && quantum > 0
                    ? Math.max(quantum * 1e-6, Math.abs(expectedPrice) * Number.EPSILON * 16)
                    : Math.max(1e-12, Math.abs(expectedPrice) * Number.EPSILON * 16);
                return Math.abs(actualPrice - expectedPrice) <= tolerance;
            });
        } catch (error) {
            // 图形在创建的同一事件循环内可能暂时不可读；应视为尚未验证并重试。
            // 若此处直接接受，静默吸附到错误坐标的矩形会被永久保留。
            return false;
        }
    }

    _acceptStrictEntity(scope, generation, contextToken, item, realId) {
        const api = this._strictApi();
        const desired = this._strictDesiredByScope.get(scope);
        const isCurrent = (
            realId != null &&
            this._strictReconcileEpoch.current(scope, generation) &&
            this._strictStructureContextToken === contextToken &&
            desired?.get(item.logicalKey) === item.renderKey
        );
        const container = this._strictContainers.get(scope);
        if (!isCurrent || !container || container.some((entry) => entry.logicalKey === item.logicalKey)) {
            if (realId != null) this.safeRemove(realId);
            return false;
        }
        if (!this._strictCreatedGeometryMatches(item, realId)) {
            // TV 在目标历史 K 线尚未装入画布时会把多点图形静默吸附到当前
            // 最左/最右 K 线。不能把这个错误坐标记进容器，否则源快照不变时
            // 后续历史加载也不会触发替换，只能靠人工“重新加载数据”恢复。
            console.warn('[STRICT-CHART] rejected snapped strict entity', {
                logicalKey: item.logicalKey,
                expected: item.points,
            });
            this.safeRemove(realId);
            this._scheduleReconcileRetry('strict-create-snapped');
            return false;
        }
        container.push({
            id: realId,
            logicalKey: item.logicalKey,
            renderKey: item.renderKey,
            geometryFingerprint: item.geometryFingerprint,
            time: item.points[0]?.time,
            tailTime: item.points[item.points.length - 1]?.time,
        });
        this._markAutomaticShapeId(realId);
        this._pendingRemovalIds?.delete(realId);
        // 创建时可能先返回请求坐标，待时间轴加载完成后又被 TradingView 重新锚定。
        // 因此要在稳定画布上再次验证；_isVerifyingNow 用于防止验证修复再次安排循环。
        if (!this._isVerifyingNow()) this._scheduleVerifyRebuild();
        return true;
    }

    _reconcileStrictScope(scope, incoming, loadedRange, visibleRange, createFunc, contextToken) {
        this._ensureStrictReconcileState();
        const api = this._strictApi();
        if (!this._strictContainers.has(scope)) this._strictContainers.set(scope, []);
        this._strictScopes.add(scope);
        const container = this._strictContainers.get(scope);
        const plan = api.planReconcile(container, incoming, loadedRange, visibleRange);
        const generation = this._strictReconcileEpoch.next(scope);
        const desiredItems = new Map(
            (plan.desiredItems || []).map((item) => [item.logicalKey, item]),
        );
        const removeIds = new Set(plan.removeIds.filter((id) => id != null));
        const createItemsByKey = new Map(
            plan.createItems.map((item) => [item.logicalKey, item]),
        );

        // 指纹只描述创建实体时请求的坐标，不能证明当前 TradingView 线条工具仍保有
        // 这些坐标：历史分页和时间轴重新布局都可能把既有矩形静默吸附到其他 K 线。
        // 所以要重新验证全部保留实体，并把漂移或缺失实体转成普通的删除后重建差量。
        for (const entry of container) {
            const desiredItem = desiredItems.get(entry.logicalKey);
            if (
                desiredItem
                && entry.renderKey === desiredItem.renderKey
                && entry.geometryFingerprint === desiredItem.geometryFingerprint
                && !this._strictCreatedGeometryMatches(desiredItem, entry.id)
            ) {
                if (entry.id != null) removeIds.add(entry.id);
                createItemsByKey.set(desiredItem.logicalKey, desiredItem);
                console.warn('[STRICT-CHART] repairing drifted strict entity', {
                    logicalKey: entry.logicalKey,
                    expected: desiredItem.points,
                });
            }
        }
        if (removeIds.size) {
            for (const id of removeIds) this.safeRemove(id);
            const retained = container.filter((entry) => !removeIds.has(entry.id));
            container.length = 0;
            retained.forEach((entry) => container.push(entry));
        }

        const desired = new Map();
        for (const entry of container) desired.set(entry.logicalKey, entry.renderKey);
        for (const item of createItemsByKey.values()) desired.set(item.logicalKey, item.renderKey);
        this._strictDesiredByScope.set(scope, desired);
        this._strictDesiredItemsByScope.set(scope, desiredItems);

        for (const item of createItemsByKey.values()) {
            let result;
            try { result = createFunc(item); }
            catch (error) {
                console.warn('[STRICT-CHART] create strict entity failed', error);
                this._scheduleReconcileRetry('strict-create-throw');
                continue;
            }
            if (result != null && typeof result.then === 'function') {
                const pendingKey = `${scope}|${item.logicalKey}|${generation}`;
                const promise = Promise.resolve(result);
                this._strictPendingCreates.set(pendingKey, { scope, promise });
                promise.then((realId) => {
                    if (realId == null) {
                        this._scheduleReconcileRetry('strict-create-null');
                        return;
                    }
                    this._acceptStrictEntity(scope, generation, contextToken, item, realId);
                }).catch((error) => {
                    console.warn('[STRICT-CHART] async strict entity failed', error);
                    this._scheduleReconcileRetry('strict-create-reject');
                }).finally(() => {
                    if (this._strictPendingCreates.get(pendingKey)?.promise === promise) {
                        this._strictPendingCreates.delete(pendingKey);
                    }
                    this._settleReconcileRetryIfComplete();
                });
            } else if (result != null) {
                this._acceptStrictEntity(scope, generation, contextToken, item, result);
            } else {
                this._scheduleReconcileRetry('strict-create-null');
            }
        }
    }

    _strictReconcileComplete() {
        if ((this._strictPendingCreates?.size || 0) > 0) return false;
        if (!(this._strictDesiredByScope instanceof Map)) return true;
        for (const [scope, desired] of this._strictDesiredByScope.entries()) {
            const container = this._strictContainers?.get(scope) || [];
            if (container.length !== desired.size) return false;
            const actual = new Map(
                container.map(
                    (entry) => [entry.logicalKey, entry.renderKey],
                ),
            );
            if (actual.size !== desired.size) return false;
            for (const [logicalKey, renderKey] of desired.entries()) {
                if (actual.get(logicalKey) !== renderKey) return false;
            }
            const desiredItems = this._strictDesiredItemsByScope?.get(scope);
            if (desiredItems instanceof Map) {
                for (const entry of container) {
                    const item = desiredItems.get(entry.logicalKey);
                    if (!item || !this._strictCreatedGeometryMatches(item, entry.id)) return false;
                }
            }
        }
        return true;
    }

    _settleReconcileRetryIfComplete() {
        if (this._strictReconcileComplete()) {
            this._clearReconcileRetryBudget();
        }
    }

    _clearStrictScope(scope, reason = 'clear') {
        if (!(this._strictContainers instanceof Map)) return;
        if (this._strictReconcileEpoch) {
            try { this._strictReconcileEpoch.next(scope); } catch (e) { /* disposed */ }
        }
        const container = this._strictContainers.get(scope) || [];
        for (const entry of container) this.safeRemove(entry.id);
        this._strictContainers.delete(scope);
        this._strictScopes?.delete(scope);
        this._strictDesiredByScope?.delete(scope);
        this._strictDesiredItemsByScope?.delete(scope);
        clog(`[STRICT-CHART] cleared scope reason=${reason} scope=${scope}`);
    }

    _clearAllStrictScopes(reason = 'clear-all') {
        const scopes = new Set();
        if (this._strictScopes instanceof Set) this._strictScopes.forEach((scope) => scopes.add(scope));
        if (this._strictContainers instanceof Map) this._strictContainers.forEach((_, scope) => scopes.add(scope));
        if (this._strictPendingCreates instanceof Map) {
            this._strictPendingCreates.forEach((pending) => { if (pending?.scope) scopes.add(pending.scope); });
        }
        scopes.forEach((scope) => this._clearStrictScope(scope, reason));
    }

    _canRetainStrictSnapshot(chartData, currentInterval) {
        const snapshot = this._strictStructureSnapshot;
        if (!snapshot || !this._strictStructureContextToken) return false;
        const displayFrequency = this._strictFrequencyFromResolution(currentInterval);
        const chartSymbol = this._strictNormalizeSymbol(
            chartData?.chartSymbol || this.widget?.symbolInterval?.()?.symbol,
        );
        if (
            !chartSymbol
            || this._strictNormalizeSymbol(snapshot.symbol) !== chartSymbol
            || snapshot.display_frequency !== displayFrequency
            || snapshot.source_frequency !== displayFrequency
        ) return false;
        try {
            // 只允许沿用比当前行情旧的同上下文快照；若缓存反而来自未来，说明
            // 正在切换数据上下文，必须清空，不能把旧标的图形带过来。
            const currentSourceClosedAt = this._strictSourceClosedAt(chartData?.barsResult);
            const lagSeconds = currentSourceClosedAt - snapshot.source_closed_at;
            let maxLagSeconds;
            if (/^[1-9][0-9]*m$/.test(displayFrequency)) {
                maxLagSeconds = Number.parseInt(displayFrequency, 10) * 60 * 8;
            } else {
                maxLagSeconds = {
                    '10s': 80,
                    '30s': 240,
                    d: 4 * 86400,
                    '2d': 8 * 86400,
                    w: 14 * 86400,
                    m: 62 * 86400,
                    q: 190 * 86400,
                    y: 740 * 86400,
                }[displayFrequency] || 0;
            }
            return lagSeconds >= 0 && lagSeconds <= maxLagSeconds;
        } catch (error) {
            return false;
        }
    }

    _strictUnavailable(code, chartData = null, currentInterval = null) {
        const errorCode = code || 'strict_evidence_invalid';
        if (this._canRetainStrictSnapshot(chartData, currentInterval)) {
            this._setStrictStructureStatus('stale', errorCode);
            clog(`[STRICT-CHART] retained last good snapshot code=${errorCode}`);
            return true;
        }
        this._clearAllStrictScopes(code || 'unavailable');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        this._setStrictStructureStatus('unavailable', errorCode);
        return false;
    }

    _drawStrictStructure(chartData, currentInterval) {
        const barsResult = chartData?.barsResult;
        const mode = barsResult?.strict_structure_mode;
        if (mode === 'unavailable') {
            this._strictUnavailable(
                barsResult.strict_structure_error?.code,
                chartData,
                currentInterval,
            );
            return;
        }
        let snapshot;
        if (mode === 'replace') snapshot = barsResult.strict_structure;
        else if (mode === 'unchanged') snapshot = this._strictStructureSnapshot;
        else {
            this._strictUnavailable('strict_transport_missing', chartData, currentInterval);
            return;
        }
        if (!snapshot) {
            this._strictUnavailable('strict_snapshot_missing', chartData, currentInterval);
            return;
        }

        try {
            const validated = this._validateStrictStructureSnapshot(snapshot, chartData, currentInterval);
            const groups = this._strictRenderGroups(snapshot, validated.context);
            if (this._strictStructureContextToken !== validated.contextToken) {
                this._clearReconcileRetryBudget();
            }
            this._strictStructureSnapshot = snapshot;
            this._strictStructureContextToken = validated.contextToken;
            this._recMaxLevel = Math.max(
                0,
                ...snapshot.levels.map((level) => level.structural_level),
            );

            const nextScopes = new Set(groups.keys());
            const priorScopes = new Set(this._strictScopes instanceof Set ? this._strictScopes : []);
            for (const [scope, items] of groups.entries()) {
                this._reconcileStrictScope(
                    scope,
                    items,
                    validated.loadedRange,
                    validated.visibleRange,
                    (item) => this._createStrictShape(item, currentInterval, barsResult.bars),
                    validated.contextToken,
                );
            }
            for (const scope of priorScopes) {
                if (!nextScopes.has(scope)) this._clearStrictScope(scope, 'snapshot-replace');
            }
            this._settleReconcileRetryIfComplete();
            this._setStrictStructureStatus('ready');
        } catch (error) {
            console.warn('[STRICT-CHART] rejected strict snapshot', error);
            this._strictUnavailable('strict_context_mismatch', chartData, currentInterval);
        }
    }

    getChartData() {
        const symbolInterval = this.widget.symbolInterval(); if (!symbolInterval) return null;
        const symbolResKey = `${symbolInterval.symbol.toString().toLowerCase()}${symbolInterval.interval.toString().toLowerCase()}`;
        let barsResult = this.udf_datafeed?._historyProvider?.bars_result?.get(symbolResKey);

        if (!barsResult) {
            // 容错: 写键(getBars 用 symbolInfo.ticker)与读键(symbolInterval().symbol)可能差一个
            // "market:" 前缀。剥前缀直查、或在现有键里找"去前缀后唯一相等"项再试一次。
            const _map = this.udf_datafeed?._historyProvider?.bars_result;
            if (_map) {
                const _interval = symbolInterval.interval.toString().toLowerCase();
                const _bare = symbolResKey.replace(/^[a-z]+:/, '');
                let _alt = _map.get(_bare);
                if (!_alt) {
                    const _cands = Array.from(_map.keys()).filter(k =>
                        k.toLowerCase().endsWith(_interval) &&
                        k.toLowerCase().replace(/^[a-z]+:/, '') === _bare);
                    if (_cands.length === 1) _alt = _map.get(_cands[0]);
                }
                if (_alt) barsResult = _alt;
            }
        }
        if (!barsResult) {
            // 切标的瞬间新标的 getBars 尚未回填 bars_result 属正常过渡态(draw_chanlun 会 retry,
            // 数据到位后 handleBarsReadyEvent 兜底重绘), 用 clog(debug) 而非 warn, 避免 console 刷屏。
            const availableKeys = this.udf_datafeed?._historyProvider?.bars_result ? Array.from(this.udf_datafeed._historyProvider.bars_result.keys()) : [];
            clog(`[DEBUG-CHARTS] getChartData for ${symbolResKey}: NOT FOUND. Available keys:`, availableKeys);
            return null;
        }

        if (!this.chart) {
            clog("[DEBUG-CHARTS] getChartData aborted: this.chart is null.");
            return null;
        }
        const visibleRange = this.chart.getVisibleRange();
        if (!visibleRange || !visibleRange.from || !visibleRange.to) {
            // 切标的后 chart 仍在 loading 时 VisibleRange 短暂无效, 同属正常过渡态, 降级为 clog。
            clog("[DEBUG-CHARTS] getChartData aborted: VisibleRange invalid (chart loading).");
            return null;
        }

        const from = visibleRange.from;
        const symbolKey = `${symbolInterval.symbol}_${symbolInterval.interval}`;
        return {
            symbolKey,
            chartSymbol: symbolInterval.symbol,
            chartInterval: symbolInterval.interval,
            barsResult,
            from,
            visibleRange: { from: visibleRange.from, to: visibleRange.to },
        };
    }



    makeKey(item) {
        // linestyle 不写入 key：末段 pending→done 翻转（linestyle 1→0）不应被视为不同 shape，
        // 否则每次翻转都触发全量 rebuild 导致闪烁。起/终点足以唯一标识多点形态。
        if (Array.isArray(item.points)) {
            return item.points.map(p => `${p.time}_${p.price}`).join('_');
        } else if (item.points?.time !== undefined) {
            return `${item.points.time}_${item.points.price}_${item.text || ''}`;
        }
        return `${item.id || Math.random()}`;
    }

    _centerChartTimeCoordinate(sourceTime, currentInterval) {
        const epochSeconds = Number(sourceTime);
        if (!Number.isInteger(epochSeconds)) return null;
        const frequency = this._strictFrequencyFromResolution(currentInterval);
        if (!['d', '2d', 'w', 'm', 'q', 'y'].includes(frequency)) {
            return epochSeconds;
        }
        const source = new Date(epochSeconds * 1000);
        let year = source.getUTCFullYear();
        let month = source.getUTCMonth();
        let day = source.getUTCDate();
        if (frequency === 'w') {
            day -= (source.getUTCDay() + 6) % 7;
        } else if (frequency === 'm') {
            day = 1;
        } else if (frequency === 'q') {
            month = Math.floor(month / 3) * 3;
            day = 1;
        } else if (frequency === 'y') {
            month = 0;
            day = 1;
        }
        const coordinate = Date.UTC(year, month, day) / 1000;
        return Number.isInteger(coordinate) ? coordinate : null;
    }
    _fractalRenderList(sourceList, bars, visibleRange, currentInterval) {
        if (!Array.isArray(sourceList) || sourceList.length === 0) return [];
        if (!Array.isArray(bars) || bars.length === 0) return [];

        // bars_result 中的 Bar.time 已经是 TradingView 实际使用的坐标。日/周/月线
        // 会从市场收盘时刻规整到 UTC 周期锚点，因此不能直接把分型的原始收盘时刻
        // 交给 createMultipointShape 再依赖 TV 猜测最近 K 线。
        const barTimes = new Set(
            bars
                .map((bar) => Number(bar?.time) / 1000)
                .filter((time) => Number.isInteger(time)),
        );
        if (barTimes.size === 0) return [];

        const rawFrom = Number(visibleRange?.from);
        const rawTo = Number(visibleRange?.to);
        const visibleFrom = Number.isFinite(rawFrom) ? Math.floor(rawFrom) : Number.NEGATIVE_INFINITY;
        const visibleTo = Number.isFinite(rawTo) ? Math.ceil(rawTo) : Number.POSITIVE_INFINITY;
        const result = [];
        sourceList.forEach((item) => {
            if (!item || !Array.isArray(item.points) || item.points.length === 0) return;
            const anchorTime = this._centerChartTimeCoordinate(
                item.points[0]?.time,
                currentInterval,
            );
            if (
                !Number.isInteger(anchorTime)
                || !barTimes.has(anchorTime)
                || anchorTime < visibleFrom
                || anchorTime > visibleTo
            ) return;

            const points = item.points.map((point) => ({
                ...point,
                time: anchorTime,
            }));
            if (points.some((point) => !Number.isFinite(Number(point.price)))) return;
            result.push({ ...item, points });
        });
        return result;
    }

    _fractalCreatedAnchorMatches(item, realId) {
        if (!this.chart || typeof this.chart.getShapeById !== 'function') return true;
        try {
            const shape = this.chart.getShapeById(realId);
            if (!shape || typeof shape.getPoints !== 'function') return false;
            const actual = shape.getPoints()?.[0];
            const expected = item?.points?.[0];
            const expectedPrice = Number(expected?.price);
            const actualPrice = Number(actual?.price);
            if (
                Number(actual?.time) !== Number(expected?.time)
                || !Number.isFinite(expectedPrice)
                || !Number.isFinite(actualPrice)
            ) return false;
            const tolerance = Math.max(1e-10, Math.abs(expectedPrice) * 1e-10);
            return Math.abs(actualPrice - expectedPrice) <= tolerance;
        } catch (e) {
            return false;
        }
    }
    getUniqueRenderList(sourceList) {
        if (!sourceList || !Array.isArray(sourceList)) return [];
        const finished = [];
        const unfinished = [];
        sourceList.forEach(item => {
            if (item.linestyle == '1' || item.linestyle == 1) {
                unfinished.push(item);
            } else {
                finished.push(item);
            }
        });
        if (unfinished.length > 0) {
            finished.push(unfinished[unfinished.length - 1]);
        }
        return finished;
    }

    _sameReconcileSnapshot(snapshot, from, keys, unfinishedKeys) {
        if (!snapshot || snapshot.from !== from) return false;
        if (!(snapshot.keys instanceof Set) || snapshot.keys.size !== keys.size) return false;
        if (!(snapshot.unfinishedKeys instanceof Set) || snapshot.unfinishedKeys.size !== unfinishedKeys.size) return false;
        for (const key of keys) {
            if (!snapshot.keys.has(key)) return false;
        }
        for (const key of unfinishedKeys) {
            if (!snapshot.unfinishedKeys.has(key)) return false;
        }
        return true;
    }

    reconcile(
        type,
        sourceList,
        from,
        symbolKey,
        createFunc,
        useUnique = true,
        includeOverlaps = false,
        verifyGeometry = false,
    ) {
        if (!Array.isArray(this.obj_charts[symbolKey][type])) {
            this.obj_charts[symbolKey][type] = [];
        }
        const container = this.obj_charts[symbolKey][type];
        const beforeCount = container.length;
        let renderList = sourceList || [];
        const sourceCount = renderList.length;
        if (useUnique) {
            renderList = this.getUniqueRenderList(renderList);
        }
        const afterUniqueCount = renderList.length;

        // 按可视窗口过滤：历史元素可达数百根，全画会造成视觉杂乱。
        // 默认要求 headTime >= from；横跨窗口的多点元素可传
        // includeOverlaps=true，只要 tailTime >= from 就入渲染。
        const newKeys = new Set();
        const itemsToProcess = [];
        renderList.forEach(item => {
            let headTime, tailTime;
            if (Array.isArray(item.points)) {
                headTime = item.points[0]?.time;
                tailTime = item.points[item.points.length - 1]?.time;
            } else {
                headTime = tailTime = item.points?.time;
            }
            // 默认只在头部进入可视窗(必已加载)时创建，避免 createMultipointShape 把画外/未加载
            // 角点 snap 到边缘造成错位。
            // 单点分型的 head 与 tail 相同；tailTime 仍用于下方 keep 判定。
            const shouldRender = includeOverlaps ? tailTime >= from : headTime >= from;
            if (shouldRender) {
                const key = this.makeKey(item);
                newKeys.add(key);
                itemsToProcess.push({ item, key, time: headTime, tailTime });
            }
        });

        // 精确状态守卫：完整几何 key、from 和未完成状态都相同才跳过。
        // 不再截断字符串，避免最新中枢位于截断范围后时漏掉边界修正。
        const unfinishedKeys = new Set(
            itemsToProcess
                .filter(p => p.item.linestyle == '1' || p.item.linestyle == 1)
                .map(p => p.key)
        );
        const guardKey = `${symbolKey}__${type}`;
        const keyToRenderItem = new Map(
            itemsToProcess.map(({ key, item }) => [key, item]),
        );
        const geometryVerifier = typeof verifyGeometry === 'function'
            ? verifyGeometry
            : null;
        // TradingView 可能在后续加载历史或改变可视区时移动已经创建好的 line tool。
        // 数据 key 没变并不代表画布几何仍正确；启用校验的中枢/分型需要在 W1
        // 守卫之前读取实体端点，漂移或实体丢失时强制走 remove + recreate 自愈路径。
        const driftedKeys = new Set();
        if (geometryVerifier) {
            container.forEach((existing) => {
                const renderItem = keyToRenderItem.get(existing.key);
                if (
                    renderItem
                    && existing.id != null
                    && !geometryVerifier(renderItem, existing.id)
                ) {
                    driftedKeys.add(existing.key);
                }
            });
        }
        if (
            driftedKeys.size === 0
            && this._sameReconcileSnapshot(this._reconcileGuard[guardKey], from, newKeys, unfinishedKeys)
        ) {
            if (window.__chanlunDebug) {
                console.log(
                    `[CHANLUN-DIAG][reconcile.${type}] W1 guard skip ` +
                    `(unchanged: ${newKeys.size} keys, from=${from})`
                );
            }
            return;
        }
        if (!(this._reconcileEpochs instanceof Map)) this._reconcileEpochs = new Map();
        const generation = (this._reconcileEpochs.get(guardKey) || 0) + 1;
        this._reconcileEpochs.set(guardKey, generation);

        // makeKey 不含 linestyle，pending→done 翻转（虚→实）不触发重建，避免端点漂移
        const beforeContainerLen = container.length;
        // key→新 item 映射,供 toKeep 检测 pending↔done 翻转(makeKey 不含 linestyle,翻转命中同 key)
        const keyToNewItem = new Map();
        itemsToProcess.forEach(p => keyToNewItem.set(p.key, p.item));
        const toKeep = [];
        let removedCount = 0;
        for (const existing of container) {
            const existingTail = existing.tailTime ?? existing.time;
            if (
                newKeys.has(existing.key)
                && existingTail >= from
                && !driftedKeys.has(existing.key)
            ) {
                // 端点不变但 linestyle 翻转(笔/线段 pending↔done):同 key 命中 toKeep,但旧 TV shape
                // 仍是旧样式(用户报:笔已完成页面仍显未完成虚线)。就地 setProperties 刷新 linestyle
                // (不重建=无闪烁/无端点漂移),同步 entry.isUnfinished。单点形态(无 linestyle)
                // isUnfinished 恒 false 不触发,天然只作用于笔/线段/走势类型线。
                const newItem = keyToNewItem.get(existing.key);
                if (newItem) {
                    const newUnfinished = (newItem.linestyle == '1' || newItem.linestyle == 1);
                    if (newUnfinished !== existing.isUnfinished && existing.id != null) {
                        try {
                            const sh = this.chart.getShapeById(existing.id);
                            if (sh && sh.setProperties) {
                                sh.setProperties({ linestyle: parseInt(newItem.linestyle) || 0 });
                            }
                        } catch (e) {}
                        existing.isUnfinished = newUnfinished;
                    }
                }
                toKeep.push(existing);
            } else {
                this.safeRemove(existing.id);
                removedCount += 1;
            }
        }
        container.length = 0;
        toKeep.forEach(item => container.push(item));

        const existingKeys = new Set(container.map(item => item.key));
        let createSync = 0, createAsync = 0, createSkip = 0;
        let createFailed = false;   // 本轮有 create→null(chart 未 ready/点超范围)→ 不落守卫、放行重试
        itemsToProcess.forEach(({ item, key, time, tailTime }) => {
            if (existingKeys.has(key)) { createSkip += 1; return; }
            const result = createFunc(item);
            const entry = {
                time: time,
                tailTime: tailTime,
                key: key,
                isUnfinished: (item.linestyle == '1' || item.linestyle == 1),
            };
            if (result != null && typeof result.then === 'function') {
                createAsync += 1;
                result.then(realId => {
                    if (realId == null) {
                        console.warn(`[CHANLUN-DIAG][reconcile.${type}] async create→null key=${(key||'').slice(0,40)}`);
                        delete this._reconcileGuard[guardKey];   // 撤守卫,否则重试同签名被 skip
                        this._scheduleReconcileRetry(`${type}:async-null`);
                        return;
                    }
                    if (this._disposed || this._reconcileEpochs.get(guardKey) !== generation) {
                        this.safeRemove(realId);
                        return;
                    }
                    if (geometryVerifier && !geometryVerifier(item, realId)) {
                        this.safeRemove(realId);
                        delete this._reconcileGuard[guardKey];
                        this._scheduleReconcileRetry(`${type}:async-snapped`);
                        return;
                    }
                    entry.id = realId;
                    container.push(entry);
                    this._markAutomaticShapeId(realId);
                    if (typeof verifyGeometry === 'function' && !this._isVerifyingNow()) {
                        this._scheduleVerifyRebuild();
                    }
                }).catch((e) => {
                    console.warn(`[CHANLUN-DIAG][reconcile.${type}] async create→reject key=${(key||'').slice(0,40)}`, e);
                    delete this._reconcileGuard[guardKey];
                    this._scheduleReconcileRetry(`${type}:async-reject`);
                });
            } else if (result != null && (
                !geometryVerifier || geometryVerifier(item, result)
            )) {
                entry.id = result;
                container.push(entry);
                this._markAutomaticShapeId(result);
                if (typeof verifyGeometry === 'function' && !this._isVerifyingNow()) {
                    this._scheduleVerifyRebuild();
                }
                createSync += 1;
            } else if (result != null) {
                this.safeRemove(result);
                createFailed = true;
                this._scheduleReconcileRetry(`${type}:sync-snapped`);
            } else {
                console.warn(`[CHANLUN-DIAG][reconcile.${type}] sync create→null key=${(key||'').slice(0,40)}`);
                createFailed = true;
                this._scheduleReconcileRetry(`${type}:sync-null`);
            }
        });

        // 同步路径完成后记录完整状态快照；异步 create 仍在 pending 无妨，resolve 后 push 到
        // container 与快照目标状态一致，下次 guard 时容器已正确。
        // 但若本轮有 sync create→null(chart 未 ready/点超已加载范围),**不落守卫**——否则
        // 状态未变时 _scheduleReconcileRetry 触发的重画会被上面 W1 guard 误 skip,失败的
        // shape 永不重建(表现为「中枢框初次不显、点中枢重勾后才出」)。async→null 在其回调删守卫同理。
        if (!createFailed) {
            this._reconcileGuard[guardKey] = {
                from,
                keys: new Set(newKeys),
                unfinishedKeys: new Set(unfinishedKeys),
            };
        }

        // 仅在 window.__chanlunDebug=true 时输出调试摘要，避免生产 console 刷屏
        // 错误诊断走 console.warn，不受此开关控制
        if (window.__chanlunDebug && (removedCount > 0 || createSync > 0 || createAsync > 0)) {
            console.log(
                `[CHANLUN-DIAG][reconcile.${type}] src=${sourceCount} unique=${afterUniqueCount} ` +
                `inWin=${itemsToProcess.length} containerWas=${beforeContainerLen}→${container.length} ` +
                `removed=${removedCount} createSync=${createSync} createAsync=${createAsync} skip=${createSkip} ` +
                `from=${from} owned=${this._reconcileOwnedIds?.size ?? '?'}`
            );
        }
    }

    // 失败重试调度：createFunc 返回 null（chart 未 ready）时定时重触 draw_chanlun。
    // 首屏数据就绪但布局未完成时极易丢失 shape，导致线段不连续；
    // 通过指数退避并限制次数，避免重试风暴。
    _scheduleReconcileRetry(reason) {
        if (this._disposed) return;
        if (!this._reconcileRetry) {
            this._reconcileRetry = { count: 0, timer: null };
        }
        const state = this._reconcileRetry;
        if (state.count >= 7) return;          // 最多 7 次：100→200→400→800→1600→3200→6400ms ≈ 12.7s
        if (state.timer) return;               // 已排队，避免叠加
        const delayMs = Math.min(100 * Math.pow(2, state.count), 6400);
        state.count += 1;
        state.timer = setTimeout(() => {
            state.timer = null;
            if (this._disposed) return;
            clog(`[CHANLUN-TIMING] reconcile retry #${state.count} (${reason}) after ${delayMs}ms`);
            // 绕过防抖直接调用：持续缩放时 visibleRangeChange 会不停 reset 300ms 防抖，
            // retry 经 debounced 路径会被无限期延后
            this.draw_chanlun();
        }, delayMs);
    }

    _clearReconcileRetryBudget() {
        if (this._reconcileRetry?.timer) {
            clearTimeout(this._reconcileRetry.timer);
        }
        this._reconcileRetry = { count: 0, timer: null };
    }

    _resetReconcileRetry() {
        this._clearReconcileRetryBudget();
        // 同步清掉守卫缓存，否则下次源数据相同时会被 W1 guard 误 skip
        this._reconcileGuard = {};
        if (this._reconcileEpochs instanceof Map) this._reconcileEpochs.clear();
        if (this._verifyRebuildTimer) {
            clearTimeout(this._verifyRebuildTimer);
            this._verifyRebuildTimer = null;
        }
        this._verifyingUntil = null;
    }

    // 补绘调度：full rebuild 后 500ms 触发一次 verify-rebuild，用于补齐布局抖动遗漏的 shape。
    // 守门用 timestamp（_verifyingUntil）而非微任务标志：draw_chanlun 是 async，
    // microtask 重置会在首个 await 之前跑掉，导致 verify 自身又排队 → 500ms 自循环。
    _scheduleVerifyRebuild() {
        if (this._disposed) return;
        if (this._verifyRebuildTimer) {
            clearTimeout(this._verifyRebuildTimer);
        }
        this._verifyRebuildTimer = setTimeout(() => {
            this._verifyRebuildTimer = null;
            if (this._disposed) return;
            this._verifyingUntil = performance.now() + 1500;
            // 清掉守卫缓存，确保 reconcile 走全量 rebuild 路径，基于稳定布局重新落位 shape
            this._reconcileGuard = {};
            if (this._reconcileEpochs instanceof Map) this._reconcileEpochs.clear();
            clog(`[CHANLUN-TIMING] verify-rebuild firing`);
            this.draw_chanlun();
        }, 500);
    }

    _isVerifyingNow() {
        return this._verifyingUntil != null && performance.now() < this._verifyingUntil;
    }

    initChartContainer(symbolKey) {
        if (!this.obj_charts[symbolKey]) {
            this.obj_charts[symbolKey] = {};
            CHART_CONFIG.CHART_TYPES.forEach((type) => { this.obj_charts[symbolKey][type] = []; });
        }
        return this.obj_charts[symbolKey];
    }

    getMACDStudyId() {
        if (this.macdStudyId) return this.macdStudyId;
        const studies = this.chart.getAllStudies();
        const macdStudy = studies.find(s => s.name === 'MACD_HTF');
        if (macdStudy) { this.macdStudyId = macdStudy.id; return macdStudy.id; }
        return null;
    }

    drawChartElements(chartData, currentInterval) {
        const { symbolKey, barsResult, from } = chartData;
        if (!barsResult) return;
        this.initChartContainer(symbolKey);

        clog("[DataVerify][Charts] drawChartElements interval=" + currentInterval, {
            fxs: barsResult.fxs?.length || 0,
            bis: barsResult.bis?.length || 0,
            xds: barsResult.xds?.length || 0,
            strict_mode: barsResult.strict_structure_mode || 'missing',
            strict_revision: barsResult.strict_structure?.render_revision || null,
        });

        const safeCreate = (promise, type) => {
            if (promise && typeof promise.then === 'function') {
                this._automaticShapeCreateCount = (this._automaticShapeCreateCount || 0) + 1;
                return Promise.resolve(promise)
                    .catch(e => {
                        console.error(`[DEBUG-CHARTS] Error creating shape (${type}):`, e);
                        return null;
                    })
                    .finally(() => {
                        this._automaticShapeCreateCount = Math.max(
                            0,
                            (this._automaticShapeCreateCount || 1) - 1,
                        );
                    });
            }
            return promise;
        };

        // 使用本图表实例独立的显示配置，多图布局下互不影响
        const cfg = this.cl_show_config;
        const visibleRange = chartData.visibleRange || {
            from,
            to: Number.POSITIVE_INFINITY,
        };
        const fractalRenderItems = this._fractalRenderList(
            barsResult.fxs || [],
            barsResult.bars || [],
            visibleRange,
            currentInterval,
        );
        this.reconcile(
            'fxs',
            cfg.fx ? fractalRenderItems : [],
            from,
            symbolKey,
            (item) => safeCreate(ChartUtils.createFxShape(this.chart, item), 'fx'),
            false,
            false,
            (item, realId) => this._fractalCreatedAnchorMatches(item, realId),
        );
        // 基础结构按绝对递归级别取色，同时让线段比笔再粗一级；菜单色块走同一颜色函数。
        const biLineStyle = getBaseStructureStyle(currentInterval, 'bis');
        const xdLineStyle = getBaseStructureStyle(currentInterval, 'xds');
        this.reconcile('bis', cfg.bi ? barsResult.bis : [], from, symbolKey, (item) => safeCreate(ChartUtils.createLineShape(this.chart, item, biLineStyle), 'bi'));
        this.reconcile('xds', cfg.xd ? barsResult.xds : [], from, symbolKey, (item) => safeCreate(ChartUtils.createLineShape(this.chart, item, xdLineStyle), 'xd'));
        // 中枢、走势、买卖点与背驰由同一个严格原子快照统一绘制。
        this._drawStrictStructure(chartData, currentInterval);
        this.updateDrawPalette();
        if (this._sweepOrphanTimer) clearTimeout(this._sweepOrphanTimer);
        this._sweepOrphanTimer = setTimeout(() => {
            this._sweepOrphanTimer = null;
            if (this._disposed) return;
            this.sweepOrphanShapes();
        }, 100);
        return;
    }

    /**
     * 扫描并清理孤儿 shape:存在于 _reconcileOwnedIds 但所有 container 都已不引用的 entity。
     *
     * 触发场景:
     *   1. safeRemove 在 TV 内部静默失败(removeEntity 抛错被 try/catch 吞)
     *   2. reconcile 之间的 race:旧 container 已清零,async safeRemove 还未真的删
     *   3. 同一 key 因端点漂移走两次 create 路径,产生重复 shape
     *
     * 不会误删用户手画的 line tool:它们从未通过 reconcile 创建,不在 _reconcileOwnedIds 中。
     */
    sweepOrphanShapes() {
        if (!this.chart) {
            clog('[CHANLUN-DIAG][sweep] skipped: this.chart is null');
            return;
        }
        if (!this._reconcileOwnedIds) {
            clog('[CHANLUN-DIAG][sweep] skipped: _reconcileOwnedIds is null');
            return;
        }
        const inUseIds = new Set();
        Object.values(this.obj_charts || {}).forEach(symbolData => {
            Object.values(symbolData || {}).forEach(items => {
                (items || []).forEach(item => {
                    if (item && item.id != null && typeof item.id !== 'object') {
                        inUseIds.add(item.id);
                    }
                });
            });
        });
        if (this._strictContainers instanceof Map) {
            this._strictContainers.forEach((items) => {
                (items || []).forEach((item) => {
                    if (item && item.id != null && typeof item.id !== 'object') inUseIds.add(item.id);
                });
            });
        }
        let tvShapes = [];
        let canInspectShapes = false;
        try {
            tvShapes = this.chart.getAllShapes() || [];
            canInspectShapes = Array.isArray(tvShapes);
        } catch (e) { console.warn('[CHANLUN-DIAG][sweep] getAllShapes 抛错', e); }
        const tvIds = new Set(tvShapes.map(s => s.id));

        // ownedOrphans：owned 有但 container 已无引用 → 应删除的孤儿
        const ownedOrphans = [];
        this._reconcileOwnedIds.forEach(id => {
            if (!inUseIds.has(id)) ownedOrphans.push(id);
        });

        // trulyForeign：TV 里有但 owned/inUse 都无记录 → 理论上是用户手画，race 时可能漏 add
        const trulyForeign = canInspectShapes
            ? tvShapes.filter(s => !inUseIds.has(s.id) && !this._reconcileOwnedIds.has(s.id))
            : [];

        // sweep 总结仅在 window.__chanlunDebug 时输出，避免生产 console 刷屏
        if (window.__chanlunDebug) {
            console.log(
                `[CHANLUN-DIAG][sweep] tvShapes=${tvShapes.length} owned=${this._reconcileOwnedIds.size} ` +
                `inUse=${inUseIds.size} ownedOrphans=${ownedOrphans.length} trulyForeign=${trulyForeign.length}`
            );
        }

        // 打头 5 个真正"我们没跟踪但 TV 里有"的 shape — 诊断用,看是不是用户手画 vs 漏跟踪
        if (window.__chanlunDebug && trulyForeign.length > 0) {
            console.log('[CHANLUN-DIAG][sweep] trulyForeign sample:', trulyForeign.slice(0, 5).map(s => ({
                id: s.id, name: s.name,
                // EntityInfo 含 zorder/visible 等;具体 points 需用 getShapeById 取
            })));
            // 进一步:对前 3 个尝试 getShapeById 拿 points,确认是 trend_line 错位
            trulyForeign.slice(0, 3).forEach(s => {
                try {
                    const api = this.chart.getShapeById(s.id);
                    const points = api?.getPoints?.();
                    console.log(`[CHANLUN-DIAG][sweep] foreign detail id=${s.id} name=${s.name} points=`, points);
                } catch (e) {
                    console.log(`[CHANLUN-DIAG][sweep] foreign detail id=${s.id} getShapeById 抛错`, e);
                }
            });
        }

        if (ownedOrphans.length > 0) {
            let confirmedGone = 0;
            let retried = 0;
            ownedOrphans.forEach(id => {
                if (canInspectShapes && !tvIds.has(id)) {
                    this._reconcileOwnedIds.delete(id);
                    this._pendingRemovalIds?.delete(id);
                    confirmedGone += 1;
                    return;
                }
                retried += 1;
                this.safeRemove(id);
            });
            if (window.__chanlunDebug) {
                console.log(
                    `[CHANLUN-DIAG][sweep] confirmed-gone=${confirmedGone} ` +
                    `retried=${retried} owned-after=${this._reconcileOwnedIds.size}`
                );
            }
        }

        // snap 校验由各 reconcile scope 在创建与稳定画布复核时完成；这里仅负责
        // 所有权容器之外的孤儿扫描，避免和按类型的几何自愈重复删除同一实体。
    }

    async draw_chanlun() {
        if (this._disposed) return;
        const currentGeneration = this._intervalGeneration;
        const capturedSeq = this._intervalSwitchSeq;

        if (!this.chart) {
            try {
                this.chart = this.widget.activeChart();
            } catch (e) {
                console.warn("[DEBUG-CHARTS] draw_chanlun: activeChart not available");
                return;
            }
        }

        const dataContextGeneration = this._dataContextGeneration || 0;
        const dataContextIdentity = this._currentDataIdentityKey();
        if (
            this._tvDataReadyGeneration !== dataContextGeneration ||
            this._tvDataReadyIdentity !== dataContextIdentity ||
            !this._chartDataReadyNow()
        ) {
            this._requestChanlunDrawWhenReady();
            return;
        }

        await new Promise(resolve => setTimeout(resolve, 0));

        if (this._intervalGeneration !== currentGeneration || capturedSeq !== this._intervalSwitchSeq) {
            console.warn("周期已切换，丢弃过期的缠论渲染任务");
            return;
        }

        const chartData = this.getChartData();
        if (!chartData) {
            if (!this._drawRetryCount) this._drawRetryCount = 0;
            // 切标的后新标的 getBars 的发起+回填+chart 渲染就绪可能 >5s(多标的并发/长桥 QPS
            // 排队时尤甚), retry 上限拉长到 30×500ms≈15s 覆盖; 数据到位后 handleBarsReadyEvent
            // 也会兜底主动重绘。retry 期间「数据未就绪」是切标的的正常过渡态, 用 clog(debug) 而非
            // console.warn, 避免切标的瞬间 console 被 NOT FOUND/exhausted 刷屏(被误读成功能故障)。
            if (this._drawRetryCount < 30) {
                this._drawRetryCount++;
                clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry#${this._drawRetryCount}/30 in 500ms`);
                setTimeout(() => this.debouncedDrawChanlun(), 500);
            } else {
                clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun: chartData=null, retry exhausted (handleBarsReadyEvent 仍会在数据到位后兜底重绘)`);
            }
            return;
        }
        this._drawRetryCount = 0;

        const symbolInterval = this.widget.symbolInterval();
        if (!symbolInterval) {
            console.warn("[DEBUG-CHARTS] draw_chanlun aborted: symbolInterval is null");
            return;
        }

        // 计算可视区与 bars 范围对比，分析"可视区比 bars 窄多少"
        const vr = this.chart?.getVisibleRange?.();
        const firstBarSec = (chartData.barsResult.bars?.[0]?.time || 0) / 1000;
        const lastBarSec = (chartData.barsResult.bars?.[chartData.barsResult.bars.length - 1]?.time || 0) / 1000;
        clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun executing interval=${symbolInterval.interval}`);
        clog(`[CHANLUN-TIMING]   from=${chartData.from} (visibleRange.from), barsRange=[${firstBarSec.toFixed(0)}, ${lastBarSec.toFixed(0)}], visibleRange=[${vr?.from?.toFixed(0)}, ${vr?.to?.toFixed(0)}]`);
        clog(`[CHANLUN-TIMING]   barsResult: bars=${chartData.barsResult.bars?.length || 0} fxs=${chartData.barsResult.fxs?.length || 0} bis=${chartData.barsResult.bis?.length || 0} xds=${chartData.barsResult.xds?.length || 0} strict=${chartData.barsResult.strict_structure_mode || 'missing'}`);

        const bisInside = (chartData.barsResult.bis || []).filter(b => (b.points?.[0]?.time ?? 0) >= chartData.from).length;
        const xdsInside = (chartData.barsResult.xds || []).filter(x => (x.points?.[0]?.time ?? 0) >= chartData.from).length;
        const totalBis = chartData.barsResult.bis?.length || 0;
        const totalXds = chartData.barsResult.xds?.length || 0;
        clog(`[CHANLUN-TIMING]   FILTER bis: ${bisInside}/${totalBis} pass (filtered out ${totalBis - bisInside}), xds: ${xdsInside}/${totalXds} pass (filtered out ${totalXds - xdsInside})`);
        this.markDrawingMutationStart('chanlun-redraw');
        const drawStartTs = performance.now();
        try {
            this.drawChartElements(chartData, symbolInterval.interval);
            // 切回已缓存周期时 TV 不再触发 onDataLoaded，handleDataReady 不来，
            // _initialLoadDone 永远 false，导致后续缩放平移无法补绘左侧形态。
            // 兜底：draw_chanlun 成功执行说明 chart 已有数据，强制置 true 恢复响应能力。
            this._initialLoadDone = true;
            clog(`[CHANLUN-TIMING] @${performance.now().toFixed(0)}ms draw_chanlun DONE (took ${(performance.now() - drawStartTs).toFixed(0)}ms) [_initialLoadDone=true]`);
        } finally {
            this.markDrawingMutationEnd('chanlun-redraw');
        }
    }

    _closeSseStream() {
        if (this._sse) {
            try { this._sse.close(); } catch (e) { /* ignore */ }
            this._sse = null;
        }
        this._clearSseFallback();
    }

    // SSE 被中间层(frp http vhost / nginx)缓冲时的兜底：手动驱动 datafeed 的
    // DataPulseProvider 轮询(getBars 增量, 拉后端最新 K线+缠论), 频率远快于默认 30s。
    _startSseFallback() {
        if (this._sseFallbackInterval) return;
        const dpp = this.udf_datafeed && this.udf_datafeed._dataPulseProvider;
        if (!dpp || typeof dpp._updateData !== 'function') return;
        this._sseFallbackInterval = setInterval(() => {
            try { dpp._updateData(); } catch (e) { /* ignore */ }
        }, 6000);
    }

    // 清理 SSE 健康哨兵 + fallback 快轮询定时器。
    _clearSseFallback() {
        if (this._sseHealthTimer) { clearTimeout(this._sseHealthTimer); this._sseHealthTimer = null; }
        if (this._sseFallbackInterval) { clearInterval(this._sseFallbackInterval); this._sseFallbackInterval = null; }
    }

    // 取 TV 画布当前渲染到的末根秒数(从 bars_result 末根, 与 onmessage/看门狗同源)。
    // 用于断档/看门狗判定的"画布末根"参照, 以及退避"上次 reset 是否生效"判定。无数据返回 null。
    _getViewLatestSec(resKey, interval) {
        try {
            const map = this.udf_datafeed && this.udf_datafeed._historyProvider && this.udf_datafeed._historyProvider.bars_result;
            if (!map) return null;
            let prev = map.get(resKey);
            if (!prev) {
                // 与 getChartData 同口径(M-1): 写键(getBars 用 symbolInfo.ticker)与读键
                // (symbolInterval().symbol)可能差一个 "market:" 前缀。剥前缀直查 / 去前缀唯一匹配再试。
                // 否则 G3(SSE 半开从无帧, 只有轮询写的 ticker 键)下看门狗 plain get 命中不到 → 静默失效。
                const bare = String(resKey).replace(/^[a-z]+:/, '');
                prev = map.get(bare);
                if (!prev && interval) {
                    const iv = String(interval).toLowerCase();
                    const cands = Array.from(map.keys()).filter(k =>
                        k.toLowerCase().endsWith(iv) &&
                        k.toLowerCase().replace(/^[a-z]+:/, '') === bare);
                    if (cands.length === 1) prev = map.get(cands[0]);
                }
            }
            const bars = prev && prev.bars;
            const lastBar = (bars && bars.length) ? bars[bars.length - 1] : null;
            return (lastBar && lastBar.time) ? Math.round(lastBar.time / 1000) : null;
        } catch (e) { return null; }
    }

    // 统一 resetData 入口: 防抖 + 指数退避(避免冷缓存/数据源停滞下每 8s reset 风暴) +
    // resetCache()(TV 文档契约, 清内部 datafeed 缓存/合并基准, 与重载按钮一致) + 退避记账。
    // 返回是否真的执行了 reset(false=被防抖跳过或无图表, 调用方据此决定是否继续走增量)。
    _doReset(resKey, reason, viewLatestSec) {
        try {
            if (typeof window === 'undefined' || !window.SseGap) return false;
            const nowSec = Math.floor(Date.now() / 1000);
            // 防抖+退避决策走纯函数(M-2): 被拒不消耗退避, 放行才基于"画布较上次 reset 是否前进"更新退避。
            const gate = window.SseGap.computeResetGate(this._resetState[resKey], nowSec, viewLatestSec, 30);
            if (!gate.allow) return false;
            const ch = (this.widget && typeof this.widget.activeChart === 'function') ? this.widget.activeChart() : null;
            // 无图表: 不持久化 gate.state(等同未 reset, 下次重新判), 避免空记账污染退避。
            if (!ch || typeof ch.resetData !== 'function') return false;
            // 契约: resetData 前先 resetCache(TV 文档要求)。
            this.widget.resetCache();
            // H1(阶段E): 置一次性 force_refresh 标志 → 下次 firstDataRequest(即将由 resetData 触发)绕过后端
            // 缓存重算,补齐断档(datafeed getBars 读此标志注入 force_refresh=1,用后即清)。同步置位,不改时序。
            try {
                const _hp = this.udf_datafeed && this.udf_datafeed._historyProvider;
                if (_hp) _hp._forceRefreshOnce = true;
            } catch (e) { /* ignore */ }
            ch.resetData();
            this._resetState[resKey] = gate.state;  // 仅真执行 reset 后落记账
            clog(`[SSE] resetData 全量补齐 reason=${reason} resKey=${resKey} backoff=${gate.state.backoffLevel}`);
            return true;
        } catch (e) { clog('[SSE] _doReset 异常', e); return false; }
    }

    _openSseStream() {
        // feature flag 关闭 → 完全不建连, 退回纯轮询(现状)。
        if (typeof window !== 'undefined' && window.__CHANLUN_SSE_ENABLED === false) return;
        if (typeof EventSource === 'undefined' || !this.widget) return;
        this._closeSseStream();
        let si = null;
        try { si = this.widget.symbolInterval(); } catch (e) { si = null; }
        if (!si || !si.symbol || !si.interval) return;
        // 与 getChartData 读 bars_result 的 key 同源(widget.symbolInterval())，
        // 保证 SSE 更新写入的 res_key 与缠论重绘读取的一致。
        const symbol = si.symbol;
        const resolution = si.interval;
        const url = `/tv/stream?symbol=${encodeURIComponent(symbol)}&resolution=${encodeURIComponent(resolution)}`;
        let es = null;
        try { es = new EventSource(url); } catch (e) { console.warn('[SSE] 创建失败', e); return; }
        this._sse = es;
        this._sseGotData = false;
        es.addEventListener('chanlun', (ev) => {
            // 首个数据帧到达 → SSE 通道确实通(未被中间层缓冲) → 取消 fallback 快轮询。
            if (!this._sseGotData) {
                this._sseGotData = true;
                this._clearSseFallback();
                clog('[SSE] 数据帧到达, 通道健康, 取消 fallback 快轮询');
            }
            try {
                // 断流时长门槛(M-3): es.onerror 记录的断开若 ≥30s 视为真断档 → 置位重连补齐;
                // 弱网瞬断秒回则忽略, 避免频繁无条件 resetData 闪烁。navigator 'online' 另路直接置位。
                if (this._disconnectedSinceMs != null) {
                    const _downMs = Date.now() - this._disconnectedSinceMs;
                    this._disconnectedSinceMs = null;
                    if (_downMs >= 30000) this._needResetOnNextData = true;
                }
                const data = JSON.parse(ev.data);
                const resKey = String(symbol).toLowerCase() + String(resolution).toLowerCase();
                const hp = this.udf_datafeed && this.udf_datafeed._historyProvider;

                // reconnect 后首帧: 无条件全量补齐, 不去猜中间缺几根。只要发生过真断档重连(online 事件
                // 或 es.onerror 断流≥30s 置位 _needResetOnNextData), 这一帧就整段重拉——治"30s 轮询抢在
                // SSE 首帧前推进 bars_result、污染断档参照系致漏判"的竞态, 以及服务端重启后的整段补齐。
                if (this._needResetOnNextData) {
                    this._needResetOnNextData = false;
                    if (this._doReset(resKey, 'reconnect', this._getViewLatestSec(resKey, resolution))) return;
                }

                // 常规断档检测(SSE 持续连通但数据跳变/长时间无更新恢复): data.t 完整快照里
                // (画布末根, SSE 末根) 之间真有缺根才补。_doReset 内含防抖+退避+resetCache。
                try {
                    if (hp && hp.bars_result && typeof window !== 'undefined' && window.SseGap) {
                        const _tvLatestSec = this._getViewLatestSec(resKey, resolution);
                        if (window.SseGap.shouldResetForGap(data.t, _tvLatestSec, resolution)) {
                            if (this._doReset(resKey, 'gap-detect', _tvLatestSec)) return;
                        }
                    }
                } catch (_gapErr) { /* gap 检测失败不阻断正常推送 */ }

                // 复用 getBars 同一合并逻辑(applyChanlunUpdate=_processHistoryResponse):
                // 更新 bars_result + 派发 chanlun-bars-ready → draw_chanlun 重绘缠论。
                if (hp && typeof hp.applyChanlunUpdate === 'function') {
                    hp.applyChanlunUpdate(data, { symbol, resolution });
                }
                // K线也随 SSE 实时刷新：把推送里的最新 bar 喂给 TV(绕过轮询/节流/bars<2 抛错)。
                if (this.udf_datafeed && typeof this.udf_datafeed.feedRealtimeBar === 'function') {
                    this.udf_datafeed.feedRealtimeBar(resKey, data, resolution);
                }
            } catch (e) { console.warn('[SSE] 处理推送失败', e); }
        });
        es.onerror = () => {
            // 记录首次断开时刻(不重复刷新)。下一帧到达时按"断开时长"决定是否需要全量补齐(M-3):
            // 弱网瞬断秒回(<阈值)忽略, 避免频繁无条件 resetData 闪烁; 真断档(≥阈值)才置位 reconnect-reset。
            // navigator 'online'(可靠的真断网恢复信号)另路直接置位, 不受此门槛限制。
            if (this._disconnectedSinceMs == null) this._disconnectedSinceMs = Date.now();
            clog('[SSE] 连接错误, 浏览器将自动重连');
        };
        // SSE 健康哨兵：连上后 12s 仍未收到任何数据帧 → 判定被中间层 vhost 缓冲 →
        // 启动 6s fallback 快轮询兜底实时；后续收到帧会自动取消(见 addEventListener)。
        this._sseHealthTimer = setTimeout(() => {
            if (!this._sseGotData) {
                clog('[SSE] 12s 未收到数据帧(疑被中间层缓冲), 启动 6s fallback 快轮询');
                this._startSseFallback();
            }
        }, 12000);
        clog(`[SSE] opened symbol=${symbol} res=${resolution}`);
    }

    dispose() {
        if (this._disposed) return;
        this._disposed = true;
        this._resetReconcileRetry();
        if (this._sweepOrphanTimer) {
            clearTimeout(this._sweepOrphanTimer);
            this._sweepOrphanTimer = null;
        }
        this._clearAllStrictScopes('dispose');
        this._strictStructureSnapshot = null;
        this._strictStructureContextToken = null;
        if (this._clDisplayButtonA11yCleanup) {
            const cleanup = this._clDisplayButtonA11yCleanup;
            this._clDisplayButtonA11yCleanup = null;
            try { cleanup(); } catch (e) { /* already disposed */ }
        }
        if (this._clDisplayMenuOutsideCleanup) {
            const cleanup = this._clDisplayMenuOutsideCleanup;
            this._clDisplayMenuOutsideCleanup = null;
            try { cleanup(); } catch (e) { /* already disposed */ }
        }
        const displayMenu = document.getElementById('cl_display_menu_' + this.id);
        if (displayMenu) displayMenu.remove();
        if (this._strictReconcileEpoch) {
            try { this._strictReconcileEpoch.dispose(); } catch (e) { /* already disposed */ }
            this._strictReconcileEpoch = null;
        }
        this._closeSseStream();
        // 工具栏注入重试定时器:正常 ≤4.2s 自停,但实例若在窗口内被 dispose 需显式清,
        // 避免定时器多跑几拍对已 dispose 实例操作(与 _sweepOrphanTimer 等清理对齐,审查 L-3)。
        if (this._lvlbtnTimer) { clearInterval(this._lvlbtnTimer); this._lvlbtnTimer = null; }
        if (this._watchdogTimer) { clearInterval(this._watchdogTimer); this._watchdogTimer = null; }
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
            this._visibilityHandler = null;
        }
        if (this._onlineHandler) {
            if (typeof window !== 'undefined') window.removeEventListener('online', this._onlineHandler);
            this._onlineHandler = null;
        }
        if (this._barReadyHandler) {
            window.removeEventListener('chanlun-bars-ready', this._barReadyHandler);
            this._barReadyHandler = null;
        }
        const registry = getTVRegistry();
        registry.chartManagers.delete(this.instanceId);
        registry.datafeeds.delete(this.instanceId);
        registry.widgets.delete(this.instanceId);
        if (registry.activeManagerId === this.instanceId) {
            registry.activeManagerId = null;
        }
    }
}

var Charts = (function () {
    return {
        show_tv_chart: function (id) {
            const chartManager = new ChartManager(id).init();
            // 调试钩子:把每个 ChartManager 实例存到 window.__cm[id],
            // 便于 Playwright/DevTools 直接 inspect cl_show_config / obj_charts。
            // 生产无害——挂在 window 不影响业务逻辑;若担心暴露面,可改为
            // 仅当 ``window.__chanlunDebug=true`` 时挂。
            try {
                if (!window.__cm) window.__cm = {};
                window.__cm[id] = chartManager;
            } catch (e) { /* ignore */ }
            return chartManager.widget;
        },
    };
})();
