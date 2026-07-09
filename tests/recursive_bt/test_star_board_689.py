"""R8-#1: 科创板 CDR 689xxx 被判为主板(±10%)而非科创板(±20%)。

market_rules_for_code:157 `num[:3] in ("688","300","301")` 与三处 ashare_board
(market_runtime/fetch/fundamentals, 均 `num.startswith("688")`)只认 688 漏 689,
致 SH.689009(九号公司,科创板 CDR,真实 ±20%)涨跌停闸误判(paper 漏入场/卡止损、
portfolio 经 pkl limit_pct 同款错判)。修复=四处补 689。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

from chanlun.recursive_bt.engine import market_runtime as mr  # noqa: E402
from chanlun.recursive_bt.data import fetch  # noqa: E402
from chanlun.recursive_bt.engine import fundamentals  # noqa: E402


def test_star_cdr_689_is_gem_rules():
    # 科创板 CDR 689 应走 A_GEM(±20%), 非 A_STOCK(±10%)
    assert mr.market_rules_for_code("a", "SH.689009").limit_pct == 0.20


def test_star_688_still_gem():
    assert mr.market_rules_for_code("a", "SH.688981").limit_pct == 0.20


def test_main_board_unchanged():
    assert mr.market_rules_for_code("a", "SZ.000001").limit_pct == 0.10


def test_ashare_board_689_is_star_all_copies():
    assert mr.ashare_board("SH.689009") == "star"
    assert fetch.ashare_board("SH.689009") == "star"
    assert fundamentals.ashare_board("SH.689009") == "star"


def test_ashare_board_688_still_star():
    assert mr.ashare_board("SH.688981") == "star"
    assert fetch.ashare_board("SH.688981") == "star"
    assert fundamentals.ashare_board("SH.688981") == "star"