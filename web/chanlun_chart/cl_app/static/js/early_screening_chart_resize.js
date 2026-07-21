"use strict";

(function initTradingScreeningChartResize(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TradingScreeningChartResize = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createResizeApi() {
  const LAYOUTS = new Set(["focus", "dual", "triple"]);
  const DEFAULT_SIZING = Object.freeze({
    heights: Object.freeze({ focus: null, dual: null, triple: null }),
    dualRatio: 50,
    tripleMainRatio: 67,
    tripleSideRatio: 50,
  });
  const LIMITS = Object.freeze({
    height: Object.freeze([520, 1200]),
    dualRatio: Object.freeze([30, 70]),
    tripleMainRatio: Object.freeze([55, 80]),
    tripleSideRatio: Object.freeze([25, 75]),
  });
  const FALLBACK_HEIGHTS = Object.freeze({ focus: 800, dual: 720, triple: 820 });

  function isRecord(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function normalizedNumber(value, fallback, limits) {
    const parsed = finiteNumber(value);
    if (parsed === null) return fallback;
    return Math.round(clamp(parsed, limits[0], limits[1]));
  }

  function normalizedHeight(value) {
    const parsed = finiteNumber(value);
    if (parsed === null) return null;
    return Math.round(clamp(parsed, LIMITS.height[0], LIMITS.height[1]));
  }

  function normalizeSizing(value) {
    const source = isRecord(value) ? value : {};
    const heights = isRecord(source.heights) ? source.heights : {};
    return {
      heights: {
        focus: normalizedHeight(heights.focus),
        dual: normalizedHeight(heights.dual),
        triple: normalizedHeight(heights.triple),
      },
      dualRatio: normalizedNumber(source.dualRatio, DEFAULT_SIZING.dualRatio, LIMITS.dualRatio),
      tripleMainRatio: normalizedNumber(
        source.tripleMainRatio,
        DEFAULT_SIZING.tripleMainRatio,
        LIMITS.tripleMainRatio,
      ),
      tripleSideRatio: normalizedNumber(
        source.tripleSideRatio,
        DEFAULT_SIZING.tripleSideRatio,
        LIMITS.tripleSideRatio,
      ),
    };
  }

  function cloneSizing(value) {
    const sizing = normalizeSizing(value);
    return { ...sizing, heights: { ...sizing.heights } };
  }

  function normalizedLayout(value) {
    return LAYOUTS.has(value) ? value : "focus";
  }

  function setCssVariable(style, name, value) {
    if (style && typeof style.setProperty === "function") style.setProperty(name, value);
  }

  function removeCssVariable(style, name) {
    if (style && typeof style.removeProperty === "function") style.removeProperty(name);
  }

  function handleFor(rootElement, type) {
    return rootElement && typeof rootElement.querySelector === "function"
      ? rootElement.querySelector(`[data-chart-resizer="${type}"]`)
      : null;
  }

  function measuredHeight(rootElement, layout, preferVisibleFrame = false) {
    if (preferVisibleFrame && rootElement && typeof rootElement.querySelectorAll === "function") {
      const frames = Array.from(rootElement.querySelectorAll("[data-chart-frame]") || []);
      for (const frame of frames) {
        if (!frame || typeof frame.getBoundingClientRect !== "function") continue;
        const rect = frame.getBoundingClientRect();
        const frameHeight = rect && finiteNumber(rect.height);
        if (frameHeight !== null && frameHeight > 0) {
          return Math.round(clamp(frameHeight, LIMITS.height[0], LIMITS.height[1]));
        }
      }
    }
    const grid = rootElement && typeof rootElement.querySelector === "function"
      ? rootElement.querySelector("[data-chart-grid]")
      : null;
    if (grid && typeof grid.getBoundingClientRect === "function") {
      const rect = grid.getBoundingClientRect();
      const height = rect && finiteNumber(rect.height);
      if (height !== null && height > 0) {
        return Math.round(clamp(height, LIMITS.height[0], LIMITS.height[1]));
      }
    }
    return FALLBACK_HEIGHTS[layout];
  }

  function setSeparatorState(handle, options) {
    if (!handle) return;
    const active = Boolean(options.active);
    handle.tabIndex = active ? 0 : -1;
    if (typeof handle.setAttribute !== "function") return;
    handle.setAttribute("aria-hidden", String(!active));
    handle.setAttribute("aria-label", options.label);
    handle.setAttribute("aria-valuemin", String(options.minimum));
    handle.setAttribute("aria-valuemax", String(options.maximum));
    handle.setAttribute("aria-valuenow", String(options.value));
    handle.setAttribute("aria-valuetext", options.valueText);
  }

  function applySizing(rootElement, requestedSizing, requestedLayout) {
    if (!rootElement) return normalizeSizing(requestedSizing);
    const sizing = normalizeSizing(requestedSizing);
    const layout = normalizedLayout(requestedLayout || (rootElement.dataset && rootElement.dataset.layout));
    const height = sizing.heights[layout];
    if (height === null) removeCssVariable(rootElement.style, "--es-chart-height");
    else setCssVariable(rootElement.style, "--es-chart-height", `${height}px`);

    setCssVariable(rootElement.style, "--es-dual-primary", `${sizing.dualRatio}fr`);
    setCssVariable(rootElement.style, "--es-dual-secondary", `${100 - sizing.dualRatio}fr`);
    setCssVariable(rootElement.style, "--es-dual-split", `${sizing.dualRatio}%`);
    setCssVariable(rootElement.style, "--es-triple-primary", `${sizing.tripleMainRatio}fr`);
    setCssVariable(rootElement.style, "--es-triple-secondary", `${100 - sizing.tripleMainRatio}fr`);
    setCssVariable(rootElement.style, "--es-triple-column-split", `${sizing.tripleMainRatio}%`);
    setCssVariable(rootElement.style, "--es-triple-top", `${sizing.tripleSideRatio}fr`);
    setCssVariable(rootElement.style, "--es-triple-bottom", `${100 - sizing.tripleSideRatio}fr`);
    setCssVariable(rootElement.style, "--es-triple-row-split", `${sizing.tripleSideRatio}%`);

    const currentHeight = height === null ? measuredHeight(rootElement, layout) : height;
    setSeparatorState(handleFor(rootElement, "columns"), {
      active: layout === "dual" || layout === "triple",
      label: layout === "triple" ? "调整主图与辅助图宽度" : "调整双周期图表宽度",
      minimum: layout === "triple" ? LIMITS.tripleMainRatio[0] : LIMITS.dualRatio[0],
      maximum: layout === "triple" ? LIMITS.tripleMainRatio[1] : LIMITS.dualRatio[1],
      value: layout === "triple" ? sizing.tripleMainRatio : sizing.dualRatio,
      valueText: `${layout === "triple" ? sizing.tripleMainRatio : sizing.dualRatio}%`,
    });
    setSeparatorState(handleFor(rootElement, "rows"), {
      active: layout === "triple",
      label: "调整两张辅助图高度比例",
      minimum: LIMITS.tripleSideRatio[0],
      maximum: LIMITS.tripleSideRatio[1],
      value: sizing.tripleSideRatio,
      valueText: `${sizing.tripleSideRatio}%`,
    });
    setSeparatorState(handleFor(rootElement, "height"), {
      active: true,
      label: "调整图表区高度",
      minimum: LIMITS.height[0],
      maximum: LIMITS.height[1],
      value: currentHeight,
      valueText: `${currentHeight} 像素`,
    });
    return sizing;
  }

  function createController(rootElement, initialSizing, options = {}) {
    if (!rootElement || typeof rootElement.querySelector !== "function") return null;
    const windowRef = options.windowRef || (typeof window !== "undefined" ? window : null);
    const onChange = typeof options.onChange === "function" ? options.onChange : null;
    const grid = rootElement.querySelector("[data-chart-grid]");
    const status = rootElement.querySelector("[data-chart-resize-status]");
    const resetButton = rootElement.querySelector("[data-chart-size-reset]");
    const handles = ["columns", "rows", "height"]
      .map((type) => handleFor(rootElement, type))
      .filter(Boolean);
    let layout = normalizedLayout(rootElement.dataset && rootElement.dataset.layout);
    let sizing = normalizeSizing(initialSizing);
    let activeDrag = null;
    let destroyed = false;
    const bindings = [];

    function controllerMeasuredHeight() {
      let compact = false;
      try {
        compact = Boolean(
          windowRef
          && typeof windowRef.matchMedia === "function"
          && windowRef.matchMedia("(max-width: 1100px)").matches,
        );
      } catch (_error) { /* Fall back to desktop measurement. */ }
      return measuredHeight(rootElement, layout, compact || layout !== "triple");
    }

    function apply() {
      sizing = applySizing(rootElement, sizing, layout);
    }

    function statusText(type) {
      if (type === "height") {
        const value = sizing.heights[layout] === null
          ? controllerMeasuredHeight()
          : sizing.heights[layout];
        return `图表高度 ${value} 像素`;
      }
      if (type === "rows") return `辅助图上下比例 ${sizing.tripleSideRatio}% / ${100 - sizing.tripleSideRatio}%`;
      const value = layout === "triple" ? sizing.tripleMainRatio : sizing.dualRatio;
      return `图表左右比例 ${value}% / ${100 - value}%`;
    }

    function announce(type, reset = false) {
      if (status) status.textContent = `${reset ? "已恢复默认，" : ""}${statusText(type)}`;
    }

    function notifyResize() {
      if (!windowRef || typeof windowRef.dispatchEvent !== "function") return;
      try {
        const EventConstructor = windowRef.Event || (typeof Event === "function" ? Event : null);
        if (EventConstructor) windowRef.dispatchEvent(new EventConstructor("resize"));
      } catch (_error) {
        // 图表库通常自行监听容器；resize 通知失败不影响尺寸状态。
      }
    }

    function commit() {
      if (onChange) onChange(cloneSizing(sizing));
    }

    function updateValue(type, value) {
      if (type === "height") {
        sizing.heights[layout] = normalizedHeight(value);
      } else if (type === "rows") {
        sizing.tripleSideRatio = normalizedNumber(
          value,
          sizing.tripleSideRatio,
          LIMITS.tripleSideRatio,
        );
      } else if (layout === "triple") {
        sizing.tripleMainRatio = normalizedNumber(
          value,
          sizing.tripleMainRatio,
          LIMITS.tripleMainRatio,
        );
      } else if (layout === "dual") {
        sizing.dualRatio = normalizedNumber(value, sizing.dualRatio, LIMITS.dualRatio);
      }
      apply();
      announce(type);
    }

    function typeIsActive(type) {
      if (type === "height") return true;
      if (type === "rows") return layout === "triple";
      return layout === "dual" || layout === "triple";
    }

    function pointerDown(event) {
      if (destroyed) return;
      const handle = event.currentTarget;
      const type = handle && handle.dataset ? handle.dataset.chartResizer : "";
      if (!typeIsActive(type)) return;
      const pointerId = event.pointerId;
      activeDrag = {
        handle,
        type,
        pointerId,
        startY: event.clientY,
        startHeight: sizing.heights[layout] === null
          ? controllerMeasuredHeight()
          : sizing.heights[layout],
      };
      if (rootElement.dataset) rootElement.dataset.resizing = "true";
      if (handle.classList) handle.classList.add("is-active");
      try {
        if (typeof handle.setPointerCapture === "function") handle.setPointerCapture(pointerId);
      } catch (_error) { /* Pointer capture is an enhancement. */ }
      if (typeof event.preventDefault === "function") event.preventDefault();
    }

    function pointerMove(event) {
      if (!activeDrag || event.pointerId !== activeDrag.pointerId || !grid) return;
      const type = activeDrag.type;
      if (type === "height") {
        updateValue(type, activeDrag.startHeight + event.clientY - activeDrag.startY);
      } else if (typeof grid.getBoundingClientRect === "function") {
        const rect = grid.getBoundingClientRect();
        if (type === "rows" && rect.height > 0) {
          updateValue(type, ((event.clientY - rect.top) / rect.height) * 100);
        } else if (type === "columns" && rect.width > 0) {
          updateValue(type, ((event.clientX - rect.left) / rect.width) * 100);
        }
      }
      if (typeof event.preventDefault === "function") event.preventDefault();
    }

    function finishPointer(event) {
      if (!activeDrag || event.pointerId !== activeDrag.pointerId) return;
      const { handle, pointerId, type } = activeDrag;
      activeDrag = null;
      if (rootElement.dataset) rootElement.dataset.resizing = "false";
      if (handle.classList) handle.classList.remove("is-active");
      try {
        if (typeof handle.releasePointerCapture === "function") handle.releasePointerCapture(pointerId);
      } catch (_error) { /* Capture may already have been released on cancel. */ }
      apply();
      announce(type);
      commit();
      notifyResize();
      if (typeof event.preventDefault === "function") event.preventDefault();
    }

    function resetType(type) {
      if (type === "height") sizing.heights[layout] = null;
      else if (type === "rows") sizing.tripleSideRatio = DEFAULT_SIZING.tripleSideRatio;
      else if (layout === "triple") sizing.tripleMainRatio = DEFAULT_SIZING.tripleMainRatio;
      else sizing.dualRatio = DEFAULT_SIZING.dualRatio;
      apply();
      announce(type, true);
      commit();
      notifyResize();
    }

    function keyDown(event) {
      if (destroyed) return;
      const handle = event.currentTarget;
      const type = handle && handle.dataset ? handle.dataset.chartResizer : "";
      if (!typeIsActive(type)) return;
      if (event.key === "Enter") {
        if (typeof event.preventDefault === "function") event.preventDefault();
        resetType(type);
        return;
      }
      const ratioStep = event.shiftKey ? 5 : 2;
      const heightStep = event.shiftKey ? 100 : 40;
      let current;
      let limits;
      let next = null;
      if (type === "height") {
        current = sizing.heights[layout] === null ? controllerMeasuredHeight() : sizing.heights[layout];
        limits = LIMITS.height;
        if (event.key === "ArrowUp") next = current - heightStep;
        if (event.key === "ArrowDown") next = current + heightStep;
      } else if (type === "rows") {
        current = sizing.tripleSideRatio;
        limits = LIMITS.tripleSideRatio;
        if (event.key === "ArrowUp") next = current - ratioStep;
        if (event.key === "ArrowDown") next = current + ratioStep;
      } else {
        current = layout === "triple" ? sizing.tripleMainRatio : sizing.dualRatio;
        limits = layout === "triple" ? LIMITS.tripleMainRatio : LIMITS.dualRatio;
        if (event.key === "ArrowLeft") next = current - ratioStep;
        if (event.key === "ArrowRight") next = current + ratioStep;
      }
      if (event.key === "Home") next = limits && limits[0];
      if (event.key === "End") next = limits && limits[1];
      if (next === null || next === undefined) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      updateValue(type, next);
      commit();
      notifyResize();
    }

    function doubleClick(event) {
      const handle = event.currentTarget;
      const type = handle && handle.dataset ? handle.dataset.chartResizer : "";
      if (!typeIsActive(type)) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      resetType(type);
    }

    function resetAll(event) {
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      sizing = cloneSizing(DEFAULT_SIZING);
      apply();
      if (status) status.textContent = "已恢复所有图表尺寸默认值";
      commit();
      notifyResize();
    }

    function bind(node, type, handler) {
      if (!node || typeof node.addEventListener !== "function") return;
      node.addEventListener(type, handler);
      bindings.push([node, type, handler]);
    }

    for (const handle of handles) {
      bind(handle, "pointerdown", pointerDown);
      bind(handle, "pointermove", pointerMove);
      bind(handle, "pointerup", finishPointer);
      bind(handle, "pointercancel", finishPointer);
      bind(handle, "keydown", keyDown);
      bind(handle, "dblclick", doubleClick);
    }
    bind(resetButton, "click", resetAll);
    apply();

    return {
      getSizing() { return cloneSizing(sizing); },
      setLayout(requestedLayout) {
        layout = normalizedLayout(requestedLayout);
        if (rootElement.dataset) rootElement.dataset.layout = layout;
        apply();
        return layout;
      },
      resetAll,
      destroy() {
        if (destroyed) return;
        destroyed = true;
        for (const [node, type, handler] of bindings) node.removeEventListener(type, handler);
        bindings.length = 0;
        activeDrag = null;
        if (rootElement.dataset) rootElement.dataset.resizing = "false";
      },
    };
  }

  return {
    DEFAULT_SIZING,
    LIMITS,
    applySizing,
    createController,
    normalizeSizing,
  };
});
