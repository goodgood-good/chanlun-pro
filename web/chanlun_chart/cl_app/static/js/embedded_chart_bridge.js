"use strict";

(function attachEmbeddedChartBridge(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) {
    root.EmbeddedChartBridge = api;
    if (root.__CHANLUN_EMBEDDED_CHART === true) api.install(root);
  }
})(typeof globalThis === "object" ? globalThis : this, function createEmbeddedChartBridge() {
  const VERSION = 1;
  const SWITCH_MESSAGE = "chanlun:chart-switch";
  const ACTIVITY_MESSAGE = "chanlun:chart-activity";
  const REALTIME_RESUME_MESSAGE = "chanlun:chart-realtime-resume";
  const READY_MESSAGE = "chanlun:chart-bridge-ready";
  const DATA_READY_MESSAGE = "chanlun:chart-data-ready";
  const ERROR_MESSAGE = "chanlun:chart-switch-error";
  const ACCEPTED_MESSAGE = "chanlun:chart-switch-accepted";
  const FREQUENCIES = new Set(["d", "30m", "5m", "1m"]);
  const MARKETS = new Set([
    "a", "hk", "us", "fx", "futures", "ny_futures", "currency", "currency_spot",
  ]);

  function text(value) {
    return value === null || value === undefined ? "" : String(value).trim();
  }

  function normalizeFrequency(value) {
    const raw = text(value).toLowerCase();
    return {
      "1d": "d",
      d: "d",
      "30": "30m",
      "30m": "30m",
      "5": "5m",
      "5m": "5m",
      "1": "1m",
      "1m": "1m",
    }[raw] || "";
  }

  function normalizeIdentity(value) {
    const safe = value && typeof value === "object" ? value : {};
    const ticker = text(safe.symbol || safe.ticker);
    const separator = ticker.indexOf(":");
    const market = text(
      safe.market || (separator >= 0 ? ticker.slice(0, separator) : ""),
    ).toLowerCase();
    const code = text(
      safe.code || (separator >= 0 ? ticker.slice(separator + 1) : ticker),
    ).toUpperCase();
    const frequency = normalizeFrequency(safe.frequency || safe.interval || safe.resolution);
    if (!MARKETS.has(market) || !code || !FREQUENCIES.has(frequency)) return null;
    return { market, code, frequency };
  }

  function normalizeSwitchMessage(value) {
    if (!value || typeof value !== "object") return null;
    if (value.type !== SWITCH_MESSAGE || value.version !== VERSION) return null;
    const identity = normalizeIdentity(value);
    const requestId = text(value.requestId);
    if (!identity || !requestId || identity.code.length > 64) return null;
    if (!/^[A-Z0-9._/\-]+$/.test(identity.code)) return null;
    return { ...identity, requestId };
  }

  function normalizeActivityMessage(value) {
    if (!value || typeof value !== "object") return null;
    if (value.type !== ACTIVITY_MESSAGE || value.version !== VERSION) return null;
    if (typeof value.active !== "boolean") return null;
    return { active: value.active };
  }

  function normalizeRealtimeResumeMessage(value) {
    if (!value || typeof value !== "object") return null;
    if (value.type !== REALTIME_RESUME_MESSAGE || value.version !== VERSION) return null;
    const requestId = text(value.requestId);
    if (!requestId || requestId.length > 128) return null;
    return { requestId };
  }

  function identitiesEqual(left, right) {
    return Boolean(
      left
      && right
      && left.market === right.market
      && left.code === right.code
      && left.frequency === right.frequency
    );
  }

  function currentIdentity(env) {
    try {
      const manager = env.__cm && env.__cm["1"];
      if (manager && typeof manager.getCurrentChartIdentity === "function") {
        return normalizeIdentity(manager.getCurrentChartIdentity());
      }
      const widget = Array.isArray(env.chart_widgets) ? env.chart_widgets[0] : null;
      const chart = widget && typeof widget.activeChart === "function"
        ? widget.activeChart()
        : null;
      if (!chart) return null;
      return normalizeIdentity({
        symbol: typeof chart.symbol === "function" ? chart.symbol() : "",
        interval: typeof chart.resolution === "function" ? chart.resolution() : "",
      });
    } catch (_error) {
      return null;
    }
  }

  function install(env = globalThis) {
    if (!env || env.__CHANLUN_EMBEDDED_CHART !== true) return null;
    if (env.__chanlunEmbeddedChartBridgeState) {
      return env.__chanlunEmbeddedChartBridgeState;
    }
    const state = {
      pending: null,
      retryTimer: null,
      retryCount: 0,
      switchInFlight: null,
      activeSwitch: null,
      active: true,
    };
    env.__chanlunEmbeddedChartBridgeState = state;

    const post = (type, values = {}) => {
      try {
        if (!env.parent || env.parent === env) return false;
        env.parent.postMessage(
          { type, version: VERSION, ...values },
          env.location.origin,
        );
        return true;
      } catch (_error) {
        return false;
      }
    };
    state.post = post;

    const fail = (reason, failedRequest = state.pending) => {
      post(ERROR_MESSAGE, {
        requestId: failedRequest && failedRequest.requestId,
        reason,
      });
    };

    const attemptSwitch = () => {
      state.retryTimer = null;
      // TradingView serializes its own setSymbol work. Starting another call
      // while the previous promise is unresolved can strand the datafeed
      // behind several obsolete symbol changes. Keep exactly one operation in
      // flight; incoming messages replace state.pending and are coalesced to
      // the newest target when the current operation settles.
      if (state.switchInFlight !== null) return;
      const pending = state.pending;
      if (!pending) return;
      const widget = Array.isArray(env.chart_widgets) ? env.chart_widgets[0] : null;
      let chart = null;
      try {
        chart = widget && typeof widget.activeChart === "function"
          ? widget.activeChart()
          : null;
      } catch (_error) {
        chart = null;
      }
      if (!chart || typeof chart.setSymbol !== "function") {
        if (state.retryCount < 100) {
          state.retryCount += 1;
          state.retryTimer = env.setTimeout(attemptSwitch, 50);
        } else {
          fail("chart_not_ready");
        }
        return;
      }

      const before = currentIdentity(env);
      const manager = env.__cm && env.__cm["1"];
      post(ACCEPTED_MESSAGE, {
        requestId: pending.requestId,
        market: pending.market,
        code: pending.code,
        frequency: pending.frequency,
      });
      // Release the old symbol's EventSource connection before TradingView
      // dispatches the new history request. The parent resumes all visible
      // frames together only after their complete K-line/structure snapshots
      // have crossed the stable-ready barrier.
      if (manager && typeof manager.deferEmbeddedRealtime === "function") {
        manager.deferEmbeddedRealtime(pending.requestId);
      }
      if (identitiesEqual(before, pending)) {
        if (
          manager
          && typeof manager.requestEmbeddedStableReady === "function"
        ) {
          manager.requestEmbeddedStableReady();
          return;
        }
        try {
          if (typeof chart.dataReady === "function" && chart.dataReady() === true) {
            post(DATA_READY_MESSAGE, pending);
          }
        } catch (_error) {
          // The regular ChartManager data-ready callback will report completion.
        }
        return;
      }
      try {
        state.switchInFlight = pending.requestId;
        state.activeSwitch = pending;
        const result = chart.setSymbol(`${pending.market}:${pending.code}`);
        if (result && typeof result.then === "function") {
          Promise.resolve(result).catch(
            () => {
              if (state.switchInFlight === pending.requestId) {
                state.switchInFlight = null;
                state.activeSwitch = null;
              }
              if (state.pending && state.pending.requestId !== pending.requestId) {
                attemptSwitch();
                return;
              }
              fail("set_symbol_failed", pending);
            },
          );
        }
      } catch (_error) {
        state.switchInFlight = null;
        state.activeSwitch = null;
        if (state.pending && state.pending.requestId !== pending.requestId) {
          attemptSwitch();
          return;
        }
        fail("set_symbol_failed", pending);
      }
    };
    state.attemptSwitch = attemptSwitch;

    state.onMessage = (event) => {
      if (
        !event
        || event.origin !== env.location.origin
        || event.source !== env.parent
      ) return;
      const activity = normalizeActivityMessage(event.data);
      if (activity) {
        state.active = activity.active;
        const manager = env.__cm && env.__cm["1"];
        if (manager && typeof manager.setEmbeddedChartActive === "function") {
          manager.setEmbeddedChartActive(activity.active);
        }
        return;
      }
      const realtimeResume = normalizeRealtimeResumeMessage(event.data);
      if (realtimeResume) {
        const pendingRequestId = state.pending && state.pending.requestId;
        if (pendingRequestId && realtimeResume.requestId !== pendingRequestId) return;
        const manager = env.__cm && env.__cm["1"];
        if (manager && typeof manager.resumeEmbeddedRealtime === "function") {
          manager.resumeEmbeddedRealtime(realtimeResume.requestId);
        }
        return;
      }
      const pending = normalizeSwitchMessage(event.data);
      if (!pending) return;
      state.pending = pending;
      state.retryCount = 0;
      if (state.retryTimer !== null) env.clearTimeout(state.retryTimer);
      attemptSwitch();
    };
    env.addEventListener("message", state.onMessage);
    post(READY_MESSAGE, currentIdentity(env) || {});
    return state;
  }

  function notifyDataReady(value, env = globalThis) {
    const state = env && env.__chanlunEmbeddedChartBridgeState;
    if (!state || typeof state.post !== "function") return false;
    const identity = normalizeIdentity(value) || currentIdentity(env);
    if (!identity) return false;
    const activeSwitch = state.activeSwitch;
    if (activeSwitch && identitiesEqual(identity, activeSwitch)) {
      if (state.switchInFlight === activeSwitch.requestId) {
        state.switchInFlight = null;
      }
      state.activeSwitch = null;
      if (state.pending && state.pending.requestId !== activeSwitch.requestId) {
        if (typeof state.attemptSwitch === "function") state.attemptSwitch();
      }
    }
    const pending = state.pending;
    if (pending && !identitiesEqual(identity, pending)) return false;
    return state.post(DATA_READY_MESSAGE, {
      ...identity,
      requestId: pending ? pending.requestId : "",
    });
  }

  return {
    VERSION,
    SWITCH_MESSAGE,
    ACTIVITY_MESSAGE,
    REALTIME_RESUME_MESSAGE,
    READY_MESSAGE,
    DATA_READY_MESSAGE,
    ERROR_MESSAGE,
    normalizeFrequency,
    normalizeIdentity,
    normalizeSwitchMessage,
    normalizeActivityMessage,
    normalizeRealtimeResumeMessage,
    identitiesEqual,
    install,
    notifyDataReady,
  };
});
