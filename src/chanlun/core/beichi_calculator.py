"""级别无关的背驰判定内核。

全部为纯函数：力度(MACD)经注入的 ld_provider 回调获取，内核不依赖 CL。
走势段用鸭子类型——BI(笔)/XD(线段)/ZSLX(走势类型) 均为 LINE 子类，
共享 .type/.start/.end/.high/.low 接口，故同一套内核适用于任意级别。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from chanlun.core.types import BI, Config, FX, LINE, ZS

# 力度 provider 类型：给定走势段的起止分型，返回 query_macd_ld 风格的 ld 字典
LdProvider = Callable[[FX, FX], dict]


def _use_huangbai(seg: LINE, frequency: Optional[str] = None) -> bool:
    """力度口径是否包含黄白线。

    低级别波动只比柱子面积；级别足够高时同时考虑黄白线。

    判据：
      - ``frequency=None`` (旧 API 兼容)：仅按对象类型——笔(BI) → 仅柱子；
        线段/走势类型 → 加黄白线。
      - ``frequency='1m'`` / ``'1min'``：笔 → 仅柱子；其它段 → 加黄白线。
      - ``frequency`` ≥ 5m（含 5m/30m/d/w/m/y）：所有段(含笔，此时笔是
        跨多根 K 线的方向段) → 全部加黄白线。
    """
    if frequency is None:
        return not isinstance(seg, BI)
    if frequency in ('1m', '1min') and isinstance(seg, BI):
        return False
    return True


def _xingao_xindi(seg_a: LINE, seg_b: LINE) -> bool:
    """创新高/新低前提：未创新高/新低则不构成背驰。

    seg_a 在前、seg_b 在后、同向。up 段要求 seg_b 高点高于 seg_a；
    down 段要求 seg_b 低点低于 seg_a。
    """
    if seg_b.type == "up":
        return seg_b.high > seg_a.high
    return seg_b.low < seg_a.low


def _ld_area(ld: dict, direction: str) -> float:
    """柱子同向面积：up 看红柱和 up_sum，down 看绿柱和 down_sum。"""
    return ld["hist"]["up_sum"] if direction == "up" else ld["hist"]["down_sum"]


def _ld_height(ld: dict, direction: str) -> float:
    """柱子高度（伸长高度/乖离极限）：up 看最高红柱 ``hist.max``，down 看最深
    绿柱 ``hist.min``。数据由 query_macd_ld 现成提供（``types/interface.py`` 的
    ld 字典含 hist.max/min）。"""
    return ld["hist"]["max"] if direction == "up" else ld["hist"]["min"]


def _bar_decays(ld_a: dict, ld_b: dict, direction: str) -> bool:
    """柱子维度衰竭 = 面积衰竭 **AND** 柱高度不创新高(用户口径 2026-06-26: 收紧, 原为 OR)。

    面积与柱高度取 AND，两者都弱才算柱子力度衰竭(收紧背驰; 原 OR 任一弱即判, 偏松,
    审计 E-MED-2)。
    up：柱高 ``hist.max`` 后 < 前 = 不创新高 = 衰竭；
    down：柱深 ``hist.min`` 后 > 前(更接近 0 轴 = 更浅)= 衰竭。
    """
    if _ld_area(ld_b, direction) >= _ld_area(ld_a, direction):    # 面积未衰 → 柱子不算衰竭
        return False
    if direction == "up":                                          # 面积衰 且 柱高度不创新高
        return _ld_height(ld_b, direction) < _ld_height(ld_a, direction)
    return _ld_height(ld_b, direction) > _ld_height(ld_a, direction)


def _ld_huangbai(ld: dict, direction: str) -> float:
    """黄白线高度：up 看 DIF 最大值（离 0 轴最远），down 看 DIF 最小值。"""
    return ld["dif"]["max"] if direction == "up" else ld["dif"]["min"]


def _ld_decays(
    seg_a: LINE, seg_b: LINE, ld_provider: LdProvider,
    frequency: Optional[str] = None,
) -> bool:
    """力度是否衰竭（seg_b 在前者 seg_a 之后、同向）。

    步骤2 柱子衰竭：面积衰竭 **AND** 柱高度不创新高(用户口径收紧, 见 _bar_decays)。
    步骤3 黄白线衰竭(仅线段及以上,见 _use_huangbai)：只看 DIF 高度衰减
    (用户口径 2026-06-26 删"DIF 回抽 0 轴"宽口径, 见 _huangbai_decays)。
    """
    direction = seg_b.type
    ld_a = ld_provider(seg_a.start, seg_a.end)
    ld_b = ld_provider(seg_b.start, seg_b.end)

    # 步骤2：柱子维度衰竭 = 面积衰竭 OR 柱高度不创新高
    if not _bar_decays(ld_a, ld_b, direction):
        return False

    # 步骤3：黄白线衰竭——仅线段及以上(笔跳过,见 _use_huangbai)
    if _use_huangbai(seg_b, frequency):
        if not _huangbai_decays(ld_a, ld_b, direction):
            return False
    return True


def _huangbai_decays(ld_a: dict, ld_b: dict, direction: str) -> bool:
    """黄白线是否衰竭(用户口径 2026-06-26: 收紧——删"DIF 回抽 0 轴"宽口径, 只看 DIF 高度衰减)。

    DIF 高度衰减——up: seg_b DIF.max < seg_a DIF.max;down: seg_b DIF.min > seg_a DIF.min。
    (原"DIF 回抽 0 轴(min≤0 / max≥0)"宽口径在多数真实段近乎恒真、旁路黄白线腿致 is_beichi
    退化为单看柱子, 审计 E-HIGH-1 证偏松——按用户口径删除该分支。)
    """
    hb_a = _ld_huangbai(ld_a, direction)
    hb_b = _ld_huangbai(ld_b, direction)
    if direction == "up":
        return hb_b < hb_a
    return hb_b > hb_a


def is_beichi(
    seg_a: LINE, seg_b: LINE, ld_provider: LdProvider,
    frequency: Optional[str] = None,
) -> bool:
    """背驰原语：seg_b（在后）相对 seg_a（在前、同向）是否背驰。

    背驰 = 力度衰竭。判定两步——
      1. 创新高/新低前提；不满足直接非背驰。
      2. 力度衰竭（柱子面积 + 级别相关黄白线）。

    ``frequency`` 传入时按真实 K 线周期决定是否使用黄白线,
    见 ``_use_huangbai``;缺省按 seg 类型回退。
    """
    if not _xingao_xindi(seg_a, seg_b):
        return False
    return _ld_decays(seg_a, seg_b, ld_provider, frequency)


def core_envelope(zs: ZS) -> Tuple[float, float]:
    """中枢本体包络 (GG, DD)：取定义中枢的前 3 段，剔除延伸/离开段的远摆。

    GD 档比较包络时，若用完整 gg/dd，离开段/延伸的远摆会把包络撑爆、相邻中枢
    恒重叠、趋势恒判不出。取前 3 段本体——zs_branch 校正后的中枢离开段已剥离、
    前3段即本体；旧 ZsCalculator 中枢的离开段/延伸在 ``lines[3:]``，前3段亦正确
    剔除。无 lines 时退化用 ``zs.gg/zs.dd``。

    与 ``zs_branch.body_envelope`` 同口径（那边返回 (dd,gg)，此处按 ``is_qs`` 调用
    约定返回 (gg,dd)）。
    """
    body = zs.lines[:3] if zs.lines else None
    if not body:
        return zs.gg, zs.dd
    return max(ln.zs_high for ln in body), min(ln.zs_low for ln in body)


def is_qs(
    one_zs: ZS, two_zs: ZS, wzgx_config: str, use_core_envelope: bool = False
) -> Optional[str]:
    """趋势关系判定：相邻两中枢是否「依次同向」构成趋势。

    趋势需至少两个依次同向的走势中枢。「依次同向」的松紧由 wzgx_config 给定——
    「一个中枢整体在另一个之上/下」对应严格档 GD；默认档 ZGGDD 较宽，沿用代码库
    现有默认配置。
    返回 'up' / 'down' / None。

    ``use_core_envelope``(仅作用于 GD 档)：True 时 GD 比较用「中枢本体包络」
    (core_envelope, 剔除围绕中枢的波动/延伸/离开段)而非完整 gg/dd——这是买卖点
    链路(cl.beichi_qs / zss_is_qs)的正确趋势口径(否则远摆撑爆包络、趋势恒判不出)。
    递归走势类型划分(zslx)保持 False(完整 gg/dd), 避免本体口径让走势类型变少、
    递归被饿死。
    """
    if wzgx_config == Config.ZS_WZGX_ZGD.value:
        # 宽松：zg 与 zd
        if one_zs.zg < two_zs.zd:
            return "up"
        if one_zs.zd > two_zs.zg:
            return "down"
    elif wzgx_config == Config.ZS_WZGX_ZGGDD.value:
        # 较宽松：zg 与 dd、zd 与 gg
        if one_zs.zg < two_zs.dd:
            return "up"
        if one_zs.zd > two_zs.gg:
            return "down"
    elif wzgx_config == Config.ZS_WZGX_GD.value:
        # 严格档：比较包络 gg/dd。买卖点链路用「中枢本体包络」(core_envelope,
        # 剔除围绕中枢的波动/延伸/离开段)——否则波动的远摆极值撑爆包络、相邻中枢
        # 恒重叠、趋势恒判不出。递归走势类型划分保持完整 gg/dd(use_core_envelope=False)。
        if use_core_envelope:
            one_gg, one_dd = core_envelope(one_zs)
            two_gg, two_dd = core_envelope(two_zs)
        else:
            one_gg, one_dd = one_zs.gg, one_zs.dd
            two_gg, two_dd = two_zs.gg, two_zs.dd
        if one_gg < two_dd:
            return "up"
        if one_dd > two_gg:
            return "down"
    return None


def beichi_pz(
    zs: ZS, now_seg: LINE, ld_provider: LdProvider,
    frequency: Optional[str] = None,
) -> Tuple[bool, Optional[LINE]]:
    """盘整背驰：1 个中枢的盘整中，离开段相对**进入段**是否力度衰竭。

    比较对象 = 中枢进入段 ``zs.start``(围绕中枢的前后两段同向次级别走势比较,
    与 zs_branch._divergence_for 同口径;审计 F4——原「中枢内最近同向段」在中枢
    延伸后比较对象漂移为延伸段,非盘整背驰本义,该语义拆到
    ``beichi_zs_oscillation`` 供中枢震荡力度比较)。
    无进入段(开头中枢)/进入段与离开段异向/端点缺失 → 不判(False, None)。
    ``frequency`` 透传到 ``is_beichi`` 决定黄白线口径。
    返回 (是否背驰, 比较的走势段=进入段)。
    """
    a = getattr(zs, "start", None)
    if (a is None or getattr(a, "type", None) != now_seg.type
            or a.start is None or a.end is None):
        return False, None
    return is_beichi(a, now_seg, ld_provider, frequency), a


def beichi_zs_oscillation(
    zs: ZS, now_seg: LINE, ld_provider: LdProvider,
    frequency: Optional[str] = None,
) -> Tuple[bool, Optional[LINE]]:
    """中枢震荡力度比较：当下段相对中枢内(除末段外)最近同向段是否衰竭。

    即原 beichi_pz 的「最近同向段」语义——不是盘整背驰(那是进入 vs 离开,见
    ``beichi_pz``),而是中枢震荡内部前后两次同向摆动的力度对比;legacy 2 类
    经验法条件 B(bs_point_calculator)用它判「创新低/高但对震荡构成背驰」。
    返回 (是否背驰, 比较的走势段)。
    """
    if len(zs.lines) < 2:
        return False, None
    compare_line = None
    for line in reversed(zs.lines[:-1]):
        if line.type == now_seg.type:
            compare_line = line
            break
    if compare_line is None:
        return False, None
    return is_beichi(compare_line, now_seg, ld_provider, frequency), compare_line


def beichi_qs(
    lines: List[LINE],
    zss: List[ZS],
    now_seg: LINE,
    ld_provider: LdProvider,
    wzgx_config: str,
    frequency: Optional[str] = None,
    use_core_envelope: bool = False,
) -> Tuple[bool, List[LINE]]:
    """趋势背驰：≥2 个同向中枢的趋势中，离开末中枢的段相对连接两中枢的同向段是否衰竭。

    两中枢趋势结构 = [中枢0] → A段 → [B=末中枢] → C段。
      - A 段 = 连接两个中枢的段 = 进入末中枢 last_zs 的段 = ``last_zs.start``
        (即 prev_zs 的离开段);
      - B = 末中枢 last_zs;C 段 = 离开末中枢的段 = ``now_seg``。
    比较 C 段与 A 段力度，C < A 即标准趋势背驰。
    注意：连接段 A(``last_zs.start``) 与「前面低部回拉的第一段」(``prev_zs.start``)
    是两根不同的段，比较对象须取连接段 ``last_zs.start``。

    返回非背驰的情形：
    - ``len(zss) < 2`` —— 仅一个中枢时的背驰按盘整背驰处理；
    - 两中枢不构成同向趋势（``is_qs`` 失败）；
    - 末中枢无进入段（开头中枢）或其方向与 now_seg 不一致 —— A/C 同向前提不满足。

    ``lines`` 参数已不消费——保留仅为薄壳调用方接口兼容。

    返回 (是否背驰, [比较的走势段])。
    """
    if len(zss) < 2:
        return False, []

    last_zs, prev_zs = zss[-1], zss[-2]

    # 最后两个中枢须依次同向构成趋势，且趋势方向与当前段一致
    qs_direction = is_qs(prev_zs, last_zs, wzgx_config, use_core_envelope=use_core_envelope)
    if not qs_direction or qs_direction != now_seg.type:
        return False, []

    # A 段 = 连接两个中枢的段 = 进入末中枢的段 = last_zs.start
    # (= prev_zs 的离开段)。注意区别于「前面低部回拉的第一段」prev_zs.start。
    compare_line = last_zs.start
    if compare_line is None or getattr(compare_line, "type", None) != now_seg.type:
        return False, []

    return is_beichi(compare_line, now_seg, ld_provider, frequency), [compare_line]
