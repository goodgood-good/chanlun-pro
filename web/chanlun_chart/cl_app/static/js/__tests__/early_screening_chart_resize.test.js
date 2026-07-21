"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const path = require("node:path");

const resizePath = path.resolve(__dirname, "../early_screening_chart_resize.js");

function loadResize() {
  delete require.cache[require.resolve(resizePath)];
  return require(resizePath);
}

function makeClassList() {
  const values = new Set();
  return {
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    contains(name) { return values.has(name); },
  };
}

function makeElement(dataset = {}) {
  const listeners = new Map();
  const attributes = new Map();
  return {
    dataset: { ...dataset },
    classList: makeClassList(),
    tabIndex: 0,
    capturedPointer: null,
    releasedPointer: null,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(handler);
    },
    removeEventListener(type, handler) {
      if (listeners.has(type)) listeners.get(type).delete(handler);
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
    setPointerCapture(pointerId) { this.capturedPointer = pointerId; },
    releasePointerCapture(pointerId) { this.releasedPointer = pointerId; },
    dispatch(type, values = {}) {
      const event = {
        type,
        currentTarget: this,
        target: this,
        pointerId: 1,
        clientX: 0,
        clientY: 0,
        key: "",
        shiftKey: false,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        ...values,
      };
      for (const handler of listeners.get(type) || []) handler(event);
      return event;
    },
  };
}

function makeStyle() {
  const values = new Map();
  return {
    setProperty(name, value) { values.set(name, String(value)); },
    removeProperty(name) { values.delete(name); },
    getPropertyValue(name) { return values.get(name) || ""; },
  };
}

function makeFixture(layout = "triple") {
  const grid = makeElement({ chartGrid: "" });
  grid.getBoundingClientRect = () => ({ left: 100, top: 50, width: 1000, height: 800 });
  const columns = makeElement({ chartResizer: "columns" });
  const rows = makeElement({ chartResizer: "rows" });
  const height = makeElement({ chartResizer: "height" });
  const reset = makeElement({ chartSizeReset: "" });
  const status = makeElement({ chartResizeStatus: "" });
  status.textContent = "";
  const nodes = new Map([
    ["[data-chart-grid]", grid],
    ['[data-chart-resizer="columns"]', columns],
    ['[data-chart-resizer="rows"]', rows],
    ['[data-chart-resizer="height"]', height],
    ["[data-chart-size-reset]", reset],
    ["[data-chart-resize-status]", status],
  ]);
  const root = {
    dataset: { layout },
    style: makeStyle(),
    querySelector(selector) { return nodes.get(selector) || null; },
  };
  const resizeEvents = [];
  class FakeEvent {
    constructor(type) { this.type = type; }
  }
  const windowRef = {
    Event: FakeEvent,
    dispatchEvent(event) { resizeEvents.push(event.type); },
  };
  return { root, grid, columns, rows, height, reset, status, windowRef, resizeEvents };
}

test("normalizeSizing preserves responsive heights and clamps every adjustable boundary", () => {
  const Resize = loadResize();

  assert.deepEqual(Resize.normalizeSizing(null), {
    heights: { focus: null, dual: null, triple: null },
    dualRatio: 50,
    tripleMainRatio: 67,
    tripleSideRatio: 50,
  });
  assert.deepEqual(Resize.normalizeSizing({
    heights: { focus: 300, dual: "900", triple: 1600 },
    dualRatio: 95,
    tripleMainRatio: 20,
    tripleSideRatio: "68",
  }), {
    heights: { focus: 520, dual: 900, triple: 1200 },
    dualRatio: 70,
    tripleMainRatio: 55,
    tripleSideRatio: 68,
  });
  assert.equal(Resize.normalizeSizing({ heights: { focus: "bad" } }).heights.focus, null);
});

test("applySizing exposes ratios as CSS variables and keeps irrelevant handles out of focus order", () => {
  const Resize = loadResize();
  const fixture = makeFixture("focus");

  Resize.applySizing(fixture.root, {
    heights: { focus: 760, dual: null, triple: null },
    dualRatio: 58,
    tripleMainRatio: 72,
    tripleSideRatio: 45,
  }, "focus");

  assert.equal(fixture.root.style.getPropertyValue("--es-chart-height"), "760px");
  assert.equal(fixture.root.style.getPropertyValue("--es-dual-primary"), "58fr");
  assert.equal(fixture.root.style.getPropertyValue("--es-triple-column-split"), "72%");
  assert.equal(fixture.root.style.getPropertyValue("--es-triple-row-split"), "45%");
  assert.equal(fixture.columns.tabIndex, -1);
  assert.equal(fixture.rows.tabIndex, -1);
  assert.equal(fixture.height.tabIndex, 0);
  assert.equal(fixture.height.getAttribute("aria-valuenow"), "760");
});

