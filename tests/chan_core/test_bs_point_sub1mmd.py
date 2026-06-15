# -*- coding: utf-8 -*-
"""`_find_subordinate_1mmd_in_window`(xd 层定律一·次级别1类查找)→ 索引版的**等价性**单测。

黄金主大网测得到 xd 层 2 买,但合成线序能更密地穷举窗口边界,故这里直接钉死:
`_sub_1mmd_from_index`(O(log) bisect 窗口查找)对所有 now_line 窗口 × 两方向 ×
多档容差,都与真实 scan 版 `_find_subordinate_1mmd_in_window` 返回**同一个对象**。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from chanlun.core.bs_point_calculator import BsPointCalculator as B  # noqa: E402


def _mk_bi(index, typ, end_k, mmd_names):
    mmds = [SimpleNamespace(name=n) for n in mmd_names]
    return SimpleNamespace(
        index=index, type=typ,
        end=SimpleNamespace(k=SimpleNamespace(k_index=end_k)),
        zs_type_mmds={'bi': mmds},
    )


def _mk_line(start_k, end_k, typ):
    return SimpleNamespace(
        type=typ,
        start=SimpleNamespace(k=SimpleNamespace(k_index=start_k)),
        end=SimpleNamespace(k=SimpleNamespace(k_index=end_k)),
    )


def _scan_self(bis):
    # scan 版只用到 self.zs_type 与 self.cl.get_bis()
    return SimpleNamespace(zs_type='xd', cl=SimpleNamespace(get_bis=lambda: bis))


# 合成笔序:end_k 严格升序(相邻笔共享端点的真实约束);覆盖
# 命中向/错向 1类、错 mmd、无 mmd、同 end_k 多匹配(末根覆盖语义)。
_BIS = [
    _mk_bi(0, 'down', 5,  ['1buy']),          # down 命中
    _mk_bi(1, 'up',   10, ['1sell']),         # up 命中
    _mk_bi(2, 'down', 15, ['1sell']),         # 错 mmd(down 桶不收)
    _mk_bi(3, 'up',   20, []),                # 无 mmd
    _mk_bi(4, 'down', 25, ['2buy', '1buy']),  # down 命中(多 mmd)
    _mk_bi(5, 'down', 25, ['1buy']),          # 同 end_k 再一根 down 命中 → 末根覆盖
    _mk_bi(6, 'up',   30, ['1sell']),         # up 命中
    _mk_bi(7, 'down', 35, ['1buy']),          # down 命中
]


def test_sub1mmd_index_equiv_to_scan():
    fake = _scan_self(_BIS)
    index = B._build_sub_1mmd_index(_BIS)
    end_ks = [b.end.k.k_index for b in _BIS]
    sweep = sorted(set([k for k in end_ks] + [k - 1 for k in end_ks]
                       + [k + 1 for k in end_ks] + [0, 4, 26, 40]))
    checked = 0
    for typ in ('down', 'up'):
        for end_k in sweep:
            for start_k in (0, end_k - 12, end_k - 6, end_k):
                if start_k > end_k:
                    continue
                now = _mk_line(start_k, end_k, typ)
                for tol in (0, 1, 3, 100):
                    exp = B._find_subordinate_1mmd_in_window(fake, now, typ, tol)
                    got = B._sub_1mmd_from_index(index, now, typ, tol)
                    assert got is exp, (
                        f"typ={typ} start_k={start_k} end_k={end_k} tol={tol}: "
                        f"got={getattr(got,'index',None)} exp={getattr(exp,'index',None)}"
                    )
                    checked += 1
    assert checked > 500  # 确实穷举了足够多窗口


def test_sub1mmd_empty_and_degenerate():
    # 空笔序 / 无命中 / 端点缺失,index 与 scan 同为 None
    empty_idx = B._build_sub_1mmd_index([])
    now = _mk_line(0, 10, 'down')
    assert B._sub_1mmd_from_index(empty_idx, now, 'down') is None
    assert B._find_subordinate_1mmd_in_window(_scan_self([]), now, 'down') is None

    # now_line 端点缺失(start=None)→ AttributeError 路径,两者皆 None
    bad = SimpleNamespace(type='down', start=None,
                          end=SimpleNamespace(k=SimpleNamespace(k_index=10)))
    idx = B._build_sub_1mmd_index(_BIS)
    assert B._sub_1mmd_from_index(idx, bad, 'down') is None
    assert B._find_subordinate_1mmd_in_window(_scan_self(_BIS), bad, 'down') is None
