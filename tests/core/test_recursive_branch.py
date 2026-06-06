"""tests/core/test_recursive_branch.py — P4b 递归装配 TDD。

自带 helper（受控 ZS/ZSLX/线段，绕开笔划分浮点敏感）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS, ZSLX
from chanlun.core import recursive_branch


def _seg(index, _type, start_val, end_val) -> XD:
    def _fx(kidx, val, ftype):
        k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=val)
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _make_zs(core_segs, zd, zg) -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _make_zslx(zss, zslx_type, index=99, _type=None) -> ZSLX:
    # 走势类型方向(.type=_type)默认按 zslx_type 推：上涨→up、下跌→down、盘整→up。
    # 旧版恒置 "up"(连"下跌"也 type=up)会掩盖方向交替——原文 line7268 中枢必为
    # 上下上/下上下,喂回 zs_branch 判中枢要读对 .type,故此处须给出真实方向。
    if _type is None:
        _type = {"上涨": "up", "下跌": "down"}.get(zslx_type, "up")
    z = ZSLX(zslx_level=None, start=zss[0].lines[0].start, end=zss[-1].lines[-1].end,
             start_line=zss[0].lines[0], end_line=zss[-1].lines[-1],
             _type=_type, index=index, done=True)
    z.zss = list(zss)
    z.zslx_type = zslx_type
    z.zs_high = max(x.gg for x in zss)
    z.zs_low = min(x.dd for x in zss)
    return z


# ---- _as_units: ZSLX index 重排 ----
def test_as_units_reindexes_and_preserves_original():
    z1 = _make_zslx([_make_zs([_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8)], 5, 8)], "盘整", index=5)
    z2 = _make_zslx([_make_zs([_seg(3, "up", 16, 19), _seg(4, "down", 19, 16), _seg(5, "up", 16, 19)], 16, 19)], "盘整", index=3)
    out = recursive_branch._as_units([z1, z2])
    assert [u.index for u in out] == [0, 1]      # 任意输入 index(5,3) → 拷贝重排为 0,1
    assert z1.index == 5 and z2.index == 3       # 原对象 index 不变(浅拷贝隔离)
    assert out[0].zs_high == z1.zs_high          # 包络沿用


# ---- _mark_upgrades: 9段 / expand 标注 ----
def test_mark_upgrades_nine_segments():
    # 9 段中枢 → 升级标注
    nine = [_seg(i, "up" if i % 2 == 0 else "down", 5, 8) for i in range(9)]
    z = _make_zs(nine, 5, 8)
    assert recursive_branch._mark_upgrades([z]) == [0]


def test_mark_upgrades_expand_pair():
    # 两中枢本体相交(expand) → 后者标升级
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 9, 6), _seg(4, "up", 6, 9), _seg(5, "down", 9, 6)], 6, 9)  # body[6,9]∩[5,8]
    assert recursive_branch._mark_upgrades([z1, z2]) == [1]


def test_mark_upgrades_clean_trend_none():
    # 干净趋势(本体分离)→ 无升级标注
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 19, 16), _seg(4, "up", 16, 19), _seg(5, "down", 19, 16)], 16, 19)
    assert recursive_branch._mark_upgrades([z1, z2]) == []


def test_mark_upgrades_nine_priority_over_expand():
    """一个中枢既 9段又与前驱 expand → 只记一次(走 9段分支)。"""
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    nine = [_seg(i + 3, "up" if i % 2 == 0 else "down", 6, 9) for i in range(9)]  # body[6,9]∩z1[5,8] 且 9段
    z2 = _make_zs(nine, 6, 9)
    assert recursive_branch._mark_upgrades([z1, z2]) == [1]   # z2 记一次(9段优先于 expand)


# ---- RecursiveBranchCalculator.calculate ----

from chanlun.core.cl_interface import Config


def _ld_none(s, e):
    """fake ld_provider：返回零力度，背驰不触发(本测试靠方向反转/结构切走势类型)。"""
    return {"hist": {"up_sum": 0.0, "down_sum": 0.0}, "dif": {"max": 0.0, "min": 0.0}}


def test_calculate_empty_returns_empty():
    assert recursive_branch.RecursiveBranchCalculator().calculate(
        [], _ld_none, Config.ZS_WZGX_ZGD.value) == []


def test_calculate_level0_produces_zhongshu():
    """一段线段序列至少产出 L0 级(中枢 + 走势类型)。"""
    lines = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    res = recursive_branch.RecursiveBranchCalculator().calculate(
        lines, _ld_none, Config.ZS_WZGX_ZGD.value)
    assert len(res) >= 1
    assert res[0].level == 0
    assert len(res[0].zss) >= 1
    assert len(res[0].zslxs) >= 1


def test_calculate_records_pending_when_no_done():
    """某级只有 pending 中枢(无 done 中枢)→ 记录该级 pending 中枢(H2 leave 读法),zslxs 空。

    5 段(进入段 + 4 核心重叠[5,8],右边缘未离开确认)→ zs_branch 产 pending(done_zss 空,
    live=[H1,H2])。calculate 应记录该级的 H2 pending 中枢,展示右边缘正在形成的高级中枢。
    """
    lines = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    # 防腐:显式锁定该序列走的是 pending 分支(zs_branch 判 pending、无 done 中枢)——
    # 否则将来 ZsCalculator 的 pending/done 边界口径变动,此测试可能静默改测别的路径。
    from chanlun.core.zs_branch import ZsBranchCalculator
    probe = ZsBranchCalculator(
        ld_provider=_ld_none, frequency=None,
        wzgx=Config.ZS_WZGX_ZGD.value, min_zs_lines=4).calculate(lines)
    assert probe.done_zss == [] and any(h.node1 == "leave" for h in probe.live), \
        "前提:该序列须无 done 中枢、有 pending H2(leave) —— 锁定测试意图"

    res = recursive_branch.RecursiveBranchCalculator().calculate(
        lines, _ld_none, Config.ZS_WZGX_ZGD.value)
    assert len(res) == 1
    assert res[0].level == 0
    assert len(res[0].zss) >= 1          # pending 中枢被记录
    assert res[0].zslxs == []            # pending 未完成 → 不切走势类型


def test_calculate_two_levels():
    """L0 走势类型→L1 中枢 关键步骤验证（降级方案）。

    注：靠线段序列自然驱动出 ≥3 个独立 done_zss 并切出 ≥3 个 zslxs 极难——
    ZsCalculator 把后续触及区间的段并入当前中枢（延伸/扩张），难以形成独立中枢序列。
    降级策略：直接手工构造 3 个区间重叠的 ZSLX，验证 _as_units → ZsBranchCalculator
    (min_zs_lines=3) 能产出 ≥1 个 L1 中枢，且其构成段均为 ZSLX 实例。
    完整两级端到端验证交 Task5 真实数据 probe。
    """
    from chanlun.core.zs_branch import ZsBranchCalculator

    # 3 个方向交替、包络重叠的 L0 走势类型（手工构造，绕开 ZsCalculator 扩张难题）
    # zslx1(上涨, zs_low=5, zs_high=15) / zslx2(下跌, zs_low=8, zs_high=18) / zslx3(上涨, zs_low=10, zs_high=20)
    # 相邻 zs_high/zs_low 彼此重叠：[5,15]∩[8,18]=[8,15], [8,18]∩[10,20]=[10,18] → ZsBranch 能聚中枢
    z1 = _make_zs([_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8)], 5, 8)
    z2 = _make_zs([_seg(3, "up", 10, 13), _seg(4, "down", 13, 10), _seg(5, "up", 10, 13)], 10, 13)
    z3 = _make_zs([_seg(6, "up", 12, 15), _seg(7, "down", 15, 12), _seg(8, "up", 12, 15)], 12, 15)

    zslx1 = _make_zslx([z1], "上涨", index=0)
    zslx1.zs_low, zslx1.zs_high = 5, 15

    zslx2 = _make_zslx([z2], "下跌", index=1)
    zslx2.zs_low, zslx2.zs_high = 8, 18

    zslx3 = _make_zslx([z3], "上涨", index=2)
    zslx3.zs_low, zslx3.zs_high = 10, 20

    # 第 4 个 ZSLX 作为离开段：使 ZsCalculator 确认前三段已构成中枢(done)
    # zslx4 区间低于 zslx2 的 zs_low=8，冲出区间向下作为离开段
    z4 = _make_zs([_seg(9, "down", 15, 3), _seg(10, "up", 3, 6), _seg(11, "down", 6, 3)], 3, 6)
    zslx4 = _make_zslx([z4], "下跌", index=3)
    zslx4.zs_low, zslx4.zs_high = 1, 7

    units = recursive_branch._as_units([zslx1, zslx2, zslx3, zslx4])
    assert [u.index for u in units] == [0, 1, 2, 3]

    res = ZsBranchCalculator(min_zs_lines=3).calculate(units)
    assert len(res.done_zss) >= 1, "L0 走势类型喂回后应能聚出 L1 中枢"
    assert all(isinstance(seg, ZSLX) for seg in res.done_zss[0].lines), \
        "L1 中枢的构成段应为 ZSLX（L0 走势类型）实例"


def test_calculate_same_direction_units_form_no_zhongshu():
    """原文 line7268:中枢必为『向上+向下+向上』或『向下+向上+向下』方向交替三段重合。
    3 个同向(down)走势类型即便价格区间重叠也不构成中枢——这是 301004 假 L1 中枢
    [49.72,49.91](3 个连续下跌走势类型 [49.72,53.08]/[45.58,50.06]/[43.10,49.91] 价格
    重合处硬挤出)的根因。zs_branch 须强制方向交替校验(require_alternation=True)。"""
    from chanlun.core.zs_branch import ZsBranchCalculator

    # 4 个同向 down、价格区间依次下移但仍重叠的走势类型(模拟 301004 一路下台阶)
    u1 = _make_zslx([_make_zs([_seg(0, "up", 7, 10), _seg(1, "down", 10, 7), _seg(2, "up", 7, 10)], 7, 10)], "下跌", index=0)
    u1.zs_low, u1.zs_high = 7, 10
    u2 = _make_zslx([_make_zs([_seg(3, "up", 6, 9), _seg(4, "down", 9, 6), _seg(5, "up", 6, 9)], 6, 9)], "下跌", index=1)
    u2.zs_low, u2.zs_high = 6, 9
    u3 = _make_zslx([_make_zs([_seg(6, "up", 5, 8), _seg(7, "down", 8, 5), _seg(8, "up", 5, 8)], 5, 8)], "下跌", index=2)
    u3.zs_low, u3.zs_high = 5, 8
    u4 = _make_zslx([_make_zs([_seg(9, "up", 1, 4), _seg(10, "down", 4, 1), _seg(11, "up", 1, 4)], 1, 4)], "下跌", index=3)
    u4.zs_low, u4.zs_high = 1, 4
    # u1∩u2∩u3 = [max(7,6,5),min(10,9,8)] = [7,8] 非退化(若不校验方向就会挤出假中枢);
    # 全 down 不交替 → 原文判定无中枢。
    assert all(u.type == "down" for u in (u1, u2, u3, u4))

    units = recursive_branch._as_units([u1, u2, u3, u4])
    res = ZsBranchCalculator(min_zs_lines=3).calculate(units)
    assert res.done_zss == []          # 同向不交替 → 无已完成中枢(原文 line7268)
    assert res.live == []              # 也无右边缘 pending 中枢(无任何交替三段)


def test_calculate_reaches_level1_end_to_end():
    """端到端 线段→L0(≥3走势类型)→_as_units 喂回→L1:覆盖 recursive while 循环的
    L≥1 升级分支(units=_as_units(zslxs);level+=1 后再跑 ZsBranchCalculator)。该分支
    被手工构造的 test_calculate_two_levels 绕过(它直接调 ZsBranchCalculator),此前仅
    真实数据 probe 兜底——本测试补上自动化覆盖(评审 What's Missing)。

    构造:6 个中枢,级别 [20,23]/[40,43]/[20,23]/[2,5]/[20,23]/[40,43],关系
    up,down,down,up,up → zslx_branch 切出 **3 个方向交替**的走势类型
    [上涨[z1,z2], 下跌[z3,z4], 上涨[z5,z6]](走势3 用双中枢上涨趋势,方向稳为 up、
    不靠单中枢盘整净值)。3 个 up/down/up 走势类型本体重叠 [20,23] → _as_units 喂回
    后,L1 zs_branch 在【方向交替校验(require_alternation=True)】下聚出 L1 中枢。

    注:走势类型须**真正方向交替**才成 L1 中枢(原文 line7268:中枢=上下上/下上下)。
    旧版构造产出『上涨,上涨,盘整』连续同向走势类型,曾靠关闭交替校验的假中枢才递归到
    L1;交替校验修复后改用本交替构造。
    """
    lines = [
        _seg(0, "up", 19, 23), _seg(1, "down", 23, 20), _seg(2, "up", 20, 23), _seg(3, "down", 23, 20),
        _seg(4, "up", 20, 42),                                                   # 强上行→中枢2
        _seg(5, "down", 42, 40), _seg(6, "up", 40, 43), _seg(7, "down", 43, 40), _seg(8, "up", 40, 43),
        _seg(9, "down", 40, 19),                                                 # 强下行(反转)→中枢3
        _seg(10, "up", 19, 23), _seg(11, "down", 23, 20), _seg(12, "up", 20, 23), _seg(13, "down", 23, 20),
        _seg(14, "up", 20, 23),                                                  # 中枢3 多一段,结束于 up
        _seg(15, "down", 23, 2),                                                 # 强下行(反转)→中枢4
        _seg(16, "up", 2, 5), _seg(17, "down", 5, 2), _seg(18, "up", 2, 5), _seg(19, "down", 5, 2),
        _seg(20, "up", 2, 23),                                                   # 强上行(反转)→中枢5
        _seg(21, "down", 23, 20), _seg(22, "up", 20, 23), _seg(23, "down", 23, 20),
        _seg(24, "up", 20, 42),                                                  # 强上行→中枢6
        _seg(25, "down", 42, 40), _seg(26, "up", 40, 43), _seg(27, "down", 43, 40), _seg(28, "up", 40, 43),
        _seg(29, "down", 40, 36),                                                # 离开确认中枢6
    ]
    res = recursive_branch.RecursiveBranchCalculator().calculate(
        lines, _ld_none, Config.ZS_WZGX_ZGD.value)
    assert len(res) >= 2, "应递归到 L1(L0 切出≥3方向交替走势类型→_as_units 喂回升级)"
    assert res[0].level == 0 and len(res[0].zslxs) >= 3   # L0 喂回前提:≥3 走势类型
    # 喂回的 3 走势类型须方向交替(up/down/up),否则交替校验下聚不出 L1 中枢
    assert [zx.type for zx in res[0].zslxs[:3]] == ["up", "down", "up"]
    assert res[1].level == 1                              # 升级分支(level+=1)被执行
    assert len(res[1].zss) >= 1                           # L1 中枢产出(done 或 pending)


def test_calculate_ld_provider_for_level():
    """换周期 MACD:有 factory → 各级用 factory(level) 算背驰、不用单一 ld_provider;无则退化。"""
    seen_levels = []
    single_called = []

    def factory(level):
        seen_levels.append(level)
        return _ld_none

    def single(s, e):
        single_called.append(1)
        return _ld_none(s, e)

    lines = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    recursive_branch.RecursiveBranchCalculator().calculate(
        lines, single, Config.ZS_WZGX_ZGD.value, ld_provider_for_level=factory)
    assert seen_levels and seen_levels[0] == 0            # factory(0) 被调(各级用对应 provider)
    assert not single_called                             # 有 factory → 单一 ld_provider 未被使用
