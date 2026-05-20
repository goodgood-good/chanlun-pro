"""tests/core/test_beichi_calculator.py — 级别无关背驰内核(beichi_calculator)测试。

缠论原文(《缠中说禅股市技术理论解释2017》第二章·第四节《背驰与盘整背驰》)：
  背驰 = 趋势力度比上一次趋势力度弱；盘整背驰 = 盘整中当下笔/线段比前一笔/线段弱。
原文细则(第三章·第二十五节实战例)：
  - 力度口径与级别相关：1分钟以下级别只比柱子面积；1分钟级别及以上加黄白线。
  - 创新高是前提：「背驰如果没有创新高，是不存在的」。

这些用例直接构造受控走势段 + 假 ld_provider 喂入内核，不走 K 线流水线。
"""

from __future__ import annotations

from chanlun.core import beichi_calculator as bc
from chanlun.core.cl_interface import BI, CLKline, FX, XD, ZSLX, Level


def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(cls, index: int, _type: str, start_val: float, end_val: float):
    """构造一根走势段。cls ∈ {BI, XD, ZSLX}。

    up 段起点底分型、终点顶分型；down 段相反。LINE.__init__ 会据此算 high/low。
    """
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    if cls is ZSLX:
        return ZSLX(zslx_level=Level.M1, start=start, end=end, _type=_type, index=index)
    return cls(start=start, end=end, _type=_type, index=index)


def test_use_huangbai_bi_is_area_only():
    """笔(BI)+ 缺省 frequency → 仅柱子面积口径(向后兼容回退口径)。"""
    bi = _seg(BI, 0, "up", 4, 8)
    assert bc._use_huangbai(bi) is False


def test_use_huangbai_xd_and_zslx_use_huangbai():
    """线段(XD)/走势类型(ZSLX)+ 缺省 frequency → 双重确认,需看黄白线。"""
    assert bc._use_huangbai(_seg(XD, 0, "up", 4, 8)) is True
    assert bc._use_huangbai(_seg(ZSLX, 0, "up", 4, 8)) is True


def test_use_huangbai_frequency_1m_bi_is_area_only():
    """frequency='1m' + 笔 → 仅柱子面积(笔对应 1m 以下波动,原文细则1)。"""
    bi = _seg(BI, 0, "up", 4, 8)
    assert bc._use_huangbai(bi, frequency="1m") is False


def test_use_huangbai_frequency_1m_xd_uses_huangbai():
    """frequency='1m' + 线段 → 黄白线(线段是 1m 级别走势类型,原文细则1)。"""
    xd = _seg(XD, 0, "up", 4, 8)
    assert bc._use_huangbai(xd, frequency="1m") is True


def test_use_huangbai_frequency_5m_bi_also_uses_huangbai():
    """frequency='5m' + 笔 → 黄白线(5m 周期下笔已超 1m 级别,原文细则1)。

    旧实现 ``not isinstance(seg, BI)`` 在 5m/30m/d 等周期下仍按"仅柱子"判错——
    此时笔本身是 K 线层方向段、跨度远超 1m。
    """
    bi = _seg(BI, 0, "up", 4, 8)
    assert bc._use_huangbai(bi, frequency="5m") is True


def test_use_huangbai_frequency_30m_bi_uses_huangbai():
    """frequency='30m' + 笔 → 黄白线(同上,30m 笔已远超 1m 级别)。"""
    bi = _seg(BI, 0, "up", 4, 8)
    assert bc._use_huangbai(bi, frequency="30m") is True


def test_xingao_xindi_up_requires_new_high():
    """up 段：后段创出更高的高点 → True；未创新高 → False。"""
    seg_a = _seg(XD, 0, "up", 4, 8)    # 高点 8
    seg_b_new = _seg(XD, 2, "up", 5, 10)   # 高点 10 > 8
    seg_b_old = _seg(XD, 2, "up", 5, 7)    # 高点 7 < 8
    assert bc._xingao_xindi(seg_a, seg_b_new) is True
    assert bc._xingao_xindi(seg_a, seg_b_old) is False


