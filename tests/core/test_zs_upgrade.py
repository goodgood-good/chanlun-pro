"""tests/core/test_zs_upgrade.py — P9 中枢升级·扩展(子中枢运行交集分组)。

513100 真实 QMT 数据(z1+z2 涉及线段 xd6-15)当 oracle: 用户标注 下xd7-9/上xd10-12/盘xd13-15,
扩展中枢区间 [1.713,1.737](用户多轮确认的正确值, = 子中枢包络重合 [max dd, min gg])。
"""
from chanlun.core.cl_interface import ZS, Config
from chanlun.core.zs_upgrade import (
    is_kuozhan, kuozhan_zhongshu, kuozhan_level_signals, _tongjibie_groups,
    _jiehe_segments,
)


class _L:
    """最小线段桩：type / zs_low / zs_high (+ start/end 占位, 建 ZS 用)。"""

    def __init__(self, t, lo, hi):
        self.type, self.zs_low, self.zs_high = t, lo, hi
        self.start = self.end = None


def _xds_513100():
    """513100 z1+z2 涉及线段 xd6-15(QMT 真实数据,(start_val→end_val) 转 zs_low/zs_high)。"""
    return [
        _L("up", 1.664, 1.737),    # xd6
        _L("down", 1.689, 1.737),  # xd7
        _L("up", 1.689, 1.711),    # xd8
        _L("down", 1.666, 1.711),  # xd9  ← 底
        _L("up", 1.666, 1.693),    # xd10
        _L("down", 1.672, 1.693),  # xd11
        _L("up", 1.672, 1.756),    # xd12 ← 顶
        _L("down", 1.713, 1.756),  # xd13
        _L("up", 1.713, 1.742),    # xd14
        _L("down", 1.729, 1.742),  # xd15
    ]


def _lines_from_pivots(prices):
    """相邻价格 → _L 线段(方向交替, 每段 prices[k]→prices[k+1])。"""
    return [_L("up" if prices[k + 1] > prices[k] else "down",
               min(prices[k], prices[k + 1]), max(prices[k], prices[k + 1]))
            for k in range(len(prices) - 1)]


def _zs(zd, zg, dd, gg, done=True):
    z = ZS(zs_type="xd", start=None)
    z.zd, z.zg, z.dd, z.gg, z.done = zd, zg, dd, gg, done
    return z


def test_is_kuozhan_513100_z1_z2_true():
    """z1[核1.689-1.711 包1.664-1.737] + z2[核1.729-1.742 包1.713-1.756]:
    包络重叠[1.713,1.737] + 核心区分离(1.711<1.729) = 扩展。"""
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z2 = _zs(1.729, 1.742, 1.713, 1.756)
    assert is_kuozhan(z1, z2) is True


def test_is_kuozhan_trend_false():
    """包络分离(后中枢整体在上) = 趋势, 非扩展。"""
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z2 = _zs(2.00, 2.10, 1.90, 2.20)
    assert is_kuozhan(z1, z2) is False


def test_kuozhan_zhongshu_513100_z1_z2():
    """z1(线段 xd6-11)+z2(线段 xd13-15)成组 → 1 个扩展中枢 [1.713,1.737]。"""
    xds = _xds_513100()                  # xd6..xd15
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z1.lines = xds[0:6]                  # xd6-11
    z2 = _zs(1.729, 1.742, 1.713, 1.756)
    z2.lines = xds[7:10]                 # xd13-15
    out = kuozhan_zhongshu([z1, z2], xds)
    assert len(out) == 1
    assert round(out[0].zd, 4) == 1.7130 and round(out[0].zg, 4) == 1.7370
    assert out[0].expanded_with == [z1, z2]
    assert out[0].dd <= out[0].zd and out[0].zg <= out[0].gg    # 核心区 ⊂ 包络


def test_kuozhan_zhongshu_trend_no_group():
    """相邻中枢是趋势(包络分离)→ 不成组、无扩展中枢。"""
    xds = _xds_513100()
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z1.lines = xds[0:6]
    z2 = _zs(2.00, 2.10, 1.90, 2.20)     # 整体在上 = 趋势
    z2.lines = xds[7:10]
    assert kuozhan_zhongshu([z1, z2], xds) == []


