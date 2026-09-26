"""本周期分型、笔、线段、中枢与 MACD 的固定生产配置。"""

from __future__ import annotations

import hashlib
import json

STRICT_BASE_PROFILE_ID = "chanlun-source-faithful-base"
STRICT_STROKE_MODE = "old-user-path-b-completed-prefix-v14"


_STRICT_BASE_CONFIG: dict[str, object] = {'fractal_rule': 'three-cl-k-both-extremes',
 'inclusion_initial_context_rule': 'common-price-geometry-with-separate-source-provenance-v2',
 'fractal_completion_rule': 'physical-fractal-independent-of-stroke-completion',
 'stroke_rule': STRICT_STROKE_MODE,
 'stroke_distance_rule': 'merged-centers-at-least-four',
 'stroke_price_rule': 'top-middle-high-and-low-above-bottom-middle',
 'stroke_secondary_fractal_rule': 'retained-facts-without-internal-extreme-veto',
 'stroke_endpoint_range_rule': 'symmetric-center-ranges-user-P-C-policy',
 'stroke_near_opposite_rule': 'path-b-tail-reselection-with-completed-prefix-guard',
 'stroke_same_type_rule': 'strict-extreme-replacement-of-mutable-tail',
 'stroke_equal_extreme_rule': 'keep-earlier-fractal',
 'stroke_path_rule': 'causal-selected-chain-without-discarded-path-restoration',
 'stroke_origin_rule': 'earliest-relative-fractal-with-unqualified-origin-recovery',
 'stroke_pending_rule': 'retain-physical-facts-outside-selected-chain',
 'stroke_lock_rule': 'third-qualified-edge-with-closed-source-witness',
 'stroke_completion_scope': 'qualified-continued-and-final-completion-distinguished',
 'stroke_revision_rule': 'recompute-dependent-structures-after-endpoint-reselection',
 'physical_unit_evidence_rule': 'version-selection-and-confirmation-evidence-v1',
 'stroke_evidence_rule': 'physical-fractal-visibility-separated-from-selection-time',
 'stroke_disconnected_rule': 'one-continuous-chain-and-unselected-observations',
 'stroke_processing_rule': 'fresh-batch-and-causal-tail-update-share-source-rules',
 'stroke_downstream_rule': 'continuous-formal-input-and-isolated-conditional-observations-v2',
 'segment_rule': 'feature-sequence',
 'segment_start_rule': 'overlapping-origin-with-causal-unestablished-origin-recovery-v2',
 'segment_gap_rule': 'second-feature-fractal-with-physical-turn-continuation-v4',
 'segment_gap_classification_rule': 'standard-gap-with-covering-first-reversal-exception-v4',
 'segment_pending_input_rule': 'explicit-first-break-hypotheses-separated-from-locked-evidence-v2',
 'segment_feature_pivot_rule': 'record-protected-break-and-standard-local-complete-fractal-v6',
 'segment_reverse_break_rule': 'established-predecessor-with-certified-raw-first-end-extension-v8',
 'segment_feature_inclusion_rule': 'sequential-inclusion-before-reference-pivot-selection-v4',
 'segment_equal_extreme_rule': 'earliest-middle-feature-boundary-source-with-separate-stem-provenance-v2',
 'segment_tail_rule': 'directional-extreme-or-current-tail-without-historical-backfill-v2',
 'segment_confirmation_rule': 'all-source-pens-through-causal-witness-v2',
 'segment_scan_rule': 'earliest-complete-first-break-with-protected-second-feature-priority-v7',
 'segment_cache_rule': 'finite-values-before-source-snapshot-revalidation-v3',
 'segment_price_range_rule': 'constituent-stroke-extremes-with-actual-market-time-and-export-v2',
 'center_seed_rule': 'physical-entry-middle-three-core-independent-leave-five-closed-single-price-v2',
 'center_lifecycle_rule': 'external-departure-first-non-crossing-return-third-class-v2',
 'third_class_boundary_rule': 'departure-side-contact-permitted-inclusive-return-E2-v3',
 'center_scan_rule': 'five-role-physical-seed-causal-lifecycle-owner',
 'center_frame_role_rule': 'first-directional-departure-preserved-after-failed-return-v1',
 'center_followup_rule': 'source-ownership-separated-from-strict-return-confirmation-v1',
 'center_frame_range_rule': 'external-directional-exit-separated-from-pending-lifecycle-v2',
 'conditional_rule_state_version': 'chanlun-local-source-conditioned-v4',
 'idx_macd_fast': 12,
 'idx_macd_slow': 26,
 'idx_macd_signal': 9,
 'chart_structure_rule': 'native-segment-centers-v2'}


def strict_base_config() -> dict:
    """返回唯一生产结构配置的新副本。"""

    return dict(_STRICT_BASE_CONFIG)


def strict_base_config_revision() -> str:
    """返回完整生产结构配置的确定性修订标识。"""

    encoded = json.dumps(
        _STRICT_BASE_CONFIG,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