def test_xingao_xindi_down_requires_new_low():
    """down 段：后段创出更低的低点 → True；未创新低 → False。"""
    seg_a = _seg(XD, 0, "down", 10, 5)   # 低点 5
    seg_b_new = _seg(XD, 2, "down", 9, 3)  # 低点 3 < 5
    seg_b_old = _seg(XD, 2, "down", 9, 6)  # 低点 6 > 5
    assert bc._xingao_xindi(seg_a, seg_b_new) is True
    assert bc._xingao_xindi(seg_a, seg_b_old) is False


def _ld(up_sum=0.0, down_sum=0.0, dif_max=0.0, dif_min=0.0) -> dict:
    """构造 query_macd_ld 风格的 ld 字典（只填内核会读的字段）。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def test_ld_decays_up_area_weaker():
    """up 段：后段红柱面积更小 → 力度衰竭 True。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    ld_a, ld_b = _ld(up_sum=100, dif_max=3), _ld(up_sum=50, dif_max=2)
    provider = {(0, 1): ld_a, (2, 3): ld_b}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_up_area_not_weaker():
    """up 段：后段红柱面积没变小 → False。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=50, dif_max=2), (2, 3): _ld(up_sum=100, dif_max=1)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is False


def test_ld_decays_xd_needs_huangbai_too():
    """线段：柱子面积衰竭但黄白线既未高度衰减、也未回抽 0 轴 → False（双重确认）。

    黄白线衰竭判据(原文细则1)有两路:高度衰减 OR 回抽 0 轴。要让 seg_b 的
    黄白线 **彻底未衰竭** 须两路都不成立——dif_max 不下降 + dif_min 始终在
    0 轴之上(up 段)。
    """
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    # up_sum 衰竭(100→50)；dif_max 反升(2→3) → 高度未衰减;
    # dif_min 在 0 轴上方(>0) → 未回抽 0 轴
    provider = {
        (0, 1): _ld(up_sum=100, dif_max=2, dif_min=1),
        (2, 3): _ld(up_sum=50, dif_max=3, dif_min=1),
    }
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is False


def test_ld_decays_xd_huangbai_decays_via_zero_axis_pullback():
    """线段(up 段)：DIF 高度没下降,但回抽到 0 轴下方 → 黄白线衰竭(原文细则1)。

    原文「同时考虑黄白线回抽 0 轴的情况」——up 段中 DIF 通常在 0 轴上方,
    若过程中 DIF 跌穿/触及 0 轴(min<=0),即为衰竭信号,即便末值还在高位。
    """
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    # up_sum 衰竭(100→50);dif_max 没下降(2→3) 但 dif_min=-1 < 0 → 回抽 0 轴
    provider = {
        (0, 1): _ld(up_sum=100, dif_max=2, dif_min=1),
        (2, 3): _ld(up_sum=50, dif_max=3, dif_min=-1),
    }
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_xd_huangbai_decays_via_zero_axis_pullback_down():
    """线段(down 段)：DIF 高度没"衰减",但回抽到 0 轴上方 → 黄白线衰竭。

    down 段:DIF 通常在 0 轴下方,若过程中 DIF 升触/穿过 0 轴(max>=0),
    也是衰竭。
    """
    seg_a, seg_b = _seg(XD, 0, "down", 10, 5), _seg(XD, 2, "down", 9, 3)
    # down_sum 衰竭(100→50);dif_min 没上移(-2→-3) 但 dif_max=1>0 → 触及 0 轴
    provider = {
        (0, 1): _ld(down_sum=100, dif_min=-2, dif_max=-1),
        (2, 3): _ld(down_sum=50, dif_min=-3, dif_max=1),
    }
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_bi_skips_huangbai():
    """笔：只比柱子面积，黄白线没衰竭也不影响 → True（原文：1分钟以下只比柱子）。"""
    seg_a, seg_b = _seg(BI, 0, "up", 4, 8), _seg(BI, 2, "up", 5, 10)
    # 与上一用例同样的数据：up_sum 衰竭、dif_max 没衰竭
    provider = {(0, 1): _ld(up_sum=100, dif_max=2), (2, 3): _ld(up_sum=50, dif_max=3)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_down_uses_green_area_and_dif_min():
    """down 段：比绿柱面积 down_sum；黄白线衰竭 = dif_min 上移(less negative)。"""
    seg_a, seg_b = _seg(XD, 0, "down", 10, 5), _seg(XD, 2, "down", 9, 3)
    # down_sum 衰竭(100→50)；dif_min 由 -3 上移到 -2 → 黄白线衰竭
    provider = {(0, 1): _ld(down_sum=100, dif_min=-3), (2, 3): _ld(down_sum=50, dif_min=-2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_is_beichi_true_when_new_high_and_decay():
    """创新高 + 力度衰竭 → 背驰。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=100, dif_max=3), (2, 3): _ld(up_sum=50, dif_max=2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc.is_beichi(seg_a, seg_b, ldp) is True


def test_is_beichi_false_without_new_high():
    """没创新高 → 直接非背驰，即使力度衰竭（原文细则2）。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 7)  # 高点 7 < 8
    provider = {(0, 1): _ld(up_sum=100, dif_max=3), (2, 3): _ld(up_sum=50, dif_max=2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc.is_beichi(seg_a, seg_b, ldp) is False


def test_is_beichi_false_when_strength_not_decayed():
    """创新高但力度没衰竭 → 非背驰。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=50, dif_max=2), (2, 3): _ld(up_sum=100, dif_max=3)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc.is_beichi(seg_a, seg_b, ldp) is False


