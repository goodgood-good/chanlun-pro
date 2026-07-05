"""小转大(小背驰-大转折)检测器(只读结构预警)。

小级别(顶/底)背驰引发大级别(向下/向上)的必要条件是该级别走势最后一个次级别
中枢出现第三类(卖/买)点;由于只是必要条件而非充分条件，本检测器只产候选/预警,
不产定性买卖点。

消费 recursive_branch 的多级 LevelResult:逐相邻级别(次级别 L_{k-1} / 大级别 L_k),
查 L_{k-1} 最后一个中枢是否出三类点(复用 BsBranchCalculator._third_class)且 L_{k-1} **最近
一次坐实**趋势背驰同向、其终点不晚于三类点锚(底背驰 leave=down ↔ 三买 → 向上;顶背驰
leave=up ↔ 三卖 → 向下)→ 发候选。
当下:只用 done 中枢 + 当下三类点(三类当下稳定);无未来函数。

孤立、不改上游。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from chanlun.core.types import FX, ZS


@dataclass
class XiaoZhuanDaCandidate:
    """小转大候选(必要条件满足、非充分):大级别 ``level`` 可能向 ``direction`` 转。"""
    level: int                  # 大级别 L_k(被转的级别)
    direction: str              # 'up'(三买 + 底背驰) / 'down'(三卖 + 顶背驰)
    necessary_zs: ZS            # 必要条件中枢 = 次级别最后一个中枢
    anchor_fx: FX               # 三类点出图锚(回试段终点)
    invalid: float              # 结构失效位:向上 = ZG、向下 = ZD
    sub_level: int              # 次级别 L_{k-1}
    sub_trend_zs_count: int = 1  # 次级别末尾连续同向中枢数(L043:20 小转大高发于趋势
    # 冲顶/赶底:≥2=真趋势背驰所在,越深越接近冲顶/赶底,供候选质量分级)


def _tail_trend_depth(zss) -> int:
    """末尾连续「依次同向」中枢数(classify_rel 趋势关系链长+1);无趋势关系=1。

    L043:20:小转大最值得注意的出现在趋势的冲顶/赶底中——深度≥2 表示背驰发生在
    真趋势末端,越深越接近冲顶/赶底。仅读已完成中枢当下几何,无未来函数。"""
    from chanlun.core.zs_branch import classify_rel

    if len(zss) < 2:
        return len(zss)
    rel0 = classify_rel(zss[-2], zss[-1])
    if rel0 not in ("trend_up", "trend_down"):
        return 1
    n = 2
    for i in range(len(zss) - 2, 0, -1):
        if classify_rel(zss[i - 1], zss[i]) == rel0:
            n += 1
        else:
            break
    return n


def _last_settled_beichi(level):
    """该级别时序最后一个坐实趋势背驰(qs & is_beichi):返回 (离开方向, 背驰段终点 k_index),
    无则 (None, None)。小转大的「小级别背驰」须是引发当下转折的那次背驰——全历史方向集合
    会让陈年背驰给此后所有三类点配对(审计 F5),故只认最近一次;其终点须不晚于三类点锚
    (由调用方校验)。底背驰 leave=down(跌势衰竭 → 转上)、顶背驰 leave=up。"""
    best_dir = None
    best_k = None
    for dv in (getattr(level, "done_divergence", None) or []):
        if (dv is None or not getattr(dv, "is_beichi", False)
                or getattr(dv, "kind", None) != "qs"):
            continue
        seg = getattr(dv, "leave_seg", None)
        end = getattr(seg, "end", None) if seg is not None else None
        k = getattr(getattr(end, "k", None), "k_index", None) if end is not None else None
        if k is None:
            continue
        if best_k is None or k >= best_k:
            best_dir, best_k = seg._type, k
    return best_dir, best_k


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
            last_zs = sub_zss[-1]                              # 必要条件:最后一个次级别中枢
            zr = ZsBranchResult(
                done_zss=sub_zss, live=[], freeze_idx=0,
                done_divergence=getattr(sub, "done_divergence", None) or [],
            )
            thirds = [p for p in bs._third_class(zr, getattr(sub, "units", None) or [])
                      if p.zs is last_zs]                      # 仅最后中枢的三类点
            if not thirds:
                continue
            bdir, bdir_k = _last_settled_beichi(sub)       # 最近一次坐实趋势背驰(方向,终点)
            if bdir is None:
                continue
            for p in thirds:
                anchor_k = getattr(getattr(p.anchor_fx, "k", None), "k_index", None)
                if (anchor_k is not None and bdir_k is not None
                        and bdir_k > anchor_k):
                    continue                               # 背驰晚于三类点 → 非本次转折诱因
                if p.bs_type == "3buy" and bdir == "down":   # 三买 + 最近为底背驰 → 向上小转大
                    out.append(XiaoZhuanDaCandidate(
                        level=k, direction="up", necessary_zs=last_zs,
                        anchor_fx=p.anchor_fx, invalid=last_zs.zg, sub_level=k - 1,
                        sub_trend_zs_count=_tail_trend_depth(sub_zss)))
                elif p.bs_type == "3sell" and bdir == "up":  # 三卖 + 最近为顶背驰 → 向下小转大
                    out.append(XiaoZhuanDaCandidate(
                        level=k, direction="down", necessary_zs=last_zs,
                        anchor_fx=p.anchor_fx, invalid=last_zs.zd, sub_level=k - 1,
                        sub_trend_zs_count=_tail_trend_depth(sub_zss)))
        return out