def _guard_overlap_case():
    """两组相邻 is_kuozhan 中枢(中间 trend 断开): (z1,z2)低位[9.5,11.5]、(z3,z4)高位[12,13.1]。
    相邻成对扩张 → 各成一个高级别中枢(共 2 个)。"""
    prices = [9, 11.5, 9.5, 11.5, 9.5, 11.5, 9.6, 13, 12, 13.1, 12.2, 13.0, 12.3, 12.95, 12.4]
    xds = _lines_from_pivots(prices)
    z1 = _zs(10.0, 10.5, 9.5, 11.5)
    z1.lines = xds[0:4]
    z2 = _zs(10.8, 11.2, 9.5, 11.5)
    z2.lines = xds[3:7]
    z3 = _zs(12.2, 12.5, 12.0, 13.1)
    z3.lines = xds[6:10]                 # 起点 line6 落在 run1 区域内
    z4 = _zs(12.6, 12.9, 12.0, 13.1)
    z4.lines = xds[9:14]
    return [z1, z2, z3, z4], xds


def test_kuozhan_keeps_overlapping_later_group():
    """两组相邻 is_kuozhan 中枢(中间 trend 断开)→ 各自相邻成对扩张, 共 2 个升级中枢。"""
    zss, xds = _guard_overlap_case()
    assert (is_kuozhan(zss[0], zss[1]) and not is_kuozhan(zss[1], zss[2])
            and is_kuozhan(zss[2], zss[3])), "前提: run1(z1,z2) + trend断开 + run2(z3,z4)"
    out = kuozhan_zhongshu(zss, xds)
    assert len(out) == 2, f"两组扩展都应产出(主修前 guard 误杀 run2 → 仅 1 个), 实得 {len(out)}"


def _subs_with_lines(specs):
    """specs=[(dd,zd,zg,gg),...] → (子中枢列表, xds)。每子中枢给 2 段线段(zs_low/zs_high=dd/gg)
    依次拼成 xds。新核心 kuozhan 区间只取子中枢 dd/gg(与线段走向无关), 此 helper 直接喂子中枢。"""
    xds = []
    subs = []
    for dd, zd, zg, gg in specs:
        a, b = _L("up", dd, gg), _L("down", dd, gg)
        xds.extend([a, b])
        z = _zs(zd, zg, dd, gg)
        z.lines = [a, b]
        subs.append(z)
    return subs, xds


def test_kuozhan_run_splits_at_intersection_collapse():
    """4 个连续两两 is_kuozhan 的下行子中枢, 按**相邻成对扩张**(定理二 line10029)切成
    [z1,z2]+[z3,z4] 两个升级中枢(各取两中枢包络重合 [max dd, min gg])。"""
    subs, xds = _subs_with_lines([(10, 10.5, 11, 12), (9, 9.2, 9.8, 11),
                                  (7, 7.5, 8.5, 9.5), (6, 6.2, 7, 8)])
    assert all(is_kuozhan(subs[k], subs[k + 1]) for k in range(3)), "前提: 4 子中枢两两成一个 run"
    out = kuozhan_zhongshu(subs, xds)
    assert len(out) == 2, f"run 在交集塌缩点切成 2 个中枢, 实得 {len(out)}"
    assert (round(out[0].zd), round(out[0].zg)) == (10, 11), "中枢1=z1,z2 包络重合[max dd,min gg]"
    assert (round(out[1].zd), round(out[1].zg)) == (7, 8), "中枢2=z3,z4 包络重合"