from chanlun.core.cl_interface import Config, ZS


def _zs(zg, zd, gg, dd) -> ZS:
    """构造一个中枢（只填趋势判定会读的边界字段）。"""
    z = ZS(zs_type="xd", start=None, zg=zg, zd=zd, gg=gg, dd=dd)
    return z


def test_is_qs_zggdd_up():
    """默认档 ZGGDD：前 zg < 后 dd → 向上趋势。"""
    one, two = _zs(zg=8, zd=5, gg=9, dd=4), _zs(zg=15, zd=12, gg=16, dd=10)
    assert bc.is_qs(one, two, Config.ZS_WZGX_ZGGDD.value) == "up"


def test_is_qs_zggdd_down():
    """默认档 ZGGDD：前 zd > 后 gg → 向下趋势。"""
    one, two = _zs(zg=15, zd=12, gg=16, dd=10), _zs(zg=8, zd=5, gg=11, dd=4)
    assert bc.is_qs(one, two, Config.ZS_WZGX_ZGGDD.value) == "down"


def test_is_qs_none_when_overlap():
    """两中枢重叠 → 无趋势 None。"""
    one, two = _zs(zg=8, zd=5, gg=9, dd=4), _zs(zg=9, zd=6, gg=10, dd=5)
    assert bc.is_qs(one, two, Config.ZS_WZGX_ZGGDD.value) is None


def test_is_qs_gd_strict():
    """严格档 GD：前 gg < 后 dd 才算向上趋势。"""
    one, two = _zs(zg=8, zd=5, gg=9, dd=4), _zs(zg=15, zd=12, gg=16, dd=10)
    assert bc.is_qs(one, two, Config.ZS_WZGX_GD.value) == "up"
    # gg(9) 不小于 dd(8) → 严格档不成立
    one2, two2 = _zs(zg=8, zd=5, gg=9, dd=4), _zs(zg=10, zd=8, gg=11, dd=8)
    assert bc.is_qs(one2, two2, Config.ZS_WZGX_GD.value) is None


def test_beichi_pz_true():
    """盘整背驰：离开段相对中枢内前一同向段力度衰竭 → True。"""
    core_a = _seg(XD, 0, "up", 4, 8)     # 中枢内同向段
    core_b = _seg(XD, 2, "down", 8, 5)
    core_c = _seg(XD, 4, "up", 5, 8)     # zs.lines[-1]（不参与比较）
    now = _seg(XD, 6, "up", 5, 10)       # 离开段，创新高 10 > 8
    zs = _zs(zg=8, zd=5, gg=8, dd=4)
    zs.lines = [core_a, core_b, core_c]
    provider = {(0, 1): _ld(up_sum=100, dif_max=3),   # core_a
                (6, 7): _ld(up_sum=50, dif_max=2)}    # now
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]

    is_bc, compare = bc.beichi_pz(zs, now, ldp)
    assert is_bc is True
    assert compare is core_a


