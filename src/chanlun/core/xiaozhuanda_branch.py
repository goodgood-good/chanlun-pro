"""xiaozhuanda_branch.py — B6 小转大(小背驰-大转折)检测器(块R,只读结构预警)。

原文 L044 小背驰-大转折定理:小级别(顶/底)背驰引发大级别(向下/向上)的**必要条件**是
该级别走势**最后一个次级别中枢出现第三类(卖/买)点**;且「只有必要条件,而没有充分条件」
——故本检测器只产**候选/预警**,不产定性买卖点。L053:40 小转大 = 小级别背驰致大级别
中枢 restructure(a+A+b 变为大级别中枢 B 的次级别波动)。

消费块R recursive_branch 的多级 LevelResult:逐相邻级别(次级别 L_{k-1} / 大级别 L_k),
查 L_{k-1} 最后一个中枢是否出三类点(复用 BsBranchCalculator._third_class)且 L_{k-1} 有
同向趋势背驰(底背驰 leave=down ↔ 三买 → 向上;顶背驰 leave=up ↔ 三卖 → 向下)→ 发候选。
当下:只用 done 中枢 + 当下三类点(三类当下稳定,见审计 B5);无未来函数。

孤立、不改上游、不动 zs_upgrade(块U);消费块R LevelResult → 与 R5(升级统一到块R)前向兼容。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from chanlun.core.types import FX, ZS


@dataclass
class XiaoZhuanDaCandidate:
    """小转大候选(L044 必要条件满足、非充分):大级别 ``level`` 可能向 ``direction`` 转。"""
    level: int                  # 大级别 L_k(被转的级别)
    direction: str              # 'up'(三买 + 底背驰) / 'down'(三卖 + 顶背驰)
    necessary_zs: ZS            # L044 必要条件中枢 = 次级别最后一个中枢
    anchor_fx: FX               # 三类点出图锚(回试段终点)
    invalid: float              # 结构失效位:向上 = ZG、向下 = ZD
    sub_level: int              # 次级别 L_{k-1}


def _beichi_leave_dirs(level) -> set:
    """该级别已坐实趋势背驰(qs & is_beichi)的离开段方向集合。底背驰 leave='down'
    (跌势衰竭 → 转上)、顶背驰 leave='up'。供小转大「小级别背驰」触发的同向匹配。"""
    dirs = set()
    for dv in (getattr(level, "done_divergence", None) or []):
        if (dv is not None and getattr(dv, "is_beichi", False)
                and getattr(dv, "kind", None) == "qs"
                and getattr(dv, "leave_seg", None) is not None):
            dirs.add(dv.leave_seg._type)
    return dirs


class XiaoZhuanDaCalculator:
    """小转大检测器。无状态,每次 calculate 全量重算。"""

    def calculate(self, levels: list) -> List[XiaoZhuanDaCandidate]:
        from chanlun.core.bs_branch import BsBranchCalculator
        from chanlun.core.zs_branch import ZsBranchResult

        out: List[XiaoZhuanDaCandidate] = []
        bs = BsBranchCalculator()
        for k in range(1, len(levels)):                        # 需大级别 L_k 存在
            sub = levels[k - 1]                                # 次级别
            sub_zss = getattr(sub, "zss", None) or []
            if not sub_zss:
                continue
            last_zs = sub_zss[-1]                              # L044 必要条件:最后一个次级别中枢
            zr = ZsBranchResult(
                done_zss=sub_zss, live=[], freeze_idx=0,
                done_divergence=getattr(sub, "done_divergence", None) or [],
            )
            thirds = [p for p in bs._third_class(zr, getattr(sub, "units", None) or [])
                      if p.zs is last_zs]                      # 仅最后中枢的三类点
            if not thirds:
                continue
            bdirs = _beichi_leave_dirs(sub)                    # 次级别趋势背驰离开方向
            for p in thirds:
                if p.bs_type == "3buy" and "down" in bdirs:    # 三买 + 底背驰 → 向上小转大
                    out.append(XiaoZhuanDaCandidate(
                        level=k, direction="up", necessary_zs=last_zs,
                        anchor_fx=p.anchor_fx, invalid=last_zs.zg, sub_level=k - 1))
                elif p.bs_type == "3sell" and "up" in bdirs:   # 三卖 + 顶背驰 → 向下小转大
                    out.append(XiaoZhuanDaCandidate(
                        level=k, direction="down", necessary_zs=last_zs,
                        anchor_fx=p.anchor_fx, invalid=last_zs.zd, sub_level=k - 1))
        return out
