"use strict";

(function attachChartTransition(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) {
    root.ChartTransition = api;
    if (root.document) api.install(root);
  }
})(typeof globalThis === "object" ? globalThis : this, function createChartTransition() {
  const STATE_KEY = "__chanlunChartTransitionState";
  const MAX_WAIT_MS = 45000;

  function text(value) {
    return value === null || value === undefined ? "" : String(value).trim();
  }

  function normalizeIdentity(value, fallback) {
    const safe = value && typeof value === "object" ? value : {};
    const base = fallback && typeof fallback === "object" ? fallback : {};
    const ticker = text(safe.symbol || safe.ticker);
    const separator = ticker.indexOf(":");
    const market = text(
      safe.market || (separator >= 0 ? ticker.slice(0, separator) : base.market),
    ).toLowerCase();
    const code = text(
      safe.code || (separator >= 0 ? ticker.slice(separator + 1) : ticker) || base.code,
    ).toUpperCase();
    if (!market || !code) return null;
    return { market, code, key: `${market}:${code}` };
  }

  function expectedCount(value) {
    const parsed = Math.floor(Number(value));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  }

  function install(env = globalThis) {
    if (!env || !env.document) return null;
    if (env[STATE_KEY]) return env[STATE_KEY];

    const overlay = env.document.getElementById("tv_chart_transition");
    const area = env.document.getElementById("tv_charts_area");
    const title = env.document.getElementById("tv_chart_transition_title");
    const detail = env.document.getElementById("tv_chart_transition_detail");
    if (!overlay) return null;
    const spinner = overlay.querySelector?.('.ca-chart-transition__spinner');
    const card = overlay.querySelector?.('.ca-chart-transition__card');
    let actions = null;
    let retryButton = null;
    if (card && typeof env.document.createElement === 'function') {
      actions = env.document.createElement('div');
      actions.hidden = true;
      actions.style.cssText = 'display:none;gap:8px;align-items:center;justify-content:center';
      retryButton = env.document.createElement('button');
      retryButton.type = 'button';
      retryButton.className = 'layui-btn layui-btn-sm';
      retryButton.textContent = '重试行情';
      const dismiss = env.document.createElement('button');
      dismiss.type = 'button';
      dismiss.className = 'layui-btn layui-btn-primary layui-btn-sm';
      dismiss.textContent = '返回图表';
      retryButton.addEventListener('click', () => { void state.retry(); });
      dismiss.addEventListener('click', () => state.finish());
      actions.appendChild(retryButton);
      actions.appendChild(dismiss);
      card.appendChild(actions);
    }

    const state = {
      active: false,
      failed: false,
      target: null,
      expected: 0,
      ready: new Set(),
      timeout: null,
      generation: 0,
    };
    env[STATE_KEY] = state;

    const clearDeadline = () => {
      if (state.timeout !== null && typeof env.clearTimeout === "function") {
        env.clearTimeout(state.timeout);
      }
      state.timeout = null;
    };

    state.begin = (value) => {
      const target = normalizeIdentity(value);
      if (!target) return false;
      const count = expectedCount(value && value.expected);
      if (state.active && state.target && state.target.key === target.key) {
        state.expected = Math.max(state.expected, count);
        return true;
      }

      clearDeadline();
      state.active = true;
      state.failed = false;
      state.generation += 1;
      state.target = target;
      state.expected = count;
      state.ready.clear();
      overlay.hidden = false;
      overlay.dataset.state = "loading";
      if (spinner) { spinner.hidden = false; spinner.style.display = ''; spinner.style.animation = ''; }
      if (actions) { actions.hidden = true; actions.style.display = 'none'; }
      if (area) area.setAttribute("aria-busy", "true");
      if (title) title.textContent = `正在切换至 ${text(value.label) || target.code}`;
      if (detail) detail.textContent = "正在加载 K 线，线段和中枢随后补充";
      if (typeof env.setTimeout === "function") {
        const key = target.key;
        const generation = state.generation;
        state.timeout = env.setTimeout(() => {
          state.timeout = null;
          if (!state.active || !state.target || state.target.key !== key || state.generation !== generation) return;
          state.fail('行情加载超时，请重试行情或重新选择标的。');
        }, MAX_WAIT_MS);
      }
      return true;
    };

    state.finish = () => {
      if (!state.active && !state.failed) return false;
      clearDeadline();
      state.active = false;
      state.failed = false;
      state.ready.clear();
      overlay.hidden = true;
      overlay.dataset.state = "idle";
      if (area) area.setAttribute("aria-busy", "false");
      return true;
    };

    state.fail = (message) => {
      if (!state.active && !state.failed) return false;
      clearDeadline();
      state.active = false;
      state.failed = true;
      overlay.hidden = false;
      overlay.dataset.state = "error";
      if (area) area.setAttribute("aria-busy", "false");
      if (spinner) { spinner.hidden = true; spinner.style.display = 'none'; spinner.style.animation = 'none'; }
      if (actions) { actions.hidden = false; actions.style.display = 'flex'; }
      if (retryButton) retryButton.disabled = false;
      if (title) title.textContent = "行情暂不可用";
      if (detail) detail.textContent = text(message) || "请重新选择标的后重试";
      return true;
    };

    state.retry = async () => {
      if (!state.failed || !state.target) return false;
      const target = state.target;
      const managers = Object.values(env.__cm || {}).filter(manager => {
        try {
          return !manager._disposed && typeof manager.manualReloadData === 'function'
            && normalizeIdentity(manager.widget?.symbolInterval?.())?.key === target.key;
        } catch (_) { return false; }
      });
      if (!managers.length) {
        state.fail('图表尚未就绪，请重新选择标的。');
        return false;
      }
      state.begin({...target, expected: managers.length});
      const generation = state.generation;
      const results = await Promise.allSettled(managers.map(manager => Promise.resolve().then(() => manager.manualReloadData())));
      if (generation !== state.generation || state.target?.key !== target.key) return false;
      if (results.some(result => result.status === 'rejected' || result.value === false)) {
        state.fail('重新加载未完成，请稍后重试或重新选择标的。');
        return false;
      }
      return true;
    };

    state.markReady = (value) => {
      if ((!state.active && !state.failed) || !state.target) return false;
      const identity = normalizeIdentity(value, state.target);
      if (!identity || identity.key !== state.target.key) return false;
      const readyKey = text(value && value.managerId)
        || `${identity.key}|${text(value && (value.interval || value.resolution))}`;
      state.ready.add(readyKey);
      if (state.ready.size < state.expected) return true;
      state.finish();
      return true;
    };

    if (typeof env.addEventListener === 'function') {
      env.addEventListener('chanlun-history-error', (event) => {
        const detail = event && event.detail;
        const identity = normalizeIdentity(detail);
        if (!identity) return;
        if (!state.active && !state.failed) {
          // An initial page has no symbol-switch transition yet. Match the
          // live chart owner before opening feedback for its first request.
          const manager = Object.values(env.__cm || {}).find(cm => cm.instanceId === detail.managerId);
          let context = null;
          try { context = manager && manager.widget.symbolInterval(); } catch (_) { return; }
          const current = normalizeIdentity(context);
          if (!current || current.key !== identity.key || String(context.interval) !== String(detail.resolution)) return;
          state.begin(identity);
        }
        if (!state.target || identity.key !== state.target.key) return;
        state.fail(detail.message);
      });
    }

    return state;
  }

  function stateFor(env = globalThis) {
    return env && (env[STATE_KEY] || install(env));
  }

  function begin(value, env = globalThis) {
    const state = stateFor(env);
    return state ? state.begin(value || {}) : false;
  }

  function markReady(value, env = globalThis) {
    const state = stateFor(env);
    return state ? state.markReady(value || {}) : false;
  }

  function fail(message, env = globalThis) {
    const state = stateFor(env);
    return state ? state.fail(message) : false;
  }

  return {
    MAX_WAIT_MS,
    normalizeIdentity,
    install,
    begin,
    markReady,
    fail,
  };
});
