import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  deepWarmupDiagnosticPresentation,
  deferredEvidenceDisclosure,
} = require("../../web/chanlun_chart/cl_app/static/js/human_review_screening.js");

test("compact candidates disclose deferred evidence without pretending it is missing", () => {
  const result = deferredEvidenceDisclosure(
    { evidence_detail_available: true },
    "M/W/D",
    `sha256:${"1".repeat(64)}`,
  );

  assert.equal(result.tag, "M/W/D按需加载");
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
  assert.match(result.lines[0], /PREFIX_SIGNATURE_DIVERGED/);
});
