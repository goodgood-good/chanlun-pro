"""§5.2 全流程买卖点采集 测试。三买（各级别确定性）+ 一买（level0 趋势背驰）。"""

from chan.config import DEFAULT
from chan.recursive import LevelResult
from chan.signals import (
    collect_first_points_level0,
    collect_signals,
    collect_third_points,
)
from chan.types import (
    Bar,
    ChanStatus,
    Direction,
    Duan,
    MMDType,
    Zhongshu,
    ZoushiKind,
    ZoushiType,
)

U, D = Direction.UP, Direction.DOWN


def _dn(direction, lo, hi, cb, sb=-1, eb=-1):
    sv, ev = (lo, hi) if direction is U else (hi, lo)
    return Duan(direction=direction, start_bi=0, end_bi=0, start_value=sv, end_value=ev,
                high=hi, low=lo, confirm_bar=cb, status=ChanStatus.CONFIRMED,
                start_bar=sb, end_bar=eb)


# ── 三买采集（确定性，经 LevelResult）──
def test_collect_third_points():
    duans = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),   # 中枢 [11,14]
             _dn(U, 15, 18, 4),    # 离开：上破 ZG
             _dn(D, 15, 17, 5)]    # 回试：不破 ZG
    zs = Zhongshu(direction=U, start_seg=0, end_seg=2, zg=14, zd=11, gg=15, dd=10,
                  confirm_bar=3, status=ChanStatus.CONFIRMED, closed=True, level=0)
    lr = LevelResult(level=0, units=duans, zhongshus=[zs], zoushi=[])
    mmds = collect_third_points([lr], DEFAULT)
    assert len(mmds) == 1 and mmds[0].mmd_type is MMDType.BUY3 and mmds[0].price == 15


# ── 一买采集（level0 下跌趋势底背驰，provisional）──
def _beichi_bars():
    closes = []
    closes += [30 - 2.0 * i for i in range(9)]          # 0-8 陡跌（a 段，力度大）
    closes += [14 + 0.6 * i for i in range(1, 11)]       # 9-18 反抽
    closes += [20 - 0.7 * i for i in range(1, 16)]       # 19-33 缓跌创新低（c 段，力度衰竭）
    return [Bar(idx=i, dt=None, o=c, h=c, l=c, c=c) for i, c in enumerate(closes)]


def test_collect_first_points_bottom_beichi():
    bars = _beichi_bars()
    # 下跌趋势：下-上-下，同向腿 a=duans[0](低14)、c=duans[2](新低9.5)
    duans = [_dn(D, 14, 30, 8, sb=0, eb=8),
             _dn(U, 14, 20, 18, sb=8, eb=18),
             _dn(D, 9.5, 20, 33, sb=19, eb=33)]
    zsl = ZoushiType(kind=ZoushiKind.DOWN, direction=D, start_unit=0, end_unit=2,
                     start_value=30, end_value=9.5, high=30, low=9.5, confirm_bar=33,
                     status=ChanStatus.CONFIRMED, level=0)
    mmds = collect_first_points_level0([zsl], duans, bars, DEFAULT)
    assert len(mmds) == 1
    assert mmds[0].mmd_type is MMDType.BUY1
    assert mmds[0].price == 9.5 and mmds[0].status is ChanStatus.PROVISIONAL


# ── 趋势力度未衰竭 → 不发一买 ──
def test_no_first_point_when_not_beichi():
    # c 段不创新低（low 15 > a 段 low 14）→ 一票否决
    bars = _beichi_bars()
    duans = [_dn(D, 14, 30, 8, sb=0, eb=8),
             _dn(U, 15, 20, 18, sb=8, eb=18),
             _dn(D, 15, 20, 33, sb=19, eb=33)]   # c.low=15>14 没创新低
    zsl = ZoushiType(kind=ZoushiKind.DOWN, direction=D, start_unit=0, end_unit=2,
                     start_value=30, end_value=15, high=30, low=15, confirm_bar=33,
                     status=ChanStatus.CONFIRMED, level=0)
    assert collect_first_points_level0([zsl], duans, bars, DEFAULT) == []


# ── 盘整走势不发一买（仅趋势）──
def test_panzheng_no_first_point():
    bars = _beichi_bars()
    duans = [_dn(D, 14, 30, 8, sb=0, eb=8), _dn(U, 14, 20, 18, sb=8, eb=18),
             _dn(D, 9.5, 20, 33, sb=19, eb=33)]
    zsl = ZoushiType(kind=ZoushiKind.PANZHENG, direction=D, start_unit=0, end_unit=2,
                     start_value=30, end_value=9.5, high=30, low=9.5, confirm_bar=33,
                     status=ChanStatus.CONFIRMED, level=0)
    assert collect_first_points_level0([zsl], duans, bars, DEFAULT) == []


# ── 端到端 smoke：collect_signals 返回有序合法买卖点列表（无崩溃）──
def test_collect_signals_smoke():
    sigs = collect_signals(_beichi_bars(), DEFAULT)
    assert isinstance(sigs, list)
    assert all(isinstance(m.mmd_type, MMDType) for m in sigs)
    # 按 confirm_bar 有序
    assert [m.confirm_bar for m in sigs] == sorted(m.confirm_bar for m in sigs)