def test_kuozhan_only_last_zhongshu_unfinished():
    """完成度=结束条件(原文 line7260「走势终完美」/ line10031 三类点): **只有序列最后一个**中枢
    未完成(右边缘正在形成、未被后续中枢确认离开);其余(含中间的 2 子中枢组)都已被后续确认 →
    已完成。任意时刻图上只应有一个未完成的高级别中枢(不能因「2 子中枢=进行式」把历史中间的
    2 子中枢组也标成未完成——那些早已成定局)。"""
    # 3 个独立 is_kuozhan run(中间趋势断开), 各 2 子中枢 → 3 个 5min 中枢(中间组也是 2 子中枢)
    subs, xds = _subs_with_lines([(10, 10.5, 11, 12), (10, 11.5, 11.8, 12),    # run1
                                  (20, 20.5, 21, 22), (20, 21.5, 21.8, 22),    # run2(中间, 2 子中枢)
                                  (30, 30.5, 31, 32), (30, 31.5, 31.8, 32)])   # run3(最后)
    assert is_kuozhan(subs[0], subs[1]) and not is_kuozhan(subs[1], subs[2]), "run1 + 趋势断开"
    assert is_kuozhan(subs[2], subs[3]) and not is_kuozhan(subs[3], subs[4]), "run2 + 趋势断开"
    assert is_kuozhan(subs[4], subs[5]), "run3"
    out = kuozhan_zhongshu(subs, xds)
    assert len(out) == 3
    assert out[0].done is True, "首组(非最后) → 已完成"
    assert out[1].done is True, "中间组(2 子中枢、非最后) → 已完成(不是未完成!)"
    assert out[2].done is False, "最后一组 → 未完成(右边缘正在形成)"
    assert sum(1 for z in out if not z.done) == 1, "全图只应有一个未完成中枢"


def test_kuozhan_last_zhongshu_unfinished_even_if_region_before_edge():
    """完成度=结束条件(原文 line7260「走势终完美」): 序列**最后一个**中枢未被后续中枢确认离开
    → **未完成**(done=False), 即便其区域并未延伸到右边缘末线段(只是子中枢恰好止于边缘前)。"""
    # 单组 z1,z2(2 子中枢), 其线段区域止于 xds[6], 右边缘是 xds[9](还有 3 段未入任何中枢)
    prices = [12, 10, 11, 10, 11, 10, 11, 6, 7, 6.5, 7]
    xds = _lines_from_pivots(prices)
    z1 = _zs(10.5, 11.0, 10.0, 12.0)
    z1.lines = xds[0:4]
    z2 = _zs(9.8, 10.2, 9.5, 11.0)
    z2.lines = xds[3:7]
    assert is_kuozhan(z1, z2), "前提: z1,z2 构成扩展"
    out = kuozhan_zhongshu([z1, z2], xds)
    assert len(out) == 1
    assert out[0].lines[-1] is not xds[-1], "前提: 该中枢区域止于右边缘之前"
    assert out[0].done is False, "序列最后一个中枢未被后续确认 → 未完成(尽管区域未到右边缘)"


# ---- 延伸型升级: 单中枢 line_num>=9 → 3+3+3 分 3 组重合(原文 line8157/23045/30290) ----
def test_kuozhan_yanshen_9_segments_3plus3plus3():
    """延伸:单中枢9段 → 按3+3+3分3组,每组取**组内真实** max高/min低(line30290 非连接点),
    升级中枢=三组重合[max(三组低),min(三组高)](line10012/23045)。"""
    g = ([_L("up", 10, 12), _L("down", 11, 13), _L("up", 12, 14)]    # 组1: low10 high14
         + [_L("up", 12, 14), _L("down", 13, 15), _L("up", 14, 16)]  # 组2: low12 high16
         + [_L("up", 11, 13), _L("down", 12, 14), _L("up", 13, 15)])  # 组3: low11 high15
    xds = list(g)
    z = _zs(11, 13, 10, 16)
    z.lines = list(g)                                    # line_num=9 → 延伸
    out = kuozhan_zhongshu([z], xds)
    assert len(out) == 1
    assert out[0].zd == 12 and out[0].zg == 14           # [max(10,12,11), min(14,16,15)]
    assert out[0].expanded_with == [z]


