"""§5.1 背驰 测试。创新高/新低一票否决 + 力度衰竭（柱面积 + 黄白线）。"""

from chan.beichi import LiduSeg, compute_lidu, is_beichi, make_beichi
from chan.config import DEFAULT
from chan.types import ChanStatus, Direction

U, D = Direction.UP, Direction.DOWN


def _macd_down(strong_b=False):
    """造一组 hist/dif：a段[0..5]力度大，b段[10..15]创新低但力度衰竭（除非 strong_b）。"""
    hist = [0.0] * 20
    for i in range(0, 6):
        hist[i] = -3.0                       # a 绿柱面积 18
    for i in range(10, 16):
        hist[i] = -5.0 if strong_b else -1.0  # b 绿柱面积 30(强) / 6(衰)
    dif = [(-5.0 if i < 6 else (-2.0 if 10 <= i < 16 else 0.0)) for i in range(20)]
    return hist, dif


# ── 底背驰：后段创新低 + 面积衰竭 + DIF 回收 ──
def test_bottom_beichi():
    hist, dif = _macd_down()
    a = LiduSeg(0, 5, high=20, low=12, direction=D)
    b = LiduSeg(10, 15, high=15, low=10, direction=D)   # low10<12 创新低
    assert is_beichi(a, b, hist, dif, DEFAULT)


# ── 创新低一票否决：没创新低 → 非背驰（连力度都不比）──
def test_no_new_low_vetoes():
    hist, dif = _macd_down()
    a = LiduSeg(0, 5, high=20, low=12, direction=D)
    b = LiduSeg(10, 15, high=15, low=13, direction=D)   # low13>12 没创新低
    assert not is_beichi(a, b, hist, dif, DEFAULT)


# ── 创新低但力度更大 → 非背驰 ──
def test_new_low_but_stronger_not_beichi():
    hist, dif = _macd_down(strong_b=True)
    a = LiduSeg(0, 5, high=20, low=12, direction=D)
    b = LiduSeg(10, 15, high=15, low=10, direction=D)
    assert not is_beichi(a, b, hist, dif, DEFAULT)


# ── 顶背驰（对称）──
def test_top_beichi():
    hist = [0.0] * 20
    for i in range(0, 6):
        hist[i] = 3.0
    for i in range(10, 16):
        hist[i] = 1.0
    dif = [(5.0 if i < 6 else (2.0 if 10 <= i < 16 else 0.0)) for i in range(20)]
    a = LiduSeg(0, 5, high=12, low=5, direction=U)
    b = LiduSeg(10, 15, high=15, low=8, direction=U)    # high15>12 创新高
    assert is_beichi(a, b, hist, dif, DEFAULT)


# ── 力度量 compute_lidu ──
def test_compute_lidu():
    hist = [2.0, -1.0, 3.0, -4.0]
    dif = [1.0, -2.0, 0.5, -3.0]
    up = compute_lidu(hist, dif, 0, 3, U)
    assert up.area == 5.0 and up.dif_max == 1.0 and up.dif_min == -3.0   # 红柱 2+3
    dn = compute_lidu(hist, dif, 0, 3, D)
    assert dn.area == 5.0                                                  # 绿柱 1+4


# ── ★ C1：背驰只用 ≤b.end_bar 的 bar（截断 b.end 之后不改结果）──
def test_causal_no_lookahead():
    hist, dif = _macd_down()
    a = LiduSeg(0, 5, high=20, low=12, direction=D)
    b = LiduSeg(10, 15, high=15, low=10, direction=D)
    full = is_beichi(a, b, hist, dif, DEFAULT)
    # 截断到 b.end_bar(=15)：之后的 bar 清零，不应改变背驰判定
    trunc_hist = hist[:16] + [0.0] * 4
    trunc_dif = dif[:16] + [0.0] * 4
    assert is_beichi(a, b, trunc_hist, trunc_dif, DEFAULT) == full


# ── 背驰对象（provisional，confirm_bar=C段完成）──
def test_make_beichi_provisional():
    a = LiduSeg(0, 5, high=20, low=12, direction=D)
    b = LiduSeg(10, 15, high=15, low=10, direction=D)
    bc = make_beichi(a, b, "趋势")
    assert bc.status is ChanStatus.PROVISIONAL
    assert bc.confirm_bar == 15 and bc.direction is D