test("pointer drag adjusts triple columns and commits once after pointer release", () => {
  const Resize = loadResize();
  const fixture = makeFixture("triple");
  const changes = [];
  const controller = Resize.createController(fixture.root, null, {
    windowRef: fixture.windowRef,
    onChange(value) { changes.push(value); },
  });

  fixture.columns.dispatch("pointerdown", { pointerId: 9, clientX: 770, clientY: 100 });
  assert.equal(fixture.root.dataset.resizing, "true");
  assert.equal(fixture.columns.capturedPointer, 9);
  fixture.columns.dispatch("pointermove", { pointerId: 9, clientX: 850, clientY: 100 });
  assert.equal(controller.getSizing().tripleMainRatio, 75);
  assert.equal(fixture.root.style.getPropertyValue("--es-triple-primary"), "75fr");
  assert.equal(changes.length, 0);

  fixture.columns.dispatch("pointerup", { pointerId: 9, clientX: 850, clientY: 100 });
  assert.equal(fixture.root.dataset.resizing, "false");
  assert.equal(fixture.columns.releasedPointer, 9);
  assert.equal(changes.length, 1);
  assert.equal(changes[0].tripleMainRatio, 75);
  assert.deepEqual(fixture.resizeEvents, ["resize"]);
});

test("height drag and pointer cancel preserve the latest safe value and restore interaction", () => {
  const Resize = loadResize();
  const fixture = makeFixture("dual");
  const changes = [];
  const controller = Resize.createController(fixture.root, null, {
    windowRef: fixture.windowRef,
    onChange(value) { changes.push(value); },
  });

  fixture.height.dispatch("pointerdown", { pointerId: 4, clientY: 800 });
  fixture.height.dispatch("pointermove", { pointerId: 4, clientY: 1100 });
  assert.equal(controller.getSizing().heights.dual, 1100);
  fixture.height.dispatch("pointercancel", { pointerId: 4, clientY: 1100 });

  assert.equal(fixture.root.dataset.resizing, "false");
  assert.equal(fixture.root.style.getPropertyValue("--es-chart-height"), "1100px");
  assert.equal(changes.at(-1).heights.dual, 1100);
  assert.match(fixture.status.textContent, /1100/);
});

test("compact triple layout measures one visible chart instead of the stacked grid", () => {
  const Resize = loadResize();
  const fixture = makeFixture("triple");
  const visibleFrame = makeElement({ chartFrame: "5m" });
  visibleFrame.getBoundingClientRect = () => ({ width: 600, height: 620 });
  fixture.grid.getBoundingClientRect = () => ({ left: 0, top: 0, width: 600, height: 1900 });
  fixture.root.querySelectorAll = (selector) => selector === "[data-chart-frame]" ? [visibleFrame] : [];
  fixture.windowRef.matchMedia = () => ({ matches: true });
  const controller = Resize.createController(fixture.root, null, { windowRef: fixture.windowRef });

  fixture.height.dispatch("pointerdown", { pointerId: 5, clientY: 500 });
  fixture.height.dispatch("pointermove", { pointerId: 5, clientY: 540 });
  fixture.height.dispatch("pointerup", { pointerId: 5, clientY: 540 });

  assert.equal(controller.getSizing().heights.triple, 660);
  assert.equal(fixture.height.getAttribute("aria-valuenow"), "660");
});

test("keyboard controls clamp ratios and Enter resets only the active separator", () => {
  const Resize = loadResize();
  const fixture = makeFixture("triple");
  const changes = [];
  const controller = Resize.createController(fixture.root, {
    heights: { focus: 700, dual: 720, triple: 900 },
    dualRatio: 60,
    tripleMainRatio: 79,
    tripleSideRatio: 50,
  }, {
    windowRef: fixture.windowRef,
    onChange(value) { changes.push(value); },
  });

  const move = fixture.columns.dispatch("keydown", { key: "ArrowRight" });
  assert.equal(move.defaultPrevented, true);
  assert.equal(controller.getSizing().tripleMainRatio, 80);
  fixture.rows.dispatch("keydown", { key: "ArrowUp", shiftKey: true });
  assert.equal(controller.getSizing().tripleSideRatio, 45);
  fixture.height.dispatch("keydown", { key: "ArrowDown", shiftKey: true });
  assert.equal(controller.getSizing().heights.triple, 1000);

  fixture.columns.dispatch("keydown", { key: "Enter" });
  const sizing = controller.getSizing();
  assert.equal(sizing.tripleMainRatio, 67);
  assert.equal(sizing.tripleSideRatio, 45);
  assert.equal(sizing.heights.triple, 1000);
  assert.ok(changes.length >= 4);
});

test("layout changes select independent heights and reset button restores all responsive defaults", () => {
  const Resize = loadResize();
  const fixture = makeFixture("focus");
  const controller = Resize.createController(fixture.root, {
    heights: { focus: 740, dual: 860, triple: 980 },
    dualRatio: 65,
    tripleMainRatio: 74,
    tripleSideRatio: 60,
  }, { windowRef: fixture.windowRef });

  controller.setLayout("dual");
  assert.equal(fixture.root.style.getPropertyValue("--es-chart-height"), "860px");
  assert.equal(fixture.columns.tabIndex, 0);
  assert.equal(fixture.rows.tabIndex, -1);
  fixture.reset.dispatch("click");

  assert.deepEqual(controller.getSizing(), {
    heights: { focus: null, dual: null, triple: null },
    dualRatio: 50,
    tripleMainRatio: 67,
    tripleSideRatio: 50,
  });
  assert.equal(fixture.root.style.getPropertyValue("--es-chart-height"), "");
  controller.destroy();
  fixture.columns.dispatch("keydown", { key: "ArrowRight" });
  assert.equal(controller.getSizing().dualRatio, 50);
});
