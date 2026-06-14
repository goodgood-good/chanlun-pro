"""§5.2 买卖点 测试。三买/三卖=确定性；一买=背驰(provisional)；課21:72 合法性。"""

from chan.beichi import LiduSeg, make_beichi
from chan.config import DEFAULT
from chan.maimaidian import check_legality, find_third_mmd, first_mmd_from_beichi
from chan.types import ChanStatus, Direction, Duan, MaiMaiDian, MMDType, Zhongshu

U, D = Direction.UP, Direction.DOWN


def _dn(direction, lo, hi, cb):
    sv, ev = (lo, hi) if direction is U else (hi, lo)
    return Duan(direction=direction, start_bi=0, end_bi=0, start_value=sv, end_value=ev,
                high=hi, low=lo, confirm_bar=cb, status=ChanStatus.CONFIRMED)


def _zs(zd=11, zg=14):
    return Zhongshu(direction=U, start_seg=0, end_seg=2, zg=zg, zd=zd, gg=15, dd=10,
                    confirm_bar=3, status=ChanStatus.CONFIRMED, closed=True)


# ── 三买：向上离开 ZG + 第一次回试不破 ZG（取 low）→ 确定性 ──
def test_third_buy():
    duans = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),  # 中枢 [11,14]
             _dn(U, 15, 18, 4),    # 离开段：整体上破 ZG（low15>14）
             _dn(D, 15, 17, 5)]    # 回试：low15≥14 不破
    mmds = find_third_mmd([_zs()], duans, DEFAULT)
    assert len(mmds) == 1
    m = mmds[0]
    assert m.mmd_type is MMDType.BUY3
    assert m.price == 15 and m.confirm_bar == 5
    assert m.status is ChanStatus.CONFIRMED          # 确定性（中枢定）


# ── 回试破 ZG → 非三买 ──
def test_retest_breaks_zg_no_third_buy():
    duans = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),
             _dn(U, 15, 18, 4),
             _dn(D, 12, 17, 5)]    # 回试 low12<14 破 ZG
    assert find_third_mmd([_zs()], duans, DEFAULT) == []


# ── 离开段未整体上破 ZG → 非三买 ──
def test_no_upward_leave_no_third_buy():
    duans = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),
             _dn(U, 13, 18, 4),    # 离开段 low13<14（未整体上破）
             _dn(D, 15, 17, 5)]
    assert find_third_mmd([_zs()], duans, DEFAULT) == []


# ── 三卖（对称）──
def test_third_sell():
    z = Zhongshu(direction=D, start_seg=0, end_seg=2, zg=14, zd=11, gg=15, dd=10,
                 confirm_bar=3, status=ChanStatus.CONFIRMED, closed=True)
    duans = [_dn(D, 11, 15, 1), _dn(U, 11, 14, 2), _dn(D, 11, 14, 3),
             _dn(D, 7, 10, 4),     # 离开段：整体下破 ZD（high10<11）
             _dn(U, 8, 11, 5)]     # 回试：high11≤11 不破 ZD
    mmds = find_third_mmd([z], duans, DEFAULT)
    assert len(mmds) == 1 and mmds[0].mmd_type is MMDType.SELL3
    assert mmds[0].price == 11


# ── 三买情形B（常见）：突破段 low 在中枢内被吸收(延伸)，回试段=离开段本身 ──
def test_third_buy_case_b_absorbed_breakout():
    # 中枢 [12,19] 含被吸收的突破段(seg3, low13在内/high25在上)；回试 seg4(down)整体不破 ZG
    duans = [_dn(U, 10, 20, 1), _dn(D, 12, 20, 2), _dn(U, 12, 19, 3),
             _dn(U, 13, 25, 4),    # 突破段：low13≤ZG 被中枢吸收(延伸) → end_seg=3
             _dn(D, 20, 25, 5)]    # 回试段：整体在 ZG 上方(low20≥19) = 不破 → 三买
    z = Zhongshu(direction=U, start_seg=0, end_seg=3, zg=19, zd=12, gg=25, dd=10,
                 confirm_bar=4, status=ChanStatus.CONFIRMED, closed=True)
    mmds = find_third_mmd([z], duans, DEFAULT)
    assert len(mmds) == 1 and mmds[0].mmd_type is MMDType.BUY3 and mmds[0].price == 20


# ── 回试段未走出 → 不发（待定）──
def test_retest_not_out_yet():
    duans = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(U, 15, 18, 4)]
    assert find_third_mmd([_zs()], duans, DEFAULT) == []


# ── 一买 / 一卖 由背驰生成（provisional）──
def test_first_buy_from_bottom_beichi():
    bc = make_beichi(LiduSeg(0, 5, 20, 12, D), LiduSeg(10, 15, 15, 10, D), "趋势")
    m = first_mmd_from_beichi(bc, price=10.0)
    assert m.mmd_type is MMDType.BUY1 and m.price == 10.0
    assert m.status is ChanStatus.PROVISIONAL and m.confirm_bar == 15

    bc_top = make_beichi(LiduSeg(0, 5, 12, 5, U), LiduSeg(10, 15, 15, 8, U), "趋势")
    assert first_mmd_from_beichi(bc_top, price=15.0).mmd_type is MMDType.SELL1


# ── 課21:72 合法性：3买/3卖不可作开头 ──
def test_legality():
    buy3 = MaiMaiDian(MMDType.BUY3, 15, 5, ChanStatus.CONFIRMED)
    buy1 = MaiMaiDian(MMDType.BUY1, 10, 5, ChanStatus.PROVISIONAL)
    assert check_legality([buy3, buy1]) is not None       # 3买开头违规
    assert check_legality([buy1, buy3]) is None           # 1买开头合法
    assert check_legality([]) is None
