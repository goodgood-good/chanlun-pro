"""D2-F1/H3 回归: HK/currency open_* 同 code 去重(券商已持有该 code 则跳过重复开仓)。

崩溃发生在"券商已成交、本地未落盘"之间时, 重启 reconcile 仅告警不改仓, self.positions 为空;
下一 tick 策略再次给出同 code 买点, open_* 原仅判总持仓数(< b_space/poss_max)不拦 -> 二次真单,
持仓翻倍。futures.open_* 已有同 code 判(pos_long>0), HK/currency 缺失。

修复: BackTestTrader._broker_already_holds(code) 共享去重(仅查询成功且券商非空时拦截; 查询失败
不新增拦截, 不因 flaky query 阻塞), HK/currency 四个 open_* 调用它。exchange_futu/binance 依赖
futu/ccxt SDK(本环境未装)无法离线构造 trader, 故: 逻辑用基类行为测, wiring 用源码扫描。
"""
import pathlib

from chanlun.trading.backtest_trader import BackTestTrader

_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _FakeEx:
    def __init__(self, pos):
        self._pos = pos

    def positions(self, code=""):
        return self._pos


class _FailEx:
    def positions(self, code=""):
        raise RuntimeError("broker query timeout")


def _mk(ex):
    t = BackTestTrader.__new__(BackTestTrader)  # 绕过 __init__
    t.ex = ex
    t.log = None
    return t


def test_dedup_true_when_broker_holds_code():
    t = _mk(_FakeEx([{"code": "HK.00700", "amount": 100}]))
    assert t._broker_already_holds("HK.00700") is True


def test_dedup_false_when_broker_empty():
    t = _mk(_FakeEx([]))
    assert t._broker_already_holds("HK.00700") is False


def test_dedup_false_on_query_fail():
    # 查询失败不得拦截(否则 flaky query 阻塞所有开仓); 与 reconcile 的 fail 处理一致
    t = _mk(_FailEx())
    assert t._broker_already_holds("HK.00700") is False


def test_open_methods_wire_dedup_guard():
    """HK/currency 的 open_buy/open_sell 必须调用 _broker_already_holds(F1 wiring)。"""
    for rel in ("src/chanlun/trader/trader_hk_stock.py",
                "src/chanlun/trader/trader_currency.py"):
        txt = (_ROOT / rel).read_text(encoding="utf-8")
        assert txt.count("_broker_already_holds") >= 2, (
            f"{rel}: open_buy/open_sell 未接同 code 去重守卫 _broker_already_holds"
        )