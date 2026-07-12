(function (root, factory) {
  "use strict";

  var api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.DecisionSupport = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  var POLL_INTERVAL_MS = 15000;
  var TRACK_ROLES = Object.freeze({
    trend: "trend_continuation",
    reversal: "bottom_reversal",
  });
  var MANUAL_DECISIONS = Object.freeze({
    accepted: true,
    ignored: true,
    executed_externally: true,
  });
  var FINGERPRINT_RE = /^sha256:[0-9a-f]{64}$/;
  var A_SHARE_CODE_RE = /^(?:SH|SZ)\.\d{6}$/;
  var PAPER_GATE_REASON_LABELS = Object.freeze({
    trusted_bar_store_degraded: "可信K线存储已降级",
    paper_runtime_unhealthy: "模拟盘运行周期异常",
    paper_scan_not_complete: "本周期扫描尚未完整完成",
    paper_exit_coverage_unavailable: "本周期离场覆盖不可用",
    paper_exit_coverage_incomplete: "本周期离场覆盖不完整",
    paper_exit_coverage_stale: "离场覆盖不是当前K线周期",
    paper_exit_coverage_failure: "至少一笔持仓离场分析失败",
    paper_exit_cycle_failure: "离场分析周期失败",
    insufficient_paper_trading_days: "完整纸面观察交易日不足",
    insufficient_paper_executable_events: "实际成交的独立买入事件不足",
  });

  function resolveSafeHtml(explicit) {
    if (explicit && typeof explicit.escapeText === "function") {
      return explicit;
    }
    if (root.SafeHtml && typeof root.SafeHtml.escapeText === "function") {
      return root.SafeHtml;
    }
    if (typeof require === "function") {
      return require("./safe_html.js");
    }
    throw new Error("SafeHtml is required before DecisionSupport");
  }

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  }

  function primitive(value) {
    if (value == null || value === "") return "—";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "string") return value;
    return "—";
  }

  function escapedList(safeHtml, values) {
    if (!Array.isArray(values) || values.length === 0) return "";
    return (
      '<ul class="ds-fact-list">' +
      values
        .map(function (value) {
          return "<li>" + safeHtml.escapeText(primitive(value)) + "</li>";
        })
        .join("") +
      "</ul>"
    );
  }

  function factRows(safeHtml, rows) {
    return rows
      .filter(function (row) {
        return row[1] !== undefined && row[1] !== null && row[1] !== "";
      })
      .map(function (row) {
        return (
          '<div class="ds-fact-row"><dt>' +
          safeHtml.escapeText(row[0]) +
          "</dt><dd>" +
          safeHtml.escapeText(primitive(row[1])) +
          "</dd></div>"
        );
      })
      .join("");
  }

  function forTrack(payload, track) {
    var expected = TRACK_ROLES[track];
    var source = asObject(payload)[track];
    if (!expected || !Array.isArray(source)) return [];
    return source.filter(function (item) {
      return asObject(item).strategy_track === expected;
    });
  }

  function renderCandidateWith(safeHtml, candidate) {
    var item = asObject(candidate);
    var reasons = Array.isArray(item.reason_codes)
      ? item.reason_codes.join(" · ")
      : item.reason || item.summary || "";
    return (
      '<button type="button" class="ds-candidate-button"' +
      ' data-event-id="' + safeHtml.escapeText(primitive(item.event_id)) + '"' +
      ' data-market="' + safeHtml.escapeText(primitive(item.market)) + '"' +
      ' data-code="' + safeHtml.escapeText(primitive(item.code)) + '">' +
      '<span class="ds-candidate-code">' +
      safeHtml.escapeText(primitive(item.code)) +
      "</span>" +
      '<span class="ds-candidate-name">' +
      safeHtml.escapeText(primitive(item.name)) +
      "</span>" +
      '<span class="ds-candidate-reason">' +
      safeHtml.escapeText(primitive(reasons)) +
      "</span>" +
      "</button>"
    );
  }

  function renderPlanWith(safeHtml, event) {
    var value = asObject(event);
    var plan = asObject(value.plan);
    var rows = [
      ["标的", value.code],
      ["名称", value.name],
      ["策略轨道", value.strategy_track],
      ["事件状态", value.state],
      ["方向", plan.direction !== undefined ? plan.direction : value.direction],
      ["触发条件", plan.trigger !== undefined ? plan.trigger : value.trigger],
      ["入场参考", plan.entry_price !== undefined ? plan.entry_price : value.entry_price],
      ["止损参考", plan.stop_price !== undefined ? plan.stop_price : value.stop_price],
      ["目标参考", plan.target_price !== undefined ? plan.target_price : value.target_price],
      ["风险比例", plan.risk_fraction !== undefined ? plan.risk_fraction : value.risk_fraction],
      ["建议仓位", plan.position_size !== undefined ? plan.position_size : value.position_size],
      ["观测时间", value.observed_at],
      ["数据新鲜度", value.freshness],
    ];
    var body = factRows(safeHtml, rows);
    var content = body
      ? '<dl class="ds-facts">' + body + "</dl>"
      : '<p class="ds-empty">暂无可核验交易计划。</p>';
    if (typeof value.event_id === "string" && value.event_id) {
      content +=
        '<div class="ds-actions" aria-label="人工决策操作">' +
        '<button type="button" class="ds-action-button" data-ds-review>请求复核</button>';
      if (
        typeof value.event_data_fingerprint === "string" &&
        FINGERPRINT_RE.test(value.event_data_fingerprint)
      ) {
        content +=
          '<button type="button" class="ds-action-button" data-ds-decision="accepted">接受候选</button>' +
          '<button type="button" class="ds-action-button" data-ds-decision="ignored">忽略候选</button>' +
          '<button type="button" class="ds-action-button" data-ds-decision="executed_externally">已在外部执行</button>';
      }
      content += "</div>";
    }
    return content;
  }

  function renderEvidenceWith(safeHtml, evidence) {
    var value = asObject(evidence);
    var parts = [];
    var headingRows = factRows(safeHtml, [
      ["事件", value.event_id],
      ["可复核", value.reviewable],
      ["复核状态", value.status],
    ]);
    if (headingRows) parts.push('<dl class="ds-facts">' + headingRows + "</dl>");
    if (Array.isArray(value.blockers) && value.blockers.length) {
      parts.push('<h4 class="ds-subtitle">阻断原因</h4>' + escapedList(safeHtml, value.blockers));
    }

    var markdownValues = [value.review_markdown, value.summary_markdown, value.markdown];
    var review = asObject(value.review);
    if (review.markdown) markdownValues.push(review.markdown);
    markdownValues.forEach(function (markdown) {
      if (typeof markdown !== "string" || !markdown) return;
      try {
        parts.push('<div class="ds-markdown">' + safeHtml.renderMarkdown(markdown) + "</div>");
      } catch (_) {
        parts.push('<p class="ds-empty">证据 Markdown 暂不可用。</p>');
      }
    });

    if (Array.isArray(value.supporting) && value.supporting.length) {
      var supporting = value.supporting.map(function (raw) {
        var item = asObject(raw);
        return (
          '<li><strong>' +
          safeHtml.escapeText(primitive(item.lesson || item.source || item.title)) +
          "</strong> " +
          safeHtml.escapeText(primitive(item.text || item.excerpt || item.citation)) +
          "</li>"
        );
      });
      parts.push(
        '<h4 class="ds-subtitle">原文依据</h4><ul class="ds-evidence-list">' +
          supporting.join("") +
          "</ul>"
      );
    }
    return parts.length ? parts.join("") : '<p class="ds-empty">暂无原文证据。</p>';
  }

  function manualCheckRequirements(record) {
    var value = asObject(record);
    if (
      typeof value.event_id !== "string" ||
      !value.event_id ||
      typeof value.context_fingerprint !== "string" ||
      !FINGERPRINT_RE.test(value.context_fingerprint) ||
      !Array.isArray(value.required_checks) ||
      value.required_checks.length === 0 ||
      value.required_checks.length > 64
    ) {
      return null;
    }
    var seen = Object.create(null);
    var checks = [];
    for (var index = 0; index < value.required_checks.length; index += 1) {
      var item = asObject(value.required_checks[index]);
      var id = item.manual_check_id;
      var evidenceIds = item.evidence_ids;
      if (
        typeof id !== "string" ||
        !id ||
        id.length > 191 ||
        seen[id] ||
        typeof item.prompt !== "string" ||
        !item.prompt ||
        !Array.isArray(evidenceIds) ||
        evidenceIds.length === 0 ||
        evidenceIds.some(function (evidenceId) {
          return typeof evidenceId !== "string" || !evidenceId;
        }) ||
        new Set(evidenceIds).size !== evidenceIds.length
      ) {
        return null;
      }
      seen[id] = true;
      checks.push({
        evidence_ids: evidenceIds.slice(),
        manual_check_id: id,
        prompt: item.prompt,
      });
    }
    return checks;
  }

  function renderManualChecksWith(safeHtml, record) {
    var value = asObject(record);
    var checks = manualCheckRequirements(value);
    if (!checks) {
      return '<p class="ds-empty">人工图形核验记录不可用，当前保持阻断。</p>';
    }
    var items = checks.map(function (check) {
      var evidence = check.evidence_ids.map(function (evidenceId) {
        return "<li>" + safeHtml.escapeText(evidenceId) + "</li>";
      }).join("");
      if (value.status === "approved") {
        return (
          '<li class="ds-manual-check-item"><strong>' +
          safeHtml.escapeText(check.prompt) +
          '</strong><ul class="ds-fact-list">' + evidence + "</ul></li>"
        );
      }
      return (
        '<li class="ds-manual-check-item"><label>' +
        '<input type="checkbox" autocomplete="off" data-ds-manual-check="' +
        safeHtml.escapeText(check.manual_check_id) +
        '"> <span>' + safeHtml.escapeText(check.prompt) + "</span></label>" +
        '<div class="ds-manual-evidence"><span>核验依据</span>' +
        '<ul class="ds-fact-list">' + evidence + "</ul></div></li>"
      );
    }).join("");
    if (value.status === "approved") {
      return (
        '<p class="ds-manual-approved">本次人工图形核验已通过。</p>' +
        '<ul class="ds-manual-check-list">' + items + "</ul>"
      );
    }
    if (value.status !== "pending") {
      return '<p class="ds-empty">人工图形核验状态不可用，当前保持阻断。</p>';
    }
    return (
      '<p class="ds-manual-warning">请对照当前图表和列出的原文依据逐项人工确认；系统不会自动勾选。</p>' +
      '<ul class="ds-manual-check-list">' + items + "</ul>" +
      '<button type="button" class="ds-action-button" data-ds-manual-submit>' +
      "提交人工核验</button>"
    );
  }

  function renderRiskWith(safeHtml, risk) {
    var value = asObject(risk);
    var body = factRows(safeHtml, [
      ["风控可用", value.available],
      ["日内亏损锁定", value.daily_loss_locked],
      ["回撤锁定", value.drawdown_locked],
      ["日内亏损", value.daily_loss],
      ["当前回撤", value.drawdown],
      ["纸面验证待完成", value.paper_gate_pending],
      ["晋级状态", value.promotion_state],
      ["数据时间", value.as_of],
    ]);
    var reasons = escapedList(safeHtml, value.promotion_reasons);
    if (!body && !reasons) return '<p class="ds-empty">风控状态不可用。</p>';
    return (
      (body ? '<dl class="ds-facts">' + body + "</dl>" : "") +
      (reasons ? '<h4 class="ds-subtitle">限制原因</h4>' + reasons : "")
    );
  }

  function validPaperSnapshot(raw) {
    var value = asObject(raw);
    return value.mode === "research_paper" &&
      value.read_only === true &&
      value.auto_order_enabled === false &&
      value.live_order_capability === false
      ? value
      : null;
  }

  function renderPaperWith(safeHtml, paper) {
    var bundle = asObject(paper);
    var status = validPaperSnapshot(bundle.status);
    if (!status) {
      return '<p class="ds-empty">研究模拟盘不可用；当前不会执行任何交易。</p>';
    }
    var account = validPaperSnapshot(bundle.account) || {};
    var positions = validPaperSnapshot(bundle.positions) || {};
    var exits = validPaperSnapshot(bundle.exits) || {};
    var gate = asObject(status.paper_observation_gate);
    var barStore = asObject(status.trusted_bar_store);
    var exitCoverage = asObject(status.exit_coverage);
    var exitsAreCurrent = exitCoverage.complete === true &&
      exitCoverage.fresh === true &&
      exitCoverage.failure_count === 0 &&
      exitCoverage.scan_code === "scan_complete" &&
      !exitCoverage.cycle_failure;
    var statusRows = factRows(safeHtml, [
      ["观察交易日", primitive(gate.trading_days) + " / " + primitive(gate.minimum_trading_days)],
      ["可执行事件", primitive(gate.executable_events) + " / " + primitive(gate.minimum_executable_events)],
      ["观察门槛通过", gate.passed],
      ["可信K线降级", barStore.degraded],
      ["离场覆盖为当前周期", exitCoverage.fresh],
      ["离场分析失败数", exitCoverage.failure_count],
      ["合规确认", status.broker_compliance_confirmation],
      ["可用买力", account.available_buying_power],
      ["成本口径权益", account.cost_basis_equity],
    ]);
    var reasons = escapedList(
      safeHtml,
      Array.isArray(gate.reasons)
        ? gate.reasons.map(function (reason) {
          return PAPER_GATE_REASON_LABELS[reason] || reason;
        })
        : gate.reasons
    );
    var positionItems = Array.isArray(positions.items) ? positions.items : [];
    var positionHtml = positionItems.slice(0, 50).map(function (raw) {
      var item = asObject(raw);
      return (
        '<button type="button" class="ds-candidate-button ds-paper-position"' +
        ' data-ds-paper-code="' + safeHtml.escapeText(primitive(item.code)) + '">' +
        '<span class="ds-candidate-code">' +
        safeHtml.escapeText(primitive(item.code)) +
        "</span>" +
        '<span class="ds-candidate-name">' +
        safeHtml.escapeText(primitive(item.shares)) +
        " 股</span>" +
        '<span class="ds-candidate-reason">均价 ' +
        safeHtml.escapeText(primitive(item.average_price)) +
        " · 查看图表</span></button>"
      );
    }).join("");
    var exitItems = Array.isArray(exits.items) ? exits.items : [];
    var exitHtml = (exitsAreCurrent ? exitItems : []).slice(0, 20).map(function (raw) {
      var item = asObject(raw);
      var recommendation = asObject(item.recommendation_payload);
      return (
        "<li>" +
        safeHtml.escapeText(primitive(item.entry_event_id)) +
        " · " +
        safeHtml.escapeText(primitive(recommendation.action)) +
        " · " +
        safeHtml.escapeText(primitive(recommendation.urgency)) +
        "</li>"
      );
    }).join("");
    return (
      '<p class="ds-paper-readonly">研究验证中 · 只读 · 不可作为实盘交易依据 · 无实盘下单能力</p>' +
      '<dl class="ds-facts">' + statusRows + "</dl>" +
      (reasons ? '<h4 class="ds-subtitle">观察门槛</h4>' + reasons : "") +
      '<h4 class="ds-subtitle">持仓（点击仅切换图表）</h4>' +
      (positionHtml || '<p class="ds-empty">暂无纸面持仓。</p>') +
      '<h4 class="ds-subtitle">最近离场分析</h4>' +
      (!exitsAreCurrent
        ? '<p class="ds-empty">当前周期离场分析不可用或不完整；不会沿用历史结果。</p>'
        : exitHtml
        ? '<ul class="ds-fact-list">' + exitHtml + "</ul>"
        : '<p class="ds-empty">暂无离场分析。</p>')
    );
  }

  function createController(options) {
    var config = options || {};
    var safeHtml = resolveSafeHtml(config.safeHtml);
    var documentObject = config.document || root.document || null;
    var ajax = config.ajax || (root.AppRequest && root.AppRequest.ajax);
    var setTimer = config.setTimeout || (root.setTimeout && root.setTimeout.bind(root));
    var clearTimer = config.clearTimeout || (root.clearTimeout && root.clearTimeout.bind(root));
    var AbortControllerType = config.AbortController || root.AbortController;
    var changeChartTicker = config.changeChartTicker || root.change_chart_ticker;
    var now = config.now || Date.now;
    var random = config.random || Math.random;
    var state = {
      activeTrack: "trend",
      candidates: { trend: [], reversal: [] },
      destroyed: false,
      evidence: null,
      initialized: false,
      manualChecks: null,
      paper: { status: null, account: null, positions: null, exits: null },
      risk: null,
      selectedCandidate: null,
      selectedEvent: null,
      selectedEventId: null,
      stale: false,
    };
    var sequences = Object.create(null);
    var inFlight = Object.create(null);
    var pendingWrites = Object.create(null);
    var pollTimer = null;
    var idempotencySequence = 0;
    var visibilityHandler = null;
    var unloadHandler = null;

    function element(id) {
      return documentObject && typeof documentObject.getElementById === "function"
        ? documentObject.getElementById(id)
        : null;
    }

    function setHtml(id, html) {
      var target = element(id);
      if (target) target.innerHTML = html;
    }

    function setStatus(text) {
      var target = element("ds_status");
      if (target) target.textContent = String(text);
    }

    function isVisible() {
      return !documentObject || documentObject.hidden !== true;
    }

    function abortRequest(key) {
      var active = inFlight[key];
      if (!active) return;
      if (active.controller && typeof active.controller.abort === "function") {
        active.controller.abort();
      }
      if (active.request && typeof active.request.abort === "function") {
        active.request.abort();
      }
      delete inFlight[key];
    }

    function issueRequest(key, settings, callbacks) {
      if (typeof ajax !== "function") {
        throw new Error("AppRequest.ajax is required before DecisionSupport requests");
      }
      var sequence = (sequences[key] || 0) + 1;
      sequences[key] = sequence;
      abortRequest(key);
      var controller = AbortControllerType ? new AbortControllerType() : null;
      var request = null;
      var handlers = callbacks || {};
      var requestSettings = Object.assign({}, settings, {
        success: function (payload) {
          if (
            sequences[key] !== sequence ||
            (controller && controller.signal && controller.signal.aborted)
          ) {
            return;
          }
          if (typeof handlers.success === "function") handlers.success(payload);
        },
        error: function () {
          if (
            sequences[key] !== sequence ||
            (controller && controller.signal && controller.signal.aborted)
          ) {
            return;
          }
          if (typeof handlers.error === "function") handlers.error();
        },
        complete: function () {
          if (sequences[key] !== sequence) return;
          delete inFlight[key];
          if (typeof handlers.complete === "function") handlers.complete();
        },
      });
      if (controller && controller.signal) requestSettings.signal = controller.signal;
      request = ajax(requestSettings);
      inFlight[key] = { controller: controller, request: request, sequence: sequence };
      return request;
    }

    function responseData(payload) {
      var response = asObject(payload);
      return response.ok === true ? asObject(response.data) : null;
    }

    function renderCandidates() {
      var items = state.candidates[state.activeTrack] || [];
      var html = items.map(function (item) {
        return renderCandidateWith(safeHtml, item);
      }).join("");
      setHtml(
        "ds_candidate_list",
        html || '<p class="ds-empty">当前轨道暂无候选。</p>'
      );
    }

    function refreshCandidates() {
      return issueRequest(
        "candidates",
        {
          dataType: "json",
          method: "GET",
          timeout: 10000,
          url: "/decision-support/candidates?limit=25",
        },
        {
          success: function (payload) {
            var data = responseData(payload);
            if (!data) {
              setStatus("候选数据不可用，保留上次结果");
              return;
            }
            state.candidates = {
              trend: forTrack(data, "trend"),
              reversal: forTrack(data, "reversal"),
            };
            state.stale = data.stale === true;
            renderCandidates();
            setStatus(state.stale ? "候选数据已过期" : "候选数据已更新");
          },
          error: function () {
            setStatus("候选请求失败，保留上次结果");
          },
        }
      );
    }

    function refreshRisk() {
      function markUnavailable(message) {
        state.risk = null;
        setHtml(
          "ds_risk_view",
          '<p class="ds-empty">风控状态加载失败，当前按不可用处理。</p>'
        );
        setStatus(message || "风控状态不可用");
      }
      return issueRequest(
        "risk",
        {
          dataType: "json",
          method: "GET",
          timeout: 10000,
          url: "/decision-support/risk-status",
        },
        {
          success: function (payload) {
            var data = responseData(payload);
            if (!data) {
              markUnavailable("风控状态不可用");
              return;
            }
            state.risk = data;
            setHtml("ds_risk_view", renderRiskWith(safeHtml, data));
          },
          error: function () {
            markUnavailable("风控请求失败，当前按不可用处理");
          },
        }
      );
    }

    function renderPaper() {
      setHtml("ds_paper_view", renderPaperWith(safeHtml, state.paper));
    }

    function refreshPaper() {
      var endpoints = {
        status: "/decision-support/paper/status",
        account: "/decision-support/paper/account",
        positions: "/decision-support/paper/positions",
        exits: "/decision-support/paper/exits",
      };
      return Object.keys(endpoints).map(function (part) {
        return issueRequest(
          "paper-" + part,
          {
            dataType: "json",
            method: "GET",
            timeout: 10000,
            url: endpoints[part],
          },
          {
            success: function (payload) {
              state.paper[part] = validPaperSnapshot(responseData(payload));
              renderPaper();
            },
            error: function () {
              state.paper[part] = null;
              renderPaper();
              if (part === "status") {
                setStatus("研究模拟盘不可用；不会执行任何交易");
              }
            },
          }
        );
      });
    }

    function schedulePoll() {
      if (!state.initialized || state.destroyed || !isVisible() || !setTimer) return;
      if (pollTimer !== null && clearTimer) clearTimer(pollTimer);
      pollTimer = setTimer(function () {
        pollTimer = null;
        if (!isVisible() || state.destroyed) return;
        refreshCandidates();
        refreshRisk();
        schedulePoll();
      }, POLL_INTERVAL_MS);
    }

    function startPolling() {
      if (state.destroyed || !isVisible()) return;
      refreshCandidates();
      refreshRisk();
      schedulePoll();
    }

    function stopPolling() {
      if (pollTimer !== null && clearTimer) clearTimer(pollTimer);
      pollTimer = null;
      abortRequest("candidates");
      abortRequest("risk");
      ["status", "account", "positions", "exits"].forEach(function (part) {
        abortRequest("paper-" + part);
      });
    }

    function inferMarket(candidate) {
      if (candidate.market && candidate.market !== "—") return candidate.market;
      var code = String(candidate.code || "").toUpperCase();
      if (code.indexOf("SH.") === 0 || code.indexOf("SZ.") === 0) return "a";
      if (root.Utils && typeof root.Utils.get_market === "function") {
        return root.Utils.get_market();
      }
      return "a";
    }

    function selectPaperPosition(code) {
      if (typeof code !== "string" || !A_SHARE_CODE_RE.test(code)) {
        return false;
      }
      if (typeof changeChartTicker !== "function") return false;
      changeChartTicker("a", code);
      return true;
    }

    function selectEvent(candidateOrId) {
      var candidate = typeof candidateOrId === "string"
        ? { event_id: candidateOrId }
        : asObject(candidateOrId);
      var eventId = candidate.event_id;
      if (typeof eventId !== "string" || !eventId) {
        throw new Error("event_id is required");
      }
      state.selectedCandidate = candidate;
      state.selectedEventId = eventId;
      state.selectedEvent = null;
      state.evidence = null;
      state.manualChecks = null;
      setHtml(
        "ds_manual_check_view",
        '<p class="ds-empty">人工图形核验记录加载中。</p>'
      );
      if (
        candidate.code &&
        typeof changeChartTicker === "function"
      ) {
        changeChartTicker(inferMarket(candidate), candidate.code);
      }
      var encoded = encodeURIComponent(eventId);
      var eventRequest = issueRequest(
        "event",
        {
          dataType: "json",
          method: "GET",
          timeout: 10000,
          url: "/decision-support/events/" + encoded,
        },
        {
          success: function (payload) {
            var data = responseData(payload);
            if (!data || state.selectedEventId !== eventId) return;
            state.selectedEvent = data;
            setHtml("ds_plan_view", renderPlanWith(safeHtml, data));
          },
          error: function () {
            setHtml("ds_plan_view", '<p class="ds-empty">交易计划加载失败。</p>');
          },
        }
      );
      issueRequest(
        "evidence",
        {
          dataType: "json",
          method: "GET",
          timeout: 10000,
          url: "/decision-support/events/" + encoded + "/evidence",
        },
        {
          success: function (payload) {
            var data = responseData(payload);
            if (!data || state.selectedEventId !== eventId) return;
            state.evidence = data;
            setHtml("ds_evidence_view", renderEvidenceWith(safeHtml, data));
          },
          error: function () {
            setHtml("ds_evidence_view", '<p class="ds-empty">原文证据加载失败。</p>');
          },
        }
      );
      issueRequest(
        "manual-checks",
        {
          dataType: "json",
          method: "GET",
          timeout: 10000,
          url: "/decision-support/events/" + encoded + "/manual-checks",
        },
        {
          success: function (payload) {
            var data = responseData(payload);
            if (!data || state.selectedEventId !== eventId) return;
            state.manualChecks = data;
            setHtml(
              "ds_manual_check_view",
              renderManualChecksWith(safeHtml, data)
            );
          },
          error: function () {
            if (state.selectedEventId !== eventId) return;
            state.manualChecks = null;
            setHtml(
              "ds_manual_check_view",
              '<p class="ds-empty">当前事件没有可提交的人工图形核验记录。</p>'
            );
          },
        }
      );
      return eventRequest;
    }

    function nextIdempotencyKey() {
      idempotencySequence += 1;
      var randomPart = Math.floor(random() * 0x100000000)
        .toString(16)
        .padStart(8, "0");
      return (
        "ds-" +
        Number(now()).toString(36) +
        "-" +
        idempotencySequence +
        "-" +
        randomPart
      );
    }

    function setBusy(button, busy) {
      if (!button) return;
      button.disabled = busy;
      if (typeof button.setAttribute === "function" && busy) {
        button.setAttribute("aria-busy", "true");
      }
      if (typeof button.removeAttribute === "function" && !busy) {
        button.removeAttribute("aria-busy");
      }
    }

    function submitUserDecision(eventId, action, writeOptions) {
      var opts = writeOptions || {};
      if (!MANUAL_DECISIONS[action]) {
        throw new Error("unsupported manual decision");
      }
      var fingerprint = opts.event_data_fingerprint ||
        asObject(state.selectedEvent).event_data_fingerprint ||
        asObject(state.selectedCandidate).event_data_fingerprint;
      if (typeof fingerprint !== "string" || !FINGERPRINT_RE.test(fingerprint)) {
        throw new Error("valid event_data_fingerprint is required");
      }
      if (opts.note !== undefined && (typeof opts.note !== "string" || opts.note.length > 1000)) {
        throw new Error("manual decision note must be at most 1000 characters");
      }
      var pendingKey = "decision:" + eventId + ":" + action;
      if (pendingWrites[pendingKey]) return pendingWrites[pendingKey];
      var payload = {
        action: action,
        event_data_fingerprint: fingerprint,
        idempotency_key: opts.idempotency_key || nextIdempotencyKey(),
      };
      if (opts.note !== undefined) payload.note = opts.note;
      setBusy(opts.button, true);
      var request = issueRequest(
        pendingKey,
        {
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify(payload),
          dataType: "json",
          method: "POST",
          timeout: 10000,
          type: "POST",
          url: "/decision-support/events/" + encodeURIComponent(eventId) + "/user-decision",
        },
        {
          success: function () {
            setStatus("人工决策已记录");
          },
          error: function () {
            setStatus("人工决策记录失败");
          },
          complete: function () {
            delete pendingWrites[pendingKey];
            setBusy(opts.button, false);
          },
        }
      );
      pendingWrites[pendingKey] = request;
      return request;
    }

    function requestReview(eventId, reviewOptions) {
      var opts = reviewOptions || {};
      var force = opts.force === true;
      var pendingKey = "review:" + eventId;
      if (pendingWrites[pendingKey]) return pendingWrites[pendingKey];
      setBusy(opts.button, true);
      var request = issueRequest(
        pendingKey,
        {
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify({ force: force }),
          dataType: "json",
          method: "POST",
          timeout: 10000,
          type: "POST",
          url: "/decision-support/events/" + encodeURIComponent(eventId) + "/review",
        },
        {
          success: function () {
            setStatus("复核请求已提交");
          },
          error: function () {
            setStatus("复核请求失败");
          },
          complete: function () {
            delete pendingWrites[pendingKey];
            setBusy(opts.button, false);
          },
        }
      );
      pendingWrites[pendingKey] = request;
      return request;
    }

    function submitManualChecks(eventId, submitOptions) {
      var opts = submitOptions || {};
      var record = asObject(state.manualChecks);
      var checks = manualCheckRequirements(record);
      if (
        typeof eventId !== "string" ||
        !eventId ||
        record.event_id !== eventId ||
        record.status !== "pending" ||
        !checks
      ) {
        throw new Error("valid pending manual-check record is required");
      }
      var view = element("ds_manual_check_view");
      var operatorId = opts.operatorId;
      if (
        operatorId === undefined &&
        view &&
        typeof view.getAttribute === "function"
      ) {
        operatorId = view.getAttribute("data-operator-id");
      }
      if (
        typeof operatorId !== "string" ||
        !operatorId ||
        operatorId.length > 191
      ) {
        throw new Error("authenticated operator_id is required");
      }
      var checkedIds = opts.checkedIds;
      if (
        checkedIds === undefined &&
        view &&
        typeof view.querySelectorAll === "function"
      ) {
        checkedIds = Array.prototype.filter.call(
          view.querySelectorAll("[data-ds-manual-check]"),
          function (checkbox) { return checkbox.checked === true; }
        ).map(function (checkbox) {
          return checkbox.getAttribute("data-ds-manual-check");
        });
      }
      if (!Array.isArray(checkedIds)) checkedIds = [];
      var selected = Object.create(null);
      var invalidSelection = checkedIds.some(function (id) {
        if (typeof id !== "string" || !id || selected[id]) return true;
        selected[id] = true;
        return false;
      });
      if (
        invalidSelection ||
        checkedIds.length !== checks.length ||
        checks.some(function (check) {
          return selected[check.manual_check_id] !== true;
        })
      ) {
        throw new Error("all manual checks must be explicitly selected");
      }
      var recordedAt = opts.recordedAt;
      if (recordedAt === undefined) {
        recordedAt = new Date(Number(now())).toISOString();
      }
      if (typeof recordedAt !== "string" || !recordedAt) {
        throw new Error("valid manual-check recorded_at is required");
      }
      var payload = {
        manual_checks: checks.map(function (check) {
          return {
            manual_check_id: check.manual_check_id,
            value: true,
            operator_id: operatorId,
            recorded_at: recordedAt,
            event_id: eventId,
            context_fingerprint: record.context_fingerprint,
            evidence_ids: check.evidence_ids.slice(),
          };
        }),
      };
      var pendingKey = "manual-checks:" + eventId;
      if (pendingWrites[pendingKey]) return pendingWrites[pendingKey];
      setBusy(opts.button, true);
      var request = issueRequest(
        pendingKey,
        {
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify(payload),
          dataType: "json",
          method: "POST",
          timeout: 10000,
          type: "POST",
          url: "/decision-support/events/" +
            encodeURIComponent(eventId) +
            "/manual-checks",
        },
        {
          success: function (response) {
            var data = responseData(response);
            var updated = asObject(data).record;
            if (updated && asObject(updated).event_id === eventId) {
              state.manualChecks = updated;
              setHtml(
                "ds_manual_check_view",
                renderManualChecksWith(safeHtml, updated)
              );
            }
            setStatus(
              asObject(data).accepted === true
                ? "人工图形核验已提交并通过。"
                : "人工图形核验未通过，事件保持阻断。"
            );
          },
          error: function () {
            setStatus("人工图形核验提交失败，事件保持阻断。");
          },
          complete: function () {
            delete pendingWrites[pendingKey];
            setBusy(opts.button, false);
          },
        }
      );
      pendingWrites[pendingKey] = request;
      return request;
    }

    function activateTrack(track) {
      if (!TRACK_ROLES[track]) return;
      state.activeTrack = track;
      renderCandidates();
      var toggle = element("ds_track_toggle");
      if (!toggle || typeof toggle.querySelectorAll !== "function") return;
      Array.prototype.forEach.call(
        toggle.querySelectorAll("[data-ds-track]"),
        function (button) {
          button.setAttribute("aria-pressed", String(button.getAttribute("data-ds-track") === track));
        }
      );
    }

    function activateTab(button) {
      if (!button || typeof button.getAttribute !== "function") return;
      var panelId = button.getAttribute("aria-controls");
      [
        "ds_candidate_tab",
        "ds_plan_tab",
        "ds_evidence_tab",
        "ds_risk_tab",
        "ds_paper_tab",
      ].forEach(
        function (id) {
          var tab = element(id);
          if (!tab) return;
          var selected = tab === button;
          tab.setAttribute("aria-selected", String(selected));
          tab.tabIndex = selected ? 0 : -1;
          var target = element(tab.getAttribute("aria-controls"));
          if (target) target.hidden = !selected;
        }
      );
      var panel = element(panelId);
      if (panel && typeof panel.focus === "function") panel.focus();
      if (panelId === "ds_paper_view") refreshPaper();
    }

    function bindDom() {
      var toggle = element("ds_track_toggle");
      if (toggle && typeof toggle.addEventListener === "function") {
        toggle.addEventListener("click", function (event) {
          var button = event.target && event.target.closest
            ? event.target.closest("[data-ds-track]")
            : event.target;
          if (button && typeof button.getAttribute === "function") {
            activateTrack(button.getAttribute("data-ds-track"));
          }
        });
      }
      var tabList = documentObject && documentObject.querySelector
        ? documentObject.querySelector(".ds-tab-list")
        : null;
      if (tabList && typeof tabList.addEventListener === "function") {
        tabList.addEventListener("click", function (event) {
          var button = event.target && event.target.closest
            ? event.target.closest('[role="tab"]')
            : event.target;
          activateTab(button);
        });
        tabList.addEventListener("keydown", function (event) {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          var tabs = Array.prototype.slice.call(tabList.querySelectorAll('[role="tab"]'));
          var index = tabs.indexOf(event.target);
          if (index < 0 || !tabs.length) return;
          var step = event.key === "ArrowRight" ? 1 : -1;
          activateTab(tabs[(index + step + tabs.length) % tabs.length]);
          event.preventDefault();
        });
      }
      var candidateList = element("ds_candidate_list");
      if (candidateList && typeof candidateList.addEventListener === "function") {
        candidateList.addEventListener("click", function (event) {
          var button = event.target && event.target.closest
            ? event.target.closest(".ds-candidate-button")
            : null;
          if (!button || !candidateList.contains(button)) return;
          var eventId = button.getAttribute("data-event-id");
          var candidates = state.candidates[state.activeTrack] || [];
          var selected = candidates.find(function (item) {
            return item.event_id === eventId;
          });
          if (selected) selectEvent(selected);
        });
      }
      var paperView = element("ds_paper_view");
      if (paperView && typeof paperView.addEventListener === "function") {
        paperView.addEventListener("click", function (event) {
          var button = event.target && event.target.closest
            ? event.target.closest("[data-ds-paper-code]")
            : null;
          if (!button || !paperView.contains(button)) return;
          var code = button.getAttribute("data-ds-paper-code");
          selectPaperPosition(code);
        });
      }
      var planView = element("ds_plan_view");
      if (planView && typeof planView.addEventListener === "function") {
        planView.addEventListener("click", function (event) {
          var target = event.target && event.target.closest
            ? event.target.closest("[data-ds-review], [data-ds-decision]")
            : null;
          if (!target || !planView.contains(target) || target.disabled) return;
          var eventId = state.selectedEventId;
          if (!eventId) return;
          try {
            if (target.hasAttribute("data-ds-review")) {
              requestReview(eventId, { button: target, force: false });
              return;
            }
            submitUserDecision(
              eventId,
              target.getAttribute("data-ds-decision"),
              { button: target }
            );
          } catch (_) {
            setStatus("人工操作参数不完整");
          }
        });
      }
      var manualCheckView = element("ds_manual_check_view");
      if (
        manualCheckView &&
        typeof manualCheckView.addEventListener === "function"
      ) {
        manualCheckView.addEventListener("click", function (event) {
          var target = event.target && event.target.closest
            ? event.target.closest("[data-ds-manual-submit]")
            : null;
          if (
            !target ||
            !manualCheckView.contains(target) ||
            target.disabled ||
            !state.selectedEventId
          ) {
            return;
          }
          try {
            submitManualChecks(state.selectedEventId, { button: target });
          } catch (_) {
            setStatus("请先逐项完成人工图形核验，当前事件保持阻断。");
          }
        });
      }
    }

    function init() {
      if (state.initialized || state.destroyed) return controller;
      state.initialized = true;
      bindDom();
      if (documentObject && typeof documentObject.addEventListener === "function") {
        visibilityHandler = function () {
          if (isVisible()) startPolling();
          else stopPolling();
        };
        documentObject.addEventListener("visibilitychange", visibilityHandler);
      }
      if (root.addEventListener) {
        unloadHandler = function () { destroy(); };
        root.addEventListener("beforeunload", unloadHandler);
      }
      startPolling();
      return controller;
    }

    function destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      stopPolling();
      Object.keys(inFlight).forEach(abortRequest);
      if (documentObject && visibilityHandler && documentObject.removeEventListener) {
        documentObject.removeEventListener("visibilitychange", visibilityHandler);
      }
      if (root.removeEventListener && unloadHandler) {
        root.removeEventListener("beforeunload", unloadHandler);
      }
    }

    function getState() {
      return {
        activeTrack: state.activeTrack,
        candidates: {
          trend: state.candidates.trend.slice(),
          reversal: state.candidates.reversal.slice(),
        },
        evidence: state.evidence,
        manualChecks: state.manualChecks,
        paper: {
          status: state.paper.status,
          account: state.paper.account,
          positions: state.paper.positions,
          exits: state.paper.exits,
        },
        risk: state.risk,
        selectedEvent: state.selectedEvent,
        selectedEventId: state.selectedEventId,
        stale: state.stale,
      };
    }

    var controller = {
      activateTrack: activateTrack,
      destroy: destroy,
      getState: getState,
      init: init,
      refreshCandidates: refreshCandidates,
      refreshPaper: refreshPaper,
      refreshRisk: refreshRisk,
      renderCandidate: function (candidate) { return renderCandidateWith(safeHtml, candidate); },
      renderEvidence: function (evidence) { return renderEvidenceWith(safeHtml, evidence); },
      renderManualChecks: function (record) {
        return renderManualChecksWith(safeHtml, record);
      },
      renderPlan: function (event) { return renderPlanWith(safeHtml, event); },
      renderPaper: function (paper) { return renderPaperWith(safeHtml, paper); },
      renderRisk: function (risk) { return renderRiskWith(safeHtml, risk); },
      requestReview: requestReview,
      selectEvent: selectEvent,
      selectPaperPosition: selectPaperPosition,
      startPolling: startPolling,
      stopPolling: stopPolling,
      submitManualChecks: submitManualChecks,
      submitUserDecision: submitUserDecision,
    };
    return controller;
  }

  var defaultController = null;
  function defaults() {
    if (!defaultController) defaultController = createController();
    return defaultController;
  }

  return {
    createController: createController,
    forTrack: forTrack,
    init: function () { return defaults().init(); },
    refreshCandidates: function () { return defaults().refreshCandidates(); },
    refreshPaper: function () { return defaults().refreshPaper(); },
    renderCandidate: function (candidate) {
      return renderCandidateWith(resolveSafeHtml(), candidate);
    },
    renderEvidence: function (evidence) {
      return renderEvidenceWith(resolveSafeHtml(), evidence);
    },
    renderManualChecks: function (record) {
      return renderManualChecksWith(resolveSafeHtml(), record);
    },
    renderPlan: function (event) {
      return renderPlanWith(resolveSafeHtml(), event);
    },
    renderPaper: function (paper) {
      return renderPaperWith(resolveSafeHtml(), paper);
    },
    renderRisk: function (risk) {
      return renderRiskWith(resolveSafeHtml(), risk);
    },
    requestReview: function (eventId, options) {
      return defaults().requestReview(eventId, options);
    },
    selectEvent: function (candidateOrId) {
      return defaults().selectEvent(candidateOrId);
    },
    submitManualChecks: function (eventId, options) {
      return defaults().submitManualChecks(eventId, options);
    },
    submitUserDecision: function (eventId, action, options) {
      return defaults().submitUserDecision(eventId, action, options);
    },
  };
});
