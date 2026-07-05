"""定理二升级预告(O3b-lite):相邻同级别中枢「核心区分离+波动区间重叠」=更大级别中枢正在形成。

原文依据:L020:39 中心定理二「后 ZG<前 ZD 且后 GG≥前 DD(或对称) ⟺ 形成高级别的走势中枢」
+ L043 缠答「2 个 30 分钟中枢如果有区间重合,必然扩展成一个日线级别的中枢…进行式非完成式」。
故按**进行式**语义注入 L_{k+1} 的 live_zss(虚线预告,done=False),不入 done 链——完成确认
仍走自然递归(3 个次级别走势类型重叠)。无未来函数:仅用已完成中枢的当下几何。
"""
from chanlun.core.recursive_branch import _dingli2_upgrade_forming


class _K:
    def __init__(self, idx):
        self.k_index = idx
        self.index = idx
        self.date = None


class _FX:
    def __init__(self, idx, val):
        self.k = _K(idx)
        self.val = val


class _Line:
    def __init__(self, type_, s_i, s_v, e_i, e_v):
        self.type = type_
        self._type = type_
        self.start = _FX(s_i, s_v)
        self.end = _FX(e_i, e_v)
        self.zs_low = min(s_v, e_v)
        self.zs_high = max(s_v, e_v)
        self.done = True


class _ZS:
    def __init__(self, zd, zg, dd, gg, i0):
        self.zs_type = "xd"
        self.level = None
        self.done = True
        self.zd, self.zg, self.dd, self.gg = map(float, (zd, zg, dd, gg))
        self.lines = [_Line("up", i0, zd, i0 + 10, zg),
                      _Line("down", i0 + 10, zg, i0 + 20, zd),
                      _Line("up", i0 + 20, zd, i0 + 30, zg)]
        self.start = self.lines[0]
        self.end = self.lines[-1]


def test_core_separated_wave_overlap_emits_forming():
    a = _ZS(zd=12, zg=18, dd=10, gg=22, i0=100)
    b = _ZS(zd=19, zg=24, dd=15, gg=26, i0=200)   # ZD(19)>ZG(18) 核心分离;DD(15)<GG(22) 波动重叠
    out = _dingli2_upgrade_forming([a, b])
    assert len(out) == 1
    z = out[0]
    assert z.done is False and getattr(z, "_dingli2_upgrade", False) is True
    assert z.zd == 15.0 and z.zg == 22.0          # 核心区候选=波动重叠带 [max(dd),min(gg)]
    assert z.lines[0] is a.lines[0] and z.lines[-1] is b.lines[-1]


def test_trend_pair_no_forming():
    a = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)
    b = _ZS(zd=25, zg=33, dd=22, gg=35, i0=200)   # DD(22)>GG(20) 波动完全分离=趋势
    assert _dingli2_upgrade_forming([a, b]) == []


def test_core_overlap_no_forming():
    a = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)
    b = _ZS(zd=15, zg=21, dd=13, gg=23, i0=200)   # 核心区重叠=延伸族,非定理二升级
    assert _dingli2_upgrade_forming([a, b]) == []

def test_historical_upgrade_pair_not_previewed():
    """升级对若非末对(其后已有新 done 中枢)→ 已成历史,不预告(进行式=当下)。"""
    a = _ZS(zd=12, zg=18, dd=10, gg=22, i0=100)
    b = _ZS(zd=19, zg=24, dd=15, gg=26, i0=200)   # a-b 满足升级
    c = _ZS(zd=40, zg=48, dd=38, gg=50, i0=300)   # 但其后已有趋势离开的新中枢
    out = _dingli2_upgrade_forming([a, b, c])
    assert out == []                              # b-c 是趋势对、a-b 已成历史
