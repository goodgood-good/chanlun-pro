"use strict";

(function exposeHumanReviewMarkoutAudit(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.HumanReviewMarkoutAudit = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildAuditLabels() {
  function text(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function hashIdentity(value) {
    const full = text(value, "");
    const attested = /^sha256:[0-9a-f]{64}$/.test(full);
    return {
      attested,
      full: attested ? full : null,
      short: attested ? `${full.slice(0, 18)}…` : "未认证",
    };
  }

  function cohortLabel(markoutValue) {
    const markout = markoutValue || {};
    if (markout.status === "INVALID") return "证据无效";
    if (markout.status !== "AVAILABLE") return "尚无观察样本";
    const sample = markout.sample || {};
    const sourceStatus = text(markout.source_provenance_status, "UNAVAILABLE");
    const identityStatus = text(sample.source_identity_status, "UNAVAILABLE");
    const cohortCount = Array.isArray(sample.source_cohort_ids)
      ? sample.source_cohort_ids.length
      : 0;
    if (sourceStatus !== "COMPLETE") return "价格来源未完整";
    if (identityStatus === "NOT_APPLICABLE") return "尚无观察样本";
    if (identityStatus !== "ATTESTED") return `${cohortCount} 批 · 源码未认证`;
    if (sample.mixed_sample_cohorts === true) {
      return `${cohortCount} 批 · 已拆分，禁止合并`;
    }
    return `${cohortCount} 批 · 单一已认证实现`;
  }

  function horizonLabel(markoutValue, horizonValue) {
    const markout = markoutValue || {};
    const horizon = String(horizonValue);
    if (markout.status === "INVALID") return "证据无效";
    if (markout.status !== "AVAILABLE") return "尚未积累 · 不可评价";
    const sample = markout.sample || {};
    const row = (markout.summary || {})[horizon] || {};
    const sourceStatus = text(markout.source_provenance_status, "UNAVAILABLE");
    const identityStatus = text(sample.source_identity_status, "UNAVAILABLE");
    const minimum = Number(sample.minimum_strategic_observations || 100);
    const eligible = Number(row.eligible_count || 0);
    const sufficient = (sample.sample_sufficient_by_horizon || {})[horizon] === true
      && Number.isFinite(minimum)
      && minimum > 0
      && eligible >= minimum;
    let verdict = "同批样本不足";
    if (sourceStatus === "INCOMPLETE") verdict = "价格来源未完整";
    else if (sourceStatus !== "COMPLETE" || identityStatus !== "ATTESTED") {
      verdict = "源码未认证";
    } else if (sample.mixed_sample_cohorts === true) {
      verdict = "已拆分，禁止合并";
    } else if (sufficient) verdict = "同批样本门通过";
    return `${eligible} 成熟 / ${Number(row.pending_count || 0)} 待 · ${verdict}`;
  }

  return { cohortLabel, hashIdentity, horizonLabel };
});
