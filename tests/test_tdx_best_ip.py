"""L17 回归:select_best_ip 在所有候选 IP 都不可用时的行为。

select_best_ip 对每个候选 IP 调 ping;ping 返回哨兵 ``timedelta(9, 9, 0)``
表示不可用。若全部不可用,results 为空 —— 旧代码 ``return results[0]``
直接抛裸 IndexError(信息不明)。修复后应抛语义清晰的异常。
"""

from __future__ import annotations

import datetime

import pytest

from chanlun.tools import tdx_best_ip


def test_select_best_ip_all_unavailable_raises_clear_error(monkeypatch):
    """全部 IP 不可用 → 抛出明确异常,而非裸 IndexError。"""
    # ping 一律返回"不可用"哨兵
    monkeypatch.setattr(
        tdx_best_ip,
        "ping",
        lambda *_a, **_k: datetime.timedelta(9, 9, 0),
    )
    with pytest.raises(ConnectionError):
        tdx_best_ip.select_best_ip("stock")
