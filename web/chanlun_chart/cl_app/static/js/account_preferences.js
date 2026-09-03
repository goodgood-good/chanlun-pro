(function (root) {
  "use strict";

  var SCHEMA = "chanlun-account-chart-preferences/v1";
  var CACHE_PREFIX = "chanlun_account_preferences_v1:";
  var bootstrap = root.__CHANLUN_ACCOUNT_PREFERENCES__;
  // Decision-support charts are URL-driven views inside four same-origin
  // iframes.  localStorage is shared by the parent and every iframe, so an
  // embedded chart must never apply or persist its temporary symbol/interval
  // as the account workspace.  Detect this before index.html initializes its
  // body-level flag; account_preferences.js is loaded from <head>.
  var embeddedReadOnly = false;
  try {
    embeddedReadOnly = new URLSearchParams(
      (root.location && root.location.search) || ""
    ).get("chart_embed") === "decision-support";
  } catch (_) { /* an unavailable URL parser keeps the normal page behavior */ }
  var enabled = !!(
    bootstrap &&
    bootstrap.account_key &&
    bootstrap.preferences &&
    bootstrap.preferences.schema === SCHEMA
  );
  var cacheKey = enabled
    ? CACHE_PREFIX + bootstrap.account_key
    : "";
  var values = {};
  var dirty = false;
  var dirtyKeyVersions = {};
  var mutationSerial = 0;
  var saveTimer = null;
  var savePromise = null;

  function storageGet(key) {
    try { return root.localStorage.getItem(key); }
    catch (_) { return null; }
  }

  function storageSet(key, value) {
    try { root.localStorage.setItem(key, String(value)); return true; }
    catch (_) { return false; }
  }

  function storageRemove(key) {
    try { root.localStorage.removeItem(key); return true; }
    catch (_) { return false; }
  }

  function isApprovedKey(key) {
    return key === "tv_chart" ||
      key === "trading_screening_view" ||
      key === "chart_menu_width" ||
      key === "chart_menu_collapsed" ||
      key === "chart_analysis_overview_collapsed" ||
      /^cl_show_config_[1-4]_[A-Za-z0-9_]{1,10}$/.test(key) ||
      /^cl_independent_drawings_[1-4]$/.test(key);
  }

  function cloneValues(source) {
    var output = {};
    if (!source || typeof source !== "object" || Array.isArray(source)) return output;
    Object.keys(source).forEach(function (key) {
      if (isApprovedKey(key) && typeof source[key] === "string") {
        output[key] = source[key];
      }
    });
    return output;
  }

  function localApprovedKeys() {
    var keys = [
      "tv_chart",
      "trading_screening_view",
      "chart_menu_width",
      "chart_menu_collapsed",
      "chart_analysis_overview_collapsed"
    ];
    try {
      for (var i = 0; i < root.localStorage.length; i += 1) {
        var key = root.localStorage.key(i);
        if (key && isApprovedKey(key) && keys.indexOf(key) === -1) keys.push(key);
      }
    } catch (_) { /* storage enumeration is optional */ }
    return keys;
  }

  function readAccountCache() {
    if (!cacheKey) return null;
    try {
      var parsed = JSON.parse(storageGet(cacheKey) || "null");
      if (!parsed || parsed.schema !== SCHEMA || !parsed.pending) return null;
      var cachedValues = cloneValues(parsed.values);
      var changedKeys = Array.isArray(parsed.changedKeys)
        ? parsed.changedKeys.filter(isApprovedKey)
        : Object.keys(cachedValues);
      return { values: cachedValues, changedKeys: changedKeys };
    } catch (_) {
      return null;
    }
  }

  function writeAccountCache(pending) {
    if (!cacheKey) return;
    storageSet(cacheKey, JSON.stringify({
      schema: SCHEMA,
      pending: pending === true,
      changedKeys: Object.keys(dirtyKeyVersions),
      values: values
    }));
  }

  function applyAccountValues(nextValues) {
    localApprovedKeys().forEach(storageRemove);
    Object.keys(nextValues).forEach(function (key) {
      storageSet(key, nextValues[key]);
    });
  }

  function csrfToken() {
    var meta = root.document && root.document.querySelector
      ? root.document.querySelector('meta[name="csrf-token"]')
      : null;
    return meta && meta.content ? meta.content : "";
  }

  function saveNow() {
    if (!enabled || !dirty || typeof root.fetch !== "function") {
      return savePromise || Promise.resolve(false);
    }
    if (savePromise) {
      return savePromise.then(function () { return dirty ? saveNow() : true; });
    }

    var sentVersions = {};
    Object.keys(dirtyKeyVersions).forEach(function (key) {
      sentVersions[key] = dirtyKeyVersions[key];
    });
    var changedKeys = Object.keys(sentVersions);
    dirty = false;
    var snapshot = cloneValues(values);
    writeAccountCache(true);
    var headers = { "Content-Type": "application/json", "Accept": "application/json" };
    var token = csrfToken();
    if (token) headers["X-CSRFToken"] = token;
    var failed = false;
    savePromise = root.fetch("/api/chart/preferences", {
      method: "PUT",
      credentials: "same-origin",
      cache: "no-store",
      keepalive: true,
      headers: headers,
      body: JSON.stringify({
        schema: SCHEMA,
        values: snapshot,
        merge: true,
        changed_keys: changedKeys
      })
    }).then(function (response) {
      if (!response.ok) throw new Error("chart preferences save failed: " + response.status);
      return response.json();
    }).then(function (payload) {
      if (!payload || payload.ok !== true) throw new Error("chart preferences save rejected");
      changedKeys.forEach(function (key) {
        if (dirtyKeyVersions[key] === sentVersions[key]) delete dirtyKeyVersions[key];
      });
      dirty = Object.keys(dirtyKeyVersions).length > 0;
      if (!dirty) writeAccountCache(false);
      return true;
    }).catch(function (error) {
      failed = true;
      dirty = true;
      writeAccountCache(true);
      if (root.console && typeof root.console.warn === "function") {
        root.console.warn("[AccountPreferences] save failed", error);
      }
      return false;
    }).finally(function () {
      savePromise = null;
      // Retry new changes that arrived during a successful in-flight save.
      // A network failure stays in the account-specific pending cache and is
      // retried on the next user change/page load, avoiding a tight retry loop.
      if (dirty && !failed && !saveTimer) scheduleSave();
    });
    return savePromise;
  }

  function scheduleSave() {
    if (!enabled) return;
    if (saveTimer !== null) root.clearTimeout(saveTimer);
    saveTimer = root.setTimeout(function () {
      saveTimer = null;
      saveNow();
    }, 250);
  }

  function setItem(key, value) {
    if (!isApprovedKey(key)) return storageSet(key, value);
    var normalized = String(value);
    if (embeddedReadOnly) return true;
    storageSet(key, normalized);
    if (!enabled) return true;
    if (values[key] === normalized) return true;
    values[key] = normalized;
    dirtyKeyVersions[key] = ++mutationSerial;
    dirty = true;
    writeAccountCache(true);
    scheduleSave();
    return true;
  }

  function removeItem(key) {
    if (embeddedReadOnly && isApprovedKey(key)) return true;
    storageRemove(key);
    if (!enabled || !isApprovedKey(key) || !Object.prototype.hasOwnProperty.call(values, key)) {
      return true;
    }
    delete values[key];
    dirtyKeyVersions[key] = ++mutationSerial;
    dirty = true;
    writeAccountCache(true);
    scheduleSave();
    return true;
  }

  function flush() {
    if (saveTimer !== null) {
      root.clearTimeout(saveTimer);
      saveTimer = null;
    }
    return saveNow();
  }

  function waitBrieflyForSave() {
    return new Promise(function (resolve) {
      var completed = false;
      var timeoutId = root.setTimeout(function () {
        if (completed) return;
        completed = true;
        resolve(false);
      }, 600);
      Promise.resolve(flush()).then(function (result) {
        if (completed) return;
        completed = true;
        root.clearTimeout(timeoutId);
        resolve(result);
      }, function () {
        if (completed) return;
        completed = true;
        root.clearTimeout(timeoutId);
        resolve(false);
      });
    });
  }

  if (enabled) {
    var serverValues = cloneValues(bootstrap.preferences.values);
    var pendingCache = readAccountCache();
    // A pending per-account snapshot represents a user change that the server
    // has not acknowledged yet.  It must win over an older server snapshot on
    // reload, otherwise a transient network failure silently rolls settings
    // back to their previous values.
    if (pendingCache) {
      values = pendingCache.values;
      // Legacy pending caches did not record deletions. Include every server
      // key as changed so an absent local key is removed during the merge.
      var pendingKeys = pendingCache.changedKeys.concat(
        Object.keys(serverValues).filter(function (key) {
          return !Object.prototype.hasOwnProperty.call(values, key);
        })
      );
      pendingKeys.forEach(function (key) {
        dirtyKeyVersions[key] = ++mutationSerial;
      });
      dirty = true;
    } else if (bootstrap.exists === true) {
      values = serverValues;
    }
    if (!embeddedReadOnly) {
      applyAccountValues(values);
      writeAccountCache(dirty);
      if (dirty) scheduleSave();
    }

    if (!embeddedReadOnly && typeof root.addEventListener === "function") {
      root.addEventListener("pagehide", function () { flush(); });
    }
  }

  function getItem(key) {
    if (
      embeddedReadOnly &&
      isApprovedKey(key) &&
      Object.prototype.hasOwnProperty.call(values, key)
    ) return values[key];
    return storageGet(key);
  }

  root.AccountPreferences = {
    schema: SCHEMA,
    enabled: enabled,
    readOnly: embeddedReadOnly,
    username: enabled ? String(bootstrap.username || "") : "",
    isApprovedKey: isApprovedKey,
    getItem: getItem,
    setItem: setItem,
    removeItem: removeItem,
    captureTvChart: function () {
      var raw = getItem("tv_chart");
      if (raw !== null) setItem("tv_chart", raw);
      return raw;
    },
    flush: flush,
    reloadAfterSave: function () {
      return waitBrieflyForSave().finally(function () { root.location.reload(); });
    },
    navigateAfterSave: function (url, replace) {
      return waitBrieflyForSave().finally(function () {
        if (replace) root.location.replace(url);
        else root.location.assign(url);
      });
    }
  };
})(typeof window !== "undefined" ? window : globalThis);
