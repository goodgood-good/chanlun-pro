# -*- coding: utf-8 -*-
"""
按原始笔与特征序列证据构建线段。

原文定位、项目规则和工程约定见 docs/segment_rules.md。
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import List, Optional
from math import isfinite
from numbers import Rational

from chanlun.core.types import BI, XD
from chanlun.tools.log_util import LogUtil
from chanlun.core.segment_evidence import (
    FeatureEvidence,
    FeatureGapEvidence,
    ObservationOriginEvidence,
    SecondFeatureBreakEvidence,
    DirectFirstBreakEvidence,
    ReverseSegmentEvidence,
    ReturnSegmentEvidence,
    FirstPenContinuationEvidence,
    FirstPenBreakEvidence,
    SegmentEvidence,
    SegmentTail,
    PendingBoundary,
    SourcePens,
    confirmation_time,
)

_log = LogUtil


_TYPE2_CONFIRMED = "confirmed"
_TYPE2_PENDING = "pending"
_TYPE2_INVALIDATED = "invalidated"


def _is_finite_price(value) -> bool:
    # math.isfinite converts exact numeric types to float. A finite Decimal,
    # integer or Fraction can exceed float's range without being infinite.
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, Rational):
        return True
    return isfinite(value)


@dataclass(frozen=True)
class _GapConfirmationInvalidated:
    """待定的第二种缺口结构被更晚的笔否定。

    这根原方向延伸笔结束整个旧候选上下文。须把它纳入原段后继续扫描，不能
    只保存时间，再回头借用该未完成结构内部的局部分型。
    """

    # None 表示否定证据自身仍是候选笔，只能改变预览，不能确认线段。
    witnessed_at: object
    witness_index: Optional[int] = None
    candidate_index: Optional[int] = None
    first_sequence: tuple[FeatureEvidence, ...] = ()


@dataclass(frozen=True)
class _CandidateExtension:
    witness_index: int


@dataclass(frozen=True)
class _FeatureTail:
    """A normalized suffix result, independent of its incoming price sources."""

    status: str
    stop: int | None
    low: object
    high: object
    low_source: int
    high_source: int


@dataclass(frozen=True)
class _FeaturePens:
    """Transient strided view; final public evidence still owns an index tuple."""

    values: object
    start: int
    stop: int

    def __len__(self):
        return len(range(self.start, self.stop, 2))

    def __iter__(self):
        for i in range(self.start, self.stop, 2):
            yield self.values[i]


def _bi_label(bi: BI) -> str:
    return f"bi[{bi.index}]{bi.type}({bi.start.val}→{bi.end.val})"


def _elem_label(e: dict) -> str:
    merged = e.get("merged_bis")
    if merged:
        return f"{{h={e['high']},l={e['low']},merged={len(merged)}}}"
    return f"{{h={e['high']},l={e['low']}}}"


# ============================================================
# 特征序列工具函数（纯函数，无状态）
# ============================================================


def _bi_to_cs_elem(bi: BI) -> dict:
    return {"bi": bi, "high": bi.high, "low": bi.low}


def _overlap(a, b) -> bool:
    h1, l1 = (a["high"], a["low"]) if isinstance(a, dict) else (a.high, a.low)
    h2, l2 = (b["high"], b["low"]) if isinstance(b, dict) else (b.high, b.low)
    return max(l1, l2) <= min(h1, h2)


def _classify_first_feature_gap(left: dict, first: BI, middle: dict, direction: str):
    """L067 standard gap, with the scoped L071/L078 boundary exception.

    A covering first reversal must not have its strength erased by inclusion
    across the assumed boundary. It is different from a merely overlapping
    reversal whose standard middle later becomes gapped. The former retains
    the reviewed figure-01 classification; the latter needs a second fractal.
    A shared far edge is still containment (L075), but the turning extreme
    must be strict. This is a documented deduction, not a verbatim source rule.
    """
    covers_reference = (
        first.high > left["high"] and first.low <= left["low"]
        if direction == "up"
        else first.low < left["low"] and first.high >= left["high"]
    )
    return FeatureGapEvidence(
        raw_first=FeatureEvidence.from_element(_bi_to_cs_elem(first)),
        raw_gap=not _overlap(left, first),
        standard_gap=not _overlap(left, middle),
        basis="protected-first-reversal" if covers_reference else "standard-first-feature",
    )


def _elem_farthest_bi_index(elem: dict) -> int:
    """返回特征元素所代表的最远物理笔序号。"""

    merged = elem.get("merged_bis")
    if merged:
        return max(bi.index for bi in merged)
    return elem["bi"].index


def _has_inclusion(a: dict, b: dict) -> bool:
    return (a["high"] >= b["high"] and a["low"] <= b["low"]) or (
        b["high"] >= a["high"] and b["low"] <= a["low"]
    )


def _merge_two(prev: dict, cur: dict, direction: str) -> dict:
    if direction == "up":
        mh, ml = max(prev["high"], cur["high"]), max(prev["low"], cur["low"])
    else:
        mh, ml = min(prev["high"], cur["high"]), min(prev["low"], cur["low"])
    prev_bis = prev.get("merged_bis", [prev["bi"]])
    cur_bis = cur.get("merged_bis", [cur["bi"]])
    return {
        "bi": prev["bi"],
        "high": mh,
        "low": ml,
        "merged_bis": SourcePens.join(prev_bis, cur_bis),
    }


def _process_inclusion(elems: List[dict], direction: str) -> List[dict]:
    """特征序列包含处理 → 标准特征序列。

    按第65课的顺序，用同语境中最近非包含元素的方向处理下一次包含。
    缺少该前项时使用调用方提供的方向种子；种子选择是工程约定。
    """
    result = []
    for elem in elems:
        _append_feature(result, elem, direction)
    return result


def _append_feature(result, element, initial_direction):
    """L065 chronological inclusion, in one eligible feature context.

    The last non-contained pair supplies direction. Seeding a context with
    only one element uses the examined segment direction as a convention.
    Do not back-merge a previously resolved pair using a different direction.
    """
    if not result or not _has_inclusion(result[-1], element):
        result.append(element.copy())
        return
    direction = initial_direction
    if len(result) >= 2:
        direction = "up" if result[-1]["high"] > result[-2]["high"] else "down"
    result[-1] = _merge_two(result[-1], element, direction)


def _resolve_pivot_bi(elem: dict, seg_type: str):
    """从（可能被包含合并的）反向 CS 元素中，定位"枢轴反向笔"——
    即用于回溯定位原线段终点的那根反向笔。

    语义说明：
      - 一个 CS 元素可能由若干根反向笔合并而成（merged_bis）。
      - 调用者拿到的是分型中心 elem，需要据此推算"原线段终点 = 该反向笔的前一根同向笔"。
      - 选择规则：取使原线段达到方向极值的那根反向笔
          * up 段（反向 CS 是 down 笔）→ 取 high 最大的 down 笔
            原因：down 笔的起点 = 上一根 up 笔的终点；high 越大 → 上一根 up 笔涨得越高
          * down 段（反向 CS 是 up 笔）→ 取 low 最小的 up 笔
            原因：up 笔的起点 = 上一根 down 笔的终点；low 越小 → 上一根 down 笔跌得越低

    Args:
        elem: CS 元素 dict，含 'bi'（首根反向笔）和可选 'merged_bis'（合并的反向笔列表）
        seg_type: 原线段方向（'up' 或 'down'）

    Returns:
        枢轴反向笔（BI 对象）。其前一根同向笔即为原线段终点候选。
    """
    if elem.get("_pivot_direction") == seg_type:
        return elem["_pivot_bi"]
    target = elem["bi"]
    merged = elem.get("merged_bis")
    if merged:
        target = (
            max(merged, key=lambda b: b.high)
            if seg_type == "up"
            else min(merged, key=lambda b: b.low)
        )
    return target


def _contained_pivot_origins(all_bis):
    """Index nested same-direction stems once, preserving physical pen indices.

    Only a later pen lying inside the immediately previous same-direction
    pen continues this stem. An outward pen starts a new context. This is
    narrower than arbitrary/transitive inclusion; it is the L079 lower
    figure's still-unbroken 7-8 / 9-10 candidate, extended recursively.
    """
    origins = list(range(len(all_bis)))
    for i in range(2, len(all_bis)):
        previous, current = all_bis[i - 2], all_bis[i]
        if previous.low <= current.low and current.high <= previous.high:
            origins[i] = origins[i - 2]
    return origins


def _next_strict_pen_extensions(values):
    """Next same-direction end beyond each physical end, in linear work.

    A reversal's origin is the preceding pen's end. Reuse that price-order
    fact when bounding independent probes; long equal-price containment must
    not rescan the entire remaining history for every candidate.
    """
    following = [None] * len(values)
    stacks = {'up': [], 'down': []}
    for i in range(len(values) - 1, -1, -1):
        pen = values[i]
        stack = stacks[pen.type]
        while stack and (values[stack[-1]].end.val <= pen.end.val if pen.type == 'up'
                         else values[stack[-1]].end.val >= pen.end.val):
            stack.pop()
        if stack:
            following[i] = stack[-1]
        stack.append(i)
    return following


def _next_feature_progress(values):
    """Next strict far-edge progression between adjacent same-direction pens.

    Any standalone first fractal needs an outward right element. If all of an
    up segment's down-feature lows are nondecreasing (or a down segment's up-
    feature highs nonincreasing), neither inclusion nor a protected first break
    can produce that right element. This is only a negative filter, not a proof.
    """
    following = [None] * len(values)
    next_at = {'up': None, 'down': None}
    for i in range(len(values) - 1, -1, -1):
        item = values[i]
        if i >= 2 and (item.end.val > values[i - 2].end.val if item.type == 'up'
                       else item.end.val < values[i - 2].end.val):
            next_at[item.type] = i
        following[i] = next_at[item.type]
    return following


def _next_feature_turn(values):
    """Necessary entering edge of a standalone first fractal, without a parent.

    Down features must have a higher later start for a top, and up features a
    lower later start for a bottom. A monotone sequence of starts cannot supply
    such a strict pivot through inclusion either. A predecessor-dependent
    nonordinary break is deliberately unavailable to standalone probes.
    """
    following = [None] * len(values)
    next_at = {'up': None, 'down': None}
    for i in range(len(values) - 1, -1, -1):
        item = values[i]
        if i >= 2 and (item.start.val < values[i - 2].start.val if item.type == 'up'
                       else item.start.val > values[i - 2].start.val):
            next_at[item.type] = i
        following[i] = next_at[item.type]
    return following


def _advance_left_reference(reference, candidate, direction):
    """Advance the standard element of the eligible pre-boundary records.

    This is the pre-boundary reference role inferred from L081 (12, 56, 78).
    It does not filter the post-boundary or second feature sequences.
    Inclusion contributes to the existing standard element before deciding
    whether there is a new reference pivot. A lower top inside that element
    is not a new pivot, but can still raise its low and supply provenance.
    This also preserves a candidate's already processed included stem when
    a later extension returns it to the original segment (L065/L071).
    """
    previous = reference if isinstance(reference, dict) else _bi_to_cs_elem(reference)
    following = candidate if isinstance(candidate, dict) else _bi_to_cs_elem(candidate)
    if _has_inclusion(previous, following):
        return _merge_two(previous, following, direction)
    advances = (
        following["high"] > previous["high"]
        if direction == "up"
        else following["low"] < previous["low"]
    )
    if not advances:
        return previous
    return following


class XdCalculator:
    """Build segments from original pens; retain proofs separately from previews."""

    def __init__(self):
        self.xds: List[XD] = []
        self._input_snapshot = None
        self._proofs: dict[tuple, SegmentEvidence] = {}
        self.evidence: tuple[SegmentEvidence, ...] = ()
        self.tail_state = SegmentTail(None, None, "insufficient-pens")
        # Reuse suffix calculations only within one validated input replay.
        self._feature_scan_cache = None
        self._origin_recovery = None

    def __getstate__(self):
        state = dict(self.__dict__)
        # Object ids are process-local and cannot validate deserialized or
        # deep-copied pen bindings. Keep geometry, invalidate only the cache.
        state["_input_snapshot"] = None
        return state

    @staticmethod
    def _snapshot(bis):
        # No monotone identity-prefix assumption: direct callers may append,
        # replace history, or revise a pen's confirmation in the same list.
        return tuple(
            (
                id(b),
                id(b.start),
                id(b.end),
                b.index,
                getattr(b, "component_index", 0),
                b.type,
                b.start.val,
                b.end.val,
                b.high,
                b.low,
                getattr(b, "locked_at", None),
                getattr(b, "selection_pending", False),
            )
            for b in bis
        )

    def calculate(self, bis: List[BI]) -> List[XD]:
        if len({getattr(bi, "component_index", 0) for bi in bis}) > 1:
            raise ValueError("segment input crosses an unresolved stroke boundary")
        if any(bi.index != position for position, bi in enumerate(bis)):
            raise ValueError(
                "segment input indices must match continuous scope positions"
            )
        # Validate before comparing cached values: Decimal sNaN can raise
        # during equality itself, including after an in-place historical edit.
        for i, pen in enumerate(bis):
            if not all(
                _is_finite_price(v)
                for v in (pen.start.val, pen.end.val, pen.high, pen.low)
            ):
                raise ValueError("segment input requires finite prices")
            if (
                pen.type not in ("up", "down")
                or pen.start.val == pen.end.val
                or (pen.end.val > pen.start.val) != (pen.type == "up")
            ):
                raise ValueError("segment input pen direction is inconsistent")
            if pen.high != max(pen.start.val, pen.end.val) or pen.low != min(
                pen.start.val, pen.end.val
            ):
                raise ValueError("segment input pen range must match its endpoints")
            if i and (
                bis[i - 1].type == pen.type or bis[i - 1].end.val != pen.start.val
            ):
                raise ValueError("segment input must be connected alternating pens")
        snapshot = self._snapshot(bis)
        if snapshot == self._input_snapshot:
            return self.xds
        # Assign a new output list so a later calculation cannot mutate a
        # previously returned result by clearing/appending to the same list.
        self.xds = []
        self._proofs = {}
        self.evidence = ()
        self.tail_state = SegmentTail(None, None, "insufficient-pens")
        self._origin_recovery = None
        if len(bis) >= 3:
            start = self._find_strict_start(bis)
            if start >= 0:
                self._feature_scan_cache = {}
                try:
                    self._build_segments(bis, start)
                finally:
                    self._feature_scan_cache = None
            else:
                self.tail_state = SegmentTail(None, None, "no-observation-origin")
        self._input_snapshot = snapshot
        return self.xds

    def _scan_first_feature(self, values, first_index, direction, extreme):
        """Normalize a protected middle through its first independent right.

        Both ordinary and local candidates consume the same chronological
        suffix. The invalidation extreme remains explicit because their
        eligibility contexts can differ. A cache key includes the physical
        cursor, interval, direction and extreme, not the candidate's identity.

        Cached equal prices inherit the CURRENT incoming source; only a strict
        change can borrow a future source. This preserves the earlier-source
        convention without moving one candidate's boundary to another's.
        """
        first = values[first_index]
        low, high = first.low, first.high
        low_source = high_source = first_index
        up = direction == "up"
        cache = getattr(self, "_feature_scan_cache", None)
        visited = []
        outcome = None
        for i in range(first_index + 1, len(values)):
            if cache is not None:
                key = (i, direction, extreme, low, high)
                try:
                    cached = cache.get(key)
                except TypeError:
                    # Valid custom numeric objects need not be hashable.
                    # They keep the same scanner, without memoization.
                    cache = None
                    visited = []
                else:
                    if cached is not None:
                        if cached.status == "ready":
                            low_source = low_source if cached.low == low else cached.low_source
                            high_source = high_source if cached.high == high else cached.high_source
                            # Equal numbers may have different exact numeric
                            # types. Keep the current supplier's actual value.
                            low, high = values[low_source].low, values[high_source].high
                        outcome = _FeatureTail(cached.status, cached.stop, low, high,
                                               low_source, high_source)
                        break
                    visited.append(key)
            pen = values[i]
            if pen.type == direction:
                if pen.high > extreme if up else pen.low < extreme:
                    outcome = _FeatureTail("extension", i, low, high, low_source, high_source)
                    break
                continue
            included = (low <= pen.low and pen.high <= high) or (
                pen.low <= low and high <= pen.high
            )
            if not included:
                outcome = _FeatureTail("ready", i, low, high, low_source, high_source)
                break
            if pen.low > low if up else pen.low < low:
                low, low_source = pen.low, i
            if pen.high > high if up else pen.high < high:
                high, high_source = pen.high, i
        if outcome is None:
            outcome = _FeatureTail("pending", None, low, high, low_source, high_source)
        if cache is not None:
            for key in visited:
                cache[key] = outcome
        if outcome.status == "pending":
            return PendingBoundary("waiting-first-feature", first_index - 1)
        assert outcome.stop is not None
        if outcome.status == "extension":
            return _CandidateExtension(outcome.stop)
        middle = {
            "bi": first, "low": low, "high": high,
            "_low_source_index": low_source, "_high_source_index": high_source,
            "_pivot_direction": direction,
            "_pivot_bi": values[high_source if up else low_source],
        }
        if outcome.stop > first_index + 2:
            middle["merged_bis"] = _FeaturePens(values, first_index, outcome.stop)
        return middle, _bi_to_cs_elem(values[outcome.stop])

    # ----------------------------------------------------------
    def _find_strict_start(self, all_bis: List[BI]) -> int:
        """在所给历史窗口内选最早的局部方向起点，随后独立识别线段。

        初始三笔须重叠，且首笔起点是这三笔的方向起始极值。第 78 课允许
        先选观察高低点；这里选最早满足条件者属于工程约定，不声明窗口之前
        已有完整走势。第 81 课的 0—9 图允许 3 低于 1，不能再附加首笔终点
        低于后两根同向笔的五笔单调条件。后继段仍须通过原段破坏规则识别。
        """
        self._origin_recovery = None
        for i in range(len(all_bis) - 2):
            bi_i = all_bis[i]
            bi_i2 = all_bis[i + 2]
            is_extreme = (
                bi_i.start.val <= bi_i2.start.val
                if bi_i.type == "up"
                else bi_i.start.val >= bi_i2.start.val
            )
            if is_extreme and _overlap(bi_i, bi_i2):
                return self._resolve_observation_origin(all_bis, i)
        return -1

    def _resolve_observation_origin(self, all_bis, start):
        """Recover only an unestablished origin at the observation-window edge.

        L078 permits choosing an observed high/low, without inventing history
        before that point. The earliest overlapping triple is provisional:
        if it never advances beyond its first pen, and its origin is crossed
        before a viable boundary hypothesis exists, use that first pen's
        opposite extreme. Directional extension, a proof, or an eligible
        pending boundary by that same event preserves it.
        This is an observation policy, not a new interior segment-break rule.
        """
        first = all_bis[start]
        up = first.type == "up"
        for index in range(start + 2, len(all_bis)):
            pen = all_bis[index]
            if pen.type == first.type:
                if pen.end.val > first.end.val if up else pen.end.val < first.end.val:
                    return start
            elif pen.end.val < first.start.val if up else pen.end.val > first.start.val:
                # Never discard an already established boundary, including a
                # valid non-extreme boundary completed by this very crossing.
                prefix = XdCalculator()
                prefix._feature_scan_cache = {}
                prefix._build_segments(all_bis[:index + 1], start)
                if (prefix.evidence or prefix.tail_state.reason in {"waiting-first-feature", "waiting-second-feature"}
                        or self._has_local_boundary_at_origin_cross(all_bis, start, index)):
                    return start
                self._origin_recovery = ObservationOriginEvidence(start, start + 1, index)
                return start + 1
        return start

    @staticmethod
    def _has_local_boundary_at_origin_cross(values, start, crossing):
        """Preserve an eligible strong turn even before its right pen arrives.

        At the first origin crossing, the reverse pen already passes every
        earlier shoulder's far edge. Only its turning extreme and any retained
        same-direction contained stem remain to be checked. No future pen is
        fabricated to make the ordinary three-pen scanner run.
        """
        first = values[crossing]
        def turns(left):
            return first.high > left.high if values[start].type == "up" else first.low < left.low
        if turns(values[crossing - 2]):
            return True
        stem = crossing - 1
        while stem - 2 >= start:
            before, after = values[stem - 2], values[stem]
            if not (before.low <= after.low and after.high <= before.high):
                break
            stem -= 2
        return stem > start and stem < crossing - 1 and turns(values[stem - 1])

    # ----------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------
    def _build_segments(self, all_bis: List[BI], start: int, *, first_proof_only=False,
                        pivot_origins=None, next_extensions=None, feature_progress=None,
                        feature_turns=None):
        """Accept complete geometric proofs in order, then project one tail.

        There is no post-hoc extreme relocation, historical pen skipping, or
        cascade absorption of already proven boundaries. A revised input is
        replayed from its observation origin through this same code path.
        """
        segs = []
        locked_candidates = {}
        accepted = []
        pos = start
        pending = None
        if pivot_origins is None:
            pivot_origins = _contained_pivot_origins(all_bis)
        if next_extensions is None:
            next_extensions = _next_strict_pen_extensions(all_bis)
        self._reverse_pivot_origins = pivot_origins
        self._reverse_extensions = next_extensions
        self._reverse_feature_progress = (
            _next_feature_progress(all_bis) if feature_progress is None else feature_progress
        )
        self._reverse_feature_turns = (
            _next_feature_turn(all_bis) if feature_turns is None else feature_turns
        )
        self._reverse_probe_cache = {}
        self._origin_context_prefixes = {}

        def accept(proof):
            origin = self._origin_recovery if not accepted else None
            if origin is not None:
                proof = replace(proof, witness_index=max(proof.witness_index, origin.witness_index),
                                origin_evidence=origin)
            formed_at = confirmation_time(
                all_bis, origin.initial_index if origin else proof.start_index, proof.witness_index
            )
            if segs:
                previous_time = segs[-1][3]
                formed_at = (
                    max(previous_time, formed_at)
                    if previous_time is not None and formed_at is not None
                    else None
                )
            segs.append((*proof.key, formed_at))
            accepted.append(proof)
            self._freeze_confirmed_candidate(segs, locked_candidates)

        while pos + 2 < len(all_bis):
            if first_proof_only and accepted:
                break
            if not _overlap(all_bis[pos], all_bis[pos + 2]):
                break
            inherited = (
                accepted[-1].reverse_segment.successor
                if accepted and accepted[-1].reverse_segment is not None
                else self._successor_from_second_sequence(accepted[-1], all_bis)
                if accepted and accepted[-1].second_sequence
                else None
            )
            if inherited is not None:
                # L077: the same second fractal completes B as well as A.
                # Re-scanning B independently would lose the strict second-
                # sequence context, or improperly require a third sequence.
                accept(inherited)
                pos = inherited.end_index + 1
                continue
            seg_start, seg_end = pos, pos + 2
            seg_type = all_bis[pos].type
            anchor = all_bis[pos].start.val
            extreme = all_bis[seg_end].end.val
            left_reference = all_bis[pos + 1]
            standard_references = [_bi_to_cs_elem(left_reference)]
            check = seg_end + 1
            decision_index = seg_end
            result = None
            minimum_end = self._successor_formation_end(accepted[-1]) if accepted else None
            if minimum_end is not None:
                # The same-side pens used as the predecessor's completed
                # continuation belong to this newly formed segment. Starting
                # another boundary inside that span would split the element
                # whose inclusion was just used to establish the predecessor.
                while seg_end < minimum_end:
                    extreme = (max(extreme, all_bis[seg_end].end.val) if seg_type == 'up'
                               else min(extreme, all_bis[seg_end].end.val))
                    left_reference = _advance_left_reference(left_reference, all_bis[check], seg_type)
                    _append_feature(standard_references, _bi_to_cs_elem(all_bis[check]), seg_type)
                    seg_end, check = check + 1, check + 2
                decision_index = max(decision_index, seg_end)
            while check + 1 < len(all_bis):
                end_value = all_bis[seg_end].end.val
                extreme = (
                    max(extreme, end_value)
                    if seg_type == "up"
                    else min(extreme, end_value)
                )
                seg_high, seg_low = (
                    (extreme, anchor) if seg_type == "up" else (anchor, extreme)
                )
                next_same = all_bis[check + 1]
                decision_index = max(decision_index, check + 1)
                extends = (
                    next_same.high > extreme
                    if seg_type == "up"
                    else next_same.low < extreme
                )
                if not extends:
                    search_context = "ordinary"
                    result = self._try_end(
                        all_bis,
                        seg_start,
                        seg_end,
                        seg_type,
                        seg_high,
                        seg_low,
                        check,
                        [left_reference],
                    )
                    if result is None:
                        search_context = "local"
                        result = self._try_end_r34(
                            all_bis, seg_start, seg_type, seg_high, seg_low, check,
                            max(seg_start, pivot_origins[check - 1]),
                        )
                    if result is None:
                        search_context = "standard"
                        result = self._try_end_standard_local(
                            all_bis, seg_start, seg_type, check, standard_references
                        )
                    if result is None and accepted:
                        search_context = "direct"
                        result = self._try_established_reverse_break(
                            all_bis, seg_start, seg_type, check, accepted[-1]
                        )
                    predecessor = accepted[-1] if accepted else None
                    result = self._try_first_case_completions(
                        all_bis, seg_start, seg_type, check, result, predecessor,
                    )
                    result = self._try_completed_return_segment(
                        all_bis, seg_start, seg_type, check, result, predecessor,
                    )
                    result = self._resolve_first_search_outcome(
                        all_bis, seg_start, seg_type, check, result,
                        standard_references, search_context, left_reference, pivot_origins,
                        predecessor,
                    )
                    if isinstance(
                        result, (_CandidateExtension, _GapConfirmationInvalidated)
                    ):
                        extension = result.witness_index
                        if extension is None:
                            raise ValueError("candidate extension requires a witness")
                        # L078: without the second fractal, the original-
                        # direction extension absorbs the provisional pieces.
                        # Advancing just the clock would wrongly resurrect
                        # interior turns from this now-closed context.
                        for i in range(check, extension, 2):
                            left_reference = _advance_left_reference(
                                left_reference, all_bis[i], seg_type
                            )
                            _append_feature(standard_references, _bi_to_cs_elem(all_bis[i]), seg_type)
                        seg_end, check = extension, extension + 1
                        decision_index = max(decision_index, extension)
                        result = None
                        continue
                    if isinstance(result, PendingBoundary):
                        pending = result
                        result = None
                        break
                    if result is not None:
                        break
                left_reference = _advance_left_reference(
                    left_reference, all_bis[check], seg_type
                )
                _append_feature(standard_references, _bi_to_cs_elem(all_bis[check]), seg_type)
                seg_end = check + 1
                check += 2
            if result is None:
                break
            real_end = result[0]
            key = (seg_start, real_end, seg_type)
            proof = self._proofs[key]
            proof = replace(
                proof, witness_index=max(proof.witness_index, decision_index)
            )
            accept(proof)
            pos = real_end + 1
        self.evidence = tuple(accepted)
        self._proofs = {p.key: p for p in accepted}
        if first_proof_only:
            # A certificate probe needs no display tail or XD objects. Apart
            # from wasting work, materializing a whole future tail here made
            # repeated small independent probes quadratic.
            return
        tail = (pos, all_bis[pos].type) if pos < len(all_bis) else None
        self.tail_state = SegmentTail(
            pos if tail else None,
            tail[1] if tail else None,
            pending.reason if pending else "forming",
            pending.candidate_index if pending else None,
            minimum_end_index=(self._successor_formation_end(accepted[-1])
                               if tail and accepted else None),
        )
        self._emit_segments(all_bis, segs, tail, start, locked_candidates)

    @staticmethod
    def _successor_formation_end(parent):
        """Keep a direct first-case continuation intact in the output chain.

        Return certificates are internal witnesses, not the next segment's
        formation span. F2 and explicit reverse certificates inherit their
        exact successor through the separate existing path.
        """
        if parent.parent_key is not None or parent.second_sequence or parent.reverse_segment or parent.return_segment:
            return None
        ends = []
        if parent.first_break_evidence is not None:
            ends.append(parent.first_break_evidence.extension_index)
        if parent.first_sequence:
            ends.append(max(parent.first_sequence[-1].source_indices))
        minimum = max(ends) if ends else None
        return minimum if minimum is not None and minimum > parent.end_index + 3 else None

    @staticmethod
    def _successor_from_second_sequence(parent, all_bis):
        """L067/L077: a resolved second fractal also fixes its own segment end.

        The standard middle's directional extreme supplies the actual pivot.
        Its first-two-element gap does not request a third sequence. Formal
        confirmation still depends on the parent's entire closed evidence.
        """
        direction = "down" if parent.direction == "up" else "up"
        left, middle, _ = parent.second_sequence
        pivot = (
            middle.low_source_index if direction == "down" else middle.high_source_index
        )
        start, end = parent.end_index + 1, pivot - 1
        if (
            end < start + 2
            or (end - start) % 2
            or all_bis[start].type != direction
            or all_bis[end].type != direction
            or not _overlap(all_bis[start], all_bis[start + 2])
            or not (
                all_bis[end].end.val < all_bis[start].start.val
                if direction == "down"
                else all_bis[end].end.val > all_bis[start].start.val
            )
        ):
            raise ValueError("second feature proof has an invalid successor boundary")
        return SegmentEvidence(
            start,
            end,
            direction,
            "parent-second-feature",
            parent.witness_index,
            first_sequence=parent.second_sequence,
            initial_gap=not _overlap(left, middle),
            parent_key=parent.key,
        )

    def _try_end_standard_local(self, all_bis, start, direction, check, standard, allow_type2=True):
        """L065/L067 first or second case in the chronological standard sequence.

        The caller first resolves the protected and record-reference contexts;
        a pending earlier hypothesis never reaches this fallback (L078).
        The left shoulder must survive full chronological inclusion, and the
        post-boundary elements must independently form the first fractal.
        A gap then requires the same strict second-sequence proof as any other
        L067 second case. A one-pen initial extreme is not an extra veto on an
        otherwise qualified non-extreme endpoint (L078, lines 280-313).
        """
        if len(standard) < 2:
            return None
        left, first = standard[-1], all_bis[check]
        # A covering reversal may only cover the NORMALIZED left element;
        # its raw predecessor can have a different far edge. Let _try_end
        # validate this protected case as well as an ordinary standard turn.
        turns = first.high > left["high"] if direction == "up" else first.low < left["low"]
        if not turns:
            return None
        result = self._try_end(
            all_bis, start, check - 1, direction, first.high, first.low, check, [left], allow_type2
        )
        if isinstance(result, tuple):
            key = (start, result[0], direction)
            self._proofs[key] = replace(self._proofs[key], reference_mode="standard-local")
        return result

    def _resolve_first_search_outcome(self, values, start, direction, first, result,
                                      standard, context, reference, pivot_origins=None,
                                      predecessor=None):
        """Compare the current standard context as well as later candidates.

        A local origin-retest can be pending while this same candidate already
        completes against its surviving standard reference (L067/L071). The
        later-candidate search starts two pens ahead and cannot find it. Keep
        the earlier F2-fractal/origin-return deadline, and preserve the actual
        winning proof when both contexts address the same physical boundary.
        """
        chosen = self._resolve_later_first_search_outcome(
            values, start, direction, first, result, standard, context, reference,
            pivot_origins, predecessor,
        )
        retained = None
        if isinstance(chosen, PendingBoundary) and chosen.reason == 'waiting-first-feature':
            limit = len(values) - 1
        elif (isinstance(chosen, PendingBoundary) and chosen.reason == 'waiting-second-feature'
              or isinstance(chosen, _GapConfirmationInvalidated)):
            if not chosen.first_sequence:
                return chosen
            limit = max(chosen.first_sequence[-1].source_indices) - 1
        elif isinstance(chosen, _CandidateExtension):
            limit = chosen.witness_index - 1
        elif isinstance(chosen, tuple):
            retained = self._proofs[(start, chosen[0], direction)]
            if retained.second_sequence:
                limit = max(retained.first_sequence[-1].source_indices) - 1
            else:
                cert = retained.first_pen_continuation
                # A later origin-retest hypothesis must not replace the
                # current standard boundary at the same completion event.
                # Keep the established standard proof on this new-route tie.
                origin_fallback = cert is not None and cert.reference_context == 'origin-retest'
                limit = retained.witness_index - (0 if origin_fallback else 1)
        else:
            return chosen
        if first + 2 > limit:
            return chosen
        alternate = self._try_end_standard_local(
            values, start, direction, first, standard, allow_type2=False,
        )
        if isinstance(alternate, tuple):
            proof = self._proofs[(start, alternate[0], direction)]
            if proof.witness_index <= limit:
                return alternate
        if retained is not None:
            self._proofs[retained.key] = retained
        return chosen

    def _resolve_later_first_search_outcome(self, values, start, direction, first, result,
                                            standard, context, reference, pivot_origins=None,
                                            predecessor=None):
        """L071: an unfinished first break cannot hide a completed later turn.

        Earlier complete gapped fractals retain L067/L078's F2 priority.
        Only a still-incomplete FIRST-case continuation opens this search;
        candidates use the surviving chronological standard left element.
        A first-pen completion delayed beyond its right element also waits
        long enough for a later candidate to finish earlier, so compare the
        actual witness, not the first candidate's position alone.
        """
        if isinstance(result, PendingBoundary) and result.reason == "waiting-first-feature":
            last = len(values) - 1
        elif (isinstance(result, PendingBoundary) and result.reason == "waiting-second-feature"
              or isinstance(result, _GapConfirmationInvalidated)):
            if not result.first_sequence:
                return result
            # L06764-118: a second-case hypothesis first requires a first
            # fractal. It cannot retroactively suppress a completion that
            # already existed before that fractal's right element arrived.
            last = max(result.first_sequence[-1].source_indices) - 1
        elif isinstance(result, _CandidateExtension):
            last = result.witness_index - 1
        elif isinstance(result, tuple):
            proof = self._proofs[(start, result[0], direction)]
            if proof.second_sequence:
                last = max(proof.first_sequence[-1].source_indices) - 1
            # A delayed ordinary F1 can also hide an earlier completed local
            # continuation. Restricting this comparison to explicit raw-break
            # receipts makes a later right shoulder replace an already locked
            # boundary. Every first-case proof competes by its actual witness;
            # a completed or unresolved F2 retains its separate priority.
            else:
                if proof.witness_index <= first + 2:
                    return result
                last = proof.witness_index - 1
        else:
            return result
        if first + 4 > last:
            return result
        refs = list(standard)
        if pivot_origins is None:
            pivot_origins = _contained_pivot_origins(values)
        body_high = max(values[i].high for i in range(start, first, 2))
        body_low = min(values[i].low for i in range(start, first, 2))
        best = None
        for check in range(first + 2, last - 1, 2):
            _append_feature(refs, _bi_to_cs_elem(values[check - 2]), direction)
            body_high = max(body_high, values[check - 1].high)
            body_low = min(body_low, values[check - 1].low)
            candidate = self._try_end_standard_local(
                values, start, direction, check, refs, allow_type2=False,
            )
            # L079's contained original-direction stem can retain its own
            # outside reference, which is different from a fabricated raw
            # replacement for an absorbed ordinary left element.
            if not isinstance(candidate, tuple):
                local = self._try_end_r34(
                    values, start, direction, body_high, body_low, check,
                    max(start, pivot_origins[check - 1]), False,
                )
                if isinstance(local, tuple):
                    proof = self._proofs[(start, local[0], direction)]
                    left = (proof.first_pen_continuation.reference if proof.first_pen_continuation
                            else proof.first_sequence[0])
                    if proof.pivot_stem is not None or (refs and
                            (left.low, left.high) == (refs[-1]['low'], refs[-1]['high'])):
                        candidate = local
            candidate = self._try_first_case_completions(
                values, start, direction, check, candidate, predecessor,
                latest_witness=last,
            )
            candidate = self._try_completed_return_segment(
                values, start, direction, check, candidate, predecessor,
                latest_witness=last,
            )
            if not isinstance(candidate, tuple):
                continue
            proof = self._proofs[(start, candidate[0], direction)]
            if proof.witness_index > last:
                continue
            if best is None or (proof.witness_index, proof.end_index) < (best[0], best[1][0]):
                best = (proof.witness_index, candidate)
        return result if best is None else best[1]

    def _try_first_case_completions(self, values, start, direction, first,
                                    result, predecessor, *, latest_witness=None):
        """Compare all established-predecessor first-case completion routes.

        L071 100-124 supplies the first-break continuation; L078 208-220
        requires the established predecessor and preserves F2 priority.
        Both the main scan and the interior candidate search must consider
        this existing route. A pending first hypothesis or a later independent
        reverse proof must not hide its earlier completion. Equal witnesses
        retain the already selected proof.
        """
        original = result
        result = self._try_completed_reverse_segment(
            values, start, direction, first, result, predecessor,
            latest_witness=latest_witness,
        )
        if predecessor is None or isinstance(original, _GapConfirmationInvalidated):
            return result
        if isinstance(original, PendingBoundary) and original.reason != 'waiting-first-feature':
            return result
        limit = len(values) - 1 if latest_witness is None else latest_witness
        if isinstance(original, _CandidateExtension):
            limit = min(limit, original.witness_index - 1)
        if isinstance(result, tuple):
            current = self._proofs[(start, result[0], direction)]
            if current.second_sequence or current.parent_key is not None:
                return result
            limit = min(limit, current.witness_index - 1)

        # The candidate can share its key with the selected reverse proof.
        # Restore the latter if the candidate loses the witness comparison.
        saved_proofs = dict(self._proofs)
        try:
            candidate = self._try_established_reverse_break(
                values, start, direction, first, predecessor,
            )
            proof = (self._proofs[(start, candidate[0], direction)]
                     if isinstance(candidate, tuple) else None)
        finally:
            self._proofs = saved_proofs
        if proof is not None and proof.witness_index <= limit:
            self._proofs[proof.key] = proof
            return candidate
        return result

    def _try_completed_reverse_segment(self, values, start, direction, first,
                                       result, predecessor, *, latest_witness=None):
        """Transmit an independently complete FIRST-case reverse proof (L078).

        L071's raw-first-end extension is sufficient, not the only route once
        the reverse segment has its own complete break. Preserve L078's prior
        established segment and origin-return order. A pending/invalidated F2
        retains its separate priority. Never let the child assume its parent:
        probe its fixed origin with an empty accepted chain, then inherit that
        exact proof instead of rescanning with the newly confirmed parent.
        """
        if predecessor is None or predecessor.end_index + 1 != start:
            return result
        if first < start + 3 or first + 5 >= len(values):
            return result
        if isinstance(result, _GapConfirmationInvalidated):
            return result
        if isinstance(result, PendingBoundary) and result.reason != 'waiting-first-feature':
            return result
        limit = len(values) - 1 if latest_witness is None else latest_witness
        if isinstance(result, _CandidateExtension):
            limit = min(limit, result.witness_index - 1)
        elif isinstance(result, tuple):
            current = self._proofs[(start, result[0], direction)]
            if current.second_sequence or current.parent_key is not None:
                return result
            # An already complete native proof wins a tie; there is no need
            # to inspect data arriving at or after its actual witness.
            limit = min(limit, current.witness_index - 1)
        reversal, reference = values[first], values[first - 2]
        strong = reversal.low < reference.low if direction == 'up' else reversal.high > reference.high
        endpoint = reversal.start.val
        net = endpoint > values[start].start.val if direction == 'up' else endpoint < values[start].start.val
        if not (strong and net and _overlap(reversal, reference)
                and _overlap(reversal, values[first + 2])):
            return result
        returned_at = self._reverse_extensions[first - 1]
        if returned_at is not None:
            limit = min(limit, returned_at - 1)
        if limit < first + 5:
            return result
        child = self._standalone_first_proof(values, first, limit)
        if child is None:
            return result
        proof = SegmentEvidence(
            start, first - 1, direction, 'completed-reverse-segment', child.witness_index,
            predecessor_key=predecessor.key,
            reverse_segment=ReverseSegmentEvidence(
                first, first - 2, first + 2, predecessor.key, child,
            ),
        )
        self._proofs[proof.key] = proof
        return first - 1, first, proof.witness_index, confirmation_time(values, start, proof.witness_index)

    def _standalone_first_proof(self, values, first, limit):
        """Independent fixed-origin proof; no established parent is supplied."""
        if limit < first + 5:
            return None
        progress = self._reverse_feature_progress[first + 3]
        turn = self._reverse_feature_turns[first + 3]
        if progress is None or progress > limit or turn is None or turn > limit:
            return None
        cache = self._reverse_probe_cache
        cache_key = (first, limit)
        if cache_key not in cache:
            probe = XdCalculator()
            probe._feature_scan_cache = {}
            probe._build_segments(values[:limit + 1], first, first_proof_only=True,
                                  pivot_origins=self._reverse_pivot_origins,
                                  next_extensions=self._reverse_extensions,
                                  feature_progress=self._reverse_feature_progress,
                                  feature_turns=self._reverse_feature_turns)
            cache[cache_key] = probe.evidence[0] if probe.evidence else None
        return cache[cache_key]

    def _try_completed_return_segment(self, values, start, direction, first,
                                      result, predecessor, *, latest_witness=None):
        """L078 160-211: the completed return remains inside the break origin.

        The return begins immediately AFTER the first breaking pen, in the
        original direction. Prove it independently, including its own break;
        three alternating pens alone do not prove that the return has ended.
        It is an internal witness, so do not inherit it as the actual successor.
        Actual successor proofs may use this now-established parent, with their
        clocks bounded by the entire parent's proof. This route can finish
        before the reverse segment itself has an independently completed end.
        """
        if predecessor is None or predecessor.end_index + 1 != start:
            return result
        if first < start + 3 or first + 6 >= len(values):
            return result
        if isinstance(result, _GapConfirmationInvalidated):
            return result
        if isinstance(result, PendingBoundary) and result.reason != 'waiting-first-feature':
            return result
        limit = len(values) - 1 if latest_witness is None else latest_witness
        if isinstance(result, _CandidateExtension):
            limit = min(limit, result.witness_index - 1)
        elif isinstance(result, tuple):
            current = self._proofs[(start, result[0], direction)]
            if current.second_sequence or current.parent_key is not None:
                return result
            limit = min(limit, current.witness_index - 1)
        reversal, reference = values[first], values[first - 2]
        strong = reversal.low < reference.low if direction == 'up' else reversal.high > reference.high
        net = reversal.start.val > values[start].start.val if direction == 'up' else reversal.start.val < values[start].start.val
        if not (strong and net and _overlap(reversal, reference)
                and _overlap(reversal, values[first + 2])):
            return result
        returned_at = self._reverse_extensions[first - 1]
        if returned_at is not None:
            limit = min(limit, returned_at - 1)
        support = self._standalone_first_proof(values, first + 1, limit)
        if support is None:
            return result
        support_chain = (support,)
        if support.second_sequence:
            support_chain += (self._successor_from_second_sequence(support, values),)
        proof = SegmentEvidence(
            start, first - 1, direction, 'completed-return-segment', support.witness_index,
            predecessor_key=predecessor.key,
            return_segment=ReturnSegmentEvidence(
                first, first - 2, first + 2, predecessor.key, support_chain,
            ),
        )
        self._proofs[proof.key] = proof
        return first - 1, first, proof.witness_index, confirmation_time(values, start, proof.witness_index)

    def _try_established_reverse_break(self, values, start, direction, check, predecessor):
        """L071 raw first-break continuation with L078's established predecessor.

        A strong reverse pen can start above/below a contained internal price
        source, so a new local extremum against that source is not mandatory.
        The effective first-ending edge follows same-side inclusion under
        the user-selected rule. Its first exit must precede an origin return.
        The complete predecessor is indispensable;
        an unproved initial observation does not receive this shortcut.
        """
        if predecessor.end_index + 1 != start or check < start + 3 or check + 2 >= len(values):
            return None
        first, left = values[check], values[check - 2]
        strong = first.low < left.low if direction == 'up' else first.high > left.high
        # Ordinary strict-turn cases remain with their feature-sequence rules.
        turns = first.high > left.high if direction == 'up' else first.low < left.low
        if not strong or turns or not _overlap(first, values[check + 2]):
            return None
        origin, endpoint = values[start].start.val, first.start.val
        if not (endpoint > origin if direction == 'up' else endpoint < origin):
            return None
        scan = self._scan_first_feature(values, check, direction, first.start.val)
        literal = self._first_continuation_proof(
            values, start, direction, check, _bi_to_cs_elem(left),
            reference_context='established', predecessor=predecessor,
        )
        if literal is not None and (not isinstance(scan, tuple)
                or literal.witness_index < _elem_farthest_bi_index(scan[1])):
            return self._commit_first_continuation(values, literal)
        if not isinstance(scan, tuple):
            return scan
        middle, right = scan
        outward = (right['high'] < middle['high'] and right['low'] < middle['low']
                   if direction == 'up' else
                   right['low'] > middle['low'] and right['high'] > middle['high'])
        if not outward:
            return None
        witness = _elem_farthest_bi_index(right)
        gap = _classify_first_feature_gap(_bi_to_cs_elem(left), first, middle, direction)
        if gap.effective_gap:
            return None
        completion = self._first_pen_break_completion(values, first, direction)
        if not isinstance(completion, FirstPenBreakEvidence):
            return completion
        witness = max(witness, completion.extension_index)
        proof = SegmentEvidence(
            start, check - 1, direction, 'established-predecessor-reverse-break', witness,
            tuple(FeatureEvidence.from_element(e) for e in (_bi_to_cs_elem(left), middle, right)),
            gap_context=gap, first_break_witness_index=completion.extension_index, predecessor_key=predecessor.key,
            first_break_evidence=completion,
            direct_first_break=DirectFirstBreakEvidence(
                check, check - 2, check + 2, completion.extension_index, predecessor.key,
            ),
        )
        self._proofs[proof.key] = proof
        return check - 1, check, witness, confirmation_time(values, start, witness)

    def _try_end_r34(
        self, all_bis, seg_start, seg_type, seg_high, seg_low, check,
        pivot_start=None, allow_type2=True,
    ) -> (
        tuple[int, int, int, object]
        | PendingBoundary
        | _CandidateExtension
        | _GapConfirmationInvalidated
        | None
    ):
        """L071 first-pen break followed by its own developing reverse stem.

        L077/L079 allow a non-extreme turn: the local first reverse pen must
        break the preceding feature's end, not the whole segment origin.
        The reviewed equal-origin case is separate: an initial one-pen high/
        low could not be a segment end. A later qualified return to that price
        can use a strictly lower/higher local shoulder and an overlapping first
        reversal. This does not relax the L079 internal-extreme stem rule.
        Contained following elements remain in this same stem. They cannot
        be skipped to seek a distant, unrelated break. A return beyond this
        turn invalidates it; a non-contained outward element completes it.
        If all following elements remain contained, preserve the pending
        context instead of retrying every interior turn. If the first escape
        is an original-direction extension, resume at that physical witness.

        A still-unbroken same-direction contained stem can move the candidate
        inside its old extreme (L079 lower figure). Its outside left reference
        remains eligible; an intervening interior pen cannot replace it. The
        stem is retained as explicit source evidence, with no physical edits.
        """
        if check < seg_start + 3 or check >= len(all_bis):
            return None
        left, first = all_bis[check - 2], all_bis[check]
        origin_retest = (
            first.high == all_bis[seg_start].high and seg_high <= first.high
            if seg_type == "up"
            else first.low == all_bis[seg_start].low and seg_low >= first.low
        )

        def breaks_reference(reference):
            strong = first.low < reference.low if seg_type == "up" else first.high > reference.high
            return strong or (origin_retest and _overlap(first, reference))

        breaks = breaks_reference(left)
        # Boundary protection (L071) preserves a strong reversal's inclusion;
        # it does not waive the directional extremum of the fractal (L067).
        # L079 permits an endpoint below an earlier INTERNAL high, while the
        # local middle must still be strictly above its left/right shoulders.
        turns = first.high > left.high if seg_type == "up" else first.low < left.low
        stem_start = None
        if not turns or not breaks:
            if pivot_start is None:
                # Private callers may omit the precomputed index. Production
                # supplies it, so deeply nested candidates do not rescan it.
                pivot_start = check - 1
                while pivot_start - 2 >= seg_start:
                    before, after = all_bis[pivot_start - 2], all_bis[pivot_start]
                    if not (before.low <= after.low and after.high <= before.high):
                        break
                    pivot_start -= 2
            if pivot_start <= seg_start or pivot_start == check - 1:
                return None
            # This stem explains the left reference, not the actual boundary.
            # Its earliest price supplier may precede this newly valid break
            # context. The post-boundary middle feature supplies the endpoint;
            # equality with an interior stem high/low cannot veto that proof.
            left = all_bis[pivot_start - 1]
            turns = first.high > left.high if seg_type == "up" else first.low < left.low
            breaks = breaks_reference(left)
            if not turns or not breaks:
                return None
            stem_start = pivot_start
        real_end = check - 1
        anchor, endpoint = all_bis[seg_start].start.val, all_bis[real_end].end.val
        if not (endpoint > anchor if seg_type == "up" else endpoint < anchor):
            return None
        if check + 2 >= len(all_bis):
            # A qualified first break is a named hypothesis while its right
            # evidence is absent (L071 76-124 / L079 286-295). It is not a
            # completed fractal. An already observed return beyond its origin
            # invalidates it even before the third pen can arrive.
            if check + 1 < len(all_bis):
                next_same = all_bis[check + 1]
                if (next_same.high > first.high if seg_type == "up" else next_same.low < first.low):
                    return _CandidateExtension(check + 1)
            return PendingBoundary("waiting-first-feature", real_end)
        if not _overlap(first, all_bis[check + 2]):
            return None
        scan = self._scan_first_feature(
            all_bis, check, seg_type, first.high if seg_type == "up" else first.low
        )
        literal = self._first_continuation_proof(
            all_bis, seg_start, seg_type, check, _bi_to_cs_elem(left),
            reference_context='local', pivot_start=stem_start,
        )
        if literal is not None and (not isinstance(scan, tuple)
                or literal.witness_index < _elem_farthest_bi_index(scan[1])):
            return self._commit_first_continuation(all_bis, literal)
        if not isinstance(scan, tuple):
            return scan
        middle, element = scan
        i = element["bi"].index
        outward = (
            element["high"] < middle["high"] and element["low"] < middle["low"]
            if seg_type == "up"
            else element["low"] > middle["low"] and element["high"] > middle["high"]
        )
        if not outward:
            return None
        completion = self._complete_first_fractal(all_bis, _bi_to_cs_elem(left), middle, element, seg_type)
        if not isinstance(completion, tuple):
            return completion
        continuation_witness, first_break_witness, first_break_evidence = completion
        elements = tuple(
            FeatureEvidence.from_element(e)
            for e in (_bi_to_cs_elem(left), middle, element)
        )
        gap_context = _classify_first_feature_gap(
            _bi_to_cs_elem(left), first, middle, seg_type
        )
        has_gap = gap_context.effective_gap
        second = ()
        second_breaks = ()
        witness = max(i, continuation_witness)
        if has_gap:
            if not allow_type2:
                return None
            status, witness2 = self._check_type2(all_bis, middle, seg_type)
            if status == _TYPE2_PENDING:
                return PendingBoundary("waiting-second-feature", real_end, elements)
            if witness2 is None:
                raise ValueError("type-2 resolution requires a causal witness")
            if status == _TYPE2_INVALIDATED:
                return _GapConfirmationInvalidated(
                    all_bis[witness2].locked_at, witness2, real_end, elements
                )
            witness = max(witness, witness2)
            second = self._second_evidence
            second_breaks = self._second_breaks
        pivot_stem = None
        if stem_start is not None:
            stem = _bi_to_cs_elem(all_bis[stem_start])
            fold_direction = "down" if seg_type == "up" else "up"
            for j in range(stem_start + 2, check, 2):
                stem = _merge_two(stem, _bi_to_cs_elem(all_bis[j]), fold_direction)
            pivot_stem = FeatureEvidence.from_element(stem)
        strong_break = first.low < left.low if seg_type == "up" else first.high > left.high
        local_rule = "origin-extreme-retest" if origin_retest and not strong_break else "first-pen-break"
        proof = SegmentEvidence(
            seg_start,
            real_end,
            seg_type,
            "second-feature-fractal" if has_gap else local_rule,
            witness,
            elements,
            second,
            initial_gap=has_gap,
            pivot_stem=pivot_stem,
            gap_context=gap_context,
            second_sequence_breaks=second_breaks,
            first_break_witness_index=first_break_witness,
            first_break_evidence=first_break_evidence,
        )
        self._proofs[proof.key] = proof
        return real_end, check, i, confirmation_time(all_bis, seg_start, witness)

    def _freeze_confirmed_candidate(self, segs, locked_candidates) -> None:
        """完整破坏证据出现即冻结，不能另加固定数量的后继段延迟。

        _try_end 检查两种特征分型；_try_end_r34 检查第一笔破坏后沿
        同一候选的包含与后续破坏。formed_at 为 None 时仍有待定证据。
        """
        if not segs:
            return
        candidate = segs[-1]
        formed_at = candidate[3]
        if formed_at is None:
            return
        key = tuple(candidate[:3])
        locked_candidates.setdefault(key, formed_at)

    def _emit_segments(
        self,
        all_bis,
        segs,
        pending_tail,
        start,
        locked_candidates,
    ):
        """输出已确认前缀及证据尚未完整的尾部，再补正在形成的末段。"""
        for s, e, t, formed_at in segs:
            locked_at = locked_candidates.get((s, e, t))
            self._make_xd(
                all_bis[s : e + 1],
                t,
                done=locked_at is not None,
                locked_at=locked_at,
                formed_at=formed_at,
            )
        if pending_tail is not None:
            if segs:
                # 临时单元仍参与严格结构预览，因此必须与锁定单元保持相同的连续性和
                # 方向交替约定。对每个 ``pending_tail`` 生产者都保留这一规范化防御边界。
                expected_start = segs[-1][1] + 1
                expected_type = "down" if segs[-1][2] == "up" else "up"
                pending_tail = (expected_start, expected_type)
            self._emit_pending(all_bis, pending_tail[0], pending_tail[1])
        elif segs:
            # 内层被 _try_end 命中直至数据末尾:在最后段之后补末段未完成线段
            pstart = segs[-1][1] + 1
            if pstart < len(all_bis):
                ptype = "down" if segs[-1][2] == "up" else "up"
                already = (
                    bool(self.xds)
                    and (not self.xds[-1].done)
                    and self.xds[-1].type == ptype
                )
                if not already:
                    self._emit_pending(all_bis, pstart, ptype)
        elif start < len(all_bis):
            self._emit_pending(all_bis, start, all_bis[start].type)

    # ----------------------------------------------------------
    # 尝试结束线段。
    # ----------------------------------------------------------
    def _first_pen_break_completion(self, all_bis, first, direction):
        """First exit from the evolving effective interval, in observed order.

        User-selected interpretation, 2026-09-21: subsequent same-side
        inclusion updates the end used for continuation. Inspect an incoming
        pen against the interval that existed before it; a witnessed exit
        cannot be erased by including that very pen afterward. The candidate
        origin remains protected until an earlier completion or strict return.

        Reuse suffix states only within the current input, like feature scans.
        The separate key tag prevents mixing first-break and fractal scans.
        """
        up = direction == 'up'
        low, high = first.low, first.high
        low_source = high_source = first.index
        cache = getattr(self, '_feature_scan_cache', None)
        visited, outcome = [], None
        for i in range(first.index + 1, len(all_bis)):
            if cache is not None:
                key = ('first-break', i, direction, low, high)
                try:
                    cached = cache.get(key)
                except TypeError:
                    cache = None
                    visited = []
                else:
                    if cached is not None:
                        if cached.status == 'completed':
                            low_source = low_source if cached.low == low else cached.low_source
                            high_source = high_source if cached.high == high else cached.high_source
                            low, high = all_bis[low_source].low, all_bis[high_source].high
                        outcome = _FeatureTail(cached.status, cached.stop, low, high,
                                               low_source, high_source)
                        break
                    visited.append(key)
            item = all_bis[i]
            if item.type == direction:
                if item.high > high if up else item.low < low:
                    outcome = _FeatureTail('extension', i, low, high, low_source, high_source)
                    break
                continue
            if item.end.val < low if up else item.end.val > high:
                outcome = _FeatureTail('completed', i, low, high, low_source, high_source)
                break
            # Prior to this first escape, the incoming interval is contained
            # in the protected same-side element. Equal prices retain the
            # earlier physical supplier and its exact numeric representation.
            if item.low > low if up else item.low < low:
                low, low_source = item.low, i
            if item.high > high if up else item.high < high:
                high, high_source = item.high, i
        if outcome is None:
            outcome = _FeatureTail('pending', None, low, high, low_source, high_source)
        if cache is not None:
            for key in visited:
                cache[key] = outcome
        if outcome.status == 'pending':
            return PendingBoundary('waiting-first-feature', first.index - 1)
        if outcome.status == 'extension':
            return _CandidateExtension(outcome.stop)
        return FirstPenBreakEvidence(
            first.index,
            FeatureEvidence(outcome.low, outcome.high,
                            tuple(range(first.index, outcome.stop, 2)),
                            outcome.low_source, outcome.high_source),
            outcome.stop,
        )

    def _complete_first_fractal(self, all_bis, left, middle, right, direction):
        """Distinguish a full fractal from L071's covering-first-pen break.

        A protected covering reversal can have only the turning coordinate
        of an ordinary fractal. In that exceptional case, it must actually
        continue beyond its effective first element under the user's selected
        interpretation. Containment alone is not completion. Full-coordinate
        fractals keep their own proof; local reference qualification is not
        broadened, preserving L079's unfinished lower figure.
        """
        up = direction == "up"
        full = (middle['low'] > max(left['low'], right['low']) if up
                else middle['high'] < min(left['high'], right['high']))
        if full:
            return _elem_farthest_bi_index(right), None, None
        first = _resolve_pivot_bi(middle, direction)
        covering = (first.high > left['high'] and first.low <= left['low'] if up
                    else first.low < left['low'] and first.high >= left['high'])
        if not covering:
            return None
        completion = self._first_pen_break_completion(all_bis, first, direction)
        if isinstance(completion, FirstPenBreakEvidence):
            return (max(completion.extension_index, _elem_farthest_bi_index(right)),
                    completion.extension_index, completion)
        return completion

    def _origin_context_before(self, values, start, first, direction):
        """Price-only standard prefix before a qualified origin retest.

        A surviving standard shoulder owns its ordinary/F2 classification;
        an absorbed raw shoulder must not bypass that gap. Prefix summaries
        also avoid rescanning a long equal-price body for every local turn.
        No future outcome or completed proof is cached here.
        """
        cache = getattr(self, '_origin_context_prefixes', None)
        if cache is None:
            cache = self._origin_context_prefixes = {}
        key = (id(values), start, direction)
        if key not in cache:
            cache[key] = {'next': start + 1, 'elements': [], 'before': {},
                          'extreme': values[start].end.val}
        state = cache[key]
        if first not in state['before']:
            up = direction == 'up'
            while state['next'] <= first:
                index = state['next']
                elements = state['elements']
                last = elements[-1] if elements else None
                state['before'][index] = (len(elements), last, state['extreme'])
                item = values[index]
                new = (item.low, item.high)
                if elements:
                    old = elements[-1]
                    included = (old[0] <= new[0] and new[1] <= old[1]
                                or new[0] <= old[0] and old[1] <= new[1])
                    if included:
                        rising = up if len(elements) == 1 else old[1] > elements[-2][1]
                        choose = max if rising else min
                        elements[-1] = (choose(old[0], new[0]), choose(old[1], new[1]))
                    else:
                        elements.append(new)
                else:
                    elements.append(new)
                state['extreme'] = (max(state['extreme'], item.high) if up
                                    else min(state['extreme'], item.low))
                state['next'] += 2
        return state['before'][first]

    def _first_continuation_proof(self, values, start, direction, first, reference,
                                      *, reference_context='record', predecessor=None,
                                      pivot_start=None):
        """Effective first-break completion, independent of a separate right.

        A retrace exactly to the breaking pen's origin can make an extending
        pen contain the previous effective interval. Including that witness
        afterward must not erase an already completed FIRST-case break.
        L065 226-244 includes contact with the near edge in stroke destruction;
        it does not require crossing the reference's far edge. For an already
        qualified ordinary reference, retain that case as well. L067 49-118
        still requires the normalized first feature to be ungapped. Local and
        established reference qualification stays with the caller's context.
        """
        if first < start + 3 or first + 2 >= len(values):
            return None
        raw = values[first]
        up = direction == 'up'
        strong = raw.low < reference['low'] if up else raw.high > reference['high']
        turns = raw.high > reference['high'] if up else raw.low < reference['low']
        if not (_overlap(raw, reference) and _overlap(raw, values[first + 2])):
            return None
        net = raw.start.val > values[start].start.val if up else raw.start.val < values[start].start.val
        if not net:
            return None
        if reference_context == 'established':
            if not strong or predecessor is None or predecessor.end_index + 1 != start or turns:
                return None
        elif not turns:
            return None
        elif reference_context != 'record' and not strong:
            if reference_context != 'local' or predecessor is not None:
                return None
            # _try_end_r34 already admits a retest of the initial extreme.
            # Its completed continuation must also survive an equal-origin
            # retrace; do not apply this to arbitrary contained local turns.
            origin_retest = raw.high == values[start].high if up else raw.low == values[start].low
            if not origin_retest:
                return None
            reference_context = 'origin-retest'
        receipt = self._first_pen_break_completion(values, raw, direction)
        if not isinstance(receipt, FirstPenBreakEvidence):
            return None
        if reference_context == 'origin-retest':
            count, standard_left, body_extreme = self._origin_context_before(values, start, first, direction)
            if body_extreme > raw.high if up else body_extreme < raw.low:
                return None
            if count >= 2 and (raw.high > standard_left[1] if up else raw.low < standard_left[0]):
                # The normal standard route already handles its completed F1
                # or waits for its required F2. This local fallback cannot
                # replace that reference, including after inclusion opens a gap.
                return None
        if reference_context in {'record', 'origin-retest'}:
            middle = {'low': receipt.effective_first.low, 'high': receipt.effective_first.high}
            if _classify_first_feature_gap(reference, raw, middle, direction).effective_gap:
                return None
        completion = receipt.extension_index
        stem = None
        if pivot_start is not None:
            element = _bi_to_cs_elem(values[pivot_start])
            for i in range(pivot_start + 2, first, 2):
                element = _merge_two(element, _bi_to_cs_elem(values[i]), 'down' if up else 'up')
            stem = FeatureEvidence.from_element(element)
        return SegmentEvidence(
            start, first - 1, direction, 'first-pen-continuation', completion,
            pivot_stem=stem,
            predecessor_key=predecessor.key if predecessor is not None else None,
            first_pen_continuation=FirstPenContinuationEvidence(
                first, first + 2, completion, FeatureEvidence.from_element(reference), reference_context,
            ),
            first_break_witness_index=completion,
            first_break_evidence=receipt,
        )

    def _commit_first_continuation(self, values, proof):
        self._proofs[proof.key] = proof
        first = proof.end_index + 1
        return proof.end_index, first, proof.witness_index, confirmation_time(values, proof.start_index, proof.witness_index)

    def _try_end(
        self,
        all_bis,
        seg_start,
        seg_end,
        seg_type,
        seg_high,
        seg_low,
        check_pos,
        seg_cs_bis_cache: List[BI | dict],
        allow_type2: bool = True,
    ) -> (
        tuple[int, int, int, object]
        | PendingBoundary
        | _CandidateExtension
        | _GapConfirmationInvalidated
        | None
    ):
        """尝试用反向特征序列分型判定当前线段是否终结。

        命中返回 (实际终点笔位置, 反向起点, 已观察范围, 确认时间)。
        尚未齐备、继续延伸、第二种候选被否定分别返回带语义的状态对象。
        """
        cs_bi_type = "down" if seg_type == "up" else "up"
        frac_name = "顶分型" if seg_type == "up" else "底分型"

        # The caller advances this one reference as it consumes original pens.
        # Accept older private callers' lists too, without building an unused
        # standard prefix. Only the last strict record supplies the left role.
        if not seg_cs_bis_cache:
            _log.debug(lambda: "    _try_end: 段内无CS笔 → 跳过")
            return None
        reference = seg_cs_bis_cache[0]
        for bi in seg_cs_bis_cache[1:]:
            reference = _advance_left_reference(reference, bi, seg_type)
        if check_pos >= len(all_bis) or all_bis[check_pos].type != cs_bi_type:
            return None
        # L071 protects the hypothesized boundary: the left reference cannot
        # merge with the first post-boundary element. Those on the same side
        # remain subject to chronological inclusion.
        first_elem = (
            reference.copy()
            if isinstance(reference, dict)
            else _bi_to_cs_elem(reference)
        )
        # This candidate cannot become an ordinary fractal. Any later source
        # improving the middle beyond this left reference would first cross
        # seg_high/seg_low and extend the candidate. Let the chronological
        # caller examine local contexts instead of repeatedly normalizing a
        # long contained tail whose middle is already ineligible (L081).
        first = all_bis[check_pos]
        if (
            first.high <= first_elem["high"] and seg_high <= first_elem["high"]
            if seg_type == "up"
            else first.low >= first_elem["low"] and seg_low >= first_elem["low"]
        ):
            return None
        scan = self._scan_first_feature(
            all_bis, check_pos, seg_type, seg_high if seg_type == "up" else seg_low
        )
        literal = self._first_continuation_proof(
            all_bis, seg_start, seg_type, check_pos, first_elem,
        )
        if literal is not None and (not isinstance(scan, tuple)
                or literal.witness_index < _elem_farthest_bi_index(scan[1])):
            return self._commit_first_continuation(all_bis, literal)
        if not isinstance(scan, tuple):
            return scan
        mid, right = scan
        left = first_elem
        look_elems = [left, mid, right]
        gap_context = _classify_first_feature_gap(left, first, mid, seg_type)
        has_gap = gap_context.effective_gap
        if has_gap and not allow_type2:
            return None
        if seg_type == "up":
            is_frac = mid["high"] > left["high"] and mid["high"] > right["high"]
        else:
            is_frac = mid["low"] < left["low"] and mid["low"] < right["low"]
        if not is_frac:
            _log.debug(lambda: f"    _try_end: 固定三元素不构成{frac_name}")
            return None
        completion = self._complete_first_fractal(all_bis, left, mid, right, seg_type)
        if not isinstance(completion, tuple):
            return completion
        continuation_witness, first_break_witness, first_break_evidence = completion
        _log.debug(
            lambda: (
                f"    _try_end: {[_elem_label(e) for e in look_elems]} → {frac_name}"
            )
        )

        # ---- 步骤5 ----
        mid_elem = mid
        if has_gap:
            _log.debug(lambda: "    _try_end: 第二种情况,进入_check_type2验证...")
            first_features = tuple(FeatureEvidence.from_element(e) for e in look_elems)
            type2_status, type2_witness_idx = self._check_type2(
                all_bis,
                mid_elem,
                seg_type,
            )
            if type2_status == _TYPE2_PENDING:
                _log.debug(lambda: "    _try_end: 第二特征序列尚未完成 → 保持pending")
                return PendingBoundary(
                    "waiting-second-feature",
                    _resolve_pivot_bi(mid_elem, seg_type).index - 1,
                    first_features,
                )
            if type2_status == _TYPE2_INVALIDATED:
                _log.debug(
                    lambda: "    _try_end: 第二特征序列被原段新极值否定 → 返回None"
                )
                if type2_witness_idx is None:
                    raise ValueError("type-2 invalidation requires a causal witness")
                witnessed_at = all_bis[type2_witness_idx].locked_at
                return _GapConfirmationInvalidated(
                    witnessed_at,
                    type2_witness_idx,
                    _resolve_pivot_bi(mid_elem, seg_type).index - 1,
                    first_features,
                )
            _log.debug(lambda: "    _try_end: _check_type2成功")
        else:
            type2_witness_idx = None

        # ---- 步骤6: 定位当前线段结束位置 + 反向线段范围 ----
        target_bi = _resolve_pivot_bi(mid_elem, seg_type)

        # bi.index 恒等于其在 all_bis 中的位置(BiCalculator 重建尾部时保证),
        # 故直接用 .index 代替原 _bi_pos[id(bi)] 映射,省去每次 calculate O(B) 重建。
        end_bi_idx = target_bi.index - 1
        if end_bi_idx <= seg_start or end_bi_idx >= len(all_bis):
            _log.debug(
                lambda: (
                    f"    _try_end: end_bi_idx={end_bi_idx} 越界(seg_start={seg_start},len={len(all_bis)}) → 返回None"
                )
            )
            return None
        if all_bis[end_bi_idx].type != seg_type:
            _log.debug(
                lambda: (
                    f"    _try_end: 终点笔{_bi_label(all_bis[end_bi_idx])} 方向≠{seg_type} → 返回None"
                )
            )
            return None
        if end_bi_idx - seg_start + 1 < 3:
            _log.debug(
                lambda: f"    _try_end: 笔数{end_bi_idx - seg_start + 1}<3 → 返回None"
            )
            return None

        # L077-L079: use the actual proven boundary, even if an internal
        # extremum is more extreme. Never move it to a different price witness.
        seg_anchor_val = all_bis[seg_start].start.val
        seg_end_val = all_bis[end_bi_idx].end.val
        if seg_type == "up" and not (seg_end_val > seg_anchor_val):
            _log.debug(
                lambda: (
                    f"    _try_end: up段终点{seg_end_val}≤起点{seg_anchor_val} 方向矛盾 → 返回None"
                )
            )
            return None
        if seg_type == "down" and not (seg_end_val < seg_anchor_val):
            _log.debug(
                lambda: (
                    f"    _try_end: down段终点{seg_end_val}≥起点{seg_anchor_val} 方向矛盾 → 返回None"
                )
            )
            return None

        # 反向线段: 起点=当前线段终点+1, 终点=look_elems中最远的CS笔位置
        next_start = end_bi_idx + 1
        # look_elems 的最后一个元素对应反向线段已探明的最远同向笔
        last_look = look_elems[-1]
        last_look_bis = last_look.get("merged_bis")
        if last_look_bis:
            next_end = max(b.index for b in last_look_bis)
        else:
            next_end = last_look["bi"].index
        # 确保 next_end 至少为 next_start + 2（最少3笔）
        next_end = (
            max(next_end, next_start + 2) if next_end >= next_start else next_start + 2
        )

        _log.debug(
            lambda: (
                f"    _try_end: ✓ 线段结束于{_bi_label(all_bis[end_bi_idx])}, "
                f"反向线段 bi[{all_bis[next_start].index}]~bi[{all_bis[min(next_end, len(all_bis) - 1)].index}]"
            )
        )
        # 无缺口分支以特征序列分型右肩为见证；有缺口分支还必须纳入第二
        # 特征序列分型实际检查到的最远笔。主循环在调用本函数前还读取了
        # check_pos + 1 这根同向笔以排除“继续创新高/低而只是延伸”，因此它也
        # 是不可省略的因果见证。若该笔尚未锁定，几何可以投影，但不得把更早
        # 的历史时间回填成 XD.locked_at。
        witness_idx = _elem_farthest_bi_index(right)
        witness_idx = max(witness_idx, continuation_witness)
        if type2_witness_idx is not None:
            witness_idx = max(witness_idx, type2_witness_idx)
        witness_idx = max(witness_idx, check_pos + 1)
        if next_start + 2 >= len(all_bis) or not _overlap(
            all_bis[next_start], all_bis[next_start + 2]
        ):
            return None
        features = tuple(FeatureEvidence.from_element(e) for e in (left, mid, right))
        proof = SegmentEvidence(
            seg_start,
            end_bi_idx,
            seg_type,
            "second-feature-fractal" if has_gap else "first-feature-fractal",
            witness_idx,
            features,
            getattr(self, "_second_evidence", ()) if has_gap else (),
            initial_gap=has_gap,
            gap_context=gap_context,
            second_sequence_breaks=getattr(self, "_second_breaks", ()) if has_gap else (),
            first_break_witness_index=first_break_witness,
            first_break_evidence=first_break_evidence,
        )
        self._proofs[proof.key] = proof
        formed_at = confirmation_time(all_bis, seg_start, witness_idx)
        return end_bi_idx, next_start, next_end, formed_at

    # ----------------------------------------------------------
    # 检查第二种情况。
    # ----------------------------------------------------------
    @staticmethod
    def _raw_first_break_followthrough(all_bis, middle, original_direction):
        """Resolve the physical turn's own continuation after same-side inclusion.

        A/C have original_direction; the standard middle locates B's turn.
        A standard F2 can combine prices from both sides of B's actual pivot.
        Its outgoing C must still develop on its own side of that pivot. Same-
        side containment updates the continuation edge (L065/L079); requiring
        a new raw first-pen extreme would wrongly reject internal extrema.
        Returning through the pivot first rejects this B turn, without
        rejecting A's still-pending second-sequence search.

        This combines L065/L071 with L077 367-391/L078 208-211 and L079. Strict
        second-sequence inclusion is preserved; no third fractal is required.
        """
        successor_direction = "down" if original_direction == "up" else "up"
        first = _resolve_pivot_bi(middle, successor_direction)
        down = original_direction == "down"
        edge = first.end.val
        for i in range(first.index + 1, len(all_bis)):
            pen = all_bis[i]
            if pen.type == first.type:
                if pen.end.val < edge if down else pen.end.val > edge:
                    return "directional-extension", i, first.index
                # Before this first outward element, every same-direction
                # pen is contained in the current context. Its opposite
                # edge follows max(low) for down pens / min(high) for up pens.
                edge = pen.end.val
            elif pen.high > first.high if down else pen.low < first.low:
                return "origin-return", i, first.index
        return "pending", None, first.index

    def _check_type2(self, all_bis, mid_elem, seg_type) -> tuple[str, Optional[int]]:
        """返回第二种情况状态及最远因果见证笔序号。"""
        target_bi = _resolve_pivot_bi(mid_elem, seg_type)

        start_pos = target_bi.index + 1
        if start_pos >= len(all_bis):
            _log.debug(lambda: f"      _check_type2: start_pos={start_pos}越界 → False")
            return _TYPE2_PENDING, None

        cs2_type = "up" if seg_type == "up" else "down"
        cs2_dir = "down" if seg_type == "up" else "up"
        frac2_name = "底分型" if seg_type == "up" else "顶分型"

        _log.debug(
            lambda: (
                f"      _check_type2: 从{_bi_label(target_bi)}之后开始, 寻找反向线段CS({cs2_type}笔)的{frac2_name}"
            )
        )

        def _is_tail_fractal(elems: List[dict]) -> bool:
            """O(1) 检查最后三个元素是否构成反向段所需的分型。
            up 段的反向段找底分型(mid.low < 两侧)；down 段反向段找顶分型(mid.high > 两侧)。
            只检查尾部三元素：每次新追加/合并 cs2_elems 后调用一次即可，
            等价于原 find_frac2(cs2_elems) 的语义但耗时从 O(k) 降到 O(1)。
            """
            if len(elems) < 3:
                return False
            a, b, c = elems[-3], elems[-2], elems[-1]
            if seg_type == "up":  # 反向 = down 段 → 找底分型
                return (
                    b["low"] < a["low"]
                    and b["low"] < c["low"]
                    and b["high"] < a["high"]
                    and b["high"] < c["high"]
                )
            else:  # 反向 = up 段   → 找顶分型
                return (
                    b["high"] > a["high"]
                    and b["high"] > c["high"]
                    and b["low"] > a["low"]
                    and b["low"] > c["low"]
                )

        self._second_evidence = ()
        self._second_breaks = ()
        break_evidence = []
        ignored_until = -1
        cs2_elems = []
        i = start_pos
        while i < len(all_bis):
            bi = all_bis[i]
            # 在当前候选笔路径上继续判断几何；_try_end 与主循环单独
            # 检查这些证据的 locked_at，缺失时输出待确认线段。

            # 原线段严格创新高/低检查（> / <，等价不算创新极值）。
            # 直接 return False 会让"等价新高 + 跨段后续创新高"误判为段延伸，
            # 故先把创新高/低的笔收进 cs2_elems 再判定分型，分型成立才认反向段。
            is_strict_new_extreme = (
                seg_type == "up" and bi.type == "up" and bi.high > target_bi.high
            ) or (seg_type == "down" and bi.type == "down" and bi.low < target_bi.low)

            if bi.type == cs2_type:
                # 包含处理
                new_elem = _bi_to_cs_elem(bi)
                _append_feature(cs2_elems, new_elem, cs2_dir)

                # 每次添加/合并后立即检查分型（仅看尾部三元素，O(1)）
                if i >= ignored_until and _is_tail_fractal(cs2_elems):
                    fractal = tuple(
                        FeatureEvidence.from_element(e) for e in cs2_elems[-3:]
                    )
                    outcome, resolved_at, first_index = self._raw_first_break_followthrough(
                        all_bis, cs2_elems[-2], seg_type
                    )
                    if outcome == "pending":
                        return _TYPE2_PENDING, None
                    if outcome == "origin-return" or resolved_at > i:
                        break_evidence.append(SecondFeatureBreakEvidence(
                            fractal, first_index, first_index - 2, i, outcome, resolved_at
                        ))
                    if outcome == "origin-return":
                        # L071: the attempted successor did not develop. Keep
                        # every physical pen in A's F2 normalization, but do
                        # not borrow an interior turn from the failed context.
                        ignored_until = max(i, resolved_at)
                    else:
                        self._second_evidence = fractal
                        self._second_breaks = tuple(break_evidence)
                        return _TYPE2_CONFIRMED, max(i, resolved_at)

                # 收完后才判断"严格创新极值停止"：
                # 此根 cs 笔创了原段方向的新极值，后续走势不可能再形成本段的反向段，
                # 必须立即停止扫描；反向段是否成立由已收集的 cs2_elems 决定。
                if is_strict_new_extreme:
                    _log.debug(
                        lambda: (
                            f"      _check_type2: {_bi_label(bi)} 创新极值且已收进 cs2_elems → 停止扫描"
                        )
                    )
                    return _TYPE2_INVALIDATED, i
            elif is_strict_new_extreme:
                # 非 cs2 笔但创了新极值（兜底）：原段延伸，反向段不成立
                _log.debug(
                    lambda: (
                        f"      _check_type2: {_bi_label(bi)} 非CS笔但创新极值 → 原线段延伸,False"
                    )
                )
                return _TYPE2_INVALIDATED, i
            # 注：原此处对每根非 cs2 笔重复 find_frac2(cs2_elems) 的 elif 分支已删除——
            # 分型只可能在新元素加入/合并时产生新结构，非 cs2 笔不会改变 cs2_elems，
            # 重复检查纯属冗余且产生大量噪音日志。
            i += 1

        if len(cs2_elems) < 3:
            _log.debug(
                lambda: f"      _check_type2: cs2_elems仅{len(cs2_elems)}个<3 → False"
            )
            return _TYPE2_PENDING, None
        # Every appended feature was checked above, including the raw first-
        # break outcome. A bare final fractal must not bypass that decision.
        return _TYPE2_PENDING, None

    # ----------------------------------------------------------
    # 输出线段
    # ----------------------------------------------------------
    def _make_xd(
        self,
        seg_bis: List[BI],
        seg_type: str,
        done: bool,
        locked_at=None,
        formed_at=None,
    ) -> XD:
        """构造并追加 XD 对象（_emit_segment 与 _emit_pending 的公共逻辑）。

        Args:
            seg_bis: 组成该线段的笔列表（首笔=段起点，末笔=段终点）
            seg_type: 'up' / 'down'
            done: 是否为已完成段

        第 78 课要求后续分析关心实际区间。兼容字段 zs_high/zs_low 与
        high/low 都保留全部组成笔的极值；确认状态不能把区间缩成端点价。

        Returns:
            构造好的 XD 对象（已 append 到 self.xds）
        """
        xd = XD(
            start=seg_bis[0].start,
            end=seg_bis[-1].end,
            start_line=seg_bis[0],
            end_line=seg_bis[-1],
            _type=seg_type,
            index=len(self.xds),
        )
        xd.high = max(bi.high for bi in seg_bis)
        xd.low = min(bi.low for bi in seg_bis)
        done = bool(
            done
            and locked_at is not None
            and confirmation_time(seg_bis, 0, len(seg_bis) - 1) is not None
        )
        xd.zs_high, xd.zs_low = xd.high, xd.low
        xd.done = done
        xd.locked_at = locked_at if done else None
        xd.formed_at = formed_at
        xd.construction_evidence = self._proofs.get(
            (seg_bis[0].index, seg_bis[-1].index, seg_type)
        )
        self.xds.append(xd)
        return xd

    def _emit_pending(self, all_bis, start, seg_type):
        """Preview a directional extreme or the current same-direction tail.

        L079's unfinished lower figure can have a non-extreme live endpoint.
        Preserve that preview when its current endpoint has the right direction.
        If it crosses the origin, do not search backwards for a historical
        endpoint merely to emit a line. Keep an explicit unresolved interval
        outside XD units instead. This adds no new segment-end rule.
        """
        state = self.tail_state
        if state.start_index != start or state.direction != seg_type:
            state = SegmentTail(start, seg_type, "forming")

        def projection(reason, index=None):
            self.tail_state = replace(
                state, projection_reason=reason, projection_index=index,
                observed_end_index=len(all_bis) - 1 if all_bis else None,
            )

        candidates = all_bis[start:]
        if len(candidates) < 3:
            projection("insufficient-pens")
            return
        if not _overlap(candidates[0], candidates[2]):
            projection("initial-pens-not-overlapping")
            return
        best_idx = None
        last_same_idx = None
        minimum_offset = max(2, (state.minimum_end_index or start + 2) - start)
        for i, current in enumerate(candidates):
            if current.type != seg_type:
                continue
            last_same_idx = i
            if best_idx is None or (
                current.high >= candidates[best_idx].high if seg_type == "up"
                else current.low <= candidates[best_idx].low
            ):
                best_idx = i
        if best_idx is None or last_same_idx is None or last_same_idx < minimum_offset:
            projection("insufficient-pens")
            return
        preview_kind = "directional-extreme"
        if best_idx < minimum_offset:
            best_idx = last_same_idx
            preview_kind = "latest-same-direction"
        pending_bis = candidates[:best_idx + 1]
        origin, endpoint = pending_bis[0].start.val, pending_bis[-1].end.val
        if not (endpoint > origin if seg_type == "up" else endpoint < origin):
            # The search may already be waiting at a specific qualified
            # boundary (L071/L079). Preserve that hypothesis, not an arbitrary
            # old price found by a backwards scan through the tail.
            candidate = state.candidate_index
            if (state.reason in {"waiting-first-feature", "waiting-second-feature"}
                    and candidate is not None and start + minimum_offset <= candidate < len(all_bis)
                    and all_bis[candidate].type == seg_type
                    and (all_bis[candidate].end.val > origin if seg_type == "up"
                         else all_bis[candidate].end.val < origin)):
                best_idx = candidate - start
                pending_bis = candidates[:best_idx + 1]
                preview_kind = "boundary-hypothesis"
            else:
                projection("tail-end-direction-conflict")
                return
        projection(preview_kind, start + best_idx)

        xd = self._make_xd(pending_bis, seg_type, done=False)
        xd.forming = (
            True  # 显示口径：唯一"正在形成的最后一段"（图表画虚线）；与 done 解耦
        )
        sv, ev = pending_bis[0].start.val, pending_bis[-1].end.val
        _log.debug(
            lambda: (
                f"[未完成] XD[{xd.index}] {seg_type} {_bi_label(pending_bis[0])}~{_bi_label(pending_bis[-1])} ({len(pending_bis)}笔) {sv}→{ev}"
            )
        )
