# -*- coding: utf-8 -*-
"""
按原始笔与特征序列证据构建线段。

原文、图例、推导和工程约定见 docs/segment_construction_audit.md。
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
        for i in range(len(all_bis) - 2):
            bi_i = all_bis[i]
            bi_i2 = all_bis[i + 2]
            is_extreme = (
                bi_i.start.val <= bi_i2.start.val
                if bi_i.type == "up"
                else bi_i.start.val >= bi_i2.start.val
            )
            if is_extreme and _overlap(bi_i, bi_i2):
                return i
        return -1

    # ----------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------
    def _build_segments(self, all_bis: List[BI], start: int):
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
        pivot_origins = _contained_pivot_origins(all_bis)

        def accept(proof):
            formed_at = confirmation_time(
                all_bis, proof.start_index, proof.witness_index
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
            if not _overlap(all_bis[pos], all_bis[pos + 2]):
                break
            inherited = (
                self._successor_from_second_sequence(accepted[-1], all_bis)
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
            check = seg_end + 1
            decision_index = seg_end
            result = None
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
                        result = self._try_end_r34(
                            all_bis, seg_start, seg_type, seg_high, seg_low, check,
                            max(seg_start, pivot_origins[check - 1]),
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
        tail = (pos, all_bis[pos].type) if pos < len(all_bis) else None
        self.tail_state = SegmentTail(
            pos if tail else None,
            tail[1] if tail else None,
            pending.reason if pending else "forming",
            pending.candidate_index if pending else None,
        )
        self._emit_segments(all_bis, segs, tail, start, locked_candidates)

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

    def _try_end_r34(
        self, all_bis, seg_start, seg_type, seg_high, seg_low, check,
        pivot_start=None,
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
        if check < seg_start + 3 or check + 2 >= len(all_bis):
            return None
        left, first = all_bis[check - 2], all_bis[check]
        origin_retest = (
            first.high == all_bis[seg_start].high and seg_high <= first.high
            if seg_type == "up"
            else first.low == all_bis[seg_start].low and seg_low >= first.low
        )

        def breaks_reference(reference):
            strong = (
                first.low < reference.low
                if seg_type == "up"
                else first.high > reference.high
            )
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
        if not _overlap(first, all_bis[check + 2]):
            return None
        real_end = check - 1
        anchor, endpoint = all_bis[seg_start].start.val, all_bis[real_end].end.val
        if not (endpoint > anchor if seg_type == "up" else endpoint < anchor):
            return None
        scan = self._scan_first_feature(
            all_bis, check, seg_type, first.high if seg_type == "up" else first.low
        )
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
        elements = tuple(
            FeatureEvidence.from_element(e)
            for e in (_bi_to_cs_elem(left), middle, element)
        )
        # L071's boundary classification uses the original first reversal;
        # later same-side inclusion still supplies the standard fractal.
        # This is the interpretation selected in review chart 01 (B).
        has_gap = not _overlap(left, first)
        second = ()
        witness = i
        if has_gap:
            status, witness2 = self._check_type2(all_bis, middle, seg_type)
            if status == _TYPE2_PENDING:
                return PendingBoundary("waiting-second-feature", real_end)
            if witness2 is None:
                raise ValueError("type-2 resolution requires a causal witness")
            if status == _TYPE2_INVALIDATED:
                return _GapConfirmationInvalidated(
                    all_bis[witness2].locked_at, witness2, real_end
                )
            witness = max(witness, witness2)
            second = self._second_evidence
        pivot_stem = None
        if stem_start is not None:
            stem = _bi_to_cs_elem(all_bis[stem_start])
            fold_direction = "down" if seg_type == "up" else "up"
            for j in range(stem_start + 2, check, 2):
                stem = _merge_two(stem, _bi_to_cs_elem(all_bis[j]), fold_direction)
            pivot_stem = FeatureEvidence.from_element(stem)
        strong_break = first.low < left.low if seg_type == "up" else first.high > left.high
        local_rule = "first-pen-break" if strong_break else "origin-extreme-retest"
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
        if not isinstance(scan, tuple):
            return scan
        mid, right = scan
        left = first_elem
        look_elems = [left, mid, right]
        # Reviewed L071 reading: classify the protected left and original
        # first reversal, then use the contained middle to prove the fractal.
        # A gap created only by later inclusion does not change this class.
        has_gap = not _overlap(left, _bi_to_cs_elem(first))
        if seg_type == "up":
            is_frac = mid["high"] > left["high"] and mid["high"] > right["high"]
        else:
            is_frac = mid["low"] < left["low"] and mid["low"] < right["low"]
        if not is_frac:
            _log.debug(lambda: f"    _try_end: 固定三元素不构成{frac_name}")
            return None
        _log.debug(
            lambda: (
                f"    _try_end: {[_elem_label(e) for e in look_elems]} → {frac_name}"
            )
        )

        # ---- 步骤5 ----
        mid_elem = mid
        if has_gap:
            _log.debug(lambda: "    _try_end: 第二种情况,进入_check_type2验证...")
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
        )
        self._proofs[proof.key] = proof
        formed_at = confirmation_time(all_bis, seg_start, witness_idx)
        return end_bi_idx, next_start, next_end, formed_at

    # ----------------------------------------------------------
    # 检查第二种情况。
    # ----------------------------------------------------------
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
                if _is_tail_fractal(cs2_elems):
                    self._second_evidence = tuple(
                        FeatureEvidence.from_element(e) for e in cs2_elems[-3:]
                    )
                    _log.debug(
                        lambda: f"      _check_type2: 尾部三元素构成{frac2_name} → True"
                    )
                    return (
                        _TYPE2_CONFIRMED,
                        _elem_farthest_bi_index(cs2_elems[-1]),
                    )

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
        # 走到这里说明扫描结束（要么 i 越界，要么遇到 strict_new_extreme break）
        # 由于循环内每次追加/合并后都已经检查过尾部分型，此处只需对最终状态做一次兜底检查。
        result = _is_tail_fractal(cs2_elems)
        elems_str = " ".join(_elem_label(e) for e in cs2_elems)
        _log.debug(
            lambda: (
                f"      _check_type2: 最终[{elems_str}] → {frac2_name}{'成立' if result else '不成立'} → {result}"
            )
        )
        if result:
            return _TYPE2_CONFIRMED, _elem_farthest_bi_index(cs2_elems[-1])
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
        """输出活动尾段的几何投影，不赋予分界或完成证据。

        终点选择策略（双路保障，确保有 ≥3 根笔时必有输出）：

        主路径（全局极值）：
          扫描 candidates 中所有 seg_type 同向笔，取使段达到方向极值的那根作为终点
             - up 段 → 取 high 最大的 up 笔
             - down 段 → 取 low 最小的 down 笔
          同价时展示较晚位置（用户图 03）；已证明段界的较早来源约定不变。
          这只是尾部的显示约定，不能用于移动已经证明的实际段界。

        兜底路径（确保有输出）：
          若主路径选出的极值笔位置导致 pending_bis < 3 根
          （典型场景：段第一根同向笔就是全段极值，后续震荡不再突破），
          则改用 candidates 中**最后一根**同向笔作为初选终点。

        方向校验：
          上面选出的初选终点若使 pending 段方向矛盾（终点价落在与 seg_type
          相反的一侧），则改在"方向合法的同向笔"中重新取方向极值；若无任何
          方向合法的 ≥3 笔终点，则不输出（该区间不构成合法的 seg_type 线段）。

        说明：
          实际段界由破坏证据决定。此处的活动终点可以随后改变，
          不声称它是原文规定的最终端点，更不能用它推翻已有分界。

        candidates 不过滤 is_done()：BiCalculator 可以输出多笔待定尾部，
        它们参与当前路径的几何预览，但不能单独提供正式确认。
        """
        candidates = list(all_bis[start:])
        if len(candidates) < 3 or not _overlap(candidates[0], candidates[2]):
            return

        # 主路径：找全局极值的同向笔
        best_idx = -1
        last_same_idx = -1  # 同时记录最后一根同向笔位置，作为兜底
        for i in range(len(candidates)):
            if candidates[i].type != seg_type:
                continue
            last_same_idx = i
            if best_idx == -1:
                best_idx = i
                continue
            cur = candidates[i]
            best = candidates[best_idx]
            if seg_type == "up" and cur.high >= best.high:
                best_idx = i
            elif seg_type == "down" and cur.low <= best.low:
                best_idx = i

        if best_idx == -1:
            return

        pending_bis = candidates[: best_idx + 1]
        # 兜底：若全局极值导致段太短（<3 根），改用最后一根同向笔
        if len(pending_bis) < 3 and last_same_idx > best_idx:
            pending_bis = candidates[: last_same_idx + 1]

        if len(pending_bis) < 3:
            return

        # 方向校验：未完成段同样必须方向自洽。"极值优先 + 兜底
        # 末尾同向笔"在段内出现巨幅反向笔时，兜底路径会把终点落到方向相反的
        # 一侧。若初选 pending 段方向矛盾，则改在"方向合法（终点价落在与 seg_type
        # 一致一侧）且笔数≥3 的同向笔"中重新取方向极值；无合法候选则不强行成段。
        seg_anchor = candidates[0].start.val
        _end_val = pending_bis[-1].end.val
        _dir_ok = (
            (_end_val > seg_anchor) if seg_type == "up" else (_end_val < seg_anchor)
        )
        if not _dir_ok:
            valid_idx = -1
            for i in range(len(candidates)):
                if candidates[i].type != seg_type or i + 1 < 3:
                    continue
                ev_i = candidates[i].end.val
                if not (
                    (ev_i > seg_anchor) if seg_type == "up" else (ev_i < seg_anchor)
                ):
                    continue
                if valid_idx == -1 or (
                    (seg_type == "up" and ev_i > candidates[valid_idx].end.val)
                    or (seg_type == "down" and ev_i < candidates[valid_idx].end.val)
                ):
                    valid_idx = i
            if valid_idx == -1:
                _log.debug(lambda: f"[未完成] {seg_type} 段无方向合法终点 → 不输出")
                return
            pending_bis = candidates[: valid_idx + 1]

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
