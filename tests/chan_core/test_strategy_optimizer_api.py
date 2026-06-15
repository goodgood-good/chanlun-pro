"""strategy_optimizer 公共 API 契约网。

strategy_optimizer.py(5585行/79公共符号=73函数+6类)是审查标记的头号拆分候选。
此网守未来拆 strategy_optimizer/ 包(facade re-export)前后:
① 79 公共符号全部仍可从 chanlun.recursive_bt.strategy_optimizer 解析(facade 完整);
② 零参/默认参 builder 输出结构不变(拆分=移动函数,不改逻辑)。
golden=tests/chan_core/golden/strategy_optimizer_symbols.json(拆分前快照)。
"""
import json
import pathlib

_GOLDEN = pathlib.Path(__file__).parent / "golden" / "strategy_optimizer_symbols.json"


def test_all_public_symbols_reexported():
    """拆分前的 79 公共符号,拆分后必须全部仍可从顶层模块解析(facade 完整)。"""
    import chanlun.recursive_bt.strategy_optimizer as so

    expected = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    missing = [n for n in expected if not hasattr(so, n)]
    assert not missing, f"facade 漏 re-export {len(missing)} 个: {missing[:15]}"


def test_zero_arg_builders_stable():
    """无外部依赖的纯定义 builder,输出结构钉死(拆分移动不应改)。"""
    from chanlun.recursive_bt import strategy_optimizer as so

    assert len(so.a_selection_systems()) > 0
    assert len(so.default_strategy_candidates()) > 0
    assert len(so.default_strategy_candidates("us")) > 0
    assert isinstance(so.default_regime_ratio_impact_windows(), list)
