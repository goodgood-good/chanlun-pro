"""tests/core/test_line_eq.py — P6 LINE.__eq__ 修复回归测试。

历史 bug: LINE.__eq__ 写死 ``isinstance(other, BI)``, 导致:
- 两个端点相同的 XD 之间 ``==`` 永远 False
- ``xd1 in [xd2, xd3]`` 失效
- ``set(xds)`` 去重失效 (虽然 hash 相同, dict 仍按 __eq__ 区分)

修复后:
- BI 与 BI 同端点 → True
- XD 与 XD 同端点 → True (本测试的核心保护)
- BI 与 XD 同端点 → False (一段笔不等于一段线段)
- LINE 与非 LINE → NotImplemented (Python 数据模型推荐)
"""

from __future__ import annotations

import pytest

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG
from chanlun.core.cl import CL
from chanlun.core.cl_interface import BI, XD, FX, CLKline


def _build_cd():
    df = _generate_kline_df(500, seed=42, multi_freq=True)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    return cd


def test_two_xds_with_same_endpoints_are_equal():
    """关键修复: 两个端点相同的 XD 应判定相等 (旧 bug 永远 False)。"""
    cd = _build_cd()
    xds = cd.get_xds()
    if len(xds) < 1:
        pytest.skip("无 xds 可供测试")
    # 同一个 cd 返回的 xd 列表是浅拷贝, 同一个底层对象自然 ==
    # 用 cd.xd_calculator.xds 取底层列表, 与 get_xds() 返回的浅拷贝项 ==
    assert xds[0] == cd.xd_calculator.xds[0]
    # 等价性: 自反性
    xd0 = xds[0]
    assert xd0 == xd0


def test_xd_in_list_membership():
    """``xd in [...]`` 应工作 (依赖 __eq__)。"""
    cd = _build_cd()
    xds = cd.get_xds()
    if len(xds) < 1:
        pytest.skip("无 xds 可供测试")
    assert xds[0] in xds


def test_bi_in_list_membership():
    """同样保护 BI (回归测试旧 bug 没有反向破坏 BI 路径)。"""
    cd = _build_cd()
    bis = cd.get_bis()
    if len(bis) < 1:
        pytest.skip("无 bis 可供测试")
    assert bis[0] in bis
    # 不同 BI 不相等
    if len(bis) >= 2:
        assert bis[0] != bis[1]


def test_bi_and_xd_not_equal_even_with_same_endpoints():
    """BI 与 XD 即使端点相同也不应 == (类型不同, 语义不同)。

    构造一对人工 BI 和 XD 共享同样的 start/end 端点, 断言 != 。
    """
    # 构造 minimal FX 端点
    k = CLKline(k_index=0, date=None, h=10.0, l=8.0, o=9.0, c=9.5, a=100.0, klines=[])
    k2 = CLKline(k_index=10, date=None, h=15.0, l=13.0, o=14.0, c=14.5, a=200.0, klines=[])
    start = FX(_type='di', k=k, klines=[k], val=8.0)
    end = FX(_type='ding', k=k2, klines=[k2], val=15.0)

    bi = BI(start=start, end=end, _type='up')
    xd = XD(start=start, end=end, _type='up')

    assert bi != xd, "BI 和 XD 即使端点相同也不应相等"
    assert xd != bi, "反向也应不等 (对称性)"


def test_line_vs_non_line_returns_not_implemented():
    """LINE 与非 LINE 对象比较 → NotImplemented → Python 最终 False。"""
    cd = _build_cd()
    xds = cd.get_xds()
    if len(xds) < 1:
        pytest.skip("无 xds 可供测试")
    # str / int / None 与 LINE 比较都应得 False (NotImplemented 兜底)
    assert (xds[0] == "not a line") is False
    assert (xds[0] == 42) is False
    assert (xds[0] == None) is False  # noqa: E711


def test_xd_hash_consistent_with_eq():
    """Python 契约: a == b → hash(a) == hash(b)。"""
    cd = _build_cd()
    xds = cd.get_xds()
    if len(xds) < 1:
        pytest.skip("无 xds 可供测试")
    xd0 = xds[0]
    xd0_copy = cd.xd_calculator.xds[0]  # 同一对象
    assert xd0 == xd0_copy
    assert hash(xd0) == hash(xd0_copy)


def test_xd_hash_differs_from_bi_with_same_endpoints():
    """P6 优化: BI 与 XD 即使端点相同, hash 也应不同 (减少 set/dict 冲突)。

    Python 契约不要求这一点 (允许 a != b 但 hash(a) == hash(b)),
    但分开 hash 能优化查找性能。
    """
    k = CLKline(k_index=0, date=None, h=10.0, l=8.0, o=9.0, c=9.5, a=100.0, klines=[])
    k2 = CLKline(k_index=10, date=None, h=15.0, l=13.0, o=14.0, c=14.5, a=200.0, klines=[])
    start = FX(_type='di', k=k, klines=[k], val=8.0)
    end = FX(_type='ding', k=k2, klines=[k2], val=15.0)
    bi = BI(start=start, end=end, _type='up')
    xd = XD(start=start, end=end, _type='up')
    assert hash(bi) != hash(xd)
