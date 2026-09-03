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
    if (!env || env.__CHANLUN_EMBEDDED_CHART === true || !env.document) return null;
    if (env[STATE_KEY]) return env[STATE_KEY];

    const overlay = env.document.getElementById("tv_chart_transition");
    const area = env.document.getElementById("tv_charts_area");
    const title = env.document.getElementById("tv_chart_transition_title");
    const detail = env.document.getElementById("tv_chart_transition_detail");
    if (!overlay) return null;

    const state = {
      active: false,
      target: null,
      expected: 0,
      ready: new Set(),
      timeout: null,
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
      state.target = target;
      state.expected = count;
      state.ready.clear();
      overlay.hidden = false;
      overlay.dataset.state = "loading";
      if (area) area.setAttribute("aria-busy", "true");
      if (title) title.textContent = `正在切换至 ${text(value.label) || target.code}`;
      if (detail) detail.textContent = "K 线、指标与缠论结构全部稳定后统一显示";
      if (typeof env.setTimeout === "function") {
        const key = target.key;
        state.timeout = env.setTimeout(() => {
          state.timeout = null;
          if (!state.active || !state.target || state.target.key !== key) return;
          overlay.dataset.state = "delayed";
          if (title) title.textContent = `${target.code} 加载时间较长`;
          if (detail) detail.textContent = "仍在等待完整结构，请稍候或重新选择标的";
        }, MAX_WAIT_MS);
      }
      return true;
    };

    state.finish = () => {
      if (!state.active) return false;
      clearDeadline();
      state.active = false;
      state.ready.clear();
      overlay.hidden = true;
      overlay.dataset.state = "idle";
      if (area) area.setAttribute("aria-busy", "false");
      return true;
    };

    state.fail = (message) => {
      if (!state.active) return false;
      clearDeadline();
      overlay.hidden = false;
      overlay.dataset.state = "error";
      if (title) title.textContent = "图表切换未完成";
      if (detail) detail.textContent = text(message) || "请重新选择标的后重试";
      return true;
    };

    state.markReady = (value) => {
      if (!state.active || !state.target) return false;
      const identity = normalizeIdentity(value, state.target);
      if (!identity || identity.key !== state.target.key) return false;
      const readyKey = text(value && value.managerId)
        || `${identity.key}|${text(value && (value.interval || value.resolution))}`;
      state.ready.add(readyKey);
      if (state.ready.size < state.expected) return true;
      state.finish();
      return true;
    };

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
