"""zslx_calculator.py — 级别无关的缠论走势类型划分(子项目③)。

用双信号状态机把中枢序列切成走势类型(ZSLX)：边界由「背驰」或「中枢方向
断裂」任一触发。盘整 = 1 中枢；趋势 = ≥2 依次同向中枢。复用 ② 的背驰内核。

原文依据见 docs/chanlun_core_redesign_3_zslx_design.md §2。
"""

from __future__ import annotations

from typing import List, Optional, Tuple
from weakref import WeakKeyDictionary

from chanlun.core.beichi_calculator import LdProvider, beichi_pz, beichi_qs, is_qs
from chanlun.core.types import LINE, ZS, ZSLX


def _classify(zss: List[ZS], wzgx_config: str) -> Tuple[str, str]:
    """对一个走势类型的中枢列表分类，返回 (zslx_type, direction)。

    1 中枢 → ("盘整", 净方向)；≥2 中枢 → ("上涨"/"下跌", "up"/"down")。
    """
    if len(zss) == 1:
        lines = zss[0].lines
        net_up = lines[-1].end.val >= lines[0].start.val
        return "盘整", ("up" if net_up else "down")
    direction = is_qs(zss[0], zss[1], wzgx_config)
    return ("上涨" if direction == "up" else "下跌"), direction


def _wt_beichi(
    zss: List[ZS], lines: List[LINE], ld_provider: LdProvider, wzgx_config: str,
    frequency: Optional[str] = None,
) -> bool:
    """判定一个走势类型(中枢列表 zss)是否在最新中枢的离开段处背驰。

    离开段 = 最新中枢的最后一段核心(zss[-1].lines[-1]，① Tier-1 已把离开段
    计入核心)。≥2 中枢走趋势背驰 beichi_qs，1 中枢走盘整背驰 beichi_pz。
    ``frequency`` 透传到背驰内核决定黄白线口径(原文细则1)。
    """
    leave_seg = zss[-1].lines[-1]
    if len(zss) >= 2:
        is_bc, _ = beichi_qs(lines, zss, leave_seg, ld_provider, wzgx_config, frequency)
    else:
        is_bc, _ = beichi_pz(zss[-1], leave_seg, ld_provider, frequency)
    return is_bc


def _finalize(
    zss: List[ZS], lines: List[LINE], wzgx_config: str, done: bool
) -> ZSLX:
    """把一个中枢列表收尾成 ZSLX：分类、填字段、回填中枢方向。"""
    zslx_type, direction = _classify(zss, wzgx_config)
    first_seg = zss[0].lines[0]
    last_seg = zss[-1].lines[-1]
    zslx = ZSLX(
        zslx_level=zss[0].level,
        start=first_seg.start,
        end=last_seg.end,
        start_line=first_seg,
        end_line=last_seg,
        _type=direction,
        index=0,
        done=done,
    )
    zslx.zss = zss
    zslx.zslx_type = zslx_type
    # 回填中枢方向(第24课「中枢形成的三段方向是怎么开始的」)：中枢方向由其**构成**
    # 决定——向上走势里的中枢=下-上-下、向下=上-下-上，即「核心首段的反向」。
    #   - 趋势走势类型：继承趋势方向(与各中枢构成一致)。
    #   - 盘整走势类型：单中枢仍有构成方向(从高/低点起算),不应统一抹成 zd——否则
    #     「向上=下上下 / 向下=上下上」无从体现(用户 2026-05-23 指出 + 第24课)。
    # 中枢方向**一律由构成判定**(核心首段的反向)：下-上-下=向上、上-下-上=向下。
    # 正确划分下,这与所在趋势方向一致(第24课);若某趋势走势类型里的中枢构成与趋势
    # 相反,说明该中枢划分有误(更深的"走势类型-aware 划分"问题),此处按构成如实标注
    # 以暴露,不掩盖。走势类型自身方向另存于 zslx._type(供背驰/买卖点用)。
    for zs in zss:
        head = zs.lines[0].type if getattr(zs, "lines", None) else None
        zs.type = ("up" if head == "down" else "down") if head in ("up", "down") else (direction or "zd")
    return zslx


