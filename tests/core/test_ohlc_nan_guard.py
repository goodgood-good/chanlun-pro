"""审计 D4-HIGH-2:process_kline_values(live/walk-forward 主快路径)OHLC NaN/Inf 兜底。

原仅 volume 有 pd.isna 兜底,OHLC 裸 float() → 坏 bar 把 NaN 灌进 klines → bi/xd `.val`
比较静默失效(nan>x 与 nan<x 皆 False)+ MACD inc≠batch。修复与 _convert 的 ffill 对齐:
坏值顶上一根 → 无前根则 bfill 本 bar 任一有限 OHLC → 全非有限丢弃该 bar。
"""
import math

import pandas as pd

from chanlun.core.kline_data_processor import KlineDataProcessor


def _feed(p, i, o, h, l, c, v=1.0):
    return p.process_kline_values(
        pd.Timestamp("2024-01-01 09:30:00") + pd.Timedelta(minutes=i), o, h, l, c, v
    )


def _all_finite(k):
    return (
        math.isfinite(k.o)
        and math.isfinite(k.h)
        and math.isfinite(k.l)
        and math.isfinite(k.c)
    )


def _assert_ohlc_geometry(k):
    assert k.h >= max(k.o, k.c, k.l)
    assert k.l <= min(k.o, k.c, k.h)


def test_process_kline_values_all_nonfinite_first_bar_is_dropped():
    for value in (float("nan"), float("inf")):
        p = KlineDataProcessor()
        result = _feed(p, 0, value, value, value, value)
        assert result == []
        assert p.klines == []


def test_process_kline_batch_all_nonfinite_single_bar_is_dropped():
    for value in (float("nan"), float("inf")):
        p = KlineDataProcessor()
        df = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2024-01-01 09:30:00"),
                    "open": value,
                    "high": value,
                    "low": value,
                    "close": value,
                    "volume": 1.0,
                }
            ]
        )
        result = p.process_kline(df)
        assert result == []
        assert p.klines == []


def test_process_kline_batch_does_not_backfill_bad_first_bar_from_future():
    """首根全坏时应丢弃，不能偷看后一根价格反向补值。"""
    p = KlineDataProcessor()
    first_date = pd.Timestamp("2024-01-01 09:30:00")
    second_date = first_date + pd.Timedelta(minutes=1)
    df = pd.DataFrame(
        [
            {
                "date": first_date,
                "open": float("nan"),
                "high": float("nan"),
                "low": float("nan"),
                "close": float("nan"),
                "volume": 1.0,
            },
            {
                "date": second_date,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 2.0,
            },
        ]
    )

    result = p.process_kline(df)

    assert len(result) == 1
    assert result[0].date == second_date
    assert len(p.klines) == 1


def test_process_kline_values_missing_first_bar_is_dropped():
    """实时快路径收到缺失 OHLC 时不应抛 TypeError。"""
    p = KlineDataProcessor()

    result = _feed(p, 0, None, None, None, None)

    assert result == []
    assert p.klines == []


def test_process_kline_batch_supports_nullable_float_ohlc():
    """Pandas 可空 Float64/pd.NA 应进入同一坏值修复路径。"""
    p = KlineDataProcessor()
    df = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-01 09:30:00")],
            "open": pd.Series([pd.NA], dtype="Float64"),
            "high": pd.Series([101.0], dtype="Float64"),
            "low": pd.Series([99.0], dtype="Float64"),
            "close": pd.Series([100.5], dtype="Float64"),
            "volume": [1.0],
        }
    )

    result = p.process_kline(df)

    assert len(result) == 1
    assert _all_finite(result[0])
    assert result[0].o == 100.5


def test_process_kline_batch_bad_increment_uses_previous_bar():
    """已有历史时，批量入口应与逐根入口一样用上一根补全坏增量。"""
    p = KlineDataProcessor()
    _feed(p, 0, 100.0, 101.0, 99.0, 100.5)
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01 09:31:00"),
                "open": float("nan"),
                "high": float("nan"),
                "low": float("nan"),
                "close": float("nan"),
                "volume": 2.0,
            }
        ]
    )

    result = p.process_kline(df)

    assert len(result) == 1
    assert (result[0].o, result[0].h, result[0].l, result[0].c) == (
        100.0,
        101.0,
        99.0,
        100.5,
    )


