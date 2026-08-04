"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const audit = require("../human_review_markout_audit.js");

function markout(overrides = {}) {
  return {
    status: "AVAILABLE",
    source_provenance_status: "COMPLETE",
    summary: { "5": { eligible_count: 99, pending_count: 1 } },
    sample: {
      source_identity_status: "ATTESTED",
      source_cohort_ids: ["sha256:" + "1".repeat(64)],
      mixed_sample_cohorts: false,
      minimum_strategic_observations: 100,
      sample_sufficient_by_horizon: { "5": false },
    },
    ...overrides,
  };
}

test("hash identity exposes only exact lowercase sha256 evidence", () => {
  const value = "sha256:" + "a".repeat(64);
  assert.deepEqual(audit.hashIdentity(value), {
    attested: true,
    full: value,
    short: `${value.slice(0, 18)}…`,
  });
  assert.deepEqual(audit.hashIdentity("sha256:" + "A".repeat(64)), {
    attested: false,
    full: null,
    short: "未认证",
  });
  assert.equal(audit.hashIdentity(null).short, "未认证");
});

test("cohort label distinguishes unavailable legacy mixed and single source", () => {
  assert.equal(audit.cohortLabel({ status: "INVALID" }), "证据无效");
  assert.equal(
    audit.cohortLabel({ status: "UNQUALIFIED" }),
    "历史样本 · 会话资格未认证",
  );
  assert.equal(audit.cohortLabel({ status: "NOT_AVAILABLE" }), "尚无观察样本");
  assert.equal(
    audit.cohortLabel(markout({ source_provenance_status: "LEGACY_UNATTESTED" })),
    "旧版样本 · 源码未认证",
  );
  assert.equal(
    audit.cohortLabel(markout({ sample: { source_identity_status: "NOT_APPLICABLE" } })),
    "尚无观察样本",
  );
  assert.equal(
    audit.cohortLabel(markout({
      sample: {
        source_identity_status: "LEGACY_UNATTESTED",
        source_cohort_ids: ["legacy"],
      },
    })),
    "1 批 · 源码未认证",
  );
  assert.equal(
    audit.cohortLabel(markout({
      sample: {
        source_identity_status: "ATTESTED",
        source_cohort_ids: ["one", "two"],
        mixed_sample_cohorts: true,
      },
    })),
    "2 批 · 已拆分，禁止合并",
  );
  assert.equal(audit.cohortLabel(markout()), "1 批 · 单一已认证实现");
});

test("horizon label keeps the 100 observation equality boundary honest", () => {
  assert.equal(audit.horizonLabel({ status: "INVALID" }, 5), "证据无效");
  assert.equal(
    audit.horizonLabel({ status: "UNQUALIFIED" }, 5),
    "不合格会话样本已排除",
  );
  assert.equal(
    audit.horizonLabel({ status: "NOT_AVAILABLE" }, 5),
    "尚未积累 · 不可评价",
  );
  assert.match(
    audit.horizonLabel(markout({ source_provenance_status: "INCOMPLETE" }), 5),
    /价格来源未完整$/,
  );
  assert.match(
    audit.horizonLabel(markout({ source_provenance_status: "LEGACY_UNATTESTED" }), 5),
    /源码未认证$/,
  );
  assert.match(
    audit.horizonLabel(markout({
      sample: {
        source_identity_status: "ATTESTED",
        mixed_sample_cohorts: true,
        minimum_strategic_observations: 100,
        sample_sufficient_by_horizon: { "5": true },
      },
    }), 5),
    /已拆分，禁止合并$/,
  );
  assert.match(
    audit.horizonLabel(markout({
      sample: {
        source_identity_status: "ATTESTED",
        mixed_sample_cohorts: false,
        minimum_strategic_observations: 100,
        sample_sufficient_by_horizon: { "5": true },
      },
    }), 5),
    /99 成熟 \/ 1 待 · 同批样本不足$/,
  );
  const exactlyEnough = markout({
    summary: { "5": { eligible_count: 100, pending_count: 0 } },
    sample: {
      source_identity_status: "ATTESTED",
      source_cohort_ids: ["one"],
      mixed_sample_cohorts: false,
      minimum_strategic_observations: 100,
      sample_sufficient_by_horizon: { "5": true },
    },
  });
  assert.match(audit.horizonLabel(exactlyEnough, "5"), /同批样本门通过$/);
});
