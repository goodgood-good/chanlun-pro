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
    if (kind === "center_preview") {
      identifier = item.preview_id;
    } else if (
      kind === "formal_center" ||
      kind === "center_projection" ||
      kind === "center_observation"
    ) {
      identifier = item.center_id;
    } else if (kind === "strict_trend") {
      identifier = item.trend_id;
    } else if (kind === "pending_movement") {
      identifier = item.partition_id;
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

  function chartTimeCoordinate(epochSeconds, frequency) {
    if (!Number.isInteger(epochSeconds)) {
      throw new Error("chart source time must be epoch seconds");
    }
    const calendar = String(frequency || "");
    if (!["d", "2d", "w", "m", "q", "y"].includes(calendar)) {
      return epochSeconds;
    }

    const source = new Date(epochSeconds * 1000);
    let year = source.getUTCFullYear();
    let month = source.getUTCMonth();
    let day = source.getUTCDate();
    if (calendar === "w") {
      day -= (source.getUTCDay() + 6) % 7;
    } else if (calendar === "m") {
      day = 1;
    } else if (calendar === "q") {
      month = Math.floor(month / 3) * 3;
      day = 1;
    } else if (calendar === "y") {
      month = 0;
      day = 1;
    }
    const coordinate = Date.UTC(year, month, day) / 1000;
    if (!Number.isInteger(coordinate)) {
      throw new Error("calendar chart coordinate is invalid");
    }
    return coordinate;
  }

  function itemToChartCoordinates(item, frequency) {
    if (!item || !Array.isArray(item.points)) {
      throw new Error("strict render item requires points");
    }
    return {
      ...item,
      points: item.points.map((point) => ({
        ...point,
        time: chartTimeCoordinate(point && point.time, frequency),
      })),
    };
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

  function lowerBound(values, target) {
    let low = 0;
    let high = values.length;
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2);
      if (values[middle] < target) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function drawableRange(loadedRange, visibleRange) {
    const loaded = requireRange(loadedRange, "loadedRange");
    const visible = requireRange(visibleRange, "visibleRange");
    const left = Math.max(loaded.from, visible.from);
    const right = Math.min(loaded.to, visible.to);
    if (left > right) return null;

    // 可视区边界可能落在休市时段或周末。若把线段锚定在这种任意时间，
    // TradingView 会静默吸附到其他 K 线，所以优先使用已经加载的真实 K 线坐标。
    const barTimes = Array.isArray(loaded.barTimes)
      ? loaded.barTimes
      : null;
    if (!barTimes || barTimes.length === 0) return { from: left, to: right };

    const firstIndex = lowerBound(barTimes, left);
    const afterLastIndex = lowerBound(barTimes, right + 1);
    if (firstIndex >= barTimes.length || afterLastIndex <= firstIndex) return null;
    const from = barTimes[firstIndex];
    const to = barTimes[afterLastIndex - 1];
    if (from > right || to < left || from > to) return null;
    return { from, to };
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
        semantic_direction: item.semantic_direction,
        direction_status: item.direction_status,
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
    const drawable = drawableRange(loaded, visible);
    const previous = new Map();
    const duplicatePrevious = new Map();
    for (const entity of existing || []) {
      const key = requireString(entity.logicalKey, "existing logicalKey");
      if (previous.has(key)) {
        const duplicates = duplicatePrevious.get(key) || [previous.get(key)];
        duplicates.push(entity);
        duplicatePrevious.set(key, duplicates);
        continue;
      }
      previous.set(key, entity);
    }

    const planned = new Map();
    for (const sourceItem of incoming || []) {
      const key = logicalKey(sourceItem);
      if (planned.has(key)) throw new Error(`duplicate incoming key: ${key}`);
      if (!intersects(sourceItem, visible)) continue;
      if (drawable === null) continue;
      // 不修改不可变的严格证据，只把 TradingView 绘图副本裁剪到当前画面的
      // 真实 K 线；否则画面外锚点会在创建后被吸附，无法通过精确校验。
      const renderItem = clipToLoadedRange(sourceItem, drawable);
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
      const duplicates = duplicatePrevious.get(key);
      if (duplicates) {
        // 延迟异步回调不能让两个实体共同占有一个逻辑结构。这里从头重建该键，
        // 同时修复已损坏的容器，避免因拒绝整份快照而在画面留下重叠图形。
        for (const duplicate of duplicates) {
          if (duplicate.id != null && !removeIds.includes(duplicate.id)) {
            removeIds.push(duplicate.id);
          }
        }
        if (next) createItems.push(next);
        continue;
      }
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
    // 除增量外还返回完整目标集合。TradingView 在加载历史或改变可视区时可能
    // 移动已创建的线工具，因此调用方必须把保留实体与当前目标几何重新比较，
    // 不能只依赖创建时保存的不可变指纹。
    return {
      removeIds,
      createItems,
      desiredItems: Array.from(planned.values()),
    };
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
    chartTimeCoordinate,
    clipToLoadedRange,
    drawableRange,
    geometryFingerprint,
    logicalKey,
    itemToChartCoordinates,
    planReconcile,
    renderKey,
    scopeKey,
  };
});
