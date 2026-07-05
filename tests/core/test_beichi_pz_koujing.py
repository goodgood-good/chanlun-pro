"""盘整背驰口径(审计 F4):比较对象 = 中枢进入段 vs 离开段。

原 beichi_pz 用「中枢内(除末段)最近同向段」——中枢延伸多段后比较对象漂移为延伸段,
非盘整背驰本义(原文/文章口径:围绕中枢的进入段与离开段力度比较)。旧语义(中枢震荡
内部力度比较)拆到 beichi_zs_oscillation, 供 legacy 2 类经验法条件 B 继续使用。
"""
from chanlun.core.beichi_calculator import beichi_pz, beichi_zs_oscillation


class _K:
    def __init__(self, k_index):
        self.k_index = k_index


class _FX:
    def __init__(self, k_index, val):
        self.k = _K(k_index)
        self.val = val


class _Line:
    def __init__(self, type_, s_ki, s_val, e_ki, e_val, high, low):
        self.type = type_
        self._type = type_
        self.start = _FX(s_ki, s_val)
        self.end = _FX(e_ki, e_val)
        self.high = high
        self.low = low


class _ZS:
    def __init__(self, start, lines):
        self.start = start
        self.lines = lines
        self.zd = 8.0
        self.zg = 10.0


def _ld(strength):
    return {"hist": {"up_sum": strength, "down_sum": strength,
                     "max": strength, "min": -strength},
            "dif": {"max": strength, "min": -strength}}


def _fixture():
    """进入段 a(强 10) + 核心[u1,d1,m(弱 2),d2] + 离开段 c(中 5, 创新高)。

    新口径: c vs a → 衰竭 → 背驰 True;
    旧口径: c vs m(最近同向) → 5>2 不衰竭 → False。
    """
    a = _Line("up", 0, 5.0, 10, 10.0, high=10.0, low=5.0)
    u1 = _Line("up", 10, 8.0, 20, 10.0, high=10.5, low=8.0)
    d1 = _Line("down", 20, 10.0, 30, 8.0, high=10.0, low=8.0)
    m = _Line("up", 30, 8.0, 40, 11.0, high=11.0, low=8.0)
    d2 = _Line("down", 40, 11.0, 50, 8.5, high=11.0, low=8.5)
    c = _Line("up", 50, 8.5, 60, 12.0, high=12.0, low=8.5)
    strength = {id(a.start): 10.0, id(u1.start): 3.0, id(d1.start): 3.0,
                id(m.start): 2.0, id(d2.start): 3.0, id(c.start): 5.0}
    ld_provider = lambda s, e: _ld(strength[id(s)])
    zs = _ZS(start=a, lines=[u1, d1, m, d2, c])
    return zs, a, m, c, ld_provider


def test_pz_compares_enter_vs_leave():
    zs, a, m, c, lp = _fixture()
    is_bc, cmp_line = beichi_pz(zs, c, lp)
    assert is_bc is True
    assert cmp_line is a, "盘整背驰比较对象必须是进入段 zs.start"


def test_pz_no_entry_returns_false():
    zs, a, m, c, lp = _fixture()
    zs.start = None                      # 开头中枢无进入段 → 不判
    assert beichi_pz(zs, c, lp) == (False, None)


def test_pz_direction_mismatch_returns_false():
    zs, a, m, c, lp = _fixture()
    zs.start = _Line("down", 0, 12.0, 10, 5.0, high=12.0, low=5.0)  # 进入段异向
    assert beichi_pz(zs, c, lp) == (False, None)


def test_oscillation_keeps_old_semantics():
    zs, a, m, c, lp = _fixture()
    is_bc, cmp_line = beichi_zs_oscillation(zs, c, lp)
    assert cmp_line is m, "中枢震荡口径 = 中枢内(除末段)最近同向段"
    assert is_bc is False                # c(5) 强于 m(2), 不衰竭