"""recursive_calculator.py — 缠论递归装配(子项目④)。

把 ①ZsCalculator(中枢) / ②beichi_calculator(背驰) / ③ZslxCalculator(走势类型)
交替递归，构建 中枢-L0 → 走势类型-L0 → 中枢-L1 → … 的多级层级树。

并存独立子系统：不动周期多级分析、不动 bs_point_calculator。
原文依据见 docs/chanlun_core_redesign_4_recursive_design.md §2。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.types import LINE, XD, ZS, ZSLX
from chanlun.core.zs_calculator import ZsCalculator
from chanlun.core.zslx_calculator import ZslxCalculator

# 递归层数上限——防御性护栏。走势单元数逐级收缩，正常远不及此。
_MAX_LEVELS = 50


@dataclass
class LevelResult:
    """单个递归级别的结果。"""
    level: int            # 0 = L0
    zss: List[ZS]         # 本级中枢
    zslxs: List[ZSLX]     # 本级走势类型


def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """把走势类型列表整备成下一级中枢扫描的走势单元（就地写字段后返回）。

    ZsCalculator 扫描靠构成段的 zs_high/zs_low 判重叠、靠 index 定位；ZSLX
    默认这两者为 0，喂回前必须写入：
    - zs_high/zs_low = 所含中枢的包络 [min(dd), max(gg)]（spec 决策 3）；
    - index 按 0,1,2… 重排（③ 的 _finalize 给所有 ZSLX 置 index=0）。

    前置不变量：每个 ZSLX 的 zss 非空（ZslxCalculator 仅对非空中枢段
    finalize）；若该不变量被破坏，下面的 max/min 会抛 ValueError。
    """
    for i, zslx in enumerate(zslxs):
        zslx.zs_high = max(zs.gg for zs in zslx.zss)
        zslx.zs_low = min(zs.dd for zs in zslx.zss)
        zslx.index = i
    return zslxs


def _split_one(zs: ZS) -> List[ZS]:
    """把一个 ≥9 段中枢按 (123)(456)(789)… 拆成 ⌊N/3⌋ 个三段子中枢。

    N%3≠0 时余 1~2 段并入最后一组（保证每组 ≥3）。子中枢边界口径与
    ZsCalculator 初始三段一致：zg/zd 取前三段、gg/dd 由 update_boundaries 全量。

    子中枢进入段：首组沿用原中枢进入段（可能为 None），其余组取前一组末段
    （紧邻本组核心之前的走势段）——子中枢若构成趋势即可参与趋势背驰。

    所有子中枢标记 ``_split_group_id = id(zs)`` —— ⑤ 扩展识别据此跳过「同源
    子中枢之间互相合并」(否则会立即合回原中枢、白拆),仅允许它们与外部独立
    中枢扩展。这样既保留原文 #2.1 的「9 段拆 → 上层升级」路径,又允许真正的
    跨中枢扩展(原文 #391)。
    """
    lines = zs.lines
    n = len(lines)
    groups_count = n // 3
    subs: List[ZS] = []
    idx = 0
    split_group_id = id(zs)
    for g in range(groups_count):
        size = 3 if g < groups_count - 1 else (n - idx)  # 末组吸收余数
        group = lines[idx:idx + size]
        # 子中枢进入段：首组沿用原中枢进入段，其余组取前一组末段（= 紧邻本组
        # 核心之前的走势段），使子中枢若构成趋势可参与趋势背驰。
        entry = zs.start if idx == 0 else lines[idx - 1]
        idx += size
        sub = ZS(zs_type=zs.zs_type, start=entry)
        sub.lines = group
        sub._bounds_dirty = True
        # 原文 kobo.125.1：ZG=min(g₁,g₂)、ZD=max(d₁,d₂)——只取前 2 段。
        # 与 ZsCalculator 初始三段中枢的 zg/zd 口径(同样取前 2 段)保持一致。
        sub.zg = min(s.zs_high for s in group[:2])
        sub.zd = max(s.zs_low for s in group[:2])
        sub.update_boundaries()
        sub._split_group_id = split_group_id    # ⑤ 同源标记,防扩展自合
        subs.append(sub)
    return subs


def _split_oversized(zss: List[ZS]) -> List[ZS]:
    """对中枢列表做 9 段分裂：≥9 段的中枢拆成三段子中枢，其余原样。

    落地缠论段2.1「延伸超9段升级」——单个 ≥9 段中枢经 ③划分只得 1 个盘整、
    单独无法升级；拆开后子中枢经划分、上一级扫描可聚成高级中枢。
    """
    out: List[ZS] = []
    for zs in zss:
        if len(zs.lines) <= 8:
            out.append(zs)
        else:
            out.extend(_split_one(zs))
    return out


def _merge_zss(subs: List[ZS]) -> ZS:
    """把 N(≥2)个子中枢合并为 1 个高级扩展中枢(原文 #391·走势中枢扩展)。

    高级中枢的字段口径:
      - ``gg`` / ``dd`` = 子中枢包络合并(max(sub.gg) / min(sub.dd))——原文
        定义的「瞬间波动区间」自然合并;
      - ``zg`` / ``zd`` = 子中枢核心区[ZD,ZG]的**包络**(max(sub.zg) / min(sub.dd
        →min(sub.zd))。原文(第57课 L57_03 实例「中枢扩展区间取值=三个组合
        中枢的[ZD,ZG]中的最高和最低」=[4015,4122])即子核心包络,而非交集。
        旧实现取交集(min sub.zg/max sub.zd),子核心分离时退化为 gg/dd,与原文不符。
        包络 zd=min(sub.zd)<zg=max(sub.zg) 恒成立,无需空交集兜底。
      - ``lines`` = 子中枢 lines 时序拼接;
      - ``start`` = 首子中枢的 start;``end`` = 末子中枢的 end;``done`` = 全部子完成;
      - ``expanded_with`` = subs(供消费方追溯子结构)。
    """
    gg = max(s.gg for s in subs)
    dd = min(s.dd for s in subs)
    zg = max(s.zg for s in subs if s.zg is not None)
    zd = min(s.zd for s in subs if s.zd is not None)

    merged = ZS(
        zs_type=subs[0].zs_type,
        start=subs[0].start,
        zg=zg, zd=zd, gg=gg, dd=dd,
        level=subs[0].level,
    )
    merged.end = subs[-1].end
    merged.done = all(s.done for s in subs)
    merged.real = all(getattr(s, "real", True) for s in subs)
    merged.expanded_with = list(subs)
    # 时序拼接子中枢的 lines
    merged.lines = [ln for sub in subs for ln in sub.lines]
    merged.line_num = len(merged.lines)
    # gg/dd 已显式设置,边界缓存口径直接置 clean
    merged._gg_cache = gg
    merged._dd_cache = dd
    merged._bounds_dirty = False
    return merged


def _expand_overlapping(zss: List[ZS]) -> List[ZS]:
    """合并相邻 GG/DD 包络重叠的同级别中枢(原文 #391·中枢扩展)。

    顺序扫描中枢序列,维护一个 pending 组(初始为单个中枢);每个新中枢:
      - 若与 pending 组的合并中枢 GG/DD 包络重叠 → 加入 pending 组;
      - 否则 → 收尾 pending 组(单个 → 原样、≥2 个 → 合并为扩展中枢) +
        新开 pending 组从当前中枢起。

    返回的中枢列表 index 按序号重排。
    """
    if not zss:
        return []
    out: List[ZS] = []
    pending: List[ZS] = [zss[0]]
    pending_env_gg = zss[0].gg
    pending_env_dd = zss[0].dd
    for cur in zss[1:]:
        # 跳过同源(9 段分裂出的子中枢之间)互相扩展——见 _split_one 注释。
        # pending 与 cur 同属一个 _split_group_id 时,不合并,保留原文 #2.1 升 L1 路径。
        prev_group = getattr(pending[-1], "_split_group_id", None)
        cur_group = getattr(cur, "_split_group_id", None)
        same_source = (prev_group is not None and prev_group == cur_group)

        # 判断 cur 是否与 pending 当前包络相交
        overlap = (
            not same_source
            and pending_env_gg is not None and pending_env_dd is not None
            and cur.dd is not None and cur.gg is not None
            and max(pending_env_dd, cur.dd) <= min(pending_env_gg, cur.gg)
        )
        if overlap:
            pending.append(cur)
            pending_env_gg = max(pending_env_gg, cur.gg)
            pending_env_dd = min(pending_env_dd, cur.dd)
        else:
            out.append(pending[0] if len(pending) == 1 else _merge_zss(pending))
            pending = [cur]
            pending_env_gg = cur.gg
            pending_env_dd = cur.dd
    out.append(pending[0] if len(pending) == 1 else _merge_zss(pending))
    # 重排 index
    for i, zs in enumerate(out):
        zs.index = i
    return out


class RecursiveCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def calculate(
        self, xds: List[XD], ld_provider: LdProvider, wzgx_config: str,
        frequency: Optional[str] = None,
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。

        每级：扫描(ZsCalculator) → 9段分裂 → 划分(ZslxCalculator) → 走势类型
        经 _as_units 变下一级走势单元。终止：扫不出中枢，或走势类型 <3。
        ``frequency`` 透传到 ZslxCalculator/背驰内核决定黄白线口径。
        """
        if not xds:
            return []

        results: List[LevelResult] = []
        units: List[LINE] = list(xds)
        level = 0
        while level < _MAX_LEVELS:
            # L0 构成段是线段 → 最小中枢 4 段（项目口径，偏离原文）；
            # L≥1 构成段是走势类型 → 按原文「3 个次级别走势类型重叠成
            # 中枢」用 3。与 require_alternation 同为分级参数。
            # 递归子系统**不封顶延伸**(max_zs_lines 给极大值)：本子系统的升级走
            # _split_oversized「≥9 段拆为 3 段子中枢」这条路(下一行),需要 ≥9 段中枢
            # 存在才能触发。主链路(cl.zss_calculator)用默认封顶 8 段得到「显示用的
            # 合理基础中枢」；递归这里保留原始延伸 + 拆分升级——同一数据两种合理划分
            # (走势多义性)：主链路看基础中枢、递归看升级层级。
            zss = ZsCalculator(
                require_alternation=(level == 0),
                min_zs_lines=(4 if level == 0 else 3),
                max_zs_lines=10 ** 9,
            ).calculate(units)
            # 9 段优先于扩展(spec ⑤ 决策):先把 ≥9 段中枢拆为 3 段子中枢,再做
            # 扩展识别——子中枢之间的 GG/DD 包络重叠由扩展合并,自然涌现
            # 「超 9 段中枢升级为高级中枢」的原文 #2.1 语义。
            zss = _split_oversized(zss)
            zss = _expand_overlapping(zss)
            if not zss:
                break
            zslxs = ZslxCalculator().calculate(
                zss, units, ld_provider, wzgx_config, frequency
            )
            results.append(LevelResult(level, zss, zslxs))
            if len(zslxs) < 3:
                break
            units = _as_units(zslxs)
            level += 1
        return results