def test_beichi_pz_false_when_no_same_direction_compare_line():
    """中枢线段不足 2 段 → 非背驰。"""
    now = _seg(XD, 6, "down", 8, 3)
    zs = _zs(zg=8, zd=5, gg=8, dd=4)
    zs.lines = [_seg(XD, 0, "up", 4, 8)]
    is_bc, compare = bc.beichi_pz(zs, now, lambda s, e: {})
    assert is_bc is False
    assert compare is None


def test_beichi_qs_false_when_less_than_two_zs():
    """不足 2 个中枢 → 不是趋势背驰（原文：第一中枢的背驰只算盘整背驰）。"""
    now = _seg(XD, 10, "up", 5, 12)
    is_bc, compare = bc.beichi_qs([], [_zs(zg=8, zd=5, gg=9, dd=4)], now,
                                  lambda s, e: {}, Config.ZS_WZGX_ZGGDD.value)
    assert is_bc is False
    assert compare == []


def test_beichi_qs_true():
    """趋势背驰：≥2 同向中枢，离开末中枢段相对连接前一对中枢的同向段衰竭。

    原文 A/B/C（第二章·第四节·四）：A 段 = 连接前两个中枢的同向走势段 =
    进入前一中枢的段 `prev_zs.start`。C 段 = 离开末中枢的段 `now_seg`。
    """
    # A 段 = prev_zs.start（进入前一中枢的同向段）
    entry_prev = _seg(XD, 2, "up", 4, 8)        # 高点 8
    prev_zs = _zs(zg=8, zd=5, gg=9, dd=4)
    prev_zs.start = entry_prev
    last_zs = _zs(zg=15, zd=12, gg=16, dd=10)   # 与 prev_zs 构成向上趋势
    now = _seg(XD, 20, "up", 12, 20)            # 离开末中枢段，创新高 20 > 8
    # `lines` 参数已在新口径下不消费（A 段直接取 prev_zs.start），保留传入兼容
    lines: list = []
    provider = {(2, 3): _ld(up_sum=100, dif_max=3),    # entry_prev = A 段
                (20, 21): _ld(up_sum=40, dif_max=1)}   # now = C 段
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]

    is_bc, compare = bc.beichi_qs(lines, [prev_zs, last_zs], now, ldp,
                                  Config.ZS_WZGX_ZGGDD.value)
    assert is_bc is True
    assert compare == [entry_prev]


def test_beichi_qs_compare_line_is_prev_zs_entry():
    """A 段身份固化：原文 A/B/C 中 A 段 = prev_zs 的进入段，不是"再前一根同向段"。

    旧实现取 `end_k <= prev_zs.start.start.k.k_index` 的最后一根同向段——
    正好把 prev_zs.start 排除掉，错位一段（取到 prev_zs.start 前面那根）。
    本测试明确断言 A 段就是 prev_zs.start 本身，固化原文严格口径。
    """
    # 一根更早的同向段——按旧实现就是它被错取为 A 段
    seg_earlier = _seg(XD, 0, "up", 3, 6)
    entry_prev = _seg(XD, 2, "up", 4, 8)
    prev_zs = _zs(zg=8, zd=5, gg=9, dd=4)
    prev_zs.start = entry_prev
    last_zs = _zs(zg=15, zd=12, gg=16, dd=10)
    now = _seg(XD, 20, "up", 12, 20)
    lines = [seg_earlier, entry_prev]
    # 关键设计:让 seg_earlier 与 now "看起来满足背驰"(力度衰竭+创新高),
    # 但让 entry_prev 与 now "不满足背驰"——则旧实现 True、新实现 False。
    # 这样断言区分出两种语义。
    provider = {
        (0, 1): _ld(up_sum=50, dif_max=1),    # seg_earlier: 力度小,now 反而更大
        (2, 3): _ld(up_sum=100, dif_max=3),   # entry_prev: 力度大,now 衰竭
        (20, 21): _ld(up_sum=80, dif_max=2),  # now
    }
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]

    is_bc, compare = bc.beichi_qs(lines, [prev_zs, last_zs], now, ldp,
                                  Config.ZS_WZGX_ZGGDD.value)
    # 新口径:A = entry_prev,力度 100→80 衰竭 + dif_max 3→2 衰竭 → 背驰 True
    assert is_bc is True
    assert compare == [entry_prev]