def test_kuozhan_yanshen_priority_over_kuozhang():
    """延伸优先于扩张(用户硬性):9段中枢即便与相邻中枢包络重叠,也按延伸升级、不参与扩张。"""
    g0 = [_L("up", 10, 12), _L("down", 11, 13), _L("up", 12, 14),
          _L("up", 12, 14), _L("down", 13, 15), _L("up", 14, 16),
          _L("up", 11, 13), _L("down", 12, 14), _L("up", 13, 15)]
    extra = [_L("up", 13, 17), _L("down", 14, 17)]
    xds = g0 + extra
    z0 = _zs(11, 13, 10, 16)
    z0.lines = list(g0)                                  # 9段延伸
    z1 = _zs(14, 15, 13, 17)
    z1.lines = list(extra)
    assert is_kuozhan(z0, z1), "前提: z0,z1 包络重叠(无优先级则会扩张)"
    out = kuozhan_zhongshu([z0, z1], xds)
    assert len(out) == 1                                 # 仅 z0 延伸升级;z1 被排除、单独不升级
    assert out[0].zd == 12 and out[0].zg == 14           # z0 的延伸区间,非 z0∩z1 扩张区间


# ---- 扩张升级区间 = [max(DD),min(GG)](原文10018简化公式,Z段=前/后中枢段整段极值) ----
def test_kuozhang_interval_is_envelope_overlap_line10018():
    """扩张升级区间=[max(前DD,后DD), min(前GG,后GG)](line10018「无论哪种情况都可简化为
    [max(a2,c2),min(a1,c1)]」,A/C=前后中枢段;非空性 ⟺ 中心定理二触及条件)。
    桩:中枢内部线段极值交错(首根不含 dd/gg)——曾用顶底切三走势会把本体劈开、区间偏窄/空。"""
    from chanlun.core.zs_upgrade import _kuozhang_upgrade
    a_lines = [_L("up", 21, 25), _L("down", 19, 23)]      # a: dd=19(第2根) gg=25(第1根)
    b_lines = [_L("down", 14, 18), _L("up", 16, 20)]      # b: dd=14 gg=20(第2根)
    xds = a_lines + b_lines
    a = _zs(20, 22, 19, 25)
    a.lines = a_lines
    b = _zs(15, 16, 14, 20)
    b.lines = b_lines
    assert is_kuozhan(a, b), "前提:核心分离(16<20)+包络触及(19<=20)"
    up = _kuozhang_upgrade(a, b, xds)
    assert up is not None
    assert (up.zd, up.zg) == (19, 20)                     # [max(19,14), min(25,20)]


def test_kuozhang_envelope_separated_none():
    """包络分离(后GG<前DD)=趋势延续(中心定理二),不升级。"""
    from chanlun.core.zs_upgrade import _kuozhang_upgrade
    a_lines = [_L("up", 21, 25), _L("down", 20, 23)]
    b_lines = [_L("down", 14, 18), _L("up", 15, 17)]
    a = _zs(21, 22, 20, 25)
    a.lines = a_lines
    b = _zs(15, 16, 14, 18)
    b.lines = b_lines
    assert not is_kuozhan(a, b)
    assert _kuozhang_upgrade(a, b, a_lines + b_lines) is None


# ---- kuozhan_level_signals: 各级(5m/30m)背驰+买卖点 ----
class _LS:
    """线段桩(带 end.val/end.k,供买卖点锚点/三类回试)。"""

    def __init__(self, t, lo, hi, end_val, kidx=0):
        self.type, self.zs_low, self.zs_high = t, lo, hi
        self.start = None
        self.end = type("FX", (), {"val": end_val,
                                   "k": type("K", (), {"date": None, "k_index": kidx})()})()


def _zs_with_lines(zd, zg, lines):
    z = _zs(zd, zg, min(x.zs_low for x in lines), max(x.zs_high for x in lines))
    z.lines = lines
    return z


def _xds_zs_for_3class(leave_type, retest_end):
    """造『进入+本体2段+离开+回试』5段 xds + 中枢[ZD=6,ZG=9],离开方向/回试端点可调。"""
    b0 = _LS("up", 6, 9, 9)
    b1 = _LS("down", 6, 9, 6)
    if leave_type == "up":
        enter = _LS("down", 6, 9, 6)
        leave = _LS("up", 6, 14, 14)
        retest = _LS("down", retest_end, 14, retest_end)
    else:
        enter = _LS("up", 6, 9, 9)
        leave = _LS("down", 2, 9, 2)
        retest = _LS("up", 2, retest_end, retest_end)
    xds = [enter, b0, b1, leave, retest]
    return xds, _zs_with_lines(6, 9, [b0, b1])


