import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  deepWarmupDiagnosticPresentation,
  deferredEvidenceDisclosure,
  mappedStateLabel,
  paperReasonLabel,
  realtimeNotificationCandidate,
  reviewAlertVisibleForSource,
} = require("../../web/chanlun_chart/cl_app/static/js/human_review_screening.js");

test("current review sources reject archived alert types", () => {
  assert.equal(reviewAlertVisibleForSource("POSSIBLE_5M_TRADE_BUY", "latest"), true);
  assert.equal(reviewAlertVisibleForSource("REALTIME_SELL_POINT", "live"), true);
  assert.equal(reviewAlertVisibleForSource("POSSIBLE_30M_BUY", "latest"), false);
  assert.equal(reviewAlertVisibleForSource("POSSIBLE_5M_TACTICAL_SELL", "live"), false);
  assert.equal(reviewAlertVisibleForSource("POSSIBLE_30M_BUY", "historical"), true);
  assert.equal(reviewAlertVisibleForSource("POSSIBLE_5M_TACTICAL_SELL", "forward"), true);
});

test("compact candidates disclose deferred evidence without pretending it is missing", () => {
  const result = deferredEvidenceDisclosure(
    { evidence_detail_available: true },
    "日线高级别证据",
    `sha256:${"1".repeat(64)}`,
  );

  assert.equal(result.tag, "日线高级别证据按需加载");
  assert.match(result.lines[0], /轻量摘要/);
  assert.deepEqual(result.factIds, [`sha256:${"1".repeat(64)}`]);
});

test("unselected candidates do not receive a deep-warmup tag", () => {
  const result = deepWarmupDiagnosticPresentation(null);

  assert.equal(result.selected, false);
  assert.equal(result.tag, null);
  assert.equal(result.tone, "neutral");
});

test("complete candidate diagnostics are presentation-only summaries", () => {
  const result = deepWarmupDiagnosticPresentation({
    selected: true,
    rank: 2,
    status: "AVAILABLE",
    frequencies: [
      {
        frequency: "30m",
        status: "STABLE_ALL_PREFIXES",
        prefix_bar_counts: [480, 960, 1440],
        available_bar_count: 1600,
        reason_codes: [],
      },
    ],
  });

  assert.equal(result.selected, true);
  assert.equal(result.tone, "ok");
  assert.equal(result.tag, "深暖机已审计");
  assert.match(result.headline, /第 2 位/);
  assert.match(result.lines[0], /30m/);
  assert.match(result.lines[0], /480\/960\/1440/);
});

test("non-monotonic evidence is visibly amber without becoming a gate", () => {
  const result = deepWarmupDiagnosticPresentation({
    selected: true,
    rank: 1,
    status: "NON_MONOTONIC",
    frequencies: [
      {
        frequency: "1m",
        status: "NON_MONOTONIC",
        prefix_bar_counts: [1440, 2880],
        available_bar_count: 3000,
        reason_codes: ["PREFIX_SIGNATURE_DIVERGED"],
      },
    ],
  });

  assert.equal(result.tone, "warning");
  assert.equal(result.tag, "深暖机非单调");
  assert.match(result.lines[0], /多前缀结构签名不一致/);
  assert.doesNotMatch(result.lines[0], /PREFIX_SIGNATURE_DIVERGED/);
});

test("realtime notification projection uses canonical review enums", () => {
  const candidate = realtimeNotificationCandidate(
    {
      schema: "chanlun-realtime-review-notification",
      notification_id: `sha256:${"1".repeat(64)}`,
      side: "buy",
      code: "SZ.000001",
      point_type: "2buy",
      signal_time: "2026-08-20T10:00:00+08:00",
      chart_urls: { "5m": "/chart/5m" },
      review_required: true,
      automated_action_authorized: false,
      real_order_transport_enabled: false,
      live_status: "LIVE_DISABLED",
      delivery_status: "delivered",
    },
    new Date("2026-08-20T10:01:00+08:00"),
  );

  assert.equal(candidate.confidence, "MEDIUM");
  assert.equal(candidate.entry_boundary_attestation, "NOT_AVAILABLE");
  assert.equal(candidate.market_risk_gate, "UNRESOLVED");
  assert.equal(candidate.sector_risk_gate, "UNRESOLVED");
  assert.equal(candidate.symbol_risk_gate, "UNRESOLVED");
});

