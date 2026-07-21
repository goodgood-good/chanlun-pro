"use strict";

(function attachChartStructureReconcile(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChartStructureReconcile = api;
})(typeof globalThis === "object" ? globalThis : this, function createApi() {
  function requireString(value, field) {
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`${field} is required`);
    }
    return value;
  }

  function requireRange(range, field) {
    if (
      !range ||
      !Number.isInteger(range.from) ||
      !Number.isInteger(range.to) ||
      range.from > range.to
    ) {
      throw new Error(`${field} must contain ordered epoch seconds`);
    }
    return range;
  }

  function logicalKey(item) {
    if (!item || typeof item !== "object") {
      throw new Error("strict render item is required");
    }
    const kind = requireString(item.render_kind, "render_kind");
    let identifier;
    if (
      kind === "formal_center" ||
      kind === "center_projection" ||
      kind === "center_observation"
    ) {
      identifier = item.center_id;
    } else if (kind === "strict_trend") {
      identifier = item.trend_id;
    } else if (
      kind === "point_confirmed" ||
      kind === "point_approaching"
    ) {
      identifier = item.point_id;
    } else if (kind === "strict_divergence") {
      identifier = item.divergence_id;
    } else {
      throw new Error(`unsupported strict render_kind: ${kind}`);
    }
    return `${kind}:${requireString(identifier, `${kind} identity`)}`;
  }

  function renderKey(item) {
    return requireString(item && item.render_id, "render_id");
  }

  function scopeKey(context, item) {
    if (!context || typeof context !== "object") {
      throw new Error("chart context is required");
    }
    const parts = [
      requireString(context.chartInstanceId, "chartInstanceId"),
      requireString(context.symbol, "symbol"),
      requireString(context.interval, "interval"),
      requireString(context.price_basis_revision, "price_basis_revision"),
      String(item && item.structural_level),
      requireString(item && item.render_kind, "render_kind"),
    ];
    if (!Number.isInteger(item && item.structural_level)) {
      throw new Error("structural_level must be an integer");
    }
    return parts.map((part) => encodeURIComponent(part)).join("|");
  }

  function barTimeMsToEpochSeconds(barTime) {
    if (
      !Number.isInteger(barTime) ||
      Math.abs(barTime) < 100000000000 ||
      barTime % 1000 !== 0
    ) {
      throw new Error("bar time must be milliseconds aligned to a whole second");
    }
    return barTime / 1000;
  }

  function sourceBounds(item) {
    if (!item || !Array.isArray(item.points) || item.points.length === 0) {
      throw new Error("strict render item requires points");
    }
    const times = item.points.map((point) => {
      if (!point || !Number.isInteger(point.time)) {
        throw new Error("strict shape point time must be epoch seconds");
      }
      return point.time;
    });
    return { from: Math.min(...times), to: Math.max(...times) };
  }

  function intersects(item, range) {
    const bounds = sourceBounds(item);
    return bounds.to >= range.from && bounds.from <= range.to;
  }

  function clipToLoadedRange(item, loadedRange) {
    const loaded = requireRange(loadedRange, "loadedRange");
    const bounds = sourceBounds(item);
    if (bounds.to < loaded.from || bounds.from > loaded.to) return null;
    const points = item.points.map((point) => ({ ...point }));
    if (points.length === 1) {
      if (points[0].time < loaded.from || points[0].time > loaded.to) {
        return null;
      }
    } else {
      points[0].time = Math.max(points[0].time, loaded.from);
      points[points.length - 1].time = Math.min(
        points[points.length - 1].time,
        loaded.to,
      );
      if (points[0].time > points[points.length - 1].time) return null;
    }
    return { ...item, points };
  }

  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      const result = {};
      for (const key of Object.keys(value).sort()) {
        result[key] = canonical(value[key]);
      }
      return result;
    }
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new Error("geometry values must be finite");
    }
    return value;
  }

  function geometryFingerprint(item) {
    if (!item || typeof item !== "object") {
      throw new Error("render item is required");
    }
    return JSON.stringify(
      canonical({
        render_kind: item.render_kind,
        points: item.points,
        state: item.state,
        direction: item.direction,
        point_type: item.point_type,
        kind: item.kind,
        variant: item.variant,
        tradable: item.tradable,
        linestyle: item.linestyle,
      }),
    );
  }

  function planReconcile(existing, incoming, loadedRange, visibleRange) {
    const loaded = requireRange(loadedRange, "loadedRange");
    const visible = requireRange(visibleRange, "visibleRange");
    const previous = new Map();
    for (const entity of existing || []) {
      const key = requireString(entity.logicalKey, "existing logicalKey");
      if (previous.has(key)) throw new Error(`duplicate existing key: ${key}`);
      previous.set(key, entity);
    }

    const planned = new Map();
    for (const sourceItem of incoming || []) {
      const key = logicalKey(sourceItem);
      if (planned.has(key)) throw new Error(`duplicate incoming key: ${key}`);
      if (!intersects(sourceItem, visible)) continue;
      const renderItem = clipToLoadedRange(sourceItem, loaded);
      if (renderItem === null) continue;
      planned.set(key, {
        ...renderItem,
        logicalKey: key,
        renderKey: renderKey(sourceItem),
        geometryFingerprint: geometryFingerprint(renderItem),
      });
    }

    const removeIds = [];
    const createItems = [];
    for (const [key, entity] of previous.entries()) {
      const next = planned.get(key);
      if (
        !next ||
        entity.renderKey !== next.renderKey ||
        entity.geometryFingerprint !== next.geometryFingerprint
      ) {
        removeIds.push(entity.id);
      }
    }
    for (const [key, next] of planned.entries()) {
      const entity = previous.get(key);
      if (
        !entity ||
        entity.renderKey !== next.renderKey ||
        entity.geometryFingerprint !== next.geometryFingerprint
      ) {
        createItems.push(next);
      }
    }
    return { removeIds, createItems };
  }

  class ReconcileEpoch {
    constructor() {
      this.values = new Map();
      this.disposed = false;
    }

    next(scope) {
      if (this.disposed) throw new Error("reconcile epoch is disposed");
      const value = (this.values.get(scope) || 0) + 1;
      this.values.set(scope, value);
      return value;
    }

    current(scope, value) {
      return !this.disposed && this.values.get(scope) === value;
    }

    dispose() {
      this.disposed = true;
      this.values.clear();
    }
  }

  return {
    ReconcileEpoch,
    barTimeMsToEpochSeconds,
    clipToLoadedRange,
    geometryFingerprint,
    logicalKey,
    planReconcile,
    renderKey,
    scopeKey,
  };
});
