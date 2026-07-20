"""Guard the CL signal surface against dropping recursive L1+ first-class points."""

from types import SimpleNamespace

from chanlun.core.bs1_branch import Bs1BranchCalculator
from chanlun.core.bs2_branch import Bs2BranchCalculator
from chanlun.core.bs3_branch import Bs3BranchCalculator
from chanlun.core.bs_branch import BsBranchCalculator
from chanlun.core.cl import CL


def test_get_branch_bspoints_includes_multilevel_first_class(monkeypatch) -> None:
    level_zero = SimpleNamespace(
        level=0,
        zss=[],
        done_divergence=[],
        units=[],
    )
    level_one = SimpleNamespace(
        level=1,
        zss=[],
        done_divergence=[],
        units=[],
    )
    l0_point = SimpleNamespace(bs_type="1buy", level=None)
    l1_point = SimpleNamespace(bs_type="1buy", level=1)

    monkeypatch.setattr(
        BsBranchCalculator,
        "calculate",
        lambda self, result, units: [l0_point],
    )
    monkeypatch.setattr(
        BsBranchCalculator,
        "second_class",
        lambda self, result, units, provider, frequency: [],
    )
    monkeypatch.setattr(
        Bs1BranchCalculator,
        "calculate",
        lambda self, levels: [l1_point],
    )
    monkeypatch.setattr(
        Bs2BranchCalculator,
        "calculate",
        lambda self, levels, provider, frequency: [],
    )
    monkeypatch.setattr(
        Bs3BranchCalculator,
        "calculate",
        lambda self, levels: [],
    )

    fake_cl = SimpleNamespace(
        _recursive_memo={},
        frequency="1m",
        get_recursive_branch_levels_for_tower=lambda *, use_xd: [
            level_zero,
            level_one,
        ],
    )

    points = CL.get_branch_bspoints(fake_cl, use_xd=True)

    assert points == [l0_point, l1_point]