def test_kuozhan_level_signals_3buy_geometric():
    """离开向上(冲出 ZG=9)+回试低点 10≥ZG → 3buy(几何,不需背驰,ld=None)。"""
    xds, z = _xds_zs_for_3class("up", 10)
    bsp, bcs = kuozhan_level_signals([z], xds, None, Config.ZS_WZGX_ZGD.value)
    assert [p.bs_type for p in bsp] == ["3buy"]
    assert bsp[0].anchor_fx.val == 10 and bcs == []


def test_kuozhan_level_signals_3sell_geometric():
    """离开向下(跌破 ZD=6)+回试高点 5≤ZD → 3sell。"""
    xds, z = _xds_zs_for_3class("down", 5)
    bsp, _ = kuozhan_level_signals([z], xds, None, Config.ZS_WZGX_ZGD.value)
    assert [p.bs_type for p in bsp] == ["3sell"]


def test_kuozhan_level_signals_retest_breaks_no_3buy():
    """回试低点 7<ZG=9(破核心)→ 不产 3buy。"""
    xds, z = _xds_zs_for_3class("up", 7)
    bsp, _ = kuozhan_level_signals([z], xds, None, Config.ZS_WZGX_ZGD.value)
    assert bsp == []


def test_kuozhan_level_signals_leave_at_right_edge_no_signal():
    """中枢区域止于右边缘(无离开段)→ 不产买卖点/背驰。"""
    b0 = _LS("up", 6, 9, 9)
    b1 = _LS("down", 6, 9, 6)
    xds = [_LS("down", 6, 9, 6), b0, b1]          # b1 是末段、无 xds[b0+1]
    z = _zs_with_lines(6, 9, [b0, b1])
    bsp, bcs = kuozhan_level_signals([z], xds, None, Config.ZS_WZGX_ZGD.value)
    assert bsp == [] and bcs == []


# ---- 同级别分解(30m): 3段走势类型重合=中枢,恰好3段不延伸(line24727/24735) ----
class _W:
    """走势类型桩(同级别分解只读 zs_low/zs_high 价格区间)。"""

    def __init__(self, lo, hi):
        self.zs_low, self.zs_high = lo, hi


def test_tongjibie_3_overlap_one_zs():
    """上下上 3 段价格区间重合 → 1 个 30m 中枢(line24727)。"""
    ws = [_W(10, 20), _W(15, 25), _W(12, 22)]        # 共同重合 [15,20]
    assert _tongjibie_groups(ws) == [(0, 2)]


def test_tongjibie_no_common_overlap_none():
    """3 段无共同重合区间 → 不成中枢。"""
    ws = [_W(10, 20), _W(30, 40), _W(50, 60)]
    assert _tongjibie_groups(ws) == []


def test_tongjibie_6_segments_two_zs_not_extended():
    """6 段都重合 → **2 个**中枢(恰好3段不延伸、允许盘整+盘整,line24728/24735),非1个延伸大中枢。"""
    ws = [_W(10, 20) for _ in range(6)]
    assert _tongjibie_groups(ws) == [(0, 2), (3, 5)]


def test_tongjibie_advance_one_when_no_zs():
    """前3段不重合则前移1段试下一组(连接走势不吞段)。"""
    ws = [_W(10, 20), _W(30, 40), _W(35, 45), _W(33, 43)]   # [1,2,3] 重合 [35,40]
    assert _tongjibie_groups(ws) == [(1, 3)]