test("realtime notification never advertises 1m L1 as strict segment evidence", () => {
  const candidate = realtimeNotificationCandidate(
    {
      schema: "chanlun-realtime-review-notification",
      notification_id: `sha256:${"2".repeat(64)}`,
      side: "buy",
      code: "SZ.000001",
      point_type: "2buy",
      signal_time: "2026-08-20T10:00:00+08:00",
      chart_urls: { "5m": "/chart/5m" },
      review_required: true,
      automated_action_authorized: false,
      real_order_transport_enabled: false,
      live_status: "LIVE_DISABLED",
      delivery_status: "delivered",
      new_stage: "segment_enriched",
      segment_difference_present: true,
      segment_difference_status: "current",
      segment_difference_current: true,
      segment_difference_evidence_status: "present",
      segment_difference_boundary_status: "current",
      segment_difference_point_type: "1buy",
      segment_difference_recursive_level: 1,
      segment_difference_available_at: "2026-08-20T10:01:00+08:00",
    },
    new Date("2026-08-20T10:02:00+08:00"),
  );

  assert.equal(candidate.realtime_notification_segment_difference_present, false);
  assert.equal(candidate.realtime_notification_segment_difference_status, "absent");
  assert.equal(
    candidate.realtime_notification_segment_difference_evidence_status,
    "absent",
  );
  assert.equal(candidate.realtime_notification_event_kind, "FIVE_MINUTE_LIFECYCLE");
  assert.equal(candidate.alert_type, "REALTIME_BUY_POINT");
});

test("segment enrichment remains actionable independently of parent setup age", () => {
  const candidate = realtimeNotificationCandidate(
    {
      schema: "chanlun-realtime-review-notification",
      notification_id: `sha256:${"3".repeat(64)}`,
      side: "buy",
      code: "SZ.000001",
      point_type: "3buy",
      signal_time: "2026-08-20T10:00:00+08:00",
      signal_available_at: "2026-08-20T10:00:00+08:00",
      detected_at: "2026-08-20T10:01:00+08:00",
      chart_urls: { "5m": "/chart/5m", "1m": "/chart/1m" },
      review_required: true,
      automated_action_authorized: false,
      real_order_transport_enabled: false,
      live_status: "LIVE_DISABLED",
      delivery_status: "delivered",
      new_stage: "segment_enriched",
      segment_difference_present: true,
      segment_difference_status: "current",
      segment_difference_current: true,
      segment_difference_evidence_status: "present",
      segment_difference_boundary_status: "current",
      segment_difference_point_type: "1buy",
      segment_difference_divergence_kind: "trend",
      segment_difference_recursive_level: 0,
      segment_difference_available_at: "2026-08-20T09:40:00+08:00",
    },
    new Date("2026-08-20T10:01:00+08:00"),
  );

  assert.equal("realtime_notification_current_age_seconds" in candidate, false);
  assert.equal("realtime_notification_is_historical" in candidate, false);
  assert.equal(candidate.review_lane, "ACTIONABLE_REVIEW");
  assert.equal(
    candidate.realtime_notification_segment_difference_divergence_kind,
    "trend",
  );
});

test("unknown review diagnostics retain their exact code instead of generic copy", () => {
  assert.equal(
    paperReasonLabel("NEW_REVIEW_DIAGNOSTIC"),
    "诊断代码：NEW_REVIEW_DIAGNOSTIC",
  );
  assert.equal(
    mappedStateLabel({ READY: "已就绪" }, "NEW_STATE"),
    "诊断代码：NEW_STATE",
  );
});