class ZslxCalculator:
    """级别无关的走势类型划分计算器。无状态，每次 calculate 全量重算。"""

    def __init__(self):
        self._cache: dict[tuple, List[ZSLX]] = {}
        # done 中枢 zs_sig 弱键缓存。本实例被 cl.py 的 bi_zss(方向回填)/xd_zss
        # (走势类型)两路交替调用,单槽前缀 splice 会互相冲刷;改按中枢对象缓存其
        # sig:done 中枢字段(level/done/lines/zd/zg)并入 ZsCalculator.zss 后冻结
        # (append 前已 done + promote 完毕,之后不原地改;扩展是递归层独立对象、
        # 不回改 legacy 中枢),故任意调用流可复用,消除每次 _signature 重建全部中枢
        # 的 2×_line_sig(profiler 实测 _line_sig 占 zslx 近半)。WeakKeyDictionary:
        # 中枢 GC 时自动剔除、不污染 ZS.__dict__(快照/pickle 无感),ZS 默认 id-hash 可弱键。
        self._zs_sig_cache: "WeakKeyDictionary[ZS, tuple]" = WeakKeyDictionary()
        self._backfill_cache: Optional[List[ZS]] = None  # 上轮 backfill 的 bi_zss,增量跳过 identity 稳定前缀

    def __getstate__(self):
        # WeakKeyDictionary 不可 pickle 且为瞬态(可重建),序列化时丢弃。
        state = self.__dict__.copy()
        state.pop("_zs_sig_cache", None)
        state.pop("_backfill_cache", None)  # 瞬态优化缓存,丢弃后下次全量回填一次重建
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        if "_zs_sig_cache" not in self.__dict__:
            self._zs_sig_cache = WeakKeyDictionary()

    @staticmethod
    def _line_sig(line: LINE) -> tuple:
        return (
            getattr(line, "type", None),
            getattr(getattr(line.start, "k", None), "index", None),
            getattr(getattr(line.end, "k", None), "index", None),
            line.start.val,
            line.end.val,
            bool(getattr(line, "done", False)),
        )

    def _signature(
        self,
        zss: List[ZS],
        lines: List[LINE],
        wzgx_config: str,
        frequency: Optional[str],
    ) -> tuple:
        cache = self._zs_sig_cache

        def zs_sig(zs: ZS) -> tuple:
            done = bool(getattr(zs, "done", False))
            if done:
                hit = cache.get(zs)
                if hit is not None:
                    return hit
            zs_lines = getattr(zs, "lines", []) or []
            first = zs_lines[0] if zs_lines else None
            last = zs_lines[-1] if zs_lines else None
            sig = (
                getattr(zs, "level", 0),
                done,
                len(zs_lines),
                None if first is None else self._line_sig(first),
                None if last is None else self._line_sig(last),
                getattr(zs, "zd", None),
                getattr(zs, "zg", None),
            )
            if done:
                cache[zs] = sig
            return sig

        return (
            str(wzgx_config),
            str(frequency or ""),
            tuple(zs_sig(zs) for zs in zss),
            len(lines),
            None if not lines else self._line_sig(lines[0]),
            None if not lines else self._line_sig(lines[-1]),
        )

    def backfill_zs_directions(self, zss: List[ZS]) -> None:
        """笔层中枢方向回填(cl.py §3.6)——纯 per-zs 副作用:zs.type = 核心首段方向的反向
        (第24课:向上走势的中枢=下-上-下→type=up)。**不依赖走势类型分组**:正常中枢
        lines[0].type∈{up,down},_finalize 与缓存命中回放在此条件下等价(见 calculate 回放
        分支),状态机的 direction/zd 分支是该前提下的死路。bi 流(cl.py)仅取此回填、走势
        类型返回值被丢弃,故直调本方法可省去 zslx 全部签名+状态机+缓存开销(占 zslx 调用约半)。
        等价性由 golden(快照含 zs.type)+ test_incremental_equivalence(bi_zss type)守。

        增量:bi_zss 每轮新列表但中枢对象身份保留(ZsCalculator copy + 尾部 no-op),identity
        前缀稳定的中枢其 lines[0] 不变(完成中枢冻结、pending 延伸不改首段)→ type 已在上轮
        设妥、可跳过,只回填新增/变化尾部。消除每 bar 全量回填 O(bi_zss)·O(n)调用 = O(n²)
        (profiler 实测 backfill exp 2.02 是 zslx 最大头)。
        """
        cache = getattr(self, "_backfill_cache", None)
        start = 0
        if cache is not None:
            lo, hi = 0, min(len(zss), len(cache))
            while lo < hi:
                mid = (lo + hi) // 2
                if zss[mid] is cache[mid]:
                    lo = mid + 1
                else:
                    hi = mid
            start = lo
        for zs in zss[start:]:
            zs_lines = getattr(zs, "lines", None)
            head = zs_lines[0].type if zs_lines else None
            if head in ("up", "down"):
                zs.type = "up" if head == "down" else "down"
        self._backfill_cache = zss

    def calculate(
        self,
        zss: List[ZS],
        lines: List[LINE],
        ld_provider: LdProvider,
        wzgx_config: str,
        frequency: Optional[str] = None,
    ) -> List[ZSLX]:
        """把中枢序列切成走势类型列表，末个 done=False。

        ``frequency`` 透传到背驰内核(``_wt_beichi``)决定黄白线口径,见
        ``beichi_calculator._use_huangbai``。
        """
        if not zss:
            return []
        sig = self._signature(zss, lines, wzgx_config, frequency)
        cached = self._cache.get(sig)
        if cached is not None:
            # 缓存命中:走势类型(返回值)可复用,但「中枢方向回填」是对传入 zss
            # 的**副作用**(_finalize 按核心首段反向设 zs.type,第24课),仅在未命中
            # 经 _finalize 执行。命中时传入的是 zs_calculator 本轮新建的中枢
            # (zs.type 仍为 _create_zs 的占位 seg_b.type),必须在此重放回填,否则
            # 与未命中路径分叉(cl.py:240 调本方法仅取副作用回填 bi_zss 方向;
            # 见 tests/chan_core/test_incremental_equivalence 的 bi_zss type)。
            for zs in zss:
                head = zs.lines[0].type if getattr(zs, "lines", None) else None
                if head in ("up", "down"):
                    zs.type = "up" if head == "down" else "down"
            return cached

        wts: List[ZSLX] = []
        cur: Optional[List[ZS]] = [zss[0]]
        cur_dir: Optional[str] = None

        for zi in zss[1:]:
            if cur is None:
                # 上一个走势类型已被背驰终结，zi 另起
                cur = [zi]
                cur_dir = None
                continue

            qs = is_qs(cur[-1], zi, wzgx_config)
            if len(cur) >= 2:
                boundary = (qs != cur_dir)        # 趋势：方向不一致即断裂
            else:
                boundary = (qs is None)           # 盘整：无同向关系即断裂

            if boundary:
                wts.append(_finalize(cur, lines, wzgx_config, done=True))
                cur = [zi]
                cur_dir = None
            else:
                cur.append(zi)
                if len(cur) == 2:
                    cur_dir = qs

            # 背驰信号：当前走势类型在最新中枢离开段处背驰 → 终结
            if not boundary and cur is not None and _wt_beichi(
                cur, lines, ld_provider, wzgx_config, frequency
            ):
                wts.append(_finalize(cur, lines, wzgx_config, done=True))
                cur = None
                cur_dir = None

        if cur is not None:
            wts.append(_finalize(cur, lines, wzgx_config, done=False))
        if len(self._cache) > 16:
            self._cache.clear()
        self._cache[sig] = wts
        return wts
