"""R6-#5: xg_single_xd_bi_23_overlapped 的 and/or 优先级 bug。

`A and B and overlapped_23_bi or overlapped_23_bi_2 or overlapped_23_bi_3` 解析为
`(A and B and overlapped_23_bi) or overlapped_23_bi_2 or overlapped_23_bi_3`, 使线段方向(A)
与首中枢对齐(B)门槛只约束 overlapped_23_bi 分支; overlapped_23_bi_2/_3 为真时无视方向直接命中
→ 假阳性选股(docstring 要求"上涨线段的第一个笔中枢")。修复=加括号 `A and B and (C or D or E)`。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

from chanlun.xuangu.xuangu import xg_single_xd_bi_23_overlapped  # noqa: E402


class _Start:
    def __init__(self, index):
        self.index = index


class _Xd:
    def __init__(self, type_, start_index):
        self.type = type_
        self.start = _Start(start_index)


class _Bi:
    def __init__(self, mmds):
        self._mmds = set(mmds)

    def mmd_exists(self, wanted):
        return any(w in self._mmds for w in wanted)


class _Line:
    def __init__(self, start_index):
        self.start = _Start(start_index)


class _Zs:
    def __init__(self, first_line_start_index):
        self.lines = [_Line(first_line_start_index)]


class _Cd:
    def __init__(self, xd, zs, bis):
        self._xd, self._zs, self._bis = xd, zs, bis

    def get_xds(self):
        return [self._xd]

    def get_bi_zss(self):
        return [self._zs]

    def get_bis(self):
        return self._bis

    def get_code(self):
        return "TEST"


class _MkDatas:
    def __init__(self, cd):
        self._cd = cd
        self.frequencys = ["5m"]

    def get_cl_data(self, code, freq):
        return self._cd


def _mk(xd_type, xd_start, zs_start, bi, bi_2, bi_3):
    # get_bis()[-1]=bi, [-2]=bi_2, [-3]=bi_3
    cd = _Cd(_Xd(xd_type, xd_start), _Zs(zs_start), [bi_3, bi_2, bi])
    return _MkDatas(cd)


def test_down_xd_with_overlap3_not_selected():
    # xd 向下(A False)但 bi_3 挂 2买+3买、bi 挂 l3买 → overlapped_23_bi_3=True。
    # 修复前 and/or 优先级使 overlapped_23_bi_3 旁路方向门槛误命中; 修复后应返 None。
    mk = _mk("down", 0, 0, _Bi(["l3buy"]), _Bi([]), _Bi(["2buy", "3buy"]))
    assert xg_single_xd_bi_23_overlapped("TEST", mk) is None


def test_up_xd_aligned_with_overlap3_selected():
    # 合法路径: xd 向上 + 起点对齐首中枢首段(B True) + overlapped_23_bi_3 → 命中(不回归)。
    mk = _mk("up", 5, 5, _Bi(["l3buy"]), _Bi([]), _Bi(["2buy", "3buy"]))
    res = xg_single_xd_bi_23_overlapped("TEST", mk)
    assert res is not None
    assert res["code"] == "TEST"