# ---- 同级别分解(原文38/39课):次级别=5分钟走势类型(zslx),结合运算合并同方向→严格交替→三段重合 ----
# 修复(用户指出30m中枢不对):原「交替腿」从原始线段中枢按中心分组=级别错——原文25178次级别单位是
# 5分钟走势类型 Ai(严格交替,奇下偶上,line25179),靠结合运算把走势并成交替段。改为
# ZslxBranchCalculator 出走势类型 → _jiehe_segments 合并相邻同方向(结合运算)→ 严格交替段 →
# _tongjibie_groups 三段上下上/下上下重合(line24727)。000001 5m:交替腿误给2中枢、原文口径=1。
class _Z:
    """走势类型(ZSLX)桩:_type 方向 / zs_low / zs_high / zss(构成中枢,供 region 定位)。"""

    def __init__(self, t, lo, hi, zss=None):
        self._type, self.zs_low, self.zs_high, self.zss = t, lo, hi, list(zss or [])


def test_jiehe_merge_same_direction():
    """结合运算(原文25179):合并相邻同方向走势类型 → 严格交替段(上下上),区间取并。"""
    zslxs = [_Z("up", 10, 20), _Z("up", 15, 25), _Z("down", 18, 22),
             _Z("up", 19, 30), _Z("up", 20, 35)]
    segs = _jiehe_segments(zslxs)
    assert [s.dir for s in segs] == ["up", "down", "up"]
    assert segs[0].zs_low == 10 and segs[0].zs_high == 25   # 并 up(10,20)+up(15,25)
    assert segs[1].zs_low == 18 and segs[1].zs_high == 22
    assert segs[2].zs_low == 19 and segs[2].zs_high == 35   # 并 up(19,30)+up(20,35)


def test_jiehe_single_direction_one_segment():
    """全程同方向(纯趋势)→ 结合运算并成 1 段 → 不足3段、不成中枢。"""
    segs = _jiehe_segments([_Z("up", 10, 20), _Z("up", 15, 25), _Z("up", 20, 30)])
    assert len(segs) == 1
    assert _tongjibie_groups(segs) == []


def test_jiehe_then_three_overlap_one_zhongshu():
    """结合运算后三段上下上重合 → 1 个 30m 中枢(line24727);区间=三段重合。"""
    segs = _jiehe_segments([_Z("up", 10, 25), _Z("down", 18, 22), _Z("up", 19, 35)])
    assert _tongjibie_groups(segs) == [(0, 2)]
    zd = max(s.zs_low for s in segs)
    zg = min(s.zs_high for s in segs)
    assert zd == 19 and zg == 22                            # [max(10,18,19), min(25,22,35)]


# ---- 段区间口径(原文20课):gn/dn = 次级别走势类型 Zn 的**整段高低点**,非段内中枢包络 ----
class _FXv:
    def __init__(self, val):
        self.val = val


class _ZSe:
    """中枢桩:gg/dd(含瞬间波动的中枢极值)。"""

    def __init__(self, dd, gg):
        self.dd, self.gg = dd, gg


class _Z2:
    """真 ZSLX 形状的走势类型桩:start/end 分型(端点价) + zss(段内中枢) + 包络字段。"""

    def __init__(self, t, start_v, end_v, zss, lo, hi):
        self._type = t
        self.start, self.end = _FXv(start_v), _FXv(end_v)
        self.zss = list(zss)
        self.zs_low, self.zs_high = lo, hi      # 旧包络口径(zslx_branch 喂回字段)


def test_seg_span_uses_segment_extremes_not_zs_envelope():
    """整段高低口径:下段从 30 跌到 12(端点超出其段内中枢[20,24]),
    包络口径三段不重合(max(lo)=20 > min(hi)=18)→饿死;整段口径下段=[12,30]与两上段重合→成中枢。
    原文20课:ZG=min(g1,g2)/ZD=max(d1,d2),gn、dn 是 Zn 的高、低点(整段极值)。"""
    up1 = _Z2("up", 10, 18, [_ZSe(10, 18)], 10, 18)         # 上段 [10,18]
    dn = _Z2("down", 30, 12, [_ZSe(20, 24)], 20, 24)        # 下段端点 30→12,段内中枢[20,24]
    up2 = _Z2("up", 12, 17, [_ZSe(11, 17)], 11, 17)         # 上段 [11,17]
    segs = _jiehe_segments([up1, dn, up2])
    assert [s.dir for s in segs] == ["up", "down", "up"]
    assert segs[1].zs_low == 12 and segs[1].zs_high == 30   # 下段=整段区间,非中枢包络
    assert _tongjibie_groups(segs) == [(0, 2)]              # 三段重合 [max(10,12,11), min(18,30,17)]