def test_beichi_qs_false_when_prev_zs_has_no_entry():
    """prev_zs 无进入段（开头中枢 / 9段分裂首组无 entry）→ 趋势背驰不成立。

    原文 A/B/C 要求 A 段存在；若进入前一中枢的同向段缺失，按原文降级为
    非趋势背驰（不能识别为标准趋势背驰）。
    """
    prev_zs = _zs(zg=8, zd=5, gg=9, dd=4)
    prev_zs.start = None                         # 无进入段
    last_zs = _zs(zg=15, zd=12, gg=16, dd=10)
    now = _seg(XD, 20, "up", 12, 20)
    is_bc, compare = bc.beichi_qs([], [prev_zs, last_zs], now,
                                  lambda s, e: {}, Config.ZS_WZGX_ZGGDD.value)
    assert is_bc is False
    assert compare == []


def test_beichi_qs_false_when_prev_zs_entry_wrong_direction():
    """prev_zs 进入段方向与 now_seg 不同 → 非趋势背驰。

    9 段分裂子中枢非首组的 entry 是"前一组的离开段"，按方向交替它与本组首段
    反向、与离开本中枢的 now_seg 也反向——原文 A/C 须同向，不同向直接否决。
    """
    entry_prev_down = _seg(XD, 2, "down", 8, 4)  # 方向 down
    prev_zs = _zs(zg=8, zd=5, gg=9, dd=4)
    prev_zs.start = entry_prev_down
    last_zs = _zs(zg=15, zd=12, gg=16, dd=10)
    now = _seg(XD, 20, "up", 12, 20)             # 方向 up
    is_bc, compare = bc.beichi_qs([], [prev_zs, last_zs], now,
                                  lambda s, e: {}, Config.ZS_WZGX_ZGGDD.value)
    assert is_bc is False
    assert compare == []


def test_beichi_qs_false_when_not_qs():
    """两中枢不构成趋势（重叠）→ 非趋势背驰。"""
    prev_zs = _zs(zg=8, zd=5, gg=9, dd=4)
    prev_zs.start = _seg(XD, 2, "up", 4, 8)
    last_zs = _zs(zg=9, zd=6, gg=10, dd=5)      # 与 prev_zs 重叠 → 无趋势
    now = _seg(XD, 20, "up", 12, 20)
    is_bc, compare = bc.beichi_qs([_seg(XD, 0, "up", 3, 7)], [prev_zs, last_zs],
                                  now, lambda s, e: {}, Config.ZS_WZGX_ZGGDD.value)
    assert is_bc is False
    assert compare == []


import pytest


@pytest.mark.parametrize("cls", [XD, ZSLX])
def test_is_beichi_level_agnostic(cls):
    """级别无关性：XD(线段)与 ZSLX(走势类型)在同一组数据上结论一致。"""
    seg_a, seg_b = _seg(cls, 0, "up", 4, 8), _seg(cls, 2, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=100, dif_max=3), (2, 3): _ld(up_sum=50, dif_max=2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc.is_beichi(seg_a, seg_b, ldp) is True


@pytest.mark.parametrize("cls", [XD, ZSLX])
def test_beichi_pz_level_agnostic(cls):
    """级别无关性：盘整背驰对线段与走势类型行为一致。"""
    zs = _zs(zg=8, zd=5, gg=8, dd=4)
    zs.lines = [_seg(cls, 0, "up", 4, 8), _seg(cls, 2, "down", 8, 5),
                _seg(cls, 4, "up", 5, 8)]
    now = _seg(cls, 6, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=100, dif_max=3), (6, 7): _ld(up_sum=50, dif_max=2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    is_bc, _ = bc.beichi_pz(zs, now, ldp)
    assert is_bc is True