def test_process_kline_values_nan_ohlc_does_not_enter_klines():
    p = KlineDataProcessor()
    for i in range(5):
        _feed(p, i, 100 + i, 101 + i, 99 + i, 100 + i)
    assert all(_all_finite(k) for k in p.klines)
    # 坏 bar:OHLC 全 NaN
    _feed(p, 5, float("nan"), float("nan"), float("nan"), float("nan"))
    assert all(_all_finite(k) for k in p.klines), "NaN OHLC 灌进了 klines(D4-HIGH-2 未修)"
    # Inf 同样兜底
    _feed(p, 6, float("inf"), float("inf"), float("-inf"), float("inf"))
    assert all(_all_finite(k) for k in p.klines), "Inf OHLC 灌进了 klines"
    # 后续正常 bar 不受污染
    _feed(p, 7, 106, 107, 105, 106)
    assert math.isfinite(p.klines[-1].c)


def test_process_kline_values_partial_nan_ffills_from_prev():
    p = KlineDataProcessor()
    for i in range(3):
        _feed(p, i, 100 + i, 101 + i, 99 + i, 100 + i)
    prev_c = p.klines[-1].c
    _feed(p, 3, 103, 104, 102, float("nan"))  # 仅 close=NaN → 顶上一根 close
    assert math.isfinite(p.klines[-1].c)
    assert p.klines[-1].c == prev_c


def test_process_kline_values_clean_data_unchanged():
    """干净数据零改变:走守卫旁路,OHLC 原值进 klines。"""
    p = KlineDataProcessor()
    _feed(p, 0, 100.0, 101.5, 98.5, 100.7)
    k = p.klines[-1]
    assert (k.o, k.h, k.l, k.c) == (100.0, 101.5, 98.5, 100.7)


def test_process_kline_values_repaired_high_and_low_keep_ohlc_geometry():
    """以前值补缺后仍须扩展 high/low，不能生成不可能蜡烛。"""
    p = KlineDataProcessor()
    _feed(p, 0, 100.0, 110.0, 90.0, 105.0)

    _feed(p, 1, 120.0, float("nan"), 95.0, 115.0)
    _assert_ohlc_geometry(p.klines[-1])
    assert p.klines[-1].h == 120.0

    _feed(p, 2, 80.0, 100.0, float("nan"), 85.0)
    _assert_ohlc_geometry(p.klines[-1])
    assert p.klines[-1].l == 80.0


def test_process_kline_batch_repaired_values_keep_ohlc_geometry():
    p = KlineDataProcessor()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01 09:30:00"),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1.0,
            },
            {
                "date": pd.Timestamp("2024-01-01 09:31:00"),
                "open": 120.0,
                "high": float("inf"),
                "low": 95.0,
                "close": 115.0,
                "volume": 1.0,
            },
            {
                "date": pd.Timestamp("2024-01-01 09:32:00"),
                "open": 80.0,
                "high": 100.0,
                "low": float("nan"),
                "close": 85.0,
                "volume": 1.0,
            },
        ]
    )

    result = p.process_kline(df)

    assert len(result) == 3
    for kline in result:
        _assert_ohlc_geometry(kline)
    assert result[1].h == 120.0
    assert result[2].l == 80.0


def test_process_kline_finite_but_inverted_ohlc_is_normalized():
    p = KlineDataProcessor()

    result = _feed(p, 0, 100.0, 99.0, 102.0, 101.0)

    assert len(result) == 1
    _assert_ohlc_geometry(result[0])
    assert result[0].h == 102.0
    assert result[0].l == 99.0


def test_process_kline_values_nonfinite_volume_is_zero():
    for volume in (float("inf"), float("-inf"), float("nan")):
        p = KlineDataProcessor()
        result = _feed(p, 0, 100.0, 101.0, 99.0, 100.5, volume)
        assert len(result) == 1
        assert result[0].a == 0.0
        assert math.isfinite(result[0].a)


def test_process_kline_batch_nonfinite_volume_is_zero():
    p = KlineDataProcessor()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01 09:30:00"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": float("inf"),
            }
        ]
    )

    result = p.process_kline(df)

    assert len(result) == 1
    assert result[0].a == 0.0
    assert math.isfinite(result[0].a)


def test_process_kline_batch_numeric_and_invalid_ohlc_strings_are_sanitized():
    p = KlineDataProcessor()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01 09:30:00"),
                "open": "bad",
                "high": "101.0",
                "low": "99.0",
                "close": "100.5",
                "volume": "1.0",
            }
        ]
    )

    result = p.process_kline(df)

    assert len(result) == 1
    assert _all_finite(result[0])
    _assert_ohlc_geometry(result[0])


def test_convert_defensively_coerces_object_ohlc_without_preprocess():
    """私有转换边界也应自洽，不能依赖调用方一定先完成数值化。"""
    p = KlineDataProcessor()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01 09:30:00"),
                "open": "bad",
                "high": "101.0",
                "low": "99.0",
                "close": "100.5",
                "volume": "1.0",
            }
        ]
    )

    result = p._convert(df)

    assert len(result) == 1
    assert _all_finite(result[0])
    _assert_ohlc_geometry(result[0])