def test_seg_span_unfinished_end_none_falls_back():
    """末段未完成(end=None)→ 整段口径用 start+中枢极值,不崩。"""
    z = _Z2("up", 10, 0, [_ZSe(12, 20)], 12, 20)
    z.end = None
    segs = _jiehe_segments([z])
    assert segs[0].zs_low == 10 and segs[0].zs_high == 20


# ---- 同级别分解的段粒度信号(30m买卖点/背驰):离开/回试用次级别走势类型段,非单根线段 ----
class _FXk:
    """带 k.date 的分型桩。"""

    class _K:
        def __init__(self, date):
            self.date = date

    def __init__(self, val, date="2026-01-01"):
        self.val = val
        self.k = _FXk._K(date)


class _ZSzgzd:
    def __init__(self, zd, zg):
        self.zd, self.zg = zd, zg


def _mk_seg(t, lo, hi, end_val, end_date="2026-02-01"):
    from chanlun.core.zs_upgrade import _Seg
    z = _Z2(t, lo if t == "up" else hi, end_val, [], lo, hi)
    z.start = _FXk(z.start.val if hasattr(z.start, "val") else lo)
    z.end = _FXk(end_val, end_date)
    s = _Seg(z)
    s.zs_low, s.zs_high = lo, hi          # 显式段区间(桩)
    return s


def test_tongjibie_signals_third_buy_on_retest_segment():
    """上下上中枢(zg=20)后,回试段(down)终点 22>=zg → 3buy 锚在回试段终点(第20课:
    向上离开后第一次回抽不破 ZG;段粒度=segs[e+1] 整段即回抽)。"""
    from chanlun.core.zs_upgrade import tongjibie_level_signals
    segs = [_mk_seg("up", 10, 18, 18), _mk_seg("down", 14, 18, 14),
            _mk_seg("up", 14, 25, 25), _mk_seg("down", 22, 25, 22, "2026-03-01")]
    zss = [_ZSzgzd(zd=14, zg=18)]
    meta = {"segs": segs, "groups": [(0, 2)]}
    bsp, bcs = tongjibie_level_signals(zss, meta, None, "zs_wzgx_zgd", "5m")
    assert [p.bs_type for p in bsp] == ["3buy"]
    assert bsp[0].anchor_fx.val == 22


def test_tongjibie_signals_third_sell_symmetric():
    """下上下中枢(zd=14)后,回试段(up)终点 12<=zd → 3sell。"""
    from chanlun.core.zs_upgrade import tongjibie_level_signals
    segs = [_mk_seg("down", 14, 25, 14), _mk_seg("up", 14, 18, 18),
            _mk_seg("down", 8, 18, 8), _mk_seg("up", 8, 12, 12)]
    zss = [_ZSzgzd(zd=14, zg=18)]
    meta = {"segs": segs, "groups": [(0, 2)]}
    bsp, bcs = tongjibie_level_signals(zss, meta, None, "zs_wzgx_zgd", "5m")
    assert [p.bs_type for p in bsp] == ["3sell"]


def test_tongjibie_signals_retest_breaks_core_no_signal():
    """回试段跌破 ZG(终点 16 < zg=18 但 >zd → 破核心上沿回中枢内)→ 无三类。"""
    from chanlun.core.zs_upgrade import tongjibie_level_signals
    segs = [_mk_seg("up", 10, 18, 18), _mk_seg("down", 14, 18, 14),
            _mk_seg("up", 14, 25, 25), _mk_seg("down", 16, 25, 16)]
    zss = [_ZSzgzd(zd=14, zg=18)]
    meta = {"segs": segs, "groups": [(0, 2)]}
    bsp, _ = tongjibie_level_signals(zss, meta, None, "zs_wzgx_zgd", "5m")
    assert bsp == []
