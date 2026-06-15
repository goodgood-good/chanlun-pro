# -*- coding: utf-8 -*-
"""`_find_recent_1mmd_lines`(2买锚点倒扫)→ 索引版增量化的**等价性**单测。

黄金主大网只产 3 类点、测不到 2 买路径,故这里用合成线序直接钉死:
`_recent_1mmd_from_pool`(O(log) 索引)对所有前缀、两方向,都与真实
`_find_recent_1mmd_lines`(reversed 倒扫)返回**完全相同**的结果。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"
                       if (Path(__file__).resolve().parents[2] / "src").exists()
                       else Path(__file__).resolve().parents[2] / "src"))
from chanlun.core.bs_point_calculator import BsPointCalculator as B  # noqa: E402

ZT = "zs_type_bz"


def _mk(idx, typ, has_1mmd):
    name = "1buy" if typ == "down" else "1sell"
    mmds = [SimpleNamespace(name=name)] if has_1mmd else []
    return SimpleNamespace(index=idx, type=typ, zs_type_mmds={ZT: mmds})


def _ref(prev_lines, target_type):
    # 真实方法,fake self 仅需 .zs_type
    return B._find_recent_1mmd_lines(SimpleNamespace(zs_type=ZT), prev_lines, target_type, 3)


def test_recent_1mmd_from_pool_equiv_to_scan():
    # 覆盖:交替/疏(长段无1类)/连续同向/末尾密集
    spec = [
        ("down", 1), ("up", 0), ("down", 0), ("up", 1), ("down", 1), ("up", 1),
        ("down", 0), ("down", 1), ("up", 0), ("up", 1), ("down", 0), ("down", 0),
        ("up", 0), ("down", 1), ("up", 1), ("down", 1), ("down", 1), ("up", 1),
    ]
    lines = [_mk(i, t, h) for i, (t, h) in enumerate(spec)]
    pools, keys = B._build_1mmd_pools(lines, ZT)
    for i in range(len(lines) + 1):
        for t in ("down", "up"):
            got = B._recent_1mmd_from_pool(pools, keys, t, i)
            exp = _ref(lines[:i], t)
            assert got == exp, (
                f"i={i} t={t}: got={[l.index for l in got]} exp={[l.index for l in exp]}"
            )


def test_recent_1mmd_empty_and_unknown_type():
    lines = [_mk(0, "down", 0), _mk(1, "up", 0)]  # 无 1 类
    pools, keys = B._build_1mmd_pools(lines, ZT)
    assert B._recent_1mmd_from_pool(pools, keys, "down", 2) == []
    assert B._recent_1mmd_from_pool(pools, keys, "weird", 2) == []
