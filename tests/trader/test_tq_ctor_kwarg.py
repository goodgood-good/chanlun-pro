"""D2-F2 回归: TraderFutures / reboot_trader_futures 构造 ExchangeTq 的关键字必须有效。

exchange_tq.py 的 __init__ 形参是 use_simulate_account(True=快期模拟账号=Demo 语义,
False=config 实盘账号); 旧名 use_account 已不存在。调用点若传 ExchangeTq(use_account=True),
fun.singleton 透传 kwargs 不吞错 -> 首次构造即 TypeError -> 期货交易链 DOA
(reboot_trader_futures.py 首行即构造必崩; 且 H4 成交量修复代码永远跑不到)。

本环境未装 tqsdk, exchange_tq 顶层 import tqsdk 直接 ImportError, 无法离线构造/内省
(conftest 与 test_exchange_tq_fill 亦回避 import)。故用源码扫描钉死这一确定性回归。
"""
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CALLERS = [
    "src/chanlun/trader/trader_futures.py",
    "script/trader/reboot_trader_futures.py",
]


def test_tq_callers_do_not_pass_removed_use_account_kwarg():
    for rel in _CALLERS:
        txt = (_ROOT / rel).read_text(encoding="utf-8")
        assert "use_account=" not in txt, (
            f"{rel}: 仍用 ExchangeTq(use_account=...), 但 __init__ 形参是 use_simulate_account "
            f"-> 首次构造 TypeError 期货链 DOA。应改 use_simulate_account=(Demo 语义 True=模拟账号)。"
        )