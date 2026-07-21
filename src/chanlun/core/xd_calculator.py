# -*- coding: utf-8 -*-
"""
线段计算模块 v2
基于笔列表识别线段，逻辑简洁清晰。
"""
from typing import List, Optional

from chanlun.core.types import BI, XD
from chanlun.tools.log_util import LogUtil

_log = LogUtil


_GAP_CONFIRMATION_PENDING = object()
_TYPE2_CONFIRMED = "confirmed"
_TYPE2_PENDING = "pending"
_TYPE2_INVALIDATED = "invalidated"


def _bi_label(bi: BI) -> str:
    return f"bi[{bi.index}]{bi.type}({bi.start.val:.3f}→{bi.end.val:.3f})"


def _elem_label(e: dict) -> str:
    merged = e.get('merged_bis')
    if merged:
        return f"{{h={e['high']:.3f},l={e['low']:.3f},merged={len(merged)}}}"
    return f"{{h={e['high']:.3f},l={e['low']:.3f}}}"


# ============================================================
# 特征序列工具函数（纯函数，无状态）
# ============================================================

def _bi_to_cs_elem(bi: BI) -> dict:
    return {'bi': bi, 'high': bi.high, 'low': bi.low}


def _overlap(a, b) -> bool:
    h1, l1 = (a['high'], a['low']) if isinstance(a, dict) else (a.high, a.low)
    h2, l2 = (b['high'], b['low']) if isinstance(b, dict) else (b.high, b.low)
    return max(l1, l2) <= min(h1, h2)


def _elem_farthest_bi_index(elem: dict) -> int:
    """Return the farthest physical BI represented by a feature element."""

    merged = elem.get('merged_bis')
    if merged:
        return max(bi.index for bi in merged)
    return elem['bi'].index


def _has_inclusion(a: dict, b: dict) -> bool:
    return (a['high'] >= b['high'] and a['low'] <= b['low']) or \
           (b['high'] >= a['high'] and b['low'] <= a['low'])


def _merge_two(prev: dict, cur: dict, direction: str) -> dict:
    if direction == 'up':
        mh, ml = max(prev['high'], cur['high']), max(prev['low'], cur['low'])
    else:
        mh, ml = min(prev['high'], cur['high']), min(prev['low'], cur['low'])
    prev_bis = prev.get('merged_bis', [prev['bi']])
    cur_bis = cur.get('merged_bis', [cur['bi']])
    return {
        'bi': prev['bi'], 'high': mh, 'low': ml,
        'merged_bis': prev_bis + cur_bis,
    }


def _process_inclusion(elems: List[dict], direction: str) -> List[dict]:
    """特征序列包含处理 → 标准特征序列。

    顺序原则：从左到右逐相邻、每一对都查包含，有包含按方向合并（up 取高高、down 取低低）
    后用合并结果继续与下一根比（级联）。
    """
    if len(elems) < 2:
        return list(elems)

    result = [elems[0].copy()]
    for i in range(1, len(elems)):
        cur = elems[i]
        # 逐相邻查包含；有包含则按方向合并后向前级联（包含不满足传递律，须重查前一对）
        if _has_inclusion(result[-1], cur):
            result[-1] = _merge_two(result[-1], cur, direction)
            while len(result) >= 2 and _has_inclusion(result[-2], result[-1]):
                merged = _merge_two(result[-2], result[-1], direction)
                result.pop()
                result[-1] = merged
        else:
            result.append(cur.copy())

    return result


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
    target = elem['bi']
    merged = elem.get('merged_bis')
    if merged:
        target = max(merged, key=lambda b: b.high) if seg_type == 'up' else min(merged, key=lambda b: b.low)
    return target

# _try_end 中"反向 CS 元素扫描上限"：防止反向 CS 一直被包含合并、
# second_elems 凑不齐 2 个，导致单次扫到数组末尾造成 O(n²) 退化。
# 默认 50，可用环境变量 CHANLUN_XD_LOOKAHEAD 覆盖(import 时读一次的模块全局 SAFETY_LOOKAHEAD,改后须重启)。
# 注：无 config 实例级覆盖(_try_end 直读模块全局 SAFETY_LOOKAHEAD, 非 self.config); 且该值不进 cl_config/
# source_fingerprint/kline signature 任何缓存指纹 → 改 env 后磁盘 chart_data 旧条目不自动失效(需重启+靠新鲜度窗口)。
import os as _os

def _get_default_safety_lookahead() -> int:
    raw = _os.environ.get('CHANLUN_XD_LOOKAHEAD', '50')
    try:
        v = int(raw)
        # 不接受 < 5 的过小值（容易误中止合理走势），也不接受 > 1000 的过大值（性能失控）
        if v < 5:
            return 5
        if v > 1000:
            return 1000
        return v
    except (TypeError, ValueError):
        return 50

SAFETY_LOOKAHEAD = _get_default_safety_lookahead()


