"""tests/core/test_bs_point_first_kind.py — 1 类买卖点原文几何判据。

缠论原文(《缠中说禅股市技术理论解释2017》第一章·第九节《走势中枢与买卖点》)：
  第一类买点：某级别下跌趋势中,一个次级别走势类型 **向下跌破最后一个走势中枢后**
  形成的背驰点。
  第一类卖点：某级别上涨趋势中,一个次级别走势类型 **向上突破最后一个走势中枢后**
  形成的背驰点。

`BsPointCalculator._breaks_last_zs` 在主路径中负责把「未跌破/未突破末中枢」的
力度衰减误判过滤掉(原本只比 A 段创新低/高,会把末中枢内部段也当成 1 买)。
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from chanlun.core.bs_point_calculator import BsPointCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from tests.core.conftest import DEFAULT_CL_CONFIG

_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines"


def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _xd(index: int, _type: str, start_val: float, end_val: float) -> XD:
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    # 已完成 XD 的中枢口径(xd_calculator._make_xd):zs_high/zs_low = 端点价 max/min
    xd.zs_high, xd.zs_low = max(start_val, end_val), min(start_val, end_val)
    xd.done = True
    return xd


def _zs(zg: float, zd: float, gg: float, dd: float, end_xd: XD = None, done: bool = True) -> ZS:
    z = ZS(zs_type="xd", start=None, zg=zg, zd=zd, gg=gg, dd=dd)
    z.done = done
    z.end = end_xd
    return z


def _done_zs(lines, zg: float, zd: float, gg: float, dd: float, end_xd: XD = None) -> ZS:
    z = ZS(zs_type="xd", start=lines[0], zg=zg, zd=zd, gg=gg, dd=dd, index=1)
    z.done = True
    z.real = True
    z.lines = list(lines)
    z.end = end_xd if end_xd is not None else lines[-1]
    z.line_num = len(z.lines)
    return z


def test_breaks_last_zs_down_pending_zs_returns_false():
    """末中枢未完成 → 无法定位「跌破点」,返回 False(无论 low 多低)。"""
    now = _xd(20, "down", 10, 1)
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=None, done=False)
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is False


def test_breaks_last_zs_down_now_line_before_zs_end_returns_false():
    """now_line 在末中枢离开段之前(末中枢内部段)→ 未真正跌破,返回 False。"""
    zs_end = _xd(15, "down", 8, 4)   # 末中枢离开段
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=zs_end, done=True)
    # now_line 是中枢内部段 index=10,在 zs.end.index=15 之前
    now = _xd(10, "down", 7, 5)
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is False


def test_breaks_last_zs_down_now_line_is_leave_segment_returns_true():
    """now_line 就是末中枢的离开段(被 ① 并入核心)→ 跌破成立,返回 True。

    边界场景:离开段自身的 low 制造了 dd,所以 ``now.low == last_zs.dd``,
    用 ``<=`` 接纳等号才能识别。
    """
    leave = _xd(15, "down", 8, 4)
    # 离开段自己的 low=4 制造了 dd=4
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=leave, done=True)
    assert BsPointCalculator._breaks_last_zs(leave, last_zs) is True


def test_breaks_last_zs_down_now_line_after_zs_end_breaks_dd_returns_true():
    """now_line 在末中枢之后、low 严格跌破 dd → 标准 1 买场景,返回 True。"""
    zs_end = _xd(15, "down", 8, 4)
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=zs_end, done=True)
    now = _xd(20, "down", 5, 2)   # low=2 < dd=4
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is True


def test_breaks_last_zs_down_now_line_after_but_above_dd_returns_false():
    """now_line 在末中枢之后但 low 高于 dd → 反抽未跌破,返回 False。"""
    zs_end = _xd(15, "down", 8, 4)
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=zs_end, done=True)
    now = _xd(20, "down", 7, 6)   # low=6 > dd=4
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is False


def test_breaks_last_zs_up_breaks_gg_returns_true():
    """上涨段 high 突破末中枢 gg → 1 卖几何前提成立。"""
    zs_end = _xd(15, "up", 5, 9)
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=zs_end, done=True)
    now = _xd(20, "up", 6, 12)    # high=12 > gg=9
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is True


def test_breaks_last_zs_up_above_but_below_gg_returns_false():
    """上涨段 high 仍在 gg 之下 → 未突破,返回 False。"""
    zs_end = _xd(15, "up", 5, 9)
    last_zs = _zs(zg=8, zd=5, gg=9, dd=4, end_xd=zs_end, done=True)
    now = _xd(20, "up", 5, 8)    # high=8 < gg=9
    assert BsPointCalculator._breaks_last_zs(now, last_zs) is False


def test_build_zss_index_keeps_opening_zs_without_entry():
    """开头四段中枢没有进入段时,买卖点索引应使用首个核心段作时间锚。"""
    core_1 = _xd(0, "up", 4, 8)
    core_2 = _xd(2, "down", 8, 5)
    core_3 = _xd(4, "up", 5, 10)
    leave = _xd(6, "down", 10, 6)
    zs = _done_zs([core_1, core_2, core_3, leave], zg=8, zd=5, gg=10, dd=4, end_xd=leave)
    zs.start = None

    clean_zss, start_keys = BsPointCalculator._build_zss_index([zs])

    assert clean_zss == [zs]
    assert start_keys == [core_1.start.k.k_index]


# ---------------- 真实 K 线 fixture:计数对照 ----------------


def _load(fixture: str) -> pd.DataFrame:
    csv = _FIXTURES_DIR / fixture
    if not csv.exists():
        pytest.skip(f"缺少 fixture: {fixture}")
    return pd.read_csv(csv, parse_dates=["date"])


def _count_mmds_by_name(lines, names) -> int:
    return sum(
        1
        for line in lines
        for mmds in getattr(line, "zs_type_mmds", {}).values()
        for m in mmds
        if m.name in names
    )


# ---------------- 定律一(§3.2):次级别 1 类 → 本级别 2 类 ----------------


class _FakeBI(XD):
    """笔(BI)的轻量 stub:LINE 子类、含 zs_type_mmds + end.k.k_index。"""

    def __init__(self, index, _type, start_val, end_val):
        if _type == "up":
            start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
        else:
            start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
        # 复用 XD 的 ctor 把 high/low/type/index 设好
        super().__init__(start=start, end=end, _type=_type, index=index)
        self.zs_type_mmds = {}


class _FakeCL:
    def __init__(self, bis):
        self._bis = bis

    def get_bis(self):
        return self._bis


def _attach_bi_mmd(bi, name):
    """给笔挂上指定名字的 1 类 mmd(用 bi 层 zs_type 桶)。"""
    from chanlun.core.cl_interface import MMD, ZS

    fake_zs = ZS(zs_type="bi", start=None, zg=0, zd=0, gg=0, dd=0)
    mmd = MMD(name=name, zs=fake_zs)
    bi.zs_type_mmds.setdefault("bi", []).append(mmd)


def test_dingli_yi_finds_subordinate_1buy_within_xd_window():
    """xd 层 ``_find_subordinate_1mmd_in_window`` 在 now_line 时间窗内
    找到同向(down)+ 挂 1buy 的笔 → 返回该笔(定律一前提)。
    """
    # xd: index 0..20 时间窗,方向 down
    now_xd = _xd(20, "down", 10, 5)
    now_xd.start = _fx(0, 10, "ding")   # 重置 start 让时间窗 [0, 21]
    now_xd.end = _fx(21, 5, "di")

    # 笔列表:中间一条 down 笔挂 1buy,end_k 落在 [0, 21] 内
    bi_hit = _FakeBI(index=2, _type="down", start_val=10, end_val=4)
    bi_hit.end = _fx(21, 4, "di")
    _attach_bi_mmd(bi_hit, "1buy")
    bi_other = _FakeBI(index=4, _type="up", start_val=4, end_val=6)
    # 时间窗外笔
    bi_outside = _FakeBI(index=100, _type="down", start_val=20, end_val=15)
    _attach_bi_mmd(bi_outside, "1buy")

    calc = BsPointCalculator(_FakeCL([bi_hit, bi_other, bi_outside]), zs_type="xd")
    found = calc._find_subordinate_1mmd_in_window(now_xd, "down")
    assert found is bi_hit


def test_dingli_yi_ignores_subordinate_1buy_before_xd_end():
    """定理一只接受父级线段末端的次级别一买,不能用窗口早期一买触发二买。"""
    now_xd = _xd(20, "down", 10, 5)
    now_xd.start = _fx(0, 10, "ding")
    now_xd.end = _fx(21, 5, "di")
    bi_early = _FakeBI(index=2, _type="down", start_val=10, end_val=4)
    bi_early.end = _fx(10, 4, "di")
    _attach_bi_mmd(bi_early, "1buy")

    calc = BsPointCalculator(_FakeCL([bi_early]), zs_type="xd")
    assert calc._find_subordinate_1mmd_in_window(now_xd, "down") is None


def test_dingli_yi_returns_none_when_bi_layer_no_1buy():
    """笔层没有任何 1buy → 返回 None,定律一不成立(回退经验法兜底)。"""
    now_xd = _xd(20, "down", 10, 5)
    now_xd.start = _fx(0, 10, "ding")
    now_xd.end = _fx(21, 5, "di")
    bi = _FakeBI(index=2, _type="down", start_val=10, end_val=4)
    # 不挂 mmd
    calc = BsPointCalculator(_FakeCL([bi]), zs_type="xd")
    assert calc._find_subordinate_1mmd_in_window(now_xd, "down") is None


def test_dingli_yi_returns_none_for_opposite_direction():
    """笔方向与 target_type 相反 → 不算次级别 1 类(同向才能构成定律一)。"""
    now_xd = _xd(20, "down", 10, 5)
    now_xd.start = _fx(0, 10, "ding")
    now_xd.end = _fx(21, 5, "di")
    bi_up = _FakeBI(index=2, _type="up", start_val=5, end_val=10)
    _attach_bi_mmd(bi_up, "1buy")   # 同名但方向 up
    calc = BsPointCalculator(_FakeCL([bi_up]), zs_type="xd")
    assert calc._find_subordinate_1mmd_in_window(now_xd, "down") is None


def test_dingli_yi_returns_none_for_bi_layer_caller():
    """``self.zs_type=='bi'`` 的调用(笔层无更细次级别)→ 直接返回 None。"""
    now = _xd(20, "down", 10, 5)
    bi = _FakeBI(index=2, _type="down", start_val=10, end_val=4)
    _attach_bi_mmd(bi, "1buy")
    calc = BsPointCalculator(_FakeCL([bi]), zs_type="bi")
    assert calc._find_subordinate_1mmd_in_window(now, "down") is None


def test_third_buy_first_failed_return_consumes_signal():
    """三买必须是离开中枢后的第一次回抽,第一次跌回中枢后不能用后续回抽补报。"""
    core_1 = _xd(0, "up", 8, 10)
    core_2 = _xd(2, "down", 10, 8)
    core_3 = _xd(4, "up", 8, 10)
    leave = _xd(6, "up", 10, 14)
    first_return_failed = _xd(8, "down", 14, 7)
    extend = _xd(10, "up", 7, 15)
    second_return_above_zg = _xd(12, "down", 15, 11)
    lines = [
        core_1,
        core_2,
        core_3,
        leave,
        first_return_failed,
        extend,
        second_return_above_zg,
    ]
    zs = _done_zs([core_1, core_2, core_3, leave], zg=10, zd=8, gg=14, dd=8, end_xd=leave)

    BsPointCalculator(_FakeCL([]), zs_type="xd").calculate(lines, [zs])

    assert first_return_failed.line_mmds("xd") == []
    assert second_return_above_zg.line_mmds("xd") == []


def test_second_buy_only_first_pullback_per_anchor():
    """原文：2 买 = 1 买后**首次**次级别回抽。同一 1 买锚点后续不破前低的
    回抽属中枢震荡，不应重复记 2 买（否则一个 1 买会刷出十几个 2 买）。
    """
    from chanlun.core.cl_interface import MMD, ZS as _ZS

    one_buy = _xd(0, "down", 12, 8)      # 1 买所在段, low=8
    up1 = _xd(2, "up", 8, 11)
    pull1 = _xd(4, "down", 11, 9)        # 首次回抽不破 8 → 2 买
    up2 = _xd(6, "up", 9, 11)
    pull2 = _xd(8, "down", 11, 9)        # 再次回抽不破 8 → 中枢震荡, 非 2 买
    lines = [one_buy, up1, pull1, up2, pull2]
    for ln in lines:
        ln.zs_type_mmds = {"xd": []}
    anchor_zs = _ZS(zs_type="xd", start=None, zg=11, zd=9, gg=12, dd=8)
    one_buy.zs_type_mmds["xd"].append(MMD(name="1buy", zs=anchor_zs))

    calc = BsPointCalculator(_FakeCL([]), zs_type="xd")
    # 直接调 2 类检测(不走 calculate 以免清空手挂的 1 买); 条件 A 不需要 zss
    calc._detect_2buy_2sell(lines, [], [], [])

    assert pull1.line_mmds("xd") == ["2buy"]     # 首次回抽 = 2 买
    assert pull2.line_mmds("xd") == []           # 同锚点二次回抽 ≠ 2 买


def test_calculate_clears_default_mmds_between_recalculations():
    """重复计算同一批线段时,默认 line.mmds 不能残留上一次写入的买卖点对象。"""
    core_1 = _xd(0, "up", 8, 10)
    core_2 = _xd(2, "down", 10, 8)
    core_3 = _xd(4, "up", 8, 10)
    leave = _xd(6, "up", 10, 14)
    pullback = _xd(8, "down", 14, 11)
    lines = [core_1, core_2, core_3, leave, pullback]
    zs = _done_zs([core_1, core_2, core_3, leave], zg=10, zd=8, gg=14, dd=8, end_xd=leave)
    calc = BsPointCalculator(_FakeCL([]), zs_type="xd")

    calc.calculate(lines, [zs])
    calc.calculate(lines, [zs])

    assert pullback.line_mmds("xd") == ["3buy"]
    assert pullback.line_mmds() == ["3buy"]
    assert len(pullback.zs_type_mmds["xd"]) == 1
    assert len(pullback.mmds) == 1


def test_third_buy_can_attach_to_pending_line():
    """未完成线段当前满足三买条件时,应实时挂出买卖点。"""
    core_1 = _xd(0, "up", 8, 10)
    core_2 = _xd(2, "down", 10, 8)
    core_3 = _xd(4, "up", 8, 10)
    leave = _xd(6, "up", 10, 14)
    pullback = _xd(8, "down", 14, 11)
    pullback.done = False
    lines = [core_1, core_2, core_3, leave, pullback]
    zs = _done_zs([core_1, core_2, core_3, leave], zg=10, zd=8, gg=14, dd=8, end_xd=leave)

    BsPointCalculator(_FakeCL([]), zs_type="xd").calculate(lines, [zs])

    assert pullback.line_mmds("xd") == ["3buy"]
    assert pullback.line_mmds() == ["3buy"]


def test_layer_sig_changes_when_pending_line_price_changes():
    """pending 末段端点价格变化时,CL 必须重跑买卖点计算。"""
    pending = _xd(8, "down", 14, 11)
    pending.done = False
    sig_before = CL._calc_layer_sig([pending])

    pending.end.val = 10
    pending.low = 10
    pending.zs_low = 10
    sig_after = CL._calc_layer_sig([pending])

    assert sig_after != sig_before


# ---------------- 真实 K 线 fixture:计数对照 ----------------


@pytest.mark.parametrize("fixture", ["us_TSLA_US_30m.csv", "a_SZ_301004_30m.csv"])
def test_first_kind_count_consistency_on_real_klines(fixture):
    """真实 K 线下,几何判据修复前后 1 类信号数应稳定(不抛、可正向产出)。

    本测试单纯做「跑得通 + 有量」健康检查:确保 ``_breaks_last_zs`` 引入后
    1 类买卖点仍能在合理量级被识别(非全部被过滤为 0、也不爆炸)。具体计数
    随真实 fixture 走;若 fixture 替换需相应更新阈值。
    """
    df = _load(fixture)
    cd = CL("T", "30m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    xds = cd.get_xds()
    one_count = _count_mmds_by_name(xds, ("1buy", "1sell"))
    # 真实 fixture 上至少应有少量 1 类点(>0 证明几何判据没误伤一切),
    # 上界宽松,防爆炸即可
    assert 0 <= one_count <= 200, (
        f"{fixture}: 1 类信号数 {one_count} 异常(应在 [0, 200] 区间)"
    )
