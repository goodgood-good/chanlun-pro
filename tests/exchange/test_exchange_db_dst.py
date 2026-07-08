"""Round12 LOW-3: ExchangeDB 货币市场 self.tz=get_localzone()(本机时区); 部署在观测 DST 的服务器
(US/Eastern/欧洲)时, 加密货币 7x24 有春令时那天 02:00-03:00 不存在的墙钟时刻的 bar → tz_localize
默认 nonexistent='raise' → NonExistentTimeError → klines() 崩。加 nonexistent='shift_forward' 修复。
(中国 Asia/Shanghai 无 DST 对用户 dormant, 属可移植性硬化。)"""

import pandas as pd
import pytz

from chanlun.exchange import exchange_db as edb


class _FakeK:
    def __init__(self, dt):
        self.code = "BTC/USDT"
        self.dt = dt
        self.o = self.h = self.l = self.c = 100.0
        self.v = 1.0
        self.p = 0.0


def test_currency_klines_dst_spring_forward_no_crash(monkeypatch):
    ex = edb.ExchangeDB("currency")
    # 模拟部署在观测 DST 的服务器: 覆盖 tz 为 US/Eastern(有 DST)
    ex.tz = pytz.timezone("US/Eastern")
    # 2024-03-10 02:30 在 US/Eastern 春令时(02:00→03:00)不存在
    monkeypatch.setattr(
        edb.db,
        "klines_query",
        lambda *a, **k: [_FakeK(pd.Timestamp("2024-03-10 02:30:00"))],
    )
    # 修复前: nonexistent 默认 raise → NonExistentTimeError 崩; 修复后: shift_forward 不崩
    df = ex.klines("BTC/USDT", "1m")
    assert len(df) == 1