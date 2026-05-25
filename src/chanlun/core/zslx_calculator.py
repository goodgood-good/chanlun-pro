"""zslx_calculator.py — 级别无关的缠论走势类型划分(子项目③)。

用双信号状态机把中枢序列切成走势类型(ZSLX)：边界由「背驰」或「中枢方向
断裂」任一触发。盘整 = 1 中枢；趋势 = ≥2 依次同向中枢。复用 ② 的背驰内核。

原文依据见 docs/chanlun_core_redesign_3_zslx_design.md §2。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.beichi_calculator import LdProvider, beichi_pz, beichi_qs, is_qs
from chanlun.core.cl_interface import LINE, ZS, ZSLX


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
        return wts