class XdCalculator:
    """线段计算器：基于笔列表全量识别线段（每次调用都全量重算，不做增量）。"""

    def __init__(self, config: dict):
        self.config = config
        self.xds: List[XD] = []
        # 上轮喂入的 bis 列表对象。bi_calculator 在 bis 变更时换新列表、未变时返回
        # 同一对象;且变更只「删后缀 + 新建」(_rebuild_from_fxs:del bis[stable:]),
        # 共享前缀对象按 identity 保留。故可用 identity 二分(_identity_prefix_len)
        # O(log B) 求公共前缀,取代原每次 O(B) 重建值签名 + O(B) 值-LCP 两处 O(n)
        # (walk-forward 整体 O(n²) 主因之一,profiler 实测 _bi_signature 占 xd 69%)。
        self._last_bis_obj: Optional[List[BI]] = None

    @staticmethod
    def _identity_prefix_len(new_bis: List[BI], old_bis: Optional[List[BI]]) -> int:
        """二分求 new_bis 与 old_bis 的 identity 公共前缀长度 O(log B)。

        依赖 bi_calculator 的 rebuild 契约(_rebuild_from_fxs:del bis[stable:] + 新建):
        一次变更只换连续后缀对象、共享前缀对象不动,故 ``new_bis[i] is old_bis[i]``
        在分歧点前恒真、之后恒假(单调),可二分定位边界。

        identity-match ⊆ value-match(同对象必同值)。当前唯一用途=calculate 的 identity 脏检查
        (返回值==len ⟺ 新旧 bis 列表逐元素同对象 → 直接复用上轮线段;实测 walk-forward 约半数
        calculate 命中,省一次全量 xd 重建)。
        """
        if old_bis is None:
            return 0
        lo, hi = 0, min(len(new_bis), len(old_bis))
        while lo < hi:
            mid = (lo + hi) // 2
            if new_bis[mid] is old_bis[mid]:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # ----------------------------------------------------------
    # 公共接口
    # ----------------------------------------------------------
    def calculate(self, bis: List[BI]) -> List[XD]:
        """根据笔列表计算线段（当前=全量重建；段增量已禁用）。

        确认级联会使已确认段终点被后续假反弹回溯合并，旧段增量「删末 2 段、复用前缀
        （依赖 done 段不回改）」的前提不再成立，故 calculate 改为每次全量重建：
        self.xds.clear() + _find_start + _build_segments。下方 identity 脏检查
        （_identity_prefix_len）保留：实测约半数 calculate 命中、省全量 xd 重建。
        全量重建下，增量喂入 == 批量 由 tests/chan_core/test_incremental_equivalence.py 对拍守护。
        """
        all_bis = bis
        if all_bis is self._last_bis_obj:
            return self.xds

        old_bis = self._last_bis_obj
        ident_lcp = self._identity_prefix_len(all_bis, old_bis)

        # 脏检查:新列表但每根笔(按 identity)与上次完全一致 → 复用上轮线段。
        # bi rebuild 必换尾部对象,正常不命中,留作正确性兜底(等价旧「值签名相等」分支)。
        if (old_bis is not None and len(all_bis) == len(old_bis)
                and ident_lcp == len(all_bis)):
            self._last_bis_obj = all_bis
            return self.xds

        if len(all_bis) < 3:
            self.xds.clear()
            self._last_bis_obj = all_bis
            return self.xds

        # 禁用段增量(全量重建保 inc==batch)：级联使已确认段终点可被后续假反弹回溯合并,
        # 旧段增量「删末2段、复用前缀(依赖 done 段不回改)」的前提不再成立。
        self.xds.clear()
        start = self._find_start(all_bis)
        self._build_segments(all_bis, start)

        self._last_bis_obj = all_bis
        return self.xds

    # ----------------------------------------------------------
    def _find_strict_start(self, all_bis: List[BI]) -> int:
        """关键笔起点扫描 (严格版, 不走 fallback)。

        条件: bi[i].start/end 同时是 (bi[i], bi[i+2], bi[i+4]) 中的方向极值
        ——up 笔取最低, down 笔取最高——并且 bi[i] 与 bi[i+2] 有重叠。

        Returns:
            int: 找到的起点位置 (>= 0); 未找到返回 -1。

        与 ``_find_start`` 的关系: 后者在 strict 找不到时 fallback 到 overlap-only,
        给出权宜起点供首次建段。
        """
        for i in range(len(all_bis) - 4):
            bi_i = all_bis[i]
            bi_i2 = all_bis[i + 2]
            bi_i4 = all_bis[i + 4]
            if bi_i.type == 'up':
                is_extreme = (
                    bi_i.start.val < bi_i2.start.val
                    and bi_i.start.val < bi_i4.start.val
                    and bi_i.end.val < bi_i2.end.val
                    and bi_i.end.val < bi_i4.end.val
                )
            else:  # down
                is_extreme = (
                    bi_i.start.val > bi_i2.start.val
                    and bi_i.start.val > bi_i4.start.val
                    and bi_i.end.val > bi_i2.end.val
                    and bi_i.end.val > bi_i4.end.val
                )
            if is_extreme and _overlap(bi_i, bi_i2):
                return i
        return -1

    def _find_start(self, all_bis: List[BI]) -> int:
        """寻找首段起点 (含 fallback)。

        首段无前段可破坏,缺乏精确起点规则,故 strict/fallback 为工程取舍。

        优先策略 (关键笔, 见 ``_find_strict_start``): 段起点恰好是方向极值,
        避免 ``xd.start.val 与 xd.low/high 语义不一致的退化首段``。

        回退策略: strict 找不到时退回 overlap-only 的"权宜起点", 让首段能建立。
          再失败返回 0, 主循环 pos+=1 兜底。
        """
        strict = self._find_strict_start(all_bis)
        if strict >= 0:
            return strict
        # Fallback: overlap-only
        for i in range(len(all_bis) - 2):
            if _overlap(all_bis[i], all_bis[i + 2]):
                return i
        return 0

    # ----------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------
    def _build_segments(self, all_bis: List[BI], start: int):
        """主循环：逐段构造线段 + 确认级联（breaks-back 合并 + 推迟 done）。

        线段只有被「合法反向线段」破坏才真正终结。若反向只是假反弹(跌破/涨破转折点 T),
        则未破坏本段 → 本段延伸吞掉假反弹至真极值(_cascade_merge_back)。又因破坏本段的
        反向段自身需待其反向确认,故最后一条已确认段推迟为 pending(_emit_segments_deferred)。
        """
        # 候选段携带本分支真正使用到的因果见证时间；只有后续确认级联跨过
        # deferred 边界后，才会把该见证写成 XD.locked_at。
        segs: List[tuple] = []      # (seg_start, real_end, seg_type, formed_at)
        locked_candidates = {}      # (start, end, type) -> first causal lock time
        pos = start
        reverse_end_hint = None
        pending_tail = None         # 内层自然结束的末段未完成线段 (start, type)
        r34_starts: set = set()     # 反向单调成段(退化失败反弹)的 seg_start 集,供级联 A-B-C 吸收门控

        while pos + 2 < len(all_bis):
            # 确定 seg_end 初始值
            if reverse_end_hint is not None:
                # 反向线段已成立，跳过 overlap 检查，直接使用已知范围
                seg_end = reverse_end_hint
                reverse_end_hint = None
            else:
                if not _overlap(all_bis[pos], all_bis[pos + 2]):
                    pos += 1
                    continue
                seg_end = pos + 2

            seg_type = all_bis[pos].type
            seg_start = pos
            check = seg_end + 1

            # 计算 seg_high/seg_low 的初始值
            # 注意：一个段确定方向后，"反方向"那一边是固定值（段起点价），
            # "顺方向"那一边随 seg_end 推进而刷新。简化为：
            #   - up 段：seg_low = 起点常量；seg_high 跟随 seg_end.end.val 取 max
            #   - down 段：seg_high = 起点常量；seg_low 跟随 seg_end.end.val 取 min
            seg_anchor = all_bis[seg_start].start.val  # 段起点价（恒定的"反方向"边）
            if seg_type == 'up':
                seg_low = seg_anchor
                seg_high = all_bis[seg_end].end.val
            else:
                seg_high = seg_anchor
                seg_low = all_bis[seg_end].end.val

            # 维护增量 seg_cs_bis 列表（cs = 反向笔）
            # 初始范围 [seg_start, seg_end]，后续延伸/吸收时同步追加。
            # 这样 _try_end 不必每次重新过滤段内 CS 笔。
            cs_bi_type = 'down' if seg_type == 'up' else 'up'
            seg_cs_bis: List[BI] = [all_bis[i] for i in range(seg_start, seg_end + 1)
                                     if all_bis[i].type == cs_bi_type]

            bi_s = all_bis[seg_start]
            _log.debug(lambda:f"[新线段] {seg_type} 起点={_bi_label(bi_s)}, seg_end={_bi_label(all_bis[seg_end])}, seg_high={seg_high:.3f}, seg_low={seg_low:.3f}")

            while check + 1 < len(all_bis):
                # 仅刷新"顺方向"那一边的极值（反方向边恒等于 seg_anchor，无需重算）
                if seg_type == 'up':
                    seg_high = max(seg_high, all_bis[seg_end].end.val)
                else:
                    seg_low = min(seg_low, all_bis[seg_end].end.val)
                # next_same 取 check+1（同向笔）：延伸的判据是「下一根同向笔是否
                # 创出段方向新极值」，必须看同向笔。check 本身是反向(cs)笔，其
                # high/low 恒落在 seg_anchor 一侧，用它判延伸将永不成立（死分支）。
                # while 条件已保证 check+1 < len(all_bis)，无需再越界检查。
                next_same = all_bis[check + 1]

                # Step 1: 延伸
                # 延伸吃掉 [check, check+1] 两根笔，其中 check 是反向笔(cs)、check+1 是同向笔。
                # 增量缓存：把 check 这根 cs 笔追加到 seg_cs_bis。
                if seg_type == 'up' and next_same.high > seg_high:
                    _log.debug(lambda:f"  [延伸] {_bi_label(next_same)} high={next_same.high:.3f} > seg_high={seg_high:.3f}")
                    if all_bis[check].type == cs_bi_type:
                        seg_cs_bis.append(all_bis[check])
                    seg_end = check + 1
                    check += 2
                    continue
                if seg_type == 'down' and next_same.low < seg_low:
                    _log.debug(lambda:f"  [延伸] {_bi_label(next_same)} low={next_same.low:.3f} < seg_low={seg_low:.3f}")
                    if all_bis[check].type == cs_bi_type:
                        seg_cs_bis.append(all_bis[check])
                    seg_end = check + 1
                    check += 2
                    continue

                # Step 2: 分型检测（传入增量缓存避免重算）
                _log.debug(lambda:f"  [检测] seg_end={_bi_label(all_bis[seg_end])}, check={_bi_label(all_bis[check])}")
                end_result = self._try_end(all_bis, seg_start, seg_end, seg_type,
                                           seg_high, seg_low, check,
                                           seg_cs_bis_cache=seg_cs_bis)
                if end_result is _GAP_CONFIRMATION_PENDING:
                    # 原文第二种情况一旦出现缺口，就必须等待第二特征序列
                    # 分型完成。不能继续吸收后把同一破坏改判成“无缺口”，
                    # 否则历史前缀会过早锁定并在未来回写 locked_at。
                    pending_tail = (seg_start, seg_type)
                    pos = len(all_bis)
                    break
                if end_result is not None:
                    real_end, next_start, next_end, formed_at = end_result
                    if segs:
                        previous_formed_at = segs[-1][3]
                        formed_at = (
                            max(previous_formed_at, formed_at)
                            if previous_formed_at is not None and formed_at is not None
                            else None
                        )
                    # 收集为待定段 + 确认级联(假反弹则并入前段、终点回溯到真极值)
                    segs.append((seg_start, real_end, seg_type, formed_at))
                    merged = self._cascade_merge_back(
                        all_bis,
                        segs,
                        r34_starts,
                        locked_candidates,
                    )
                    self._freeze_confirmed_candidate(segs, locked_candidates)
                    pos = segs[-1][1] + 1   # 从(可能已合并的)最后段终点之后续建
                    # 反向区间提示:无合并时沿用外层 check(反向段同向笔);合并后作废
                    if merged:
                        reverse_end_hint = None
                    elif check >= next_start + 2 and check < len(all_bis):
                        reverse_end_hint = check
                    break

                # Step 2.6: 反向线段破坏 —— 补 _try_end 顶/底分型路径结构性漏掉的「反向单调成段」
                # 破坏。判据：第一笔破坏前线段→延伸三笔→第三笔破第一笔结束位置→新线段形成、
                # 前线段结束。_try_end_r34 须笔破坏 + 反向延伸成段才认定。
                # 修复「更低高点结尾 up 段后单调暴跌→顶分型首元素卡死→段跑飞」的退化场景。
                r34 = self._try_end_r34(all_bis, seg_start, seg_type, seg_high, seg_low, check)
                if r34 is not None:
                    real_end, next_start, next_end, formed_at = r34
                    if segs:
                        previous_formed_at = segs[-1][3]
                        formed_at = (
                            max(previous_formed_at, formed_at)
                            if previous_formed_at is not None and formed_at is not None
                            else None
                        )
                    segs.append((seg_start, real_end, seg_type, formed_at))
                    r34_starts.add(seg_start)   # 标记反向单调成段(退化失败反弹),供级联 A-B-C 吸收
                    self._cascade_merge_back(
                        all_bis,
                        segs,
                        r34_starts,
                        locked_candidates,
                    )
                    self._freeze_confirmed_candidate(segs, locked_candidates)
                    pos = segs[-1][1] + 1
                    reverse_end_hint = None
                    break

                # Step 3: 吸收
                # 吸收吃掉 [check, check+1] 两根笔，其中 check 是 cs 笔。
                # 增量缓存：把 check 这根 cs 笔追加到 seg_cs_bis。
                if all_bis[check].type == cs_bi_type:
                    seg_cs_bis.append(all_bis[check])
                seg_end = check + 1
                check += 2
            else:
                pending_tail = (seg_start, seg_type)
                break

        self._emit_segments_deferred(
            all_bis,
            segs,
            pending_tail,
            start,
            locked_candidates,
        )

    @staticmethod
    def _breaks_back(all_bis, prior, cur) -> bool:
        """cur(反向段)是否「破了 prior 转折点 T 那一笔的底/顶」→ prior 未结束、继续延续。
        即:反向段破了该笔的底/顶则原线段未结束、继续延续;未破则原线段被破坏(反向段成立、不合并)。
        T=all_bis[pe].end.val=prior 终点笔的底/顶,几何上恰=破坏笔的底/顶(笔首尾相接)。
        prior=down(T=谷): cur(up)段内最低<T 即跌破; prior=up(T=峰): cur(down)段内最高>T。"""
        ps, pe, pt = prior[:3]
        cs, ce, _ = cur[:3]
        turn = all_bis[pe].end.val
        if pt == 'down':
            return min(all_bis[j].low for j in range(cs, ce + 1)) < turn - 1e-9
        return max(all_bis[j].high for j in range(cs, ce + 1)) > turn + 1e-9

    @staticmethod
    def _extreme_idx(all_bis, s, e, seg_type) -> int:
        """[s,e] 内达 seg_type 方向真极值的笔下标(down→最低 low 谷笔 / up→最高 high 峰笔)。"""
        bidx = s
        if seg_type == 'down':
            best = all_bis[s].low
            for j in range(s, e + 1):
                if all_bis[j].low < best:
                    best, bidx = all_bis[j].low, j
        else:
            best = all_bis[s].high
            for j in range(s, e + 1):
                if all_bis[j].high > best:
                    best, bidx = all_bis[j].high, j
        return bidx

    @staticmethod
    def _breaks_extreme(all_bis, prior, cur) -> bool:
        """cur 是否在 prior 方向上突破 prior 转折点极值(prior,cur **同向**,区别于反向的 _breaks_back)。
        prior=down: cur 段内最低 < prior 终点谷; prior=up: cur 段内最高 > prior 终点峰。"""
        _ps, pe, pt = prior[:3]
        cs, ce, _ = cur[:3]
        turn = all_bis[pe].end.val
        if pt == 'down':
            return min(all_bis[j].low for j in range(cs, ce + 1)) < turn - 1e-9
        return max(all_bis[j].high for j in range(cs, ce + 1)) > turn + 1e-9

    @staticmethod
    def _candidate_key(candidate) -> tuple:
        return candidate[0], candidate[1], candidate[2]

    def _cascade_merge_back(
        self,
        all_bis,
        segs,
        r34_starts,
        locked_candidates,
    ) -> bool:
        """确认级联：两类合并循环至稳定，返回是否合并过。
        ① 深度-1 假反弹（_breaks_back）：末段(cur)破前段转折点 → 并入前段，终点取真极值
           (_extreme_idx)。
        ② A-B-C 吸收：当 B(=segs[-2]) 是反向单调成段(退化失败反弹,seg_start ∈ r34_starts)、
           且 C(=segs[-1]) 与 A(=segs[-3]) 同向并突破 A 的方向极值（_breaks_extreme）→ B 是
           假反弹未顶住、趋势穿过 A 继续，A、B、C 合并为 A 方向一段。仅 B 为反向单调成段才触发
           （用 r34_starts 门控），杜绝正常趋势 A(down)-B(真反弹 up,顶分型终结)-C(down) 被误合并。
           消除假上冲/假回调导致的过度切碎。
        合并后终点取段内真极值（详见步骤6.5 注释）。"""
        merged = False
        while True:
            if (
                len(segs) >= 2
                and self._breaks_back(all_bis, segs[-2], segs[-1])
                and self._candidate_key(segs[-2]) not in locked_candidates
            ):
                ps, _pe, pt, prior_formed_at = segs[-2]
                cs, ce, _, current_formed_at = segs[-1]
                new_end = self._extreme_idx(all_bis, cs, ce, pt)
                formed_at = (
                    max(prior_formed_at, current_formed_at)
                    if prior_formed_at is not None and current_formed_at is not None
                    else None
                )
                segs[-2] = (ps, new_end, pt, formed_at)
                segs.pop()
                merged = True
                continue
            if len(segs) >= 3 and segs[-2][0] in r34_starts:
                A, B, C = segs[-3], segs[-2], segs[-1]
                if (
                    A[2] == C[2]
                    and self._breaks_extreme(all_bis, A, C)
                    and self._candidate_key(A) not in locked_candidates
                ):
                    new_end = self._extreme_idx(all_bis, A[0], C[1], A[2])
                    witnesses = (A[3], B[3], C[3])
                    formed_at = max(witnesses) if all(w is not None for w in witnesses) else None
                    segs[-3] = (A[0], new_end, A[2], formed_at)
                    r34_starts.discard(B[0])
                    segs.pop()
                    segs.pop()
                    merged = True
                    continue
            break
        return merged

    def _try_end_r34(self, all_bis, seg_start, seg_type, seg_high, seg_low, check):
        """反向线段破坏（第一笔破坏前线段→延伸三笔→第三笔破第一笔结束位置→新线段形成、
        前线段结束）—— 补 `_try_end` 顶/底分型路径结构性漏掉的「反向单调成段」破坏。

        背景：当 up 段以「更低高点」结尾（端点低于内部峰）、随后单调暴跌时，特征序列首元素
        =段内回调笔(高=内部峰) 恒 ≥ 反向所有元素(单调递减) → 顶分型永不成立 → `_try_end`
        恒 None → 段无限延伸跑飞。

        判据：须
          ① rb1：check 之后出现破段起点(seg_anchor)的反向笔（笔破坏）；
          ② rb2：rb1 之后反向方向再创新极值、破 rb1 的结束位置（反向方向已确立 ≥3 笔线段，
             满足「段被段破坏」）。
        扫描中若同向笔先创段方向新极值 → 是延伸非破坏，放弃（交回主循环 Step1/3）。
        命中返回 (real_end, next_start, next_end, formed_at)；终点取段内真峰/谷
        (≥3 笔最小段约束)，formed_at 来自 rb2 这根实际确认见证笔。"""
        cs_bi_type = 'down' if seg_type == 'up' else 'up'
        seg_anchor = all_bis[seg_start].start.val
        n = len(all_bis)

        def _same_new_extreme(b) -> bool:
            return b.type == seg_type and (
                b.high > seg_high + 1e-9 if seg_type == 'up' else b.low < seg_low - 1e-9)

        def _broke(b, level) -> bool:
            return (b.low < level - 1e-9) if seg_type == 'up' else (b.high > level + 1e-9)

        rb1 = None
        for j in range(check, n):
            b = all_bis[j]
            if _same_new_extreme(b):
                return None
            if b.type == cs_bi_type and _broke(b, seg_anchor):
                rb1 = j
                break
        if rb1 is None:
            return None
        rb1_end = all_bis[rb1].low if seg_type == 'up' else all_bis[rb1].high
        rb2 = None
        for j in range(rb1 + 1, n):
            b = all_bis[j]
            if _same_new_extreme(b):
                return None
            if b.type == cs_bi_type and _broke(b, rb1_end):
                rb2 = j
                break
        if rb2 is None:
            return None
        peak_idx = self._extreme_idx(all_bis, seg_start, rb1 - 1, seg_type)
        real_end = max(peak_idx, seg_start + 2)
        if real_end >= rb1 or all_bis[real_end].type != seg_type:
            return None
        return real_end, real_end + 1, rb2, all_bis[rb2].locked_at

    # 确认级联推迟 done 的深度:一条段被确认(done)须其反向段「锁定不再延伸」——反向段
    # 自身的反向被确认时才锁定(确认有递归前提)。breaks-back 合并可回溯 ≥1 级(反向假反弹的
    # 高/低点可越过更前段起点),故末 _DEFER_DONE 条已确认段保持 pending,防止「已 done 段被
    # 后续假反弹回溯合并」的当下性违例。此处 2 是末尾 done/pending 边界的经验值,因 calculate
    # 全量重建、不影响已 done 段端点正确性;若发现需 ≥3 级回溯再调。
    _DEFER_DONE = 2

    def _freeze_confirmed_candidate(self, segs, locked_candidates) -> None:
        """Freeze geometry and the first witness once two successors exist."""
        if len(segs) <= self._DEFER_DONE:
            return
        index = len(segs) - self._DEFER_DONE - 1
        candidate = segs[index]
        boundary = segs[index + self._DEFER_DONE]
        formed_at = candidate[3]
        boundary_witness = boundary[3]
        if formed_at is None or boundary_witness is None:
            return
        key = self._candidate_key(candidate)
        locked_candidates.setdefault(key, max(formed_at, boundary_witness))

    def _emit_segments_deferred(
        self,
        all_bis,
        segs,
        pending_tail,
        start,
        locked_candidates,
    ):
        """发射 segs:推迟 done——末 _DEFER_DONE 条已确认段(反向尚未锁定)标 pending、其余
        done;再补末段未完成线段。"""
        for s, e, t, _formed_at in segs:
            locked_at = locked_candidates.get((s, e, t))
            self._make_xd(
                all_bis[s:e + 1],
                t,
                done=locked_at is not None,
                locked_at=locked_at,
            )
        if pending_tail is not None:
            self._emit_pending(all_bis, pending_tail[0], pending_tail[1])
        elif segs:
            # 内层被 _try_end 命中直至数据末尾:在最后段之后补末段未完成线段
            pstart = segs[-1][1] + 1
            if pstart < len(all_bis):
                ptype = 'down' if segs[-1][2] == 'up' else 'up'
                already = bool(self.xds) and (not self.xds[-1].done) and self.xds[-1].type == ptype
                if not already:
                    self._emit_pending(all_bis, pstart, ptype)
        elif start < len(all_bis):
            self._emit_pending(all_bis, start, all_bis[start].type)

    # ----------------------------------------------------------
    # _try_end
    # ----------------------------------------------------------
    def _try_end(self, all_bis, seg_start, seg_end, seg_type,
                 seg_high, seg_low, check_pos,
                 seg_cs_bis_cache: Optional[List[BI]] = None) -> Optional[tuple]:
        """尝试用反向特征序列分型判定当前线段是否终结。

        命中返回 (当前段终点笔位置, 反向段起点, 反向段终点)，否则返回 None。
        """
        cs_bi_type = 'down' if seg_type == 'up' else 'up'
        inc_dir = 'up' if seg_type == 'up' else 'down'
        frac_name = '顶分型' if seg_type == 'up' else '底分型'

        # ---- 步骤1 ----
        # 优先使用调用方传入的增量缓存（由 _build_segments 维护），
        # 否则回退为按 seg_start..seg_end 全量过滤（兜底/向后兼容）。
        # 增量维护把每次 _try_end 的 cs 笔收集成本从 O(seg_len) 降到 O(1)，
        # 在 90天 1min 数据这种长段场景下消除 O(n²) 退化。
        if seg_cs_bis_cache is not None:
            seg_cs_bis = seg_cs_bis_cache
        else:
            seg_cs_bis = [all_bis[i] for i in range(seg_start, seg_end + 1)
                          if all_bis[i].type == cs_bi_type]
        if not seg_cs_bis:
            _log.debug(lambda:"    _try_end: 段内无CS笔 → 跳过")
            return None
        if check_pos >= len(all_bis) or all_bis[check_pos].type != cs_bi_type:
            return None

        current_cs_bi = all_bis[check_pos]

        _log.debug(lambda:f"    _try_end: 段内CS={len(seg_cs_bis)}根, 当前CS笔={_bi_label(current_cs_bi)}")

        # ---- 步骤2 ----
        if len(seg_cs_bis) >= 2:
            std_seg = _process_inclusion([_bi_to_cs_elem(bi) for bi in seg_cs_bis[:-1]], inc_dir)
            std_seg.append(_bi_to_cs_elem(seg_cs_bis[-1]))
        else:
            std_seg = [_bi_to_cs_elem(seg_cs_bis[0])]
        has_gap = not _overlap(std_seg[-1], current_cs_bi)
        _log.debug(lambda:f"    _try_end: 包含处理后std_seg={len(std_seg)}个, 缺口={'有' if has_gap else '无'} → {'第二种' if has_gap else '第一种'}")

        # ---- 步骤3 ----
        # 第一元素（属于原段的最后一根CS笔）从 std_seg 中取出冻结：转折点前后的两个元素
        # 不可做包含处理，因此 first_elem 不能与后续收集到的元素（属于反向段或原段延续，
        # 性质未定）合并。只有 second_elems（look_elems[1:]）内部可以做包含处理。
        first_elem = std_seg.pop(-1)
        second_elems: List[dict] = []
        # 步骤4 需要构成分型 (first_elem, second_elems[0], second_elems[1])，
        # 因此本步骤必须至少收集到 2 个 second_elems（包含合并后），
        # 否则下一轮外层吸收一根 CS 后 first_elem 又会被新的反向笔替换，
        # second_elems 永远凑不到 2 个，导致死循环（segment 无限延伸）。
        min_second = 2
        ready = False
        i = check_pos
        while i < len(all_bis):
            if i - check_pos > SAFETY_LOOKAHEAD:
                _log.debug(lambda:f"    _try_end: 扫描超过{SAFETY_LOOKAHEAD}笔仍未凑齐second_elems → 放弃本轮")
                return None
            bi = all_bis[i]
            if bi.type == cs_bi_type:
                new_elem = _bi_to_cs_elem(bi)
                if second_elems and _has_inclusion(second_elems[-1], new_elem):
                    second_elems[-1] = _merge_two(second_elems[-1], new_elem, inc_dir)
                    _log.debug(lambda:f"    _try_end: {_bi_label(bi)} 与前元素包含,合并→{_elem_label(second_elems[-1])}")
                else:
                    second_elems.append(new_elem)
                    _log.debug(lambda:f"    _try_end: 收集CS {_bi_label(bi)} → second_elems={len(second_elems)}个 (first_elem冻结)")
                    if len(second_elems) >= min_second:
                        ready = True
            elif ready:
                # 已收集到足够 second_elems，遇到非CS笔 → 停止收集，去检查分型
                break
            else:
                # 未收集够 second_elems，检查同向笔是否创新极值（线段延伸）
                if seg_type == 'up' and bi.type == 'up' and bi.high > seg_high:
                    _log.debug(lambda:f"    _try_end: {_bi_label(bi)} 创新高({bi.high:.3f}>{seg_high:.3f}) → 线段应延伸,返回None")
                    return None
                if seg_type == 'down' and bi.type == 'down' and bi.low < seg_low:
                    _log.debug(lambda:f"    _try_end: {_bi_label(bi)} 创新低({bi.low:.3f}<{seg_low:.3f}) → 线段应延伸,返回None")
                    return None
            i += 1
        look_elems = [first_elem] + second_elems

        if not look_elems:
            return None

        # ---- 步骤4 ----
        # 特征序列分型的结构是固定的（第一元素=分界点前线段的最后一个特征元素，第二元素=
        # 从转折点开始的第一笔）：
        #   左肩 = 第一元素 = first_elem        → combined 中位置 = len(std_seg)
        #   中心 = 第二元素 = second_elems[0]   → combined 中位置 = len(std_seg) + 1
        #   右肩 = 第三元素 = second_elems[1]   → combined 中位置 = len(std_seg) + 2
        # 因此分型中心点的位置是固定的，不能在整个 combined 中贪心搜索任意位置。
        # 否则会错误地把 first_elem 之前的 std_seg 元素当成分型中心
        # （第一元素属于原段，不能与反向段元素一起参与分型判定）。
        combined = std_seg + look_elems
        if len(combined) < 3:
            _log.debug(lambda:f"    _try_end: combined={len(combined)}个 < 3 → 不足以判断分型")
            return None
        # 分型中心固定为第二元素的位置：left=first_elem, mid=second_elems[0], right=second_elems[1]
        mid_pos = len(std_seg) + 1  # = combined 中 second_elems[0] 的索引
        if mid_pos + 1 >= len(combined):
            # second_elems 不足 2 个，无法判定分型
            elems_str = " ".join(_elem_label(e) for e in combined)
            _log.debug(lambda:f"    _try_end: combined=[{elems_str}] → second_elems<2,无法判定{frac_name}")
            return None
        left, mid, right = combined[mid_pos - 1], combined[mid_pos], combined[mid_pos + 1]
        if seg_type == 'up':
            is_frac = mid['high'] > left['high'] and mid['high'] > right['high']
        else:
            is_frac = mid['low'] < left['low'] and mid['low'] < right['low']
        elems_str = " ".join(_elem_label(e) for e in combined)
        if not is_frac:
            _log.debug(lambda:f"    _try_end: combined=[{elems_str}] mid_pos={mid_pos} → 无{frac_name}")
            return None
        frac_idx = mid_pos
        _log.debug(lambda:f"    _try_end: combined=[{elems_str}] → {frac_name}在[{frac_idx}](固定第二元素位置)")

        # ---- 步骤5 ----
        mid_elem = combined[frac_idx]
        if has_gap:
            _log.debug(lambda:"    _try_end: 第二种情况,进入_check_type2验证...")
            type2_status, type2_witness_idx = self._check_type2(
                all_bis,
                mid_elem,
                seg_type,
            )
            if type2_status == _TYPE2_PENDING:
                _log.debug(lambda:"    _try_end: 第二特征序列尚未完成 → 保持pending")
                return _GAP_CONFIRMATION_PENDING
            if type2_status == _TYPE2_INVALIDATED:
                _log.debug(lambda:"    _try_end: 第二特征序列被原段新极值否定 → 返回None")
                return None
            _log.debug(lambda:"    _try_end: _check_type2成功")
        else:
            type2_witness_idx = None

        # ---- 步骤6: 定位当前线段结束位置 + 反向线段范围 ----
        target_bi = _resolve_pivot_bi(mid_elem, seg_type)

        # bi.index 恒等于其在 all_bis 中的位置(bi_calculator._reindex_bis 保证),
        # 故直接用 .index 代替原 _bi_pos[id(bi)] 映射,省去每次 calculate O(B) 重建。
        end_bi_idx = target_bi.index - 1
        if end_bi_idx <= seg_start or end_bi_idx >= len(all_bis):
            _log.debug(lambda:f"    _try_end: end_bi_idx={end_bi_idx} 越界(seg_start={seg_start},len={len(all_bis)}) → 返回None")
            return None
        if all_bis[end_bi_idx].type != seg_type:
            _log.debug(lambda:f"    _try_end: 终点笔{_bi_label(all_bis[end_bi_idx])} 方向≠{seg_type} → 返回None")
            return None
        if end_bi_idx - seg_start + 1 < 3:
            _log.debug(lambda:f"    _try_end: 笔数{end_bi_idx - seg_start + 1}<3 → 返回None")
            return None

        # ---- 步骤6.5: 端点校正到段内真峰谷（标准化口径 + 当下性）----
        # 把端点钉成段内极值是「标准化」口径：供下游以线段为基础的分析（中枢/走势类型/买卖点，
        #   即本仓 XD 的全部消费方）把线段当成无内部结构的基本部件。它不是原始线段端点的划分
        #   规则（原始线段端点可非极值）；本仓所有消费方均属此类分析，故此标准化口径正确。
        # 此校正同时消除当下性漂移（真 bug，勿退回到校正前）：_resolve_pivot_bi 给出的是
        # 「吸收漂移」后的局部顶/底：当反向特征序列分型在段首峰出现前还凑不齐时，主循环 Step3
        # 吸收会把 seg_end/check 推过真峰，使 _try_end 据此算出的端点落在真峰之后的较低同向笔 →
        # 该端点随未来 K 的吸收步数漂移，已 done 段端点被未来 K 回改。
        # 校正：端点取 [seg_start, end_bi_idx] 内达到段方向极值的同向笔（真峰 seg_high / 真谷
        # seg_low）。该极值位「当下稳定」——延伸(Step1)已把任何更高/更低同向笔纳入极值，吸收
        # 只扩大搜索区间、不改变极值所在笔，故无论未来 K 如何，真峰谷恒定，端点不再回改。
        # 例外：真峰谷落在段首 2 笔内（end<seg_start+2，无法凑足 ≥3 笔合法线段）时，
        # 端点非极值是「≥3 笔最小线段」约束强制、非漂移，且其本身当下稳定 → 保留原局部端点。
        peak_idx, peak_val = end_bi_idx, all_bis[end_bi_idx].end.val
        for j in range(seg_start, end_bi_idx):
            bj = all_bis[j]
            if bj.type != seg_type:
                continue
            v = bj.end.val
            if (v > peak_val) if seg_type == 'up' else (v < peak_val):
                peak_idx, peak_val = j, v
        if peak_idx != end_bi_idx and peak_idx >= seg_start + 2:
            _log.debug(lambda:f"    _try_end: 端点校正 {end_bi_idx}→{peak_idx} "
                       f"(真峰谷={peak_val:.3f} 原局部={all_bis[end_bi_idx].end.val:.3f})")
            end_bi_idx = peak_idx

        # 方向校验：线段终点必须落在与方向一致的一侧（向上线段其顶必大于第一笔的底，
        # 反之亦然）。当段内出现巨幅反向笔使净走向反转时，_try_end 据特征序列分型算出的
        # 终点会与方向矛盾，此处拒绝该终结，让线段在后续找到合法终点或交由 _emit_pending
        # 收敛，杜绝输出方向矛盾的已确认段。
        seg_anchor_val = all_bis[seg_start].start.val
        seg_end_val = all_bis[end_bi_idx].end.val
        if seg_type == 'up' and not (seg_end_val > seg_anchor_val):
            _log.debug(lambda:f"    _try_end: up段终点{seg_end_val:.3f}≤起点{seg_anchor_val:.3f} 方向矛盾 → 返回None")
            return None
        if seg_type == 'down' and not (seg_end_val < seg_anchor_val):
            _log.debug(lambda:f"    _try_end: down段终点{seg_end_val:.3f}≥起点{seg_anchor_val:.3f} 方向矛盾 → 返回None")
            return None

        # 反向线段: 起点=当前线段终点+1, 终点=look_elems中最远的CS笔位置
        next_start = end_bi_idx + 1
        # look_elems 的最后一个元素对应反向线段已探明的最远同向笔
        last_look = look_elems[-1]
        last_look_bis = last_look.get('merged_bis')
        if last_look_bis:
            next_end = max(b.index for b in last_look_bis)
        else:
            next_end = last_look['bi'].index
        # 确保 next_end 至少为 next_start + 2（最少3笔）
        next_end = max(next_end, next_start + 2) if next_end >= next_start else next_start + 2

        _log.debug(lambda:f"    _try_end: ✓ 线段结束于{_bi_label(all_bis[end_bi_idx])}, "
                   f"反向线段 bi[{all_bis[next_start].index}]~bi[{all_bis[min(next_end, len(all_bis)-1)].index}]")
        # 无缺口分支以特征序列分型右肩为见证；有缺口分支还必须纳入第二
        # 特征序列分型实际检查到的最远笔。主循环在调用本函数前还读取了
        # check_pos + 1 这根同向笔以排除“继续创新高/低而只是延伸”，因此它也
        # 是不可省略的因果见证。若该笔尚未锁定，几何可以投影，但不得把更早
        # 的历史时间回填成 XD.locked_at。
        witness_idx = _elem_farthest_bi_index(right)
        if type2_witness_idx is not None:
            witness_idx = max(witness_idx, type2_witness_idx)
        witness_times = (
            all_bis[witness_idx].locked_at,
            all_bis[check_pos + 1].locked_at,
        )
        formed_at = (
            None
            if any(value is None for value in witness_times)
            else max(witness_times)
        )
        return end_bi_idx, next_start, next_end, formed_at

    # ----------------------------------------------------------
    # _check_type2
    # ----------------------------------------------------------
    def _check_type2(self, all_bis, mid_elem, seg_type) -> tuple[str, Optional[int]]:
        """Return type-2 status and the farthest causal witness BI index."""
        target_bi = _resolve_pivot_bi(mid_elem, seg_type)

        start_pos = target_bi.index + 1
        if start_pos >= len(all_bis):
            _log.debug(lambda:f"      _check_type2: start_pos={start_pos}越界 → False")
            return _TYPE2_PENDING, None

        cs2_type = 'up' if seg_type == 'up' else 'down'
        cs2_dir = 'down' if seg_type == 'up' else 'up'
        frac2_name = '底分型' if seg_type == 'up' else '顶分型'

        _log.debug(lambda:f"      _check_type2: 从{_bi_label(target_bi)}之后开始, 寻找反向线段CS({cs2_type}笔)的{frac2_name}")

        def _is_tail_fractal(elems: List[dict]) -> bool:
            """O(1) 检查最后三个元素是否构成反向段所需的分型。
            up 段的反向段找底分型(mid.low < 两侧)；down 段反向段找顶分型(mid.high > 两侧)。
            只检查尾部三元素：每次新追加/合并 cs2_elems 后调用一次即可，
            等价于原 find_frac2(cs2_elems) 的语义但耗时从 O(k) 降到 O(1)。
            """
            if len(elems) < 3:
                return False
            a, b, c = elems[-3], elems[-2], elems[-1]
            if seg_type == 'up':   # 反向 = down 段 → 找底分型
                return b['low'] < a['low'] and b['low'] < c['low']
            else:                  # 反向 = up 段   → 找顶分型
                return b['high'] > a['high'] and b['high'] > c['high']

        cs2_elems = []
        i = start_pos
        while i < len(all_bis):
            bi = all_bis[i]
            if getattr(bi, 'locked_at', None) is None:
                break

            # 原线段严格创新高/低检查（> / <，等价不算创新极值）。
            # 直接 return False 会让"等价新高 + 跨段后续创新高"误判为段延伸，
            # 故先把创新高/低的笔收进 cs2_elems 再判定分型，分型成立才认反向段。
            is_strict_new_extreme = (
                (seg_type == 'up' and bi.type == 'up' and bi.high > target_bi.high)
                or (seg_type == 'down' and bi.type == 'down' and bi.low < target_bi.low)
            )

            if bi.type == cs2_type:
                # 包含处理
                new_elem = _bi_to_cs_elem(bi)
                if cs2_elems and _has_inclusion(cs2_elems[-1], new_elem):
                    cs2_elems[-1] = _merge_two(cs2_elems[-1], new_elem, cs2_dir)
                    _log.debug(lambda:f"      _check_type2: {_bi_label(bi)} 与前元素包含,合并→{_elem_label(cs2_elems[-1])}")
                else:
                    cs2_elems.append(new_elem)
                    _log.debug(lambda:f"      _check_type2: 收集CS {_bi_label(bi)} → cs2_elems={len(cs2_elems)}个")

                # 每次添加/合并后立即检查分型（仅看尾部三元素，O(1)）
                if _is_tail_fractal(cs2_elems):
                    _log.debug(lambda:f"      _check_type2: 尾部三元素构成{frac2_name} → True")
                    return (
                        _TYPE2_CONFIRMED,
                        _elem_farthest_bi_index(cs2_elems[-1]),
                    )

                # 收完后才判断"严格创新极值停止"：
                # 此根 cs 笔创了原段方向的新极值，后续走势不可能再形成本段的反向段，
                # 必须立即停止扫描；反向段是否成立由已收集的 cs2_elems 决定。
                if is_strict_new_extreme:
                    _log.debug(lambda:f"      _check_type2: {_bi_label(bi)} 创新极值且已收进 cs2_elems → 停止扫描")
                    return _TYPE2_INVALIDATED, None
            elif is_strict_new_extreme:
                # 非 cs2 笔但创了新极值（兜底）：原段延伸，反向段不成立
                _log.debug(lambda:f"      _check_type2: {_bi_label(bi)} 非CS笔但创新极值 → 原线段延伸,False")
                return _TYPE2_INVALIDATED, None
            # 注：原此处对每根非 cs2 笔重复 find_frac2(cs2_elems) 的 elif 分支已删除——
            # 分型只可能在新元素加入/合并时产生新结构，非 cs2 笔不会改变 cs2_elems，
            # 重复检查纯属冗余且产生大量噪音日志。
            i += 1

        if len(cs2_elems) < 3:
            _log.debug(lambda:f"      _check_type2: cs2_elems仅{len(cs2_elems)}个<3 → False")
            return _TYPE2_PENDING, None
        # 走到这里说明扫描结束（要么 i 越界，要么遇到 strict_new_extreme break）
        # 由于循环内每次追加/合并后都已经检查过尾部分型，此处只需对最终状态做一次兜底检查。
        result = _is_tail_fractal(cs2_elems)
        elems_str = " ".join(_elem_label(e) for e in cs2_elems)
        _log.debug(lambda:f"      _check_type2: 最终[{elems_str}] → {frac2_name}{'成立' if result else '不成立'} → {result}")
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
    ) -> XD:
        """构造并追加 XD 对象（_emit_segment 与 _emit_pending 的公共逻辑）。

        Args:
            seg_bis: 组成该线段的笔列表（首笔=段起点，末笔=段终点）
            seg_type: 'up' / 'down'
            done: 是否为已完成段
                  - True  → zs_high/zs_low = (起点价, 终点价) 的 max/min（已完成段中枢）
                  - False → zs_high/zs_low = 整段 high/low（未完成段无明确中枢，用宽口径）

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
            default_zs_type=self.config.get('zs_type_xd', None),
        )
        xd.high = max(bi.high for bi in seg_bis)
        xd.low = min(bi.low for bi in seg_bis)
        done = bool(
            done
            and locked_at is not None
            and all(getattr(bi, 'locked_at', None) is not None for bi in seg_bis)
        )
        if done:
            sv, ev = seg_bis[0].start.val, seg_bis[-1].end.val
            xd.zs_high, xd.zs_low = (max(sv, ev), min(sv, ev))
        else:
            xd.zs_high, xd.zs_low = xd.high, xd.low
        xd.done = done
        xd.locked_at = locked_at if done else None
        self.xds.append(xd)
        return xd

    def _emit_segment(self, all_bis, start, end, seg_type, locked_at):
        seg_bis = all_bis[start: end + 1]
        xd = self._make_xd(seg_bis, seg_type, done=True, locked_at=locked_at)
        sv, ev = seg_bis[0].start.val, seg_bis[-1].end.val
        _log.debug(lambda:f"[完成] XD[{xd.index}] {seg_type} {_bi_label(seg_bis[0])}~{_bi_label(seg_bis[-1])} ({len(seg_bis)}笔) {sv:.3f}→{ev:.3f}")

    def _emit_pending(self, all_bis, start, seg_type):
        """输出未完成线段（全局极值优先 + 兜底末尾同向笔）。

        终点选择策略（双路保障，确保有 ≥3 根笔时必有输出）：

        主路径（全局极值）：
          扫描 candidates 中所有 seg_type 同向笔，取使段达到方向极值的那根作为终点
            - up 段 → 取 high 最大的 up 笔
            - down 段 → 取 low 最小的 down 笔
          这与已完成段的"段 high/low 应为段内极值"语义一致。

        兜底路径（确保有输出）：
          若主路径选出的极值笔位置导致 pending_bis < 3 根
          （典型场景：段第一根同向笔就是全段极值，后续震荡不再突破），
          则改用 candidates 中**最后一根**同向笔作为初选终点。

        方向校验：
          上面选出的初选终点若使 pending 段方向矛盾（终点价落在与 seg_type
          相反的一侧），则改在"方向合法的同向笔"中重新取方向极值；若无任何
          方向合法的 ≥3 笔终点，则不输出（该区间不构成合法的 seg_type 线段）。

        说明：
          已完成段由 _try_end 严格判定终点；未完成段无完整反向特征序列可用，
          只能保守估计。极值优先体现"线段记录方向极值"，兜底体现"实盘需有持续反馈"。

        candidates 不过滤 is_done()：BiCalculator 把最后一根笔标 pending，只收 done
        笔会出现"反向段恰好 3 根（末根 pending）→ candidates 只剩 2"的塌陷窗口。
        pending 笔 high/low 仍有效，参与极值/兜底逻辑无副作用。
        """
        candidates = list(all_bis[start:])
        if len(candidates) < 3:
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
            if seg_type == 'up' and cur.high > best.high:
                best_idx = i
            elif seg_type == 'down' and cur.low < best.low:
                best_idx = i

        if best_idx == -1:
            return

        pending_bis = candidates[:best_idx + 1]
        # 兜底：若全局极值导致段太短（<3 根），改用最后一根同向笔
        if len(pending_bis) < 3 and last_same_idx > best_idx:
            pending_bis = candidates[:last_same_idx + 1]

        if len(pending_bis) < 3:
            return

        # 方向校验：未完成段同样必须方向自洽。"极值优先 + 兜底
        # 末尾同向笔"在段内出现巨幅反向笔时，兜底路径会把终点落到方向相反的
        # 一侧。若初选 pending 段方向矛盾，则改在"方向合法（终点价落在与 seg_type
        # 一致一侧）且笔数≥3 的同向笔"中重新取方向极值；无合法候选则不强行成段。
        seg_anchor = candidates[0].start.val
        _end_val = pending_bis[-1].end.val
        _dir_ok = (_end_val > seg_anchor) if seg_type == 'up' else (_end_val < seg_anchor)
        if not _dir_ok:
            valid_idx = -1
            for i in range(len(candidates)):
                if candidates[i].type != seg_type or i + 1 < 3:
                    continue
                ev_i = candidates[i].end.val
                if not ((ev_i > seg_anchor) if seg_type == 'up' else (ev_i < seg_anchor)):
                    continue
                if valid_idx == -1 or (
                    (seg_type == 'up' and ev_i > candidates[valid_idx].end.val)
                    or (seg_type == 'down' and ev_i < candidates[valid_idx].end.val)
                ):
                    valid_idx = i
            if valid_idx == -1:
                _log.debug(lambda:f"[未完成] {seg_type} 段无方向合法终点 → 不输出")
                return
            pending_bis = candidates[:valid_idx + 1]

        xd = self._make_xd(pending_bis, seg_type, done=False)
        xd.forming = True   # 显示口径：唯一"正在形成的最后一段"（图表画虚线）；与 done 解耦
        sv, ev = pending_bis[0].start.val, pending_bis[-1].end.val
        _log.debug(lambda:f"[未完成] XD[{xd.index}] {seg_type} {_bi_label(pending_bis[0])}~{_bi_label(pending_bis[-1])} ({len(pending_bis)}笔) {sv:.3f}→{ev:.3f}")
