/*
 * 缠论手动力度对比面板 —— signal_monitor Phase 2 前端。
 *
 * 自包含模块：页面加载后自建一个悬浮按钮 + 侧栏面板，调用后端
 *   - GET /strength_compare/lines/<market>/<code>   取笔/线段列表（下拉选参考段）
 *   - GET /strength_compare/<market>/<code>         参考段 vs 当前在走段 力度对比
 *
 * 设计取舍：缠论笔/线段在图上是 disableSelection 锁定装饰图形，无法画布点选；
 * 故用「面板下拉选段」实现「点选参考段」，不与 TradingView 图表库内部纠缠。
 * 当前 市场/代码/周期 尽力自动探测，并在面板里以可编辑输入框呈现 —— 探测不准
 * 时用户可手改，优雅降级。
 */
(function () {
  "use strict";

  // TradingView 周期 → 后端 frequency
  var TV_INTERVAL_TO_FREQ = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "60m", "120": "120m",
    "1D": "d", "D": "d", "1W": "w", "W": "w", "1M": "m", "M": "m",
  };

  function detectContext() {
    // 尽力探测当前 市场/代码/周期；探测不到就留空，由用户在面板补。
    var ctx = { market: "", code: "", freq: "" };
    try {
      if (window.Utils && typeof Utils.get_market === "function") {
        ctx.market = Utils.get_market() || "";
        if (ctx.market && typeof Utils.get_local_data === "function") {
          ctx.code = Utils.get_local_data(ctx.market + "_code") || "";
        }
      }
      if (window.tvWidget && typeof tvWidget.symbolInterval === "function") {
        var si = tvWidget.symbolInterval() || {};
        if (si.interval != null) {
          ctx.freq = TV_INTERVAL_TO_FREQ[String(si.interval)] || "";
        }
        if ((!ctx.market || !ctx.code) && si.symbol) {
          var parts = String(si.symbol).split(":");
          if (parts.length === 2) {
            ctx.market = ctx.market || parts[0];
            ctx.code = ctx.code || parts[1];
          }
        }
      }
    } catch (e) {
      /* 探测失败不致命，用户手填 */
    }
    return ctx;
  }

  function el(tag, attrs, html) {
    var e = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === "style") e.style.cssText = attrs[k];
        else e.setAttribute(k, attrs[k]);
      }
    }
    if (html != null) e.innerHTML = html;
    return e;
  }

  var PANEL_ID = "sc-panel";

  function buildPanel() {
    if (document.getElementById(PANEL_ID)) return;

    // 悬浮开关按钮
    var btn = el("div", {
      id: "sc-toggle-btn",
      style: "position:fixed;right:14px;bottom:120px;z-index:9998;" +
        "background:#2b6cb0;color:#fff;padding:7px 12px;border-radius:4px;" +
        "cursor:pointer;font-size:13px;box-shadow:0 1px 4px rgba(0,0,0,.4);",
    }, "力度对比");
    document.body.appendChild(btn);

    // 面板
    var panel = el("div", {
      id: PANEL_ID,
      style: "position:fixed;right:14px;bottom:160px;z-index:9999;width:330px;" +
        "background:#1e222d;color:#d1d4dc;border:1px solid #363a45;border-radius:6px;" +
        "padding:12px;font-size:12px;display:none;box-shadow:0 2px 12px rgba(0,0,0,.5);",
    });
    panel.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
      '  <b style="font-size:13px;">缠论手动力度对比</b>' +
      '  <span id="sc-close" style="cursor:pointer;padding:0 4px;">✕</span>' +
      '</div>' +
      '<div style="display:flex;gap:6px;margin-bottom:6px;">' +
      '  <input id="sc-market" placeholder="市场" style="width:60px;" />' +
      '  <input id="sc-code" placeholder="代码" style="flex:1;" />' +
      '  <input id="sc-freq" placeholder="周期" style="width:56px;" />' +
      '</div>' +
      '<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center;">' +
      '  <select id="sc-kind"><option value="xd">线段</option><option value="bi">笔</option></select>' +
      '  <button id="sc-load" style="flex:1;">加载线段</button>' +
      '</div>' +
      '<div style="margin-bottom:6px;">' +
      '  <div style="margin-bottom:3px;color:#868993;">参考段（与最新在走段对比）：</div>' +
      '  <select id="sc-ref" style="width:100%;"><option value="">— 先点「加载线段」—</option></select>' +
      '</div>' +
      '<button id="sc-run" style="width:100%;margin-bottom:8px;">对 比</button>' +
      '<div id="sc-result" style="border-top:1px solid #363a45;padding-top:8px;min-height:24px;color:#868993;">尚无结果</div>';
    document.body.appendChild(panel);

    // 输入框样式
    ["sc-market", "sc-code", "sc-freq"].forEach(function (id) {
      var i = document.getElementById(id);
      i.style.cssText = "background:#2a2e39;border:1px solid #363a45;color:#d1d4dc;" +
        "padding:3px 5px;border-radius:3px;" + i.style.cssText;
    });

    var ctx = detectContext();
    document.getElementById("sc-market").value = ctx.market;
    document.getElementById("sc-code").value = ctx.code;
    document.getElementById("sc-freq").value = ctx.freq;

    btn.addEventListener("click", function () {
      var p = document.getElementById(PANEL_ID);
      var show = p.style.display === "none";
      p.style.display = show ? "block" : "none";
      if (show) {
        // 每次打开重新探测一遍上下文（用户可能换了标的/周期）
        var c = detectContext();
        if (c.market) document.getElementById("sc-market").value = c.market;
        if (c.code) document.getElementById("sc-code").value = c.code;
        if (c.freq) document.getElementById("sc-freq").value = c.freq;
      }
    });
    document.getElementById("sc-close").addEventListener("click", function () {
      document.getElementById(PANEL_ID).style.display = "none";
    });
    document.getElementById("sc-load").addEventListener("click", loadLines);
    document.getElementById("sc-run").addEventListener("click", runCompare);
  }

  function ctxFields() {
    return {
      market: (document.getElementById("sc-market").value || "").trim(),
      code: (document.getElementById("sc-code").value || "").trim(),
      freq: (document.getElementById("sc-freq").value || "").trim(),
      kind: document.getElementById("sc-kind").value,
    };
  }

  function setResult(html, color) {
    var r = document.getElementById("sc-result");
    r.style.color = color || "#d1d4dc";
    r.innerHTML = html;
  }

  function loadLines() {
    var f = ctxFields();
    if (!f.market || !f.code || !f.freq) {
      setResult("请先填写 市场 / 代码 / 周期", "#e57373");
      return;
    }
    var url = "/strength_compare/lines/" + encodeURIComponent(f.market) +
      "/" + encodeURIComponent(f.code) +
      "?frequency=" + encodeURIComponent(f.freq) + "&line_kind=" + f.kind;
    setResult("加载线段中…", "#868993");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var sel = document.getElementById("sc-ref");
        sel.innerHTML = "";
        if (!res.ok) {
          setResult("加载失败：" + (res.error || "未知错误"), "#e57373");
          return;
        }
        var lines = res.lines || [];
        if (!lines.length) {
          setResult("该周期下没有可用线段", "#e57373");
          return;
        }
        // 倒序展示（新→旧）；最后一根是「当前在走段」，标注出来
        for (var i = lines.length - 1; i >= 0; i--) {
          var ln = lines[i];
          var arrow = ln.type === "up" ? "↑涨" : "↓跌";
          var tag = (i === lines.length - 1) ? "（当前段）" : "";
          var opt = document.createElement("option");
          opt.value = ln.start + "|" + ln.end;
          opt.textContent = arrow + " " + ln.start + " → " + ln.end + tag;
          sel.appendChild(opt);
        }
        setResult("已加载 " + lines.length + " 段，请选参考段后点「对比」", "#868993");
      })
      .catch(function (e) {
        setResult("请求异常：" + e, "#e57373");
      });
  }

  function runCompare() {
    var f = ctxFields();
    if (!f.market || !f.code || !f.freq) {
      setResult("请先填写 市场 / 代码 / 周期", "#e57373");
      return;
    }
    var refVal = document.getElementById("sc-ref").value;
    if (!refVal || refVal.indexOf("|") < 0) {
      setResult("请先选择参考段", "#e57373");
      return;
    }
    var rs = refVal.split("|");
    var url = "/strength_compare/" + encodeURIComponent(f.market) +
      "/" + encodeURIComponent(f.code) +
      "?frequency=" + encodeURIComponent(f.freq) +
      "&line_kind=" + f.kind +
      "&ref_start=" + encodeURIComponent(rs[0]) +
      "&ref_end=" + encodeURIComponent(rs[1]);
    setResult("对比中…", "#868993");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(renderResult)
      .catch(function (e) { setResult("请求异常：" + e, "#e57373"); });
  }

  function renderResult(res) {
    if (!res || !res.ok) {
      setResult("✗ " + ((res && res.error) || "对比失败"), "#e57373");
      return;
    }
    var beichi = res.is_beichi;
    var color = beichi ? "#ef5350" : "#26a69a";
    var ratio = (res.macd_area_ratio == null) ? "—" : Number(res.macd_area_ratio).toFixed(2);
    var html =
      '<div style="font-size:13px;font-weight:bold;color:' + color + ';margin-bottom:4px;">' +
      (beichi ? "● 背驰" : "○ 未背驰") + '　' + (res.verdict || "") + '</div>' +
      '<div>方向：' + res.direction + '　强度分：<b>' + res.strength_score + '</b>/100</div>' +
      '<div>MACD 面积比（当前/参考）：' + ratio + '</div>' +
      '<div>创新极值：' + (res.made_new_extreme ? "是" : "否") + '</div>' +
      '<div style="margin-top:4px;color:#868993;">参考段：' + res.ref.start + " → " + res.ref.end + '</div>' +
      '<div style="color:#868993;">当前段：' + res.cur.start + " → " + res.cur.end +
      (res.cur.done ? "" : "（未完成）") + '</div>';
    setResult(html, "#d1d4dc");
  }

  // 页面就绪后挂载（DOM 已加载则立即挂）
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildPanel);
  } else {
    buildPanel();
  }
})();
